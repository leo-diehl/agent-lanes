from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path

from .config import RuntimeConfig, load_config, load_task_file
from .defaults import (
    DEFAULT_CLAIM_LEASE_SECONDS,
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_NEXT_TIMEOUT_SECONDS,
    DEFAULT_WAIT_TIMEOUT_SECONDS,
)
from .errors import HandoffError, TimeoutError
from .selftest import run_self_test
from .server import serve
from .store import HandoffStore


# --- init ---------------------------------------------------------------------

INIT_PLACEHOLDER_WORKSPACE_ID = "{{WORKSPACE_ID}}"
INIT_PLACEHOLDER_WORKSPACE_ROOT = "{{WORKSPACE_ROOT}}"
INIT_PLACEHOLDER_QUEUE_ROOT = "{{QUEUE_ROOT}}"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "self-test":
            print(run_self_test())
            return 0
        if args.command == "init":
            return _cmd_init(args)
        if args.command is None:
            parser.print_help()
            return 0

        # All other commands need engine config + store.
        config = _load_config_for_command(args)
        store = HandoffStore(args.store or config.store_root)

        if args.command == "submit":
            return _cmd_submit(args, config, store)
        if args.command == "wait":
            return _cmd_wait(args, config, store)
        if args.command in {"next", "watch"}:
            task = _wait_for_lane_task(
                store,
                args.lane,
                timeout=args.timeout,
                heartbeat_seconds=args.heartbeat_seconds,
                command=args.command,
                quiet=args.quiet,
            )
            if task is None:
                _print_idle(args.lane, args.timeout, args.json, store)
                return 0
            _print({"status": "task_available", "lane": args.lane, "task": task}, args.json)
            return 0
        if args.command == "claim":
            task = store.claim_task(args.task_id, owner=args.owner, lease_seconds=args.lease_seconds)
            _print_claim(task, args.json)
            return 0
        if args.command == "renew":
            task = store.renew_claim(
                args.task_id,
                claim_token=args.claim_token,
                lease_seconds=args.lease_seconds,
            )
            _print({"status": "renewed", "task_id": task["id"], "claim_token": task["claim_token"], "task": task}, args.json)
            return 0
        if args.command == "release":
            task = store.release_claim(
                args.task_id,
                claim_token=args.claim_token,
                reason=args.reason,
            )
            _print(
                {"status": "released", "task_id": task["id"], "task": task},
                args.json,
            )
            return 0
        if args.command == "respond":
            body = _response_body(args)
            metadata = _parse_metadata(args.metadata or [])
            response = store.submit_response(
                args.task_id,
                body=body,
                reviewer=args.reviewer,
                claim_token=args.claim_token,
                status=args.status,
                follow_up_required=args.follow_up_required,
                verdict=args.verdict,
                blocking_count=args.blocking_count,
                nonblocking_count=args.nonblocking_count,
                expect_sha256=args.expect_sha256,
                metadata=metadata,
            )
            task = store.get_task(args.task_id)
            _print_response(response, task, store, args.json)
            return 0
        if args.command == "status":
            if args.rack:
                tasks = store.list_tasks(lane=args.lane, include_completed=not args.active_only)
                _print(_rack_status_payload(tasks, store), args.json)
                return 0
            if not args.task_id:
                raise HandoffError("status requires a task id or --rack")
            task_id = store.resolve_ref(args.task_id, config)
            _print({"status": "ok", "task": _task_status_view(store.get_task(task_id), store)}, args.json)
            return 0
        if args.command == "list":
            tasks = store.list_tasks(lane=args.lane, include_completed=not args.active_only)
            _print({"status": "ok", "tasks": [_task_status_view(task, store) for task in tasks], "count": len(tasks)}, args.json)
            return 0
        if args.command == "serve":
            print(f"serving agent-lanes on {args.host}:{args.port}", file=sys.stderr)
            serve(store, host=args.host, port=args.port)
            return 0
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except HandoffError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-lanes")
    parser.add_argument("--config", default="handoff/handoff.yaml", help="path to engine handoff.yaml")
    parser.add_argument("--store", help="override queue/store root")
    parser.add_argument("--json", action="store_true", help="print JSON instead of concise text")
    sub = parser.add_subparsers(dest="command")
    common_output = argparse.ArgumentParser(add_help=False)
    common_output.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")

    # init -------------------------------------------------------------
    init_cmd = sub.add_parser(
        "init",
        parents=[common_output],
        help="scaffold a handoff/ engine folder in the current directory or PATH",
    )
    init_cmd.add_argument("path", nargs="?", default=".", help="target directory (default: cwd)")
    init_cmd.add_argument("--workspace-id", default=None, help="workspace id (default: target dir basename)")
    init_cmd.add_argument(
        "--workspace-root",
        default="..",
        help="workspace root recorded in handoff.yaml, relative to handoff/ (default: ..)",
    )
    init_cmd.add_argument(
        "--queue-root",
        default="state",
        help="queue root recorded in handoff.yaml; absolute or relative path is preserved verbatim (default: state)",
    )

    # submit -----------------------------------------------------------
    submit = sub.add_parser(
        "submit",
        parents=[common_output],
        help="orchestrator: queue a task from --task <path> and/or inline flags",
    )
    submit.add_argument("--task", dest="task_path", help="path to a task definition YAML/JSON")
    submit.add_argument("--lane", help="lane name (overrides task file)")
    submit.add_argument("--request-from", dest="request_from", help="path to the request artifact")
    submit.add_argument("--response-to", dest="response_to", help="path where the response body should be written")
    submit.add_argument("--prompt", help="inline prompt body")
    submit.add_argument("--prompt-file", dest="prompt_file", help="path to a file containing the prompt body")
    submit.add_argument("--worktree-path", dest="worktree_path", help="optional worktree path")
    submit.add_argument("--branch", help="optional expected branch")
    submit.add_argument(
        "--metadata",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="free-form metadata; repeatable",
    )
    submit.add_argument("--source-agent", default="orchestrator")
    submit.add_argument(
        "--task-id",
        dest="task_correlation_id",
        help="optional correlation id to embed in the task id (default: derived from lane or task file)",
    )

    # wait -------------------------------------------------------------
    wait = sub.add_parser(
        "wait",
        parents=[common_output],
        help="orchestrator: wait for a task's response; monitor: wait --lane for task JSON",
    )
    wait.add_argument("task_id", nargs="?")
    wait.add_argument("--lane", help="wait for the next available task on a lane")
    wait.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT_SECONDS)
    wait.add_argument("--heartbeat-seconds", type=float, default=DEFAULT_HEARTBEAT_SECONDS)
    wait.add_argument("--quiet", action="store_true", help="suppress keepalive messages while waiting")

    # next/watch -------------------------------------------------------
    next_cmd = sub.add_parser("next", parents=[common_output], help="monitor: long-poll for task JSON on a lane")
    next_cmd.add_argument("--lane", required=True)
    next_cmd.add_argument("--timeout", type=float, default=DEFAULT_NEXT_TIMEOUT_SECONDS)
    next_cmd.add_argument("--heartbeat-seconds", type=float, default=DEFAULT_HEARTBEAT_SECONDS)
    next_cmd.add_argument("--quiet", action="store_true", help="suppress keepalive messages while waiting")

    watch_cmd = sub.add_parser("watch", parents=[common_output], help="long-poll for the next task on a lane")
    watch_cmd.add_argument("--lane", required=True)
    watch_cmd.add_argument("--timeout", type=float, default=DEFAULT_NEXT_TIMEOUT_SECONDS)
    watch_cmd.add_argument("--heartbeat-seconds", type=float, default=DEFAULT_HEARTBEAT_SECONDS)
    watch_cmd.add_argument("--quiet", action="store_true", help="suppress keepalive messages while waiting")

    # claim/renew/release ---------------------------------------------
    claim = sub.add_parser("claim", parents=[common_output], help="reviewer: claim one task id before reviewing")
    claim.add_argument("task_id")
    claim.add_argument("--owner", default="worker")
    claim.add_argument("--lease-seconds", type=float, default=DEFAULT_CLAIM_LEASE_SECONDS)

    renew = sub.add_parser("renew", parents=[common_output])
    renew.add_argument("task_id")
    renew.add_argument("--claim-token", required=True)
    renew.add_argument("--lease-seconds", type=float, default=DEFAULT_CLAIM_LEASE_SECONDS)

    release = sub.add_parser(
        "release",
        parents=[common_output],
        help="reviewer: return a claimed task to queued without responding",
    )
    release.add_argument("task_id")
    release.add_argument("--claim-token", required=True)
    release.add_argument("--reason", default=None)

    # respond ----------------------------------------------------------
    respond = sub.add_parser("respond", parents=[common_output], help="reviewer: submit one claimed task response")
    respond.add_argument("task_id")
    respond.add_argument("--file", help="copy this file's contents into response.json; use - for stdin")
    respond.add_argument("--body", help="inline response body")
    respond.add_argument("--reviewer", default="worker")
    respond.add_argument("--claim-token")
    respond.add_argument("--status", default="completed", choices=["completed", "failed"])
    respond.add_argument("--follow-up-required", action="store_true")
    respond.add_argument(
        "--verdict",
        choices=["accept", "accept-with-follow-ups", "needs-revision"],
        default=None,
        help="optional review verdict; tasks not tied to review (Q&A, delegation, pipeline) may omit",
    )
    respond.add_argument("--blocking-count", type=int)
    respond.add_argument("--nonblocking-count", type=int)
    respond.add_argument("--expect-sha256", help="request_sha256 the reviewer actually reviewed")
    respond.add_argument(
        "--metadata",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="free-form metadata on the response; repeatable",
    )

    # status/list ------------------------------------------------------
    status = sub.add_parser("status", parents=[common_output], help="diagnose one task or the whole rack")
    status.add_argument("task_id", nargs="?")
    status.add_argument("--rack", action="store_true", help="summarize tasks in this rack")
    status.add_argument("--lane")
    status.add_argument("--active-only", action="store_true")

    list_cmd = sub.add_parser("list", parents=[common_output], help="inspect tasks without claiming")
    list_cmd.add_argument("--lane")
    list_cmd.add_argument("--active-only", action="store_true")

    serve_cmd = sub.add_parser("serve", parents=[common_output])
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8765)

    sub.add_parser("self-test", parents=[common_output])
    return parser


# --- helpers ------------------------------------------------------------------


def _load_config_for_command(args: argparse.Namespace) -> RuntimeConfig:
    return load_config(args.config, store_override=args.store)


def _parse_metadata(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise HandoffError(f"--metadata expects KEY=VALUE, got: {item}")
        key, value = item.split("=", 1)
        result[key.strip()] = value
    return result


def _response_body(args: argparse.Namespace) -> str:
    if args.file:
        if args.file == "-":
            return sys.stdin.read()
        return Path(args.file).read_text(encoding="utf-8")
    if args.body is not None:
        return args.body
    return sys.stdin.read()


def _cmd_submit(args: argparse.Namespace, config: RuntimeConfig, store: HandoffStore) -> int:
    # Merge task-file defaults with CLI overrides.
    fields: dict[str, object] = {}
    metadata: dict[str, object] = {}
    if args.task_path:
        task_data = load_task_file(args.task_path)
        for key in ("lane", "prompt", "prompt_file", "request_from", "response_to", "supporting_paths", "worktree_path", "branch"):
            if task_data.get(key) is not None:
                fields[key] = task_data[key]
        if isinstance(task_data.get("metadata"), dict):
            metadata.update(task_data["metadata"])

    # CLI overrides
    if args.lane:
        fields["lane"] = args.lane
    if args.request_from:
        fields["request_from"] = args.request_from
    if args.response_to:
        fields["response_to"] = args.response_to
    if args.prompt is not None:
        fields["prompt"] = args.prompt
        # explicit --prompt overrides any prompt_file from the task file
        fields.pop("prompt_file", None)
    if args.prompt_file:
        fields["prompt_file"] = args.prompt_file
        fields.pop("prompt", None)
    if args.worktree_path:
        fields["worktree_path"] = args.worktree_path
    if args.branch:
        fields["branch"] = args.branch

    cli_metadata = _parse_metadata(args.metadata or [])
    metadata.update(cli_metadata)

    lane = fields.get("lane")
    if not lane:
        raise HandoffError("submit requires --lane (either inline or via --task)")
    request_from = fields.get("request_from")
    if not request_from:
        raise HandoffError("submit requires --request-from (either inline or via --task)")
    response_to = fields.get("response_to")
    if not response_to:
        raise HandoffError("submit requires --response-to (either inline or via --task)")

    # Resolve prompt body.
    prompt_body = ""
    if "prompt" in fields and fields["prompt"] is not None:
        prompt_body = str(fields["prompt"])
    elif "prompt_file" in fields and fields["prompt_file"] is not None:
        prompt_path = Path(str(fields["prompt_file"])).expanduser()
        if not prompt_path.is_absolute():
            prompt_path = (Path.cwd() / prompt_path).resolve()
        prompt_body = prompt_path.read_text(encoding="utf-8")

    # Resolve worktree/branch
    worktree_path = fields.get("worktree_path") or (
        str(config.worktree_path) if config.worktree_path else None
    )
    expected_branch = fields.get("branch") or config.expected_branch

    # checkpoint_id: use --task-id, or task-file basename, or lane-derived
    correlation = args.task_correlation_id
    if not correlation:
        if args.task_path:
            correlation = Path(args.task_path).stem
        else:
            correlation = str(lane)

    task = store.create_task(
        workspace_id=config.workspace_id,
        checkpoint_id=correlation,
        source_agent=args.source_agent,
        lane=str(lane),
        workspace_root=config.workspace_root,
        worktree_path=worktree_path,
        expected_branch=expected_branch,
        request_path=str(request_from),
        response_path=str(response_to),
        prompt=prompt_body,
        supporting_paths=fields.get("supporting_paths") or [],
        metadata=metadata,
    )
    _print({"task_id": task["id"], "task": task}, args.json)
    return 0


def _cmd_wait(args: argparse.Namespace, config: RuntimeConfig, store: HandoffStore) -> int:
    if args.lane:
        task = _wait_for_lane_task(
            store,
            args.lane,
            timeout=args.timeout,
            heartbeat_seconds=args.heartbeat_seconds,
            command="wait --lane",
            quiet=args.quiet,
        )
        if task is None:
            _print_idle(args.lane, args.timeout, args.json, store)
            return 0
        _print({"status": "task_available", "lane": args.lane, "task": task}, args.json)
        return 0
    if not args.task_id:
        raise HandoffError("wait requires a task id or --lane")
    task_id = store.resolve_ref(args.task_id, config)
    response = _wait_for_response(
        store,
        task_id,
        config=config,
        timeout=args.timeout,
        heartbeat_seconds=args.heartbeat_seconds,
        quiet=args.quiet,
    )
    output = store.write_response_output(task_id, response)
    _print(
        {
            "status": "completed",
            "task_id": task_id,
            "response_path": str(output),
            "response": response,
        },
        args.json,
    )
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    target_root = Path(args.path).expanduser().resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    handoff_dir = target_root / "handoff"
    if handoff_dir.exists():
        raise HandoffError(f"handoff/ already exists: {handoff_dir}")

    workspace_id = args.workspace_id or target_root.name or "workspace"
    workspace_root = args.workspace_root or ".."
    queue_root = args.queue_root or "state"

    template_root = _template_root()
    if not template_root.exists():
        raise HandoffError(f"workspace template missing: {template_root}")

    # Copy the template tree, substituting placeholders.
    for src in template_root.rglob("*"):
        rel = src.relative_to(template_root)
        dst = handoff_dir / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = src.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            shutil.copyfile(src, dst)
        else:
            text = text.replace(INIT_PLACEHOLDER_WORKSPACE_ID, workspace_id)
            text = text.replace(INIT_PLACEHOLDER_WORKSPACE_ROOT, workspace_root)
            text = text.replace(INIT_PLACEHOLDER_QUEUE_ROOT, queue_root)
            dst.write_text(text, encoding="utf-8")
        # preserve executable bit
        src_mode = src.stat().st_mode
        if src_mode & stat.S_IXUSR:
            dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Make wrapper and dispatcher executable explicitly.
    for relative in ("bin/handoff", "dispatcher.sh"):
        candidate = handoff_dir / relative
        if candidate.exists():
            candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    payload = {
        "status": "initialized",
        "handoff": str(handoff_dir),
        "workspace_id": workspace_id,
        "workspace_root": workspace_root,
        "queue_root": queue_root,
        "next_steps": [
            "create task definitions outside handoff/ (commonly tasks/<id>.yaml)",
            "start a dispatcher with: bash handoff/dispatcher.sh",
            "submit with: ./handoff/bin/handoff submit --task tasks/<id>.yaml --request-from <path> --response-to <path>",
        ],
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"agent-lanes engine scaffolded at: {handoff_dir}")
        print(f"workspace_id: {workspace_id}")
        print(f"workspace_root: {workspace_root}")
        print(f"queue_root: {queue_root}")
        print(
            "next: create task definitions outside handoff/ (e.g. tasks/<id>.yaml), "
            "start a dispatcher with `bash handoff/dispatcher.sh`, "
            "and submit with `./handoff/bin/handoff submit --task tasks/<id>.yaml ...`."
        )
    return 0


def _template_root() -> Path:
    # Locate templates/workspace inside the package.
    return Path(__file__).resolve().parent / "templates" / "workspace"


def _wait_for_lane_task(
    store: HandoffStore,
    lane: str,
    *,
    timeout: float,
    heartbeat_seconds: float,
    command: str,
    quiet: bool,
) -> dict[str, object] | None:
    deadline = time.monotonic() + timeout
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        interval = remaining if heartbeat_seconds <= 0 else min(heartbeat_seconds, remaining)
        task = store.next_task(lane, wait_seconds=interval)
        if task is not None:
            return task
        if time.monotonic() >= deadline:
            return None
        if not quiet:
            print(f"agent-lanes {command}: waiting for lane {lane}", file=sys.stderr, flush=True)


def _wait_for_response(
    store: HandoffStore,
    ref: str,
    *,
    config: RuntimeConfig,
    timeout: float,
    heartbeat_seconds: float,
    quiet: bool,
) -> dict[str, object]:
    if heartbeat_seconds <= 0:
        return store.wait_for_response(ref, config=config, timeout=timeout)
    deadline = time.monotonic() + timeout
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        interval = min(heartbeat_seconds, remaining)
        try:
            return store.wait_for_response(ref, config=config, timeout=interval)
        except TimeoutError:
            if time.monotonic() >= deadline:
                raise
            if not quiet:
                print(f"agent-lanes wait: waiting for response {ref}", file=sys.stderr, flush=True)


def _print_idle(lane: str, timeout: float, as_json: bool, store: HandoffStore) -> None:
    lane_tasks = store.list_tasks(lane=lane)
    reason = "no_queued_task" if lane_tasks else "no_task_submitted"
    message = (
        f"No queued tasks found on lane {lane}. If you are the polling monitor, keep waiting. "
        "If you are the orchestrator, submit a task when the next artifact is ready."
    )
    payload = {
        "status": "idle",
        "lane": lane,
        "reason": reason,
        "timeout_seconds": timeout,
        "message": message,
        "next_action": (
            f"monitor: run wait --lane {lane} --json again; "
            "orchestrator: submit when ready"
        ),
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(message + f" Timed out after {timeout:g}s.", file=sys.stderr)


def _print_claim(task: dict[str, object], as_json: bool) -> None:
    payload = {"status": "claimed", "task_id": task["id"], "claim_token": task["claim_token"], "task": task}
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"task_id={task['id']}")
    print(f"claim_token={task['claim_token']}")


def _print_response(response: dict[str, object], task: dict[str, object], store: HandoffStore, as_json: bool) -> None:
    queue_depth = sum(
        1
        for item in store.list_tasks(lane=str(task["lane"]), include_completed=False)
        if item["state"] == "queued"
    )
    payload = {
        "status": response["status"],
        "task_id": task["id"],
        "checkpoint_id": task["checkpoint_id"],
        "lane": task["lane"],
        "response_path": task["response_path"],
        "verdict": response.get("verdict"),
        "blocking_count": response.get("blocking_count"),
        "nonblocking_count": response.get("nonblocking_count"),
        "queue_depth": queue_depth,
        "next_action": (
            "reviewer: stop after this task unless instructed otherwise; "
            f"orchestrator: run wait {task['id']}; "
            f"monitor: re-arm wait --lane {task['lane']} --json if more tasks are expected"
        ),
        "response": response,
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"status={response['status']}")
    print(f"task_id={task['id']}")
    print(f"verdict={response.get('verdict')}")
    print(f"response_path={task['response_path']}")
    print(f"queue_depth={queue_depth}")
    print(payload["next_action"])


def _rack_status_payload(tasks: list[dict[str, object]], store: HandoffStore) -> dict[str, object]:
    views = [_task_status_view(task, store) for task in tasks]
    summary = {
        "queued": sum(1 for task in views if task["state"] == "queued"),
        "claimed": sum(1 for task in views if task["state"] == "claimed"),
        "completed": sum(1 for task in views if task["state"] == "completed"),
        "failed": sum(1 for task in views if task["state"] == "failed"),
        "stale_claims": sum(1 for task in views if task.get("lease_expired")),
        "missing_response": sum(1 for task in views if task.get("missing_response")),
    }
    return {
        "status": "ok",
        "tasks": views,
        "count": len(views),
        "summary": summary,
        "next_action": _rack_next_action(summary),
    }


def _task_status_view(task: dict[str, object], store: HandoffStore) -> dict[str, object]:
    from .store import lease_expired

    view = dict(task)
    response_exists = (store.task_dir(str(task["id"])) / "response.json").exists()
    view["lease_expired"] = lease_expired(task) if task["state"] == "claimed" else False
    view["missing_response"] = task["state"] == "completed" and not response_exists
    view["navigation"] = {
        "workspace_root": task.get("workspace_root"),
        "worktree_path": task.get("worktree_path"),
        "expected_branch": task.get("expected_branch"),
        "request_path": task.get("request_path"),
        "response_path": task.get("response_path"),
        "review_scope": (
            "Review request_path as the primary artifact. Use worktree_path only as implementation "
            "context unless the task prompt says otherwise."
        ),
    }
    view["next_action"] = _task_next_action(view)
    return view


def _task_next_action(task: dict[str, object]) -> str:
    state = task["state"]
    if state == "queued":
        return "monitor: report task JSON and stop; reviewer: claim this task when ready"
    if state == "claimed":
        if task.get("lease_expired"):
            return "claim lease expired; recover only if the reviewer is stale or abandoned"
        return "wait for reviewer response; recover only if stale or abandoned"
    if state == "completed" and task.get("missing_response"):
        return "response file is missing from state; inspect task state before continuing"
    if state == "completed":
        return f"orchestrator: run wait {task['id']} to write the configured response output"
    if state == "failed":
        return "inspect failure response and decide whether to resubmit the task"
    return "inspect task state"


def _rack_next_action(summary: dict[str, int]) -> str:
    if summary["queued"]:
        return "reviewer should claim queued tasks; monitors should only report task JSON"
    if summary["claimed"]:
        return "wait for reviewer responses; recover claimed tasks only if stale or abandoned"
    if summary["failed"]:
        return "inspect failed tasks and decide whether to resubmit"
    if summary["completed"]:
        return "orchestrator should ensure wait <task-id> wrote each configured response output"
    return "no tasks exist yet; orchestrator should submit a task when ready"


def _print(data: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return
    if "task_id" in data:
        print(data["task_id"])
    else:
        print(json.dumps(data, indent=2, sort_keys=True))

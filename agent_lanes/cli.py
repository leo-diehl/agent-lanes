from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import RuntimeConfig, load_config
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "self-test":
            print(run_self_test())
            return 0
        if args.command is None:
            parser.print_help()
            return 0

        config = _load_config_for_command(args)
        store = HandoffStore(args.store or config.store_root)

        if args.command == "submit":
            task = store.create_from_checkpoint(config, args.checkpoint_id, source_agent=args.source_agent)
            _print({"task_id": task["id"], "task": task}, args.json)
            return 0
        if args.command == "wait":
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
            if not args.ref:
                raise HandoffError("wait requires a task/checkpoint ref or --lane")
            response = _wait_for_response(
                store,
                args.ref,
                config=config,
                timeout=args.timeout,
                heartbeat_seconds=args.heartbeat_seconds,
                quiet=args.quiet,
            )
            task_id = store.resolve_ref(args.ref, config)
            output = store.write_response_output(task_id, response)
            _print({"status": "completed", "task_id": task_id, "response_path": str(output), "response": response}, args.json)
            return 0
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
        if args.command == "respond":
            body = _response_body(args)
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
            )
            task = store.get_task(args.task_id)
            _print_response(response, task, store, args.json)
            return 0
        if args.command == "status":
            if args.rack:
                tasks = store.list_tasks(lane=args.lane, include_completed=not args.active_only)
                _print(_rack_status_payload(tasks, store), args.json)
                return 0
            if not args.ref:
                raise HandoffError("status requires a task/checkpoint ref or --rack")
            task_id = store.resolve_ref(args.ref, config)
            _print({"status": "ok", "task": _task_status_view(store.get_task(task_id), store)}, args.json)
            return 0
        if args.command == "list":
            tasks = store.list_tasks(lane=args.lane, include_completed=not args.active_only)
            _print({"status": "ok", "tasks": [_task_status_view(task, store) for task in tasks], "count": len(tasks)}, args.json)
            return 0
        if args.command == "serve":
            print(f"serving agent handoff on {args.host}:{args.port}", file=sys.stderr)
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
    parser.add_argument("--config", default="handoff/handoff.yaml", help="path to pack-local handoff.yaml")
    parser.add_argument("--store", help="override queue/store root")
    parser.add_argument("--json", action="store_true", help="print JSON instead of concise text")
    sub = parser.add_subparsers(dest="command")
    common_output = argparse.ArgumentParser(add_help=False)
    common_output.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")

    submit = sub.add_parser("submit", parents=[common_output], help="orchestrator: queue a checkpoint id")
    submit.add_argument("checkpoint_id")
    submit.add_argument("--source-agent", default="codex")

    wait = sub.add_parser(
        "wait",
        parents=[common_output],
        help="orchestrator: wait for checkpoint response; monitor: wait --lane for task JSON",
    )
    wait.add_argument("ref", nargs="?")
    wait.add_argument("--lane", help="wait for the next available task on a lane")
    wait.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT_SECONDS)
    wait.add_argument("--heartbeat-seconds", type=float, default=DEFAULT_HEARTBEAT_SECONDS)
    wait.add_argument("--quiet", action="store_true", help="suppress keepalive messages while waiting")

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

    claim = sub.add_parser("claim", parents=[common_output], help="reviewer: claim one task id before reviewing")
    claim.add_argument("task_id")
    claim.add_argument("--owner", default="worker")
    claim.add_argument("--lease-seconds", type=float, default=DEFAULT_CLAIM_LEASE_SECONDS)

    renew = sub.add_parser("renew", parents=[common_output])
    renew.add_argument("task_id")
    renew.add_argument("--claim-token", required=True)
    renew.add_argument("--lease-seconds", type=float, default=DEFAULT_CLAIM_LEASE_SECONDS)

    respond = sub.add_parser("respond", parents=[common_output], help="reviewer: submit one claimed task response")
    respond.add_argument("task_id")
    respond.add_argument("--file", help="copy this file's contents into response.json; wait later writes response_path")
    respond.add_argument("--body", help="inline Markdown response body")
    respond.add_argument("--reviewer", default="worker")
    respond.add_argument("--claim-token")
    respond.add_argument("--status", default="completed", choices=["completed", "failed"])
    respond.add_argument("--follow-up-required", action="store_true")
    respond.add_argument("--verdict", choices=["accept", "accept-with-follow-ups", "needs-revision"])
    respond.add_argument("--blocking-count", type=int)
    respond.add_argument("--nonblocking-count", type=int)
    respond.add_argument("--expect-sha256", help="request_sha256 value the reviewer actually reviewed")

    status = sub.add_parser("status", parents=[common_output], help="diagnose one task/checkpoint or the whole rack")
    status.add_argument("ref", nargs="?")
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


def _load_config_for_command(args: argparse.Namespace) -> RuntimeConfig:
    return load_config(args.config, store_override=args.store)


def _response_body(args: argparse.Namespace) -> str:
    if args.file:
        if args.file == "-":
            return sys.stdin.read()
        return Path(args.file).read_text(encoding="utf-8")
    if args.body is not None:
        return args.body
    return sys.stdin.read()


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
        "If you are the orchestrator, submit a checkpoint when the next artifact is ready."
    )
    payload = {
        "status": "idle",
        "lane": lane,
        "reason": reason,
        "timeout_seconds": timeout,
        "message": message,
        "next_action": (
            f"monitor: run wait --lane {lane} --json again; "
            "orchestrator: submit <checkpoint-id> when ready"
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
            f"orchestrator: run wait {task['checkpoint_id']}; "
            f"monitor: re-arm wait --lane {task['lane']} --json if more checkpoints are expected"
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
        return f"orchestrator: run wait {task['checkpoint_id']} to write the configured response output"
    if state == "failed":
        return "inspect failure response and decide whether to resubmit the checkpoint"
    return "inspect task state"


def _rack_next_action(summary: dict[str, int]) -> str:
    if summary["queued"]:
        return "reviewer should claim queued tasks; monitors should only report task JSON"
    if summary["claimed"]:
        return "wait for reviewer responses; recover claimed tasks only if stale or abandoned"
    if summary["failed"]:
        return "inspect failed tasks and decide whether to resubmit checkpoints"
    if summary["completed"]:
        return "orchestrator should ensure wait <checkpoint-id> wrote each configured response output"
    return "no tasks exist yet; orchestrator should submit a checkpoint when ready"


def _print(data: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return
    if "task_id" in data:
        print(data["task_id"])
    else:
        print(json.dumps(data, indent=2, sort_keys=True))

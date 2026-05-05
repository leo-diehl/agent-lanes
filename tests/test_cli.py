from __future__ import annotations

from io import StringIO
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agent_lanes.cli import main
from agent_lanes.config import load_config
from agent_lanes.defaults import DEFAULT_CLAIM_LEASE_SECONDS, DEFAULT_NEXT_TIMEOUT_SECONDS, DEFAULT_WAIT_TIMEOUT_SECONDS
from agent_lanes.errors import ConfigError, HandoffError
from agent_lanes.store import HandoffStore


def make_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    handoff = workspace / "handoff"
    outputs = workspace / "outputs"
    handoff.mkdir(parents=True)
    outputs.mkdir()
    (outputs / "01-step-output.md").write_text("# Output\n\nReview me.\n", encoding="utf-8")
    config_path = handoff / "handoff.yaml"
    config_path.write_text(
        """
workspace_id: cli-workspace
workspace_root: ..
queue_root: state

lanes:
  claude-review:
    description: review lane
""".lstrip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    return workspace, config_path, config


def submit_inline(config_path: Path, workspace: Path, capsys, *, lane: str = "claude-review", extra: list[str] | None = None) -> str:
    request = workspace / "outputs" / "01-step-output.md"
    response = workspace / "outputs" / "01-step-review.md"
    args = [
        "--config",
        str(config_path),
        "submit",
        "--lane",
        lane,
        "--request-from",
        str(request),
        "--response-to",
        str(response),
        "--prompt",
        "Review this output.",
        "--json",
    ]
    if extra:
        args.extend(extra)
    assert main(args) == 0
    return json.loads(capsys.readouterr().out)["task_id"]


def test_cli_submit_inline_and_wait_writes_response_output(tmp_path: Path, capsys) -> None:
    workspace, config_path, config = make_workspace(tmp_path)
    task_id = submit_inline(config_path, workspace, capsys)

    store = HandoffStore(config.store_root)
    claimed = store.claim_task(task_id, owner="worker")
    store.submit_response(
        task_id,
        body="CLI review complete.\n",
        reviewer="worker",
        claim_token=claimed["claim_token"],
    )

    assert main(["--config", str(config_path), "--json", "wait", task_id, "--timeout", "1"]) == 0
    waited = json.loads(capsys.readouterr().out)
    response_path = Path(waited["response_path"])
    assert response_path.exists()
    assert response_path.read_text(encoding="utf-8") == "CLI review complete.\n"


def test_cli_wait_lane_returns_task_and_idle_is_explicit(tmp_path: Path, capsys) -> None:
    workspace, config_path, _ = make_workspace(tmp_path)

    assert main(["--config", str(config_path), "wait", "--lane", "claude-review", "--timeout", "0", "--json"]) == 0
    idle = json.loads(capsys.readouterr().out)
    assert idle["lane"] == "claude-review"
    assert idle["reason"] == "no_task_submitted"
    assert idle["status"] == "idle"
    assert idle["timeout_seconds"] == 0.0
    assert "submit" in idle["message"]
    assert "monitor" in idle["next_action"]

    task_id = submit_inline(config_path, workspace, capsys)
    assert main(["--config", str(config_path), "wait", "--lane", "claude-review", "--timeout", "1", "--json"]) == 0
    available = json.loads(capsys.readouterr().out)

    assert available["status"] == "task_available"
    assert available["lane"] == "claude-review"
    assert available["task"]["id"] == task_id


def test_cli_claim_prints_token_and_respond_records_verdict(tmp_path: Path, capsys) -> None:
    workspace, config_path, config = make_workspace(tmp_path)
    task_id = submit_inline(config_path, workspace, capsys)

    assert main(["--config", str(config_path), "claim", task_id, "--owner", "worker"]) == 0
    claim_output = capsys.readouterr().out
    assert f"task_id={task_id}" in claim_output
    token = next(line.split("=", 1)[1] for line in claim_output.splitlines() if line.startswith("claim_token="))
    request_sha = HandoffStore(config.store_root).get_task(task_id)["request_sha256"]

    assert main(
        [
            "--config",
            str(config_path),
            "respond",
            task_id,
            "--claim-token",
            token,
            "--body",
            "Needs fixes.\n",
            "--verdict",
            "needs-revision",
            "--blocking-count",
            "2",
            "--nonblocking-count",
            "1",
            "--expect-sha256",
            request_sha,
            "--json",
        ]
    ) == 0
    submitted = json.loads(capsys.readouterr().out)
    response = submitted["response"]

    assert submitted["task_id"] == task_id
    assert submitted["response_path"].endswith("01-step-review.md")
    assert submitted["queue_depth"] == 0
    assert response["verdict"] == "needs-revision"
    assert response["blocking_count"] == 2
    assert response["nonblocking_count"] == 1
    assert HandoffStore(config.store_root).get_task(task_id)["state"] == "completed"


def test_cli_respond_without_verdict_succeeds(tmp_path: Path, capsys) -> None:
    workspace, config_path, config = make_workspace(tmp_path)
    task_id = submit_inline(config_path, workspace, capsys)

    assert main(["--config", str(config_path), "claim", task_id, "--owner", "worker", "--json"]) == 0
    claim = json.loads(capsys.readouterr().out)

    assert main(
        [
            "--config",
            str(config_path),
            "respond",
            task_id,
            "--claim-token",
            claim["claim_token"],
            "--body",
            "Q&A answer here.\n",
            "--json",
        ]
    ) == 0
    submitted = json.loads(capsys.readouterr().out)
    assert submitted["response"]["verdict"] is None
    assert submitted["verdict"] is None


def test_cli_respond_metadata_recorded(tmp_path: Path, capsys) -> None:
    workspace, config_path, config = make_workspace(tmp_path)
    task_id = submit_inline(config_path, workspace, capsys)
    assert main(["--config", str(config_path), "claim", task_id, "--owner", "worker", "--json"]) == 0
    claim = json.loads(capsys.readouterr().out)

    assert main(
        [
            "--config",
            str(config_path),
            "respond",
            task_id,
            "--claim-token",
            claim["claim_token"],
            "--body",
            "ok\n",
            "--metadata",
            "model_used=claude-fake",
            "--metadata",
            "tokens_in=42",
            "--json",
        ]
    ) == 0
    submitted = json.loads(capsys.readouterr().out)
    assert submitted["response"]["metadata"] == {"model_used": "claude-fake", "tokens_in": "42"}


def test_cli_list_and_status_rack_do_not_require_store_inspection(tmp_path: Path, capsys) -> None:
    workspace, config_path, _ = make_workspace(tmp_path)
    task_id = submit_inline(config_path, workspace, capsys)

    assert main(["--config", str(config_path), "list", "--lane", "claude-review", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 1
    assert listed["tasks"][0]["id"] == task_id

    assert main(["--config", str(config_path), "status", "--rack", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["count"] == 1
    assert status["summary"]["queued"] == 1
    assert "next_action" in status["tasks"][0]


def test_cli_full_role_choreography_has_guidance(tmp_path: Path, capsys) -> None:
    workspace, config_path, config = make_workspace(tmp_path)

    assert main(["--config", str(config_path), "wait", "--lane", "claude-review", "--timeout", "0", "--json"]) == 0
    idle = json.loads(capsys.readouterr().out)
    assert idle["reason"] == "no_task_submitted"

    task_id = submit_inline(config_path, workspace, capsys)

    assert main(["--config", str(config_path), "wait", "--lane", "claude-review", "--timeout", "1", "--json"]) == 0
    monitor = json.loads(capsys.readouterr().out)
    assert monitor["task"]["id"] == task_id
    assert HandoffStore(config.store_root).get_task(task_id)["state"] == "queued"

    assert main(["--config", str(config_path), "claim", task_id, "--owner", "claude-chat", "--json"]) == 0
    claim = json.loads(capsys.readouterr().out)

    assert main(["--config", str(config_path), "status", task_id, "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["task"]["state"] == "claimed"
    assert status["task"]["claim_owner"] == "claude-chat"
    assert "wait for reviewer response" in status["task"]["next_action"]
    assert status["task"]["navigation"]["request_path"].endswith("01-step-output.md")

    assert main(
        [
            "--config",
            str(config_path),
            "respond",
            task_id,
            "--claim-token",
            claim["claim_token"],
            "--body",
            "No blockers.\n",
            "--verdict",
            "accept-with-follow-ups",
            "--blocking-count",
            "0",
            "--nonblocking-count",
            "1",
            "--expect-sha256",
            claim["task"]["request_sha256"],
            "--json",
        ]
    ) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["verdict"] == "accept-with-follow-ups"
    assert f"orchestrator: run wait {task_id}" in response["next_action"]

    assert main(["--config", str(config_path), "wait", task_id, "--timeout", "1", "--json"]) == 0
    waited = json.loads(capsys.readouterr().out)
    assert Path(waited["response_path"]).read_text(encoding="utf-8") == "No blockers.\n"


def test_cli_respond_accepts_stdin_file_dash(tmp_path: Path, capsys, monkeypatch) -> None:
    workspace, config_path, _ = make_workspace(tmp_path)
    task_id = submit_inline(config_path, workspace, capsys)
    assert main(["--config", str(config_path), "claim", task_id, "--owner", "worker", "--json"]) == 0
    claim = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(sys, "stdin", StringIO("Review from stdin.\n"))

    assert main(
        [
            "--config",
            str(config_path),
            "respond",
            task_id,
            "--claim-token",
            claim["claim_token"],
            "--file",
            "-",
            "--json",
        ]
    ) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["response"]["body"] == "Review from stdin.\n"


def test_cli_release_via_cli(tmp_path: Path, capsys) -> None:
    workspace, config_path, config = make_workspace(tmp_path)
    task_id = submit_inline(config_path, workspace, capsys)

    assert main(["--config", str(config_path), "claim", task_id, "--owner", "worker", "--json"]) == 0
    claim = json.loads(capsys.readouterr().out)
    assert HandoffStore(config.store_root).get_task(task_id)["state"] == "claimed"

    assert main(
        [
            "--config",
            str(config_path),
            "release",
            task_id,
            "--claim-token",
            claim["claim_token"],
            "--reason",
            "not-a-fit",
            "--json",
        ]
    ) == 0
    released = json.loads(capsys.readouterr().out)
    assert released["status"] == "released"
    task = HandoffStore(config.store_root).get_task(task_id)
    assert task["state"] == "queued"
    assert task["claim_token"] is None


def test_cli_submit_task_file_with_overrides(tmp_path: Path, capsys) -> None:
    workspace, config_path, _ = make_workspace(tmp_path)
    task_file = tmp_path / "code-review.yaml"
    task_file.write_text(
        """
lane: claude-review
metadata:
  min_effort: high
prompt: |
  Default prompt body.
""".lstrip(),
        encoding="utf-8",
    )

    request = workspace / "outputs" / "01-step-output.md"
    response = workspace / "outputs" / "01-step-review.md"

    assert main(
        [
            "--config",
            str(config_path),
            "submit",
            "--task",
            str(task_file),
            "--request-from",
            str(request),
            "--response-to",
            str(response),
            "--metadata",
            "thread_id=t-1",
            "--json",
        ]
    ) == 0
    submitted = json.loads(capsys.readouterr().out)
    task = submitted["task"]
    assert task["lane"] == "claude-review"
    assert task["metadata"]["min_effort"] == "high"
    assert task["metadata"]["thread_id"] == "t-1"
    assert task["prompt"].startswith("Default prompt body.")


def test_cli_submit_task_file_supporting_paths_flow_through(tmp_path: Path, capsys) -> None:
    workspace, config_path, config = make_workspace(tmp_path)
    context_path = workspace / "outputs" / "context.md"
    prior_path = workspace / "outputs" / "prior.md"
    context_path.write_text("# Context\n\nUseful background.\n", encoding="utf-8")
    prior_path.write_text("# Prior\n\nLast review notes.\n", encoding="utf-8")

    task_file = tmp_path / "review-with-context.yaml"
    task_file.write_text(
        f"""
lane: claude-review
prompt: |
  Review using context.
supporting_paths:
  - {context_path}
  - {prior_path}
""".lstrip(),
        encoding="utf-8",
    )

    request = workspace / "outputs" / "01-step-output.md"
    response = workspace / "outputs" / "01-step-review.md"

    assert main(
        [
            "--config",
            str(config_path),
            "submit",
            "--task",
            str(task_file),
            "--request-from",
            str(request),
            "--response-to",
            str(response),
            "--json",
        ]
    ) == 0
    submitted = json.loads(capsys.readouterr().out)
    task = submitted["task"]

    supporting = task["supporting_paths"]
    assert isinstance(supporting, list)
    assert len(supporting) == 2
    for record in supporting:
        assert set(record.keys()) >= {"path", "sha256"}
        assert isinstance(record["sha256"], str) and len(record["sha256"]) == 64
    paths = {record["path"] for record in supporting}
    assert str(context_path.resolve()) in paths
    assert str(prior_path.resolve()) in paths

    persisted = HandoffStore(config.store_root).get_task(task["id"])
    assert persisted["supporting_paths"] == supporting


def test_cli_submit_task_file_unknown_keys_warn_and_ignore(tmp_path: Path, capsys) -> None:
    workspace, config_path, _ = make_workspace(tmp_path)
    task_file = tmp_path / "with-unknown.yaml"
    task_file.write_text(
        """
lane: claude-review
prompt: hello
some_future_field: yes
another_unknown: 1
""".lstrip(),
        encoding="utf-8",
    )
    request = workspace / "outputs" / "01-step-output.md"
    response = workspace / "outputs" / "01-step-review.md"

    assert main(
        [
            "--config",
            str(config_path),
            "submit",
            "--task",
            str(task_file),
            "--request-from",
            str(request),
            "--response-to",
            str(response),
            "--json",
        ]
    ) == 0
    captured = capsys.readouterr()
    assert "unknown keys" in captured.err
    submitted = json.loads(captured.out)
    assert "some_future_field" not in submitted["task"]
    assert "another_unknown" not in submitted["task"]


def test_cli_submit_requires_lane(tmp_path: Path, capsys) -> None:
    workspace, config_path, _ = make_workspace(tmp_path)
    request = workspace / "outputs" / "01-step-output.md"
    response = workspace / "outputs" / "01-step-review.md"
    rc = main(
        [
            "--config",
            str(config_path),
            "submit",
            "--request-from",
            str(request),
            "--response-to",
            str(response),
            "--prompt",
            "x",
        ]
    )
    assert rc == 1
    assert "--lane" in capsys.readouterr().err


def test_cli_handoff_yaml_with_checkpoints_is_rejected(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    handoff = workspace / "handoff"
    handoff.mkdir(parents=True)
    config_path = handoff / "handoff.yaml"
    config_path.write_text(
        """
workspace_id: bad
workspace_root: ..
queue_root: state
checkpoints:
  ghost:
    lane: claude-review
""".lstrip(),
        encoding="utf-8",
    )

    rc = main(["--config", str(config_path), "list", "--json"])
    assert rc == 1
    assert "checkpoints in handoff.yaml are no longer supported" in capsys.readouterr().err


def test_cli_init_scaffolds_engine_only(tmp_path: Path, capsys) -> None:
    target = tmp_path / "fresh-project"
    target.mkdir()

    assert main(["init", str(target), "--workspace-id", "demo", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "initialized"
    assert out["workspace_id"] == "demo"

    handoff_dir = target / "handoff"
    assert (handoff_dir / "handoff.yaml").exists()
    assert (handoff_dir / "dispatcher.sh").exists()
    assert (handoff_dir / "bin" / "handoff").exists()
    assert (handoff_dir / "state").exists()
    assert (handoff_dir / "POLLING-MONITOR-PROMPT.md").exists()
    assert (handoff_dir / "REVIEWER-AGENT-PROMPT.md").exists()
    assert (handoff_dir / "README.md").exists()

    # No tasks/ folder is created.
    assert not (target / "tasks").exists()

    # Wrappers are executable.
    wrapper_mode = (handoff_dir / "bin" / "handoff").stat().st_mode
    assert wrapper_mode & stat.S_IXUSR
    dispatcher_mode = (handoff_dir / "dispatcher.sh").stat().st_mode
    assert dispatcher_mode & stat.S_IXUSR

    # Placeholders substituted.
    yaml_text = (handoff_dir / "handoff.yaml").read_text(encoding="utf-8")
    assert "{{WORKSPACE_ID}}" not in yaml_text
    assert "{{WORKSPACE_ROOT}}" not in yaml_text
    assert "{{QUEUE_ROOT}}" not in yaml_text
    assert "workspace_id: demo" in yaml_text
    assert "queue_root: state" in yaml_text


def test_cli_init_queue_root_absolute_passthrough(tmp_path: Path, capsys) -> None:
    target = tmp_path / "fresh-project"
    target.mkdir()
    queue_root = "/some/abs/path"

    assert main(["init", str(target), "--queue-root", queue_root, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["queue_root"] == queue_root

    yaml_text = (target / "handoff" / "handoff.yaml").read_text(encoding="utf-8")
    assert f"queue_root: {queue_root}" in yaml_text


def test_cli_init_queue_root_relative_passthrough(tmp_path: Path, capsys) -> None:
    target = tmp_path / "fresh-project"
    target.mkdir()
    queue_root = "../shared/state"

    assert main(["init", str(target), "--queue-root", queue_root, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["queue_root"] == queue_root

    yaml_text = (target / "handoff" / "handoff.yaml").read_text(encoding="utf-8")
    assert f"queue_root: {queue_root}" in yaml_text


def test_cli_status_rack_allows_shared_queue_outside_workspace(tmp_path: Path, capsys) -> None:
    rack = tmp_path / "rack"
    handoff = rack / "handoff"
    shared_state = tmp_path / "shared-queue" / "state"
    handoff.mkdir(parents=True)
    config_path = handoff / "handoff.yaml"
    config_path.write_text(
        f"""
workspace_id: shared-queue-rack
workspace_root: ..
queue_root: {shared_state}

lanes:
  default:
    description: shared lane
""".lstrip(),
        encoding="utf-8",
    )

    config = load_config(config_path)
    assert config.workspace_root == rack.resolve()
    assert config.store_root == shared_state.resolve()

    assert main(["--config", str(config_path), "--store", str(shared_state), "status", "--rack", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "ok"
    assert status["count"] == 0


def test_cli_init_refuses_to_overwrite(tmp_path: Path, capsys) -> None:
    target = tmp_path / "with-existing"
    target.mkdir()
    (target / "handoff").mkdir()

    rc = main(["init", str(target)])
    assert rc == 1
    assert "already exists" in capsys.readouterr().err


def test_python_module_help_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "agent_lanes", "--help"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0
    assert "agent-lanes" in result.stdout
    assert "init" in result.stdout
    assert "release" in result.stdout
    assert "submit" in result.stdout


def test_cli_submit_help_has_no_positional_task_id() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "agent_lanes", "submit", "--help"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0
    # The synopsis should not contain a positional checkpoint/task argument.
    # The first 'usage:' line ends after the optional flags; verify no positional follows --metadata.
    assert "checkpoint_id" not in result.stdout
    assert "--task" in result.stdout
    assert "--lane" in result.stdout


def test_cli_defaults_are_long_poll_friendly() -> None:
    from agent_lanes.cli import build_parser

    parser = build_parser()

    wait = parser.parse_args(["wait", "any-task-id"])
    next_task = parser.parse_args(["next", "--lane", "claude-review"])
    watch = parser.parse_args(["watch", "--lane", "claude-review"])
    claim = parser.parse_args(["claim", "task-1"])

    assert wait.timeout == DEFAULT_WAIT_TIMEOUT_SECONDS
    assert next_task.timeout == DEFAULT_NEXT_TIMEOUT_SECONDS
    assert watch.timeout == DEFAULT_NEXT_TIMEOUT_SECONDS
    assert claim.lease_seconds == DEFAULT_CLAIM_LEASE_SECONDS


def test_workspace_template_smoke_submit_claim_respond_wait(tmp_path: Path) -> None:
    runtime_root = Path(__file__).resolve().parents[1]

    target = tmp_path / "fresh"
    target.mkdir()
    # Use init to scaffold the engine.
    assert main(["init", str(target), "--workspace-id", "smoke"]) == 0
    handoff_dir = target / "handoff"

    outputs = target / "outputs"
    outputs.mkdir()
    (outputs / "01-step-output.md").write_text("# Template output\n", encoding="utf-8")

    wrapper = handoff_dir / "bin" / "handoff"
    reviewer_prompt = handoff_dir / "REVIEWER-AGENT-PROMPT.md"
    monitor_prompt = handoff_dir / "POLLING-MONITOR-PROMPT.md"
    assert reviewer_prompt.exists()
    assert monitor_prompt.exists()

    env = os.environ.copy()
    env["AGENT_LANES_RUNTIME"] = str(runtime_root)

    request = outputs / "01-step-output.md"
    response_to = outputs / "01-step-review.md"

    submit = subprocess.run(
        [
            str(wrapper),
            "--json",
            "submit",
            "--lane",
            "default",
            "--request-from",
            str(request),
            "--response-to",
            str(response_to),
            "--prompt",
            "Review.",
        ],
        cwd=target,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        env=env,
    )
    task_id = json.loads(submit.stdout)["task_id"]

    watched = subprocess.run(
        [str(wrapper), "--json", "watch", "--lane", "default", "--timeout", "1"],
        cwd=target,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        env=env,
    )
    assert json.loads(watched.stdout)["task"]["id"] == task_id

    claim = subprocess.run(
        [str(wrapper), "--json", "claim", task_id, "--owner", "smoke-worker"],
        cwd=target,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        env=env,
    )
    claim_token = json.loads(claim.stdout)["claim_token"]

    response_file = tmp_path / "response.md"
    response_file.write_text("Template review complete.\n", encoding="utf-8")
    subprocess.run(
        [str(wrapper), "--json", "respond", task_id, "--claim-token", claim_token, "--file", str(response_file)],
        cwd=target,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        env=env,
    )
    subprocess.run(
        [str(wrapper), "--json", "wait", task_id, "--timeout", "1"],
        cwd=target,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        env=env,
    )

    assert (outputs / "01-step-review.md").read_text(encoding="utf-8") == "Template review complete.\n"


def test_cli_dispatcher_template_parses() -> None:
    template = Path(__file__).resolve().parents[1] / "agent_lanes" / "templates" / "workspace" / "dispatcher.sh"
    result = subprocess.run(["bash", "-n", str(template)], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert result.returncode == 0, result.stderr

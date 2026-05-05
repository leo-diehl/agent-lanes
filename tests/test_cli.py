from __future__ import annotations

from io import StringIO
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from agent_lanes.cli import main
from agent_lanes.config import load_config
from agent_lanes.defaults import DEFAULT_CLAIM_LEASE_SECONDS, DEFAULT_NEXT_TIMEOUT_SECONDS, DEFAULT_WAIT_TIMEOUT_SECONDS
from agent_lanes.store import HandoffStore


def make_pack(tmp_path: Path):
    pack = tmp_path / "pack"
    handoff = pack / "handoff"
    outputs = pack / "outputs"
    handoff.mkdir(parents=True)
    outputs.mkdir()
    (outputs / "01-step-output.md").write_text("# Output\n\nReview me.\n", encoding="utf-8")
    config_path = handoff / "handoff.yaml"
    config_path.write_text(
        """
workspace_id: cli-workspace
workspace_root: ..
queue_root: state
checkpoints:
  phase-01-review:
    lane: claude-review
    request_from: ../outputs/01-step-output.md
    response_to: ../outputs/01-step-review.md
    required: true
    prompt: |
      Review this output.
""".lstrip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    return pack, config_path, config


def test_cli_submit_and_wait_writes_response_output(tmp_path: Path, capsys) -> None:
    _, config_path, config = make_pack(tmp_path)
    assert main(["--config", str(config_path), "submit", "phase-01-review", "--json"]) == 0
    submitted = json.loads(capsys.readouterr().out)
    task_id = submitted["task_id"]

    store = HandoffStore(config.store_root)
    claimed = store.claim_task(task_id, owner="worker")
    store.submit_response(
        task_id,
        body="CLI review complete.\n",
        reviewer="worker",
        claim_token=claimed["claim_token"],
    )

    assert main(["--config", str(config_path), "--json", "wait", "phase-01-review", "--timeout", "1"]) == 0
    waited = json.loads(capsys.readouterr().out)
    response_path = Path(waited["response_path"])
    assert response_path.exists()
    assert response_path.read_text(encoding="utf-8") == "CLI review complete.\n"


def test_cli_wait_lane_returns_task_and_idle_is_explicit(tmp_path: Path, capsys) -> None:
    _, config_path, _ = make_pack(tmp_path)

    assert main(["--config", str(config_path), "wait", "--lane", "claude-review", "--timeout", "0", "--json"]) == 0
    idle = json.loads(capsys.readouterr().out)
    assert idle["lane"] == "claude-review"
    assert idle["reason"] == "no_task_submitted"
    assert idle["status"] == "idle"
    assert idle["timeout_seconds"] == 0.0
    assert "submit a checkpoint" in idle["message"]
    assert "monitor" in idle["next_action"]

    assert main(["--config", str(config_path), "submit", "phase-01-review", "--json"]) == 0
    task_id = json.loads(capsys.readouterr().out)["task_id"]
    assert main(["--config", str(config_path), "wait", "--lane", "claude-review", "--timeout", "1", "--json"]) == 0
    available = json.loads(capsys.readouterr().out)

    assert available["status"] == "task_available"
    assert available["lane"] == "claude-review"
    assert available["task"]["id"] == task_id


def test_cli_claim_prints_token_and_respond_records_verdict(tmp_path: Path, capsys) -> None:
    _, config_path, config = make_pack(tmp_path)
    assert main(["--config", str(config_path), "submit", "phase-01-review", "--json"]) == 0
    task_id = json.loads(capsys.readouterr().out)["task_id"]

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


def test_cli_list_and_status_rack_do_not_require_store_inspection(tmp_path: Path, capsys) -> None:
    _, config_path, _ = make_pack(tmp_path)
    assert main(["--config", str(config_path), "submit", "phase-01-review", "--json"]) == 0
    task_id = json.loads(capsys.readouterr().out)["task_id"]

    assert main(["--config", str(config_path), "list", "--lane", "claude-review", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 1
    assert listed["tasks"][0]["id"] == task_id

    assert main(["--config", str(config_path), "status", "--rack", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["count"] == 1
    assert status["summary"]["queued"] == 1
    assert status["tasks"][0]["checkpoint_id"] == "phase-01-review"
    assert "next_action" in status["tasks"][0]


def test_cli_full_role_choreography_has_guidance(tmp_path: Path, capsys) -> None:
    _, config_path, config = make_pack(tmp_path)

    assert main(["--config", str(config_path), "wait", "--lane", "claude-review", "--timeout", "0", "--json"]) == 0
    idle = json.loads(capsys.readouterr().out)
    assert idle["reason"] == "no_task_submitted"

    assert main(["--config", str(config_path), "submit", "phase-01-review", "--json"]) == 0
    task_id = json.loads(capsys.readouterr().out)["task_id"]

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
    assert "orchestrator: run wait phase-01-review" in response["next_action"]

    assert main(["--config", str(config_path), "wait", "phase-01-review", "--timeout", "1", "--json"]) == 0
    waited = json.loads(capsys.readouterr().out)
    assert Path(waited["response_path"]).read_text(encoding="utf-8") == "No blockers.\n"


def test_cli_respond_accepts_stdin_file_dash(tmp_path: Path, capsys, monkeypatch) -> None:
    _, config_path, _ = make_pack(tmp_path)
    assert main(["--config", str(config_path), "submit", "phase-01-review", "--json"]) == 0
    task_id = json.loads(capsys.readouterr().out)["task_id"]
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
            "--verdict",
            "accept",
            "--blocking-count",
            "0",
            "--json",
        ]
    ) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["response"]["body"] == "Review from stdin.\n"


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


def test_cli_defaults_are_long_poll_friendly() -> None:
    from agent_lanes.cli import build_parser

    parser = build_parser()

    wait = parser.parse_args(["wait", "phase-01-review"])
    next_task = parser.parse_args(["next", "--lane", "claude-review"])
    watch = parser.parse_args(["watch", "--lane", "claude-review"])
    claim = parser.parse_args(["claim", "task-1"])

    assert wait.timeout == DEFAULT_WAIT_TIMEOUT_SECONDS
    assert next_task.timeout == DEFAULT_NEXT_TIMEOUT_SECONDS
    assert watch.timeout == DEFAULT_NEXT_TIMEOUT_SECONDS
    assert claim.lease_seconds == DEFAULT_CLAIM_LEASE_SECONDS


def test_workspace_template_smoke_submit_claim_respond_wait(tmp_path: Path) -> None:
    runtime_root = Path(__file__).resolve().parents[1]
    template = runtime_root / "agent_lanes" / "templates" / "workspace"
    workspace = tmp_path / "template-workspace"
    outputs = workspace / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "01-step-output.md").write_text("# Template output\n", encoding="utf-8")
    shutil.copytree(template, workspace / "handoff")
    wrapper = workspace / "handoff" / "bin" / "handoff"
    wrapper.chmod(0o755)
    claude_prompt = workspace / "handoff" / "CLAUDE-REVIEWER-PROMPT.md"
    assert claude_prompt.exists()
    assert "claude-review" in claude_prompt.read_text(encoding="utf-8")
    monitor_prompt = workspace / "handoff" / "POLLING-MONITOR-PROMPT.md"
    assert monitor_prompt.exists()
    monitor_text = monitor_prompt.read_text(encoding="utf-8")
    assert "wait --lane claude-review --json" in monitor_text
    assert "Do not claim" in monitor_text

    env = os.environ.copy()
    env["AGENT_LANES_RUNTIME"] = str(runtime_root)

    submit = subprocess.run(
        [str(wrapper), "--json", "submit", "phase-01-review"],
        cwd=workspace,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        env=env,
    )
    task_id = json.loads(submit.stdout)["task_id"]

    watched = subprocess.run(
        [str(wrapper), "--json", "watch", "--lane", "claude-review", "--timeout", "1"],
        cwd=workspace,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        env=env,
    )
    assert json.loads(watched.stdout)["task"]["id"] == task_id

    claim = subprocess.run(
        [str(wrapper), "--json", "claim", task_id, "--owner", "template-worker"],
        cwd=workspace,
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
        cwd=workspace,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        env=env,
    )
    subprocess.run(
        [str(wrapper), "--json", "wait", "phase-01-review", "--timeout", "1"],
        cwd=workspace,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        env=env,
    )

    assert (outputs / "01-step-review.md").read_text(encoding="utf-8") == "Template review complete.\n"

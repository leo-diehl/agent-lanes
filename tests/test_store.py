from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agent_lanes.config import load_config
from agent_lanes.errors import ConfigError, StoreError, TimeoutError
from agent_lanes.store import HandoffStore, atomic_write_json


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
workspace_id: test-workspace
workspace_root: ..
queue_root: state

lanes:
  claude-review:
    description: review lane
""".lstrip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    return workspace, config, HandoffStore(config.store_root)


def make_task(store: HandoffStore, config, *, lane: str = "claude-review", metadata=None):
    workspace = config.workspace_root
    return store.create_task(
        workspace_id=config.workspace_id,
        correlation_id="phase-01-review",
        source_agent="orchestrator",
        lane=lane,
        workspace_root=config.workspace_root,
        worktree_path=None,
        expected_branch=None,
        request_path=workspace / "outputs" / "01-step-output.md",
        response_path=workspace / "outputs" / "01-step-review.md",
        prompt="Review this output.",
        metadata=metadata,
    )


def test_store_creates_task_with_valid_state(tmp_path: Path) -> None:
    _, config, store = make_workspace(tmp_path)
    task = make_task(store, config)

    assert task["state"] == "queued"
    assert task["id"]
    assert task["request_sha256"]
    assert task["metadata"] == {}
    assert (config.store_root / "tasks" / task["id"] / "task.json").exists()
    assert store.latest_task_id("test-workspace", "phase-01-review") == task["id"]


def test_store_persists_metadata(tmp_path: Path) -> None:
    _, config, store = make_workspace(tmp_path)
    task = make_task(store, config, metadata={"min_effort": "high", "thread_id": "t-1"})

    assert task["metadata"] == {"min_effort": "high", "thread_id": "t-1"}
    reloaded = store.get_task(task["id"])
    assert reloaded["metadata"] == {"min_effort": "high", "thread_id": "t-1"}


def test_atomic_write_json_leaves_valid_json_and_no_visible_tmp(tmp_path: Path) -> None:
    target = tmp_path / "state.json"

    for index in range(25):
        atomic_write_json(target, {"index": index})
        parsed = json.loads(target.read_text(encoding="utf-8"))
        assert parsed == {"index": index}

    assert not list(tmp_path.glob("*.tmp"))


def test_two_workers_cannot_claim_same_task(tmp_path: Path) -> None:
    _, config, store = make_workspace(tmp_path)
    task = make_task(store, config)
    first = store.claim_task(task["id"], owner="worker-1")

    assert first["state"] == "claimed"
    with pytest.raises(StoreError, match="active lease"):
        store.claim_task(task["id"], owner="worker-2")


def test_stale_lease_can_be_reclaimed(tmp_path: Path) -> None:
    _, config, store = make_workspace(tmp_path)
    task = make_task(store, config)
    first = store.claim_task(task["id"], owner="worker-1", lease_seconds=-1)
    second = store.claim_task(task["id"], owner="worker-2")

    assert first["claim_token"] != second["claim_token"]
    assert second["claim_owner"] == "worker-2"


def test_response_submission_completes_task(tmp_path: Path) -> None:
    _, config, store = make_workspace(tmp_path)
    task = make_task(store, config)
    claimed = store.claim_task(task["id"], owner="worker")

    response = store.submit_response(
        task["id"],
        body="No blockers.\n",
        reviewer="worker",
        claim_token=claimed["claim_token"],
    )

    assert response["status"] == "completed"
    assert response["verdict"] is None
    assert response["metadata"] == {}
    assert store.get_task(task["id"])["state"] == "completed"


def test_response_records_metadata(tmp_path: Path) -> None:
    _, config, store = make_workspace(tmp_path)
    task = make_task(store, config)
    claimed = store.claim_task(task["id"], owner="worker")

    response = store.submit_response(
        task["id"],
        body="ok\n",
        reviewer="worker",
        claim_token=claimed["claim_token"],
        metadata={"model_used": "fake", "tokens_in": 42},
    )

    assert response["metadata"] == {"model_used": "fake", "tokens_in": 42}


def test_response_submission_rejects_changed_request_source(tmp_path: Path) -> None:
    workspace, config, store = make_workspace(tmp_path)
    task = make_task(store, config)
    claimed = store.claim_task(task["id"], owner="worker")
    (workspace / "outputs" / "01-step-output.md").write_text("changed after claim\n", encoding="utf-8")

    with pytest.raises(StoreError, match="request hash mismatch"):
        store.submit_response(
            task["id"],
            body="No blockers.\n",
            reviewer="worker",
            claim_token=claimed["claim_token"],
            verdict="accept",
        )


def test_response_submission_rejects_inconsistent_verdict_counts(tmp_path: Path) -> None:
    _, config, store = make_workspace(tmp_path)
    task = make_task(store, config)
    claimed = store.claim_task(task["id"], owner="worker")

    with pytest.raises(StoreError, match="blocking-count > 0"):
        store.submit_response(
            task["id"],
            body="Blocking issue.\n",
            reviewer="worker",
            claim_token=claimed["claim_token"],
            verdict="accept",
            blocking_count=1,
        )


def test_claim_rejects_changed_request_source(tmp_path: Path) -> None:
    workspace, config, store = make_workspace(tmp_path)
    task = make_task(store, config)
    (workspace / "outputs" / "01-step-output.md").write_text("changed after submit\n", encoding="utf-8")

    with pytest.raises(StoreError, match="request hash mismatch"):
        store.claim_task(task["id"], owner="worker")


def test_renew_claim_extends_lease(tmp_path: Path) -> None:
    _, config, store = make_workspace(tmp_path)
    task = make_task(store, config)
    claimed = store.claim_task(task["id"], owner="worker", lease_seconds=1)

    renewed = store.renew_claim(task["id"], claim_token=claimed["claim_token"], lease_seconds=60)

    assert renewed["claim_token"] == claimed["claim_token"]
    assert renewed["lease_expires_at"] != claimed["lease_expires_at"]


def test_release_returns_task_to_queued(tmp_path: Path) -> None:
    _, config, store = make_workspace(tmp_path)
    task = make_task(store, config)
    claimed = store.claim_task(task["id"], owner="worker")

    released = store.release_claim(task["id"], claim_token=claimed["claim_token"], reason="not-a-fit")

    assert released["state"] == "queued"
    assert released["claim_owner"] is None
    assert released["claim_token"] is None
    assert released["lease_expires_at"] is None
    assert released["claimed_at"] is None

    events_path = config.store_root / "tasks" / task["id"] / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line]
    types = [event["type"] for event in events]
    assert "released" in types


def test_release_with_wrong_claim_token_is_rejected(tmp_path: Path) -> None:
    _, config, store = make_workspace(tmp_path)
    task = make_task(store, config)
    store.claim_task(task["id"], owner="worker")

    with pytest.raises(StoreError, match="claim token"):
        store.release_claim(task["id"], claim_token="bogus-token")


def test_release_unclaimed_task_is_rejected(tmp_path: Path) -> None:
    _, config, store = make_workspace(tmp_path)
    task = make_task(store, config)

    with pytest.raises(StoreError, match="cannot release"):
        store.release_claim(task["id"], claim_token="anything")


def test_wait_for_response_blocks_until_response_arrives(tmp_path: Path) -> None:
    _, config, store = make_workspace(tmp_path)
    task = make_task(store, config)
    claimed = store.claim_task(task["id"], owner="worker")

    def respond() -> None:
        store.submit_response(
            task["id"],
            body="Arrived.\n",
            reviewer="worker",
            claim_token=claimed["claim_token"],
        )

    thread = threading.Thread(target=respond)
    thread.start()
    response = store.wait_for_response(task["id"], config=config, timeout=5)
    thread.join(timeout=5)

    assert response["body"] == "Arrived.\n"


def test_wait_for_response_times_out(tmp_path: Path) -> None:
    _, config, store = make_workspace(tmp_path)
    make_task(store, config)
    # The wait targets a non-existent task id to confirm the unknown-task path is rejected.
    with pytest.raises(StoreError):
        store.wait_for_response("does-not-exist", config=config, timeout=0.01)


def test_wait_for_response_existing_task_times_out(tmp_path: Path) -> None:
    _, config, store = make_workspace(tmp_path)
    task = make_task(store, config)

    with pytest.raises(TimeoutError):
        store.wait_for_response(task["id"], config=config, timeout=0.01)


def test_config_allows_queue_root_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    handoff = workspace / "handoff"
    shared_state = tmp_path / "shared-queue" / "state"
    handoff.mkdir(parents=True)
    config_path = handoff / "handoff.yaml"
    config_path.write_text(
        f"""
workspace_id: shared-queue-workspace
workspace_root: ..
queue_root: {shared_state}

lanes:
  default: {{}}
""".lstrip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.workspace_root == workspace.resolve()
    assert config.store_root == shared_state.resolve()


def test_legacy_task_json_with_checkpoint_id_loads_via_correlation_id(tmp_path: Path) -> None:
    """A task.json written by an older release that still uses ``checkpoint_id``
    must remain readable for one minor version. Loaded tasks expose the value
    under the canonical ``correlation_id`` name."""
    _, config, store = make_workspace(tmp_path)
    task = make_task(store, config)
    task_path = config.store_root / "tasks" / task["id"] / "task.json"

    # Rewrite the on-disk task.json in the legacy shape: drop correlation_id and
    # set checkpoint_id as the older release would have.
    raw = json.loads(task_path.read_text(encoding="utf-8"))
    legacy_value = raw.pop("correlation_id")
    raw["checkpoint_id"] = legacy_value
    task_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    loaded = store.get_task(task["id"])
    assert loaded["correlation_id"] == legacy_value
    assert "checkpoint_id" not in loaded


def test_config_rejects_checkpoints_section(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    handoff = workspace / "handoff"
    handoff.mkdir(parents=True)
    config_path = handoff / "handoff.yaml"
    config_path.write_text(
        """
workspace_id: ws
workspace_root: ..
queue_root: state
checkpoints:
  bogus:
    lane: claude-review
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="checkpoints in handoff.yaml are no longer supported"):
        load_config(config_path)

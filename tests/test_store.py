from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agent_lanes.config import load_config
from agent_lanes.errors import ConfigError, StoreError, TimeoutError
from agent_lanes.store import HandoffStore, atomic_write_json


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
workspace_id: test-workspace
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
    return pack, config, HandoffStore(config.store_root)


def test_store_creates_task_with_valid_state(tmp_path: Path) -> None:
    _, config, store = make_pack(tmp_path)
    task = store.create_from_checkpoint(config, "phase-01-review")

    assert task["state"] == "queued"
    assert task["id"]
    assert task["request_sha256"]
    assert (config.store_root / "tasks" / task["id"] / "task.json").exists()
    assert store.latest_task_id("test-workspace", "phase-01-review") == task["id"]


def test_store_records_supporting_paths_with_hashes(tmp_path: Path) -> None:
    pack, _, _ = make_pack(tmp_path)
    context = pack / "outputs" / "context.md"
    context.write_text("Pinned context\n", encoding="utf-8")
    config_path = pack / "handoff" / "handoff.yaml"
    config_path.write_text(
        """
workspace_id: support-workspace
workspace_root: ..
queue_root: state
checkpoints:
  meta-review:
    lane: claude-review
    request_from: ../outputs/01-step-output.md
    response_to: ../outputs/01-step-review.md
    supporting_paths:
      - ../outputs/context.md
    prompt: |
      Review with pinned context.
""".lstrip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    store = HandoffStore(config.store_root)

    task = store.create_from_checkpoint(config, "meta-review")

    assert task["supporting_paths"][0]["path"] == str(context)
    assert task["supporting_paths"][0]["sha256"]


def test_atomic_write_json_leaves_valid_json_and_no_visible_tmp(tmp_path: Path) -> None:
    target = tmp_path / "state.json"

    for index in range(25):
        atomic_write_json(target, {"index": index})
        parsed = json.loads(target.read_text(encoding="utf-8"))
        assert parsed == {"index": index}

    assert not list(tmp_path.glob("*.tmp"))


def test_two_workers_cannot_claim_same_task(tmp_path: Path) -> None:
    _, config, store = make_pack(tmp_path)
    task = store.create_from_checkpoint(config, "phase-01-review")
    first = store.claim_task(task["id"], owner="worker-1")

    assert first["state"] == "claimed"
    with pytest.raises(StoreError, match="active lease"):
        store.claim_task(task["id"], owner="worker-2")


def test_stale_lease_can_be_reclaimed(tmp_path: Path) -> None:
    _, config, store = make_pack(tmp_path)
    task = store.create_from_checkpoint(config, "phase-01-review")
    first = store.claim_task(task["id"], owner="worker-1", lease_seconds=-1)
    second = store.claim_task(task["id"], owner="worker-2")

    assert first["claim_token"] != second["claim_token"]
    assert second["claim_owner"] == "worker-2"


def test_response_submission_completes_task(tmp_path: Path) -> None:
    _, config, store = make_pack(tmp_path)
    task = store.create_from_checkpoint(config, "phase-01-review")
    claimed = store.claim_task(task["id"], owner="worker")

    response = store.submit_response(
        task["id"],
        body="No blockers.\n",
        reviewer="worker",
        claim_token=claimed["claim_token"],
    )

    assert response["status"] == "completed"
    assert store.get_task(task["id"])["state"] == "completed"


def test_response_submission_rejects_changed_request_source(tmp_path: Path) -> None:
    pack, config, store = make_pack(tmp_path)
    task = store.create_from_checkpoint(config, "phase-01-review")
    claimed = store.claim_task(task["id"], owner="worker")
    (pack / "outputs" / "01-step-output.md").write_text("changed after claim\n", encoding="utf-8")

    with pytest.raises(StoreError, match="request hash mismatch"):
        store.submit_response(
            task["id"],
            body="No blockers.\n",
            reviewer="worker",
            claim_token=claimed["claim_token"],
            verdict="accept",
        )


def test_response_submission_rejects_inconsistent_verdict_counts(tmp_path: Path) -> None:
    _, config, store = make_pack(tmp_path)
    task = store.create_from_checkpoint(config, "phase-01-review")
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
    pack, config, store = make_pack(tmp_path)
    task = store.create_from_checkpoint(config, "phase-01-review")
    (pack / "outputs" / "01-step-output.md").write_text("changed after submit\n", encoding="utf-8")

    with pytest.raises(StoreError, match="request hash mismatch"):
        store.claim_task(task["id"], owner="worker")


def test_renew_claim_extends_lease(tmp_path: Path) -> None:
    _, config, store = make_pack(tmp_path)
    task = store.create_from_checkpoint(config, "phase-01-review")
    claimed = store.claim_task(task["id"], owner="worker", lease_seconds=1)

    renewed = store.renew_claim(task["id"], claim_token=claimed["claim_token"], lease_seconds=60)

    assert renewed["claim_token"] == claimed["claim_token"]
    assert renewed["lease_expires_at"] != claimed["lease_expires_at"]


def test_wait_for_response_blocks_until_response_arrives(tmp_path: Path) -> None:
    _, config, store = make_pack(tmp_path)
    task = store.create_from_checkpoint(config, "phase-01-review")
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
    response = store.wait_for_response("phase-01-review", config=config, timeout=5)
    thread.join(timeout=5)

    assert response["body"] == "Arrived.\n"


def test_wait_for_response_times_out(tmp_path: Path) -> None:
    _, config, store = make_pack(tmp_path)
    store.create_from_checkpoint(config, "phase-01-review")

    with pytest.raises(TimeoutError):
        store.wait_for_response("phase-01-review", config=config, timeout=0.01)


def test_config_rejects_path_escape(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    handoff = pack / "handoff"
    outputs = pack / "outputs"
    handoff.mkdir(parents=True)
    outputs.mkdir()
    (outputs / "01-step-output.md").write_text("output", encoding="utf-8")
    config_path = handoff / "handoff.yaml"
    config_path.write_text(
        """
workspace_id: bad-workspace
workspace_root: ..
queue_root: state
checkpoints:
  bad-review:
    lane: claude-review
    request_from: ../outputs/01-step-output.md
    response_to: ../../escaped.md
    prompt: no
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="escapes workspace root"):
        load_config(config_path)

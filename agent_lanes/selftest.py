from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from .config import load_config
from .store import HandoffStore


def run_self_test() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "pack"
        handoff = root / "handoff"
        outputs = root / "outputs"
        handoff.mkdir(parents=True)
        outputs.mkdir()
        request = outputs / "01-step-output.md"
        request.write_text("# Step output\n\nNeeds review.\n", encoding="utf-8")
        config_path = handoff / "handoff.yaml"
        config_path.write_text(
            """
pack_id: self-test-pack
pack_root: ..
queue_root: state
checkpoints:
  phase-01-review:
    lane: claude-review
    request_from: ../outputs/01-step-output.md
    response_to: ../outputs/01-step-review.md
    required: true
    prompt: |
      Review this self-test output.
""".lstrip(),
            encoding="utf-8",
        )
        config = load_config(config_path)
        store = HandoffStore(config.store_root)
        task = store.create_from_checkpoint(config, "phase-01-review")

        def fake_worker() -> None:
            claimed = store.claim_task(task["id"], owner="fake-claude", lease_seconds=60)
            store.submit_response(
                task["id"],
                body="Fake review: no blockers.\n",
                reviewer="fake-claude",
                claim_token=claimed["claim_token"],
            )

        thread = threading.Thread(target=fake_worker)
        thread.start()
        response = store.wait_for_response("phase-01-review", config=config, timeout=5)
        task_id = store.resolve_ref("phase-01-review", config)
        output = store.write_response_output(task_id, response)
        thread.join(timeout=5)
        assert output.exists()
        assert "no blockers" in output.read_text(encoding="utf-8")
        return f"self-test passed: {task['id']}"

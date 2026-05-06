from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from .config import load_config
from .store import HandoffStore


def run_self_test() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "workspace"
        handoff = root / "handoff"
        outputs = root / "outputs"
        handoff.mkdir(parents=True)
        outputs.mkdir()
        request = outputs / "01-step-output.md"
        request.write_text("# Step output\n\nNeeds review.\n", encoding="utf-8")
        response_target = outputs / "01-step-review.md"
        config_path = handoff / "handoff.yaml"
        config_path.write_text(
            """
workspace_id: self-test-workspace
workspace_root: ..
queue_root: state

lanes:
  default:
    description: self-test lane
""".lstrip(),
            encoding="utf-8",
        )
        config = load_config(config_path)
        store = HandoffStore(config.store_root)
        task = store.create_task(
            workspace_id=config.workspace_id,
            correlation_id="self-test",
            source_agent="self-test",
            lane="default",
            workspace_root=config.workspace_root,
            worktree_path=None,
            expected_branch=None,
            request_path=request,
            response_path=response_target,
            prompt="Review this self-test output.",
            metadata={"min_effort": "low"},
        )

        def fake_worker() -> None:
            claimed = store.claim_task(task["id"], owner="fake-reviewer", lease_seconds=60)
            store.submit_response(
                task["id"],
                body="Fake review: no blockers.\n",
                reviewer="fake-reviewer",
                claim_token=claimed["claim_token"],
            )

        thread = threading.Thread(target=fake_worker)
        thread.start()
        response = store.wait_for_response(task["id"], config=config, timeout=5)
        output = store.write_response_output(task["id"], response)
        thread.join(timeout=5)
        assert output.exists()
        assert "no blockers" in output.read_text(encoding="utf-8")
        return f"self-test passed: {task['id']}"

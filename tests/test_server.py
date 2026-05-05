from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from agent_lanes.server import start_in_thread
from agent_lanes.store import HandoffStore


def post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _start_server(tmp_path: Path):
    workspace = tmp_path / "workspace"
    outputs = workspace / "outputs"
    outputs.mkdir(parents=True)
    request_path = outputs / "01-step-output.md"
    request_path.write_text("# Server output\n", encoding="utf-8")
    response_path = outputs / "01-step-review.md"
    store = HandoffStore(tmp_path / "state")
    server, thread = start_in_thread(store)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    return store, server, thread, base, workspace, request_path, response_path


def test_server_create_next_claim_respond_wait_flow(tmp_path: Path) -> None:
    store, server, thread, base, workspace, request_path, response_path = _start_server(tmp_path)

    try:
        created = post_json(
            f"{base}/tasks",
            {
                "workspace_id": "server-workspace",
                "checkpoint_id": "phase-01-review",
                "lane": "claude-review",
                "workspace_root": str(workspace),
                "request_path": str(request_path),
                "response_path": str(response_path),
                "prompt": "Review via server.",
                "metadata": {"min_effort": "high"},
            },
        )["task"]
        task_id = created["id"]
        assert created["metadata"] == {"min_effort": "high"}

        next_task = get_json(f"{base}/tasks/next?lane=claude-review")["task"]
        assert next_task["id"] == task_id

        claimed = post_json(f"{base}/tasks/{task_id}/claim", {"owner": "server-worker"})["task"]
        post_json(
            f"{base}/tasks/{task_id}/response",
            {
                "body": "Server review complete.\n",
                "reviewer": "server-worker",
                "claim_token": claimed["claim_token"],
                "verdict": "accept",
                "blocking_count": 0,
                "metadata": {"model_used": "fake"},
            },
        )
        response = get_json(f"{base}/tasks/{task_id}/response")["response"]

        assert response["body"] == "Server review complete.\n"
        assert response["verdict"] == "accept"
        assert response["blocking_count"] == 0
        assert response["metadata"] == {"model_used": "fake"}
        assert store.get_task(task_id)["state"] == "completed"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_server_release_endpoint_returns_task_to_queued(tmp_path: Path) -> None:
    store, server, thread, base, workspace, request_path, response_path = _start_server(tmp_path)
    try:
        created = post_json(
            f"{base}/tasks",
            {
                "workspace_id": "server-workspace",
                "checkpoint_id": "release-test",
                "lane": "claude-review",
                "workspace_root": str(workspace),
                "request_path": str(request_path),
                "response_path": str(response_path),
                "prompt": "test",
            },
        )["task"]
        task_id = created["id"]
        claimed = post_json(f"{base}/tasks/{task_id}/claim", {"owner": "server-worker"})["task"]

        released = post_json(
            f"{base}/tasks/{task_id}/release",
            {"claim_token": claimed["claim_token"], "reason": "not-a-fit"},
        )["task"]
        assert released["state"] == "queued"
        assert released["claim_token"] is None
    finally:
        server.shutdown()
        thread.join(timeout=5)

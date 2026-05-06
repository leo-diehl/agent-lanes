from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from .config import RuntimeConfig
from .defaults import DEFAULT_CLAIM_LEASE_SECONDS, DEFAULT_WAIT_TIMEOUT_SECONDS
from .errors import StoreError, TimeoutError
from .timeutil import iso_after, iso_now, parse_iso, timestamp_slug, utc_now


TASK_TERMINAL_STATES = {"completed", "failed"}


class HandoffStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.tasks_dir = self.root / "tasks"
        self.index_dir = self.root / "indexes" / "correlations"
        self.lock_path = self.root / "lock"
        self.root.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

    def create_task(
        self,
        *,
        workspace_id: str,
        correlation_id: str | None = None,
        source_agent: str,
        lane: str,
        workspace_root: str | Path,
        worktree_path: str | Path | None,
        expected_branch: str | None,
        request_path: str | Path,
        response_path: str | Path,
        prompt: str,
        supporting_paths: list[str | Path | dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        checkpoint_id: str | None = None,  # legacy alias; prefer correlation_id
    ) -> dict[str, Any]:
        # Accept legacy checkpoint_id arg as a compatibility alias for one minor version.
        if correlation_id is None:
            correlation_id = checkpoint_id
        if correlation_id is None:
            raise StoreError("create_task requires correlation_id")
        workspace_root_path = Path(workspace_root).resolve()
        request = Path(request_path).resolve()
        response = Path(response_path).resolve()
        _require_inside(workspace_root_path, request, "request_path")
        _require_inside(workspace_root_path, response, "response_path")
        if not request.exists() or not request.is_file():
            raise StoreError(f"request source does not exist: {request}")
        supporting = self._supporting_path_records(workspace_root_path, supporting_paths or [])
        request_sha = sha256_file(request)
        created_at = iso_now()
        task_id = self._new_task_id(correlation_id, request_sha)
        task = {
            "id": task_id,
            "workspace_id": workspace_id,
            "correlation_id": correlation_id,
            "source_agent": source_agent,
            "lane": lane,
            "workspace_root": str(workspace_root_path),
            "worktree_path": str(Path(worktree_path).resolve()) if worktree_path else None,
            "expected_branch": expected_branch,
            "request_path": str(request),
            "request_sha256": request_sha,
            "supporting_paths": supporting,
            "response_path": str(response),
            "prompt": prompt,
            "metadata": dict(metadata) if metadata else {},
            "state": "queued",
            "created_at": created_at,
            "updated_at": created_at,
            "claim_owner": None,
            "claim_token": None,
            "lease_expires_at": None,
            "claimed_at": None,
            "completed_at": None,
            "failed_at": None,
            "failure_reason": None,
        }
        with self.locked():
            task_dir = self.task_dir(task_id)
            task_dir.mkdir(parents=True, exist_ok=False)
            atomic_write_json(task_dir / "task.json", task)
            (task_dir / "events.jsonl").touch()
            self._write_correlation_index(workspace_id, correlation_id, task_id)
            self._append_event_unlocked(task_id, "created", "task queued", {"lane": lane})
        return task

    def get_task(self, task_id: str) -> dict[str, Any]:
        task_path = self.task_dir(task_id) / "task.json"
        if not task_path.exists():
            raise StoreError(f"unknown task: {task_id}")
        task = _load_task(task_path)
        # Read-compat: older task.json files may lack metadata.
        if "metadata" not in task:
            task["metadata"] = {}
        return task

    def resolve_ref(self, ref: str, config: RuntimeConfig | None = None) -> str:
        if (self.task_dir(ref) / "task.json").exists():
            return ref
        raise StoreError(f"unknown task: {ref}")

    def latest_task_id(self, workspace_id: str, correlation_id: str) -> str | None:
        index_path = self.index_dir / f"{slug(workspace_id)}--{slug(correlation_id)}.json"
        if not index_path.exists():
            return None
        return json.loads(index_path.read_text(encoding="utf-8"))["task_id"]

    def next_task(self, lane: str, *, wait_seconds: float = 0.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + wait_seconds
        while True:
            with self.locked():
                task = self._next_task_unlocked(lane)
                if task is not None:
                    return task
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    def list_tasks(
        self,
        *,
        lane: str | None = None,
        include_completed: bool = True,
    ) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        with self.locked():
            for task_path in self.tasks_dir.glob("*/task.json"):
                task = _load_task(task_path)
                if "metadata" not in task:
                    task["metadata"] = {}
                if lane is not None and task["lane"] != lane:
                    continue
                if not include_completed and task["state"] in TASK_TERMINAL_STATES:
                    continue
                tasks.append(task)
        tasks.sort(key=lambda item: item["created_at"])
        return tasks

    def claim_task(
        self,
        task_id: str,
        *,
        owner: str,
        lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS,
    ) -> dict[str, Any]:
        with self.locked():
            task = self.get_task(task_id)
            if task["state"] in TASK_TERMINAL_STATES:
                raise StoreError(f"cannot claim {task_id}; task is {task['state']}")
            if task["state"] == "claimed" and not lease_expired(task):
                raise StoreError(f"cannot claim {task_id}; active lease held by {task['claim_owner']}")
            self._verify_request_integrity_unlocked(task)
            now = iso_now()
            task.update(
                {
                    "state": "claimed",
                    "claim_owner": owner,
                    "claim_token": uuid.uuid4().hex,
                    "lease_expires_at": iso_after(lease_seconds),
                    "claimed_at": now,
                    "updated_at": now,
                }
            )
            atomic_write_json(self.task_dir(task_id) / "task.json", task)
            self._append_event_unlocked(task_id, "claimed", "task claimed", {"owner": owner})
            return task

    def renew_claim(
        self,
        task_id: str,
        *,
        claim_token: str,
        lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS,
    ) -> dict[str, Any]:
        with self.locked():
            task = self.get_task(task_id)
            if task["state"] != "claimed":
                raise StoreError(f"cannot renew {task_id}; task is {task['state']}")
            if claim_token != task["claim_token"]:
                raise StoreError("claim token does not match active lease")
            task.update({"lease_expires_at": iso_after(lease_seconds), "updated_at": iso_now()})
            atomic_write_json(self.task_dir(task_id) / "task.json", task)
            self._append_event_unlocked(task_id, "renewed", "claim lease renewed", {"owner": task["claim_owner"]})
            return task

    def release_claim(
        self,
        task_id: str,
        *,
        claim_token: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Return a claimed task to the queued state.

        Performs the claimed -> queued transition. Verifies the claim token,
        clears claim_owner, claim_token, lease_expires_at, claimed_at, and
        appends a 'released' event.
        """
        with self.locked():
            task = self.get_task(task_id)
            if task["state"] != "claimed":
                raise StoreError(f"cannot release {task_id}; task is {task['state']}")
            if claim_token != task["claim_token"]:
                raise StoreError("claim token does not match active lease")
            task.update(
                {
                    "state": "queued",
                    "claim_owner": None,
                    "claim_token": None,
                    "lease_expires_at": None,
                    "claimed_at": None,
                    "updated_at": iso_now(),
                }
            )
            atomic_write_json(self.task_dir(task_id) / "task.json", task)
            self._append_event_unlocked(
                task_id,
                "released",
                "claim released",
                {"reason": reason} if reason else {},
            )
            return task

    def append_event(
        self,
        task_id: str,
        event_type: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self.locked():
            self.get_task(task_id)
            self._append_event_unlocked(task_id, event_type, message, data)

    def submit_response(
        self,
        task_id: str,
        *,
        body: str,
        reviewer: str,
        claim_token: str | None = None,
        status: str = "completed",
        follow_up_required: bool = False,
        verdict: str | None = None,
        blocking_count: int | None = None,
        nonblocking_count: int | None = None,
        expect_sha256: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.locked():
            task = self.get_task(task_id)
            if task["state"] == "completed":
                raise StoreError(f"task is already completed: {task_id}")
            if task["state"] == "failed":
                raise StoreError(f"task is failed: {task_id}")
            if task["state"] == "claimed" and claim_token != task["claim_token"]:
                raise StoreError("claim token does not match active lease")
            if expect_sha256 is not None and expect_sha256 != task["request_sha256"]:
                raise StoreError(
                    f"reviewed request hash does not match task: expected {task['request_sha256']}, got {expect_sha256}"
                )
            # Verdict-conditional logic only fires when verdict is set.
            if verdict is not None and blocking_count is not None and blocking_count > 0 and verdict != "needs-revision":
                raise StoreError("blocking-count > 0 requires verdict needs-revision")
            request_changed = False
            try:
                self._verify_request_integrity_unlocked(task)
            except StoreError:
                if status == "completed":
                    raise
                request_changed = True
            now = iso_now()
            response = {
                "task_id": task_id,
                "reviewer": reviewer,
                "status": status,
                "body": body,
                "reviewed_request_sha256": expect_sha256 or task["request_sha256"],
                "request_changed_before_response": request_changed,
                "follow_up_required": follow_up_required,
                "verdict": verdict,
                "blocking_count": blocking_count,
                "nonblocking_count": nonblocking_count,
                "metadata": dict(metadata) if metadata else {},
                "created_at": now,
            }
            atomic_write_json(self.task_dir(task_id) / "response.json", response)
            task.update(
                {
                    "state": "completed" if status == "completed" else "failed",
                    "updated_at": now,
                    "completed_at": now if status == "completed" else None,
                    "failed_at": now if status != "completed" else None,
                    "failure_reason": None if status == "completed" else body,
                }
            )
            atomic_write_json(self.task_dir(task_id) / "task.json", task)
            self._append_event_unlocked(task_id, "response", "response submitted", {"reviewer": reviewer})
            return response

    def wait_for_response(
        self,
        ref: str,
        *,
        config: RuntimeConfig | None = None,
        timeout: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        task_id = self.resolve_ref(ref, config)
        deadline = time.monotonic() + timeout
        while True:
            response_path = self.task_dir(task_id) / "response.json"
            if response_path.exists():
                response = json.loads(response_path.read_text(encoding="utf-8"))
                if "metadata" not in response:
                    response["metadata"] = {}
                return response
            task = self.get_task(task_id)
            if task["state"] == "failed":
                raise StoreError(task.get("failure_reason") or f"task failed: {task_id}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for response: {ref}")
            time.sleep(0.05)

    def write_response_output(self, task_id: str, response: dict[str, Any]) -> Path:
        task = self.get_task(task_id)
        response_path = Path(task["response_path"])
        workspace_root = Path(task["workspace_root"])
        _require_inside(workspace_root, response_path.resolve(), "response_path")
        response_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(response_path, response["body"])
        return response_path

    def task_dir(self, task_id: str) -> Path:
        return self.tasks_dir / task_id

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _new_task_id(self, correlation_id: str, request_sha: str) -> str:
        return f"{timestamp_slug()}-{slug(correlation_id)}-{request_sha[:12]}"

    def _write_correlation_index(self, workspace_id: str, correlation_id: str, task_id: str) -> None:
        atomic_write_json(
            self.index_dir / f"{slug(workspace_id)}--{slug(correlation_id)}.json",
            {"workspace_id": workspace_id, "correlation_id": correlation_id, "task_id": task_id, "updated_at": iso_now()},
        )

    def _append_event_unlocked(
        self,
        task_id: str,
        event_type: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        event = {"created_at": iso_now(), "type": event_type, "message": message, "data": data or {}}
        event_path = self.task_dir(task_id) / "events.jsonl"
        with event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _next_task_unlocked(self, lane: str) -> dict[str, Any] | None:
        tasks: list[dict[str, Any]] = []
        for task_path in self.tasks_dir.glob("*/task.json"):
            task = _load_task(task_path)
            if task["lane"] != lane:
                continue
            if task["state"] == "queued" or (task["state"] == "claimed" and lease_expired(task)):
                if "metadata" not in task:
                    task["metadata"] = {}
                tasks.append(task)
        tasks.sort(key=lambda item: item["created_at"])
        return tasks[0] if tasks else None

    def _verify_request_integrity_unlocked(self, task: dict[str, Any]) -> None:
        request_path = Path(task["request_path"])
        if not request_path.exists() or not request_path.is_file():
            raise StoreError(f"request source is missing for {task['id']}: {request_path}")
        current_sha = sha256_file(request_path)
        expected_sha = task["request_sha256"]
        if current_sha != expected_sha:
            raise StoreError(
                f"request hash mismatch for {task['id']}: expected {expected_sha}, got {current_sha}"
            )

    def _supporting_path_records(
        self,
        workspace_root: Path,
        supporting_paths: list[str | Path | dict[str, Any]],
    ) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        for index, item in enumerate(supporting_paths):
            raw_path = item.get("path") if isinstance(item, dict) else item
            if raw_path is None:
                raise StoreError(f"supporting_paths[{index}] is missing path")
            path = Path(str(raw_path)).expanduser()
            if not path.is_absolute():
                path = workspace_root / path
            path = path.resolve()
            _require_inside(workspace_root, path, f"supporting_paths[{index}]")
            if not path.exists() or not path.is_file():
                raise StoreError(f"supporting path does not exist: {path}")
            records.append({"path": str(path), "sha256": sha256_file(path)})
        return records


def _load_task(task_path: Path) -> dict[str, Any]:
    """Read a task.json from disk, applying a read-compat shim for the legacy
    ``checkpoint_id`` field name.

    For one minor version we accept either ``correlation_id`` (new) or
    ``checkpoint_id`` (legacy). When both are present, ``correlation_id`` wins.
    The legacy key is dropped from the in-memory representation so callers see
    a single canonical name.
    """
    task = json.loads(task_path.read_text(encoding="utf-8"))
    if "correlation_id" not in task and "checkpoint_id" in task:
        task["correlation_id"] = task["checkpoint_id"]
    task.pop("checkpoint_id", None)
    return task


def lease_expired(task: dict[str, Any]) -> bool:
    expiry = parse_iso(task.get("lease_expires_at"))
    return expiry is not None and expiry <= utc_now()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-") or "item"


def _require_inside(root: Path, candidate: Path, label: str) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise StoreError(f"{label} is outside workspace_root: {candidate}") from exc

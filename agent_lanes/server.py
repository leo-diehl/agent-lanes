from __future__ import annotations

import json
import threading
from json import JSONDecodeError
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .defaults import DEFAULT_CLAIM_LEASE_SECONDS, DEFAULT_NEXT_TIMEOUT_SECONDS, DEFAULT_WAIT_TIMEOUT_SECONDS
from .errors import HandoffError, TimeoutError
from .store import HandoffStore


class HandoffHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], store: HandoffStore):
        self.store = store
        super().__init__(server_address, HandoffRequestHandler)


class HandoffRequestHandler(BaseHTTPRequestHandler):
    server: HandoffHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/tasks/next":
                query = parse_qs(parsed.query)
                lane = _first(query, "lane")
                wait_seconds = float(_first(query, "wait_seconds", str(DEFAULT_NEXT_TIMEOUT_SECONDS)))
                if not lane:
                    self._send_error(HTTPStatus.BAD_REQUEST, "lane is required")
                    return
                task = self.server.store.next_task(lane, wait_seconds=wait_seconds)
                self._send_json({"task": task})
                return
            if parsed.path.startswith("/tasks/") and parsed.path.endswith("/response"):
                task_id = parsed.path.split("/")[2]
                wait_seconds = float(_first(parse_qs(parsed.query), "wait_seconds", str(DEFAULT_WAIT_TIMEOUT_SECONDS)))
                response = self.server.store.wait_for_response(task_id, timeout=wait_seconds)
                self._send_json({"response": response})
                return
            if parsed.path.startswith("/tasks/"):
                task_id = parsed.path.split("/")[2]
                self._send_json({"task": self.server.store.get_task(task_id)})
                return
            self._send_error(HTTPStatus.NOT_FOUND, "not found")
        except TimeoutError as exc:
            self._send_error(HTTPStatus.REQUEST_TIMEOUT, str(exc))
        except ValueError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except HandoffError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            body = self._read_json()
            if parsed.path == "/tasks":
                # Accept either correlation_id (new) or checkpoint_id (legacy alias).
                correlation_id = body.get("correlation_id") or body.get("checkpoint_id")
                if correlation_id is None:
                    raise KeyError("correlation_id")
                task = self.server.store.create_task(
                    workspace_id=body["workspace_id"],
                    correlation_id=correlation_id,
                    source_agent=body.get("source_agent", "api"),
                    lane=body["lane"],
                    workspace_root=Path(body["workspace_root"]),
                    worktree_path=Path(body["worktree_path"]) if body.get("worktree_path") else None,
                    expected_branch=body.get("expected_branch"),
                    request_path=Path(body["request_path"]),
                    response_path=Path(body["response_path"]),
                    prompt=body.get("prompt", ""),
                    supporting_paths=body.get("supporting_paths", []),
                    metadata=body.get("metadata"),
                )
                self._send_json({"task": task}, HTTPStatus.CREATED)
                return
            if parsed.path.startswith("/tasks/") and parsed.path.endswith("/claim"):
                task_id = parsed.path.split("/")[2]
                task = self.server.store.claim_task(
                    task_id,
                    owner=body.get("owner", "worker"),
                    lease_seconds=float(body.get("lease_seconds", DEFAULT_CLAIM_LEASE_SECONDS)),
                )
                self._send_json({"task": task})
                return
            if parsed.path.startswith("/tasks/") and parsed.path.endswith("/release"):
                task_id = parsed.path.split("/")[2]
                task = self.server.store.release_claim(
                    task_id,
                    claim_token=body.get("claim_token"),
                    reason=body.get("reason"),
                )
                self._send_json({"task": task})
                return
            if parsed.path.startswith("/tasks/") and parsed.path.endswith("/events"):
                task_id = parsed.path.split("/")[2]
                self.server.store.append_event(
                    task_id,
                    body.get("type", "note"),
                    body.get("message", ""),
                    body.get("data"),
                )
                self._send_json({"ok": True})
                return
            if parsed.path.startswith("/tasks/") and parsed.path.endswith("/response"):
                task_id = parsed.path.split("/")[2]
                response = self.server.store.submit_response(
                    task_id,
                    body=body.get("body", ""),
                    reviewer=body.get("reviewer", "worker"),
                    claim_token=body.get("claim_token"),
                    status=body.get("status", "completed"),
                    follow_up_required=bool(body.get("follow_up_required", False)),
                    verdict=body.get("verdict"),
                    blocking_count=body.get("blocking_count"),
                    nonblocking_count=body.get("nonblocking_count"),
                    expect_sha256=body.get("expect_sha256"),
                    metadata=body.get("metadata"),
                )
                self._send_json({"response": response})
                return
            self._send_error(HTTPStatus.NOT_FOUND, "not found")
        except KeyError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, f"missing field: {exc.args[0]}")
        except (JSONDecodeError, ValueError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except HandoffError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("request JSON must be an object")
        return data

    def _send_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status)


def serve(store: HandoffStore, host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = HandoffHTTPServer((host, port), store)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def start_in_thread(store: HandoffStore, host: str = "127.0.0.1", port: int = 0) -> tuple[HandoffHTTPServer, threading.Thread]:
    httpd = HandoffHTTPServer((host, port), store)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


def _first(query: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
    values = query.get(key)
    if not values:
        return default
    return values[0]

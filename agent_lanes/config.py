from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError


# Engine-config keys are workspace metadata + lanes only. checkpoints: is rejected
# with a clear error pointing at task files.
ENGINE_CONFIG_ALLOWED_KEYS = {
    "workspace_id",
    "workspace_root",
    "queue_root",
    "lanes",
    # legacy/optional engine hints kept for backward compatibility
    "worktree_path",
    "expected_branch",
}

# Task-file loader accepts these fields. Unknown keys warn and are ignored.
TASK_FILE_ALLOWED_KEYS = {
    "lane",
    "metadata",
    "prompt",
    "prompt_file",
    "request_from",
    "response_to",
    "supporting_paths",
    "worktree_path",
    "branch",
}


@dataclass(frozen=True)
class LaneConfig:
    name: str
    description: str = ""


@dataclass(frozen=True)
class RuntimeConfig:
    path: Path
    config_dir: Path
    workspace_id: str
    workspace_root: Path
    store_root: Path
    worktree_path: Path | None
    expected_branch: str | None
    lanes: dict[str, LaneConfig] = field(default_factory=dict)


def load_config(config_path: str | Path, store_override: str | Path | None = None) -> RuntimeConfig:
    path = Path(config_path).expanduser()
    if not path.exists():
        raise ConfigError(f"config file does not exist: {path}")
    path = path.resolve()
    data = _load_mapping(path)
    config_dir = path.parent

    if "checkpoints" in data:
        raise ConfigError(
            "checkpoints in handoff.yaml are no longer supported. Move task definitions to "
            "standalone files (e.g. tasks/<id>.yaml) and submit with --task <path>. "
            "See CONTRACT.md."
        )

    workspace_id = str(data.get("workspace_id") or "").strip()
    if not workspace_id:
        raise ConfigError("handoff config must define workspace_id")

    workspace_root_raw = data.get("workspace_root", "..")
    workspace_root = _resolve_path(config_dir, workspace_root_raw).resolve()

    queue_root_raw = store_override if store_override is not None else data.get("queue_root", "state")
    store_root = _resolve_path(config_dir, queue_root_raw).resolve()
    _require_inside(workspace_root, store_root, "queue_root")

    worktree_raw = data.get("worktree_path")
    worktree_path = _resolve_path(config_dir, worktree_raw).resolve() if worktree_raw else None
    expected_branch = data.get("expected_branch")

    lanes = _load_lanes(data.get("lanes"))

    return RuntimeConfig(
        path=path,
        config_dir=config_dir,
        workspace_id=workspace_id,
        workspace_root=workspace_root,
        store_root=store_root,
        worktree_path=worktree_path,
        expected_branch=str(expected_branch) if expected_branch else None,
        lanes=lanes,
    )


def load_task_file(task_path: str | Path) -> dict[str, Any]:
    """Load a standalone task-definition YAML/JSON file.

    Returns a dict containing only the recognized task fields. Unknown keys are
    logged to stderr and ignored (forward compat).
    """
    path = Path(task_path).expanduser()
    if not path.exists():
        raise ConfigError(f"task file does not exist: {path}")
    path = path.resolve()
    data = _load_mapping(path)

    accepted: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in data.items():
        if key in TASK_FILE_ALLOWED_KEYS:
            accepted[key] = value
        else:
            unknown.append(key)
    if unknown:
        print(
            f"agent-lanes: warning: task file {path} has unknown keys (ignored): "
            + ", ".join(sorted(unknown)),
            file=sys.stderr,
        )

    # Resolve prompt_file relative to the task file's directory.
    if "prompt_file" in accepted and accepted["prompt_file"] is not None:
        prompt_file = Path(str(accepted["prompt_file"])).expanduser()
        if not prompt_file.is_absolute():
            prompt_file = path.parent / prompt_file
        accepted["prompt_file"] = str(prompt_file.resolve())

    metadata = accepted.get("metadata")
    if metadata is None:
        accepted["metadata"] = {}
    elif not isinstance(metadata, dict):
        raise ConfigError(f"task file {path}: metadata must be a mapping")
    return accepted


def _load_lanes(raw: Any) -> dict[str, LaneConfig]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("lanes must be a mapping")
    lanes: dict[str, LaneConfig] = {}
    for name, value in raw.items():
        description = ""
        if isinstance(value, dict):
            description = str(value.get("description") or "")
        elif value is not None:
            description = str(value)
        lanes[str(name)] = LaneConfig(name=str(name), description=description)
    return lanes


def _load_mapping(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(raw)
    except ModuleNotFoundError:
        data = json.loads(raw)
    except Exception as exc:  # pragma: no cover - exact parser errors vary
        raise ConfigError(f"failed to parse config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config must parse to a mapping")
    return data


def _resolve_path(base: Path, value: Any) -> Path:
    if value is None:
        raise ConfigError("path value is required")
    candidate = Path(str(value)).expanduser()
    if candidate.is_absolute():
        return candidate
    return base / candidate


def _require_inside(root: Path, candidate: Path, label: str) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ConfigError(f"{label} escapes workspace root: {candidate}") from exc

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError


@dataclass(frozen=True)
class CheckpointConfig:
    id: str
    lane: str
    request_from: Path
    response_to: Path
    prompt: str
    required: bool = True
    supporting_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class RuntimeConfig:
    path: Path
    config_dir: Path
    pack_id: str
    pack_root: Path
    store_root: Path
    worktree_path: Path | None
    expected_branch: str | None
    checkpoints: dict[str, CheckpointConfig]


def load_config(config_path: str | Path, store_override: str | Path | None = None) -> RuntimeConfig:
    path = Path(config_path).expanduser()
    if not path.exists():
        raise ConfigError(f"config file does not exist: {path}")
    path = path.resolve()
    data = _load_mapping(path)
    config_dir = path.parent

    pack_id = str(data.get("pack_id") or "").strip()
    if not pack_id:
        raise ConfigError("handoff config must define pack_id")

    pack_root_raw = data.get("pack_root", "..")
    pack_root = _resolve_path(config_dir, pack_root_raw).resolve()

    queue_root_raw = store_override if store_override is not None else data.get("queue_root", "state")
    store_root = _resolve_path(config_dir, queue_root_raw).resolve()
    _require_inside(pack_root, store_root, "queue_root")

    worktree_raw = data.get("worktree_path")
    worktree_path = _resolve_path(config_dir, worktree_raw).resolve() if worktree_raw else None
    expected_branch = data.get("expected_branch")

    raw_checkpoints = data.get("checkpoints")
    if not isinstance(raw_checkpoints, dict) or not raw_checkpoints:
        raise ConfigError("handoff config must define at least one checkpoint")

    checkpoints: dict[str, CheckpointConfig] = {}
    for checkpoint_id, raw in raw_checkpoints.items():
        if not isinstance(raw, dict):
            raise ConfigError(f"checkpoint {checkpoint_id!r} must be a mapping")
        lane = str(raw.get("lane") or "").strip()
        if not lane:
            raise ConfigError(f"checkpoint {checkpoint_id!r} must define lane")
        request_from = _resolve_path(config_dir, raw.get("request_from")).resolve()
        response_to = _resolve_path(config_dir, raw.get("response_to")).resolve()
        _require_inside(pack_root, request_from, f"{checkpoint_id}.request_from")
        _require_inside(pack_root, response_to, f"{checkpoint_id}.response_to")
        supporting_paths = _supporting_paths(config_dir, pack_root, checkpoint_id, raw.get("supporting_paths", []))
        prompt = str(raw.get("prompt") or "").strip()
        checkpoints[str(checkpoint_id)] = CheckpointConfig(
            id=str(checkpoint_id),
            lane=lane,
            request_from=request_from,
            response_to=response_to,
            prompt=prompt,
            required=bool(raw.get("required", True)),
            supporting_paths=tuple(supporting_paths),
        )

    return RuntimeConfig(
        path=path,
        config_dir=config_dir,
        pack_id=pack_id,
        pack_root=pack_root,
        store_root=store_root,
        worktree_path=worktree_path,
        expected_branch=str(expected_branch) if expected_branch else None,
        checkpoints=checkpoints,
    )


def _load_mapping(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(raw)
    except ModuleNotFoundError:
        data = json.loads(raw)
    except Exception as exc:  # pragma: no cover - exact parser errors vary
        raise ConfigError(f"failed to parse handoff config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("handoff config must parse to a mapping")
    return data


def _resolve_path(base: Path, value: Any) -> Path:
    if value is None:
        raise ConfigError("path value is required")
    candidate = Path(str(value)).expanduser()
    if candidate.is_absolute():
        return candidate
    return base / candidate


def _supporting_paths(config_dir: Path, pack_root: Path, checkpoint_id: object, raw_value: Any) -> list[Path]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise ConfigError(f"{checkpoint_id}.supporting_paths must be a list")
    paths: list[Path] = []
    for index, item in enumerate(raw_value):
        raw_path = item.get("path") if isinstance(item, dict) else item
        path = _resolve_path(config_dir, raw_path).resolve()
        _require_inside(pack_root, path, f"{checkpoint_id}.supporting_paths[{index}]")
        paths.append(path)
    return paths


def _require_inside(root: Path, candidate: Path, label: str) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ConfigError(f"{label} escapes pack root: {candidate}") from exc

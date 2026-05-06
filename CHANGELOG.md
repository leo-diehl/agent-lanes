# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Renamed the `task.json.checkpoint_id` field to `correlation_id`. Older
  on-disk task records are read transparently via a one-minor-version
  read-compat shim and rewritten under the canonical name on the next state
  mutation. The legacy key will be dropped in v0.2.
- Renamed the on-disk index directory `indexes/checkpoints/` to
  `indexes/correlations/` to match the field rename.
- Renamed the `agent-lanes status --rack` flag to `--all`. `--rack` is
  retained as a deprecated alias that emits a stderr warning at runtime and
  will be removed in v0.2.
- `agent-lanes init-pool` scaffolds the dispatcher folder as `dispatchers/`
  instead of `_dispatchers/`. The leading underscore was an unconventional
  marker for a directory the user is expected to edit.

### Removed
- `agent-lanes init` no longer scaffolds `POLLING-MONITOR-PROMPT.md`. The
  pool template's `POLLING-CHAT-PROMPT.md` is the canonical Mode A artifact;
  the per-project workspace did not need a second polling prompt.

### Documented
- `dispatcher.sh` header now explains that the bundled vendor mapping covers
  Claude and Codex by convention only; the protocol treats `required_vendor`
  as an opaque string. Forks targeting other vendors should adjust
  `model_flag()` and `effort_flag()`.

### Documented
- `status --lane <name>` and `status --active-only` are now described in
  CONTRACT.md alongside `--all`.

## [0.1.0] - 2026-05-05

### Added
- File-backed structured-RPC queue with lane routing and lease-based claim/respond.
- Vendor-routed Mode B dispatcher template (`agent_lanes/templates/workspace/dispatcher.sh`) that long-polls a lane, inspects task metadata, claims matching tasks, and spawns a fresh headless agent at the requested model and effort.
- Mode A polling chat prompt template (`agent_lanes/templates/pool/dispatchers/POLLING-CHAT-PROMPT.md`) that turns any chat into a long-running dispatcher that spawns sub-agents per task.
- `agent-lanes init` scaffolds a per-rack engine; `--queue-root <path>` points it at a shared queue.
- `agent-lanes init-pool <path>` scaffolds a workspace-level shared queue plus per-vendor dispatcher wrappers and the polling chat prompt.
- Free-form `metadata: {}` on tasks and responses; convention keys (`required_vendor`, `model_class`, `effort`, `thread_id`, `parent_task_id`, etc.) are documented but not enforced.
- `release` command for probe-then-decline: returns a claimed task to `queued` without responding.
- Optional `--verdict` on `respond` so non-review tasks can complete without a verdict.
- Local HTTP server (`agent-lanes serve`) for curl-based integrations.
- SHA-256 pinning on `request_path` and `supporting_paths` so reviewers can detect stale input.

### Notes
- Python >=3.11. POSIX-only (`fcntl.flock`); Windows users should run via WSL2.
- v0.1 - APIs may change before v1.0.

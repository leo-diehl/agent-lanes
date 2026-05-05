# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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

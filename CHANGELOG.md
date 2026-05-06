# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-05-06

Initial public release.

### Added

- File-backed structured-RPC queue with lane routing and lease-based
  claim / respond. Process-local locking via `fcntl.flock`. Atomic state
  writes via temp file + `os.replace`.
- Bundled Mode B dispatcher template
  (`agent_lanes/templates/workspace/dispatcher.sh`) that long-polls a lane,
  inspects task metadata, claims matching tasks, and spawns a fresh headless
  agent (`claude -p`, `codex exec`) at the requested model and effort. Default
  15-minute claim lease, renewed every 60 seconds while the headless child
  runs; survives transient renew failures with a 5-attempt retry; aborts if
  the dispatcher's parent process disappears.
- Bundled Mode A polling chat prompt template
  (`agent_lanes/templates/pool/dispatchers/POLLING-CHAT-PROMPT.md`) that turns
  any chat into a long-running dispatcher that spawns sub-agents per task.
- `agent-lanes init` scaffolds a per-project engine; `--queue-root <path>`
  points it at a shared queue.
- `agent-lanes init-pool <path>` scaffolds a workspace-level shared queue
  plus per-vendor dispatcher wrappers and the polling chat prompt.
- Free-form `metadata: {}` field on tasks and responses. Convention keys
  (`required_vendor`, `model_class`, `effort`, `thread_id`, `parent_task_id`)
  documented but not enforced.
- `release <task-id>` command for probe-then-decline: returns a claimed task
  to `queued` without responding.
- `event <task-id> [--type ...] [--message ...] [--data key=value]` command
  for appending diagnostic events to a task's event log. Bundled dispatcher
  emits `dispatcher_started`, `headless_started`, `headless_completed`,
  `headless_failed` automatically.
- Optional `--verdict` on `respond` so non-review tasks can complete without
  a verdict.
- Local HTTP server (`agent-lanes serve`) for curl-based integrations. Warns
  on stderr when bound to a non-loopback host (no auth layer).
- SHA-256 pinning on `request_path` and `supporting_paths` so reviewers can
  detect stale input.
- Rich `wait` diagnostics on claimed tasks: `claim_owner`,
  `claimed_age_seconds`, `lease_expires_at`, `lease_expired`, `response_path`,
  `response_exists`, `last_event`, `next_action`. Compact one-line form for
  queued tasks.
- `docs/prompt-pack-guide.md` — opinionated convention for organizing
  multi-prompt packs that use agent-lanes underneath. Optional; not protocol.

### Security

- `prompt_file` in task YAML files is constrained to `workspace_root`.
- Queue state directories and files are created with restrictive permissions
  (`0700` / `0600`).
- `task_id` values are validated against a regex before being joined into
  filesystem paths, preventing traversal via store / CLI / HTTP routes.
- `init-pool` shell-escapes paths substituted into generated wrapper scripts.
- `agent-lanes serve` warns when binding to a non-loopback host.

### Notes

- Python ≥ 3.11. POSIX-only — Windows users should run via WSL2. Single-host
  by design; networked filesystems (NFS, SMB) are not supported.
- v0.1 — APIs may change before v1.0.

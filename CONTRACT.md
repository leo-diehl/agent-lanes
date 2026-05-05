# Agent Handoff Runtime Contract

Status: implementation contract
Date: 2026-05-02

## Scope

The handoff runtime is private local operational tooling for prompt packs. It coordinates structured
review checkpoints between local agents by storing tasks on disk and exposing a CLI plus a small
HTTP server.

It is not a hosted service, authorization boundary, remote-worker system, or product runtime.

## Store Layout

For a pack-local config at `<pack>/handoff/handoff.yaml`, the default store is:

```text
<pack>/handoff/state/
  tasks/
    <task-id>/
      task.json
      events.jsonl
      response.json
  indexes/
    checkpoints/
      <pack-id>--<checkpoint-id>.json
  lock
```

All JSON state writes use a temporary file followed by `os.replace`. State transitions are guarded
by a process-local file lock using `fcntl.flock` on POSIX systems.

## Config Shape

```yaml
pack_id: example-pack
pack_root: ..
queue_root: state
worktree_path: /absolute/path/to/worktree
expected_branch: optional-branch-name
checkpoints:
  phase-01-review:
    lane: claude-review
    request_from: ../outputs/01-step-output.md
    supporting_paths:
      - ../outputs/00-context.md
    response_to: ../outputs/01-step-review.md
    required: true
    prompt: |
      Review this output for unsupported claims and missing evidence.
```

Paths are relative to the `handoff.yaml` directory unless absolute. `pack_root`, `queue_root`,
`request_from`, `supporting_paths`, and `response_to` must resolve inside the pack root. Path
traversal that escapes the pack root is rejected.

## Task States

Allowed states:

```text
queued
claimed
completed
failed
```

Allowed transitions:

```text
queued -> claimed
queued -> completed       # only for direct local test/fallback flows
claimed -> claimed        # only when the existing lease has expired
claimed -> completed
claimed -> failed
queued -> failed
```

A claimed task records `claim_owner`, `claim_token`, `lease_expires_at`, and `claimed_at`.

## Task JSON

`task.json` records:

- `id`
- `pack_id`
- `checkpoint_id`
- `source_agent`
- `lane`
- `pack_root`
- `worktree_path`
- `expected_branch`
- `request_path`
- `request_sha256`
- `supporting_paths` as `[{path, sha256}]`
- `response_path`
- `prompt`
- `state`
- `created_at`
- `updated_at`
- `claim_owner`
- `claim_token`
- `lease_expires_at`
- `completed_at`
- `failed_at`
- `failure_reason`

## Response JSON

`response.json` records:

- `task_id`
- `reviewer`
- `status`
- `body`
- `reviewed_request_sha256`
- `request_changed_before_response`
- `follow_up_required`
- `verdict`
- `blocking_count`
- `nonblocking_count`
- `created_at`

The CLI `wait` command writes `body` to the checkpoint `response_to` path after a response arrives.

## Event JSONL

Each event line records:

- `created_at`
- `type`
- `message`
- optional `data`

Events are append-only under the task lock.

## CLI

Supported commands:

```text
submit <checkpoint-id>
wait <task-id-or-checkpoint-id>
wait --lane <lane>
next --lane <lane>
watch --lane <lane>
claim <task-id>
renew <task-id>
respond <task-id>
list [--lane <lane>]
status <task-id-or-checkpoint-id>
status --rack
serve
self-test
```

Common options:

```text
--config <path-to-handoff.yaml>
--store <state-directory>
--timeout <seconds>
--quiet
```

Default wait mechanics are intentionally long-poll friendly:

- `next` and `watch`: 21,600 seconds
- `wait --lane`: 21,600 seconds
- `wait`: 21,600 seconds
- `claim`: 7,200 second lease

Reviewer workers should treat a quiet long-poll as normal waiting behavior. If a long-poll times
out without a task, run it again.

The CLI prints keepalive messages to stderr while waiting so monitor runtimes do not interpret a
quiet long-poll as a dead process.

When a low-cost/background agent is available, it should own lane polling. That monitor should run
`wait --lane <lane> --json`, report returned task JSON, and keep the lane armed without claiming.
The main reviewer should claim only after it has the task JSON and is ready to review.

After either side sends its handoff message, it should schedule the next relevant wait after a short
optimistic delay based on expected response time from the other side. The delay avoids noisy
immediate polling; it must not turn standby operation into a one-shot.

If a worker harness has a shorter foreground timeout than the handoff wait, the worker should use
the harness's background/monitor mechanism. `--quiet` suppresses keepalive output for monitors that
can safely remain silent until final JSON.

`claim` verifies the current request file SHA-256 against `request_sha256` and refuses stale input.
`respond` repeats that verification before accepting a completed response.

`respond --file` copies file contents into `response.json`; it does not move or reference the file.
`respond --file -` reads the response body from stdin.
`respond` also accepts machine-readable routing metadata:

```text
--expect-sha256 <request_sha256>
--verdict accept|accept-with-follow-ups|needs-revision
--blocking-count <int>
--nonblocking-count <int>
```

If `--blocking-count` is greater than zero, `--verdict` must be `needs-revision`.

`accept-with-follow-ups` and non-blocking feedback are not discard signals. Orchestrators should
apply every sensible suggestion within scope and record a concrete reason for anything skipped or
deferred.

## HTTP Server

The server is intentionally small and local. Routes:

```text
POST /tasks
GET  /tasks/next?lane=<lane>&wait_seconds=<n>
POST /tasks/<task_id>/claim
POST /tasks/<task_id>/events
POST /tasks/<task_id>/response
GET  /tasks/<task_id>/response?wait_seconds=<n>
GET  /tasks/<task_id>
```

## Required Proof

Tests and `python3 -m agent_handoff self-test` must prove:

```text
temp prompt pack
-> submit checkpoint
-> monitor waits by lane and receives task JSON without claiming
-> worker claims checkpoint
-> worker responds
-> waiting caller receives response
-> response markdown is written to the configured output path
```

Tests should also cover expected idle output before any checkpoint is submitted, claimed-task status
guidance, response-time request hash verification, verdict/count consistency, and pinned
`supporting_paths`.

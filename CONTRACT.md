# agent-lanes Protocol Contract

Status: implementation contract
Version: v0.1

## 1. Overview

agent-lanes is structured RPC over a file-backed queue. An orchestrator submits a
task to a named lane; a dispatcher (or a human at a terminal) claims the task,
performs the work, and submits a response. The waiting orchestrator then receives
the response. Review/checkpoint is one common application of this protocol; it is
not the only one.

The runtime is local operational tooling: a CLI plus a small HTTP server backed by
the filesystem. It is not a hosted service, an authorization boundary, or a
remote-worker system.

## 2. Architectural separation

The engine and the tasks are separate concerns:

- **The engine** is the `handoff/` folder. It contains workspace metadata, lane
  definitions, the dispatcher script, the CLI wrapper, and runtime state. The engine
  does not reference specific tasks.
- **Tasks** are standalone YAML definitions kept outside `handoff/` (commonly under
  a `tasks/` folder at the project root). Each task references a lane by name. The
  engine never enumerates task files.

This separation is enforced by the loader: the engine config (`handoff.yaml`) accepts
only engine fields. A `checkpoints:` key in `handoff.yaml` is rejected with a clear
error pointing at task files.

## 3. Store layout

For an engine config at `<project>/handoff/handoff.yaml`, the default store is:

```text
<project>/handoff/state/
  tasks/
    <task-id>/
      task.json
      events.jsonl
      response.json
  indexes/
    correlations/
      <workspace-id>--<correlation-id>.json
  lock
```

All JSON state writes use a temporary file followed by `os.replace`. State
transitions are guarded by a process-local file lock using `fcntl.flock` on POSIX
systems.

## 4. Engine config (`handoff.yaml`)

```yaml
workspace_id: example-workspace
workspace_root: ..
queue_root: state

lanes:
  default:
    description: general-purpose lane
  claude-reviewer:
    description: deep-review tier
```

Accepted fields:

- `workspace_id` (required) — string identifying the workspace.
- `workspace_root` (required) — path to the project root, relative to the
  `handoff.yaml` directory or absolute.
- `queue_root` — directory where queue state is written; defaults to `state`.
  Point multiple projects at the same shared absolute queue path to run a
  workspace-level dispatcher pool.
- `lanes` — mapping of lane names to optional metadata.

Path traversal that escapes `workspace_root` is rejected.

`checkpoints:` is **not accepted** in the engine config. If the loader encounters
this key, it raises:

```
checkpoints in handoff.yaml are no longer supported. Move task definitions to
standalone files (e.g. tasks/<id>.yaml) and submit with --task <path>.
See CONTRACT.md.
```

## 5. Task definitions

A task definition is a standalone YAML file describing a reusable task pattern.
The engine never enumerates these; the orchestrator passes a path explicitly.

Accepted fields (all optional unless noted):

- `lane` (required at submit time) — lane name to route to.
- `metadata` — free-form `dict` of declarative defaults; merged with any
  `--metadata key=value` flags supplied at submit time.
- `prompt` — inline prompt body string.
- `prompt_file` — path to a file containing the prompt body. Resolved relative
  to the task file's directory.
- `request_from` — default request artifact path.
- `response_to` — default path where the response body should be written.
- `supporting_paths` — list of additional file paths (inside `workspace_root`)
  recorded as `[{path, sha256}]` on the task. Task-file-only; no CLI flag.
- `worktree_path` — optional repository worktree.
- `branch` — expected branch name.

Per-execution overrides are supplied via CLI flags at submit time. The task file
is the default, the CLI is the override. Unknown keys in task files are warned
about and ignored (forward compat).

Task files live anywhere; convention is `tasks/<id>.yaml` at the project root.

## 6. Task states

```text
queued
claimed
completed
failed
```

## 7. Allowed transitions

```text
queued    -> claimed       # via claim
queued    -> completed     # only for direct local fallback flows
queued    -> failed
claimed   -> claimed       # only when the existing lease has expired
claimed   -> completed     # via respond (status=completed)
claimed   -> failed        # via respond (status=failed)
claimed   -> queued        # via release
```

A claimed task records `claim_owner`, `claim_token`, `lease_expires_at`,
and `claimed_at`. `release` clears these and returns the task to `queued`.

## 8. Task JSON shape

`task.json` records:

- `id`
- `workspace_id`
- `correlation_id` (free-form correlation tag)
- `source_agent`
- `lane`
- `workspace_root`
- `worktree_path`
- `expected_branch`
- `request_path`
- `request_sha256`
- `supporting_paths` as `[{path, sha256}]`
- `response_path`
- `prompt`
- `metadata` — free-form dict (default `{}`)
- `state`
- `created_at`
- `updated_at`
- `claim_owner`
- `claim_token`
- `lease_expires_at`
- `claimed_at`
- `completed_at`
- `failed_at`
- `failure_reason`

Compatibility note: `correlation_id` was previously named `checkpoint_id`. Older
on-disk `task.json` files that still use `checkpoint_id` are read transparently
for one minor version and rewritten under the canonical name on the next state
mutation; the legacy key will be dropped in v0.2.

## 9. Response JSON shape

`response.json` records:

- `task_id`
- `reviewer`
- `status`
- `body`
- `reviewed_request_sha256`
- `request_changed_before_response`
- `follow_up_required`
- `verdict` — optional; `null` for non-review tasks
- `blocking_count` — optional
- `nonblocking_count` — optional
- `metadata` — free-form dict (default `{}`)
- `created_at`

The CLI `wait` command writes `body` to the configured `response_path` after a
response arrives.

## 10. Event JSONL

Each event line records:

- `created_at`
- `type`
- `message`
- optional `data`

Events are append-only under the task lock. Event types include `created`,
`claimed`, `renewed`, `released`, `response`.

## 11. CLI

```text
init [--workspace-id NAME] [--workspace-root PATH] [--queue-root PATH] [PATH]
submit [--task <path>] [--task-id <correlation-id>] [--lane <lane>]
       [--request-from <path>] [--response-to <path>]
       [--worktree-path <path>] [--branch <name>]
       [--prompt <text>] [--prompt-file <path>] [--metadata key=value]...
       [--source-agent <name>] [--json]
wait <task-id> [--timeout <seconds>] [--json]
wait --lane <lane> [--timeout <seconds>] [--json]
next --lane <lane>
watch --lane <lane>
claim <task-id> [--owner <name>] [--lease-seconds <n>]
renew <task-id> --claim-token <token> [--lease-seconds <n>]
respond <task-id> --claim-token <token> [--file <path>|-] [--body <text>]
        [--reviewer <name>] [--status completed|failed]
        [--verdict accept|accept-with-follow-ups|needs-revision]
        [--blocking-count <n>] [--nonblocking-count <n>]
        [--metadata key=value]... [--expect-sha256 <sha>] [--json]
release <task-id> --claim-token <token> [--reason <text>] [--json]
list [--lane <lane>] [--active-only]
status <task-id>
status --rack
serve [--host HOST] [--port PORT]
self-test
```

`submit` does **not** take a positional task-id argument; it takes `--task <path>`
and/or inline flags. `wait` and `status` accept task IDs only — there is no
checkpoint lookup.

`--task-id <correlation-id>` is an optional free-form correlation tag stored on
the task record. It is not used for lookup; `wait` and `status` always take the
generated task id, never the correlation id. When omitted, the correlation id
defaults to the task-file basename (if `--task` is supplied) or the lane name.

`init --queue-root <path>` scaffolds the engine config with `queue_root` pointing
at an existing or future queue state directory. This is commonly used to point
many projects at one workspace-level queue.

`--verdict` on `respond` is optional. The verdict-conditional logic
(`--blocking-count > 0` requires `needs-revision`) only fires when verdict is set.

`--metadata key=value` is repeatable on both `submit` and `respond`.

Long-poll defaults:

- `wait`, `wait --lane`, `next`, `watch`: 21,600 seconds.
- `claim` lease: 7,200 seconds.

`claim` verifies the current request file SHA-256 against `request_sha256` and
refuses stale input. `respond` repeats that verification before accepting a
response. `--expect-sha256 <request_sha256>` pins the revision the reviewer
actually reviewed.

## 12. HTTP routes

```text
POST /tasks
GET  /tasks/next?lane=<lane>&wait_seconds=<n>
POST /tasks/<task_id>/claim
POST /tasks/<task_id>/events
POST /tasks/<task_id>/response
POST /tasks/<task_id>/release
GET  /tasks/<task_id>/response?wait_seconds=<n>
GET  /tasks/<task_id>
```

## 13. Common patterns

**Review / checkpoint.** Orchestrator submits an artifact; reviewer claims, reviews,
responds with `--verdict accept|accept-with-follow-ups|needs-revision`. The
orchestrator's `wait` writes the response body to the configured `response_to` path,
and the orchestrator integrates the verdict into its next step.

**Q&A.** Orchestrator submits a question to a specialist lane (e.g. a security
expert, a database expert). The specialist responds without a verdict. The
orchestrator integrates the answer into its working context. Verdict is null;
metadata may carry source/effort/etc.

**Delegation.** Orchestrator fans out to N children — possibly on different lanes
— each receiving one subtask. The orchestrator captures all task IDs and waits on
each in turn. Children run in parallel because the dispatcher is task-agnostic and
multiple dispatchers can poll the same lane. Each child's response feeds the
parent's aggregation step.

**Pipeline.** Stage 1's response becomes stage 2's request. The orchestrator chains
submits, possibly across multiple lanes (e.g. summarizer → critic → editor).
Threading metadata (`thread_id`, `parent_task_id`) lets dispatchers reconstruct
conversational continuity by walking the parent chain at claim time.

## 14. Metadata extension point

`metadata: dict` is a free-form key-value extension point on tasks, responses, and
task definitions. The library does not enforce keys. Convention keys are
documented here so dispatchers can share vocabulary, but they are not validated by
the engine.

Canonical convention keys:

- `required_vendor` (`claude` | `codex` | `any`) — vendor routing for dispatchers.
- `model_class` (`opus` | `sonnet` | `haiku` | `gpt-5-3` |
  `gpt-5-3-spark` | `any`) — model class request for the spawned agent.
- `effort` (`low` | `medium` | `high` | `xhigh`) — reasoning effort hint for the
  spawned agent.
- `required_capabilities` — list of capability tags the responder should have.
- `model_used` — model the responder actually used.
- `effort_used` — reasoning effort the responder actually applied.
- `tokens_in` / `tokens_out` — usage telemetry.
- `thread_id` — correlation id for multi-turn threads.
- `parent_task_id` — for thread reconstruction.

Note: `xhigh` and `gpt-5-3-spark` are conventions specific to the bundled
dispatcher's vendor mapping; protocol-wise these are opaque strings. Customize
the dispatcher script if you need different aliases for your CLI.

`model_class` is the preferred replacement for older `model_hint` conventions.
`effort` is the preferred replacement for older `min_effort` conventions.
Dispatchers may accept the older names for compatibility, but new task definitions
should use the canonical keys above.

Older clients ignore unknown keys.

## 15. Lane as capability tier

Lanes encode the subscription boundary: which queue stream a dispatcher polls.
For lane-tier topologies, lanes may also encode the primary routing decision.
Conventional lane names follow the form `<vendor>-<tier>` (e.g.
`claude-reviewer`, `codex-haiku`) but the library imposes no schema.

For shared-pool topologies, many vendors may subscribe to the same broad lane
(commonly `default`) and use task metadata for the routing decision. In that
pattern, `required_vendor` routes, while `model_class`, `effort`, and
`required_capabilities` refine the spawn request.

## 16. Dispatcher pattern

Two modes are supported. Pick based on your environment; neither is universally
better.

**Mode A — live polling chat (chat-as-dispatcher).** A long-running interactive
agent keeps `wait --lane <lane> --json` armed. When a task arrives, the operator
(or the agent itself) claims, reviews, and responds in-context. Best when you
have a chat subscription and want to see what the dispatcher does. Context
accumulates across tasks; restart the chat periodically.

**Mode B — stateless shell dispatcher (bash dispatcher).** A shell loop polls the
lane and pipes each task to a fresh headless-agent invocation. The dispatcher
itself is task-agnostic; each task carries its own prompt. Context does not
accumulate. Best for unattended use or environments without a chat client; each
task incurs API token cost. See `agent_lanes/templates/workspace/dispatcher.sh`.

**Vendor-routed dispatchers (for multi-vendor pools).** Run one
long-running dispatcher per vendor (for example, `VENDOR=claude` and
`VENDOR=codex`), all subscribed to the same lane (typically `default`) on the same
queue. Each dispatcher inspects the task's `required_vendor` metadata. If
`required_vendor != VENDOR` and `required_vendor != any`, it skips without
claiming: a probe-then-skip flow. The matching dispatcher claims, resolves
`model_class` and `effort` to vendor-specific CLI flags, spawns a fresh headless
agent, captures stdout, and responds.

This makes the dispatcher a stateless router and spawner. The task carries its own
routing intent: vendor, model class, and effort. The dispatcher does not have its
own model or effort policy beyond resolving the task metadata to vendor-specific
flags.

The dispatcher concept covers both polling chats that spawn sub-agents and bash
dispatchers that spawn headless CLI agents. Both follow the same vendor-routed,
metadata-driven pattern.

## 17. Shared-queue topology (workspace-level pools)

A workspace-level pool uses one queue for many projects. The shared queue
has its own engine config, such as `~/workspace/.agent-lanes-queue/handoff.yaml`,
and queue state directory, such as `~/workspace/.agent-lanes-queue/state/`. Each
project's `handoff/handoff.yaml` sets `queue_root` to that shared state path.

Use `agent-lanes init-pool <workspace>` to scaffold the shared queue and
dispatcher artifacts:

```bash
agent-lanes init-pool ~/workspace
```

This creates `~/workspace/.agent-lanes-queue/` plus
`~/workspace/_dispatchers/`. The dispatchers folder contains bash wrappers and a
polling chat prompt. The bash dispatcher and the polling chat prompt are
alternative consumers of the same queue and can run at the same time.

Use `agent-lanes init --queue-root <path>` to scaffold a project already pointed
at a shared queue:

```bash
agent-lanes init --queue-root ~/workspace/.agent-lanes-queue/state
```

Dispatchers then poll the shared queue rather than a project-local queue. Multiple
projects pull from the same dispatcher capacity, which is useful for personal
multi-project workflows where one operator wants shared infrastructure across many
working directories. Revisit this topology for team usage, where ownership,
capacity, and isolation usually need a stricter contract.

Lane definitions live on the workspace-level engine config that owns the shared
queue. Project-level configs inherit those lane definitions implicitly by pointing
`queue_root` at the shared queue. Treat lanes as bound to the queue, not to an
individual project.

## 18. Task threading pattern

Stateless dispatchers reconstruct conversational continuity using two metadata
keys:

- `thread_id` — stable id across a multi-turn conversation.
- `parent_task_id` — id of the previous task in the thread.

At claim time, the dispatcher walks the parent chain: fetch the parent task's
response, then its parent, then its parent. The walk produces a flattened transcript
that becomes the new task's input. This is dispatcher logic; the library keeps no
thread state. Thread integrity (cycles, tampering, missing parents) is the
dispatcher's responsibility.

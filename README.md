# agent-lanes

[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/leo-diehl/agent-lanes/actions/workflows/test.yml/badge.svg)](https://github.com/leo-diehl/agent-lanes/actions/workflows/test.yml)

A local, file-backed, structured-RPC queue for AI coding agents. agent-lanes lets one
agent submit a task and another claim, work, and respond — over a shared filesystem
queue, no daemon required. Cross-vendor by design: any agent that can run shell
commands can participate.

## Install

```bash
# From PyPI (once published)
pip install agent-lanes

# From GitHub
pip install git+https://github.com/leo-diehl/agent-lanes.git

# From source (for development)
git clone https://github.com/leo-diehl/agent-lanes.git
cd agent-lanes
pip install -e .
```

Requires Python 3.11+ on macOS or Linux. POSIX-only — Windows users should run via
WSL2.

## Quickstart

Set up a workspace pool, attach a long-running dispatcher chat, submit a task. Five
commands.

```bash
# 1. Scaffold a workspace pool (shared queue + dispatcher artifacts)
agent-lanes init-pool ~/myworkspace
```

Creates `~/myworkspace/.agent-lanes-queue/` (the shared queue) and
`~/myworkspace/_dispatchers/` (per-vendor dispatcher wrappers and a polling chat
prompt).

```bash
# 2. Scaffold a rack pointed at the pool
mkdir ~/myworkspace/example-rack && cd ~/myworkspace/example-rack
agent-lanes init --queue-root ~/myworkspace/.agent-lanes-queue/state
mkdir tasks
```

3. **Attach a dispatcher.** Open a Claude Code or Codex chat, paste
   `~/myworkspace/_dispatchers/POLLING-CHAT-PROMPT.md` into it, fill in the vendor
   identity (one line at the top), and send. The chat is now a long-running
   dispatcher — it polls the shared queue, claims tasks whose `required_vendor`
   matches its vendor, spawns a sub-agent at the requested model and effort,
   captures the result, and responds. Idle costs zero tokens (it's a blocking
   syscall).

```yaml
# 4. Define a task: tasks/code-review.yaml
lane: default
metadata:
  required_vendor: claude
  model_class: sonnet
  effort: high
prompt: |
  Review the request file. Return blocking issues, non-blocking suggestions,
  and a one-line verdict.
```

```bash
# 5. Submit and wait
TASK_ID=$(./handoff/bin/handoff submit \
  --task tasks/code-review.yaml \
  --request-from outputs/01-step.md \
  --response-to outputs/01-review.md \
  --json | jq -r .task_id)
./handoff/bin/handoff wait "$TASK_ID"
```

The polling chat picks up the task, the sub-agent does the review, the response
lands in `outputs/01-review.md`, and the wait unblocks.

**Mode A (chat-as-dispatcher) is the recommended consumer** because it uses your
chat subscription rather than per-token API billing. The polling chat is
vendor-bound only; model and effort come from each task's metadata, so the same
chat handles tasks at any model/effort the queue receives.

### Alternative: Mode B (bash dispatcher)

For headless CLI environments where per-token API billing is acceptable, run the
bundled bash wrappers in any terminal:

```bash
bash ~/myworkspace/_dispatchers/claude.sh   # one terminal
bash ~/myworkspace/_dispatchers/codex.sh    # another terminal
```

Each wrapper long-polls the queue, claims tasks for its vendor, and spawns a fresh
headless agent (`claude -p` / `codex exec`) per task. Both modes consume the same
queue and can run simultaneously.

## Try it

The `examples/two-terminal/` directory has a 30-second demo: one shell submits a
task, another claims and responds. No LLM is involved — pure shell — but it
exercises the full submit / wait / claim / respond cycle.

```bash
cd examples/two-terminal
bash agent-b.sh &
bash agent-a.sh
wait
```

## Architecture

The engine (the `handoff/` folder) is project-level infrastructure: workspace
metadata, lane definitions, the dispatcher script, the CLI wrapper, runtime state.
It does **not** contain task definitions. Tasks are separate YAML files that you
keep wherever your project organizes them (commonly a `tasks/` folder). The engine
routes tasks by lane; tasks reference lanes by name.

Two scaffolding shapes:

- **`agent-lanes init`** scaffolds a single rack with its own per-rack queue at
  `handoff/state/`. Use this for one-off projects.
- **`agent-lanes init-pool <workspace>`** scaffolds a workspace-level shared queue
  plus per-vendor dispatcher artifacts. Use this when one workspace has many racks
  that should share a single dispatcher pool. Each rack then runs
  `agent-lanes init --queue-root <abs-path-to-shared-state>` to point at the
  shared queue instead of creating its own.

Routing is metadata-driven. Every dispatcher (Mode A or Mode B) is bound only to
a vendor (`claude` or `codex`). Each task carries metadata declaring
`required_vendor`, `model_class`, and `effort`. Dispatchers inspect metadata, skip
tasks not for them, claim matching ones, and spawn (or delegate to a sub-agent)
the actual work at the requested model and effort. The dispatcher is a router;
the work happens in the spawned agent.

## Define your first task

A task is a small standalone YAML file. It declares which lane to route to, any
default metadata, and the prompt body (inline or via `prompt_file`).

```yaml
# tasks/code-review.yaml
lane: default
metadata:
  required_vendor: claude
  model_class: sonnet
  effort: high
prompt_file: ../docs/prompts/code-review.md
```

```markdown
<!-- docs/prompts/code-review.md -->
You are a code reviewer. Read the request file as the artifact under review.
Respond with blocking issues, non-blocking suggestions, and a one-line verdict.
```

Per-execution paths (the actual request and response files) come from CLI flags at
submit time, not from the task file. The same task definition can be reused across
many submissions with different request/response paths.

You can also submit fully inline without a task file:

```bash
./handoff/bin/handoff submit \
  --lane default \
  --request-from outputs/01-step.md \
  --response-to outputs/01-review.md \
  --prompt "Review for missing evidence." \
  --metadata required_vendor=claude \
  --metadata model_class=sonnet \
  --metadata effort=high \
  --json
```

## Common patterns

agent-lanes is structured RPC; review is one application. Five common shapes:

**Review / checkpoint.** An orchestrator submits an artifact and waits for a
verdict (`accept`, `accept-with-follow-ups`, `needs-revision`). The reviewer
claims, reads the request, and responds with `--verdict`. The original use case.

**Q&A.** A primary agent has a question for a specialist. Submit with no verdict
expectation, wait for the response, integrate the answer.

**Delegation.** A parent task fans out to N children on different lanes (or the
same lane), each handling one subtask. The parent submits all children, then waits
on each. Useful for parallel reviewers or any embarrassingly-parallel work.

**Pipeline.** Each agent's response feeds the next one's request. Threading
metadata (`thread_id`, `parent_task_id`) lets dispatchers reconstruct
conversational continuity across iterations.

**Vendor-routed pool.** One workspace, many racks, one queue. Mode A and Mode B
dispatchers are interchangeable consumers; both subscribe to the queue's `default`
lane and route per task metadata.

## Orchestrator-side usage

Paste this into the CLI agent that drives the queue:

```text
You have access to an agent-lanes engine at ./handoff/. Use it to coordinate with
other agents.

To submit a task:

  ./handoff/bin/handoff submit \
    --task tasks/<id>.yaml \
    --request-from <path-to-artifact> \
    --response-to <path-where-response-should-be-written> \
    [--metadata key=value ...] \
    --json

Capture the returned task_id. To wait for the response:

  ./handoff/bin/handoff wait <task-id> --json

For multi-turn iterations, pass thread metadata so a stateless dispatcher can walk
the parent chain:

  --metadata thread_id=<thread> --metadata parent_task_id=<previous-task-id>

To inspect without claiming:

  ./handoff/bin/handoff list --lane <lane> --json
  ./handoff/bin/handoff status <task-id> --json
  ./handoff/bin/handoff status --rack --json

Apply every reviewer suggestion that makes sense in scope, including non-blocking
ones. Skip a suggestion only with a concrete reason.
```

## Reference

- [`CONTRACT.md`](CONTRACT.md) — protocol contract: state machine, JSON shapes,
  CLI surface, HTTP routes, metadata convention (§ 14), dispatcher pattern (§ 16),
  shared-queue topology (§ 17).
- [`CHANGELOG.md`](CHANGELOG.md) — release notes.

## Language neutrality

The reference implementation is Python (stdlib + PyYAML). The protocol itself is
language-neutral: state lives on disk as JSON, commands are exposed via a CLI and
a small HTTP server. TypeScript, Go, and Rust ports are welcome as separate
packages once the protocol stabilizes.

## Positioning

agent-lanes is local-first and protocol-light. Compared to alternatives:

- **MCP** is for tool exposure (one agent calling tools). agent-lanes is for agent
  coordination (one agent calling another agent).
- **A2A** targets remote agent-to-agent calls over networks with auth and
  discovery. agent-lanes is the local equivalent: same shape, no network.
- **agentpost** and similar frameworks bundle orchestration. agent-lanes ships
  only the queue primitives — your orchestrator stays a few shell commands away.

## Status

v0.1 — early. APIs may change before v1.0.

## License

MIT. See [LICENSE](LICENSE).

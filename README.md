# agent-lanes

A local, file-backed, structured-RPC queue for AI coding agents. agent-lanes lets one
agent submit a task and another claim, work, and respond — over a shared filesystem
queue, no daemon required. Cross-vendor by design: any agent that can run shell
commands can participate.

## Architecture

The engine (the `handoff/` folder) is project-level infrastructure. It contains
workspace metadata, lane definitions, the dispatcher script, the CLI wrapper, and
runtime state. It does **not** contain task definitions. Tasks are separate YAML files
that you keep wherever your project organizes them (commonly a `tasks/` folder). The
engine routes tasks by lane; tasks reference lanes by name. The orchestrator submits
either by pointing at a task file (`--task path/to/task.yaml`) or by specifying every
field inline.

## Install

```bash
pip install -e .
```

This installs the `agent-lanes` console script and makes `agent_lanes` importable.

## Quickstart

End-to-end in four steps. Run from the project root.

1. Scaffold the engine:

   ```bash
   agent-lanes init
   ```

   This creates `handoff/` with engine config, dispatcher script, CLI wrapper, and
   prompt templates. It does not create a `tasks/` folder; that is yours to organize.

2. Define your first task (see "Define your first task" below).

3. Start a dispatcher in one terminal:

   ```bash
   bash handoff/dispatcher.sh
   ```

   The dispatcher long-polls the lane and pipes each arriving task to a fresh
   headless-agent invocation. While idle it consumes zero tokens.

4. From an orchestrator chat (or any shell), submit a task:

   ```bash
   ./handoff/bin/handoff submit \
     --task tasks/code-review.yaml \
     --request-from outputs/01-step-output.md \
     --response-to outputs/01-step-review.md \
     --json
   ```

   Then wait for the response:

   ```bash
   ./handoff/bin/handoff wait <task-id> --json
   ```

## Quickstart for multi-rack pools

Use a shared queue when one workspace has many racks and you want one dispatcher
pool to serve all of them.

```bash
# One-time workspace setup
mkdir -p ~/workspace/.agent-lanes-queue/state
cat > ~/workspace/.agent-lanes-queue/handoff.yaml <<'YAML'
workspace_id: my-workspace
workspace_root: ..
queue_root: state
lanes:
  default:
    description: Shared lane; routing via task metadata
YAML

# Per-rack setup
cd ~/workspace/<rack>
agent-lanes init --queue-root ~/workspace/.agent-lanes-queue/state
mkdir tasks
# write tasks/<id>.yaml with metadata.required_vendor / model_class / effort

# Run dispatchers (one per vendor, from a directory containing dispatcher.sh)
VENDOR=claude QUEUE_ROOT=~/workspace/.agent-lanes-queue/state bash dispatcher.sh
VENDOR=codex  QUEUE_ROOT=~/workspace/.agent-lanes-queue/state bash dispatcher.sh

# Submit from any rack's orchestrator
./handoff/bin/handoff submit --task tasks/<id>.yaml --json
```

In a scaffolded rack, the dispatcher script lives at `handoff/dispatcher.sh`; the
source template lives at `agent_lanes/templates/workspace/dispatcher.sh`. The
canonical metadata vocabulary is in `CONTRACT.md` section 14. Vendor-routed
dispatcher behavior is in section 16, and the shared-queue topology is in section
17.

## Define your first task

A task is a small standalone YAML file. It declares which lane to route to, any
default metadata, and the prompt body (inline or via a file).

```yaml
# tasks/code-review.yaml
lane: claude-reviewer
metadata:
  effort: high
prompt_file: ../docs/prompts/code-review.md
```

```markdown
<!-- docs/prompts/code-review.md -->
You are a code reviewer. Read the request file as the artifact under review.
Respond with:
- blocking issues (must fix before accept)
- non-blocking suggestions (apply if reasonable)
- a one-line verdict
```

Per-execution paths (the actual request and response files) come from CLI flags at
submit time, not from the task file. The same task definition can be reused across
many submissions with different request/response paths.

You can also submit fully inline without a task file:

```bash
./handoff/bin/handoff submit \
  --lane claude-reviewer \
  --request-from outputs/01-step-output.md \
  --response-to outputs/01-step-review.md \
  --prompt "Review for missing evidence." \
  --metadata effort=high \
  --json
```

## Common patterns

agent-lanes is structured RPC; review is one application. Five common shapes:

**Review / checkpoint.** An orchestrator submits an artifact and waits for a verdict
(`accept`, `accept-with-follow-ups`, `needs-revision`). The reviewer claims, reads
the request, and responds with `--verdict`. This is the original use case.

**Q&A.** A primary agent has a question for a specialist (e.g. a domain expert
agent). Submit with no verdict expectation, wait for the response, integrate the
answer. The specialist's lane carries the routing decision.

**Delegation.** A parent task fans out to N children on different lanes (or the same
lane), each handling one subtask. The parent submits all children, then waits on each
in sequence (or in any order). Useful for parallel reviewers, parallel summarization,
or any embarrassingly-parallel work.

**Pipeline.** Each agent's response feeds the next one's request. Stage 1 outputs
go into stage 2's request file; stage 2 outputs into stage 3's. Threading metadata
(`thread_id`, `parent_task_id`) lets dispatchers reconstruct conversational
continuity across iterations.

**Vendor-routed pool.** One project, many racks, one queue. A pool of long-running
dispatchers, one per vendor on this machine, subscribes to the queue's default
lane. Each task carries metadata declaring which vendor must handle it
(`required_vendor`), which model class to spawn (`model_class`), and what reasoning
effort to request (`effort`). See `CONTRACT.md` section 14 for the canonical
metadata values.

Dispatchers inspect metadata, skip if they are not a match, claim if matching, and
spawn a fresh headless agent with the resolved flags. The orchestrator is the only
chat the operator opens per rack; dispatchers are infrastructure.

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

## Language neutrality

The reference implementation is Python (stdlib only; PyYAML optional). The protocol
itself is language-neutral: state lives on disk as JSON, commands are exposed via a
CLI and a small HTTP server. TypeScript, Go, and Rust ports are welcome as separate
packages once the protocol stabilizes.

## Positioning

agent-lanes is local-first and protocol-light. Compared to alternatives:

- **MCP** is for tool exposure (one agent calling tools). agent-lanes is for agent
  coordination (one agent calling another agent).
- **A2A** targets remote agent-to-agent calls over networks with auth and discovery.
  agent-lanes is the local equivalent: same shape, no network.
- **agentpost** and similar frameworks bundle orchestration. agent-lanes ships only
  the queue primitives — your orchestrator stays a few shell commands away.

## Status

`v0.1` — internal use. Repo private until the wider open-source flip.

## License

MIT. See [LICENSE](LICENSE).

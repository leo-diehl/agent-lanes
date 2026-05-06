# handoff/ — agent-lanes engine

This folder is the agent-lanes engine for this workspace. It contains workspace
metadata, lane definitions, the dispatcher script, the CLI wrapper, and runtime
state. **It contains no task definitions.**

## Files

- `handoff.yaml` — engine config: workspace metadata, lanes, queue location.
  Engine-only; no `checkpoints:` field.
- `dispatcher.sh` — Mode B dispatcher (stateless shell loop that pipes each
  task to a fresh headless-agent invocation).
- `bin/handoff` — CLI wrapper that routes to the agent-lanes runtime.
- `state/` — transient queue state. Keep local; do not commit task content.
- `POLLING-MONITOR-PROMPT.md` — Mode A prompt for a lightweight polling agent.
- `REVIEWER-AGENT-PROMPT.md` — Mode A prompt for a reviewer agent.

Durable response outputs are written by the waiting orchestrator to whatever
`response_to` path the submission specified. Convention is the workspace's
`outputs/` folder.

## How to start a dispatcher

```bash
bash handoff/dispatcher.sh
```

Customize `LANE`, `OWNER`, and `HEADLESS_AGENT_CMD` in `dispatcher.sh` before
running, or set them via environment variables.

## How to submit and wait

```bash
./handoff/bin/handoff submit \
  --task tasks/<your-task>.yaml \
  --request-from <path-to-artifact> \
  --response-to <path-to-output> \
  --json
```

Then wait for the response (capture the `task_id` from the submit output):

```bash
./handoff/bin/handoff wait <task-id> --json
```

## Where do task definitions live?

Anywhere outside `handoff/`. A common convention is a `tasks/` folder at the
project root, with one YAML file per reusable task pattern. See the top-level
agent-lanes README's "Define your first task" section for the exact shape and a
worked example.

The engine never enumerates task files. It is the orchestrator's job to point at
a specific task file with `--task <path>`, or to submit fully inline with
`--lane`/`--prompt`/etc.

## Orchestrator-side prompt content

Lives in the **top-level agent-lanes README**, not here. This folder is
project-level engine infrastructure; it should be small and stable.

## Roles

| Role | Commands |
| --- | --- |
| Orchestrator | `submit --task ...` (or inline), then `wait <task-id>` |
| Polling monitor | keep `wait --lane <lane> --json` armed; report task JSON; never claim |
| Reviewer | `claim <task-id>`, then `respond <task-id>` |

`wait --lane`, `watch`, and `next` long-poll by default for six hours.
While waiting, they consume zero tokens. If the command times out, run it again.

## Diagnostics

```bash
./handoff/bin/handoff list --lane <lane> --json
./handoff/bin/handoff status <task-id> --json
./handoff/bin/handoff status --all --json
```

`status --all --json` is the first diagnostic command. It summarizes queued,
claimed, completed, failed, missing-response, and stale-lease tasks for the
project and includes next-action guidance.

## Runtime lookup

The wrapper uses `python3` when available and falls back to `python`.

If `AGENT_LANES_RUNTIME` is set, the wrapper uses that runtime. Otherwise it
tries `python -m agent_lanes`. Set `AGENT_LANES_RUNTIME=/absolute/path/to/agent-lanes`
if the package is not importable from the default Python.

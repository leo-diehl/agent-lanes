# Workspace Handoff

This folder is the agent-lanes engine for this workspace. It contains workspace
metadata, lane definitions, the CLI wrapper, and runtime state. It does not contain
task definitions; those live elsewhere in the project (commonly under `tasks/`).

## Commands

Submit a checkpoint request (legacy positional form; see
`./handoff/bin/handoff submit --help` for the `--task <path>` form):

```bash
./handoff/bin/handoff submit phase-01-review
```

Wait for the response and write the configured response output file:

```bash
./handoff/bin/handoff wait phase-01-review
```

Inspect status:

```bash
./handoff/bin/handoff status phase-01-review
```

Lane monitor and reviewer flow:

```bash
./handoff/bin/handoff wait --lane claude-review --json
./handoff/bin/handoff claim <task-id> --owner reviewer --json
./handoff/bin/handoff respond <task-id> --claim-token <token> --file response.md --verdict accept --json
```

## Roles

| Role | Commands |
| --- | --- |
| Orchestrator | `submit <id>`, then `wait <task-id>` |
| Polling monitor | keep `wait --lane <lane> --json` armed; report task JSON; never claim |
| Reviewer | `claim <task-id>`, then `respond <task-id>` |

`wait --lane`, `watch`, and `next` long-poll by default for six hours and print
keepalive messages to stderr while waiting. Quiet periods are normal: they mean the
reviewer is waiting for the orchestrator's next submit. If the command times out,
run it again instead of assuming the queue is broken.

A lightweight/background polling agent can own lane polling, reporting task JSON
back to the main reviewer chat. The reviewer claims and reviews only after a task
is available. This keeps keepalive output out of the review context.

Some agent shells have a shorter foreground timeout than the six-hour wait. Use the
shell's background/monitor mechanism for lane polling. If the monitor can safely
stay quiet until the final JSON, add `--quiet` to suppress keepalive output.

To inspect without claiming:

```bash
./handoff/bin/handoff list --lane claude-review --json
./handoff/bin/handoff status --rack --json
```

## Two agents using the same handoff

Both chats use this same workspace folder.

Orchestrator side:

```bash
cd <this-workspace>
./handoff/bin/handoff submit phase-01-review
./handoff/bin/handoff wait phase-01-review
```

Reviewer side:

```bash
cd <this-workspace>
./handoff/bin/handoff wait --lane claude-review --json
./handoff/bin/handoff claim <task-id> --owner reviewer --json
./handoff/bin/handoff respond <task-id> --claim-token <token> --file /path/to/review.md --verdict accept --expect-sha256 <request_sha256> --json
```

The shared connection is the local filesystem state under `handoff/handoff.yaml`
and `handoff/state/`.

After submitting a task, responding, or sending an operator message, do not stop.
Schedule the next relevant wait after a short optimistic delay based on how long
the other side is likely to take. The delay is only to avoid noisy immediate
polling, not to turn the handoff into a one-shot.

For the orchestrator, non-blocking feedback is still actionable feedback. Apply
every reviewer suggestion that makes sense within the current scope. Skip or defer
a suggestion only with a concrete reason.

Use `status --rack --json` as the first diagnostic command. It summarizes queued,
claimed, completed, failed, missing-response, and stale-lease tasks and includes
next-action guidance.

The optional HTTP server is not required for this side-by-side flow; the wrapper
commands operate directly on the file-backed store.

## Files

- `handoff.yaml` — engine config: workspace metadata, lanes, queue location.
- `bin/handoff` — CLI wrapper that routes to the agent-lanes runtime.
- `state/` — transient queue state. Keep local; do not commit task content.
- `POLLING-MONITOR-PROMPT.md` — prompt for a lightweight polling agent.
- `REVIEWER-AGENT-PROMPT.md` — prompt for a reviewer agent.

Durable response outputs are written by the waiting orchestrator to the configured
`response_to` path. Convention is the workspace's `outputs/` folder.

## Runtime lookup

The wrapper uses `python3` when available and falls back to `python`.

If `AGENT_LANES_RUNTIME` is set, the wrapper uses that runtime. Otherwise it tries
`python -m agent_lanes`.

Set `AGENT_LANES_RUNTIME=/absolute/path/to/agent-lanes` if the package is not
importable from the default Python.

## Response files

Use task-specific temp files such as `/tmp/agent-lanes-review-<task-id>.md`, or
submit from stdin with `respond --file -`. Do not reuse a shared global path for
parallel or repeated tasks.

CLI verdict tokens are exactly:

- `accept`
- `accept-with-follow-ups`
- `needs-revision`

Use `--expect-sha256 <request_sha256>` when responding so the reviewed request
revision is explicit.

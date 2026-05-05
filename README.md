# Agent Handoff

Local, file-backed handoff runtime for Myelin prompt packs.

Use this when a prompt pack needs one agent to submit a review checkpoint and another agent or
worker to answer it before the pack continues.

## Runtime And Pack Aspect

Shared runtime:

```text
product/_operational/agent-handoff/
```

Pack-local aspect copied into a prompt pack:

```text
<prompt-pack>/handoff/
  README.md
  CLAUDE-REVIEWER-PROMPT.md
  POLLING-MONITOR-PROMPT.md
  handoff.yaml
  bin/handoff
  state/
```

Durable review outputs should live in the pack's `outputs/` folder. Transient queue state lives in
`handoff/state/`.

## Common Commands

From a prompt pack with a copied `handoff/` folder:

```bash
./handoff/bin/handoff submit phase-01-review
./handoff/bin/handoff wait phase-01-review
./handoff/bin/handoff status phase-01-review
```

For a lane monitor and reviewer:

```bash
./handoff/bin/handoff wait --lane claude-review --json
./handoff/bin/handoff claim <task-id> --owner claude-worker --json
./handoff/bin/handoff respond <task-id> --claim-token <token> --file response.md --verdict accept --json
```

## Who Runs What

| Role | Commands |
| --- | --- |
| Codex orchestrator | `submit <checkpoint-id>`, then `wait <checkpoint-id>` |
| Polling monitor | keep `wait --lane <lane> --json` armed; report task JSON; never claim |
| Claude reviewer | `claim <task-id>`, then `respond <task-id>` |

Checkpoint ids and lanes are different identifiers. Orchestrators use checkpoint ids from
`handoff.yaml`. Monitors use lane names such as `claude-review`.

`wait --lane`, `watch`, and `next` long-poll by default for six hours and print keepalive messages
to stderr while waiting. Quiet periods are normal: they mean the reviewer is waiting for the
orchestrator's next checkpoint. If the command times out, run it again instead of assuming the queue
is broken.

When a lightweight/background agent is available, use it for the lane wait instead of the main
reviewer chat. A low-cost/basic, Haiku-class model is enough. The polling monitor runs only
`wait --lane <lane> --json`, reports returned task JSON, and keeps the lane armed until the operator
tells it to stop. The main reviewer chat then claims and reviews each task. This keeps keepalive and
idle messages out of the review context and avoids starting a lease before the reviewer is ready.

Some agent shells have a shorter foreground timeout than the six-hour handoff wait. In that case,
run the lane wait through the shell's background or monitor mechanism. If the monitor can safely
stay quiet until final JSON, add `--quiet` to suppress keepalive output:

```bash
./handoff/bin/handoff wait --lane claude-review --json --quiet
```

To inspect without claiming:

```bash
./handoff/bin/handoff list --lane claude-review --json
./handoff/bin/handoff status --rack --json
```

Use `status --rack --json` as the first diagnostic command. It summarizes queued, claimed,
completed, failed, missing-response, and stale-lease tasks and includes next-action guidance.

If a review is taking longer than expected:

```bash
./handoff/bin/handoff renew <task-id> --claim-token <token>
```

Run the local server:

```bash
python3 -m agent_handoff serve --config handoff/handoff.yaml
```

## Two Agents In Side-By-Side Chats

In v1, the normal workflow does not require a long-running server. Both chats use the same
pack-local `handoff/handoff.yaml` and the same `handoff/state/` directory on disk.

Agent A, for example Codex, works in the prompt pack folder and submits the checkpoint:

```bash
cd <prompt-pack>
./handoff/bin/handoff submit phase-01-review
./handoff/bin/handoff wait phase-01-review
```

The `wait` command blocks until a response exists, then writes the configured `response_to` file
from `handoff.yaml`, usually under `outputs/`.

After submitting a checkpoint or sending an operator message, do not stop. Schedule the next
`wait <checkpoint-id>` after a short optimistic delay based on how long the other side is likely to
take. For a quick review, that may be seconds; for a deeper review, use a longer delay. The delay is
only to avoid noisy immediate polling, not to turn the handoff into a one-shot.

Preferred Agent B setup has two roles:

- a lightweight polling monitor that keeps the lane wait armed and reports task JSON
- the main Claude reviewer that claims and reviews only after a task is available

The polling monitor works from the same prompt pack folder or any shell that can see the same files:

```bash
cd <prompt-pack>
./handoff/bin/handoff wait --lane claude-review --json
```

When the monitor returns task JSON, send it to the main Claude reviewer. The reviewer then runs:

```bash
cd <prompt-pack>
./handoff/bin/handoff claim <task-id> --owner claude-chat --json
./handoff/bin/handoff respond <task-id> --claim-token <token> --file /path/to/review.md --verdict accept --expect-sha256 <request_sha256> --json
```

If there is no separate lightweight/background agent, the main Claude reviewer may run
`wait --lane` directly as a fallback.

After a reviewer responds or a monitor reports task JSON, do not let the lane go cold. Schedule the
next `wait --lane claude-review --json` after a short optimistic delay based on how long the other
side will likely take to claim, respond, or submit the next checkpoint. Keep doing that until the
operator says to stop.

Both agents are "hooked up" by pointing at the same config and state folder:

```text
<prompt-pack>/handoff/handoff.yaml
<prompt-pack>/handoff/state/
```

If the prompt pack is outside the private repo, both chats should use the same runtime path:

```bash
export AGENT_HANDOFF_RUNTIME=/absolute/path/to/product/_operational/agent-handoff
```

The local HTTP server is available for future workers or curl-based integrations, but the current
CLI commands operate directly on the file-backed store. Starting `serve` does not automatically make
the CLI use HTTP.

For HTTP workers, use long-poll parameters instead of tight polling:

```text
GET /tasks/next?lane=claude-review&wait_seconds=21600
GET /tasks/<task-id>/response?wait_seconds=21600
```

If the endpoint returns without a task after the timeout, the worker should call it again.

## Response Metadata

`respond --file` copies the file contents into the task's `response.json`. It does not move the
file. Use a task-specific temp path such as `/tmp/claude-handoff-review-<task-id>.md`, or use
`--file -` to read the response body from stdin. The waiting orchestrator later writes that response
body to the checkpoint's configured `response_to` path.

Use structured metadata when possible:

```bash
./handoff/bin/handoff respond <task-id> \
  --claim-token <token> \
  --reviewer claude-chat \
  --file /tmp/claude-handoff-review-<task-id>.md \
  --verdict needs-revision \
  --blocking-count 2 \
  --nonblocking-count 1 \
  --expect-sha256 <request_sha256> \
  --json
```

Allowed verdicts:

- `accept`
- `accept-with-follow-ups`
- `needs-revision`

`accept-with-follow-ups` and non-blocking feedback still require active handling by the
orchestrator. Apply every suggestion that makes sense within scope. Defer or skip a suggestion only
with a concrete reason recorded in the next output or final response.

`respond --json` prints structured confirmation including task id, verdict, response path, queue
depth, and next-action guidance.

`claim` verifies the request file still matches the task's `request_sha256`. If the file changed
after submission, claim fails so the reviewer does not review stale input. `respond` verifies the
request hash again before accepting a completed response. Use `--expect-sha256 <request_sha256>` to
pin the revision the reviewer actually reviewed.

Checkpoints may define `supporting_paths` for meta-reviews or multi-artifact reviews:

```yaml
supporting_paths:
  - ../outputs/00-context.md
  - ../outputs/01-previous-step.md
```

Tasks store each supporting path with its SHA-256 so reviewers can audit secondary context as well
as the primary `request_path`.

## What A Scaffolding Agent Should Give The Operator

When an agent creates a new prompt pack that uses handoff, it should do two things:

1. Copy the pack-local template into `<prompt-pack>/handoff/` and edit `handoff/handoff.yaml`.
2. Give the operator the exact prompts to paste into the polling monitor and Claude-side reviewer
   chats.

The copied template includes:

```text
handoff/CLAUDE-REVIEWER-PROMPT.md
handoff/POLLING-MONITOR-PROMPT.md
```

The scaffolding agent should fill in the absolute prompt-pack path and lane, then include the filled
prompts in its final response. If the operator has a cheap/basic background agent available, use
`POLLING-MONITOR-PROMPT.md` for that agent and reserve `CLAUDE-REVIEWER-PROMPT.md` for the main
review chat. This removes ambiguity about how the other chat connects to the same queue without
clogging the review context with polling output.

## Copying The Template

From this runtime directory:

```bash
cp -R agent_handoff/templates/pack-handoff <prompt-pack>/handoff
chmod +x <prompt-pack>/handoff/bin/handoff
```

Then edit `<prompt-pack>/handoff/handoff.yaml` and define checkpoint ids for the pack.
Always replace the template `pack_id`, checkpoint ids, paths, prompts, and any worktree metadata.
When creating a pack from an existing pack rather than the clean template, remove copied transient
state under `handoff/state/` except `.gitignore` and `.gitkeep`.

If the prompt pack lives outside `myelin-private/product`, set `AGENT_HANDOFF_RUNTIME` to this
runtime directory before using the wrapper.

## Tests

From this directory:

```bash
python3 -m pytest
python3 -m agent_handoff --help
python3 -m agent_handoff self-test
git diff --check
```

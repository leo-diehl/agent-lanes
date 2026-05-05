# Pack Handoff

This folder is the local handoff mechanism for this prompt pack.

Use it when a prompt step says to request review or wait for another agent before continuing.

## Commands

Submit a checkpoint request:

```bash
./handoff/bin/handoff submit phase-01-review
```

Wait for the response and write the configured review output file:

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
to stderr while waiting. Quiet periods are expected: they mean the reviewer is waiting for the
orchestrator's next checkpoint. If the command times out, run it again.

Prefer running the lane wait in a lightweight/background polling agent when one is available. The
polling agent should only run `wait --lane <lane> --json`, report returned task JSON, and keep the
lane armed until the operator says to stop. The main reviewer chat should claim and review the task
after receiving that JSON. This keeps
keepalive output out of the main review context.

Some agent shells have a shorter foreground timeout than the six-hour handoff wait. Use the shell's
background/monitor mechanism for lane polling. If that monitor can safely stay quiet until final
JSON, add `--quiet` to suppress keepalive output.

To inspect without claiming:

```bash
./handoff/bin/handoff list --lane claude-review --json
./handoff/bin/handoff status --rack --json
```

## Two Chats Using The Same Handoff

Both chats should use this same prompt-pack folder.

Codex-side flow:

```bash
cd <this-prompt-pack>
./handoff/bin/handoff submit phase-01-review
./handoff/bin/handoff wait phase-01-review
```

Preferred Claude-side flow:

```bash
cd <this-prompt-pack>
./handoff/bin/handoff wait --lane claude-review --json
```

Run that lane wait in a lightweight polling monitor if possible. When task JSON is returned, pass it
to the main reviewer chat. The main reviewer then runs:

```bash
cd <this-prompt-pack>
./handoff/bin/handoff claim <task-id> --owner claude-chat --json
./handoff/bin/handoff respond <task-id> --claim-token <token> --file /path/to/review.md --verdict accept --expect-sha256 <request_sha256> --json
```

If no separate monitor is available, the main reviewer may run `wait --lane` directly as a fallback.

The shared connection is the local filesystem state:

```text
handoff/handoff.yaml
handoff/state/
```

The waiting agent consumes the response by running `wait`; that writes the configured
`response_to` file, usually in `outputs/`.

After submitting a checkpoint, responding to a task, or sending an operator message, do not stop.
Schedule the next relevant wait after a short optimistic delay based on how long the other side is
likely to take. For a quick review, that may be seconds; for deeper work, use a longer delay. The
delay is only to avoid noisy immediate polling, not to turn the handoff into a one-shot.

For the orchestrator, non-blocking feedback is still actionable feedback. Apply every reviewer
suggestion that makes sense within the current scope, including non-blocking suggestions. Skip or
defer a suggestion only with a concrete reason.

Use `status --rack --json` as the first diagnostic command. It summarizes queued, claimed,
completed, failed, missing-response, and stale-lease tasks and includes next-action guidance.

The optional HTTP server is not required for this side-by-side chat workflow. The wrapper commands
read and write the file-backed store directly.

## Files

- `handoff.yaml` defines checkpoints for this pack, including optional `supporting_paths` that pin
  secondary artifacts with SHA-256 hashes.
- `CLAUDE-REVIEWER-PROMPT.md` is the prompt the orchestrating agent should fill and give to the
  operator for the Claude-side chat.
- `POLLING-MONITOR-PROMPT.md` is the prompt for a cheap/basic background agent that waits for task
  JSON without claiming or reviewing.
- `bin/handoff` runs the shared handoff runtime with this pack's config.
- `state/` contains transient queue state. It may include private prompts, diffs, and intermediate
  responses. Keep it local unless a rack explicitly says otherwise.
- Durable review outputs should go in the pack's `outputs/` folder, not in `state/`.

## Runtime Lookup

The wrapper uses `python3` when available and falls back to `python`.

If `AGENT_HANDOFF_RUNTIME` is set, the wrapper uses that runtime. Otherwise it tries
`python -m agent_handoff`; if that is not importable, it searches nearby ancestors for
`product/_operational/agent-handoff` or `_operational/agent-handoff`.

Set `AGENT_HANDOFF_RUNTIME=/absolute/path/to/product/_operational/agent-handoff` if the runtime
lives somewhere else.

## Response Files

Use task-specific temp files such as `/tmp/claude-handoff-review-<task-id>.md`, or submit from stdin
with `respond --file -`. Do not reuse a shared global path for parallel or repeated review tasks.

CLI verdict tokens are exactly:

- `accept`
- `accept-with-follow-ups`
- `needs-revision`

Use `--expect-sha256 <request_sha256>` when responding so the reviewed request revision is explicit.

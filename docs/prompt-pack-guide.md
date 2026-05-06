# Opinionated Prompt-Pack Format

This is an opinionated convention for organizing multi-prompt "packs" that use agent-lanes for cross-agent review and delegation. **It is not part of the protocol.** agent-lanes only cares about lanes, tasks, and metadata. This
 guide layers on top: a folder shape, prompt roles, naming conventions, and a review discipline. Adopt freely; adapt as needed.

## What is a prompt pack?

A prompt pack is a folder describing one line of agent work. It makes the target branch, worktree, prompts, effort levels, and review flow explicit so agents do not need the same execution model re-explained every time. The
pack is the operating contract for that workstream.

A typical pack contains:

- one target branch and one target worktree (for implementation work)
- one executor prompt (the agent doing the work)
- one reviewer prompt (the agent reviewing)
- optionally a planner, a synthesis, or a final-documentation prompt
- a `handoff/` folder scaffolded by `agent-lanes init`
- a `tasks/` folder of agent-lanes task YAML files

## When to use one

Use a pack by default for:

- non-trivial implementation
- parallel-lane work
- synthesis over multiple sub-tasks
- promotion or normalization work
- any task where executor and reviewer should share the same write target

## Folder shape

/
  README.md                                # purpose, scope, target branch/worktree
  START-HERE-orchestrator-prompt.md        # what the orchestrator agent runs
  00-implementation-contract-prompt.md     # contract / plan
  01-executor-prompt.md                    # the actual implementation work
  02-results-reviewer-prompt.md            # in-pack reviewer (optional)
  03-final-handoff-prompt.md               # synthesis / final write-up
  handoff/                                 # agent-lanes scaffold (from agent-lanes init)
    handoff.yaml
    bin/handoff
    state/
  tasks/
    01-contract-review.yaml                # cross-agent review of step 00
    02-results-review.yaml                 # cross-agent review of step 02
  outputs/                                 # artifacts produced during execution
  references/                              # supplementary docs prompts read (untracked)

Numbered prompts reflect execution order. Tasks under `tasks/` correspond to checkpoints between prompts (typically: review of the prior step's output before the next step proceeds).

## Required README sections

- **Objective** — one paragraph: what this pack is for.
- **Target** — branch, worktree path, expected branch name, write scope (what's allowed and disallowed).
- **Read first** — files the prompts should consume before starting.
- **Prompts** — ordered list of which prompt files to run in what sequence.
- **Output contract** — where output files land.
- **Acceptance gates** — what makes the work "done" (tests passing, no regressions, etc.).
- **Recommended model / effort** per role — see [agent-lanes metadata routing](../CONTRACT.md#14-metadata-extension-point).
- **Handoff roles** — short table mapping orchestrator / dispatcher / reviewer to the commands they run.

## Required executor-prompt sections

- Role, target branch, target worktree, owned write scope.
- "Run this prompt with cwd set to ..." — explicit working directory.
- Read-first list.
- Task statement.
- Required outputs (file paths).
- Guardrails (do-not list).
- Final response format.

## Required reviewer-prompt sections

- Role, target branch, target worktree, review scope.
- "Run this prompt with cwd set to ..." — explicit working directory.
- Review method — what to check (correctness, contract drift, scope creep, missing evidence).
- Findings-first output rule.
- Verdict line: `accept` | `accept-with-follow-ups` | `needs-revision`.

## Review is first-class

Don't ship a pack that has only an executor. Even a single review pass catches scope drift, contract drift, and missing evidence. Most packs need:

1. executor prompt
2. reviewer prompt (cross-agent via agent-lanes)
3. an executor follow-up pass that consumes review feedback

Apply every reviewer suggestion that fits the scope — including non-blocking ones. Skip a suggestion only with a concrete reason (it's wrong, already handled, out of scope, contradicts a higher-priority instruction). Record
skipped or deferred suggestions with reasons.

## Worked example

A pack that adds a frontend feature with two cross-agent review checkpoints:

note-editor-feature-2026-MM-DD/
  README.md
  START-HERE-orchestrator-prompt.md
  00-implementation-contract-prompt.md
  01-executor-prompt.md
  02-results-reviewer-prompt.md
  03-final-handoff-prompt.md
  handoff/
    handoff.yaml                  # scaffolded by agent-lanes init
    bin/handoff
    state/
  tasks/
    01-contract-review.yaml
    02-results-review.yaml
  outputs/

`tasks/01-contract-review.yaml`:

```yaml
lane: default
metadata:
  required_vendor: claude
  model_class: sonnet
  effort: high
request_from: outputs/00-implementation-contract.md
response_to: outputs/01-contract-review.md
prompt: |
  Review the implementation contract. Check for unsupported assumptions,
  missing edge cases, integration risks, and gaps in the test plan.

  Verdict: accept | accept-with-follow-ups | needs-revision.

The orchestrator prompt drives the pack:

# After step 00 writes outputs/00-implementation-contract.md:
TASK_ID=$(./handoff/bin/handoff submit \
  --task tasks/01-contract-review.yaml \
  --json | jq -r .task_id)
./handoff/bin/handoff wait "$TASK_ID"
# wait unblocks when the reviewer's response lands at outputs/01-contract-review.md
# orchestrator reads the review, applies feedback, then runs step 01 (executor).

Naming conventions

- Pack folders: <topic>-<YYYY-MM-DD> (e.g. note-editor-feature-2026-05-06). Date helps avoid name collisions and signals when the pack was scoped.
- Prompts: numbered (00-, 01-, ...) reflecting execution order, plus a START-HERE-orchestrator-prompt.md for the orchestrator agent.
- Tasks: short slug (01-contract-review.yaml, 02-results-review.yaml).
- Outputs: numbering matches the prompt that wrote them.

Branch-specific vs reusable

Two valid pack modes:

- Branch-specific — pack is tied to one active branch / worktree. README names exact paths. Most common.
- Reusable — pack is a template, parameterized with {BRANCH}, {WORKTREE}, {OUTPUT_DIR} placeholders. Useful when a pattern recurs.

Pick at pack creation; don't drift between the two.

Vendor-neutral routing

agent-lanes' metadata routing (required_vendor, model_class, effort) is what this format leans on for cross-agent coordination. The convention is workspace-specific — pick what makes sense for your setup. A reasonable default:

- Code review → required_vendor: claude (or whichever vendor you trust most for review judgment).
- Mechanical execution with no holistic-architecture context needed → required_vendor: codex or any.

The protocol does not enforce vendor preferences; this is your team's convention. Document yours in your workspace's CLAUDE.md (or equivalent) so future scaffolding agents inherit it.

Scaffolding shortcut

Paste-ready meta-prompt for any agent (Claude Code, Codex, etc.) to scaffold a pack from a one-line description:

You are creating a new prompt pack following the agent-lanes prompt-pack convention at `docs/prompt-pack-guide.md`. The user has described the work as:

<one-line task description>

Steps:

1. Pick a pack name as `<short-slug>-<YYYY-MM-DD>` and create the folder.
2. Run `agent-lanes init --queue-root <abs-path-to-shared-state>` inside the pack folder to scaffold the engine. (If the workspace doesn't have a shared queue yet, run `agent-lanes init-pool <workspace-root>` first.)
3. Create the standard files: README.md, START-HERE-orchestrator-prompt.md, 00-implementation-contract-prompt.md, 01-executor-prompt.md, 02-results-reviewer-prompt.md, 03-final-handoff-prompt.md.
4. Create the standard tasks: tasks/01-contract-review.yaml and tasks/02-results-review.yaml. Use the metadata routing convention from your workspace (or the defaults in `docs/prompt-pack-guide.md`).
5. Fill the README's required sections from the user's task description.
6. Each prompt file should follow the required-sections shape from `docs/prompt-pack-guide.md`.
7. After scaffolding, give the user the orchestrator prompt to paste into Codex (or whichever orchestrator agent), plus the polling chat prompt path for any reviewer chats.

Stop after scaffolding. Do not start executing the pack.

This is a convention you can override or rewrite per workspace. Treat it as a starting point.

Relation to agent-lanes

agent-lanes is the protocol underneath: lanes, tasks, metadata, dispatchers. This guide layers a workflow on top. You can use agent-lanes without packs (just submit ad-hoc tasks), and you can use packs without rigidly following
 this guide (any folder shape works as long as the orchestrator submits valid tasks). They are independent.

For protocol details see CONTRACT.md.

Note: the embedded markdown above uses ` ```` ` (four backticks) as the outer fence to allow ` ``` ` blocks inside. When you write the file, write it as plain markdown — the file's contents start with `# Opinionated Prompt-Pack
 Format` and end at the line before the closing four-backtick fence. Do NOT include the four-backtick wrapper in the file itself.

#!/usr/bin/env bash
# agent-lanes dispatcher (Mode B)
#
# Polls a lane and pipes each task to a fresh headless agent invocation.
# This is a stateless task-agnostic dispatcher: each task carries its own
# prompt and context. The dispatcher does NOT have a hardcoded prompt template.
#
# Customize three variables before running:
#   LANE              — the lane to poll (e.g. "default")
#   OWNER             — claim attribution string (e.g. "${USER}-$(hostname)-$$")
#   HEADLESS_AGENT_CMD — the agent invocation, e.g.:
#                          claude -p ""               # Claude Code headless
#                          codex exec --prompt-stdin  # Codex CLI headless
#
# See agent-lanes CONTRACT.md sections "Dispatcher pattern" and
# "Task threading pattern" for the full design.

set -euo pipefail

LANE="${LANE:-default}"
OWNER="${OWNER:-${USER:-worker}-$(hostname)-$$}"
HEADLESS_AGENT_CMD="${HEADLESS_AGENT_CMD:-cat}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HANDOFF="${SCRIPT_DIR}/bin/handoff"

command -v jq >/dev/null 2>&1 || {
  echo "dispatcher: jq is required" >&2
  exit 1
}

while true; do
  # Long-poll the lane. While idle this consumes zero tokens.
  TASK_JSON="$("${HANDOFF}" wait --lane "${LANE}" --json --quiet --timeout 21600 || true)"
  if [ -z "${TASK_JSON}" ]; then
    continue
  fi

  STATUS="$(printf '%s' "${TASK_JSON}" | jq -r '.status // empty')"
  if [ "${STATUS}" != "task_available" ]; then
    # idle / timeout — re-arm
    continue
  fi

  TASK_ID="$(printf '%s' "${TASK_JSON}" | jq -r '.task.id')"
  REQUEST_PATH="$(printf '%s' "${TASK_JSON}" | jq -r '.task.request_path // empty')"
  PROMPT="$(printf '%s' "${TASK_JSON}" | jq -r '.task.prompt // empty')"
  WORKTREE_PATH="$(printf '%s' "${TASK_JSON}" | jq -r '.task.worktree_path // empty')"
  EXPECTED_BRANCH="$(printf '%s' "${TASK_JSON}" | jq -r '.task.expected_branch // empty')"
  THREAD_ID="$(printf '%s' "${TASK_JSON}" | jq -r '.task.metadata.thread_id // empty')"
  PARENT_TASK_ID="$(printf '%s' "${TASK_JSON}" | jq -r '.task.metadata.parent_task_id // empty')"

  # Claim with a 2h lease.
  CLAIM_JSON="$("${HANDOFF}" claim "${TASK_ID}" --owner "${OWNER}" --lease-seconds 7200 --json)"
  CLAIM_TOKEN="$(printf '%s' "${CLAIM_JSON}" | jq -r '.claim_token')"

  TMPDIR_RUN="$(mktemp -d)"
  trap 'rm -rf "${TMPDIR_RUN}"' EXIT

  INPUT_FILE="${TMPDIR_RUN}/input.txt"
  OUTPUT_FILE="${TMPDIR_RUN}/output.txt"

  {
    if [ -n "${PROMPT}" ]; then
      printf '%s\n\n' "${PROMPT}"
    fi
    if [ -n "${WORKTREE_PATH}" ]; then
      printf 'Worktree: %s\n' "${WORKTREE_PATH}"
    fi
    if [ -n "${EXPECTED_BRANCH}" ]; then
      printf 'Branch: %s\n' "${EXPECTED_BRANCH}"
    fi

    # If this task is part of a thread, walk the parent chain and concatenate
    # prior responses for context.
    if [ -n "${THREAD_ID}" ] && [ -n "${PARENT_TASK_ID}" ]; then
      printf '\n--- thread context (thread_id=%s) ---\n' "${THREAD_ID}"
      CURRENT="${PARENT_TASK_ID}"
      while [ -n "${CURRENT}" ]; do
        PRIOR="$("${HANDOFF}" wait "${CURRENT}" --timeout 1 --json --quiet 2>/dev/null || true)"
        if [ -z "${PRIOR}" ]; then
          break
        fi
        PRIOR_BODY="$(printf '%s' "${PRIOR}" | jq -r '.response.body // empty')"
        if [ -n "${PRIOR_BODY}" ]; then
          printf '\n>>> task %s response:\n%s\n' "${CURRENT}" "${PRIOR_BODY}"
        fi
        CURRENT="$(printf '%s' "${PRIOR}" | jq -r '.response.metadata.parent_task_id // empty')"
      done
      printf '\n--- end thread context ---\n\n'
    fi

    if [ -n "${REQUEST_PATH}" ] && [ -f "${REQUEST_PATH}" ]; then
      printf '\n--- request artifact (%s) ---\n' "${REQUEST_PATH}"
      cat "${REQUEST_PATH}"
      printf '\n--- end request artifact ---\n'
    fi
  } > "${INPUT_FILE}"

  # Pipe input to the headless agent and capture stdout.
  "${HEADLESS_AGENT_CMD}" < "${INPUT_FILE}" > "${OUTPUT_FILE}" || {
    "${HANDOFF}" respond "${TASK_ID}" \
      --claim-token "${CLAIM_TOKEN}" \
      --status failed \
      --body "headless agent invocation failed" >/dev/null
    rm -rf "${TMPDIR_RUN}"
    trap - EXIT
    continue
  }

  # Submit the response. Pattern-agnostic: do not pass --verdict.
  "${HANDOFF}" respond "${TASK_ID}" \
    --claim-token "${CLAIM_TOKEN}" \
    --file "${OUTPUT_FILE}" >/dev/null

  rm -rf "${TMPDIR_RUN}"
  trap - EXIT
done

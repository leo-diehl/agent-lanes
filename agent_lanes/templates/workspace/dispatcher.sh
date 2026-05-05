#!/usr/bin/env bash
# agent-lanes vendor-routed metadata dispatcher
#
# This script is a long-running router for a shared workspace queue. It watches
# one lane, inspects each task's metadata, claims tasks that match its vendor,
# and spawns a fresh headless agent for every claimed task.
#
# The dispatcher itself has no model or reasoning effort. Those are resolved
# from each task's metadata:
#   metadata.required_vendor - claude, codex, or any (default: any)
#   metadata.model_class     - vendor-specific class (default: any)
#   metadata.effort          - low, medium, high, or xhigh (default: medium)
#
# Required environment:
#   VENDOR      - claude or codex
#   QUEUE_ROOT  - absolute path to the shared queue state directory
#
# Optional environment:
#   CONFIG              - handoff.yaml path (default: ${QUEUE_ROOT%/state}/handoff.yaml)
#   LANE                - lane to poll (default: default)
#   OWNER               - claim owner (default: ${USER}-${VENDOR}-$$)
#   HEADLESS_AGENT_CMD  - base CLI invocation; defaults by vendor:
#                         claude: claude -p ""
#                         codex:  codex exec --prompt-stdin
#                         Override when your local CLI needs different flags.
#   LEASE_SECONDS       - claim lease duration (default: 7200)
#   WAIT_TIMEOUT        - long-poll timeout (default: 21600)
#
# Usage examples:
#   VENDOR=claude QUEUE_ROOT=/Users/me/workspace/.agent-lanes-queue/state bash dispatcher.sh
#   VENDOR=codex QUEUE_ROOT=/Users/me/workspace/.agent-lanes-queue/state bash dispatcher.sh
#
# Dependencies: jq and agent-lanes must be on PATH.

set -euo pipefail

log() { printf '%s\n' "$*" >&2; }

[ -n "${VENDOR:-}" ] || { log "dispatcher: VENDOR is required (claude or codex)"; exit 1; }
case "${VENDOR}" in
  claude|codex) ;;
  *) log "dispatcher: VENDOR must be claude or codex, got: ${VENDOR}"; exit 1 ;;
esac
[ -n "${QUEUE_ROOT:-}" ] || { log "dispatcher: QUEUE_ROOT is required and must be an absolute queue state path"; exit 1; }
case "${QUEUE_ROOT}" in
  /*) ;;
  *) log "dispatcher: QUEUE_ROOT must be absolute, got: ${QUEUE_ROOT}"; exit 1 ;;
esac
CONFIG="${CONFIG:-${QUEUE_ROOT%/state}/handoff.yaml}"
[ -f "${CONFIG}" ] || {
  log "dispatcher: CONFIG file not found at ${CONFIG}; set CONFIG env var or ensure QUEUE_ROOT's parent contains handoff.yaml"
  exit 1
}

LANE="${LANE:-default}"
OWNER="${OWNER:-${USER:-worker}-${VENDOR}-$$}"
LEASE_SECONDS="${LEASE_SECONDS:-7200}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-21600}"
if [ -z "${HEADLESS_AGENT_CMD:-}" ]; then
  [ "${VENDOR}" = "claude" ] && HEADLESS_AGENT_CMD='claude -p ""' || HEADLESS_AGENT_CMD="codex exec --prompt-stdin"
fi

command -v jq >/dev/null 2>&1 || { log "dispatcher: jq is required on PATH"; exit 1; }
command -v agent-lanes >/dev/null 2>&1 || { log "dispatcher: agent-lanes is required on PATH"; exit 1; }

normalize_effort() {
  case "${1:-medium}" in
    low|medium|high|xhigh) printf '%s\n' "${1:-medium}" ;;
    *) log "[warn] task ${TASK_ID:-unknown} unknown effort=${1:-}; using medium"; printf '%s\n' "medium" ;;
  esac
}

model_flag() {
  local key="${VENDOR}:${1:-any}"
  case "${key}" in
    claude:opus) printf '%s\n' "--model claude-opus-4-7" ;;
    claude:sonnet|claude:any|claude:) printf '%s\n' "--model claude-sonnet-4-6" ;;
    claude:haiku) printf '%s\n' "--model claude-haiku-4-5" ;;
    codex:gpt-5-3|codex:any|codex:) printf '%s\n' "--model gpt-5.3" ;;
    codex:gpt-5-3-spark) printf '%s\n' "--model gpt-5.3-codex-spark" ;;
    claude:*) log "[warn] task ${TASK_ID:-unknown} unknown model_class=${1} for vendor=claude; using sonnet"; printf '%s\n' "--model claude-sonnet-4-6" ;;
    codex:*) log "[warn] task ${TASK_ID:-unknown} unknown model_class=${1} for vendor=codex; using gpt-5.3"; printf '%s\n' "--model gpt-5.3" ;;
  esac
}

effort_flag() {
  if [ "${VENDOR}" = "codex" ]; then
    printf '%s\n' "--reasoning-effort ${1:-medium}"
  else
    # Claude headless uses an effort hint in the prompt because `claude -p`
    # may not expose a native effort flag. Override HEADLESS_AGENT_CMD and
    # this template if a future Claude CLI adds one.
    printf '%s\n' ""
  fi
}

append_thread_context() {
  local input_file="$1" thread_file current prior_task prior_response prior_body next_file
  [ -n "${THREAD_ID}" ] && [ -n "${PARENT_TASK_ID}" ] || return

  thread_file="$(mktemp)"
  current="${PARENT_TASK_ID}"
  while [ -n "${current}" ]; do
    prior_task="$(agent-lanes --config "${CONFIG}" --store "${QUEUE_ROOT}" status "${current}" --json 2>/dev/null || true)"
    [ -n "${prior_task}" ] || break

    prior_response="$(agent-lanes --config "${CONFIG}" --store "${QUEUE_ROOT}" wait "${current}" --timeout 1 --json --quiet 2>/dev/null || true)"
    prior_body="$(printf '%s' "${prior_response}" | jq -r '.response.body // empty')"
    if [ -n "${prior_body}" ]; then
      next_file="$(mktemp)"
      { printf '\n>>> task %s response:\n%s\n' "${current}" "${prior_body}"; cat "${thread_file}"; } > "${next_file}"
      mv "${next_file}" "${thread_file}"
    fi

    current="$(printf '%s' "${prior_task}" | jq -r '.task.metadata.parent_task_id // empty')"
  done

  if [ -s "${thread_file}" ]; then
    { printf '\n--- thread context (thread_id=%s) ---\n' "${THREAD_ID}"; cat "${thread_file}"; printf '\n--- end thread context ---\n\n'; } >> "${input_file}"
  fi
  rm -f "${thread_file}"
}

build_input() {
  local input_file="$1"
  {
    [ "${VENDOR}" = "claude" ] && printf '[Reasoning effort: %s]\n\n' "${EFFORT}"
    [ -n "${PROMPT}" ] && printf '%s\n\n' "${PROMPT}"
    [ -n "${WORKTREE_PATH}" ] && printf 'Worktree: %s\n' "${WORKTREE_PATH}"
    [ -n "${EXPECTED_BRANCH}" ] && printf 'Expected branch: %s\n' "${EXPECTED_BRANCH}"
    if [ -n "${REQUEST_PATH}" ]; then
      if [ -f "${REQUEST_PATH}" ]; then
        printf '\n--- request artifact (%s) ---\n' "${REQUEST_PATH}"
        cat "${REQUEST_PATH}"
        printf '\n--- end request artifact ---\n'
      else
        log "[warn] task ${TASK_ID} request_path not found: ${REQUEST_PATH}"
      fi
    fi
  } > "${input_file}"
  append_thread_context "${input_file}"
}

while true; do
  TASK_JSON="$(agent-lanes --config "${CONFIG}" --store "${QUEUE_ROOT}" wait --lane "${LANE}" --json --quiet --timeout "${WAIT_TIMEOUT}" || true)"
  [ -n "${TASK_JSON}" ] || continue
  [ "$(printf '%s' "${TASK_JSON}" | jq -r '.status // empty')" = "task_available" ] || continue

  TASK_ID="$(printf '%s' "${TASK_JSON}" | jq -r '.task.id')"
  REQUIRED_VENDOR="$(printf '%s' "${TASK_JSON}" | jq -r '.task.metadata.required_vendor // "any"')"
  MODEL_CLASS="$(printf '%s' "${TASK_JSON}" | jq -r '.task.metadata.model_class // "any"')"
  EFFORT="$(normalize_effort "$(printf '%s' "${TASK_JSON}" | jq -r '.task.metadata.effort // "medium"')")"
  PROMPT="$(printf '%s' "${TASK_JSON}" | jq -r '.task.prompt // empty')"
  REQUEST_PATH="$(printf '%s' "${TASK_JSON}" | jq -r '.task.request_path // empty')"
  WORKTREE_PATH="$(printf '%s' "${TASK_JSON}" | jq -r '.task.worktree_path // empty')"
  EXPECTED_BRANCH="$(printf '%s' "${TASK_JSON}" | jq -r '.task.expected_branch // empty')"
  THREAD_ID="$(printf '%s' "${TASK_JSON}" | jq -r '.task.metadata.thread_id // empty')"
  PARENT_TASK_ID="$(printf '%s' "${TASK_JSON}" | jq -r '.task.metadata.parent_task_id // empty')"
  REQUIRED_VENDOR="${REQUIRED_VENDOR:-any}"
  MODEL_CLASS="${MODEL_CLASS:-any}"

  if [ "${REQUIRED_VENDOR}" != "${VENDOR}" ] && [ "${REQUIRED_VENDOR}" != "any" ]; then
    log "[skip] task ${TASK_ID} wants vendor=${REQUIRED_VENDOR}; I am ${VENDOR}"
    sleep 2
    continue
  fi

  if ! CLAIM_JSON="$(agent-lanes --config "${CONFIG}" --store "${QUEUE_ROOT}" claim "${TASK_ID}" --owner "${OWNER}" --lease-seconds "${LEASE_SECONDS}" --json 2>&1)"; then
    log "[race] task ${TASK_ID} claim failed: ${CLAIM_JSON}"
    continue
  fi
  CLAIM_TOKEN="$(printf '%s' "${CLAIM_JSON}" | jq -r '.claim_token // empty')"
  [ -n "${CLAIM_TOKEN}" ] || { log "[race] task ${TASK_ID} claim returned no token"; continue; }

  MODEL_FLAG="$(model_flag "${MODEL_CLASS}")"
  EFFORT_FLAG="$(effort_flag "${EFFORT}")"
  AGENT_FLAGS="${MODEL_FLAG}${EFFORT_FLAG:+ ${EFFORT_FLAG}}"
  TMPDIR_RUN="$(mktemp -d)"
  INPUT_FILE="${TMPDIR_RUN}/input.txt"
  ERROR_FILE="${TMPDIR_RUN}/stderr.txt"
  OUTPUT_FILE="/tmp/agent-lanes-resp-${TASK_ID}.txt"
  rm -f "${OUTPUT_FILE}"

  build_input "${INPUT_FILE}"
  if ! eval "${HEADLESS_AGENT_CMD} ${AGENT_FLAGS}" < "${INPUT_FILE}" > "${OUTPUT_FILE}" 2> "${ERROR_FILE}"; then
    { printf 'headless agent invocation failed for task %s\n' "${TASK_ID}"; [ -s "${ERROR_FILE}" ] && { printf '\n--- stderr ---\n'; cat "${ERROR_FILE}"; }; } > "${OUTPUT_FILE}"
    agent-lanes --config "${CONFIG}" --store "${QUEUE_ROOT}" respond "${TASK_ID}" --claim-token "${CLAIM_TOKEN}" --status failed --file "${OUTPUT_FILE}" --json >/dev/null
    rm -rf "${TMPDIR_RUN}"
    rm -f "${OUTPUT_FILE}"
    continue
  fi

  agent-lanes --config "${CONFIG}" --store "${QUEUE_ROOT}" respond "${TASK_ID}" --claim-token "${CLAIM_TOKEN}" --file "${OUTPUT_FILE}" --json >/dev/null
  rm -rf "${TMPDIR_RUN}"
  rm -f "${OUTPUT_FILE}"
done

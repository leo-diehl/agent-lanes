#!/usr/bin/env bash
# agent-a: submits a review task, waits for the response, prints it.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$HERE/handoff.yaml"
STORE="$HERE/state"

mkdir -p "$HERE/outputs"
REQUEST="$HERE/outputs/request.md"
RESPONSE="$HERE/outputs/response.md"
echo "Review this. It says hello." > "$REQUEST"

echo "[agent-a] submitting task to lane=review..."
SUBMIT=$(agent-lanes --config "$CONFIG" --store "$STORE" \
  submit --lane review \
  --request-from "$REQUEST" \
  --response-to "$RESPONSE" \
  --prompt "Review the request artifact and respond with a verdict." \
  --json)
TASK_ID=$(printf '%s' "$SUBMIT" | jq -r '.task.id')
echo "[agent-a] queued task $TASK_ID; waiting for response..."

agent-lanes --config "$CONFIG" --store "$STORE" \
  wait "$TASK_ID" --timeout 30 --json --quiet >/dev/null
echo "[agent-a] response received:"
echo "----"
cat "$RESPONSE"
echo "----"

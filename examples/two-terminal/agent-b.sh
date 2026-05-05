#!/usr/bin/env bash
# agent-b: long-polls the review lane, claims the next task, responds with a fixed review.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$HERE/handoff.yaml"
STORE="$HERE/state"

echo "[agent-b] long-polling lane=review (timeout 30s)..."
TASK_JSON=$(agent-lanes --config "$CONFIG" --store "$STORE" \
  wait --lane review --json --quiet --timeout 30)
TASK_ID=$(printf '%s' "$TASK_JSON" | jq -r '.task.id')
echo "[agent-b] received task $TASK_ID"

CLAIM=$(agent-lanes --config "$CONFIG" --store "$STORE" \
  claim "$TASK_ID" --owner agent-b --lease-seconds 60 --json)
TOKEN=$(printf '%s' "$CLAIM" | jq -r '.claim_token')
echo "[agent-b] claimed; submitting fake review"

REVIEW_FILE="$(mktemp)"
cat > "$REVIEW_FILE" <<'EOF'
This is a fixed review for the demo. The request artifact looks fine.
VERDICT: accept
EOF

agent-lanes --config "$CONFIG" --store "$STORE" \
  respond "$TASK_ID" --claim-token "$TOKEN" \
  --file "$REVIEW_FILE" --verdict accept --json >/dev/null
echo "[agent-b] responded; done"
rm -f "$REVIEW_FILE"

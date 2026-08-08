#!/usr/bin/env bash
# Export aggregate usage CSV from Quota Service (LLM-S17.4).
# Usage:
#   ./local-dev/export-usage.sh              # event-level CSV
#   ./local-dev/export-usage.sh --summary    # daily rollup CSV
#   ./local-dev/export-usage.sh alice
set -euo pipefail
LOCAL_DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${LOCAL_DEV}/python.sh"

BASE="${QUOTA_SERVICE_URL:-http://127.0.0.1:8090}"
MODE="events"
USER_Q=""
for arg in "$@"; do
  if [[ "$arg" == "--summary" ]]; then
    MODE="summary"
  else
    USER_Q="$arg"
  fi
done

if [[ "$MODE" == "summary" ]]; then
  URL="${BASE}/v1/usage/summary"
  [[ -n "$USER_Q" ]] && URL="${URL}?user=${USER_Q}"
  curl -fsS "$URL" | "$NBI_PYTHON" -c '
import json,sys
data=json.load(sys.stdin)
print("date,user,plan_id,tokens,requests,chat_tokens,inline_tokens")
for day, users in sorted((data.get("summary") or {}).items()):
    for user, b in sorted(users.items()):
        feats = b.get("by_feature") or {}
        print(",".join([
            day, user, str(b.get("plan_id","")),
            str(b.get("tokens",0)), str(b.get("requests",0)),
            str(feats.get("chat",0)), str(feats.get("inline",0)),
        ]))
'
else
  URL="${BASE}/v1/usage"
  [[ -n "$USER_Q" ]] && URL="${URL}?user=${USER_Q}"
  echo "user,plan_id,model,feature,tokens,ts"
  curl -fsS "$URL" | "$NBI_PYTHON" -c '
import json,sys
data=json.load(sys.stdin)
for e in data.get("events",[]):
    print(",".join([
        str(e.get("user","")),
        str(e.get("plan_id","")),
        str(e.get("model","")),
        str(e.get("feature","")),
        str(e.get("tokens","")),
        str(e.get("ts","")),
    ]))
'
fi

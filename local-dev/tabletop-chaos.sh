#!/usr/bin/env bash
# Operator tabletop drills (LLM-S18.2 / S03.3) — local mock only.
# Exercises: token refresh chaos, quota-service down, mass soft-cap/429.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DEV="${ROOT}/local-dev"
cd "$ROOT"
# shellcheck disable=SC1091
source "${LOCAL_DEV}/python.sh"
export PYTHON="$NBI_PYTHON"

pass=0
fail=0
check() {
  local name="$1"; shift
  if "$@"; then echo "PASS  $name"; pass=$((pass+1)); else echo "FAIL  $name"; fail=$((fail+1)); fi
}

cleanup() {
  ./local-dev/stop-sidecar.sh >/dev/null 2>&1 || true
  ./local-dev/stop-quota-service.sh >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

echo "== drill 1: token refresh mid-session (MOCK_TOKEN_TTL_S) =="
MOCK_TOKEN_TTL_S=2 TOKEN_PROVIDER=mock ./local-dev/start-sidecar.sh
sleep 0.5
# First call
curl -fsS http://127.0.0.1:8089/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"databricks/gdp-gpt4o","messages":[{"role":"user","content":"a"}],"stream":false}' \
  -o /tmp/nbi-chaos-a.json
# Wait past TTL+skew so mint refreshes
sleep 2.5
curl -fsS http://127.0.0.1:8089/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"databricks/gdp-gpt4o","messages":[{"role":"user","content":"b"}],"stream":false}' \
  -o /tmp/nbi-chaos-b.json
check "chat after refresh" grep -q '"choices"' /tmp/nbi-chaos-b.json
check "health still warm" bash -c 'curl -fsS http://127.0.0.1:8089/healthz | grep -q token_warm.:.true'
./local-dev/stop-sidecar.sh >/dev/null 2>&1 || true

echo "== drill 2: quota service down → sidecar fail-closed =="
# Start quota, wire sidecar HTTP backend, then kill quota
rm -f local-dev/.runtime/quota-service-store.json
./local-dev/start-quota-service.sh
QUOTA_BACKEND=http QUOTA_SERVICE_URL=http://127.0.0.1:8090 \
  NBI_LLM_USER=chaos NBI_LLM_GROUPS=interns \
  ./local-dev/start-sidecar.sh
curl -fsS http://127.0.0.1:8089/quota -o /tmp/nbi-chaos-q.json
check "quota via http backend" grep -q '"plan_id"' /tmp/nbi-chaos-q.json
./local-dev/stop-quota-service.sh
# Next chat should error (connection refused) — treat as fail-closed
code=$(curl -sS -o /tmp/nbi-chaos-down.json -w '%{http_code}' \
  http://127.0.0.1:8089/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"databricks/gdp-gpt4o","messages":[{"role":"user","content":"x"}],"stream":false}' || true)
check "fail-closed when quota down" bash -c "[[ \"$code\" != \"200\" ]]"
./local-dev/stop-sidecar.sh >/dev/null 2>&1 || true

echo "== drill 3: hard 429 when over quota =="
QUOTA_TOKENS_DAY=40 ./local-dev/start-sidecar.sh
SMOKE_QUOTA_DENY=1 ./local-dev/smoke-test.sh
check "deny drill exited 0" true
./local-dev/stop-sidecar.sh >/dev/null 2>&1 || true

echo "== drill 4: refuse non-loopback bind (LLM-S01.3) =="
set +e
HOST=0.0.0.0 PORT=18091 MODE=mock TOKEN_PROVIDER=mock \
  QUOTA_BACKEND=local QUOTA_DEFAULT_PLAN=local \
  "$PYTHON" local-dev/llm-gateway-sidecar/sidecar.py >/tmp/nbi-chaos-bind.log 2>&1 &
bind_pid=$!
sleep 0.6
kill "$bind_pid" 2>/dev/null
wait "$bind_pid" 2>/dev/null
set -e
check "non-loopback refused" grep -q 'loopback only' /tmp/nbi-chaos-bind.log

echo "== results: pass=${pass} fail=${fail} =="
[[ "$fail" -eq 0 ]]
echo "TABLETOP CHAOS PASSED"

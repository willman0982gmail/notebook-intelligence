#!/usr/bin/env bash
# Smoke-test the local LLM sidecar (curl). Does not require JupyterLab.
set -euo pipefail

LOCAL_DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${LOCAL_DEV}/python.sh"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8089}"
BASE="http://${HOST}:${PORT}"
MODEL_ID="${MODEL_ID:-databricks/gdp-gpt4o}"
TMPDIR_SMOKE="${TMPDIR:-/tmp}/nbi-sidecar-smoke-$$"
mkdir -p "$TMPDIR_SMOKE"
trap 'rm -rf "$TMPDIR_SMOKE"' EXIT

pass=0
fail=0
check() {
  local name="$1"
  shift
  if "$@"; then
    echo "PASS  $name"
    pass=$((pass + 1))
  else
    echo "FAIL  $name"
    fail=$((fail + 1))
  fi
}

echo "==> smoke against ${BASE}"

check "healthz" curl -fsS "${BASE}/healthz" -o "${TMPDIR_SMOKE}/health.json"
check "healthz body" grep -q '"status": "ok"' "${TMPDIR_SMOKE}/health.json"

if [[ "${SMOKE_QUOTA_DENY:-0}" == "1" ]]; then
  # Dedicated path: do not burn budget with normal chat/stream first.
  check "quota" curl -fsS "${BASE}/quota" -o "${TMPDIR_SMOKE}/quota.json"
  big="$("$NBI_PYTHON" -c 'print("x"*80000)')"
  code="$(curl -sS -o "${TMPDIR_SMOKE}/deny.json" -w '%{http_code}' \
    "${BASE}/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"${MODEL_ID}\",\"messages\":[{\"role\":\"user\",\"content\":\"${big}\"}],\"stream\":false}" || true)"
  check "quota 429" bash -c "[[ \"$code\" == \"429\" ]]"
  check "quota type" grep -q 'quota_exceeded' "${TMPDIR_SMOKE}/deny.json"
  echo "==> results: pass=${pass} fail=${fail}"
  [[ "$fail" -eq 0 ]]
  exit 0
fi

check "models" curl -fsS "${BASE}/v1/models" -o "${TMPDIR_SMOKE}/models.json"
check "models id" grep -q "$MODEL_ID" "${TMPDIR_SMOKE}/models.json"

check "quota" curl -fsS "${BASE}/quota" -o "${TMPDIR_SMOKE}/quota.json"
check "quota user" grep -q '"user_id"' "${TMPDIR_SMOKE}/quota.json"

check "chat non-stream" curl -fsS "${BASE}/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"${MODEL_ID}\",\"messages\":[{\"role\":\"user\",\"content\":\"say hi\"}],\"stream\":false}" \
  -o "${TMPDIR_SMOKE}/chat.json"
check "chat choices" grep -q '"choices"' "${TMPDIR_SMOKE}/chat.json"

check "chat stream" curl -fsS -N "${BASE}/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"${MODEL_ID}\",\"messages\":[{\"role\":\"user\",\"content\":\"stream please\"}],\"stream\":true}" \
  -o "${TMPDIR_SMOKE}/stream.txt"
check "chat stream sse" grep -q 'data:' "${TMPDIR_SMOKE}/stream.txt"

# Inline feature tag (LLM-S09 / S22 / S24.1)
check "inline feature" curl -fsS "${BASE}/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -H 'X-NBI-Feature: inline' \
  -d "{\"model\":\"${MODEL_ID}\",\"messages\":[{\"role\":\"user\",\"content\":\"def foo():\\n    # <|fim|>\\n    pass\"}],\"stream\":false}" \
  -o "${TMPDIR_SMOKE}/inline.json"
check "inline choices" grep -q '"choices"' "${TMPDIR_SMOKE}/inline.json"

# Multi-turn history (LLM-S10.4)
check "multi-turn" curl -fsS "${BASE}/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"${MODEL_ID}\",\"messages\":[{\"role\":\"user\",\"content\":\"q1\"},{\"role\":\"assistant\",\"content\":\"a1\"},{\"role\":\"user\",\"content\":\"q2\"}],\"stream\":false}" \
  -o "${TMPDIR_SMOKE}/mt.json"
check "multi-turn choices" grep -q '"choices"' "${TMPDIR_SMOKE}/mt.json"

check "metrics" curl -fsS "${BASE}/metrics" -o "${TMPDIR_SMOKE}/metrics.txt"
check "metrics soft-cap series" grep -q 'nbi_llm_soft_cap_hits_total' "${TMPDIR_SMOKE}/metrics.txt"
check "metrics by feature" grep -q 'nbi_llm_requests_by_feature_total' "${TMPDIR_SMOKE}/metrics.txt"

echo "==> results: pass=${pass} fail=${fail}"
[[ "$fail" -eq 0 ]]

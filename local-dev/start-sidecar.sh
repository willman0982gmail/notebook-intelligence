#!/usr/bin/env bash
# Start the local LLM sidecar (mock by default).
set -euo pipefail
LOCAL_DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME="${LOCAL_DEV}/.runtime"
mkdir -p "$RUNTIME"
PID_FILE="${RUNTIME}/sidecar.pid"
LOG_FILE="${RUNTIME}/sidecar.log"

export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8089}"
export MODE="${MODE:-mock}"
export MODEL_ID="${MODEL_ID:-databricks/gdp-gpt4o}"
export NBI_LLM_USER="${NBI_LLM_USER:-local-dev}"
export NBI_LLM_PLAN="${NBI_LLM_PLAN:-local}"
export QUOTA_TOKENS_DAY="${QUOTA_TOKENS_DAY:-50000}"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "sidecar already running (pid $(cat "$PID_FILE")) on http://${HOST}:${PORT}"
  exit 0
fi

PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  for cand in python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      PY="$cand"
      break
    fi
  done
fi
[[ -n "$PY" ]] || { echo "ERROR: python3 not found"; exit 1; }

nohup "$PY" "${LOCAL_DEV}/llm-gateway-sidecar/sidecar.py" >"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"
sleep 0.5
if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "ERROR: sidecar failed to start; see $LOG_FILE"
  tail -n 40 "$LOG_FILE" || true
  exit 1
fi
echo "sidecar started pid=$(cat "$PID_FILE")  http://${HOST}:${PORT}  mode=${MODE}"
echo "log: $LOG_FILE"

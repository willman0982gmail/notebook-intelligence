#!/usr/bin/env bash
# Start Quota Service with Python >= 3.12.
set -euo pipefail
LOCAL_DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${LOCAL_DEV}/python.sh"
RUNTIME="${LOCAL_DEV}/.runtime"
mkdir -p "$RUNTIME"
PID_FILE="${RUNTIME}/quota-service.pid"
LOG_FILE="${RUNTIME}/quota-service.log"
export QUOTA_HOST="${QUOTA_HOST:-127.0.0.1}"
export QUOTA_PORT="${QUOTA_PORT:-8090}"
export QUOTA_STORE_PATH="${QUOTA_STORE_PATH:-${RUNTIME}/quota-service-store.json}"
export QUOTA_PLANS_PATH="${QUOTA_PLANS_PATH:-${LOCAL_DEV}/quota_service/plans.json}"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "quota service already running pid=$(cat "$PID_FILE")"
  exit 0
fi
echo "using Python: $NBI_PYTHON ($("$NBI_PYTHON" --version 2>&1))"
nohup "$NBI_PYTHON" "${LOCAL_DEV}/quota_service/server.py" >"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"
sleep 0.4
echo "quota service pid=$(cat "$PID_FILE") http://${QUOTA_HOST}:${QUOTA_PORT}"

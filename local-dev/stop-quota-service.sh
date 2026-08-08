#!/usr/bin/env bash
# Stop the local Quota Service if running.
set -euo pipefail
LOCAL_DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${LOCAL_DEV}/.runtime/quota-service.pid"
if [[ ! -f "$PID_FILE" ]]; then
  echo "no quota-service pid file"
  exit 0
fi
pid="$(cat "$PID_FILE")"
if kill -0 "$pid" 2>/dev/null; then
  kill "$pid" || true
  echo "stopped quota-service pid=$pid"
else
  echo "quota-service not running (stale pid $pid)"
fi
rm -f "$PID_FILE"

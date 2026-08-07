#!/usr/bin/env bash
# Stop the local LLM sidecar if running.
set -euo pipefail
LOCAL_DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${LOCAL_DEV}/.runtime/sidecar.pid"
if [[ ! -f "$PID_FILE" ]]; then
  echo "no sidecar pid file"
  exit 0
fi
pid="$(cat "$PID_FILE")"
if kill -0 "$pid" 2>/dev/null; then
  kill "$pid" || true
  echo "stopped sidecar pid=$pid"
else
  echo "sidecar not running (stale pid $pid)"
fi
rm -f "$PID_FILE"

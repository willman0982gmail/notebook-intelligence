#!/usr/bin/env bash
# Singleuser entrypoint: start LLM sidecar (and optional quota service), then Jupyter.
# LLM-S02.1 — use as container CMD or wrap jupyterhub-singleuser.
set -euo pipefail

LOCAL_DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="${LOCAL_DEV}/.runtime"
mkdir -p "$RUNTIME"

export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8089}"
export MODE="${MODE:-mock}"
export TOKEN_PROVIDER="${TOKEN_PROVIDER:-mock}"
export QUOTA_BACKEND="${QUOTA_BACKEND:-local}"
export NBI_LLM_USER="${NBI_LLM_USER:-${JUPYTERHUB_USER:-local-dev}}"

SIDECAR_PY="${LOCAL_DEV}/llm-gateway-sidecar/sidecar.py"
QUOTA_PY="${LOCAL_DEV}/quota_service/server.py"
# shellcheck disable=SC1091
source "${LOCAL_DEV}/python.sh"
PY="$NBI_PYTHON"
echo "[entrypoint] using Python: $PY ($("$PY" --version 2>&1))"

wait_http() {
  local url="$1" name="$2" tries="${3:-60}"
  local i
  for i in $(seq 1 "$tries"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "[entrypoint] ${name} ready"
      return 0
    fi
    sleep 0.25
  done
  echo "[entrypoint] ERROR: ${name} not ready: ${url}" >&2
  return 1
}

cleanup() {
  [[ -n "${QUOTA_PID:-}" ]] && kill "$QUOTA_PID" 2>/dev/null || true
  [[ -n "${SIDECAR_PID:-}" ]] && kill "$SIDECAR_PID" 2>/dev/null || true
}
trap cleanup EXIT

if [[ "${START_QUOTA_SERVICE:-0}" == "1" ]]; then
  export QUOTA_BACKEND=http
  export QUOTA_SERVICE_URL="${QUOTA_SERVICE_URL:-http://127.0.0.1:8090}"
  "$PY" "$QUOTA_PY" >"${RUNTIME}/quota-service.log" 2>&1 &
  QUOTA_PID=$!
  wait_http "${QUOTA_SERVICE_URL}/healthz" "quota-service"
fi

"$PY" "$SIDECAR_PY" >"${RUNTIME}/sidecar.log" 2>&1 &
SIDECAR_PID=$!
wait_http "http://${HOST}:${PORT}/healthz" "sidecar"

# Default: jupyter lab / jupyterhub-singleuser from PATH
if [[ $# -eq 0 ]]; then
  if command -v jupyterhub-singleuser >/dev/null 2>&1; then
    set -- jupyterhub-singleuser
  else
    set -- jupyter lab --ServerApp.ip=127.0.0.1 --ServerApp.open_browser=False
  fi
fi

echo "[entrypoint] exec: $*"
exec "$@"

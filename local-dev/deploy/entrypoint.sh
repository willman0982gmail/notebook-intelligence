#!/usr/bin/env bash
# Singleuser entrypoint: start LLM sidecar (and optional quota service), then Jupyter.
# LLM-S02.1 — use as container CMD or wrap jupyterhub-singleuser.
#
# Behaviour
# ---------
# 1. Resolve paths and create the per-pod runtime log directory.
# 2. Read (with defaults) the key env vars: HOST, PORT, MODE, TOKEN_PROVIDER,
#    QUOTA_BACKEND, NBI_LLM_USER.
# 3. Source ``python.sh`` to pin the NBI-managed Python interpreter so the
#    sidecar uses the same env as NBI itself (no venv mismatch / missing deps).
# 4. Optionally start a LOCAL quota-service (START_QUOTA_SERVICE=1) inside the
#    same container — used by default local-dev setup where there is no in-cluster
#    Quota Service Deployment.  In production this flag is OFF.
# 5. Start the sidecar in the background, wait for its /healthz to return 200
#    (this ensures Jupyter never starts before a bearer token can be minted).
# 6. If no CLI args are given, decide whether to exec ``jupyterhub-singleuser``
#    (production Hub singleuser image) or ``jupyter lab`` (local dev / laptop).
# 7. Exec the user process so PID 1 forwarding + signal handling are correct.
#
# Exit / cleanup
# --------------
# ``trap cleanup EXIT`` ensures SIGTERM / SIGINT / end-of-script always kills
# the background sidecar + quota service processes.  ``set -euo pipefail`` is
# applied so any unexpected error aborts the pod (fail-fast rather than
# silently starting Jupyter without a working sidecar).

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths.  LOCAL_DEV resolves to repo/local-dev regardless of where the script
# is invoked from — keeps python.sh / sidecar.py paths stable whether this
# entrypoint runs inside the container or on a dev laptop.
# ---------------------------------------------------------------------------
LOCAL_DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="${LOCAL_DEV}/.runtime"
mkdir -p "$RUNTIME"

# Env defaults — Hub's pre_spawn_hook injects the production values; defaults
# here make local-dev and plain-docker invocations work out of the box.
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8089}"
export MODE="${MODE:-mock}"
export TOKEN_PROVIDER="${TOKEN_PROVIDER:-mock}"
export QUOTA_BACKEND="${QUOTA_BACKEND:-local}"
# NBI_LLM_USER identity — prefer Hub-injected value, fall back to JupyterHub's
# own env var, then finally a laptop-safe local-dev placeholder.
export NBI_LLM_USER="${NBI_LLM_USER:-${JUPYTERHUB_USER:-local-dev}}"

# Paths to the two Python entry points.
SIDECAR_PY="${LOCAL_DEV}/llm-gateway-sidecar/sidecar.py"
QUOTA_PY="${LOCAL_DEV}/quota_service/server.py"
# shellcheck disable=SC1091 — python.sh is a sibling that resolves the venv.
source "${LOCAL_DEV}/python.sh"
PY="$NBI_PYTHON"
echo "[entrypoint] using Python: $PY ($("$PY" --version 2>&1))"

# ---------------------------------------------------------------------------
# Helper: poll an HTTP endpoint until it returns 2xx, up to $3 tries with
# 250ms sleep between attempts (= 15s default timeout).  Uses curl because
# the singleuser base image already ships it for Jupyter health checks.
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Cleanup: gracefully terminate background services on exit (SIGTERM / end
# of script / Ctrl-C).
#
# Issue #14 fix: escalate SIGTERM → wait loop (5s) → SIGKILL, then ``wait``
# to reap the child (prevents zombie processes when the sidecar ignores the
# initial TERM because it's stuck in a long-running upstream proxy read).
# Order matters: quota-service is shut down first so the sidecar sees the
# HTTP backend drop and can fail-closed cleanly.
# ---------------------------------------------------------------------------
_terminate_pid() {
  local pid="$1" name="$2"
  [[ -z "${pid:-}" ]] && return 0
  if ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  echo "[entrypoint] ${name}: sending SIGTERM (pid=${pid})"
  kill -TERM "$pid" 2>/dev/null || true
  local i
  for i in 1 2 3 4 5; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "[entrypoint] ${name}: exited cleanly after ${i}s"
      wait "$pid" || true
      return 0
    fi
    sleep 1
  done
  echo "[entrypoint] ${name}: still alive after 5s; sending SIGKILL (pid=${pid})"
  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" || true
}

cleanup() {
  _terminate_pid "${QUOTA_PID:-}" "quota-service"
  _terminate_pid "${SIDECAR_PID:-}" "sidecar"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# (Optional) Local quota-service.  In production the quota service runs in
# its own Deployment; START_QUOTA_SERVICE=1 is for the laptop / docker-compose
# case.  Setting this flag also forces QUOTA_BACKEND=http so the sidecar
# talks to the local co-process (which then uses the shared file store).
# ---------------------------------------------------------------------------
if [[ "${START_QUOTA_SERVICE:-0}" == "1" ]]; then
  export QUOTA_BACKEND=http
  export QUOTA_SERVICE_URL="${QUOTA_SERVICE_URL:-http://127.0.0.1:8090}"
  "$PY" "$QUOTA_PY" >"${RUNTIME}/quota-service.log" 2>&1 &
  QUOTA_PID=$!
  wait_http "${QUOTA_SERVICE_URL}/healthz" "quota-service"
fi

# ---------------------------------------------------------------------------
# Start LLM auth sidecar.  Logs go to .runtime/sidecar.log so they don't
# pollute the Jupyter server's stdout stream (which is the user's notebook
# console log stream in Hub).  wait_http blocks until the sidecar returns
# 200 on /healthz — which implies a bearer token was minted and cached,
# so the very first NBI chat request never sees a mint-time cold start.
# ---------------------------------------------------------------------------
"$PY" "$SIDECAR_PY" >"${RUNTIME}/sidecar.log" 2>&1 &
SIDECAR_PID=$!
wait_http "http://${HOST}:${PORT}/healthz" "sidecar"

# ---------------------------------------------------------------------------
# Decide what to exec if the user passed no script args:
#   - production Hub image: jupyterhub-singleuser on PATH
#   - local-dev: plain jupyter lab (no Hub, binds loopback, no browser popup)
# ---------------------------------------------------------------------------
if [[ $# -eq 0 ]]; then
  if command -v jupyterhub-singleuser >/dev/null 2>&1; then
    set -- jupyterhub-singleuser
  else
    set -- jupyter lab --ServerApp.ip=127.0.0.1 --ServerApp.open_browser=False
  fi
fi

echo "[entrypoint] exec: $*"
# ``exec`` replaces PID 1 so that SIGTERM from K8s / Ctrl-C on the laptop is
# delivered directly to the Jupyter process; cleanup trap still fires when
# the shell (now a passive wait) exits.
exec "$@"

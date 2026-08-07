#!/usr/bin/env bash
# Minimal local stack: JupyterLab 4.5.9 + Notebook Intelligence + mock LLM sidecar.
#
# Usage:
#   ./local-dev/start-local-stack.sh              # ensure install, start sidecar + Lab
#   ./local-dev/start-local-stack.sh --skip-build # reuse existing .venv-jl45 / wheel
#   ./local-dev/start-local-stack.sh --smoke-only # sidecar + curl smoke, no Lab
#   ./local-dev/start-local-stack.sh --no-lab     # install + sidecar, do not start Lab
#   ./local-dev/start-local-stack.sh --port 8890
#
# Env:
#   VENV_DIR / PYTHON / MODE / QUOTA_TOKENS_DAY / NPM_REGISTRY — see sibling scripts
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DEV="${REPO_ROOT}/local-dev"
RUNTIME="${LOCAL_DEV}/.runtime"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv-jl45}"
LAB_PORT="${JUPYTERLAB_PORT:-8890}"
SIDECAR_PORT="${PORT:-8089}"
SKIP_BUILD=0
SMOKE_ONLY=0
NO_LAB=0
OPEN_BROWSER=0

usage() {
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage ;;
    --venv) VENV_DIR="$2"; shift 2 ;;
    --port) LAB_PORT="$2"; shift 2 ;;
    --sidecar-port) SIDECAR_PORT="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --smoke-only) SMOKE_ONLY=1; shift ;;
    --no-lab) NO_LAB=1; shift ;;
    --browser) OPEN_BROWSER=1; shift ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

mkdir -p "$RUNTIME"

ensure_nbi_env() {
  if [[ -x "${VENV_DIR}/bin/jupyter" ]] && [[ "$SKIP_BUILD" -eq 1 ]]; then
    log "Reusing existing venv: ${VENV_DIR}"
    return
  fi
  if [[ -x "${VENV_DIR}/bin/jupyter" ]] && "${VENV_DIR}/bin/python" -c \
      "import jupyterlab, notebook_intelligence; assert jupyterlab.__version__.startswith('4.5.9')" \
      2>/dev/null; then
    log "Venv looks ready (JL 4.5.9 + NBI); skip rebuild (pass --skip-build to silence checks)"
    # Still allow rebuild if user didn't pass skip but env is good — rebuild only if missing
    return
  fi

  log "Building/installing JupyterLab 4.5.9 + Notebook Intelligence into ${VENV_DIR}"
  local -a args=(--venv "$VENV_DIR" --no-start --port "$LAB_PORT")
  if [[ "$SKIP_BUILD" -eq 1 ]]; then
    args+=(--skip-build)
  fi
  "${REPO_ROOT}/scripts/build-install-run-jl45.sh" "${args[@]}"
}

apply_nbi_config() {
  # Prefer env-prefix config so we do not overwrite ~/.jupyter/nbi/config.json.
  local dest="${VENV_DIR}/share/jupyter/nbi"
  mkdir -p "$dest"
  cp "${LOCAL_DEV}/nbi-config.local.json" "${dest}/config.json"
  # Also write under an isolated HOME for any code paths that only read user config.
  mkdir -p "${RUNTIME}/home/.jupyter/nbi"
  cp "${LOCAL_DEV}/nbi-config.local.json" "${RUNTIME}/home/.jupyter/nbi/config.json"
  mkdir -p "${RUNTIME}/jupyter"
  cp "${LOCAL_DEV}/jupyter_server_config.py" "${RUNTIME}/jupyter/jupyter_server_config.py"
  log "Applied NBI config → ${dest}/config.json"
}

start_sidecar() {
  export PORT="$SIDECAR_PORT"
  export MODE="${MODE:-mock}"
  export NBI_LLM_USER="${NBI_LLM_USER:-local-dev}"
  export NBI_LLM_PLAN="${NBI_LLM_PLAN:-local}"
  export QUOTA_TOKENS_DAY="${QUOTA_TOKENS_DAY:-50000}"
  "${LOCAL_DEV}/start-sidecar.sh"
}

run_smoke() {
  export PORT="$SIDECAR_PORT"
  "${LOCAL_DEV}/smoke-test.sh"
}

start_lab() {
  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"

  export JUPYTER_CONFIG_DIR="${RUNTIME}/jupyter"
  # Keep notebooks in repo-local workspace, not the isolated HOME.
  local notebooks="${RUNTIME}/notebooks"
  mkdir -p "$notebooks"

  # Point HOME only for NBI user-config fallback; Jupyter still uses JUPYTER_CONFIG_DIR.
  export HOME="${RUNTIME}/home"
  mkdir -p "$HOME"

  export NBI_CHAT_MODEL_PROVIDER="${NBI_CHAT_MODEL_PROVIDER:-openai-compatible}"
  export NBI_CHAT_MODEL_ID="${NBI_CHAT_MODEL_ID:-openai-compatible-chat-model}"
  export NBI_INLINE_COMPLETION_MODEL_PROVIDER="${NBI_INLINE_COMPLETION_MODEL_PROVIDER:-openai-compatible}"
  export NBI_INLINE_COMPLETION_MODEL_ID="${NBI_INLINE_COMPLETION_MODEL_ID:-openai-compatible-inline-completion-model}"

  log "JupyterLab http://127.0.0.1:${LAB_PORT}/lab"
  log "Sidecar     http://127.0.0.1:${SIDECAR_PORT}/healthz"
  log "Notebooks   ${notebooks}"
  echo "In NBI chat, send a message — replies come from the mock sidecar."
  echo "Stop sidecar later with: ./local-dev/stop-sidecar.sh"
  echo

  local -a lab_args=(
    --port="$LAB_PORT"
    --ServerApp.ip=127.0.0.1
    --ServerApp.root_dir="$notebooks"
    --ServerApp.token=''
    --ServerApp.password=''
    --ServerApp.allow_origin='*'
  )
  if [[ "$OPEN_BROWSER" -eq 0 ]]; then
    lab_args+=(--ServerApp.open_browser=False)
  fi

  # Verify extensions quickly
  jupyter server extension list 2>/dev/null | grep -i notebook_intelligence || true
  jupyter labextension list 2>/dev/null | grep -i notebook-intelligence || true

  exec jupyter lab "${lab_args[@]}"
}

main() {
  if [[ "$SMOKE_ONLY" -eq 1 ]]; then
    start_sidecar
    run_smoke
    exit 0
  fi

  ensure_nbi_env
  apply_nbi_config
  start_sidecar
  run_smoke

  if [[ "$NO_LAB" -eq 1 ]]; then
    log "Stack ready (--no-lab). Activate: source ${VENV_DIR}/bin/activate"
    log "Then: JUPYTER_CONFIG_DIR=${RUNTIME}/jupyter HOME=${RUNTIME}/home jupyter lab --port=${LAB_PORT}"
    exit 0
  fi

  start_lab
}

main "$@"

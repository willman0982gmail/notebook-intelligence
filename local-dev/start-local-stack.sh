#!/usr/bin/env bash
# Minimal local stack: JupyterLab 4.5.9 + Notebook Intelligence + mock LLM sidecar.
# Requires Python >= 3.12 (conda env nbi-jl45 recommended). Never uses system 3.9.
#
# Usage:
#   ./local-dev/start-local-stack.sh
#   ./local-dev/start-local-stack.sh --skip-build
#   ./local-dev/start-local-stack.sh --smoke-only
#   ./local-dev/start-local-stack.sh --no-lab
#   ./local-dev/start-local-stack.sh --conda-env nbi-jl45
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DEV="${REPO_ROOT}/local-dev"
RUNTIME="${LOCAL_DEV}/.runtime"
# shellcheck disable=SC1091
source "${LOCAL_DEV}/python.sh"
export PYTHON="$NBI_PYTHON"

ENV_PREFIX="$("$NBI_PYTHON" -c 'import sys; print(sys.prefix)')"
VENV_DIR="${VENV_DIR:-}"
CONDA_ENV_NAME="${CONDA_ENV_NAME:-}"
LAB_PORT="${JUPYTERLAB_PORT:-8890}"
SIDECAR_PORT="${SIDECAR_PORT:-${PORT:-8089}}"
SKIP_BUILD=0
SMOKE_ONLY=0
NO_LAB=0
OPEN_BROWSER=0

usage() {
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage ;;
    --venv) VENV_DIR="$2"; shift 2 ;;
    --conda-env) CONDA_ENV_NAME="$2"; shift 2 ;;
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
log "Python 3.12+ interpreter: $NBI_PYTHON ($("$NBI_PYTHON" --version 2>&1))"
log "Env prefix: $ENV_PREFIX"

ensure_nbi_env() {
  # If caller forced a venv/conda via flags, prefer the install script.
  if [[ -n "$CONDA_ENV_NAME" ]]; then
    log "Ensuring conda env '$CONDA_ENV_NAME' via build-install-run-jl45.sh"
    local -a args=(--conda-env "$CONDA_ENV_NAME" --no-start --port "$LAB_PORT")
    [[ "$SKIP_BUILD" -eq 1 ]] && args+=(--skip-build)
    "${REPO_ROOT}/scripts/build-install-run-jl45.sh" "${args[@]}"
    # shellcheck disable=SC1091
    eval "$(conda shell.bash hook)"
    conda activate "$CONDA_ENV_NAME"
    ENV_PREFIX="$(python -c 'import sys; print(sys.prefix)')"
    export PYTHON="$(command -v python)"
    export NBI_PYTHON="$PYTHON"
    return
  fi

  if [[ -n "$VENV_DIR" ]]; then
    log "Ensuring venv '$VENV_DIR' (Python 3.12+) via build-install-run-jl45.sh"
    export PYTHON="$NBI_PYTHON"
    local -a args=(--venv "$VENV_DIR" --no-start --port "$LAB_PORT")
    [[ "$SKIP_BUILD" -eq 1 ]] && args+=(--skip-build)
    "${REPO_ROOT}/scripts/build-install-run-jl45.sh" "${args[@]}"
    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
    ENV_PREFIX="$(python -c 'import sys; print(sys.prefix)')"
    export PYTHON="$(command -v python)"
    export NBI_PYTHON="$PYTHON"
    return
  fi

  # Default: use the already-selected 3.12 prefix (e.g. conda nbi-jl45).
  if "$NBI_PYTHON" -c \
      "import jupyterlab, notebook_intelligence as n; assert jupyterlab.__version__.startswith('4.5')" \
      2>/dev/null; then
    log "Reuse existing JL+NBI in $ENV_PREFIX"
    return
  fi

  if [[ "$SKIP_BUILD" -eq 1 ]]; then
    die "JL 4.5 + NBI not importable under $NBI_PYTHON; omit --skip-build to install"
  fi

  # Install into conda nbi-jl45 when that is our interpreter, else .venv-jl45 with 3.12.
  if [[ "$NBI_PYTHON" == *"/envs/nbi-jl45/"* ]]; then
    log "Installing into conda env nbi-jl45"
    "${REPO_ROOT}/scripts/build-install-run-jl45.sh" --conda-env nbi-jl45 --no-start --port "$LAB_PORT"
    # shellcheck disable=SC1091
    eval "$(conda shell.bash hook)"
    conda activate nbi-jl45
    ENV_PREFIX="$(python -c 'import sys; print(sys.prefix)')"
    export PYTHON="$(command -v python)"
    export NBI_PYTHON="$PYTHON"
  else
    VENV_DIR="${REPO_ROOT}/.venv-jl45"
    log "Creating/updating venv $VENV_DIR with $NBI_PYTHON"
    export PYTHON="$NBI_PYTHON"
    "${REPO_ROOT}/scripts/build-install-run-jl45.sh" --venv "$VENV_DIR" --no-start --port "$LAB_PORT"
    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
    ENV_PREFIX="$(python -c 'import sys; print(sys.prefix)')"
    export PYTHON="$(command -v python)"
    export NBI_PYTHON="$PYTHON"
  fi
}

apply_nbi_config() {
  local dest="${ENV_PREFIX}/share/jupyter/nbi"
  mkdir -p "$dest"
  cp "${LOCAL_DEV}/nbi-config.local.json" "${dest}/config.json"
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
  export PYTHON="$NBI_PYTHON"
  "${LOCAL_DEV}/start-sidecar.sh"
}

run_smoke() {
  export PORT="$SIDECAR_PORT"
  export PYTHON="$NBI_PYTHON"
  "${LOCAL_DEV}/smoke-test.sh"
}

start_lab() {
  export PATH="${ENV_PREFIX}/bin:${PATH}"
  export JUPYTER_CONFIG_DIR="${RUNTIME}/jupyter"
  local notebooks="${RUNTIME}/notebooks"
  mkdir -p "$notebooks"
  export HOME="${RUNTIME}/home"
  mkdir -p "$HOME"

  export NBI_CHAT_MODEL_PROVIDER="${NBI_CHAT_MODEL_PROVIDER:-openai-compatible}"
  export NBI_CHAT_MODEL_ID="${NBI_CHAT_MODEL_ID:-openai-compatible-chat-model}"
  export NBI_INLINE_COMPLETION_MODEL_PROVIDER="${NBI_INLINE_COMPLETION_MODEL_PROVIDER:-openai-compatible}"
  export NBI_INLINE_COMPLETION_MODEL_ID="${NBI_INLINE_COMPLETION_MODEL_ID:-openai-compatible-inline-completion-model}"
  export NBI_LLM_SIDECAR_URL="${NBI_LLM_SIDECAR_URL:-http://127.0.0.1:${SIDECAR_PORT}}"

  command -v jupyter >/dev/null || die "jupyter not found on PATH (prefix=$ENV_PREFIX)"

  log "JupyterLab http://127.0.0.1:${LAB_PORT}/lab"
  log "Sidecar     http://127.0.0.1:${SIDECAR_PORT}/healthz"
  log "Python      $("$NBI_PYTHON" --version 2>&1) @ $ENV_PREFIX"
  echo "Stop sidecar later with: ./local-dev/stop-sidecar.sh"

  local -a lab_args=(
    --port="$LAB_PORT"
    --ServerApp.ip=127.0.0.1
    --ServerApp.root_dir="$notebooks"
    --ServerApp.token=''
    --ServerApp.password=''
    --ServerApp.allow_origin='*'
  )
  [[ "$OPEN_BROWSER" -eq 0 ]] && lab_args+=(--ServerApp.open_browser=False)

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
    log "Stack ready (--no-lab). Python: $NBI_PYTHON"
    log "Run lab with PATH=${ENV_PREFIX}/bin:\$PATH"
    exit 0
  fi

  start_lab
}

main "$@"

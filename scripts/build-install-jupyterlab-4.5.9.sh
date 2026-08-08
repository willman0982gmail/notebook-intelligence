#!/usr/bin/env bash
# Build, package, install Notebook Intelligence against JupyterLab 4.5.9,
# then start JupyterLab.
#
# Usage (from anywhere):
#   ./scripts/build-install-run-jl45.sh
#   ./scripts/build-install-run-jl45.sh --port 8890
#   ./scripts/build-install-run-jl45.sh --skip-build          # reuse existing dist/*.whl
#   ./scripts/build-install-run-jl45.sh --no-start            # stop after install
#   ./scripts/build-install-run-jl45.sh --venv .venv-jl45
#   ./scripts/build-install-run-jl45.sh --conda-env nbi-jl45  # use conda instead of venv
#
# Env overrides:
#   PYTHON          — Python interpreter (default: first of python3.12/3.11/3.10)
#   JUPYTERLAB_PORT — same as --port (default: 8890)
#   HUSKY=0         — set automatically to quiet husky postinstall
#   NPM_REGISTRY    — optional jlpm npmRegistryServer URL
#   NODE_EXTRA_CA_CERTS / SSL_CERT_FILE / REQUESTS_CA_BUNDLE — corp TLS

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV_DIR="${VENV_DIR:-.venv-jl45}"
CONDA_ENV_NAME=""
JUPYTERLAB_VERSION="4.5.9"
NOTEBOOK_VERSION="7.5.4"
PORT="${JUPYTERLAB_PORT:-8890}"
SKIP_BUILD=0
NO_START=0
OPEN_BROWSER=0

usage() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage ;;
    --venv) VENV_DIR="$2"; shift 2 ;;
    --conda-env) CONDA_ENV_NAME="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --no-start) NO_START=1; shift ;;
    --browser) OPEN_BROWSER=1; shift ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Run with --help for usage." >&2
      exit 2
      ;;
  esac
done

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

python_is_310_plus() {
  local py="$1"
  "$py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null
}

pick_python() {
  if [[ -n "${PYTHON:-}" ]]; then
    # Accept absolute path or command name
    if [[ -x "$PYTHON" ]]; then
      :
    else
      command -v "$PYTHON" >/dev/null 2>&1 || die "PYTHON=$PYTHON not found"
    fi
    python_is_310_plus "$PYTHON" || die "PYTHON=$PYTHON must be >= 3.10"
    # Prefer documenting 3.12+ for this project's local-dev / Hub spike.
    if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
      echo "WARNING: PYTHON=$PYTHON is < 3.12; local-dev scripts require >= 3.12" >&2
    fi
    echo "$PYTHON"
    return
  fi
  local cand
  # Prefer known conda nbi-jl45 (3.12) before PATH python3.12
  for cand in \
    "/Users/bl44001/miniconda3/envs/nbi-jl45/bin/python3.12" \
    "${HOME}/miniconda3/envs/nbi-jl45/bin/python3.12" \
    python3.12
  do
    if [[ -x "$cand" ]] || command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
        if [[ -x "$cand" ]]; then echo "$cand"; else command -v "$cand"; fi
        return
      fi
    fi
  done
  # Do NOT fall back to system python3 (often 3.9 on macOS).
  if [[ -n "$CONDA_ENV_NAME" ]] || command -v conda >/dev/null 2>&1; then
    echo ""
    return
  fi
  die "Need Python >= 3.12. Use: conda activate nbi-jl45  or  PYTHON=.../python3.12"
}

check_host_tools() {
  command -v node >/dev/null 2>&1 || die "Node.js >= 18 is required (not found on PATH)"
  local node_major
  node_major="$(node -p "process.versions.node.split('.')[0]")"
  [[ "$node_major" -ge 18 ]] || die "Node.js >= 18 required (found $(node --version))"
  log "Using Node: $(node --version)"
}

activate_conda_env() {
  local name="$1"
  command -v conda >/dev/null 2>&1 || die "conda not found (needed for --conda-env $name)"

  # shellcheck disable=SC1091
  eval "$(conda shell.bash hook)"

  if ! conda env list | awk '{print $1}' | grep -qx "$name"; then
    log "Creating conda env '$name' (python=3.12)"
    conda create -y -n "$name" python=3.12
    # cryptography binary avoids source-build hangs on some macOS hosts
    conda install -y -n "$name" -c conda-forge cryptography || true
  else
    log "Reusing conda env '$name'"
  fi
  conda activate "$name"
  log "Using Python: $(command -v python) ($(python --version 2>&1))"
  python -m pip install -U pip setuptools wheel
}

ensure_env() {
  if [[ -n "$CONDA_ENV_NAME" ]]; then
    activate_conda_env "$CONDA_ENV_NAME"
    return
  fi

  local py
  py="$(pick_python)"
  if [[ -z "$py" ]]; then
    # No system Python 3.10+; auto-use conda if available.
    CONDA_ENV_NAME="${CONDA_ENV_NAME:-nbi-jl45}"
    log "No Python 3.10+ on PATH; falling back to conda env '$CONDA_ENV_NAME'"
    activate_conda_env "$CONDA_ENV_NAME"
    return
  fi

  log "Using Python: $py ($("$py" --version 2>&1))"
  if [[ ! -d "$VENV_DIR" ]]; then
    log "Creating venv at $VENV_DIR"
    "$py" -m venv "$VENV_DIR"
  else
    log "Reusing venv at $VENV_DIR"
  fi
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  python -m pip install -U pip setuptools wheel
}

install_build_deps() {
  log "Installing build deps (jupyterlab==${JUPYTERLAB_VERSION}, build)"
  # Pin JL for the build env so jlpm / labextension build match the target.
  pip install --prefer-binary \
    "jupyterlab==${JUPYTERLAB_VERSION}" \
    "notebook==${NOTEBOOK_VERSION}" \
    build

  command -v jlpm >/dev/null 2>&1 || die "jlpm not found after installing jupyterlab"
  jlpm --version
}

build_and_package() {
  export HUSKY=0

  if [[ -n "${NPM_REGISTRY:-}" ]]; then
    log "Setting jlpm npmRegistryServer=$NPM_REGISTRY"
    jlpm config set npmRegistryServer "$NPM_REGISTRY"
  fi

  log "Installing JS dependencies (jlpm install)"
  jlpm install

  log "Cleaning previous frontend build artifacts"
  jlpm clean:all

  log "Building wheel + sdist (python -m build)"
  # hatch-jupyter-builder runs jlpm run build:prod unless skip-if-exists.
  # clean:all above ensures a fresh labextension.
  python -m build

  local whl
  whl="$(ls -1t dist/notebook_intelligence-*-py3-none-any.whl 2>/dev/null | head -n1 || true)"
  [[ -n "$whl" && -f "$whl" ]] || die "No wheel found under dist/ after build"
  log "Built wheel: $whl"
}

install_runtime() {
  local whl
  whl="$(ls -1t dist/notebook_intelligence-*-py3-none-any.whl 2>/dev/null | head -n1 || true)"
  [[ -n "$whl" && -f "$whl" ]] || die "No wheel under dist/. Run without --skip-build first."

  log "Installing JupyterLab ${JUPYTERLAB_VERSION}, notebook ${NOTEBOOK_VERSION}, NBI wheel"
  # Prefer wheels to avoid litellm/Rust source builds on locked-down networks.
  pip install --prefer-binary "litellm>=1.83.7" || true
  pip install --prefer-binary \
    "jupyterlab==${JUPYTERLAB_VERSION}" \
    "notebook==${NOTEBOOK_VERSION}" \
    "$whl"

  # Keep JL/notebook pins if a dependency tried to upgrade them.
  pip install --prefer-binary \
    "jupyterlab==${JUPYTERLAB_VERSION}" \
    "notebook==${NOTEBOOK_VERSION}"

  log "Verifying extensions"
  jupyter server extension list
  jupyter labextension list

  python - <<'PY'
import jupyterlab
import notebook
import notebook_intelligence
print(f"jupyterlab={jupyterlab.__version__}")
print(f"notebook={notebook.__version__}")
print(f"notebook_intelligence={notebook_intelligence.__version__}")
PY
}

start_lab() {
  local url="http://127.0.0.1:${PORT}/lab"
  log "Starting JupyterLab on ${url}"
  echo "Press Ctrl+C to stop."
  local -a args=(
    --port="$PORT"
    --ServerApp.ip=127.0.0.1
    --ServerApp.open_browser=False
  )
  if [[ "$OPEN_BROWSER" -eq 1 ]]; then
    args=(--port="$PORT" --ServerApp.ip=127.0.0.1)
  fi
  exec jupyter lab "${args[@]}"
}

main() {
  check_host_tools
  ensure_env
  install_build_deps

  if [[ "$SKIP_BUILD" -eq 0 ]]; then
    build_and_package
  else
    log "Skipping build (--skip-build); using existing dist/*.whl"
  fi

  install_runtime

  if [[ "$NO_START" -eq 1 ]]; then
    if [[ -n "$CONDA_ENV_NAME" ]]; then
      log "Install complete (--no-start). Activate with: conda activate ${CONDA_ENV_NAME}"
    else
      log "Install complete (--no-start). Activate with: source ${VENV_DIR}/bin/activate"
    fi
    log "Then run: jupyter lab --port=${PORT}"
    exit 0
  fi

  start_lab
}

main "$@"

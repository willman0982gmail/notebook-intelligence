#!/usr/bin/env bash
# Build local Docker images for Notebook Intelligence.  Requires Docker.
#
# Image targets (set IMAGES="a b c" to build a subset):
#   nbi-quota          — quota service image (Dockerfile.quota)
#   nbi-singleuser     — JupyterLab singleuser image w/ NBI + reload endpoint
#                        monkey-patch baked in (Dockerfile.singleuser).
#   nbi-auth-sidecar   — the NEW dedicated AI Factory JWT auth sidecar
#                        (nbi-auth-sidecar/Dockerfile).
#
# Usage:
#   ./local-dev/deploy/build-images.sh                  # build all 3
#   TAG=dev ./local-dev/deploy/build-images.sh          # custom tag
#   IMAGES="nbi-auth-sidecar" ./build-images.sh         # single image only
#   SKIP_PY_COMPILE=1 ./build-images.sh                 # skip py_compile step
#
# PRE-STEPS (fail-fast):
#   For the nbi-auth-sidecar image we run `python3 -m py_compile` against
#   every .py file in the nbi_auth_sidecar package BEFORE invoking docker
#   build.  This catches syntax errors in seconds instead of minutes (the
#   full apt-get install openjdk-17-jre-headless layer takes a while).
#   The nbi-singleuser image also runs a py_compile of jupyter_server_config
#   monkey-patch file.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TAG="${TAG:-local}"
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not found" >&2
  exit 1
fi

IMAGES="${IMAGES:-nbi-quota nbi-singleuser nbi-auth-sidecar}"
SKIP_PY_COMPILE="${SKIP_PY_COMPILE:-0}"

# ---------------------------------------------------------------------------
# Helper: run python3 -m py_compile on a list of files.  Exits 1 immediately
# on any syntax error — py_compile reports the file + line number.
# ---------------------------------------------------------------------------
py_compile_or_die() {
    local desc="$1"; shift
    if [ "${SKIP_PY_COMPILE}" = "1" ]; then
        echo "  py_compile [SKIP] ${desc} (SKIP_PY_COMPILE=1)"
        return 0
    fi
    echo "  py_compile ${desc} ($# files)"
    for f in "$@"; do
        if ! python3 -m py_compile "$f"; then
            echo "ERROR: py_compile FAILED on $f — aborting build BEFORE docker (fix syntax first)" >&2
            exit 1
        fi
    done
    echo "  py_compile OK ${desc}"
}

needs_build() { echo " $IMAGES " | grep -q " $1 "; }

BUILT=()

# ---------------------------------------------------------------------------
# Target 1: nbi-quota
# ---------------------------------------------------------------------------
if needs_build nbi-quota; then
    echo "==> [1/3] building nbi-quota:${TAG}"
    docker build -f local-dev/deploy/Dockerfile.quota -t "nbi-quota:${TAG}" .
    BUILT+=("nbi-quota")
fi

# ---------------------------------------------------------------------------
# Target 2: nbi-singleuser  (Dockerfile.singleuser already includes the
#           jupyter_server_config_nbi_reload.py COPY block from T8 patch)
# ---------------------------------------------------------------------------
if needs_build nbi-singleuser; then
    echo "==> [2/3] building nbi-singleuser:${TAG}"
    py_compile_or_die "jupyter_server_config monkey-patch" \
        local-dev/deploy/jupyter_server_config_nbi_reload.py
    docker build -f local-dev/deploy/Dockerfile.singleuser -t "nbi-singleuser:${TAG}" .
    BUILT+=("nbi-singleuser")
fi

# ---------------------------------------------------------------------------
# Target 3: nbi-auth-sidecar (NEW)
#   PRE-CHECK: py_compile every .py in nbi_auth_sidecar package.
#   docker build context: local-dev/deploy/nbi-auth-sidecar/
# ---------------------------------------------------------------------------
if needs_build nbi-auth-sidecar; then
    echo "==> [3/3] building nbi-auth-sidecar:${TAG}"
    SIDECAR_DIR="local-dev/deploy/nbi-auth-sidecar"
    SIDECAR_PKG="${SIDECAR_DIR}/nbi_auth_sidecar"
    # List every .py file in the package deterministically (sorted, no globs)
    # — shell nullglob prevents a literal '*.py' if package is empty.
    mapfile -t SIDECAR_PYS < <(find "${SIDECAR_PKG}" -name '*.py' -type f | sort)
    if [ "${#SIDECAR_PYS[@]}" -eq 0 ]; then
        echo "ERROR: no .py files found under ${SIDECAR_PKG}" >&2
        exit 1
    fi
    py_compile_or_die "nbi_auth_sidecar package (${#SIDECAR_PYS[@]} modules)" \
        "${SIDECAR_PYS[@]}"
    # Build with the sidecar dir as context (not the repo root) so
    # .dockerignore applies cleanly and context upload is tiny (< 1 MB).
    docker build -f "${SIDECAR_DIR}/Dockerfile" \
        -t "nbi-auth-sidecar:${TAG}" \
        "${SIDECAR_DIR}"
    BUILT+=("nbi-auth-sidecar")
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "OK: built ${#BUILT[@]} image(s) with TAG=${TAG}"
if [ "${#BUILT[@]}" -gt 0 ]; then
    IMG_GLOB=$(printf '%s|' "${BUILT[@]}")
    IMG_GLOB="REPOSITORY|${IMG_GLOB%|}"
    docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}' \
        | grep -E "${IMG_GLOB}" || true
fi
echo
echo "Next (when cluster is up):"
echo "  ./local-dev/deploy/apply-manifests.sh --apply"
echo "  # Install/upgrade Z2JK with nbi-auth-sidecar values overlay:"
echo "  helm upgrade -i jhub jupyterhub/jupyterhub --version 3.3.0 \\"
echo "       -f charts/nbi-auth-sidecar/values.yaml -f my-site-values.yaml"
echo "  # run sidecar smoke test locally first:"
echo "  ./local-dev/deploy/nbi-auth-sidecar/smoke-test.sh nbi-auth-sidecar:${TAG}"

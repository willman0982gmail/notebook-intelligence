#!/usr/bin/env bash
# Resolve Python >= 3.12 for local-dev (never fall back to system 3.9).
# Prefer: PYTHON → conda nbi-jl45 → python3.12 on PATH → .venv-jl45
set -euo pipefail

_local_dev_pick_python() {
  local cand ver

  if [[ -n "${PYTHON:-}" ]]; then
    ver="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    if [[ "$ver" != 3.12 && "$ver" != 3.13 && "$ver" != 3.14 ]]; then
      # Allow 3.12+ only
      if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
        echo "ERROR: PYTHON=$PYTHON must be Python >= 3.12 (got ${ver:-unknown})" >&2
        return 1
      fi
    fi
    echo "$PYTHON"
    return 0
  fi

  for cand in \
    "/Users/bl44001/miniconda3/envs/nbi-jl45/bin/python3.12" \
    "${HOME}/miniconda3/envs/nbi-jl45/bin/python3.12" \
    "${HOME}/anaconda3/envs/nbi-jl45/bin/python3.12" \
    "${CONDA_PREFIX:-/nonexistent}/bin/python3.12"
  do
    if [[ -x "$cand" ]] && "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
      echo "$cand"
      return 0
    fi
  done

  if command -v python3.12 >/dev/null 2>&1 && python3.12 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    command -v python3.12
    return 0
  fi

  local repo_root
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [[ -x "${repo_root}/.venv-jl45/bin/python" ]] \
    && "${repo_root}/.venv-jl45/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    echo "${repo_root}/.venv-jl45/bin/python"
    return 0
  fi

  echo "ERROR: Need Python >= 3.12 (system python3/3.9 is not allowed)." >&2
  echo "  conda create -n nbi-jl45 python=3.12" >&2
  echo "  conda activate nbi-jl45" >&2
  echo "  Or: PYTHON=/path/to/python3.12 ./local-dev/run-regression.sh" >&2
  return 1
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  _local_dev_pick_python
else
  NBI_PYTHON="$(_local_dev_pick_python)"
  export NBI_PYTHON
  export PYTHON="${PYTHON:-$NBI_PYTHON}"
fi

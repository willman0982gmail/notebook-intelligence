#!/usr/bin/env bash
# Apply baked NBI config into a Python prefix (LLM-S07.1).
set -euo pipefail
LOCAL_DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${1:-${CONDA_PREFIX:-${VIRTUAL_ENV:-}}}"
[[ -n "$PREFIX" ]] || { echo "Usage: $0 <python-prefix>"; exit 2; }

dest="${PREFIX}/share/jupyter/nbi"
mkdir -p "$dest" "${PREFIX}/etc/jupyter"
cp "${LOCAL_DEV}/nbi-config.local.json" "${dest}/config.json"
cp "${LOCAL_DEV}/jupyter_server_config.py" "${PREFIX}/etc/jupyter/jupyter_server_config.py"

echo "Baked NBI config → ${dest}/config.json"
echo "Server config    → ${PREFIX}/etc/jupyter/jupyter_server_config.py"
echo "Env locks (set in Hub/spawner):"
echo "  NBI_CHAT_MODEL_PROVIDER=openai-compatible"
echo "  NBI_CHAT_MODEL_ID=openai-compatible-chat-model"
echo "  NBI_INLINE_COMPLETION_MODEL_PROVIDER=openai-compatible"
echo "  NBI_INLINE_COMPLETION_MODEL_ID=openai-compatible-inline-completion-model"

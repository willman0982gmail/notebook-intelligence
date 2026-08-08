#!/usr/bin/env bash
# Drop-in JAVA_BIN for JarTokenProvider local tests.
# Skips JVM -D flags and runs the script after -jar.
set -euo pipefail
args=("$@")
jar=""
for ((i=0; i<${#args[@]}; i++)); do
  if [[ "${args[$i]}" == "-jar" ]]; then
    jar="${args[$((i+1))]}"
    break
  fi
done
[[ -n "$jar" ]] || { echo '{"error":"no -jar"}' >&2; exit 2; }
# Prefer PYTHON / NBI_PYTHON (3.12+); never system python3.9.
PY="${PYTHON:-${NBI_PYTHON:-}}"
if [[ -z "$PY" ]]; then
  LOCAL_DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  # shellcheck disable=SC1091
  source "${LOCAL_DEV}/python.sh"
  PY="$NBI_PYTHON"
fi
exec "$PY" "$jar"

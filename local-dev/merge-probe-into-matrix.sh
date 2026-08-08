#!/usr/bin/env bash
# Merge probe-results.json into docs/feature-matrix.md Corp (or Local) column (LLM-S23).
# Usage:
#   ./local-dev/merge-probe-into-matrix.sh
#   PROBE_RESULTS=local-dev/.runtime/probe-results.json ./local-dev/merge-probe-into-matrix.sh
set -euo pipefail
LOCAL_DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${LOCAL_DEV}/python.sh"
RESULTS="${PROBE_RESULTS:-${LOCAL_DEV}/.runtime/probe-results.json}"
MATRIX="${LOCAL_DEV}/docs/feature-matrix.md"

"$NBI_PYTHON" - "$RESULTS" "$MATRIX" <<'PY'
import json, re, sys
from pathlib import Path

results_path, matrix_path = Path(sys.argv[1]), Path(sys.argv[2])
if not results_path.is_file():
    raise SystemExit(f"missing {results_path}; run probe-gateway.sh first")
r = json.loads(results_path.read_text(encoding="utf-8"))
caps = r.get("capabilities") or {}
label = r.get("label") or "corp"
col = "Local mock" if "local" in label else "Corp gateway"

def cell(key, default="TBD"):
    c = caps.get(key) or {}
    return c.get("status") or default

mapping = {
    "Chat non-stream": cell("chat_non_stream"),
    "Chat SSE stream": cell("chat_sse_stream"),
    "Inline completion (chat-completions FIM prompt)": cell("inline_fim_style"),
    "Tool calling": cell("tool_calling"),
    "`usage` on final stream chunk": cell("usage_field"),
}

text = matrix_path.read_text(encoding="utf-8")
lines = text.splitlines()
out = []
for line in lines:
    if not line.startswith("| ") or line.startswith("| ---") or line.startswith("| Capability"):
        out.append(line)
        continue
    parts = [p.strip() for p in line.strip("|").split("|")]
    if len(parts) < 3:
        out.append(line)
        continue
    cap = parts[0]
    if cap in mapping:
        if col.startswith("Local"):
            parts[1] = mapping[cap]
        else:
            parts[2] = mapping[cap]
        # rebuild
        notes = parts[3] if len(parts) > 3 else ""
        out.append(f"| {parts[0]} | {parts[1]} | {parts[2]} | {notes} |")
    else:
        out.append(line)

# Append provenance footer
footer = (
    f"\n\n<!-- probe merge: label={label} probed_at={r.get('probed_at')} "
    f"base={r.get('base_url')} -->\n"
)
body = "\n".join(out)
body = re.sub(r"\n<!-- probe merge:.*?-->\n?", "\n", body)
matrix_path.write_text(body.rstrip() + footer, encoding="utf-8")
print(f"Updated {matrix_path} column={col!r} from {results_path}")
for k, v in mapping.items():
    print(f"  {k}: {v}")
PY

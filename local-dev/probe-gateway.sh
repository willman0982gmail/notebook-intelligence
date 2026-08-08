#!/usr/bin/env bash
# Probe an OpenAI-compatible gateway for the feature matrix (LLM-S23).
# Usage:
#   UPSTREAM_BASE_URL=https://gateway.example.com/v1 UPSTREAM_API_KEY=... ./local-dev/probe-gateway.sh
#   # Against local mock sidecar:
#   UPSTREAM_BASE_URL=http://127.0.0.1:8089/v1 UPSTREAM_API_KEY=unused ./local-dev/probe-gateway.sh
#
# Writes machine-readable results to:
#   local-dev/.runtime/probe-results.json  (and optional PROBE_OUT)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="${ROOT}/local-dev/.runtime"
mkdir -p "$RUNTIME"

BASE="${UPSTREAM_BASE_URL:?set UPSTREAM_BASE_URL}"
KEY="${UPSTREAM_API_KEY:-}"
MODEL="${MODEL_ID:-databricks/gdp-gpt4o}"
VERIFY="${UPSTREAM_VERIFY_TLS:-1}"
OUT="${PROBE_OUT:-${RUNTIME}/probe-results.json}"
LABEL="${PROBE_LABEL:-corp}"

CURL_OPTS=(-sS -H "Content-Type: application/json")
[[ -n "$KEY" ]] && CURL_OPTS+=(-H "Authorization: Bearer ${KEY}")
[[ "$VERIFY" == "0" ]] && CURL_OPTS+=(-k)

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

probe_one() {
  local name="$1" stream="$2" extra_json="${3:-}"
  local body code file
  file="${tmpdir}/${name}.out"
  body="{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"stream\":${stream}"
  if [[ -n "$extra_json" ]]; then
    body="${body},${extra_json}"
  fi
  body="${body}}"
  code=$(curl "${CURL_OPTS[@]}" -o "$file" -w '%{http_code}' \
    "${BASE}/chat/completions" -d "$body" || echo "000")
  echo "$code"
}

echo "== non-stream =="
code_ns=$(probe_one nonstream false)
echo "HTTP $code_ns"
head -c 400 "${tmpdir}/nonstream.out" 2>/dev/null; echo

echo "== stream =="
code_st=$(curl "${CURL_OPTS[@]}" -N -o "${tmpdir}/stream.out" -w '%{http_code}' \
  "${BASE}/chat/completions" \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"stream\":true}" || echo "000")
echo "HTTP $code_st"
head -c 400 "${tmpdir}/stream.out" 2>/dev/null; echo

echo "== tools (may fail) =="
code_tools=$(probe_one tools false \
  '"tools":[{"type":"function","function":{"name":"echo","parameters":{"type":"object","properties":{}}}}]')
echo "HTTP $code_tools"
head -c 400 "${tmpdir}/tools.out" 2>/dev/null; echo

echo "== inline / FIM-style prompt (LLM-S09) =="
code_fim=$(curl "${CURL_OPTS[@]}" -o "${tmpdir}/fim.out" -w '%{http_code}' \
  "${BASE}/chat/completions" \
  -H 'X-NBI-Feature: inline' \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"def add(a,b):\\n    # complete\\n    <|fim_suffix|>\\n    return a+b\\n<|fim_prefix|>\"}],\"stream\":false}" || echo "000")
echo "HTTP $code_fim"
head -c 400 "${tmpdir}/fim.out" 2>/dev/null; echo

# Detect SSE / usage / real tool_calls (HTTP 200 alone is not enough for tools)
has_sse=0
has_usage=0
has_tool_calls=0
if grep -q '^data:' "${tmpdir}/stream.out" 2>/dev/null; then has_sse=1; fi
if grep -qi '"usage"' "${tmpdir}/stream.out" "${tmpdir}/nonstream.out" 2>/dev/null; then has_usage=1; fi
if grep -Eqi '"tool_calls"|"function_call"' "${tmpdir}/tools.out" 2>/dev/null; then has_tool_calls=1; fi

# shellcheck disable=SC1091
source "${ROOT}/local-dev/python.sh"
"$NBI_PYTHON" - "$OUT" "$LABEL" "$BASE" "$MODEL" "$code_ns" "$code_st" "$code_tools" "$code_fim" "$has_sse" "$has_usage" "$has_tool_calls" <<'PY'
import json, sys, time
from pathlib import Path

(
    out, label, base, model, code_ns, code_st, code_tools, code_fim,
    has_sse, has_usage, has_tool_calls,
) = sys.argv[1:]

def status(code: str) -> str:
    try:
        c = int(code)
    except ValueError:
        return "Error"
    if 200 <= c < 300:
        return "Supported"
    if c in (400, 404, 405, 501):
        return "Unsupported/likely"
    if c == 401 or c == 403:
        return "Auth required"
    if c == 0 or c >= 500:
        return "Error"
    return f"HTTP {c}"

def tools_status(code: str, saw_calls: bool) -> str:
    try:
        c = int(code)
    except ValueError:
        return "Error"
    if 200 <= c < 300 and saw_calls:
        return "Supported"
    if 200 <= c < 300 and not saw_calls:
        return "Accepted but no tool_calls (treat as Off/unsupported)"
    return status(code)

result = {
    "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "label": label,
    "base_url": base,
    "model": model,
    "capabilities": {
        "chat_non_stream": {"http": int(code_ns or 0), "status": status(code_ns)},
        "chat_sse_stream": {
            "http": int(code_st or 0),
            "status": status(code_st),
            "sse_data_lines": bool(int(has_sse)),
        },
        "tool_calling": {
            "http": int(code_tools or 0),
            "tool_calls_observed": bool(int(has_tool_calls)),
            "status": tools_status(code_tools, bool(int(has_tool_calls))),
        },
        "inline_fim_style": {
            "http": int(code_fim or 0),
            "status": status(code_fim),
        },
        "usage_field": {
            "observed": bool(int(has_usage)),
            "status": "Supported" if int(has_usage) else "TBD/not observed",
        },
    },
}
Path(out).parent.mkdir(parents=True, exist_ok=True)
Path(out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, indent=2))
print(f"Wrote {out}")
print("Merge into local-dev/docs/feature-matrix.md (Corp gateway column).")
PY

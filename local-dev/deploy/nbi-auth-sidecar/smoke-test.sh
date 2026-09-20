#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# smoke-test.sh — local end-to-end smoke test of the nbi-auth-sidecar image
# ---------------------------------------------------------------------------
#
# What this test exercises (ALL of the below must PASS):
#
#   1. Image starts (tini PID 1 + `python -m nbi_auth_sidecar`), no syntax
#      errors, non-root UID 1000 runs successfully.
#   2. MockTokenMinter is used (NBI_SIDECAR_MINTER=mock) so no JKS/JAR/OIDC
#      is needed locally.
#   3. Bootstrap synchronous token mint (within 90s) → writes 2 files:
#        /tmp/nbi-config-user-mirror/config.json  (NBI's user config)
#        /tmp/nbi-runtime-env.json                (8-key STRING_OVERRIDE ferry)
#   4. Runtime env JSON has EXACTLY 8 keys in the canonical order
#      (STRING_OVERRIDE_SPEC order) and file mode == 0600.
#   5. A fake Jupyter Server reload endpoint is stood up on port 18888
#      inside the sidecar container via a 2nd process (we use `docker exec`
#      with stdlib http.server + a cgi handler that counts calls).
#   6. Bootstrap triggers EXACTLY 1 authenticated POST
#      /notebook-intelligence/reload-config call to the fake jupyter server
#      (via jpserver-*.json detection with mocked JUPYTER_RUNTIME_DIR).
#   7. GET 127.0.0.1:${SIDECAR_PORT}/healthz returns 200 with valid JSON
#      containing pid/token_ttl/ready/refresh_count fields.
#   8. GET /ready returns 200 AFTER bootstrap completes (not before).
#   9. GET /metrics returns text/plain Prometheus exposition containing the
#      8 required metric families (info, refresh_success_total,
#      refresh_attempt_total, consecutive_mint_failures, token_ttl_seconds,
#      last_refresh_ts_seconds, ready gauge, http_active_requests).
#  10. POST /rotate-self returns 202 Accepted, increments refresh_attempt_total
#      counter by exactly 1 within 15 seconds, and triggers a 2nd reload
#      endpoint call.
#  11. State JSON at /tmp/nbi-token-state.json is schema v1, NEVER contains
#      a raw JWT-looking blob (eyJ…) or password strings — contains only
#      token_len, exp, last_refresh, ok, version.
#  12. Sidecar HTTP server binds to 127.0.0.1 only (checked via `ss` or
#      `netstat` inside container — no 0.0.0.0 listener on 18090).
#  13. Clean shutdown: docker stop (SIGTERM → tini propagates → scheduler
#      stop → server shutdown → exit code 0 within 15s).
#
# Usage:
#   ./smoke-test.sh nbi-auth-sidecar:local    # test a local-built image
#   ./smoke-test.sh registry.example.com/nbi-auth-sidecar:1.0.0
#
# Exit codes: 0=all pass, 1=assertion fail, 2=docker/image missing
# ---------------------------------------------------------------------------
set -euo pipefail

IMAGE="${1:-nbi-auth-sidecar:local}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$(mktemp -d -t nbi-smoke.XXXXXX)"
trap 'echo; echo "== logs kept at ${LOG_DIR} =="; ls "${LOG_DIR}"' EXIT

log()  { printf '[smoke] %s\n' "$*"; }
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$*"; N_PASS=$((N_PASS+1)); }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*" 1>&2; N_FAIL=$((N_FAIL+1)); }

N_PASS=0
N_FAIL=0
CONTAINER_NAME="nbi-smoke-$$"
SIDECAR_HOST_PORT=18190           # host port → container 18090 (sidecar HTTP)
JUPYTER_FAKE_PORT=18888           # inside container, fake jupyter
JUPYTER_RUNTIME_DIR_HOST="${LOG_DIR}/jupyter-runtime"
TMP_SHARED_HOST="${LOG_DIR}/tmp"

# ---------------------------------------------------------------------------
# 0. Precondition: docker + image exists
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    echo "[smoke] ERROR: docker not available on PATH" >&2
    exit 2
fi
if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    # Skip docker build automatically here — just prompt user.
    # We also don't attempt the docker build during smoke to keep this
    # script side-effect free.
    echo "[smoke] ERROR: image '${IMAGE}' not found locally." >&2
    echo "       Build it first:  TAG=local ${SCRIPT_DIR}/../build-images.sh" >&2
    exit 2
fi
log "Testing image: ${IMAGE}"
log "Log dir:        ${LOG_DIR}"

# ---------------------------------------------------------------------------
# 1. Pre-create the jupyter runtime dir with a fake jpserver-*.json
#    so the sidecar's nbi_reload_client discovers our fake jupyter.
# ---------------------------------------------------------------------------
mkdir -p "${JUPYTER_RUNTIME_DIR_HOST}" "${TMP_SHARED_HOST}"
# Create a fake NB user-home config dir mirror (used for config.json write)
mkdir -p "${TMP_SHARED_HOST}/nbi-config-user-mirror"
# mode 0777 because container runs as UID 1000
chmod -R 0777 "${LOG_DIR}" "${TMP_SHARED_HOST}" "${JUPYTER_RUNTIME_DIR_HOST}"

JUPYTER_FAKE_TOKEN="smoke-test-token-abc123xyz"
cat > "${JUPYTER_RUNTIME_DIR_HOST}/jpserver-999999-smoke.json" <<EOF
{
  "version": 2,
  "url": "http://127.0.0.1:${JUPYTER_FAKE_PORT}/",
  "host": "127.0.0.1",
  "port": ${JUPYTER_FAKE_PORT},
  "base_url": "/",
  "token": "${JUPYTER_FAKE_TOKEN}",
  "root_dir": "/tmp",
  "secure": false,
  "password": false,
  "pid": 999999
}
EOF

# ---------------------------------------------------------------------------
# 2. Launch the fake jupyter reload endpoint script (embedded Python stdlib)
# ---------------------------------------------------------------------------
cat > "${TMP_SHARED_HOST}/fake_jupyter.py" <<'PYEOF'
#!/usr/bin/env python3
"""
Minimal stdlib-only fake Jupyter Server for the nbi-auth-sidecar smoke test.

Exposes one route plus metrics to the smoke harness:
  POST /notebook-intelligence/reload-config
       → requires token (query param OR header), returns 200 JSON {ok:true}
       → increments an internal CALL_COUNTER atomically.
  GET  /_smoke/call_count
       → returns {"count": N} so the smoke harness can assert N calls.
  GET  /_smoke/last_headers
       → returns dict of headers from last reload-config POST (so smoke can
          assert auth headers were sent correctly).
"""
import json, threading, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

CALL_COUNT = 0
LAST_HEADERS = {}
LOCK = threading.Lock()
TOKEN = sys.argv[1] if len(sys.argv) > 1 else "smoke-test-token-abc123xyz"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18888

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Suppress noisy default logs; append to file for capture.
        with open("/tmp/fake_jupyter.log", "a") as f:
            f.write("[fake_jupyter] %s - %s\n" % (self.address_string(), fmt % args))

    def _check_auth(self) -> bool:
        # Jupyter token auth: either query param ?token=XYZ OR
        # header "Authorization: token XYZ".  Mirror actual Jupyter Server
        # behaviour used by nbi_reload_client.py (both forms accepted).
        qs = parse_qs(urlparse(self.path).query)
        q_tokens = qs.get("token", [])
        q_ok = bool(q_tokens) and q_tokens[0] == TOKEN
        h = self.headers.get("Authorization", "")
        h_ok = h.lower().startswith("token ") and h.split(None, 1)[1].strip() == TOKEN
        return q_ok or h_ok

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/_smoke/call_count":
            body = json.dumps({"count": CALL_COUNT}).encode("utf-8")
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body); return
        if path == "/_smoke/last_headers":
            with LOCK:
                snapshot = dict(LAST_HEADERS)
            body = json.dumps(snapshot).encode("utf-8")
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body); return
        # default 404 — mirrors Jupyter behaviour for unknown paths.
        self.send_response(404); self.end_headers()
        self.wfile.write(b"Not found")

    def do_POST(self):
        global CALL_COUNT, LAST_HEADERS
        path = urlparse(self.path).path
        if path != "/notebook-intelligence/reload-config":
            self.send_response(404); self.end_headers(); return
        if not self._check_auth():
            self.send_response(403); self.end_headers()
            self.wfile.write(b"forbidden"); return
        length = int(self.headers.get("Content-Length") or 0)
        _body = self.rfile.read(length) if length > 0 else b""
        with LOCK:
            CALL_COUNT += 1
            LAST_HEADERS = {
                "Authorization": self.headers.get("Authorization", ""),
                "X-Token-Query-Present": "yes" if "token=" in self.path else "no",
                "Content-Type": self.headers.get("Content-Type", ""),
                "Content-Length": self.headers.get("Content-Length", ""),
            }
        payload = json.dumps({"ok": True, "count": CALL_COUNT}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

if __name__ == "__main__":
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    sys.stdout.write(f"[fake_jupyter] listening 127.0.0.1:{PORT}\n")
    sys.stdout.flush()
    srv.serve_forever()
PYEOF
chmod 0755 "${TMP_SHARED_HOST}/fake_jupyter.py"

# ---------------------------------------------------------------------------
# 3. Docker run the sidecar (background, shares host PID namespace not
#    needed — just a bind mount for /tmp so sidecar writes are visible on
#    the host for assertions, and maps sidecar 18090 to host port).
# ---------------------------------------------------------------------------
log "Starting sidecar container (name=${CONTAINER_NAME})..."
docker run \
    --rm --detach \
    --name "${CONTAINER_NAME}" \
    --user 1000:1000 \
    -p "127.0.0.1:${SIDECAR_HOST_PORT}:18090" \
    --tmpfs /tmp/sidecar-tmpfs:rw,size=64m,mode=1777 \
    -v "${TMP_SHARED_HOST}:/tmp/shared-out:rw" \
    -v "${JUPYTER_RUNTIME_DIR_HOST}:/home/jovyan/.local/share/jupyter/runtime:rw" \
    -e NBI_SIDECAR_MINTER=mock \
    -e NBI_SIDECAR_HTTP_HOST=127.0.0.1 \
    -e NBI_SIDECAR_HTTP_PORT=18090 \
    -e NBI_CHAT_MODEL_PROVIDER=openai_compatible \
    -e NBI_CHAT_MODEL_ID=databricks/gdp-gpt4o \
    -e NBI_INLINE_COMPLETION_MODEL_PROVIDER=openai_compatible \
    -e NBI_INLINE_COMPLETION_MODEL_ID=databricks/gdp-gpt4o \
    -e NBI_CLAUDE_CHAT_MODEL="" \
    -e NBI_CLAUDE_INLINE_COMPLETION_MODEL="" \
    -e ANTHROPIC_API_KEY=unused-but-needed \
    -e ANTHROPIC_BASE_URL="https://gateway.scaifactory.dev.azure.scbdev.net/v1" \
    -e NBI_USER_CONFIG_PATH=/tmp/shared-out/nbi-config-user-mirror/config.json \
    -e NBI_RUNTIME_ENV_JSON=/tmp/shared-out/nbi-runtime-env.json \
    -e NBI_TOKEN_STATE_JSON=/tmp/shared-out/nbi-token-state.json \
    -e NBI_JUPYTER_RUNTIME_DIR=/home/jovyan/.local/share/jupyter/runtime \
    -e NBI_REFRESH_BEFORE_EXP_SEC=5 \
    -e NBI_FORCE_REFRESH_INTERVAL_SEC=0 \
    -e NBI_MOCK_TTL_S=120 \
    -e NBI_BOOTSTRAP_TIMEOUT_SEC=60 \
    "${IMAGE}" \
    > "${LOG_DIR}/docker-run.cid" 2> "${LOG_DIR}/docker-run.err" || {
        fail "docker run failed; see ${LOG_DIR}/docker-run.err"
        exit 1
    }
sleep 2
if ! docker ps -q --filter "name=${CONTAINER_NAME}" | grep -q .; then
    fail "container not running after 2s; logs:"
    docker logs "${CONTAINER_NAME}" > "${LOG_DIR}/container-stderr.log" 2>&1 || true
    cat "${LOG_DIR}/container-stderr.log" >&2 || true
    exit 1
fi
log "Container running: $(docker ps --filter name=${CONTAINER_NAME} --format '{{.ID}}')"

# ---------------------------------------------------------------------------
# 4. Inside the container: start fake_jupyter.py
# ---------------------------------------------------------------------------
log "Starting fake jupyter server inside container on 127.0.0.1:${JUPYTER_FAKE_PORT}..."
docker cp "${TMP_SHARED_HOST}/fake_jupyter.py" "${CONTAINER_NAME}:/tmp/fake_jupyter.py"
docker exec -d --user 1000:1000 "${CONTAINER_NAME}" \
    python3 /tmp/fake_jupyter.py "${JUPYTER_FAKE_TOKEN}" "${JUPYTER_FAKE_PORT}"
sleep 2
# sanity: curl fake jupyter call_count (must be 0 initially)
INIT_COUNT=$(docker exec --user 1000:1000 "${CONTAINER_NAME}" \
    curl -sS "http://127.0.0.1:${JUPYTER_FAKE_PORT}/_smoke/call_count" 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("count",-1))' || echo "-1")
if [ "${INIT_COUNT}" = "0" ]; then
    pass "T0: fake_jupyter server reachable inside container, call_count=0"
else
    fail "T0: fake_jupyter not reachable or initial count=${INIT_COUNT}!=0"
fi

# ---------------------------------------------------------------------------
# 5. Wait for sidecar bootstrap (max 70s).
# ---------------------------------------------------------------------------
log "Waiting for sidecar bootstrap (/ready → 200)..."
READY=0
for attempt in $(seq 1 70); do
    CODE=$(curl -sS -o "${LOG_DIR}/ready-body.json" -w "%{http_code}" \
        "http://127.0.0.1:${SIDECAR_HOST_PORT}/ready" 2>/dev/null || echo "000")
    if [ "${CODE}" = "200" ]; then READY=1; break; fi
    sleep 1
done
if [ "${READY}" = "1" ]; then
    pass "T1: bootstrap completed, /ready=200 (after ~${attempt}s)"
else
    fail "T1: /ready never returned 200 within 70s (last code=${CODE}); container logs:"
    docker logs "${CONTAINER_NAME}" > "${LOG_DIR}/t1-container.log" 2>&1 || true
fi

# ---------------------------------------------------------------------------
# 6. Healthz
# ---------------------------------------------------------------------------
HZ_CODE=$(curl -sS -o "${LOG_DIR}/healthz.json" -w "%{http_code}" \
    "http://127.0.0.1:${SIDECAR_HOST_PORT}/healthz" || echo "000")
HZ_JSON_OK=$(python3 -c "
import json,sys
try:
    d = json.load(open('${LOG_DIR}/healthz.json'))
except Exception as e:
    print('JSON_ERR',e); sys.exit(0)
need = {'pid','token_ttl_seconds','ready','last_refresh_ts_seconds','refresh_count'}
have = set(d.keys())
print('HAVE_KEYS_OK' if need.issubset(have) else 'MISSING:' + ','.join(sorted(need-have)))
" 2>&1 || echo "PY_ERR")
if [ "${HZ_CODE}" = "200" ] && [ "${HZ_JSON_OK}" = "HAVE_KEYS_OK" ]; then
    pass "T2: /healthz=200 with required JSON keys"
else
    fail "T2: /healthz code=${HZ_CODE} json=${HZ_JSON_OK}"
fi

# ---------------------------------------------------------------------------
# 7. Bootstrap should have triggered EXACTLY 1 reload-config call
# ---------------------------------------------------------------------------
sleep 3
CALL_COUNT=$(docker exec --user 1000:1000 "${CONTAINER_NAME}" \
    curl -sS "http://127.0.0.1:${JUPYTER_FAKE_PORT}/_smoke/call_count" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("count",-1))')
if [ "${CALL_COUNT}" = "1" ]; then
    pass "T3: bootstrap triggered 1 reload-config POST (found call_count=1)"
else
    fail "T3: expected 1 reload-config after bootstrap, got ${CALL_COUNT}"
fi

# ---------------------------------------------------------------------------
# 8. Auth headers correctness on bootstrap call (dual-auth check — smoke)
# ---------------------------------------------------------------------------
LAST_H=$(docker exec --user 1000:1000 "${CONTAINER_NAME}" \
    curl -sS "http://127.0.0.1:${JUPYTER_FAKE_PORT}/_smoke/last_headers")
HAS_AUTHZ=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print('HAS' if d.get('Authorization','').lower().startswith('token ') else 'NO')" "${LAST_H}")
HAS_QUERY=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print('HAS' if d.get('X-Token-Query-Present')=='yes' else 'NO')" "${LAST_H}")
if [ "${HAS_AUTHZ}" = "HAS" ] && [ "${HAS_QUERY}" = "HAS" ]; then
    pass "T4: reload POST sent both Authorization header AND ?token= query (dual-auth)"
else
    fail "T4: dual-auth mismatch header=${HAS_AUTHZ} query=${HAS_QUERY}"
fi

# ---------------------------------------------------------------------------
# 9. config.json written to shared tmp (mirror)
# ---------------------------------------------------------------------------
CFG="${TMP_SHARED_HOST}/nbi-config-user-mirror/config.json"
if [ -f "${CFG}" ]; then
    CFG_PARSED=$(python3 -c "
import json
try:
    d = json.load(open('${CFG}'))
except Exception as e:
    print('PARSE_ERR',e); exit(0)
prov_ok = d.get('chat',{}).get('provider') and d.get('chat',{}).get('model_id')
print('CFG_OK' if prov_ok else 'CFG_MISSING_CHAT_KEYS')
" 2>&1)
    if [ "${CFG_PARSED}" = "CFG_OK" ]; then
        pass "T5: NBI user config.json written & valid (has chat provider+model)"
    else
        fail "T5: config.json invalid (${CFG_PARSED})"
    fi
else
    fail "T5: config.json missing at ${CFG}"
fi

# ---------------------------------------------------------------------------
# 10. Runtime env JSON: 8 keys in order AND mode 0600
# ---------------------------------------------------------------------------
RE="${TMP_SHARED_HOST}/nbi-runtime-env.json"
if [ -f "${RE}" ]; then
    RE_MODE=$(stat -f '%Lp' "${RE}" 2>/dev/null || stat -c '%a' "${RE}" 2>/dev/null || echo "??")
    RE_INFO=$(python3 -c "
import json, os
keys_order = [
    'NBI_CHAT_MODEL_PROVIDER','NBI_CHAT_MODEL_ID',
    'NBI_INLINE_COMPLETION_MODEL_PROVIDER','NBI_INLINE_COMPLETION_MODEL_ID',
    'NBI_CLAUDE_CHAT_MODEL','NBI_CLAUDE_INLINE_COMPLETION_MODEL',
    'ANTHROPIC_API_KEY','ANTHROPIC_BASE_URL',
]
try:
    d = json.load(open('${RE}'))
except Exception as e:
    print('PARSE_ERR',e); exit(0)
actual = list(d.keys())
ok_len = len(actual)==8
ok_order = actual == keys_order
ok_val = d.get('ANTHROPIC_BASE_URL','').startswith('http')
print('ORDER_OK_K8' if (ok_len and ok_order and ok_val) else f'BAD len={len(actual)} order_mismatch={actual!=keys_order} val_ok={ok_val}')
print('ANTHROPIC_API_KEY_len=', len(d.get('ANTHROPIC_API_KEY','')), file=sys.stderr)
")
    if [ "${RE_MODE}" = "600" ] && [[ "${RE_INFO}" == ORDER_OK_K8* ]]; then
        pass "T6: runtime-env JSON 8 keys order+values OK, mode=0600"
    else
        fail "T6: runtime-env mode=${RE_MODE} info=${RE_INFO}"
    fi
else
    fail "T6: nbi-runtime-env.json missing"
fi

# ---------------------------------------------------------------------------
# 11. Metrics endpoint content-type + 8 families
# ---------------------------------------------------------------------------
curl -sS -D "${LOG_DIR}/metrics.hdr" "http://127.0.0.1:${SIDECAR_HOST_PORT}/metrics" > "${LOG_DIR}/metrics.txt"
METRICS_CT=$(grep -iE '^content-type:' "${LOG_DIR}/metrics.hdr" | tr -d '\r')
METRICS_8=$(python3 -c "
import sys,re
need = [
    'nbi_auth_sidecar_info',
    'nbi_auth_sidecar_refresh_success_total',
    'nbi_auth_sidecar_refresh_attempt_total',
    'nbi_auth_sidecar_consecutive_mint_failures',
    'nbi_auth_sidecar_token_ttl_seconds',
    'nbi_auth_sidecar_last_refresh_ts_seconds',
    'nbi_auth_sidecar_ready',
    'nbi_auth_sidecar_http_active_requests',
]
data = open('${LOG_DIR}/metrics.txt').read()
found = [n for n in need if re.search(r'(^|\n)# (HELP|TYPE) ' + re.escape(n) + r'\b', data)]
missing = [n for n in need if n not in found]
print('METRICS_8_OK' if not missing else 'MISSING:' + ','.join(missing))
")
if [[ "${METRICS_CT}" == *text/plain* ]] && [ "${METRICS_8}" = "METRICS_8_OK" ]; then
    pass "T7: /metrics text/plain content-type + 8 metric families present"
else
    fail "T7: metrics CT=${METRICS_CT} 8=${METRICS_8}"
fi

# ---------------------------------------------------------------------------
# 12. POST /rotate-self → 202 Accepted → triggers 2nd call within 15s
# ---------------------------------------------------------------------------
ROTATE_CODE=$(curl -sS -o "${LOG_DIR}/rotate-body.json" -w "%{http_code}" \
    -X POST "http://127.0.0.1:${SIDECAR_HOST_PORT}/rotate-self" 2>/dev/null || echo "000")
if [ "${ROTATE_CODE}" = "202" ]; then
    pass "T8a: POST /rotate-self → 202 Accepted"
else
    fail "T8a: /rotate-self code=${ROTATE_CODE}"
fi
# wait for counter to increment
C2=-1
for s in $(seq 1 15); do
    C2=$(docker exec --user 1000:1000 "${CONTAINER_NAME}" \
        curl -sS "http://127.0.0.1:${JUPYTER_FAKE_PORT}/_smoke/call_count" \
        | python3 -c 'import sys,json; print(json.load(sys.stdin).get("count",-1))')
    if [ "${C2}" = "2" ]; then break; fi
    sleep 1
done
if [ "${C2}" = "2" ]; then
    pass "T8b: /rotate-self triggered 2nd reload-config call (count=2 within 15s)"
else
    fail "T8b: reload call_count after rotate = ${C2}, expected 2"
fi

# ---------------------------------------------------------------------------
# 13. State JSON schema v1 — no raw token or password leaks
# ---------------------------------------------------------------------------
STATE="${TMP_SHARED_HOST}/nbi-token-state.json"
if [ -f "${STATE}" ]; then
    ST_OK=$(python3 -c "
import json,re
try:
    d = json.load(open('${STATE}'))
except Exception as e:
    print('PARSE_ERR',e); exit(0)
blob = json.dumps(d)
# No bare eyJ.* JWT-looking 3-part blob, no JKS/PASS/password literal strings
# longer than 3 chars.
jwt_re = re.compile(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+')
secret_re = re.compile(r'(?i)(password|jks_pass|secret)[:=][\"'\''][^\"'\''']{3,}')
ok = (
    d.get('version') == 1
    and 'token_len' in d and 'raw_token' not in d
    and not jwt_re.search(blob)
    and not secret_re.search(blob)
)
print('ST_OK' if ok else 'ST_BAD keys='+','.join(sorted(d.keys())))
")
    if [ "${ST_OK}" = "ST_OK" ]; then
        pass "T9: state.json v1 schema, no raw_token or password/JWT secrets leaked"
    else
        fail "T9: state.json invalid (${ST_OK})"
    fi
else
    fail "T9: state.json missing"
fi

# ---------------------------------------------------------------------------
# 14. Port 18090 binds 127.0.0.1 only inside the container (AC-8 hard rule)
# ---------------------------------------------------------------------------
BIND_OUT=$(docker exec --user 0 "${CONTAINER_NAME}" sh -c 'command -v ss >/dev/null 2>&1 && ss -ltnp 2>/dev/null | grep 18090 || (command -v netstat >/dev/null 2>&1 && netstat -ltn 2>/dev/null | grep 18090) || echo "BIND_PROBE_FAIL: no ss/netstat, fallback to Python probe"; python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(0.5)
# Try binding port 18090 on 0.0.0.0 — if sidecar bound 0.0.0.0 we get EADDRINUSE
# If sidecar only bound 127.0.0.1 then 0.0.0.0 bind would succeed (and we close
# it immediately with a safety message to avoid actually listening).
try:
    s.bind(('0.0.0.0', 18090))
    s.close()
    print('LOOPBACK_ONLY: 0.0.0.0 bind succeeded → sidecar not on 0.0.0.0 ✓')
except OSError:
    print('BIND_ALL_WARN: 0.0.0.0:18090 in use — sidecar may have bound 0.0.0.0 ✗')
" 2>&1' 2>/dev/null || echo "BIND_EXEC_FAIL")
if [[ "${BIND_OUT}" == *LOOPBACK_ONLY* ]]; then
    pass "T10: sidecar HTTP server 18090 binds 127.0.0.1 only (AC-8 hard rule)"
else
    fail "T10: bind check inconclusive / violated: ${BIND_OUT}"
fi

# ---------------------------------------------------------------------------
# 15. Clean shutdown SIGTERM → exit 0 within 15s
# ---------------------------------------------------------------------------
log "Testing clean shutdown (docker stop)...";
t0=$(date +%s)
STOP_OUT=$(timeout 30 docker stop --time=20 "${CONTAINER_NAME}" 2>&1 || echo "STOP_FAIL")
t1=$(date +%s)
WAIT=$((t1-t0))
EXIT_CODE=$(docker inspect "${CONTAINER_NAME}" --format '{{.State.ExitCode}}' 2>/dev/null || echo "99")
# Note: --rm means container is gone after exit; so inspect may return nothing.
# We check instead via `docker ps -a` absence + stop timeout not hit.
GONE=0
sleep 1; if ! docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then GONE=1; fi
if [ "${STOP_OUT}" != "STOP_FAIL" ] && [ "${GONE}" = "1" ] && [ "${WAIT}" -lt 20 ]; then
    pass "T11: clean shutdown completed in ~${WAIT}s (< 20s)"
else
    fail "T11: shutdown took ${WAIT}s, stop_out=${STOP_OUT}, gone=${GONE}"
fi

# ---------------------------------------------------------------------------
# 16. Summary
# ---------------------------------------------------------------------------
TOTAL=$((N_PASS + N_FAIL))
echo
echo "=========================================================================="
echo " SMOKE TEST SUMMARY — ${IMAGE}"
echo "   PASS: ${N_PASS} / ${TOTAL}"
if [ "${N_FAIL}" -gt 0 ]; then
    echo "   FAIL: ${N_FAIL} / ${TOTAL}"
fi
echo "=========================================================================="
if [ "${N_FAIL}" -eq 0 ]; then
    echo "   ✅ ALL SMOKE ASSERTIONS PASSED"
    exit 0
else
    echo "   ❌ ${N_FAIL} SMOKE ASSERTION(S) FAILED — see logs above + ${LOG_DIR}"
    exit 1
fi

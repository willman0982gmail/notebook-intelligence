# nbi-auth-sidecar

Dedicated Kubernetes sidecar image that runs _next to_ the JupyterLab/NBI
singleuser container in the same Pod. It:

1. **Mints** a fresh AI Factory (APK Ken) JWT via the corporate `token-tool.jar`,
   using a hardened `0600` `-Djava.security.properties` temp file so passwords
   never appear in `/proc/<pid>/cmdline` or any subprocess argv.
2. **Writes** the canonical `~/.jupyter/nbi/config.json` (atomic `fsync` +
   `os.replace`) and a `/tmp/nbi-runtime-env.json` ferry (8 STRING_OVERRIDE
   env vars, always `0600`) so both NBI's native JSON config and the
   property-override system get the same rotated token simultaneously.
3. **Triggers** NBI's `POST /notebook-intelligence/reload-config` endpoint
   with `broadcast=true` so the backend performs `load()` →
   `update_models_from_config()` and pushes a `MCPServerStatusChange`
   WebSocket message that forces the React frontend to call
   `fetchCapabilities()` instantly — no Settings click, no page reload, no
   user interaction required.
4. **Sleeps** until `exp - 300s` (the T-5m rule, configurable via
   `NBI_REFRESH_BEFORE_EXP_SEC`) and re-runs the full cycle.
5. **Exposes** `GET /healthz`, `GET /ready`, `GET /metrics` (Prometheus),
   `POST /rotate-self` on `127.0.0.1:18090` for K8s probes and admin ops.

The code is **pure Python 3.11 stdlib** — zero pip packages. Image size
target: **~330 MB uncompressed** (`python:3.11-slim-bookworm` +
`openjdk-17-jre-headless` + tini + curl + ca-certificates).

---

## Quick Build

```bash
# From the repo root:
cd local-dev/deploy
bash build-images.sh nbi-auth-sidecar
# …tags as:
#   ghcr.io/scbdev/notebook-intelligence/nbi-auth-sidecar:$(git rev-parse --short HEAD)
#   ghcr.io/scbdev/notebook-intelligence/nbi-auth-sidecar:latest
```

The `build-images.sh` wrapper **pre-runs** `python3 -m py_compile` on every
module BEFORE invoking `docker build` so syntax errors fail the local build
in 2s instead of 2min inside the Dockerfile layer.

---

## Configuration

All configuration comes from **environment variables** (helm `values.yaml`
→ `envFrom` on Secret `nbi-llm-auth` + `singleuser.extraEnv`). The Python
code never reads a YAML or JSON config file; everything is injection.

| Variable                                       | Default                                     | Description                                                                                                      |
| ---------------------------------------------- | ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `NBI_SIDECAR_MINTER`                           | `jar`                                       | Set to `mock` for CI / smoke tests; use `jar` in production.                                                     |
| `NBI_SIDECAR_HTTP_HOST`                        | `127.0.0.1`                                 | **Security**: bind address. MUST stay loopback-only.                                                             |
| `NBI_SIDECAR_HTTP_PORT`                        | `18090`                                     | Local HTTP port. K8s probes target this.                                                                         |
| `NBI_CHAT_MODEL_PROVIDER`                      | `openai_compatible`                         | Provider moniker. AI Factory is OpenAI-compatible so this is correct.                                            |
| `NBI_CHAT_MODEL_ID`                            | _(required)_                                | e.g. `databricks/gdp-gpt4o`. From AI Factory team.                                                               |
| `NBI_INLINE_COMPLETION_MODEL_PROVIDER`         | _(defaults to chat provider)_               | Usually identical to the chat provider.                                                                          |
| `NBI_INLINE_COMPLETION_MODEL_ID`               | _(defaults to chat model id)_               | e.g. `databricks/gdp-gpt4o-mini`.                                                                                |
| `NBI_CLAUDE_CHAT_MODEL`                        | `""`                                        | Optional: model ID for NBI's "Claude mode" participant.                                                          |
| `NBI_CLAUDE_INLINE_COMPLETION_MODEL`           | `""`                                        | Optional: inline model ID for Claude mode.                                                                       |
| `ANTHROPIC_BASE_URL`                           | _(required)_                                | AI Factory gateway, e.g. `https://gateway.scaifactory.dev.azure.scbdev.net/v1`.                                  |
| `NBI_REFRESH_BEFORE_EXP_SEC`                   | `300`                                       | "T-5m" rule. Refresh exactly this many seconds before JWT `exp`.                                                 |
| `NBI_FORCE_REFRESH_INTERVAL_SEC`               | `0`                                         | Set to e.g. `3600` if your IdP silently revokes tokens whose `exp` has not fired. `0` = disabled.                |
| `NBI_SCHEDULER_SLEEP_CHUNK_SEC`                | `10`                                        | Granularity of interruptible sleep. Smaller = faster `/rotate-self` response, more wakeups.                      |
| `NBI_BOOTSTRAP_TIMEOUT_SEC`                    | `90`                                        | Max total time for the initial synchronous bootstrap cycle. Exceeding this → sidecar exits 1 → CrashLoopBackOff. |
| `NBI_BACKOFF_BASE_SEC` / `NBI_BACKOFF_CAP_SEC` | `10` / `300`                                | Exponential backoff bounds for transient mint failures.                                                          |
| `NBI_RELOAD_DISCOVERY_TIMEOUT_SEC`             | `60`                                        | Max time waiting for notebook's `jpserver-*.json` to appear. Override to larger for slow-start kernels.          |
| `NBI_RELOAD_HTTP_TIMEOUT_SEC`                  | `5`                                         | Per-attempt POST timeout to localhost NBI.                                                                       |
| `NBI_RELOAD_POST_ATTEMPTS`                     | `3`                                         | Retries per reload() call. Backoff is 2-4-8s linear.                                                             |
| `NBI_LOG_LEVEL`                                | `INFO`                                      | `DEBUG` / `INFO` / `WARNING`. `DEBUG` prints per-mint backoff and FS poll steps.                                 |
| `JUPYTER_RUNTIME_DIR`                          | `/home/jovyan/.local/share/jupyter/runtime` | Where to read `jpserver-*.json` for jupyter port + token discovery. Already correct for Z2JK.                    |

### Credential envs (from K8s Secret `nbi-llm-auth`, NEVER bake into image)

| Variable                  | Description                                                               |
| ------------------------- | ------------------------------------------------------------------------- |
| `NBI_TOKEN_TOOL_JAR`      | Defaults to `/var/run/nbi-llm-auth/token-tool.jar` — mounted from Secret. |
| `NBI_KEYSTORE_JKS`        | Defaults to `/var/run/nbi-llm-auth/keystore.jks`.                         |
| `NBI_TRUSTSTORE_JKS`      | Defaults to `/var/run/nbi-llm-auth/aitruststore.jks`.                     |
| `OIDC_TOKEN_URL`          | OIDC `/token` endpoint of corporate IdP.                                  |
| `OIDC_CLIENT_CODE`        | OIDC public client identifier.                                            |
| `OIDC_DOMAIN`             | OIDC domain / realm.                                                      |
| `JKS_KEYSTORE_PASSWORD`   | JKS key password — delivered ONLY via envFrom secret; never on argv.      |
| `JKS_TRUSTSTORE_PASSWORD` | JKS trust password — same.                                                |

---

## HTTP Surface (`127.0.0.1:18090`)

All routes are loopback-only. Any attempt to bind to `0.0.0.0` raises
`ValueError` at construction time.

| Route          | Method | Description                                                                                                            |
| -------------- | ------ | ---------------------------------------------------------------------------------------------------------------------- |
| `/healthz`     | GET    | Always 200 while Python is alive. **Liveness probe target.**                                                           |
| `/ready`       | GET    | 200 iff the last-known token is valid AND mint is not in a 3+ failure streak. **Readiness probe target.**              |
| `/metrics`     | GET    | Prometheus text exposition format. 8 gauge/counter families. Scraped by your cluster Prom.                             |
| `/rotate-self` | POST   | 202 Accepted. Runs a full out-of-band refresh cycle. Accepts optional `{"reason":"…"}` body for audit log. Idempotent. |

Sample `kubectl exec` one-liner after mounting your kubeconfig:

```bash
kubectl -n jhub exec -it jupyter-<user> -c nbi-auth-sidecar -- \
    curl -X POST http://127.0.0.1:18090/rotate-self \
         -H 'Content-Type: application/json' \
         -d '{"reason":"ops: manual roll after IdP cert change"}'
```

---

## 5-Layer Cache Propagation

The sidecar writes tokens and then triggers reload so _all five_ NBI cache
layers update **without user interaction**:

```
[Sidecar] ──atomic write──▶ L1  ~/.jupyter/nbi/config.json        (disk)
                           L2  nbi_config.load() dict              (inproc server)
[POST reload-config] ──▶   L3  update_models_from_config + set_property_value(api_key)
[broadcast MCPServerStatusChange WS msg] ─▶ L4 TS NBIAPI.config.capabilities
                                                    └─▶ L5 React Settings / Chat state
```

If the reload POST transiently fails (e.g. NBI routes are not yet
registered because the extension is mid-init), the sidecar falls back to
**S2 FS-polling mode** (see `jupyter_server_config_nbi_reload.py`) — a
daemon thread inside the notebook container checks the 4-tuple stat
signature of `config.json` every 5s and runs the same `do_reload()` chain
when `mtime_ns` or `ino` changes. Either way L1→L5 closes within **≤ 5s
end-to-end** of a successful mint.

---

## Security Notes

1. **Passwords never on argv.** Java subprocess receives a single
   `-Djava.security.properties=/tmp/nbi-sidecar-props-XXXXXX` file of mode
   `0600` that contains all JKS paths+passwords. The file is `unlink()`ed
   in a `finally:` block before `mint()` returns, regardless of outcome.
2. **`/proc/<pid>/cmdline` leak-free.** Because passwords are not on the
   `java` command line, `ps -ef`/`cat /proc/…/cmdline` cannot disclose
   them. `e2e-validation.sh` includes a `grep` check for this.
3. **Runtime env file `0600`.** `/tmp/nbi-runtime-env.json` carries
   `ANTHROPIC_API_KEY` and is chmod'd explicitly regardless of umask.
   Atomic write pattern means a crashed writer never leaves a half-written
   JSON body.
4. **State file contains `token_len`, not raw token.**
   `/tmp/nbi-token-state.json` is metadata-only. Anyone with pod shell
   access who reads this file learns the approximate TTL but cannot
   escalate to API calls.
5. **Sidecar HTTP binds 127.0.0.1 only.** Even if your CNI accidentally
   routes pod-port traffic to the host, no external client can reach the
   sidecar surface. `0.0.0.0` is rejected at the code level.
6. **Non-root runtime user 1000:1000.** `Dockerfile` switches to
   `USER 1000:1000`; all code is copied mode `0555 root:root` so even if
   an attacker compromises the Python process they cannot overwrite the
   mint logic to exfiltrate tokens through it.
7. **Container securityContext** (pre_spawn_hook.py enforces these):
   `runAsNonRoot: true`, `allowPrivilegeEscalation: false`,
   `capabilities.drop: [ALL]`, `seccompProfile.type: RuntimeDefault`.

---

## Deployment

Full details in `charts/nbi-auth-sidecar/` overlay. Short form:

```bash
helm repo add jupyterhub https://hub.jupyter.org/helm-chart
helm repo update
helm upgrade --install nbi jupyterhub/jupyterhub \
    --namespace jhub --create-namespace \
    --version 3.3.0 \
    --values charts/nbi-auth-sidecar/values.yaml \
    --set singleuser.nbiAuthSidecar.image.tag=abc1234  # git short SHA
```

The chart overlay does **two things only**:

1. Sets `hub.extraConfig` to inject `pre_spawn_hook.py` into Hub's Python
   config, so every spawned Pod receives a _second_ container — this
   sidecar — plus the `envFrom` secret, shared `/tmp` empty-dir, and probe
   definitions.
2. Populates `hub.extraEnv` / `singleuser.extraEnv` with the 8
   STRING_OVERRIDE provider+model knobs so the admin can change global
   defaults in one file instead of editing per-profile lists.

**Upgrade note:** `helm upgrade --set singleuser.nbiAuthSidecar.image.tag=NEW_TAG`
NEVER restarts existing user pods. The sidecar is injected by the Hub
Python hook at **spawn time**, not by a K8s Deployment. Only
newly-launched notebooks will use the new image tag; already-running users
keep their existing sidecar. This is the desired zero-downtime behaviour.

---

## Troubleshooting

Symptom → Likely cause:

| Symptom                                                                  | Root Cause                                                             | Fix                                                                                                                                                            |
| ------------------------------------------------------------------------ | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Sidecar log: `Bootstrap FAILED after 90.1s`                              | mint failing — JKS/IdP unreachable, password wrong, no egress.         | Check K8s NetworkPolicy `networkpolicy-llm-egress.yaml`. Shell into container and run `curl -v $OIDC_TOKEN_URL`.                                               |
| NBI Settings shows **old masked token**, chat 401s                       | reload-config POST never reached S3/L3.                                | `kubectl exec` → `curl -X POST 127.0.0.1:18090/rotate-self`. If that works: check `NBI_RELOAD_DISCOVERY_TIMEOUT_SEC` — notebook init slower than 60s? Bump it. |
| Sidecar container `CrashLoopBackOff`, `readinessProbe` failing           | mint is in 3+ consecutive failures.                                    | Look for redacted `stderr tail` in sidecar logs; common root: OIDC_CLIENT_CODE value has a trailing space from secret copy-paste.                              |
| `/metrics` endpoint reports `nbi_auth_sidecar_ready 0` but chat works    | Stale state file carried across pod restart within empty-dir lifetime. | Manually `rm /tmp/nbi-token-state.json` and call `/rotate-self`.                                                                                               |
| `java.io.IOException: Keystore was tampered with, or password was wrong` | JKS_PASSWORD doesn't match the .jks file's real password.              | Re-download .jks from corporate certificate portal; passwords are case-sensitive.                                                                              |

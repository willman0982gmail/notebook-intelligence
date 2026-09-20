# Notebook Intelligence Auth Sidecar — Design & Deployment Guide

**Version**: 1.0.0 (2026-09-18)
**Author**: GDP Data Platform
**Scope**: SCB GDP Notebook Intelligence Enterprise (deployed on Zero-to-JupyterHub v3.3.0)
**Audience**:

- Platform / K8s Ops (responsible for image builds and Helm upgrades)
- Identity / Security teams (responsible for JKS / JAR / OIDC credential issuance and validation)
- NBI Engineers (responsible for sidecar code changes and troubleshooting)

**Companion Files**:

- Source directory: [local-dev/deploy/nbi-auth-sidecar/](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar)
- Hub Spawn Hook: [local-dev/hub/pre_spawn_hook.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/hub/pre_spawn_hook.py)
- Jupyter Server Monkey-patch: [local-dev/deploy/jupyter_server_config_nbi_reload.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/jupyter_server_config_nbi_reload.py)
- JupyterHub values overlay: [jupyterhub-values-nbi-auth-sidecar.yaml](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/k8s/jupyterhub-values-nbi-auth-sidecar.yaml)
- Helm shape assertion script: [helm-template-test.sh](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/helm-template-test.sh)
- Build script: [local-dev/deploy/build-images.sh](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/build-images.sh)
- E2E scripts & Checklist: [local-dev/deploy/nbi-auth-sidecar/](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar) (smoke-test.sh, e2e-validation.sh, e2e-checklist.md)

---

## Chapter 0: Executive Summary (TL;DR)

> **One-liner**: Append a **sidecar container** to every spawned Jupyter singleuser Pod. Within 90 seconds of Pod start the sidecar synchronously performs the first AI Factory JWT mint → atomic NBI config write → triggers a full NBI 5-layer cache refresh via an HTTP endpoint → broadcasts a WebSocket message so all browser tabs auto-refresh the Settings panel. It then automatically rotates every 5 minutes (`exp - 300 s`) so a fresh token is always in place before the previous one expires. **Zero user config / zero user awareness**.

```
┌────────────────────────── singleuser Pod (shared netns, 127.0.0.1 reachable cross-container)
│
│  ┌─ notebook container (UID1000 jovyan) ─────────────────────────────────┐
│  │                                                                        │
│  │  jupyter server (port 8888)                                            │
│  │   └─ NBI extension + ★ monkey-patch:                                  │
│  │        · POST /notebook-intelligence/reload-config  (authenticated)   │
│  │        · daemon FS-poll thread (5s tick, stat 4-tuple signature)      │
│  │                                                                        │
│  │  ~/.jupyter/nbi/config.json   ← sidecar atomic write                  │
│  │  /tmp/nbi-runtime-env.json   ← sidecar ferry 0600 8 keys              │
│  └────────────────────────────────────────────────────────────────────────┘
│                          ▲ HTTP POST reload-config (token-based auth)
│                          │
│  ┌─ nbi-auth-sidecar container (UID1000 jovyan) ────────────────────────┐
│  │                                                                        │
│  │  · port 18090 HTTP (BIND 127.0.0.1 ONLY ★ hard rule)                 │
│  │      GET  /healthz  /ready  /metrics                                  │
│  │      POST /rotate-self  (trigger on-demand refresh)                   │
│  │                                                                        │
│  │  · scheduler loop:                                                     │
│  │    ┌─ mint AI Factory JWT via SecureJarTokenMinter                    │
│  │    ├─ atomic write 2 files (config.json + ferry JSON 0600)            │
│  │    ├─ POST reload-config (dual auth: ?token= + Authorization header) │
│  │    └─ sleep interruptible in 10s chunks (wake on SIGTERM or /rotate-self)
│  └────────────────────────────────────────────────────────────────────────┘
│
└──────────────────────────────────────────────────────────────────────────
```

---

## Chapter 1: Background & Objectives

### 1.1 Why an Auth Sidecar

**Historical state (problems)**:

1. Each user manually opened the NBI Settings page and pasted an AI Factory token — high misconfiguration rate, bad UX, high training cost.
2. AI Factory JWT TTL ≈ 30–60 min. During long-running tasks the token expired mid-flight and the next chat completion returned `401 Unauthorized` — users had no idea why it failed and had to manually paste a new token in Settings.
3. Hand-written rotation scripts directly edited `config.json`, but NBI has a 5-layer cache chain (see §2.4), so after writing the file the UI remained stale.
4. Security compliance mandates: JKS passwords **MUST NOT** appear in command line arguments (`/proc/<pid>/cmdline`), env dumps, or logs.

**This solution (fixes)**:

- **Zero-user-config**: Settings are auto-populated after spawn, no Save click needed.
- **Auto-rotate 5 min before expiry**: Transparent hot refresh.
- **End-to-end security**: JKS passwords only ever live inside a 0600-mode temp file which is unlinked on every exception path.
- **No NBI source changes**: Injection uses Jupyter Server's standard `post_open_app_callbacks` hook plus a Python hook for the sidecar — no intrusion into the `notebook_intelligence` package.

### 1.2 Scope vs Out-of-Scope

| **In Scope**                       | **Out of Scope**                                    |
| ---------------------------------- | --------------------------------------------------- |
| Auto-first-mint + T-5min rotate    | Admission Webhook / Mutating Admission Controller   |
| NBI 5-layer cache chain refresh    | A new OAuth/OIDC browser dance                      |
| 127.0.0.1 sidecar HTTP local-only  | Exposing the sidecar as a cluster-internal Service  |
| Docker image ≤ 500 MB uncompressed | Multi-cluster federation / service mesh integration |
| Helm overlay merged with Z2JK      | Modifications to the NBI source package             |

---

## Chapter 2: System Architecture & Core Mechanisms

### 2.1 Two-Container Sidecar Topology

Each singleuser Pod contains 2 containers (dynamically injected before spawn by [make_pre_spawn_hook()](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/hub/pre_spawn_hook.py#L118-L463)):

| Container `name:`                  | Image                                                        | Entrypoint                                    | Purpose                                                             |
| ---------------------------------- | ------------------------------------------------------------ | --------------------------------------------- | ------------------------------------------------------------------- |
| `notebook` (upstream Z2JK default) | `nbi-singleuser:<tag>` (jupyter server + NBI + monkey-patch) | `wrapper.sh → jupyter labhub`                 | The user's actual Notebook.                                         |
| `nbi-auth-sidecar`                 | `nbi-auth-sidecar:<tag>`                                     | `/usr/bin/tini -- python -m nbi_auth_sidecar` | Independent JWT mint + scheduled rotate + hot refresh orchestrator. |

**Why a sidecar instead of cramming the logic into the notebook container?**

1. **Failure domain isolation**: A rotate crash (JVM crash, corrupt JKS) impacts the sidecar only — never the Jupyter server process. K8s restarts the sidecar container; the user's session survives.
2. **Privilege layering**: JKS / JAR / passwords are mounted only into the sidecar — the notebook container shell / terminal cannot see the credential files.
3. **Independent upgrade**: Swap only the image tag and restart Hub; newly spawned users get the new sidecar; already running users are unaffected (zero-downtime upgrade).
4. **Independent resource quota**: The JVM heap needs 256–768 Mi. If crammed into the notebook container the notebook's memory limit is inflated by the JVM, causing users to feel "I'm running no code yet but 1 GB is used".

### 2.2 Injection Point: KubeSpawner Hook (Non-Intrusive)

Z2JK's native traitlet `c.KubeSpawner.pre_spawn_hook` accepts a callable (sync or async) invoked after user auth state is fetched and before the Hub creates the Pod spec.

We register it inside [jupyterhub-values-nbi-auth-sidecar.yaml hub.extraConfig 000-nbi-auth-sidecar-hook.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/k8s/jupyterhub-values-nbi-auth-sidecar.yaml#L94-L164):

```python
try:
    from hub.pre_spawn_hook import make_pre_spawn_hook
except Exception:
    # ★ graceful degradation: import failure does NOT block hub start, only WARNING log
    # Users can still manually configure Settings if needed.
    log.warning("sidecar hook module missing — skipping sidecar injection")
else:
    if _nbi_sidecar_enabled():  # env flag NBI_AUTH_SIDECAR_ENABLE = true/false
        c.KubeSpawner.pre_spawn_hook = make_pre_spawn_hook(
            nbi_auth_sidecar_config=_sidecar_cfg,
        )
```

**Injection action list (6 steps, inside the `_inject_nbi_auth_sidecar()` closure)**:

1. **2 Volumes**:
   - `EmptyDir` `nbi-shared-tmp` → mounted at `/tmp`, shared by both containers (ferry JSON, state JSON, mock scripts).
   - `Secret` `nbi-llm-auth` → defaultMode=0400, sidecar mounts to `/var/run/nbi-llm-auth/`.
2. **Notebook container `envFrom`**: Appends `secretRef: nbi-llm-auth` (only non-password static params get injected from the secret; passwords flow via same-named env vars through K8s envFrom and are read by the sidecar process only for properties writing — never reach the CLI).
3. **Notebook container volume_mount**: `/tmp` shared.
4. **Spawner.environment** gets **7 provider-level defaults** (explicitly **NO** `ANTHROPIC_API_KEY` — API key is written from the sidecar ferry JSON only):
   - `NBI_CHAT_MODEL_PROVIDER=openai_compatible`
   - `NBI_CHAT_MODEL_ID=databricks/gdp-gpt4o`
   - `NBI_INLINE_COMPLETION_MODEL_PROVIDER=openai_compatible`
   - `NBI_INLINE_COMPLETION_MODEL_ID=databricks/gdp-gpt4o`
   - `NBI_CLAUDE_CHAT_MODEL=""`
   - `NBI_CLAUDE_INLINE_COMPLETION_MODEL=""`
   - `ANTHROPIC_BASE_URL=https://gateway.scaifactory.dev.azure.scbdev.net/v1`
5. **Sidecar container dict construction**: image, env, envFrom, volumeMounts, resources (50m/256Mi→500m/768Mi), securityContext (runAsNonRoot + UID 1000 + ALL caps drop + seccomp RuntimeDefault).
6. **Probes**: liveness `/healthz` initialDelay=10, readiness `/ready` initialDelay=15, **explicit `httpGet.host: 127.0.0.1` ★ hard rule**.

**Idempotency guarantee**:

```python
old_list = list(getattr(spawner, "extra_containers", None) or [])
deduped = [c for c in old_list if not (isinstance(c, dict) and c.get("name") == "nbi-auth-sidecar")]
deduped.append(sidecar_container)
spawner.extra_containers = deduped
```

→ N calls always yield exactly 1 sidecar container entry. Volumes, envFrom, volume_mounts, environment all use the same filter-then-append pattern.

### 2.3 Scheduler 4-Step Loop

[nbi_auth_sidecar/scheduler.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/scheduler.py)

```
bootstrap (sync, block main thread, max 90 s)
 └─ run full 4-step cycle ONCE before HTTP server /ready reports OK.

daemon loop:
  loop:
    next_run = compute_next_run(
        MIN( exp_ts - NBI_REFRESH_BEFORE_EXP_SEC (default 300s=5min),
             last_refresh_ts + NBI_FORCE_REFRESH_INTERVAL_SEC if >0 else ∞)
    )
    sleep_interruptible(until=next_run, tick=10s)
      ├─ SIGTERM → stop event set → break
      └─ POST /rotate-self → trigger event set → break
    do_refresh_cycle():
      step 1: MINT token via TokenMinter (cache hit short-cuts JVM invocation)
      step 2: WRITE atomically 2 files:
                · ~/.jupyter/nbi/config.json  (atomic_write_json matching NBI native exactly)
                · /tmp/nbi-runtime-env.json   (STRING_OVERRIDE 8 keys, mode 0600)
      step 3: POST reload-config authenticated (body: {"broadcast":true})
              3 retries w/ exponential backoff; 401=permanent fail, connect=transient
      step 4: PERSIST scheduler state v1 → /tmp/nbi-token-state.json
                (version:1, ok, token_len, exp, last_refresh, consecutive_mint_failures
                 ★ NEVER contains raw_token, password, or eyJ* JWT blob literals)
```

### 2.4 NBI 5-Layer Cache + 2-Path Hot Refresh (Critical!)

> This is the #1 subtle area where "token looks rotated but chat still 401s". Internalize this.

**5-layer cache chain** (from slowest to fastest — each layer is closer to the user than the previous):

```
L1 Disk:                 ~/.jupyter/nbi/config.json  ← script/disk write touches ONLY this
L2 Python dict:          NBIConfig._data              nbi_config.load() reads L1 → L2
L3 Provider instances:   AIServiceManager._providers[provider].api_key (set_property_value)
L4 TS fetchCapabilities: NBIAPI._cachedCapabilities   in browser TS code, re-apply STRING_OVERRIDES
L5 React state:          Settings panel state + Chat panel provider ref  (held in React hooks)
```

**Why writing `config.json` directly does NOT work**: Scripts bypass `ConfigHandler.post()` — the four follow-up steps `load → update_models_from_config → set_property_value → WS broadcast` never run, so L3/L4/L5 all remain stale.

**We guarantee L1→L5 propagation with TWO paths**:

#### ⚡ Path A (Fast, HTTP reload endpoint) — Primary

Sidecar step 3 executes: `POST 127.0.0.1:8888/notebook-intelligence/reload-config` (handler injected by the monkey-patch, see §3.4 jupyter_server_config_nbi_reload.py). The handler runs in STRICT ORDER:

1. **First merge ferry JSON into `os.environ`** → on the next config access `apply_string_overrides()` re-applies all 8 keys, defeating the L3 Provider property stale cache for STRING_OVERRIDE envs.
2. **Then call `nbi_config.load()`** → L1→L2.
3. **Then call `ai_service_manager.update_models_from_config()`** → destroys and rebuilds the Provider; calls `set_property_value("api_key", "newtoken")`, solving L3.
4. **Finally broadcast WS `MCPServerStatusChange`** → frontend hard-coded to call `fetchCapabilities()` on this message; capabilities re-fetch → L4/L5 refresh.

#### 🛟 Path B (Fallback, FS Poll) — Secondary / Fail-safe

The notebook-container monkey-patch starts an extra daemon thread that runs every 5 seconds:

```python
def _current_signature(self) -> (dev, ino, size, mtime_ns):
    st = os.stat(config_json_path)  # /home/jovyan/.jupyter/nbi/config.json
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

if sig != last_sig:
    do_reload()  # same step 1..4 as Path A!
```

→ 4-tuple signature: dev (survives remounts) + ino (`atomic_write_json` uses `os.replace()` which always changes inode) + size + mtime_ns (catches in-place overwrite on the same inode). No write mode (atomic or non-atomic) ever escapes detection.

**Combined effect**: Path A delivers sub-second refresh; if Path A's monkey-patch is accidentally removed by some custom image (or the endpoint 404s), Path B catches it within 5 seconds.

### 2.5 JKS Password Security (Hard Rule AC-5)

> Project hard constraint: passwords **MUST NEVER** be passed via CLI `-Dxxx=PASSWORD` or Java `-Djavax.net.ssl.keyStorePassword=...` flags to avoid exposure in `/proc/<pid>/cmdline`. `SecureJarTokenMinter` solves this with a 0600-mode tmpfile plus `-Djava.security.properties=<PATH>`.

Key excerpt from [nbi_auth_sidecar/mint.py SecureJarTokenMinter](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/mint.py):

```python
# STEP 1: Create 0600 tmp file with java.security properties
fd, props_path = tempfile.mkstemp(
    prefix="jsec_", suffix=".properties",
    dir=os.environ.get("NBI_JAVA_SECURITY_PROPERTIES_TMPDIR", "/tmp")
)
try:
    os.fchmod(fd, 0o600)   # mode 0600 (owner read/write only, no group/other)
    with os.fdopen(fd, "w") as f:
        # keystore (private keys) — path, password, type
        f.write(f"javax.net.ssl.keyStore={keystore_jks}\n")
        f.write(f"javax.net.ssl.keyStorePassword={keystore_password_from_os_env}\n")
        f.write(f"javax.net.ssl.keyStoreType=PKCS12\n")
        # truststore (CA certs) — path, password, type
        f.write(f"javax.net.ssl.trustStore={truststore_jks}\n")
        f.write(f"javax.net.ssl.trustStorePassword={truststore_password_from_os_env}\n")
        f.write(f"javax.net.ssl.trustStoreType=JKS\n")

    # STEP 2: subprocess argv — NO passwords, ONLY the properties file path!
    argv = [
        "java",
        "-Djava.security.properties=file:" + props_path,  # ★ single flag; safe for /proc
        "-jar", jar_path,
        "--token-url", os.environ.get("OIDC_TOKEN_URL"),
        "--client-code", os.environ.get("OIDC_CLIENT_CODE"),
        "--domain", os.environ.get("OIDC_DOMAIN"),
        "--scope", "openid profile",
    ]

    # STEP 3: Run with timeout + capture stderr; redact_secrets() before log append
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=60, check=False, env=os.environ,
    )
    if result.returncode != 0:
        scrubbed = redact_secrets(result.stderr)  # log scrub pass before write
        raise ValueError(f"token-tool.jar failed rc={result.returncode}: {scrubbed}")
    ...
finally:
    # STEP 4: Always unlink — CalledProcessError / TimeoutExpired / KeyboardInterrupt all land here
    try:
        os.unlink(props_path)
    except OSError:
        pass
```

→ Verification: `/proc/<java_pid>/cmdline` contains only `-Djava.security.properties=file:/tmp/jsec_XXXX.properties` — zero password strings.

### 2.6 STRING_OVERRIDE_SPEC 8-Key Ordering (Do NOT reshuffle!)

The NBI extension defines 8 env vars at the `@property` layer which are **re-read from environ on every access** to chat/inline config properties (bypassing L3 cache). The 8 keys sidecar writes to `/tmp/nbi-runtime-env.json` MUST follow a fixed order for clean diffing and auditing:

```
1. NBI_CHAT_MODEL_PROVIDER
2. NBI_CHAT_MODEL_ID
3. NBI_INLINE_COMPLETION_MODEL_PROVIDER
4. NBI_INLINE_COMPLETION_MODEL_ID
5. NBI_CLAUDE_CHAT_MODEL
6. NBI_CLAUDE_INLINE_COMPLETION_MODEL
7. ANTHROPIC_API_KEY          # ★ ONLY place where the raw token lives as env ferry
8. ANTHROPIC_BASE_URL
```

→ File permissions are hard-enforced to 0600 (jovyan read-only). See [config_writer.py write_all_from_token](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/config_writer.py).

### 2.7 Sidecar HTTP Interface (port 18090 / 127.0.0.1 ONLY)

[nbi_auth_sidecar/server.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/server.py)

| Method | Path           | Purpose                                            | Response fields                                                                                                                                                               |
| ------ | -------------- | -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| GET    | `/healthz`     | K8s liveness + ad-hoc health check                 | `{pid, token_ttl_seconds, ready, last_refresh_ts_seconds, refresh_count, consecutive_mint_failures}`                                                                          |
| GET    | `/ready`       | K8s readiness (200 only after first mint succeeds) | `{ready, reason}` 412 if not ready                                                                                                                                            |
| GET    | `/metrics`     | Prometheus text exposition (8 families)            | nbi_auth_sidecar_info, refresh_success_total, refresh_attempt_total, consecutive_mint_failures, token_ttl_seconds, last_refresh_ts_seconds, ready gauge, http_active_requests |
| POST   | `/rotate-self` | Manual on-demand immediate refresh                 | `{accepted: true, reason}` 202 Accepted                                                                                                                                       |

**★ BoundedThreadingHTTPServer**: Uses `Semaphore(8)` to cap concurrent handler threads, preventing ulimit `nproc` exhaustion (complies with project engineering conventions).

**★ Hard rule — loopback bind only**: Before startup `_assert_host_is_loopback(host)` is called. Any attempt to pass `0.0.0.0` / pod IP / hostname directly raises `ValueError` and aborts startup. Because K8s probes execute inside the container they MUST set `host: 127.0.0.1` explicitly, which is what we hard-code in the pre_spawn_hook probe dicts.

---

## Chapter 3: File Inventory & Core Code References

### 3.1 Python Modules (8 files, stdlib-only — zero pip)

| File                                                                                                                                                 | Role                                   | Core objects / methods                                                                                                                                                                                                 |
| ---------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [mint.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/mint.py)                           | Token issuance implementation          | `AccessToken` (dataclass), `TokenMinter` (Protocol), `MockTokenMinter`, `SecureJarTokenMinter` (0600 properties file), `RetryingTokenMinter` (exponential backoff 5×), `build_token_minter()` factory                  |
| [config_writer.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/config_writer.py)         | Atomic NBI config + ferry JSON         | `atomic_write_json()` (tempfile+fsync+chmod+os.replace+dir_fsync matching NBI native exactly), `write_all_from_token()` facade                                                                                         |
| [scheduler.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/scheduler.py)                 | Loop scheduler + state v1 persistence  | `SchedulerState` dataclass, `compute_next_run()`, `_sleep_interruptible(tick=10)`, `do_refresh_cycle()`, `bootstrap(max_wait=90)`, `run_forever()` daemon thread, `trigger_refresh_now` Event                          |
| [server.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/server.py)                       | 127.0.0.1 HTTP control plane           | `BoundedThreadingHTTPServer` (Semaphore 8), `SidecarRequestHandler` routes: /healthz, /ready, /metrics (Prometheus text exposition), /rotate-self                                                                      |
| [nbi_reload_client.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/nbi_reload_client.py) | Discover & call reload-config endpoint | `discover_jupyter_runtime(timeout=60s)` reads `jpserver-*.json`, `DefaultNBIReloadClient.post_reload(broadcast=True)` 3 retry, dual auth                                                                               |
| [redaction.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/redaction.py)                 | Log redaction / scrubbing              | 4 regex patterns: Bearer header, ENV assignment secret-sounding keys, Java `-D` password flags, bare 3-part eyJ JWT blob with minimum-length gate. `redact_secrets()` (str) + `redact_bytes()` (bytes-safe)            |
| [**main**.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/__main__.py)                   | Entrypoint wiring + signal handler     | `Options` env dataclass, `_configure_logging()`, `_install_signal_handlers()` SIGTERM/SIGINT → daemon thread schedules shutdown (**avoids same-thread deadlock on server.shutdown()**), `main()` returns int exit code |
| [**init**.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/__init__.py)                   | Package metadata                       | `__version__ = "1.0.0"`                                                                                                                                                                                                |

### 3.2 Notebook-Container Monkey-Patch (1 file)

- [jupyter_server_config_nbi_reload.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/jupyter_server_config_nbi_reload.py) — main entry `c.ServerApp.post_open_app_callbacks.append(install_nbi_reload_features)`:
  - `merge_runtime_env_into_environ('/tmp/nbi-runtime-env.json')`: **writes ferry JSON's 8 keys into `os.environ` BEFORE every reload**
  - `_locate_ai_service_manager()`: 3-fallback (ext_manager → NBI globals → gc scan by class name), resilient to NBI upgrades where the global variable name may change; this makes the monkey-patch tolerant to NBI version bumps.
  - `do_reload()` strict order: merge_env FIRST → load → update_models_from_config → broadcast.
  - `ReloadConfigHandler`: `@web.authenticated` protected route, Content-Length ≤ 64 KiB, body `{"broadcast":true}`.
  - `ConfigFilePoller`: daemon thread loop, 5 s tick, 4-tuple stat signature check.

### 3.3 Hub Spawn Hook (1 file)

- [pre_spawn_hook.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/hub/pre_spawn_hook.py) — `make_pre_spawn_hook(*, nbi_auth_sidecar_config=None)` factory; with `None` the **injection is completely skipped** (backward compatible).

### 3.4 Dockerfiles (2 + 1)

| File                                                                                                                 | Location          | Base image                                                                                          | Size (uncompressed, target)    |
| -------------------------------------------------------------------------------------------------------------------- | ----------------- | --------------------------------------------------------------------------------------------------- | ------------------------------ |
| [Dockerfile.singleuser](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/Dockerfile.singleuser) | local-dev/deploy/ | SCB-standard singleuser                                                                             | +~50 KB (just the python file) |
| [Dockerfile](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/Dockerfile)      | nbi-auth-sidecar/ | python:3.11-slim-bookworm + openjdk-17-jre-headless + tini + curl + ca-certificates, USER 1000:1000 | ≤ 500 MB (target)              |

### 3.5 Build / Test / E2E Scripts (5 files)

| Script                                                                                                                        | Location          | Purpose                                                                                                                                                                |
| ----------------------------------------------------------------------------------------------------------------------------- | ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [build-images.sh](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/build-images.sh)                      | deploy/           | `IMAGES=${targets}` selector, **pre-step `py_compile` fail-fast** before docker build. 3 targets: nbi-quota / nbi-singleuser / nbi-auth-sidecar.                       |
| [smoke-test.sh](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/smoke-test.sh)         | nbi-auth-sidecar/ | Local docker run + fake jupyter server (stdlib http.server). 16 assertions covering bootstrap, /rotate-self, dual auth, mode 0600, 8 /metrics families, clean SIGTERM. |
| [helm-template-test.sh](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/helm-template-test.sh)          | deploy/           | AC-7 helm shape assertions. Uses `helm+yq` render when available, otherwise falls back to a pure-shape-check Python script.                                            |
| [e2e-validation.sh](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/e2e-validation.sh) | nbi-auth-sidecar/ | 7 live K8s checkpoints; requires current kubectl context to target cluster + an already-running user Pod.                                                              |
| [e2e-checklist.md](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/e2e-checklist.md)   | nbi-auth-sidecar/ | 15-row Release Gate checklist. Full 11-AC coverage.                                                                                                                    |

---

## Chapter 4: Deployment Steps (Command-by-Command)

### 4.0 Prerequisite Checklist

Before you start, **4 external teams MUST deliver the following** (credentials never live in this repo! they live inside K8s Secret `nbi-llm-auth` namespace `jhub`):

| Deliver team             | Deliverable                                                                     | Mount location                                                      | Secret key name                                                                                                |
| ------------------------ | ------------------------------------------------------------------------------- | ------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Identity / Security team | `token-tool.jar` (corporate-standard AI Factory JWT issuance tool)              | `/var/run/nbi-llm-auth/token-tool.jar` inside the sidecar container | packaged via `kubectl create secret generic --from-file`, key name = `token-tool.jar` (uses filename verbatim) |
| Identity / Security team | `keystore.jks` (private key / client cert, PKCS12)                              | `/var/run/nbi-llm-auth/keystore.jks`                                | key name = `keystore.jks`                                                                                      |
| Identity / Security team | `aitruststore.jks` (CA trust anchors, JKS)                                      | `/var/run/nbi-llm-auth/aitruststore.jks`                            | key name = `aitruststore.jks`                                                                                  |
| Identity / Security team | keystore password (ASCII string, ≥12 chars)                                     | K8s envFrom → env var `JKS_KEYSTORE_PASSWORD`                       | key = `jks_keystore_password`                                                                                  |
| Identity / Security team | truststore password (ASCII string, ≥12 chars)                                   | K8s envFrom → env var `JKS_TRUSTSTORE_PASSWORD`                     | key = `jks_truststore_password`                                                                                |
| Identity / Security team | `OIDC_TOKEN_URL` (https://idp.xxx/.../token)                                    | env var                                                             | key = `oidc_token_url`                                                                                         |
| Identity / Security team | `OIDC_CLIENT_CODE`                                                              | env var                                                             | key = `oidc_client_code`                                                                                       |
| Identity / Security team | `OIDC_DOMAIN`                                                                   | env var                                                             | key = `oidc_domain`                                                                                            |
| AI Factory team          | `UPSTREAM_BASE_URL` = `https://gateway.scaifactory.dev.azure.scbdev.net/v1`     | values.yaml static value                                            | N/A (public value)                                                                                             |
| AI Factory team          | Production-approved model ID list (at minimum includes `databricks/gdp-gpt4o`)  | values.yaml static value                                            | N/A (public value)                                                                                             |
| Platform / K8s team      | `kubectl` read-write rights on namespace `jhub`                                 | operator's local kubeconfig                                         | N/A                                                                                                            |
| Platform / K8s team      | Push rights to `registry.scbdev.net/gdp/`                                       | local docker config                                                 | N/A                                                                                                            |
| Network team             | Egress whitelist for IdP + AI Factory hostnames (singleuser Pod egress 443 TCP) | NSG / CNI                                                           | N/A                                                                                                            |

For credentials we **strongly recommend SealedSecret or ExternalSecret**; the snippet below shows a quick temporary method (**TEST-ENV ONLY — production MUST use SealedSecret!**):

```bash
# ---------------------------------------------------------------------------
# 4.0.1 — Create nbi-llm-auth Secret (TEST ENV only, smoke stage)
#         PRODUCTION: use SealedSecret / ExternalSecret, NEVER a plain Opaque
#         secret with literal passwords in YAML.
# ---------------------------------------------------------------------------
cd /Users/bl44001/gdp/repo/notebook-intelligence

# Place JAR + JKS files received from the Security team into a temp dir (never git-commit!)
ls -la ~/nbi-credentials-dir/
# -rw-------  1 alice  staff   15360 Sep 18 10:00 token-tool.jar
# -rw-------  1 alice  staff    4096 Sep 18 10:00 keystore.jks
# -rw-------  1 alice  staff  128000 Sep 18 10:00 aitruststore.jks

kubectl create namespace jhub --dry-run=client -o yaml | kubectl apply -f -

kubectl -n jhub create secret generic nbi-llm-auth \
  --from-file=token-tool.jar=~/nbi-credentials-dir/token-tool.jar \
  --from-file=keystore.jks=~/nbi-credentials-dir/keystore.jks \
  --from-file=aitruststore.jks=~/nbi-credentials-dir/aitruststore.jks \
  --from-literal=oidc_token_url='https://idp.scbdev.net/adfs/oauth2/v2.0/token' \
  --from-literal=oidc_client_code='REPLACE_WITH_SECURE_VALUE_FROM_IDP' \
  --from-literal=oidc_domain='scbdev.net' \
  --from-literal=jks_keystore_password='REPLACE_WITH_SECURE_VALUE' \
  --from-literal=jks_truststore_password='REPLACE_WITH_SECURE_VALUE' \
  --dry-run=client -o yaml | kubectl apply -f -
# → secret/nbi-llm-auth created (or configured)
kubectl -n jhub describe secret nbi-llm-auth
```

### 4.1 Build Images

```bash
cd /Users/bl44001/gdp/repo/notebook-intelligence

# (Recommended) Have a local minikube / rancher-desktop / docker-desktop with docker build enabled
TAG=rc.20260918.1    # Pick a meaningful tag; convention: rc.YYYYMMDD.N

# Build sidecar ONLY (most common case) — script FIRST runs py_compile on all 8 .py modules; syntax errors fail immediately.
IMAGES="nbi-auth-sidecar" TAG="${TAG}" ./local-dev/deploy/build-images.sh

# OR build all 3 images together (nbi-quota, nbi-singleuser, nbi-auth-sidecar):
TAG="${TAG}" ./local-dev/deploy/build-images.sh

# Expected output — you should see:
#   py_compile nbi_auth_sidecar package (8 modules) → py_compile OK
#   docker build -f Dockerfile -t nbi-auth-sidecar:rc.20260918.1 local-dev/deploy/nbi-auth-sidecar
#   ✓ 3 images built / sized
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}' \
  | grep -E 'REPOSITORY|nbi-auth-sidecar|nbi-singleuser'

# If shipping to production registry, re-tag and push
REGISTRY_PREFIX="registry.scbdev.net/gdp"
docker tag "nbi-auth-sidecar:${TAG}"   "${REGISTRY_PREFIX}/nbi-auth-sidecar:${TAG}"
docker tag "nbi-singleuser:${TAG}"     "${REGISTRY_PREFIX}/nbi-singleuser:${TAG}"
docker push "${REGISTRY_PREFIX}/nbi-auth-sidecar:${TAG}"
docker push "${REGISTRY_PREFIX}/nbi-singleuser:${TAG}"
```

**Fail-fast local validation (no real IdP required)**: Run the smoke test with MockTokenMinter.

```bash
cd local-dev/deploy/nbi-auth-sidecar
# Requires a running docker daemon. Needs NO JKS/JAR/OIDC (Mock mode).
./smoke-test.sh nbi-auth-sidecar:${TAG}

# Expected:
#   PASS T0..T11 (16 assertions)
#   ✅ ALL SMOKE ASSERTIONS PASSED
# If any FAIL: inspect the logs dir (output prints /var/folders/.../tmp/nbi-smoke.XXXXXX).
```

### 4.2 Helm Values Merge & Upgrade

```bash
cd /Users/bl44001/gdp/repo/notebook-intelligence

# Optional: run helm-template-test.sh locally first to validate shapes
./local-dev/deploy/helm-template-test.sh
# Output: [helm-test] SUCCESS — all AC-7 helm-template assertions passed.

# Prepare a site-specific values file (copy template, edit as needed):
cat > /tmp/my-site-values.yaml <<'EOF'
# /tmp/my-site-values.yaml
# Example: on top of the default nbi-auth-sidecar values, layer on:
#  (1) custom image tag (rc.20260918.1)
#  (2) larger sidecar memory limit (for memory-heavy token-tool.jar builds)
#  (3) multi-profile: data-scientist profile enables Claude chat

proxy:
  secretToken: "REPLACE_WITH_LONG_RANDOM_SECRET_MIN_32_CHARS_HEX_OR_BASE64"

hub:
  image:
    name: registry.scbdev.net/gdp/jupyterhub-k8s-hub
    tag: "3.3.0-scb1"
  config:
    JupyterHub:
      cookie_secret: "REPLACE_WITH_LONG_RANDOM_SECRET_MIN_32_CHARS_HEX_OR_BASE64"
  extraEnv:
    # Override default sidecar image — align with the tag we just pushed
    - name: NBI_AUTH_SIDECAR_IMAGE
      value: "registry.scbdev.net/gdp/nbi-auth-sidecar:rc.20260918.1"
    # Custom model
    - name: NBI_CHAT_MODEL_ID
      value: "databricks/gdp-gpt4o"

singleuser:
  image:
    name: registry.scbdev.net/gdp/jupyter-singleuser
    tag: "rc.20260918.1"
  nbiAuthSidecar:
    resources:
      requests:
        cpu: "100m"
        memory: "384Mi"
      limits:
        cpu: "750m"
        memory: "1Gi"
  profileList:
    - display_name: "Default (Small)"
      description: "2 CPU, 4 GiB, default AI Factory model"
      default: true
      kubespawner_override:
        cpu_guarantee: 1
        cpu_limit: 2
        mem_guarantee: "2G"
        mem_limit: "4G"
    - display_name: "Data Science (Large + Claude)"
      description: "8 CPU, 32 GiB, Claude chat on-demand"
      kubespawner_override:
        cpu_guarantee: 4
        cpu_limit: 8
        mem_guarantee: "16G"
        mem_limit: "32G"
        # spawner.environment is set BEFORE the hook runs; hook uses "existing key NOT overwrite"
        # semantic. Per-profile keys set here therefore override the global defaults.
        environment:
          NBI_CLAUDE_CHAT_MODEL: "anthropic.claude-sonnet-4-20250514-v1:0"
          NBI_CLAUDE_INLINE_COMPLETION_MODEL: "anthropic.claude-haiku-4-20250514-v1:0"
EOF

# 3-values-stack upgrade (order MATTERS: later files override earlier ones)
helm repo add jupyterhub https://jupyterhub.github.io/helm-chart/ || true
helm repo update

RELEASE_NAME="jhub"
RELEASE_NS="jhub"
Z2JK_VERSION="3.3.0"

helm upgrade -i "${RELEASE_NAME}" jupyterhub/jupyterhub \
  --version "${Z2JK_VERSION}" \
  --namespace "${RELEASE_NS}" \
  --create-namespace \
  --values local-dev/deploy/k8s/jupyterhub-values-nbi-auth-sidecar.yaml \
  --values /tmp/my-site-values.yaml \
  --timeout 10m \
  --wait

# Wait for Hub rollout:
kubectl -n jhub rollout status deploy/hub --timeout=300s
kubectl -n jhub rollout status deploy/proxy --timeout=120s

# Critical verification: Hub Pod's environ has NBI_AUTH_SIDECAR_IMAGE
HUB_POD=$(kubectl -n jhub get pod -l app=jupyterhub,component=hub -o jsonpath='{.items[0].metadata.name}')
echo "=== Hub pod: ${HUB_POD} ==="
kubectl -n jhub exec "${HUB_POD}" -- env | grep -E 'NBI_|NBI_AUTH_SIDECAR|OIDC_|ANTHROPIC_BASE_URL' | sort
```

### 4.3 Manually Spawn a Pod for Smoke

Method 1 (Browser): Visit `https://jhub.<company-domain.com>/hub/spawn` → Small default profile → Start Server.

Method 2 (CLI): Direct JupyterHub API spawn (requires admin token):

```bash
JUPYTERHUB_API_URL=https://jhub.scbdev.net/hub/api
JUPYTERHUB_ADMIN_TOKEN=$(kubectl -n jhub get secret hub -o jsonpath='{.data.services\.token}' | base64 -d)
TEST_USER="alice"

curl -sS -X POST \
  -H "Authorization: token ${JUPYTERHUB_ADMIN_TOKEN}" \
  "${JUPYTERHUB_API_URL}/users/${TEST_USER}/server" \
  | python3 -m json.tool
```

Wait for Pod Running:

```bash
watch -n 5 "kubectl -n jhub get pod -l hub.jupyter.org/username=${TEST_USER} -o wide"
# Expected: pod "jupyter-alice" transitions to Running, READY 2/2 (both containers ready)
```

### 4.4 Live E2E Validation (7 Checkpoints)

```bash
cd local-dev/deploy/nbi-auth-sidecar
export JHUB_NS=jhub
export POD_NAME=jupyter-alice     # replace with the Pod spawned above
./e2e-validation.sh
```

Expected output:

```
 E2E VALIDATION — pod=jupyter-alice namespace=jhub
   PASS: 7 / 7
✅ All 7 E2E checkpoints PASSED.
```

If one fails (most often CK1/CK4/CK5), run the targeted manual pinpoint:

```bash
# CK1 debug: inspect sidecar logs
kubectl -n jhub logs pod/jupyter-alice -c nbi-auth-sidecar --tail=100 | less

# CK4 debug: manually port-forward then hit reload
kubectl -n jhub port-forward pod/jupyter-alice 18888:8888 > /tmp/pf.log 2>&1 &
TOKEN=$(kubectl -n jhub exec -c notebook pod/jupyter-alice -- python3 -c '
import glob, json, os
rt = os.environ.get("JUPYTER_RUNTIME_DIR", os.path.expanduser("~/.local/share/jupyter/runtime"))
latest = sorted(glob.glob(f"{rt}/jpserver-*.json"))[-1]
print(json.load(open(latest)).get("token",""))
' 2>/dev/null)
curl -sS -X POST -G --data-urlencode "token=${TOKEN}" \
  -H "Authorization: token ${TOKEN}" \
  -H "Content-Type: application/json" --data '{"broadcast":true}' \
  http://127.0.0.1:18888/notebook-intelligence/reload-config
# Expected: {"ok":true, ...} instead of 404 / 403

# CK5 debug: direct grep every PID's cmdline
kubectl -n jhub exec -c nbi-auth-sidecar pod/jupyter-alice -- \
  python3 -c '
import os,re
for p in filter(str.isdigit, os.listdir("/proc")):
    try:
        b = open(f"/proc/{p}/cmdline","rb").read()
    except: continue
    if re.search(rb"(?i)password.{0,4}=", b):
        print("FOUND HIT in pid", p, repr(b[:200]))
'
# Expected: empty output → NO passwords on command line.
```

### 4.5 Release Gate (Checklist Filing)

Fill in the 15-row table from [e2e-checklist.md](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/e2e-checklist.md) → 15 PASS → 2 engineers sign → attach to internal Jira ticket GDP-NNNNN → approve release.

---

## Chapter 5: Troubleshooting

> Covers the 9 symptoms that make up 95% of production issues — symptom → most likely cause → fix / verify.

| #   | Symptom                                                                                                                         | Most likely cause                                                                                                                                                                                                                                                  | Fix / Verification                                                                                                                                                                                                          |
| --- | ------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| 1   | Pod never reaches Running on spawn; events show `Failed to pull image "registry.scbdev.net/gdp/nbi-auth-sidecar:rc.20260918.1"` | Wrong image tag / not pushed / per-node `imagePullSecrets` not configured for the registry.                                                                                                                                                                        | Try `docker pull` manually on a node. Verify the imagePullSecret is attached to the correct namespace.                                                                                                                      |
| 2   | Pod Running but READY 1/2 (notebook ready, sidecar never ready)                                                                 | Bootstrap timeout (90 s). Check sidecar logs. 99% of the time: wrong JAR/JKS path, wrong passwords, OIDC endpoint unreachable (egress not open).                                                                                                                   | `kubectl logs -c nbi-auth-sidecar <pod>` — look for lines tagged `[Mint ERROR]`; even after redaction you'll see "SSL handshake" / "invalid password" / "DNS fail" style keywords.                                          |
| 3   | Sidecar Ready, but NBI Settings panel opens with Provider=anthropic and empty fields                                            | Bootstrap wrote the file but reload endpoint never fired / monkey-patch never loaded into notebook container.                                                                                                                                                      | Inside the notebook container verify: `ls -la /etc/jupyter/jupyter_server_config_nbi_reload.py` exists; notebook container startup logs grepped for `reload-config` should contain `install_nbi_reload_features installed`. |
| 4   | Reload endpoint returns 200, but API Key in Settings is still old (401 on chat after 5-min rotate)                              | L3 Provider cache NOT invalidated → `/tmp/nbi-runtime-env.json` was not merged into environ OR ferry JSON mode is NOT 0600 → unreadable by notebook uid.                                                                                                           | E2E CK3 validates mode=600 + 8-key ordered list; manual: `kubectl exec -c notebook <pod> -- cat /tmp/nbi-runtime-env.json                                                                                                   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d), list(d.keys()) == [...expected list...])'`. |
| 5   | Occasional spawn: 2+ `nbi-auth-sidecar` entries in `extra_containers` (pre_spawn_hook re-entrant call)                          | Old hook build was missing the dedup filter; v1.0.0 `make_pre_spawn_hook` already has filter-then-append, so confirm hub.extraConfig in values.yaml references the NEW shipped code — NOT some external inline legacy copy of the hook.                            | Inspect Hub config: `kubectl -n jhub exec pod/hub -- grep -A 20 "deduped.append" srv/jupyterhub_config.py`.                                                                                                                 |
| 6   | Security compliance scanner reports: "sidecar container process opened 0.0.0.0 listener"                                        | Someone passed a custom host via helm, e.g. `--set ...NBI_SIDECAR_HTTP_HOST=0.0.0.0`; server.py's loopback guard SHOULD raise ValueError → CrashLoopBackOff. What the scanner catches is typically the notebook's 8888 (it must listen 0.0.0.0 — that's expected). | Run E2E CK7 on every release; keep the default `NBI_SIDECAR_HTTP_HOST=127.0.0.1` untouched.                                                                                                                                 |
| 7   | After hub upgrade (helm upgrade) old user sessions see "token expired" but newly-spawned users work fine                        | Old user Pods were spawned BEFORE upgrade; they carry the OLD sidecar image tag / OLD hook injection logic. **100% by design**: upgrade only affects _newly spawned_ Pods. Have the user do Stop My Server → Start My Server via JupyterHub control panel.         | Explain the "zero-downtime upgrade tradeoff" to users; not a bug.                                                                                                                                                           |
| 8   | Plaintext JWT visible in logs                                                                                                   | Redaction pattern gap; file a PR against redact.py to add the missing regex pattern + add a matching smoke-test case.                                                                                                                                              | Grep logs for `eyJ` → identify pid/path → PR the fix.                                                                                                                                                                       |
| 9   | After every rotate the user notebook pops a "Configuration saved" toast, even though Save was never clicked                     | Expected behaviour: WS `MCPServerStatusChange` broadcast reaches frontend → fetchCapabilities → React diff → Settings re-renders. DO NOT fix. This is PROOF the L5 refresh path is live!                                                                           | No action (feature, not a bug).                                                                                                                                                                                             |

---

## Chapter 6: Operations Runbook

### 6.1 Canary New Sidecar Version Flow (Low-Risk Upgrade)

Do NOT `helm upgrade` to a global rollout. Recommended: per-profile canary.

```yaml
# Add a canary profile inside site-values.yaml
singleuser:
  profileList:
    - display_name: 'Default (Stable)'
      default: true
      kubespawner_override:
        # defaults use the global stable tag from hub env
        environment: {}
    - display_name: 'Canary — Auth Sidecar RC'
      description: 'Rolls out new nbi-auth-sidecar for brave users'
      kubespawner_override:
        # hook does NOT overwrite existing spawner.environment keys,
        # but NBI_AUTH_SIDECAR_IMAGE is read at hub.extraConfig closure binding time.
        # For canary we therefore use the spawner-scoped override key the
        # pre_spawn_hook already understands: NBI_AUTH_SIDECAR_IMAGE_OVERRIDE
        environment:
          NBI_AUTH_SIDECAR_IMAGE_OVERRIDE: 'registry.scbdev.net/gdp/nbi-auth-sidecar:rc.20260918.1'
```

→ Ask 2–3 beta users to select the Canary profile for one business day. If all good then bump the global `NBI_AUTH_SIDECAR_IMAGE`.

### 6.2 Emergency Rollback (New version has a critical bug)

**Scenario**: v1.1.0 sidecar incorrectly overrides `base_url` to `localhost` — all users hit 404.

**Rollback steps** (1 command; already-running users are untouched):

```bash
# Rollback Hub (revert to v1.0.0 image tag)
helm upgrade -i jhub jupyterhub/jupyterhub --version 3.3.0 -n jhub \
  -f local-dev/deploy/k8s/jupyterhub-values-nbi-auth-sidecar.yaml \
  -f /tmp/my-site-values.yaml \
  --set hub.extraEnv[11].name=NBI_AUTH_SIDECAR_IMAGE \
  --set-string "hub.extraEnv[11].value=registry.scbdev.net/gdp/nbi-auth-sidecar:1.0.0"

# Running user Pods → continue with the OLD sidecar tag normally, NO restart required.
# Newly spawned users → get the 1.0.0 tag as expected.
```

### 6.3 One-Button Force-Immediate Refresh for a Specific User (on-demand)

```bash
POD_NAME=jupyter-alice
kubectl -n jhub port-forward pod/${POD_NAME} 18091:18090 > /tmp/pf.log 2>&1 &
PF_PID=$!
sleep 2
curl -sS -X POST http://127.0.0.1:18091/rotate-self
# → {"accepted":true,"reason":"on-demand via POST /rotate-self"}
kill $PF_PID 2>/dev/null
# Within ~5 seconds the user's Settings panel should refresh.
```

---

## Chapter 7: Acceptance Criteria (AC) & Evidence Matrix

| AC ID  | Description                                        | Validation method                                             | Evidence file                                            |
| ------ | -------------------------------------------------- | ------------------------------------------------------------- | -------------------------------------------------------- |
| AC-1   | Stdlib-only Python image (0 new pip packages)      | Dockerfile audit + `pip list` inside container                | smoke-test.sh T0 + docker inspect                        |
| AC-2   | Bootstrap + ready ≤ 90 s                           | 10× pod restarts, take p99                                    | e2e-validation.sh CK1 + smoke log                        |
| AC-3   | T-300 s refresh fires inside a 60 s window         | Mock TTL=40 s + REFRESH_BEFORE=35 s                           | smoke-test.sh T8b (2nd POST observed within 15 s window) |
| AC-4.1 | L1→L5 via HTTP endpoint ≤ 5 s                      | POST reload → poll capabilities                               | E2E CK4                                                  |
| AC-4.2 | L1→L5 via FS Poll fallback ≤ 10 s                  | Disable endpoint → fs write → poll capabilities               | smoke + E2E auxiliary                                    |
| AC-5.1 | Sidecar cmdline: 0 password hits                   | `/proc/*/cmdline` python grep                                 | E2E CK5                                                  |
| AC-5.2 | Notebook cmdline: 0 password hits                  | Same as above                                                 | E2E CK5 variant                                          |
| AC-6.1 | Hook idempotent: N invocations → exactly 1 sidecar | Python structural smoke (inline test in README §3.3)          | See §2.2 dedup filter snippet                            |
| AC-6.2 | Helm shape idempotent                              | helm upgrade twice → identical rendered spec                  | helm-template-test.sh                                    |
| AC-7.1 | Helm render produces ≥ 2 containers                | helm-template-test.sh AC-7.1                                  | helm-template-test.sh                                    |
| AC-7.2 | notebook envFrom secretRef + sidecar probe host    | helm-template-test.sh AC-7.2/3                                | helm-template-test.sh                                    |
| AC-8   | 18090 binds 127.0.0.1 only                         | socket bind probe to 0.0.0.0:18090 + ss/netstat               | smoke-test.sh T10 + E2E CK7                              |
| AC-9   | FS fallback reload when endpoint is DOWN           | Simulate fake jupyter kill → write file → capabilities change | smoke / E2E scenario supplement                          |
| AC-10  | 0 Chinese characters in code comment lines         | awk 3-byte UTF-8 Chinese range                                | Build-time grep step                                     |
| AC-11  | Handoff rubric ≥ 4/5 signed off by 2 engineers     | checklist #15 signature                                       | e2e-checklist.md (last line)                             |

---

## Chapter 8: Known Limitations & Roadmap

| Category           | Known limitation (v1.0.0)                                                                                                                     | Possible future evolution                                                                                                            |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| Refresh triggering | Only 2 paths (sidecar loop + FS poll). If AI Factory proactively revokes a token the sidecar only finds out at next T-5min cycle.             | Optional: extend scheduler to listen on a `/revoke` webhook (requires platform team to surface events from AI Factory).              |
| Multi-tenancy      | 1 sidecar serves the single pod user; concurrent multi-user runs inside the same Pod are not supported.                                       | Z2JK default is one-user-per-Pod so this is not a blocking gap.                                                                      |
| Resource footprint | JVM is the dominant consumer (256 Mi baseline). Future: native-image compile token-tool.jar OR replace Java with a Python JKS implementation. | Introducing token-tool-native (GraalVM) is estimated to bring sidecar memory down to the 64 Mi class.                                |
| Prometheus scrape  | `/metrics` is exposed but no companion PodMonitor / ServiceMonitor is shipped; cluster Prometheus won't auto-discover.                        | Next iteration: add a PodMonitor YAML inside deploy/k8s with a label selector matching the sidecar's `prometheus.io/scrape: "true"`. |
| Audit logging      | Only stdout JSON lines; no ELK / Kafka integration.                                                                                           | Adapt JSON shape per internal platform rules for centralized log ingestion.                                                          |

---

**Document End.** First thing to try on any issue: run `./smoke-test.sh` (fastest local feedback) and `./e2e-validation.sh` (most realistic cluster feedback) under `local-dev/deploy/nbi-auth-sidecar/`.

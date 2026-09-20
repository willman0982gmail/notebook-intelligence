# Notebook Intelligence Auth Sidecar — 设计方案与部署手册

**版本**: 1.0.0 (2026-09-18)
**作者**: GDP Data Platform
**适用范围**: SCB GDP Notebook Intelligence 企业版（基于 Zero-to-JupyterHub v3.3.0 部署）
**阅读对象**:

- Platform / K8s 运维（负责镜像构建与 Helm upgrade）
- Identity / Security 团队（负责 JKS / JAR / OIDC 凭据发放与校验）
- NBI 研发（负责 sidecar 代码变更与故障排查）
  **配套文件**:
- 源代码目录: [local-dev/deploy/nbi-auth-sidecar/](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar)
- Hub Spawn Hook: [local-dev/hub/pre_spawn_hook.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/hub/pre_spawn_hook.py)
- Jupyter Server Monkey-patch: [local-dev/deploy/jupyter_server_config_nbi_reload.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/jupyter_server_config_nbi_reload.py)
- JupyterHub values overlay: [jupyterhub-values-nbi-auth-sidecar.yaml](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/k8s/jupyterhub-values-nbi-auth-sidecar.yaml)
- Helm 形状断言脚本: [helm-template-test.sh](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/helm-template-test.sh)
- 构建脚本: [local-dev/deploy/build-images.sh](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/build-images.sh)
- E2E 脚本与 Checklist: [local-dev/deploy/nbi-auth-sidecar/](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar) (smoke-test.sh, e2e-validation.sh, e2e-checklist.md)

---

## 第 0 章: 执行摘要 (TL;DR)

> **一句话方案**: 给每个启动的 Jupyter singleuser Pod **追加一个 sidecar 容器**，它在 Pod 启动 90 秒内同步完成 AI Factory JWT 首签 → 原子写入 NBI 配置文件 → 通过 HTTP endpoint 触发 NBI 5 层缓存全量刷新 → 广播 WebSocket 消息让所有浏览器 tab 自动刷新 Settings 面板。之后每 5 分钟（`exp - 300 s`）自动循环刷新，token 过期前保证换新。用户**零配置 / 零感知**。

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

## 第 1 章: 背景与目标

### 1.1 为什么要做 Auth Sidecar

**历史状态（问题）**:

1. 每个用户手动打开 NBI Settings 页面，粘贴 AI Factory token → 配置错误率高、用户体验差、培训成本高。
2. AI Factory JWT TTL ≈ 30–60 min。用户做长任务时中途 token 过期，下一次 chat completion 报 `401 Unauthorized` — 用户不知道为什么失败，只能重新去 Settings 粘一个新 token。
3. 手工 rotate 脚本写了直接改 `config.json`，但 NBI 有 5 层缓存（见 §2.4），写完界面不生效。
4. 安全合规要求：JKS 密码**禁止**出现在命令行参数 (`/proc/<pid>/cmdline`)、环境变量 dump、或日志中。

**本方案（解决）**:

- **零用户配置**: spawn 之后 settings 自动填充，不用点 Save。
- **过期前 5 min 自动 rotate**: 无感知热更新。
- **全链路安全**: JKS 密码只出现在 mode 0600 的临时文件，unlink 在所有异常路径。
- **无需改 NBI 源码**: 通过 Jupyter Server 标准的 `post_open_app_callbacks` 注入 + Python hook 注入 sidecar，不侵入 notebook_intelligence package。

### 1.2 目标与非目标

| **目标** (In Scope)               | **非目标** (Out of Scope)                              |
| --------------------------------- | ------------------------------------------------------ |
| 自动首签 + T-5min rotate          | 引入 Admission Webhook / Mutating Admission Controller |
| NBI 5-layer cache chain 刷新      | 建立新的 OAuth/OIDC dance 浏览器端                     |
| 127.0.0.1 sidecar HTTP 仅本地监听 | 把 sidecar 暴露为集群内 Service                        |
| Docker image ≤ 500 MB 非压缩      | 多集群 federation / service mesh 集成                  |
| Helm overlay 合并 Z2JK            | 修改 NBI 源代码包                                      |

---

## 第 2 章: 系统架构与核心机制

### 2.1 两容器 Sidecar 拓扑

每个 singleuser Pod 内包含 2 个容器（由 [make_pre_spawn_hook()](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/hub/pre_spawn_hook.py#L118-L463) 在 spawn 前动态注入）：

| 容器名 `name:`                     | 镜像                                                               | 启动命令                                      | 作用                                                  |
| ---------------------------------- | ------------------------------------------------------------------ | --------------------------------------------- | ----------------------------------------------------- |
| `notebook` (upstream Z2JK default) | `nbi-singleuser:<tag>`（内置 jupyter server + NBI + monkey-patch） | `wrapper.sh → jupyter labhub`                 | 用户实际使用的 Notebook。                             |
| `nbi-auth-sidecar`                 | `nbi-auth-sidecar:<tag>`                                           | `/usr/bin/tini -- python -m nbi_auth_sidecar` | 独立的 JWT mint + 定时 rotate + 热刷新 orchestrator。 |

**为什么用 sidecar 而不是把逻辑塞到 notebook 容器里？**

1. **故障域隔离**：rotate 崩溃（JVM crash、JKS 损坏）影响 sidecar 但不影响 Jupyter server 进程。K8s 重启 sidecar container 就够，用户 session 不丢。
2. **权限分层**：JKS/JAR/密码只 mount 到 sidecar，notebook 进程的 shell / 终端看不到 credential 文件。
3. **独立升级**：只换 image tag 重启 Hub，新 spawn 的用户拿到新 sidecar；已运行的用户不受影响（零停机升级）。
4. **独立资源配额**：JVM heap 需要 256–768 Mi；如果塞进 notebook，整个 notebook 的 memory limit 就被 JVM 推高，用户会感觉"明明没跑代码但内存用了 1 G"。

### 2.2 注入点: KubeSpawner Hook (无侵入)

Z2JK 原生 traitlet `c.KubeSpawner.pre_spawn_hook` 接受一个可调用对象（同步或 async），在 Hub 创建 Pod spec 之前、拿到 user auth state 之后被调用。

我们在 [jupyterhub-values-nbi-auth-sidecar.yaml hub.extraConfig 000-nbi-auth-sidecar-hook.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/k8s/jupyterhub-values-nbi-auth-sidecar.yaml#L94-L164) 内注册：

```python
try:
    from hub.pre_spawn_hook import make_pre_spawn_hook
except Exception:
    # ★ graceful degradation: 导入失败不阻止 hub 启动, 仅 log WARNING
    # 用户仍可手动配置 Settings.
    log.warning("sidecar hook module missing — skipping sidecar injection")
else:
    if _nbi_sidecar_enabled():  # env flag NBI_AUTH_SIDECAR_ENABLE = true/false
        c.KubeSpawner.pre_spawn_hook = make_pre_spawn_hook(
            nbi_auth_sidecar_config=_sidecar_cfg,
        )
```

**注入动作清单**（6 步，见 `_inject_nbi_auth_sidecar()` 闭包）:

1. **2 个 Volumes**:
   - `EmptyDir` `nbi-shared-tmp` → 挂到 `/tmp`，两容器共享（ferry JSON、state JSON、mock 脚本）。
   - `Secret` `nbi-llm-auth` → defaultMode=0400，sidecar 挂到 `/var/run/nbi-llm-auth/`。
2. **Notebook 容器 `envFrom`**: 追加 `secretRef: nbi-llm-auth`（只从 secret 注入非密码的静态参数；密码通过 secret 同名 env var 走 K8s envFrom，被 sidecar 进程读取用于 properties 写入，绝不会出现在 CLI）。
3. **Notebook 容器 volume_mount**: `/tmp` 共享。
4. **Spawner.environment** 写入 **7 个 provider 级默认值**（不含 `ANTHROPIC_API_KEY`，API Key 只能从 sidecar ferry JSON 写）：
   - `NBI_CHAT_MODEL_PROVIDER=openai_compatible`
   - `NBI_CHAT_MODEL_ID=databricks/gdp-gpt4o`
   - `NBI_INLINE_COMPLETION_MODEL_PROVIDER=openai_compatible`
   - `NBI_INLINE_COMPLETION_MODEL_ID=databricks/gdp-gpt4o`
   - `NBI_CLAUDE_CHAT_MODEL=""`
   - `NBI_CLAUDE_INLINE_COMPLETION_MODEL=""`
   - `ANTHROPIC_BASE_URL=https://gateway.scaifactory.dev.azure.scbdev.net/v1`
5. **Sidecar container dict 构造**: image、env、envFrom、volumeMounts、resources（50m/256Mi→500m/768Mi）、securityContext（runAsNonRoot + UID 1000 + ALL caps drop + seccomp RuntimeDefault）。
6. **Probes**: liveness `/healthz` initialDelay=10，readiness `/ready` initialDelay=15，**显式 `httpGet.host: 127.0.0.1` ★ hard rule**。

**幂等性保证**:

```python
old_list = list(getattr(spawner, "extra_containers", None) or [])
deduped = [c for c in old_list if not (isinstance(c, dict) and c.get("name") == "nbi-auth-sidecar")]
deduped.append(sidecar_container)
spawner.extra_containers = deduped
```

→ N 次调用永远只有 exactly 1 个 sidecar container entry。Volumes、envFrom、volume_mounts、environment 都用同样的 filter-then-append pattern。

### 2.3 Scheduler 4 步循环

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
                · ~/.jupyter/nbi/config.json  (atomic_write_json matching NBI native)
                · /tmp/nbi-runtime-env.json   (STRING_OVERRIDE 8 keys, mode 0600)
      step 3: POST reload-config authenticated (broadcast:true body)
              3 retry w/ exponential backoff, 401=permanent fail, connect=transient
      step 4: PERSIST scheduler state v1 to /tmp/nbi-token-state.json
                (version:1, ok, token_len, exp, last_refresh, consecutive_mint_failures
                 ★ NEVER contains raw_token, password, eyJ* JWT blob literals)
```

### 2.4 NBI 5 层缓存 + 热刷新两条路径 (关键!)

> 这是整个方案中最容易"看起来 token rotate 了实际 chat 还 401"的部分。务必吃透。

**5 层缓存链** (从最慢到最快，每一层都比上一层离用户近):

```
L1 Disk:                 ~/.jupyter/nbi/config.json  ← script/disk write touches only this
L2 Python dict:          NBIConfig._data              nbi_config.load() reads L1 → L2
L3 Provider instances:   AIServiceManager._providers[provider].api_key (set_property_value)
L4 TS fetchCapabilities: NBIAPI._cachedCapabilities   in browser TS code, re-apply STRING_OVERRIDES
L5 React state:          Settings panel state + Chat panel provider ref  (held in React hooks)
```

**为什么直接写 `config.json` 不生效**？脚本绕开 ConfigHandler.post() 走，后面 4 步 `load → update_models_from_config → set_property_value → WS broadcast` 全没跑，所以 L3/L4/L5 全旧。

**我们用双路径保证 L1→L5 传播**:

#### ⚡ Path A (Fast, HTTP reload endpoint) — Primary

Sidecar step 3 做：`POST 127.0.0.1:8888/notebook-intelligence/reload-config`（monkey-patch 注入的 handler，见 §3.4 jupyter_server_config_nbi_reload.py）。Handler 严格按顺序：

1. **先 merge ferry JSON 入 os.environ** → 下一次 config access 时 `apply_string_overrides()` 重新把 8 个 key 打进去，L3 Provider 属性 stale cache 对 STRING_OVERRIDE env 失效。
2. **再调用 nbi_config.load()** → L1→L2。
3. **再调用 ai_service_manager.update_models_from_config()** → 销毁并重建 Provider，调用 `set_property_value("api_key", "newtoken")`，解决 L3。
4. **最后 broadcast WS `MCPServerStatusChange`** → 前端硬编码地收到这个消息就调 `fetchCapabilities()`，重拉 capabilities → L4/L5 刷新。

#### 🛟 Path B (Fallback, FS Poll) — Secondary / Fail-safe

Notebook 容器内的 monkey-patch 额外启动一条 daemon thread，每 5 秒跑：

```python
def _current_signature(self) -> (dev, ino, size, mtime_ns):
    st = os.stat(config_json_path)  # /home/jovyan/.jupyter/nbi/config.json
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

if sig != last_sig:
    do_reload()  # same step 1..4 as Path A!
```

→ 4 元签名：dev（跨 remount）+ ino（atomic_write_json 用 os.replace，必然变 inode）+ size + mtime_ns（同 inode 覆盖时变）。任何一种写入模式（原子 / 非原子）都不会漏检。

**组合效果**：Path A 秒级；如果 Path A 的 monkey-patch 被某个 custom image 意外移除（或 endpoint 404），Path B 5 s 内兜底。

### 2.5 JKS 密码安全 (Hard Rule AC-5)

> 项目硬约束：**禁止**通过 CLI `-Dxxx=PASSWORD` 或 Java `-Djavax.net.ssl.keyStorePassword=...` 传密码，避免暴露在 `/proc/<pid>/cmdline`。`SecureJarTokenMinter` 用 0600 tmpfile 配合 `-Djava.security.properties=<PATH>` 解决。

[nbi_auth_sidecar/mint.py SecureJarTokenMinter](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/mint.py) 关键段:

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
    # STEP 4: Always unlink — even CalledProcessError / TimeoutExpired / KeyboardInterrupt
    try:
        os.unlink(props_path)
    except OSError:
        pass
```

→ 检查: `/proc/<java_pid>/cmdline` 只有 `-Djava.security.properties=file:/tmp/jsec_XXXX.properties`，零密码字符串。

### 2.6 STRING_OVERRIDE_SPEC 8 键顺序 (不可随便改！)

NBI extension 在 `@property` 层定义了 8 个 env var，**每次访问 chat/inline config 属性时都会从 environ 重读**（绕过 L3 cache）。Sidecar 写入 `/tmp/nbi-runtime-env.json` 的 8 key 必须按固定顺序，方便 diff 和审计：

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

→ 文件权限强制 0600（只 jovyan 可读），见 [config_writer.py write_all_from_token](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/config_writer.py)。

### 2.7 Sidecar HTTP 接口 (port 18090 / 127.0.0.1 ONLY)

[nbi_auth_sidecar/server.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/server.py)

| Method | Path           | 用途                              | 返回字段                                                                                                                                                                |
| ------ | -------------- | --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| GET    | `/healthz`     | K8s liveness + ad-hoc 健康检查    | `{pid, token_ttl_seconds, ready, last_refresh_ts_seconds, refresh_count, consecutive_mint_failures}`                                                                    |
| GET    | `/ready`       | K8s readiness (首签成功后才 200)  | `{ready, reason}` 412 if not ready                                                                                                                                      |
| GET    | `/metrics`     | Prometheus 文本格式（8 families） | nbi_auth_sidecar_info, refresh_success_total, refresh_attempt_total, consecutive_mint_failures, token_ttl_seconds, last_refresh_ts_seconds, ready, http_active_requests |
| POST   | `/rotate-self` | 手动触发立即刷新 (on-demand)      | `{accepted: true, reason}` 202 Accepted                                                                                                                                 |

**★ BoundedThreadingHTTPServer**：用 Semaphore(8) 限制最大并发线程，防止 ulimit nproc 被打爆（符合项目工程规范）。

**★ Hard rule — loopback bind only**：启动前执行 `_assert_host_is_loopback(host)`。任何尝试传 `0.0.0.0` / pod IP / hostname 的行为都会直接 `raise ValueError`，拒绝启动。K8s probe 因为是在容器内执行，必须显式 `host: 127.0.0.1`，这正是我们在 pre_spawn_hook 构造的 probe dict 里写死的。

---

## 第 3 章: 文件清单与核心代码参考

### 3.1 Python 模块 (8 files, stdlib-only — zero pip)

| File                                                                                                                                                 | 作用                            | 核心对象/方法                                                                                                                                                                                                         |
| ---------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [mint.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/mint.py)                           | Token 签发实现                  | `AccessToken` (dataclass), `TokenMinter` (Protocol), `MockTokenMinter`, `SecureJarTokenMinter` (0600 properties), `RetryingTokenMinter` (exponential backoff 5×), `build_token_minter()` factory                      |
| [config_writer.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/config_writer.py)         | 原子写 NBI 配置 + ferry JSON    | `atomic_write_json()` (tempfile+fsync+chmod+os.replace+dir_fsync matches NBI native exactly), `write_all_from_token()` facade                                                                                         |
| [scheduler.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/scheduler.py)                 | 调度循环 + state v1 persistence | `SchedulerState` dataclass, `compute_next_run()`, `_sleep_interruptible(tick=10)`, `do_refresh_cycle()`, `bootstrap(max_wait=90)`, `run_forever()` daemon thread, trigger_refresh_now Event                           |
| [server.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/server.py)                       | 127.0.0.1 HTTP 控制面           | `BoundedThreadingHTTPServer` (Semaphore 8), `SidecarRequestHandler` routes for /healthz, /ready, /metrics (Prometheus text exposition), /rotate-self                                                                  |
| [nbi_reload_client.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/nbi_reload_client.py) | 发现并调用 reload-config 端点   | `discover_jupyter_runtime(timeout=60s)` reads `jpserver-*.json`, `DefaultNBIReloadClient.post_reload(broadcast=True)` 3 retry, dual auth                                                                              |
| [redaction.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/redaction.py)                 | 日志脱敏                        | 4 regex patterns: Bearer header, ENV assign secret-like key, Java `-D` password flags, bare 3-part eyJ JWT blob with min length gate. `redact_secrets()` str + `redact_bytes()` bytes-safe                            |
| [**main**.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/__main__.py)                   | 入口 wiring + signal handler    | `Options` env dataclass, `_configure_logging()`, `_install_signal_handlers()` SIGTERM/SIGINT → daemon thread schedules shutdown (**avoid same-thread deadlock on server.shutdown()**), `main()` returns int exit code |
| [**init**.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/nbi_auth_sidecar/__init__.py)                   | Package metadata                | `__version__ = "1.0.0"`                                                                                                                                                                                               |

### 3.2 Notebook 容器 Monkey-patch (1 file)

- [jupyter_server_config_nbi_reload.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/jupyter_server_config_nbi_reload.py) — 主要入口 `c.ServerApp.post_open_app_callbacks.append(install_nbi_reload_features)`:
  - `merge_runtime_env_into_environ('/tmp/nbi-runtime-env.json')`: **每次 reload 之前把 ferry JSON 的 8 keys 写入 os.environ**
  - `_locate_ai_service_manager()`: 3-fallback (ext_manager → NBI globals → gc scan by class name)，因为 ai_service_manager 在不同 NBI 版本的全局引用名字可能变；这使得 monkey-patch 对 NBI 升级弹性更好。
  - `do_reload()` 严格顺序: merge_env FIRST → load → update_models_from_config → broadcast。
  - `ReloadConfigHandler`: `@web.authenticated` 受保护 route，Content-Length ≤ 64 KiB，body `{"broadcast":true}`。
  - `ConfigFilePoller`: daemon thread loop, 5 s tick, 4-tuple stat signature check。

### 3.3 Hub Spawn Hook (1 file)

- [pre_spawn_hook.py](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/hub/pre_spawn_hook.py) — `make_pre_spawn_hook(*, nbi_auth_sidecar_config=None)` factory；`None` 时 **完全跳过注入** (向后兼容)。

### 3.4 Dockerfiles (2 + 1)

| File                                                                                                                 | 位置              | Base image                                                                                          | 大小 (非压缩, 目标)       |
| -------------------------------------------------------------------------------------------------------------------- | ----------------- | --------------------------------------------------------------------------------------------------- | ------------------------- |
| [Dockerfile.singleuser](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/Dockerfile.singleuser) | local-dev/deploy/ | scb 标准 singleuser                                                                                 | 增加 ~50 KB (python 文件) |
| [Dockerfile](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/Dockerfile)      | nbi-auth-sidecar/ | python:3.11-slim-bookworm + openjdk-17-jre-headless + tini + curl + ca-certificates, USER 1000:1000 | ≤ 500 MB (目标)           |

### 3.5 Build / Test / E2E 脚本 (5 files)

| 脚本                                                                                                                          | 位置              | 作用                                                                                                                                                                     |
| ----------------------------------------------------------------------------------------------------------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [build-images.sh](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/build-images.sh)                      | deploy/           | `IMAGES=${targets}` selector, **pre-step `py_compile` fail-fast**, then docker build. 3 targets: nbi-quota / nbi-singleuser / nbi-auth-sidecar。                         |
| [smoke-test.sh](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/smoke-test.sh)         | nbi-auth-sidecar/ | 本地 docker run + fake jupyter server (stdlib http.server)，测 16 个 assertions，覆盖 bootstrap、/rotate-self、dual auth、mode 0600、metrics 8 families、clean SIGTERM。 |
| [helm-template-test.sh](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/helm-template-test.sh)          | deploy/           | AC-7 helm shape 断言。helm+yq 存在就走 helm render，否则 Python fallback 纯 shape-check。                                                                                |
| [e2e-validation.sh](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/e2e-validation.sh) | nbi-auth-sidecar/ | 7 live K8s checkpoints，需要 kubectl 当前 context 指向集群 + 一个已经在跑的用户 Pod。                                                                                    |
| [e2e-checklist.md](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/e2e-checklist.md)   | nbi-auth-sidecar/ | 15 行 Release Gate 检查表，11 AC 全覆盖。                                                                                                                                |

---

## 第 4 章: 部署步骤 (详细 Command-by-Command)

### 4.0 前置条件 Checklist

在开始前 **4 个外部团队必须交付以下内容**（本 repo 不保存凭据！这些东西 live in K8s Secret `nbi-llm-auth` namespace jhub）：

| 交付方                   | 交付物                                                                      | 放置位置                                                        | Secret key 名                                                                                           |
| ------------------------ | --------------------------------------------------------------------------- | --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| Identity / Security 团队 | `token-tool.jar` (公司标准 AI Factory JWT 签发工具)                         | `/var/run/nbi-llm-auth/token-tool.jar` inside sidecar container | 用 `kubectl create secret generic --from-file` 二进制打包, key name = `token-tool.jar` (直接引用文件名) |
| Identity / Security 团队 | `keystore.jks` (私钥/客户端证书, PKCS12)                                    | `/var/run/nbi-llm-auth/keystore.jks`                            | key name = `keystore.jks`                                                                               |
| Identity / Security 团队 | `aitruststore.jks` (CA trust anchors, JKS)                                  | `/var/run/nbi-llm-auth/aitruststore.jks`                        | key name = `aitruststore.jks`                                                                           |
| Identity / Security 团队 | keystore 密码 (ASCII 字符串, ≥12 chars)                                     | K8s envFrom → env var `JKS_KEYSTORE_PASSWORD`                   | key = `jks_keystore_password`                                                                           |
| Identity / Security 团队 | truststore 密码 (ASCII 字符串, ≥12 chars)                                   | K8s envFrom → env var `JKS_TRUSTSTORE_PASSWORD`                 | key = `jks_truststore_password`                                                                         |
| Identity / Security 团队 | `OIDC_TOKEN_URL` (https://idp.xxx/.../token)                                | env var                                                         | key = `oidc_token_url`                                                                                  |
| Identity / Security 团队 | `OIDC_CLIENT_CODE`                                                          | env var                                                         | key = `oidc_client_code`                                                                                |
| Identity / Security 团队 | `OIDC_DOMAIN`                                                               | env var                                                         | key = `oidc_domain`                                                                                     |
| AI Factory 团队          | `UPSTREAM_BASE_URL` = `https://gateway.scaifactory.dev.azure.scbdev.net/v1` | values.yaml 静态值                                              | N/A (公开)                                                                                              |
| AI Factory 团队          | 生产可用的模型 ID 清单 (至少包含 `databricks/gdp-gpt4o`)                    | values.yaml 静态值                                              | N/A (公开)                                                                                              |
| Platform / K8s 团队      | `kubectl` 对 namespace `jhub` 的读写权限                                    | 本地运行者的 kubeconfig                                         | N/A                                                                                                     |
| Platform / K8s 团队      | `registry.scbdev.net/gdp/` 的 push 权限                                     | 本地 docker config                                              | N/A                                                                                                     |
| Network 团队             | IdP + AI Factory 域名的 egress 白名单 (singleuser Pod 出方向 443 TCP)       | NSG / CNI                                                       | N/A                                                                                                     |

推荐使用 **SealedSecret** 或 **ExternalSecret** 管理凭据；下面快速演示临时方式（**仅限测试环境，生产必须用 SealedSecret！**）：

```bash
# ---------------------------------------------------------------------------
# 4.0.1 — Create nbi-llm-auth Secret (测试环境, 仅用于 smoke 阶段)
#         PRODUCTION: use SealedSecret / ExternalSecret, NEVER plain Opaque with
#         literal passwords in YAML.
# ---------------------------------------------------------------------------
cd /Users/bl44001/gdp/repo/notebook-intelligence

# 从 Security 团队拿到的 JAR + JKS 文件放在临时目录 (不要提交 git!)
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

### 4.1 构建镜像 (Build)

```bash
cd /Users/bl44001/gdp/repo/notebook-intelligence

# (Recommended) 安装本地 minikube / rancher-desktop / docker-desktop，保证能 docker build
TAG=rc.20260918.1    # 选一个有辨识度的 tag, 建议 rc.YYYYMMDD.N

# 只 build sidecar（最常用） — 注意脚本会先跑 py_compile 全部 8 个 .py，语法错会立即 fail。
IMAGES="nbi-auth-sidecar" TAG="${TAG}" ./local-dev/deploy/build-images.sh

# 或者全量 3 个镜像 (nbi-quota, nbi-singleuser, nbi-auth-sidecar):
TAG="${TAG}" ./local-dev/deploy/build-images.sh

# Result — should see:
#   py_compile nbi_auth_sidecar package (8 modules) → py_compile OK
#   docker build -f Dockerfile -t nbi-auth-sidecar:rc.20260918.1 local-dev/deploy/nbi-auth-sidecar
#   ✓ 3 images built / sized
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}' \
  | grep -E 'REPOSITORY|nbi-auth-sidecar|nbi-singleuser'

# 如果是生产 registry，重打 tag + push
REGISTRY_PREFIX="registry.scbdev.net/gdp"
docker tag "nbi-auth-sidecar:${TAG}"   "${REGISTRY_PREFIX}/nbi-auth-sidecar:${TAG}"
docker tag "nbi-singleuser:${TAG}"     "${REGISTRY_PREFIX}/nbi-singleuser:${TAG}"
docker push "${REGISTRY_PREFIX}/nbi-auth-sidecar:${TAG}"
docker push "${REGISTRY_PREFIX}/nbi-singleuser:${TAG}"
```

**Fail-fast 验证 (本地, 不连真实 IdP)**：用 MockTokenMinter 跑 smoke test。

```bash
cd local-dev/deploy/nbi-auth-sidecar
# 要求 docker daemon running. 用不到 JKS/JAR/OIDC (Mock 模式).
./smoke-test.sh nbi-auth-sidecar:${TAG}

# 期望输出:
#   PASS T0..T11 (16 assertions)
#   ✅ ALL SMOKE ASSERTIONS PASSED
# 如果有任何 FAIL: 看 logs dir (输出中会打印 /var/folders/.../tmp/nbi-smoke.XXXXXX)。
```

### 4.2 Helm Values 合并 & 升级 (Upgrade)

```bash
cd /Users/bl44001/gdp/repo/notebook-intelligence

# 可选: 先本地跑 helm-template-test.sh 验证 shapes
./local-dev/deploy/helm-template-test.sh
# 输出: [helm-test] SUCCESS — all AC-7 helm-template assertions passed.

# 准备 site-specific values 文件 (copy template, 按需修改):
cat > /tmp/my-site-values.yaml <<'EOF'
# /tmp/my-site-values.yaml
# 示例：在默认的 nbi-auth-sidecar values 之上，叠加:
#  (1) 自定义镜像 tag (rc.20260918.1)
#  (2) 更大的 sidecar memory limit （应对特别吃内存的 token-tool.jar 版本）
#  (3) 多 profile: data-scientist profile 启用 Claude chat

hub:
  image:
    name: registry.scbdev.net/gdp/jupyterhub-k8s-hub
    tag: "3.3.0-scb1"
  extraEnv:
    # 覆盖默认 sidecar image — 与我们刚 push 的 tag 对齐
    - name: NBI_AUTH_SIDECAR_IMAGE
      value: "registry.scbdev.net/gdp/nbi-auth-sidecar:rc.20260918.1"
    # 自定义模型
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
        # spawner.environment 会在 hook 之前设置；hook 逻辑是 "existing key NOT overwrite"
        # 所以 profile 级设置在这里就能覆盖全局默认值
        environment:
          NBI_CLAUDE_CHAT_MODEL: "anthropic.claude-sonnet-4-20250514-v1:0"
          NBI_CLAUDE_INLINE_COMPLETION_MODEL: "anthropic.claude-haiku-4-20250514-v1:0"

proxy:
  secretToken: "REPLACE_WITH_LONG_RANDOM_SECRET_MIN_32_CHARS_HEX_OR_BASE64"

hub:
  config:
    JupyterHub:
      cookie_secret: "REPLACE_WITH_LONG_RANDOM_SECRET_MIN_32_CHARS_HEX_OR_BASE64"
EOF

# 3-values-stack 升级 (顺序很重要, 后者覆盖前者):
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

# 等待 Hub rollout:
kubectl -n jhub rollout status deploy/hub --timeout=300s
kubectl -n jhub rollout status deploy/proxy --timeout=120s

# 关键验证: Hub Pod 的 env 里有 NBI_AUTH_SIDECAR_IMAGE
HUB_POD=$(kubectl -n jhub get pod -l app=jupyterhub,component=hub -o jsonpath='{.items[0].metadata.name}')
echo "=== Hub pod: ${HUB_POD} ==="
kubectl -n jhub exec "${HUB_POD}" -- env | grep -E 'NBI_|NBI_AUTH_SIDECAR|OIDC_|ANTHROPIC_BASE_URL' | sort
```

### 4.3 手动 spawn 一个 Pod 进行冒烟

方法 1 (浏览器): 打开 `https://jhub.<company-domain.com>/hub/spawn` → 默认 Small profile → Start Server。

方法 2 (CLI): 直接调用 JupyterHub API spawn (需要 admin token):

```bash
JUPYTERHUB_API_URL=https://jhub.scbdev.net/hub/api
JUPYTERHUB_ADMIN_TOKEN=$(kubectl -n jhub get secret hub -o jsonpath='{.data.services\.token}' | base64 -d)
TEST_USER="alice"

curl -sS -X POST \
  -H "Authorization: token ${JUPYTERHUB_ADMIN_TOKEN}" \
  "${JUPYTERHUB_API_URL}/users/${TEST_USER}/server" \
  | python3 -m json.tool
```

等待 Pod Running:

```bash
watch -n 5 "kubectl -n jhub get pod -l hub.jupyter.org/username=${TEST_USER} -o wide"
# 期望: pod "jupyter-alice" 变为 Running, READY 2/2 (两个容器都 ready)
```

### 4.4 Live E2E 验证 (E2E 7 Checkpoints)

```bash
cd local-dev/deploy/nbi-auth-sidecar
export JHUB_NS=jhub
export POD_NAME=jupyter-alice     # 替换为上面 spawn 的 Pod
./e2e-validation.sh
```

期望输出：

```
 E2E VALIDATION — pod=jupyter-alice namespace=jhub
   PASS: 7 / 7
✅ All 7 E2E checkpoints PASSED.
```

如果某一条 FAIL（通常就是 CK1/CK4/CK5），直接跑对应项手工定位：

```bash
# CK1 debug: 看 sidecar 日志
kubectl -n jhub logs pod/jupyter-alice -c nbi-auth-sidecar --tail=100 | less

# CK4 debug: 手动 port-forward 后打 reload
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
# 期望: {"ok":true, ...} 而不是 404 / 403

# CK5 debug: 直接 grep 所有 PIDs cmdline
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
# 期望: 空输出 → 没有密码在命令行上.
```

### 4.5 Release Gate (Checklist 归档)

填 [e2e-checklist.md](file:///Users/bl44001/gdp/repo/notebook-intelligence/local-dev/deploy/nbi-auth-sidecar/e2e-checklist.md) 的 15 行 table → 15 PASS → 2 工程师签署 → 贴到内部 Jira GDP-NNNNN 工单上 → 放行。

---

## 第 5 章: 故障排查 (Troubleshooting)

> 常见 95% 问题对应 9 种症状 → 原因 → 修复表。

| #   | 症状                                                                                                                     | 最可能原因                                                                                                                                                                                                                        | 修复 / 验证                                                                                                                                                                       |
| --- | ------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| 1   | Spawn 时 Pod never reach Running, 事件里 `Failed to pull image "registry.scbdev.net/gdp/nbi-auth-sidecar:rc.20260918.1"` | Image tag 写错 / 没 push / 单节点的 imagePullSecrets 没配                                                                                                                                                                         | `docker pull` 手动在节点上拉一次；检查 secret 是否对 namespace 正确。                                                                                                             |
| 2   | Pod Running 但 READY 1/2 (notebook 就绪, sidecar 永远 not ready)                                                         | Bootstrap 超时 (90 s)。查看 sidecar logs，大概率是 JAR/JKS 路径错、密码错、OIDC endpoint 连不上 (egress 没开)                                                                                                                     | `kubectl logs -c nbi-auth-sidecar <pod>` 找 `[Mint ERROR]` 那一行；redact 后仍可看到 "SSL handshake" / "invalid password" / "DNS fail" 类型的关键词。                             |
| 3   | Sidecar Ready，但打开 NBI Settings 面板依旧 Provider=anthropic 且空                                                      | Boot strap 写了文件但 reload endpoint 没触发 / monkey-patch 没加载。                                                                                                                                                              | 验证 notebook 容器: `ls -la /etc/jupyter/jupyter_server_config_nbi_reload.py` 存在；notebook 容器启动日志 grep `reload-config` 应该看到 "install_nbi_reload_features installed"。 |
| 4   | Reload endpoint 200，但 Settings 里的 API Key 还是旧的（5 min rotate 后 401 on chat）                                    | L3 Provider cache not invalidated → `/tmp/nbi-runtime-env.json` 没被 merge 进 environ 或 ferry JSON mode ≠ 0600 被 notebook uid 不可读                                                                                            | E2E CK3 查 mode=600 + 8 keys ordered；手动跑 `kubectl exec -c notebook <pod> -- cat /tmp/nbi-runtime-env.json                                                                     | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d), list(d.keys()) == [...expected list...])'`。 |
| 5   | 偶发 spawn 时 extra_containers 里出现 2+ 个 `nbi-auth-sidecar` 条目 (pre_spawn_hook 被重入)                              | 旧版 hook 缺少 dedup filter；在 v1.0.0 的 make_pre_spawn_hook 我们已经加了 filter-then-append，需要确认 values.yaml 中 hub.extraConfig 引用的是新版代码而不是外部 inline 老版本。                                                 | 查看 Hub 配置 `kubectl -n jhub exec pod/hub -- grep -A 20 "deduped.append" srv/jupyterhub_config.py`。                                                                            |
| 6   | Security 合规扫描报告 sidecar 容器跑的进程打开 0.0.0.0 监听                                                              | 用户给 helm 传了自定义 host (如 `--set ...NBI_SIDECAR_HTTP_HOST=0.0.0.0`)；server.py loopback guard 会在启动时 ValueError → CrashLoopBackOff → 扫描抓到的是 notebook 8888 (这个需要监听 0.0.0.0 是正常的)。                       | `E2E CK7` 要在每次 release 都跑；NBI_SIDECAR_HTTP_HOST 默认值必须保留 127.0.0.1。                                                                                                 |
| 7   | 升级 hub (helm upgrade) 后老用户 session 里 "token 过期" 但新 spawn 用户正常                                             | 老用户 pod 是在 upgrade 之前 spawn 的，它们用的是旧 sidecar image tag / 旧 hook 注入逻辑。**完全符合设计预期**：upgrade 只影响 _新 spawn_ 的 Pod。重启老用户 server 即可 (JupyterHub 控制面 → Stop My Server → Start My Server)。 | 向用户解释 "零停机升级的 trade-off"。                                                                                                                                             |
| 8   | 日志里看到明文 JWT                                                                                                       | Redaction 漏网之鱼；找 redact.py 新增 regex pattern + 更新 smoke test 补 case。                                                                                                                                                   | grep 日志找 `eyJ` → 找到对应 PID / 路径 → 提交 PR。                                                                                                                               |
| 9   | 每次 rotate 后用户 notebook 界面弹出 "配置已保存" 的 toast（其实根本没按 Save）                                          | 正常行为: WS MCPServerStatusChange broadcast 触达 → fetchCapabilities → React diff → 重渲染 Settings。别修，这说明 L5 刷新链路是通的！                                                                                            | 不处理 (feature not bug)。                                                                                                                                                        |

---

## 第 6 章: 运维 Runbook

### 6.1 Canary 新 sidecar 版本流程 (低风险升级)

不要 `helm upgrade` 全量切。推荐 per-profile 灰度：

```yaml
# 在 site-values.yaml 里加一个 canary profile
singleuser:
  profileList:
    - display_name: 'Default (Stable)'
      default: true
      kubespawner_override:
        # 默认走 hub env 里的全局 stable tag
        environment: {}
    - display_name: 'Canary — Auth Sidecar RC'
      description: 'Rolls out new nbi-auth-sidecar for brave users'
      kubespawner_override:
        # hook 不对已存在的 spawner.environment 键做 overwrite，
        # 但 NBI_AUTH_SIDECAR_IMAGE 是 hub.extraConfig closure 绑定时读取的,
        # 所以 canary 要换个方式: 通过 ENV 传给 spawner, 让 hook 读 spawner.environment 中的
        # NBI_AUTH_SIDECAR_IMAGE_OVERRIDE key (我们已在 pre_spawn_hook 中支持这个 key).
        environment:
          NBI_AUTH_SIDECAR_IMAGE_OVERRIDE: 'registry.scbdev.net/gdp/nbi-auth-sidecar:rc.20260918.1'
```

→ 让 2–3 个 beta 用户选 Canary profile 跑一天；没问题再改全局 `NBI_AUTH_SIDECAR_IMAGE`。

### 6.2 紧急回滚 (发现新版本有严重 bug)

**场景**: v1.1.0 sidecar 会错误地把 base_url 覆盖成 `localhost` → 用户全部 404。

**回滚步骤** (1 command, 不影响已运行用户):

```bash
# 回滚 Hub (改回 v1.0.0 image tag)
helm upgrade -i jhub jupyterhub/jupyterhub --version 3.3.0 -n jhub \
  -f local-dev/deploy/k8s/jupyterhub-values-nbi-auth-sidecar.yaml \
  -f /tmp/my-site-values.yaml \
  --set hub.extraEnv[11].name=NBI_AUTH_SIDECAR_IMAGE \
  --set-string "hub.extraEnv[11].value=registry.scbdev.net/gdp/nbi-auth-sidecar:1.0.0"

# 已经在运行的用户 Pod → 继续用旧 sidecar tag 正常, 不用重启。
# 新 spawn 用户 → 拿 1.0.0 tag。
```

### 6.3 一键强制为某个用户立即 refresh (on-demand)

```bash
POD_NAME=jupyter-alice
kubectl -n jhub port-forward pod/${POD_NAME} 18091:18090 > /tmp/pf.log 2>&1 &
PF_PID=$!
sleep 2
curl -sS -X POST http://127.0.0.1:18091/rotate-self
# → {"accepted":true,"reason":"on-demand via POST /rotate-self"}
kill $PF_PID 2>/dev/null
# 之后 5 s 内用户 Settings 应当刷新。
```

---

## 第 7 章: 验收标准 (AC) 与证据矩阵

| AC 编号 | 描述                                           | 验证方法                                            | 证据文件                                    |
| ------- | ---------------------------------------------- | --------------------------------------------------- | ------------------------------------------- |
| AC-1    | Stdlib-only Python image (0 new pip)           | Dockerfile audit + pip list                         | smoke-test.sh T0 docker inspect             |
| AC-2    | Bootstrap + ready ≤ 90 s                       | 10× pod restart 取 p99                              | e2e-validation.sh CK1 + smoke               |
| AC-3    | T-300 s refresh fires in 60 s window           | Mock TTL=40 s + REFRESH_BEFORE=35 s                 | smoke-test.sh T8b (15 s 内出现第 2 次 POST) |
| AC-4.1  | L1→L5 via HTTP endpoint ≤ 5 s                  | POST reload → poll capabilities                     | E2E CK4                                     |
| AC-4.2  | L1→L5 via FS Poll fallback ≤ 10 s              | Disable endpoint → fs write → poll capabilities     | smoke + E2E 辅助                            |
| AC-5.1  | Sidecar cmdline 0 password hits                | /proc/\*/cmdline python grep                        | E2E CK5                                     |
| AC-5.2  | Notebook cmdline 0 password hits               | 同上                                                | E2E CK5 变体                                |
| AC-6.1  | Hook idempotent N calls → exactly 1 sidecar    | Python structural smoke (README 中的 inline test)   | 见 §3.3 dedup filter snippet                |
| AC-6.2  | Helm shape idempotent                          | helm upgrade twice → same rendered spec             | helm-template-test.sh                       |
| AC-7.1  | Helm render 产生 ≥ 2 containers                | helm-template-test.sh AC-7.1                        | helm-template-test.sh                       |
| AC-7.2  | notebook envFrom secret / sidecar probe host   | helm-template-test.sh AC-7.2/3                      | helm-template-test.sh                       |
| AC-8    | 18090 binds 127.0.0.1 only                     | socket bind probe to 0.0.0.0:18090 + ss/netstat     | smoke-test.sh T10 + E2E CK7                 |
| AC-9    | FS fallback reload when endpoint DOWN          | 模拟 fake jupyter kill → 写文件 → capabilities 变更 | smoke / E2E 补充场景                        |
| AC-10   | 0 Chinese chars in code comment lines          | awk 3-byte UTF-8 Chinese range                      | 构建时 grep step                            |
| AC-11   | Handoff rubric ≥ 4/5 signed off by 2 engineers | checklist #15 签字                                  | e2e-checklist.md final line                 |

---

## 第 8 章: 已知限制与未来路线图

| 类别              | 已知限制 (v1.0.0)                                                                                         | 未来可选演进                                                                                       |
| ----------------- | --------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| 刷新触发          | 只有 sidecar 和 FS poll 两条路径。AI Factory 如果主动 revoke token，sidecar 不知道，要等到下一次 T-5min。 | 可选: 扩展 scheduler 监听 /revoke webhook (需要平台侧 AI Factory 提供事件)。                       |
| Multi-tenancy     | 同一个 sidecar 给 pod 中单用户服务；多个 concurrent user 跑同一 Pod 不支持。                              | Z2JK 默认就是单用户 Pod，不构成实际阻塞。                                                          |
| 资源占用          | JVM 是大头 (256 Mi 起步)。未来希望 native-image 编译 token-tool.jar 或用 Python JKS 实现代替 Java。       | 引入 token-tool-native（GraalVM）预估可以把 sidecar 内存降到 64 Mi 级。                            |
| Prometheus scrape | /metrics 暴露了，但没有配套 PodMonitor / ServiceMonitor，集群级 Prometheus 不会自动抓。                   | 下一迭代在 deploy/k8s 加一个 PodMonitor YAML，label 匹配 sidecar 的 prometheus.io/scrape: "true"。 |
| Audit logging     | 仅 stdout JSON line 日志；没打 ELK / Kafka。                                                              | 按内部平台规则改 JSON 格式 接入 centralized log。                                                  |

---

> **文档版本说明 (Language versions)**: 本文档英文版本位于 [nbi-auth-sidecar-design-and-deployment.en.md](file:///Users/bl44001/gdp/repo/notebook-intelligence/docs/nbi-auth-sidecar-design-and-deployment.en.md)，内容与中文版本 v1.0.0 完全一致，供英文母语工程师与 offshore 团队查阅。

**Document End.** 遇到问题第一时间在 `local-dev/deploy/nbi-auth-sidecar/` 下跑 `./smoke-test.sh` (最快) 和 `./e2e-validation.sh` (最真实)。

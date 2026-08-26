# Quick Demo: JupyterHub + Notebook Intelligence × AI Factory

**Audience:** Platform / ML engineers who already have **JupyterHub + Notebook Intelligence (NBI)** on a Kubernetes cluster and need a **fast demo** against the corporate **AI Factory** OpenAI-compatible LLM gateway.

**Related docs:**

- Full architecture: [`internal-llm-gateway-integration.md`](internal-llm-gateway-integration.md)
- Full K8s deployment (sidecar + Hub): [`ai-factory-k8s-deployment.md`](ai-factory-k8s-deployment.md)
- Handoff / go-live: [`handoff-internal-llm-gateway.md`](handoff-internal-llm-gateway.md)
- Local spike: [`../local-dev/README.md`](../local-dev/README.md)

---

## 1. Goal

Show JupyterLab users chatting with an internal model (e.g. `databricks/gdp-gpt4o`) via NBI **without GitHub Copilot**, using AI Factory as the backend.

This document prioritizes **speed for a demo**. It is **not** the production design (per-pod auth sidecar + quota + NetworkPolicy).

---

## 2. Choose a path (by speed)

| Path                               | Time          | When to use                                       | Risk                          |
| ---------------------------------- | ------------- | ------------------------------------------------- | ----------------------------- |
| **A. NBI Settings + Bearer token** | **15–60 min** | Hub + NBI already installed; you can mint a token | Token expires; demo only      |
| **B. Shared cluster auth proxy**   | **2–4 hours** | Token expires quickly, or you have JAR/JKS        | Shared credentials; demo only |
| **C. Per-user pod sidecar**        | 1–2 days      | Near-production shape                             | Too slow for “demo today”     |

**Default for a quick demo: Path A.** Use Path B if OIDC tokens die during the meeting. Defer Path C to go-live (see handoff doc).

```text
Path A (demo):
  Browser → Jupyter Server (NBI openai-compatible) → AI Factory Gateway /v1

Path B (demo, more stable):
  Browser → NBI → Cluster Service (auth proxy) → AI Factory Gateway

Path C (production):
  Browser → NBI → 127.0.0.1 sidecar (JAR + quota) → AI Factory Gateway
```

---

## 3. Path A — Fastest demo (recommended first)

### 3.1 Prerequisites

- JupyterHub user can open Lab with NBI installed
- AI Factory gateway base URL (example):  
  `https://gateway.scaifactory.dev.azure.scbdev.net/v1`
- Model id (example): `databricks/gdp-gpt4o`
- A valid **Bearer** access token (OIDC / gateway API key)
- User pod can reach the gateway host (see [§5 Corporate proxy / Zscaler](#5-corporate-proxy--zscaler-critical-on-corp-clusters))

### 3.2 Configure NBI (Settings UI)

In JupyterLab → **NBI Settings** → **OpenAI Compatible**:

| Field    | Value                                                   |
| -------- | ------------------------------------------------------- |
| Provider | `openai-compatible`                                     |
| Base URL | `https://<ai-factory-host>/v1`                          |
| API key  | Current Bearer token                                    |
| Model    | `databricks/gdp-gpt4o` (or the id your gateway expects) |

**Do not** set Base URL to `…/v1/chat/completions` or `…/v1/models`.  
The OpenAI SDK appends `/chat/completions` itself.

Optional: hide Copilot so users are not steered to a dead path:

```python
# jupyter_server_config.py / Hub-managed config
c.NotebookIntelligence.disabled_providers = ["github-copilot"]
```

### 3.3 Prove the gateway from the user pod

Run these **inside the same user pod** (Jupyter terminal or `kubectl exec`).

**Chat completions (what NBI uses):**

```bash
curl -sS -w "\nHTTP %{http_code}\n" \
  "https://gateway.scaifactory.dev.azure.scbdev.net/v1/chat/completions" \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "databricks/gdp-gpt4o",
    "messages": [{"role": "user", "content": "ping"}],
    "max_tokens": 32
  }'
```

Expect **HTTP 200** and an assistant message (e.g. `Pong!…`).

**Same call via the OpenAI Python client (same stack as NBI):**

```bash
python - <<'PY'
from openai import OpenAI
client = OpenAI(
    base_url="https://gateway.scaifactory.dev.azure.scbdev.net/v1",
    api_key="<TOKEN>",
)
r = client.chat.completions.create(
    model="databricks/gdp-gpt4o",
    messages=[{"role": "user", "content": "ping"}],
    max_tokens=32,
)
print(r.choices[0].message.content)
PY
```

**Note on `GET /v1/models`:** many AI Factory deployments return **404** for `/v1/models`. That is **OK**. NBI chat does not require a models list endpoint.

### 3.4 Demo checklist

- [ ] `curl` `POST /v1/chat/completions` → 200 from the user pod
- [ ] NBI Settings point at `/v1` + token + model
- [ ] Pod env has gateway hosts on `NO_PROXY` / `no_proxy` (if HTTPS proxy exists)
- [ ] User server **Stop → Start** after env changes
- [ ] NBI chat returns a reply with **no** GitHub login

---

## 4. Path B — Shared auth proxy (when tokens expire)

If Path A works for one curl but tokens expire mid-demo, deploy **one** cluster proxy that mints/refreshes tokens (reuse `local-dev/llm-gateway-sidecar`).

```text
NBI (all demo users)
  → http://nbi-llm-proxy.<namespace>.svc.cluster.local:8089/v1
Auth proxy Deployment (MODE=proxy, TOKEN_PROVIDER=jar|static)
  → https://<ai-factory>/v1
```

Rough steps:

1. Build/run sidecar image with `MODE=proxy`, `UPSTREAM_BASE_URL=https://<ai-factory>/v1`.
2. For demo only: `HOST=0.0.0.0`, `SIDECAR_ALLOW_NON_LOOPBACK=1`, Service on port `8089`.
3. Mount JAR/JKS Secret if using `TOKEN_PROVIDER=jar`, or set `STATIC_BEARER` / `UPSTREAM_API_KEY`.
4. Point NBI Base URL at the Service; API key can be `local`.

**Do not use Path B in production** (shared platform credentials, non-loopback bind). Prefer Path C for go-live.

---

## 5. Corporate proxy / Zscaler (critical on corp clusters)

### 5.1 Symptom

NBI logs look like:

```text
openai._base_client - Retrying request to /chat/completions ...
notebook_intelligence.api - ERROR - Error in tool call loop: Connection error.
```

Jupyter itself stays healthy (many `200 GET`s). This is almost always **egress / proxy**, not a bad prompt.

### 5.2 Root cause observed on SCB-style clusters

User pods often have:

```text
HTTPS_PROXY=http://<zscaler-ip>:443
NO_PROXY=<long list of corp domains — often missing AI Factory>
```

`curl` then does `CONNECT` via Zscaler and may get:

```text
HTTP/1.0 502 Bad Gateway
Server: Zscaler/...
curl: (56) Received HTTP code 502 from proxy after CONNECT
```

AI Factory hosts such as `gateway.scaifactory.dev.azure.scbdev.net` are **internal** and should usually **bypass** the outbound web proxy.

### 5.3 Fix: add gateway to `NO_PROXY` (and `no_proxy`)

**Temporary test in a Jupyter terminal:**

```bash
export NO_PROXY="${NO_PROXY},gateway.scaifactory.dev.azure.scbdev.net,.scaifactory.dev.azure.scbdev.net,.azure.scbdev.net"
export no_proxy="$NO_PROXY"

curl -v --connect-timeout 10 \
  "https://gateway.scaifactory.dev.azure.scbdev.net/v1/chat/completions" \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"model":"databricks/gdp-gpt4o","messages":[{"role":"user","content":"ping"}],"max_tokens":16}'
```

When bypass works, curl connects **directly** to the gateway IP (e.g. `10.x.x.x:443`), TLS verifies with the corporate CA, and chat returns **200**.

### 5.4 Make it stick for Notebook Intelligence

Exports in the Jupyter **terminal only affect that shell**.  
NBI runs inside the **Jupyter Server** process and keeps the environment from **server start**.

You must:

1. Persist the same hosts on **`NO_PROXY` and `no_proxy`** in Hub / Helm `singleuser.extraEnv` (or KubeSpawner environment).
2. **Stop My Server → Start My Server** for the demo user.
3. Confirm inside a **new** terminal after restart:

```bash
python - <<'PY'
import os
np = (os.environ.get("NO_PROXY") or "") + "," + (os.environ.get("no_proxy") or "")
print("AI Factory on NO_PROXY?", "scaifactory" in np or "azure.scbdev.net" in np)
print("HTTPS_PROXY=", os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))
PY
```

Example Hub snippet:

```python
# Append to existing NO_PROXY; do not wipe corp defaults
extra = (
    "gateway.scaifactory.dev.azure.scbdev.net,"
    ".scaifactory.dev.azure.scbdev.net,"
    ".azure.scbdev.net"
)
# Merge with your chart’s existing NO_PROXY value, then:
c.KubeSpawner.environment.update({
    "NO_PROXY": "<existing>,%s" % extra,
    "no_proxy": "<existing>,%s" % extra,
})
```

If direct connect times out after bypassing the proxy, ask networking to allow **user pod → AI Factory :443** (or use Path B with egress only from the proxy pod).

---

## 6. Troubleshooting matrix

| Observation                                           | Meaning                              | Action                                             |
| ----------------------------------------------------- | ------------------------------------ | -------------------------------------------------- |
| NBI: `Connection error` + OpenAI retries              | TCP/TLS/proxy failure to gateway     | Fix `NO_PROXY`; restart server; verify with curl   |
| curl via proxy: Zscaler **502**                       | Gateway wrongly sent through Zscaler | Add gateway domains to `NO_PROXY` / `no_proxy`     |
| curl direct: TLS OK, **`GET /v1/models` → 404**       | Models API not implemented           | Ignore; test **`POST /v1/chat/completions`**       |
| curl **`chat/completions` → 200** but NBI still fails | Server env outdated                  | Persist `NO_PROXY` on pod; **restart** user server |
| HTTP **401/403**                                      | Token/ACL                            | Re-mint Bearer; check client credentials           |
| HTTP **400** about model                              | Wrong model id                       | Use the id AI Factory documents                    |
| Settings `base_url` ends with `/chat/completions`     | Double path                          | Use `…/v1` only                                    |

---

## 7. Security reminders (demo)

- Never commit Bearer tokens, JAR/JKS, or `corp-probe.env` to git.
- Path A stores the API key in `~/.jupyter/nbi/config.json` (plaintext) — acceptable for a short demo, not for multi-tenant production.
- Prefer Path C (localhost sidecar + secrets volume) before any broad rollout.
- Rotate demo tokens after the session.

---

## 8. After the demo (next steps)

1. Replace Path A/B with **per-pod auth sidecar** (`local-dev/llm-gateway-sidecar`) + Hub `pre_spawn_hook`.
2. Add Quota Service and durable metering.
3. Tighten NetworkPolicy; keep AI Factory on `NO_PROXY` permanently in the singleuser profile.
4. Follow [`../local-dev/docs/go-live-checklist.md`](../local-dev/docs/go-live-checklist.md).

---

## 9. One-page cheat sheet

```text
1. Mint Bearer token for AI Factory
2. From user pod, ensure NO_PROXY includes gateway host (bypass Zscaler)
3. curl POST …/v1/chat/completions → expect HTTP 200
4. Persist NO_PROXY/no_proxy on Hub singleuser env → restart server
5. NBI Settings:
     base_url = https://<ai-factory-host>/v1
     api_key  = <Bearer>
     model_id = databricks/gdp-gpt4o   # or corp id
6. Chat “ping” in NBI → expect assistant reply
```

**Bottom line:** For the fastest demo, use **Path A** (stock `openai-compatible` → AI Factory). On corporate clusters, **Zscaler/`HTTPS_PROXY` is the usual blocker** — put the gateway on `NO_PROXY`, restart the user server, then demo. Ignore `/v1/models` 404 if `chat/completions` returns 200.

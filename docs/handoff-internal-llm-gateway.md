# Project Handoff — Internal LLM Gateway × Notebook Intelligence

**Handoff to:** Trae  
**Handoff date:** 2026-08-26  
**Repository:** `github.com:willman0982gmail/notebook-intelligence.git` (`main` branch, latest commit `d484a2c`)  
**Upstream NBI:** Fork of [Notebook Intelligence](https://github.com/notebook-intelligence/notebook-intelligence) with internal LLM Gateway integration

---

## 1. Project goal (one sentence)

On **JupyterHub / internal Kubernetes**, enable JupyterLab users to use an **internal OpenAI-compatible LLM Gateway** (e.g. `databricks/gdp-gpt4o`) through **Notebook Intelligence (NBI)** for chat, inline completion, and related features — **without GitHub Copilot** — with **per–Hub-user quota metering and enforcement**.

---

## 2. Background and constraints

| Constraint                                                   | Impact                                                                           |
| ------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| User pods run on internal K8s; GitHub Copilot is unreachable | Copilot must be disabled; traffic goes through the internal gateway              |
| Corporate gateway auth is **OIDC + Java JAR + JKS**          | NBI’s built-in `openai-compatible` provider cannot mint tokens directly          |
| Gateway TLS may use corporate CAs                            | Inject CA into the sidecar; avoid `verify=False` in NBI                          |
| Multi-tenant Hub                                             | Quota must be enforced in the sidecar / central service, not only in the browser |

**Architecture decision (accepted — do not overturn lightly):** Use an **auth sidecar + stock `openai-compatible` provider**; do not embed the JAR inside the NBI process. See [`local-dev/docs/adr-sidecar-vs-plugin.md`](../local-dev/docs/adr-sidecar-vs-plugin.md).

---

## 3. Architecture overview

```text
Browser (JupyterLab)
    ↕ same-origin WebSocket/REST
Jupyter Server + NBI (openai-compatible)
    → http://127.0.0.1:8089/v1  (api_key=local, no real secret)
Auth Sidecar (llm-gateway-sidecar)
    → Java JAR mints OIDC Bearer
    → Quota check / commit
    → HTTPS + Bearer → corporate LLM Gateway
```

**Identity and quota path:**

```text
Hub login → pre_spawn_hook injects NBI_LLM_USER / GROUPS / PLAN
         → Sidecar reads env + calls Quota Service
         → Over limit → 429; NBI shows error in chat, inline fails silently
```

Full design: [`internal-llm-gateway-integration.md`](internal-llm-gateway-integration.md).

---

## 4. Current status

### 4.1 Done (in-repo, verifiable locally)

| Area                                                   | Status           | How to verify                       |
| ------------------------------------------------------ | ---------------- | ----------------------------------- |
| Auth sidecar (mock / proxy)                            | Done             | `./local-dev/start-sidecar.sh`      |
| Token providers mock / jar / static + refresh          | Done             | `TOKEN_PROVIDER=jar` + fake JAR     |
| Quota: local file store + HTTP Quota Service           | Done             | `start-quota-service.sh`            |
| Hub `pre_spawn_hook` example                           | Done             | `local-dev/hub/pre_spawn_hook.py`   |
| Baked NBI defaults, `disabled_providers`               | Done             | `bake-nbi-config.sh`                |
| `X-NBI-Feature: chat\|inline` metering                 | Done             | `openai_compatible_llm_provider.py` |
| User-visible 429 / 503 errors                          | Done             | `format_openai_compatible_error()`  |
| Sidebar remaining-quota badge                          | Done             | `/notebook-intelligence/llm-quota`  |
| K8s examples (NetworkPolicy, Secret, Quota Deployment) | Done (manifests) | `local-dev/deploy/k8s/`             |
| Grafana dashboard + Prometheus alerts examples         | Done (files)     | `local-dev/deploy/grafana/`         |
| Regression + chaos drill scripts                       | Done             | `./local-dev/run-regression.sh`     |

User-story tracking: [`internal-llm-gateway-stories.md`](internal-llm-gateway-stories.md) (S01–S24; most **Done** locally).

### 4.2 Not done (needs cluster / corporate credentials)

**Canonical blockers:**

- [`local-dev/docs/remaining-tasks.md`](../local-dev/docs/remaining-tasks.md)
- [`local-dev/docs/go-live-checklist.md`](../local-dev/docs/go-live-checklist.md)

Summary (by priority):

1. **S01.5 / S07.4** — Prove chat end-to-end in a real Hub pod (zero Settings configuration)
2. **S04 / S06** — Mount real `token-tool.jar`, JKS; extract corporate CA PEM
3. **S05 / S15** — Apply NetworkPolicy; prove user pods cannot reach the gateway directly
4. **S09 / S10 / S23** — Probe corporate gateway with `probe-gateway.sh`; fill Corp column in `feature-matrix.md`
5. **S11** — Hub spawn injects `NBI_LLM_*`; two users with different quotas, end-to-end
6. **S13** — `MODE=proxy` against real gateway; confirm `usage` field and streaming behavior
7. **S16 / S17** — In-cluster Prometheus/Grafana; usage export CronJob
8. **S18** — Staging tabletop drill + link runbook from Hub admin docs

### 4.3 Known technical debt / limitations

| Item                                  | Description                                                                                                                             | Recommendation                                                                               |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| **Proxy-mode SSE**                    | In `MODE=proxy`, the sidecar buffers the full response via `urlopen` and **does not truly proxy streaming SSE**; mock mode streams fine | Validate with corp probe before go-live; implement SSE pass-through in the sidecar if needed |
| **`integration.md` slightly stale**   | Design doc does not fully reflect shipped features (X-NBI-Feature, llm-quota UI, etc.)                                                  | Sync §7/§12 per prior review notes                                                           |
| **`NBI_LLM_QUOTA_*` env vars**        | Injected by `pre_spawn_hook` but **not read** by the sidecar; enforcement uses Quota Service                                            | Documented as hints only; do not treat as the sole limit source                              |
| **Custom NBI plugin**                 | ADR defers this (S20)                                                                                                                   | Do not build unless sidecars are forbidden by policy                                         |
| **S21 central gateway per-user keys** | Deferred                                                                                                                                | See `adr-central-gateway-keys.md`                                                            |

---

## 5. Repository layout and key files

```text
docs/
  internal-llm-gateway-integration.md   # Requirements + architecture (main spec)
  internal-llm-gateway-stories.md       # User-story backlog + status
  handoff-internal-llm-gateway.md       # This document
  admin-guide.md                        # NBI admin config (llm-quota, disabled_providers)

local-dev/                              # ★ Main integration delivery area
  README.md                             # Local quick start
  start-local-stack.sh                  # JL 4.5.9 + NBI + sidecar one-shot
  run-regression.sh                     # Full regression (run after code changes)
  llm-gateway-sidecar/
    sidecar.py                          # Sidecar main process
    token_provider.py                   # mock | jar | static
    quota_store.py                      # local | http | memory
  quota_service/server.py               # Central quota HTTP API
  hub/pre_spawn_hook.py                 # Hub identity injection
  deploy/                               # Docker + K8s + Grafana + alerts
  docs/runbook.md                       # Ops (429, break-glass, rotation)
  docs/go-live-checklist.md             # Go-live checklist
  docs/remaining-tasks.md               # TODO (canonical)

notebook_intelligence/
  llm_providers/openai_compatible_llm_provider.py  # X-NBI-Feature, 429 copy
  extension.py                          # GET /notebook-intelligence/llm-quota

src/chat-sidebar.tsx                    # Quota badge + soft-cap banner
```

---

## 6. Environment setup (Trae — day one)

### 6.1 Hard requirements

- **Python ≥ 3.12** (do not use macOS system Python 3.9)
- Recommended conda env: `nbi-jl45`
- Node.js (to build NBI frontend if you change UI)

```bash
conda activate nbi-jl45
cd /path/to/notebook-intelligence
./local-dev/python.sh    # should print a 3.12+ path
```

### 6.2 Local smoke test (~5 minutes)

```bash
# 1) Unit tests (no Jupyter)
"$(./local-dev/python.sh)" local-dev/tests/test_local_stack.py

# 2) Sidecar smoke
./local-dev/start-local-stack.sh --smoke-only

# 3) Full stack (browser: http://127.0.0.1:8890/lab)
./local-dev/start-local-stack.sh

# 4) Full regression (run before committing)
./local-dev/run-regression.sh
```

Expected: NBI chat returns `[local-dev mock · plan=…]` echo replies; sidebar shows quota badge.

### 6.3 Quota + multi-user scenario

```bash
./local-dev/start-quota-service.sh
export QUOTA_BACKEND=http QUOTA_SERVICE_URL=http://127.0.0.1:8090
export NBI_LLM_USER=alice NBI_LLM_GROUPS=interns
./local-dev/start-sidecar.sh
curl -s http://127.0.0.1:8089/quota | jq .
```

Group `interns` maps to plan `intern` (200k tokens/day); use smoke scripts or manual chat to test 429.

---

## 7. Suggested work order for Trae

### Phase A — Learn the codebase (1–2 days)

1. Read [`internal-llm-gateway-integration.md`](internal-llm-gateway-integration.md) §1–§7, §12
2. Run all commands in §6.2
3. Read main flows in `sidecar.py`, `token_provider.py`, `quota_store.py`
4. Read `pre_spawn_hook.py` and `jupyterhub_config.snippet.py`

### Phase B — Obtain corporate dependencies (parallel; may block)

Request from platform / security teams (**never commit to git**):

| Resource                                                    | Purpose                    |
| ----------------------------------------------------------- | -------------------------- |
| `token-tool.jar`                                            | OIDC token mint            |
| `keystore.jks` / `truststore.jks`                           | mTLS / corporate CA        |
| Keystore / truststore passwords                             | Sidecar env vars           |
| LLM Gateway `UPSTREAM_BASE_URL`                             | e.g. `https://gateway…/v1` |
| Hub cluster kubectl access (namespace `jhub` or equivalent) | Deploy validation          |
| IdP / gateway egress allowlist approval                     | NetworkPolicy              |

Local probe template:

```bash
cp local-dev/corp-probe.env.example local-dev/corp-probe.env
# Edit UPSTREAM_BASE_URL / UPSTREAM_API_KEY
set -a && source local-dev/corp-probe.env && set +a
./local-dev/probe-gateway.sh
./local-dev/merge-probe-into-matrix.sh
```

### Phase C — Cluster integration (core delivery)

Work through [`go-live-checklist.md`](../local-dev/docs/go-live-checklist.md):

1. `extract-ca-from-jks.sh` → mount `SSL_CERT_FILE`
2. `kubectl create secret generic nbi-llm-auth …` (see `secret-llm-auth.example.yaml`)
3. `docker build -f local-dev/deploy/Dockerfile.singleuser`
4. `docker build -f local-dev/deploy/Dockerfile.quota`
5. `./local-dev/deploy/apply-manifests.sh --apply`
6. Merge Hub config + `pre_spawn_hook`
7. Fresh PVC user, zero-config smoke (S07.4)
8. Collect evidence: [`hub-evidence-checklist.md`](../local-dev/docs/hub-evidence-checklist.md)

### Phase D — Production hardening

1. Prometheus scrape sidecar `:8089/metrics`
2. Import Grafana dashboard + `alerts-nbi-llm.yml`
3. Run `tabletop-chaos.sh` in staging
4. Use `feature-matrix.md` Corp column to decide Agent / tools enablement
5. If proxy streaming UX is insufficient → implement sidecar SSE pass-through (see §4.3)

---

## 8. Important environment variables

### Sidecar

| Variable              | Default                 | Description                                                              |
| --------------------- | ----------------------- | ------------------------------------------------------------------------ |
| `MODE`                | `mock`                  | `mock` = local echo; `proxy` = forward to real gateway                   |
| `TOKEN_PROVIDER`      | `mock`                  | `mock` / `jar` / `static`                                                |
| `UPSTREAM_BASE_URL`   | _(empty)_               | Required in proxy mode, e.g. `https://gw…/v1`                            |
| `UPSTREAM_VERIFY_TLS` | `1`                     | Set `0` to disable verification inside sidecar only (not for production) |
| `NBI_LLM_USER`        | `local-dev`             | Metering subject                                                         |
| `NBI_LLM_GROUPS`      | _(empty)_               | Comma-separated; maps to quota plan                                      |
| `QUOTA_BACKEND`       | `local`                 | `local` file / `http` Quota Service                                      |
| `QUOTA_SERVICE_URL`   | `http://127.0.0.1:8090` | HTTP backend URL                                                         |
| `HOST` / `PORT`       | `127.0.0.1` / `8089`    | Must be loopback (unless `SIDECAR_ALLOW_NON_LOOPBACK=1`)                 |

### NBI

| Variable                  | Description                                           |
| ------------------------- | ----------------------------------------------------- |
| `NBI_CHAT_MODEL_PROVIDER` | Lock to `openai-compatible`                           |
| `NBI_CHAT_MODEL_ID`       | `openai-compatible-chat-model`                        |
| `NBI_LLM_SIDECAR_URL`     | Default `http://127.0.0.1:8089` for `llm-quota` proxy |

---

## 9. Operations and troubleshooting

Day-to-day ops: [`local-dev/docs/runbook.md`](../local-dev/docs/runbook.md). Common issues:

| Symptom                           | Investigation                                                                  |
| --------------------------------- | ------------------------------------------------------------------------------ |
| Chat shows quota exceeded         | `curl http://127.0.0.1:8089/quota`; Quota Service `PUT /v1/quota/{user}` boost |
| `/healthz` returns 503            | JAR / IdP / keystore issue; check sidecar logs                                 |
| Inline completion empty, no error | By design (429 fails silently); check sidecar metrics                          |
| User can reach gateway directly   | NetworkPolicy not applied or CIDR too broad                                    |
| Copilot still in Settings         | Check `disabled_providers` traitlet                                            |

Break-glass to temporarily re-enable Copilot: `runbook.md` § Break-glass (revoke after the incident).

---

## 10. Testing and quality gates

| Scenario                   | Command                                                          |
| -------------------------- | ---------------------------------------------------------------- |
| Sidecar unit tests         | `"$(./local-dev/python.sh)" local-dev/tests/test_local_stack.py` |
| Full regression            | `./local-dev/run-regression.sh`                                  |
| Chaos drill                | `./local-dev/tabletop-chaos.sh`                                  |
| OpenAI provider unit tests | `pytest tests/test_openai_compatible_llm_provider.py`            |
| Gateway capability probe   | `./local-dev/probe-gateway.sh`                                   |

**Before merge:** `run-regression.sh` green; if you change NBI core, also run project `pytest` / `jlpm test`.

---

## 11. Documentation index (reading order)

1. **This document** — handoff overview
2. [`internal-llm-gateway-integration.md`](internal-llm-gateway-integration.md) — requirements and architecture spec
3. [`internal-llm-gateway-stories.md`](internal-llm-gateway-stories.md) — stories and completion status
4. [`local-dev/README.md`](../local-dev/README.md) — local development entry
5. [`local-dev/docs/go-live-checklist.md`](../local-dev/docs/go-live-checklist.md) — go-live checklist
6. [`local-dev/docs/remaining-tasks.md`](../local-dev/docs/remaining-tasks.md) — remaining work
7. [`local-dev/docs/runbook.md`](../local-dev/docs/runbook.md) — operations
8. [`admin-guide.md`](admin-guide.md) — NBI admin (`disabled_providers`, `llm-quota`)
9. ADRs: [`adr-sidecar-vs-plugin.md`](../local-dev/docs/adr-sidecar-vs-plugin.md), [`adr-central-gateway-keys.md`](../local-dev/docs/adr-central-gateway-keys.md)

---

## 12. External dependencies and stakeholders

| Stakeholder             | Must provide                                                              |
| ----------------------- | ------------------------------------------------------------------------- |
| **Platform / K8s**      | Singleuser image build pipeline, Hub config merge rights, NetworkPolicy   |
| **Identity / Security** | JAR, JKS, password rotation policy, corporate CA                          |
| **LLM Gateway team**    | Gateway URL, streaming/tools capability matrix, per-user metering support |
| **Quota / FinOps**      | Plan catalog rules, report storage (if not using built-in Quota Service)  |
| **Monitoring**          | Prometheus/Grafana tenant, alert routing                                  |

---

## 13. Git and branches

- **Remote:** `origin` → `git@github.com:willman0982gmail/notebook-intelligence.git`
- **Main branch:** `main` (in sync with origin)
- **Key commits:**
  - `285f1e5` — Add support for AI factory (full local-dev sidecar / deploy / quota)
  - `8ac21fe` — Add solution doc for AI factory (integration design doc)
  - `d484a2c` — Fix version issues (current HEAD)

Recommended: create a feature branch for cluster integration after handoff, e.g. `feat/hub-go-live`.

---

## 14. Handoff confirmation checklist

Trae — please confirm each item after takeover:

- [ ] Can clone the repo and run `run-regression.sh` on Python 3.12
- [ ] Can open local NBI in the browser and complete one chat round
- [ ] Have read integration.md + stories.md Epics E0–E5
- [ ] Know how to request corporate JAR/JKS / gateway access and who to contact
- [ ] Have (or have requested) Hub cluster staging access
- [ ] Understand **proxy SSE limitation** and **Agent mode off by default**
- [ ] Have bookmarked runbook + go-live-checklist

---

## 15. Contacts and notes

| Item                       | Value                              |
| -------------------------- | ---------------------------------- |
| Handing off from           | _(previous owner: name / contact)_ |
| Trae                       | _(fill in after takeover)_         |
| Example corporate model    | `databricks/gdp-gpt4o`             |
| Sidecar default port       | `8089` (loopback only)             |
| Quota Service default port | `8090`                             |
| Local JupyterLab           | `http://127.0.0.1:8890/lab`        |

> **Security:** Never commit `token-tool.jar`, JKS files, passwords, `corp-probe.env`, or `config.json` containing API keys to git. `.gitignore` covers some paths; always run `git status` before committing.

---

**Bottom line for Trae:** The local spike is complete. Next step is wiring **real JAR + gateway + Hub cluster** and collecting evidence per the go-live checklist. Most code lives in `local-dev/`; NBI changes are intentionally minimal (stock provider + quota UI + error copy). When in doubt, check the runbook first, then the LLM-Sxx IDs in stories.

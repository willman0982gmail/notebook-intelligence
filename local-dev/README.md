# Local JupyterLab 4.5.9 + Notebook Intelligence test stack

Minimal + evolving platform spike for stories in [`docs/internal-llm-gateway-stories.md`](../docs/internal-llm-gateway-stories.md).

## Python requirement

**Python ≥ 3.12 only** (do not use macOS system `python3` / 3.9).

Preferred interpreter: conda env `nbi-jl45`.

```bash
conda activate nbi-jl45   # or: export PYTHON=$HOME/miniconda3/envs/nbi-jl45/bin/python3.12
./local-dev/python.sh     # prints resolved 3.12+ path
```

All `local-dev/*.sh` scripts resolve via `python.sh`.

## Quick start

```bash
# Unit tests (no Jupyter) — must be 3.12+
./local-dev/python.sh && "$(./local-dev/python.sh)" local-dev/tests/test_local_stack.py

# Sidecar smoke
./local-dev/start-local-stack.sh --smoke-only

# Full stack: JL 4.5.9 + NBI + mock sidecar
./local-dev/start-local-stack.sh
# later: ./local-dev/start-local-stack.sh --skip-build
```

Open http://127.0.0.1:8890/lab → NBI chat → mock replies.

## Layout

```text
local-dev/
  start-local-stack.sh       # JL 4.5.9 + NBI + sidecar
  start-sidecar.sh / stop-sidecar.sh
  start-quota-service.sh
  bake-nbi-config.sh
  smoke-test.sh
  nbi-config.local.json
  jupyter_server_config.py
  llm-gateway-sidecar/       # token mint, quota, OpenAI proxy/mock, /metrics
  quota_service/             # plan catalog + check/commit/usage API
  hub/                       # pre_spawn_hook + jupyterhub_config snippet
  deploy/                    # entrypoint, supervisord, NetworkPolicy, Secret, Grafana
  docs/runbook.md            # break-glass, 429, rotation matrix
  docs/hub-evidence-checklist.md
  docs/feature-matrix.md
  docs/adr-sidecar-vs-plugin.md
  deploy/prometheus/alerts-nbi-llm.yml
  tests/test_local_stack.py
```

## Story coverage (implemented in-repo)

| Stories     | What landed                                                                                |
| ----------- | ------------------------------------------------------------------------------------------ |
| S01–S02     | Sidecar mock/proxy, loopback bind, `/healthz` token warm, entrypoint + supervisord example |
| S01.2 / S03 | `TOKEN_PROVIDER=mock\|jar\|static` + refresh skew + 401 retry                              |
| S04         | `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`; `UPSTREAM_VERIFY_TLS` gated                        |
| S05–S06     | Example NetworkPolicy + Secret/volume layout                                               |
| S07–S08     | Bake config script, disabled_providers, env locks, break-glass in runbook                  |
| S09–S10     | Inline config + FIM probe + multi-turn smoke + Agent-off runbook                           |
| S11–S15     | Quota service, two-plan test, soft-cap (≥80%), forge ignored, Hub hook                     |
| S16         | `/metrics` (+ soft-cap, by-feature) + Grafana + Prometheus alerts + log redact             |
| S17–S18     | `/v1/usage`, boost API, runbook                                                            |
| S19         | Server `llm-quota` proxy + sidebar remaining-token badge                                   |
| S20         | ADR: sidecar-only                                                                          |
| S22         | `X-NBI-Feature: chat\|inline` on OpenAI-compatible client                                  |

## Fake JAR token mint (LLM-S01.2 / S03)

```bash
TOKEN_PROVIDER=jar \
JAVA_BIN="$PWD/local-dev/llm-gateway-sidecar/fake_java.sh" \
TOKEN_JAR="$PWD/local-dev/llm-gateway-sidecar/fake_token_jar.py" \
MOCK_TOKEN_TTL_S=30 \
./local-dev/start-sidecar.sh
```

## Deploy helpers (cluster)

```bash
# Example singleuser image
docker build -f local-dev/deploy/Dockerfile.singleuser -t nbi-singleuser:local .

# Quota Service + NetworkPolicy + Secret examples
kubectl -n jhub apply -f local-dev/deploy/k8s/quota-service.yaml
kubectl -n jhub apply -f local-dev/deploy/k8s/networkpolicy-llm-egress.yaml
# create Secret from real JAR/JKS (never commit): see secret-llm-auth.example.yaml
```

## Chaos / tabletop (LLM-S18)

```bash
./local-dev/tabletop-chaos.sh
```

## Regression (LLM-S24)

```bash
# Uses nbi-jl45 / Python 3.12 automatically
./local-dev/run-regression.sh
```

## Gateway probe (LLM-S23)

```bash
# Local mock (start sidecar first)
./local-dev/start-sidecar.sh
UPSTREAM_BASE_URL=http://127.0.0.1:8089/v1 UPSTREAM_API_KEY=x \
  PROBE_LABEL=local-mock ./local-dev/probe-gateway.sh
# → local-dev/.runtime/probe-results.json

# Corp gateway (fill feature-matrix Corp column)
UPSTREAM_BASE_URL=https://gateway.example.com/v1 UPSTREAM_API_KEY=… \
  PROBE_LABEL=corp ./local-dev/probe-gateway.sh
```

## Useful env

| Var                   | Default                 | Meaning                                    |
| --------------------- | ----------------------- | ------------------------------------------ |
| `MODE`                | `mock`                  | `mock` or `proxy`                          |
| `TOKEN_PROVIDER`      | `mock`                  | `mock` / `jar` / `static`                  |
| `NBI_LLM_SIDECAR_URL` | `http://127.0.0.1:8089` | Jupyter → sidecar for `/llm-quota`         |
| `QUOTA_BACKEND`       | `local`                 | `local` file store or `http` Quota Service |
| `QUOTA_SERVICE_URL`   | `http://127.0.0.1:8090` | when backend=http                          |
| `NBI_LLM_USER`        | `local-dev`             | metering subject                           |
| `NBI_LLM_GROUPS`      | _(empty)_               | group → plan                               |
| `MOCK_TOKEN_TTL_S`    | `3600`                  | exercise refresh with small values         |

## Quota service + sidecar (HTTP backend)

```bash
./local-dev/start-quota-service.sh
export QUOTA_BACKEND=http QUOTA_SERVICE_URL=http://127.0.0.1:8090
export NBI_LLM_USER=alice NBI_LLM_GROUPS=interns
./local-dev/start-sidecar.sh
curl -s http://127.0.0.1:8089/quota | jq .
```

## Hub wiring

See `hub/jupyterhub_config.snippet.py` and `deploy/entrypoint.sh`.

## Docs

- Handoff: [`docs/handoff-internal-llm-gateway.md`](../docs/handoff-internal-llm-gateway.md)
- Design: [`docs/internal-llm-gateway-integration.md`](../docs/internal-llm-gateway-integration.md)
- Stories: [`docs/internal-llm-gateway-stories.md`](../docs/internal-llm-gateway-stories.md)
- Runbook: [`docs/runbook.md`](docs/runbook.md)
- Remaining (cluster): [`docs/remaining-tasks.md`](docs/remaining-tasks.md)
- Go-live (cluster): [`docs/go-live-checklist.md`](docs/go-live-checklist.md)

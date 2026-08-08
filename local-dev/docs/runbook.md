# Operator runbook — Internal LLM + NBI (local-dev / Hub)

Companion to [`internal-llm-gateway-integration.md`](../../docs/internal-llm-gateway-integration.md) and stories LLM-S08 / S14 / S18.

## Break-glass: re-enable a disabled provider (LLM-S08.2)

In managed images Copilot is hidden via:

```python
c.NotebookIntelligence.disabled_providers = ["github-copilot", ...]
```

Temporary re-enable on one pod:

```python
c.NotebookIntelligence.allow_enabling_providers_with_env = True
# pod env:
# NBI_ENABLED_PROVIDERS=github-copilot
```

Remove after the incident. Prefer fixing the sidecar/gateway path instead.

## User sees quota exceeded (429)

1. Confirm sidecar: `curl -s http://127.0.0.1:8089/quota`
2. Confirm Quota Service (if used): `curl -s http://quota:8090/v1/quota?user=<name>`
3. Break-glass boost:

```bash
curl -X PUT http://quota:8090/v1/quota/<user> \
  -H 'Content-Type: application/json' \
  -d '{"extra_tokens": 500000}'
```

4. Or wait until `reset_at` (daily window).
5. JupyterLab itself should remain usable; only LLM calls fail.

## Inline completer on 429 (LLM-S14.3)

NBI inline completion calls the same OpenAI-compatible endpoint. A 429 fails quietly (empty suggestion) so typing is not blocked. **Chat** surfaces a bold error line from `format_openai_compatible_error` (quota / HTTP status / message).

## Quota badge in the chat header (LLM-S19)

When the auth sidecar is reachable, the NBI sidebar shows remaining tokens (e.g. `48.2K`). Hover for plan / used / reset. Data path:

`Browser → GET /notebook-intelligence/llm-quota → Jupyter Server → http://127.0.0.1:8089/quota`

Set `NBI_LLM_SIDECAR_URL` if the sidecar port differs. Soft-cap styling appears when remaining ≤ 20% of the limit. The badge is informational only — enforcement stays in the sidecar.

## IdP / token JAR down

- Symptom: sidecar `/healthz` → 503 `token_warm: false`
- Chat: 502 / auth errors after mint failure
- Mitigation: check JAR mount, keystore passwords, IdP egress; restart sidecar
- Local mock: `TOKEN_PROVIDER=mock`

## Quota store / Redis down

- File backend: check `QUOTA_STORE_PATH` permissions
- HTTP backend: sidecar should fail closed (deny or 503) — verify `QUOTA_SERVICE_URL`
- Tabletop: stop quota service, confirm chat fails safely, restart, counters intact

## Secret / JKS rotation (LLM-S06.3)

1. Issue new JKS / JAR via Secret Manager / CSI
2. Roll user pods (or restart sidecar process)
3. Revoke old keystore material
4. Never place secrets in `~/.jupyter/nbi/config.json` or git

## Egress audit note (LLM-S05.3)

Quarterly: from a user notebook, `curl -v https://<llm-gateway-host>/` should **fail** when NetworkPolicy is correct; `curl http://127.0.0.1:8089/healthz` should succeed.

## Feature matrix (LLM-S10) — local mock

See also [`feature-matrix.md`](feature-matrix.md). Probe with `./local-dev/probe-gateway.sh` (Python 3.12 via `python.sh`).

| Capability        | Local mock sidecar                      | Corp gateway (fill in) |
| ----------------- | --------------------------------------- | ---------------------- |
| Chat non-stream   | Supported                               | TBD                    |
| Chat stream (SSE) | Supported                               | TBD                    |
| Inline completion | Supported (same `/v1/chat/completions`) | TBD                    |
| Tool calling      | Not implemented in mock                 | TBD                    |
| Vision            | Not implemented in mock                 | TBD                    |
| Agent mode        | Leave off until gateway tools verified  | TBD                    |

## Soft-cap alerts (LLM-S14.4 / S16.3 / S19.3)

At ≥80% of daily tokens the sidecar still **allows** the request, increments
`nbi_llm_soft_cap_hits_total`, sets `soft_cap_hit` on `GET /quota`, and logs a warning.
NBI sidebar shows a soft-cap banner + low-quota badge styling. Hard deny remains at
100% (HTTP 429 + exhausted banner).

Example Prometheus rules: [`../deploy/prometheus/alerts-nbi-llm.yml`](../deploy/prometheus/alerts-nbi-llm.yml).

## Hub pod proof (LLM-S01.5)

Checklist + curl evidence template: [`hub-evidence-checklist.md`](hub-evidence-checklist.md).

## Tabletop drills (LLM-S18.2)

```bash
./local-dev/tabletop-chaos.sh
```

Covers: token TTL refresh, Quota Service down (fail-closed 503), hard 429, non-loopback bind refuse.

## JKS → PEM (LLM-S04.1)

```bash
TRUSTSTORE_PATH=/path/to/aitruststore.jks TRUSTSTORE_PASSWORD=… \
  ./local-dev/extract-ca-from-jks.sh
export SSL_CERT_FILE=local-dev/.runtime/corp-ca-bundle.pem
export REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"
```

## Usage CSV / daily summary (LLM-S17)

```bash
./local-dev/start-quota-service.sh
./local-dev/export-usage.sh            # events
./local-dev/export-usage.sh --summary  # daily rollup
```

## Corp feature matrix (LLM-S23)

```bash
UPSTREAM_BASE_URL=https://gateway…/v1 UPSTREAM_API_KEY=… PROBE_LABEL=corp \
  ./local-dev/probe-gateway.sh
./local-dev/merge-probe-into-matrix.sh
```

## Python for local-dev

Always Python ≥ 3.12 (`nbi-jl45`). Scripts call `local-dev/python.sh` and refuse system 3.9.

## End-user FAQ blurb (LLM-S14.5)

> Notebook Intelligence uses the bank’s internal LLM. Usage counts against your daily quota. If you see “LLM daily quota exceeded”, try again after the reset time shown in the message, or ask your platform admin for a temporary boost. Editing notebooks never requires the LLM.

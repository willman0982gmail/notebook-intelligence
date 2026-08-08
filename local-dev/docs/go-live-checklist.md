# Go-live checklist — remaining out-of-repo work

Local spike + regression are green (`./local-dev/run-regression.sh` on Python 3.12).
These items require a Hub cluster and/or corporate LLM Gateway credentials.

Short index: [`remaining-tasks.md`](remaining-tasks.md).

## 1. Secrets & TLS (LLM-S04 / S06)

- [ ] Obtain corp `token-tool.jar`, `keystore.jks`, `truststore.jks` (out of band)
- [ ] `./local-dev/extract-ca-from-jks.sh` → mount PEM as `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`
- [ ] Create `nbi-llm-auth` Secret (see `deploy/k8s/secret-llm-auth.example.yaml`)
- [ ] Confirm passwords never appear in NBI `config.json` or git

## 2. Images & Hub wiring (LLM-S02 / S07 / S11)

- [ ] Build user image: `docker build -f local-dev/deploy/Dockerfile.singleuser -t …`
- [ ] Build quota image: `docker build -f local-dev/deploy/Dockerfile.quota -t nbi-quota:…`
- [ ] Apply manifests: `./local-dev/deploy/apply-manifests.sh --apply`
- [ ] Merge `hub/jupyterhub_config.snippet.py` into Hub config
- [ ] Fresh PVC user: chat works with zero Settings clicks

## 3. NetworkPolicy (LLM-S05)

- [ ] Tighten `networkpolicy-llm-egress.yaml` CIDRs to IdP + gateway only
- [ ] From notebook: direct `curl` to gateway **fails**; `127.0.0.1:8089/healthz` **ok**
- [ ] Capture evidence per [`hub-evidence-checklist.md`](hub-evidence-checklist.md)

## 4. Corp gateway capability matrix (LLM-S09 / S10 / S23)

```bash
cp local-dev/corp-probe.env.example local-dev/corp-probe.env  # gitignored
# edit corp-probe.env, then:
set -a && source local-dev/corp-probe.env && set +a
./local-dev/probe-gateway.sh
./local-dev/merge-probe-into-matrix.sh
```

- [ ] Fill Corp column in `feature-matrix.md`
- [ ] Keep Agent off until `tool_calls_observed: true`
- [ ] Note inline latency / debounce guidance for operators

## 5. Observability (LLM-S16)

- [ ] Scrape sidecar `/metrics` (or pushgateway) into Prometheus
- [ ] Import `deploy/grafana/nbi-llm-sidecar-dashboard.json`
- [ ] Load `deploy/prometheus/alerts-nbi-llm.yml`

## 6. FinOps / ops (LLM-S17 / S18)

- [ ] Schedule `export-usage.sh --summary` (daily/monthly)
- [ ] Run `tabletop-chaos.sh` once in a staging pod
- [ ] Link runbook from Hub admin docs

## Deferred

- LLM-S21 central gateway per-user keys — see `adr-central-gateway-keys.md`

# Remaining tasks (canonical)

**Last updated:** 2026-08-08

In-repo local spike is complete (`./local-dev/run-regression.sh` on Python ≥ 3.12).
Everything below needs a **Hub cluster** and/or **corporate LLM Gateway** credentials.

Also see [`go-live-checklist.md`](go-live-checklist.md).

## Blocked — need secrets / cluster / gateway

| ID          | Task                                                      | Blocker                |
| ----------- | --------------------------------------------------------- | ---------------------- |
| S01.5       | Hub pod chat evidence (curl + NBI UI)                     | Hub pod                |
| S04         | Mount corp CA PEM from real JKS                           | JAR/JKS files          |
| S05         | Apply NetworkPolicy; prove direct gateway deny            | Cluster + gateway host |
| S06         | Create real `nbi-llm-auth` Secret                         | JAR/JKS + passwords    |
| S07.4       | Fresh PVC user zero-Settings smoke                        | Hub                    |
| S09/S10/S23 | Corp `probe-gateway.sh` → fill feature-matrix Corp column | `UPSTREAM_*`           |
| S11         | Hub spawn injects `NBI_LLM_*`; two real users             | Hub                    |
| S13         | Proxy mode with real gateway `usage`                      | Gateway                |
| S15         | Notebook cannot reach gateway without sidecar             | NetworkPolicy          |
| S16         | Import Grafana / load PrometheusRule in cluster           | Cluster monitoring     |
| S17         | CronJob writes to durable FinOps store                    | Cluster + bucket       |
| S18         | Tabletop once in staging pod; link runbook in Hub portal  | Staging                |

## Deferred (product decision)

| ID              | Task                                                            |
| --------------- | --------------------------------------------------------------- |
| S20 plugin path | N/A — ADR chose sidecar-only                                    |
| S21             | Central gateway per-user keys — revisit when gateway APIs exist |

## Unblocked local actions (when you have inputs)

```bash
# 1) Corp capability matrix
cp local-dev/corp-probe.env.example local-dev/corp-probe.env
# edit UPSTREAM_BASE_URL / UPSTREAM_API_KEY
set -a && source local-dev/corp-probe.env && set +a
./local-dev/probe-gateway.sh && ./local-dev/merge-probe-into-matrix.sh

# 2) Images (Docker available locally)
./local-dev/deploy/build-images.sh

# 3) Cluster apply (when kubectl points at a live API)
./local-dev/deploy/apply-manifests.sh --apply
```

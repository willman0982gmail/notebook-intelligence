# NBI Auth Sidecar — E2E Validation Checklist

> 15 checkboxes mapping to the 11 Acceptance Criteria defined in
> `.trae/specs/nbi-auth-sidecar-helm-integration/spec.md`.
>
> Copy this page before each release, fill in the Result column, and attach
> evidence (log snippets) for every item marked **FAIL** before escalating.

---

## Acceptance Criteria → Checklist Item Mapping

| #   | AC Ref | Checkpoint (What to verify)                                                                                                                                                                     | Script / Tool         | Result ☐ PASS ☐ FAIL | Evidence / Notes                                                                                                                                                                                                                                                                                                                                                                                        |
| --- | ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | AC-1   | Stdlib-only Python image: sidecar pip list shows NO 3rd-party packages beyond the base image; sidecar runs with `python3 -c "import yaml"` → FAIL.                                              | docker exec + pip3    | ☐ ☐                  |                                                                                                                                                                                                                                                                                                                                                                                                         |
| 2   | AC-2   | Bootstrap (pod START → sidecar /ready returns 200) completes in ≤ 90 s (99th pctl across 10 sequential pod restarts).                                                                           | kubectl + stopwatch   | ☐ ☐                  | pod count N=10 min=…s max=…s p99=…s                                                                                                                                                                                                                                                                                                                                                                     |
| 3   | AC-3   | Pre-expiry T-300 s refresh fires within 60 s of `exp - 300` — inject a short-TTL mock token (TTL=40 s, REFRESH_BEFORE=35) and assert reload within 10 s window after T-35.                      | curl + sidecar mock   | ☐ ☐                  | timeline: mint at ts=0 → rotate-self POST at ts=5 → reload-config call at ts=…                                                                                                                                                                                                                                                                                                                          |
| 4   | AC-4.1 | Write new config.json → L1→L5 propagation via **HTTP reload endpoint** path: `GET /capabilities` → `api_key_masked` field changes ≤ 5 s after POST reload-config.                               | curl_jupyter          | ☐ ☐                  | old_mask=sk-test-…-A… new_mask=sk-test-…-B… latency=…s                                                                                                                                                                                                                                                                                                                                                  |
| 5   | AC-4.2 | Same as AC-4.1 but via **FS-poll fallback path**: disable HTTP endpoint, `echo` into config.json directly → `api_key_masked` changes within 5 s + 5 s poll chunk ≤ 10 s worst case.             | echo + curl           | ☐ ☐                  | latency=…s (FS chunk size 5 s verified via daemon thread log)                                                                                                                                                                                                                                                                                                                                           |
| 6   | AC-5.1 | **Sidecar container** `/proc/*/cmdline` (all PIDs) — zero matches for regex `password[\x00:=].{3,}` case-insensitive.                                                                           | kubectl exec grep     | ☐ ☐                  | grep output pasted; grep returned zero lines matching pattern.                                                                                                                                                                                                                                                                                                                                          |
| 7   | AC-5.2 | **Notebook container** `/proc/*/cmdline` — same password grep rule; NBI processes (python3 + any jupyter/java) must pass.                                                                       | kubectl exec grep     | ☐ ☐                  |                                                                                                                                                                                                                                                                                                                                                                                                         |
| 8   | AC-6.1 | KubeSpawner hook **idempotency**: simulate `pre_spawn_hook(spawner)` called N×(≥2) on the same spawner object → `extra_containers.count(name=="nbi-auth-sidecar") == 1`.                        | python structural     | ☐ ☐                  | calls N=3 → extra_containers names list pasted; sidecar appears exactly 1 time.                                                                                                                                                                                                                                                                                                                         |
| 9   | AC-6.2 | Helm upgrade (hub restart) followed by spawning 2 pods under same user → neither pod has duplicate `nbi-auth-sidecar` container entry in spec.                                                  | helm + kubectl        | ☐ ☐                  | kubectl get pod jupyter-alice -o jsonpath='{.spec.containers[*].name}' → "notebook nbi-auth-sidecar" no duplicate                                                                                                                                                                                                                                                                                       |
| 10  | AC-7.1 | `helm template` pass: rendered singleuser spec lists containers length ≥ 2 (notebook + nbi-auth-sidecar present).                                                                               | helm-template-test.sh | ☐ ☐                  | helm-template-test.sh exit code 0 report attached.                                                                                                                                                                                                                                                                                                                                                      |
| 11  | AC-7.2 | `helm template` pass: notebook container envFrom contains `secretRef.name = nbi-llm-auth`; sidecar container probes have `httpGet.host = "127.0.0.1"`.                                          | helm-template-test.sh | ☐ ☐                  | yq extract + grep output pasted showing secretRef and probe host values.                                                                                                                                                                                                                                                                                                                                |
| 12  | AC-8   | Running sidecar container: port 18090 appears in `ss -ltnp` ONLY with bind-address `127.0.0.1` / `::1`; socket bind probe to `0.0.0.0:18090` succeeds.                                          | kubectl exec ss       | ☐ ☐                  | bind probe to 0.0.0.0:18090 returned 0 → no conflict (sidecar on loopback only).                                                                                                                                                                                                                                                                                                                        |
| 13  | AC-9   | S2 FS fallback works when reload endpoint DOWN: kill fake jupyter in smoke-test.sh, write config.json via sidecar, NBI chat still picks up new token (API 200 not 401 on next completion call). | curl NBI chat API     | ☐ ☐                  | reload endpoint POST returned 000/404 but token still rotates within FS poll chunk interval; chat call succeeds.                                                                                                                                                                                                                                                                                        |
| 14  | AC-10  | Source-level grep zero Chinese in comment lines (Python + Docker + Bash): `awk` pattern 3-byte UTF-8 Chinese range on lines starting `[whitespace]*#` — zero matches.                           | repo-wide awk script  | ☐ ☐                  | awk exit 0; files scanned count=… (list: mint.py, config_writer.py, scheduler.py, server.py, **main**.py, nbi_reload_client.py, redaction.py, **init**.py, jupyter_server_config_nbi_reload.py, pre_spawn_hook.py, Dockerfile, Dockerfile.singleuser, Dockerfile.quota, build-images.sh, smoke-test.sh, e2e-validation.sh, helm-template-test.sh, .dockerignore, README.md code fence blocks reviewed). |
| 15  | AC-11  | **Handoff rubric** (≥ 4 / 5): code review signed off by 2 engineers that the delivery is handoff-ready (production quality, 2 additional reviewers not authors).                                | human review          | ☐ ☐                  | Reviewer A: ****\_**** (date \_**\_) Reviewer B: **\_\_\_**** (date \_\_\_\_) Rubric 1-5 rating = …/5.                                                                                                                                                                                                                                                                                                  |

---

## Checklist Completion Criteria (Release Gate)

Before `helm upgrade` to a production namespace (`jhub-prod` / `jhub-uat`):

1. Items 1–15 above → **exactly 15 PASS** marks are required.
2. Any FAIL item requires:
   - a signed-off exemption _or_
   - a fix commit + re-run of that specific checkpoint _before_ release.
3. AC-5, AC-6, AC-8 (security + idempotency + loopback bind) **HAVE NO EXEMPTION PATH**: if any of them FAILS, release is blocked.
4. This completed checklist page MUST be attached to the internal release ticket at `GDP-<NNNNN>` (NBI auth-sidecar go-live).

---

## Tool Invocation Cheat Sheet

```bash
# 1. Build images with pre-checks
TAG=rc.20260918.1 IMAGES="nbi-auth-sidecar" ./local-dev/deploy/build-images.sh

# 2. Helm values shape test
cd charts/nbi-auth-sidecar && ./helm-template-test.sh

# 3. Local smoke (requires docker)
cd local-dev/deploy/nbi-auth-sidecar
./smoke-test.sh nbi-auth-sidecar:rc.20260918.1

# 4. Live K8s E2E (7 checkpoints on a single running pod)
export JHUB_NS=jhub-uat
export POD_NAME=jupyter-alice
./e2e-validation.sh
```

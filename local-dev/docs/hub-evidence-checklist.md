# Hub pod evidence checklist (LLM-S01.5 / S05.2 / S24.3)

Use this when proving the path on a real JupyterHub user pod. Local mock
coverage lives in `./local-dev/run-regression.sh` and does **not** replace Hub proof.

## Prerequisites

- [ ] Singleuser image includes sidecar binary + `deploy/entrypoint.sh` (or supervisord)
- [ ] JAR/JKS mounted via Secret/CSI (not in git / not in `~/.jupyter`)
- [ ] NetworkPolicy applied (see `deploy/k8s/networkpolicy-llm-egress.yaml`)
- [ ] Baked NBI `config.json` + `disabled_providers` (Copilot off)

## Capture (attach to ticket)

1. **Sidecar health** (inside pod):

   ```bash
   curl -sS http://127.0.0.1:8089/healthz | tee /tmp/nbi-healthz.json
   ```

   Expect `status=ok`, `token_warm=true`.

2. **Chat round-trip** (sidecar only):

   ```bash
   curl -sS http://127.0.0.1:8089/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -d '{"model":"databricks/gdp-gpt4o","messages":[{"role":"user","content":"ping"}],"stream":false}' \
     | tee /tmp/nbi-chat.json
   ```

3. **NBI UI**: screenshot of streamed reply in chat sidebar (no GitHub Copilot login).

4. **Quota**:

   ```bash
   curl -sS http://127.0.0.1:8089/quota | tee /tmp/nbi-quota.json
   ```

5. **Egress deny** (LLM-S05.2) — from a notebook cell or terminal in the pod:

   ```bash
   # Should FAIL when NetworkPolicy is correct (timeout / connection refused)
   curl -v --max-time 5 https://<llm-gateway-host>/v1/models || true
   # Should SUCCEED
   curl -sS http://127.0.0.1:8089/healthz
   ```

6. **Browser HAR** (LLM-S24.3): confirm no JAR passwords / bearer tokens / gateway keys
   appear in Lab network traffic (only Jupyter origin + `/notebook-intelligence/*`).

## Pass criteria

| Check        | Pass                                              |
| ------------ | ------------------------------------------------- |
| Chat via NBI | Reply from internal model                         |
| No Copilot   | Settings does not require GitHub login            |
| Path         | NBI → `127.0.0.1` sidecar → gateway               |
| Egress       | Direct gateway curl fails; loopback sidecar works |
| Secrets      | Absent from HAR and user home                     |

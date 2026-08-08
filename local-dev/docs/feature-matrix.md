# Gateway feature matrix (LLM-S09 / S10 / S23)

Fill after running (Python ≥ 3.12 via `python.sh`):

```bash
# Local mock
./local-dev/start-sidecar.sh
UPSTREAM_BASE_URL=http://127.0.0.1:8089/v1 UPSTREAM_API_KEY=x \
  PROBE_LABEL=local-mock ./local-dev/probe-gateway.sh

# Corp
UPSTREAM_BASE_URL=https://gateway.example.com/v1 \
UPSTREAM_API_KEY=… \
PROBE_LABEL=corp ./local-dev/probe-gateway.sh
# → local-dev/.runtime/probe-results.json
```

| Capability                                      | Local mock                                            | Corp gateway              | Notes                               |
| ----------------------------------------------- | ----------------------------------------------------- | ------------------------- | ----------------------------------- |
| Chat non-stream                                 | Supported                                             | _TBD_                     | Confirmed via `probe-gateway.sh`    |
| Chat SSE stream                                 | Supported                                             | _TBD_                     | NBI chat UX prefers stream          |
| Inline completion (chat-completions FIM prompt) | Supported                                             | _TBD_                     | Same `/v1/chat/completions` path    |
| Tool calling                                    | Accepted but no tool_calls (treat as Off/unsupported) | _TBD_                     | Keep Agent mode off until Supported |
| Vision                                          | Off                                                   | _TBD_                     |                                     |
| `usage` on final stream chunk                   | Supported                                             | _TBD_                     | Needed for accurate metering        |
| `X-NBI-Feature` honored                         | Supported                                             | N/A (sidecar strips/uses) | chat vs inline budgets              |

## Operator notes (S09.3)

If the gateway is slow, inline completion may feel laggy. Users can raise the
inline debounce in NBI Settings → Inline completion, or admins can lower
request rate. Chat remains usable while waiting for a streamed reply.

## Agent mode (LLM-S10.3)

Keep **Agent / tool-calling mode off** in Hub images until the Corp column for
Tool calling is **Supported** (real `tool_calls` in probe results, not just HTTP 200).

- Default: leave Agent disabled in product settings / feature flags for managed images.
- Enable only after `PROBE_LABEL=corp ./local-dev/probe-gateway.sh` shows
  `tool_calling.status = Supported` with `tool_calls_observed: true`.
- Chat + inline remain available without Agent.

<!-- probe merge: label=local-mock probed_at=2026-08-08T02:43:08Z base=http://127.0.0.1:8089/v1 -->

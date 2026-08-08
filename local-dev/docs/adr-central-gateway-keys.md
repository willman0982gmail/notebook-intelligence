# ADR: Central gateway per-user keys vs sidecar metering (LLM-S21)

**Status:** Deferred  
**Date:** 2026-08-07

## Context

The org LLM Gateway may eventually issue per-user API keys or accept signed
`X-User-Id` headers. Until those APIs are available and documented, metering
and quota enforcement remain in the **auth sidecar + Quota Service**.

## Decision (current)

Keep **sidecar-local / Quota Service** as the system of record (see
`local-dev/quota_service` and `llm-gateway-sidecar`).

## When to revisit

- Gateway exposes stable per-user credentials or identity headers.
- Multiple products need one shared meter outside JupyterHub.

## Migration sketch

1. At Hub `pre_spawn_hook`, mint or look up a user-scoped gateway credential.
2. Sidecar uses that credential toward the gateway; stop local token counters
   (or dual-write during transition).
3. Point Grafana at gateway metrics; keep NBI `llm-quota` proxy reading either
   sidecar or gateway usage API.

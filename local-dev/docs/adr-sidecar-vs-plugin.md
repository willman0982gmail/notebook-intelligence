# ADR: Sidecar-only vs custom NBI provider plugin (LLM-S20)

**Status:** Accepted for current phase  
**Date:** 2026-08-07

## Decision

Stay with **auth sidecar + stock `openai-compatible` provider** for JupyterHub.

Do **not** build a branded `corp-llm-gateway` NBI plugin unless a later requirement forces in-process JAR minting without a sidecar.

## Consequences

- NBI upgrades remain low-friction (no custom provider package).
- JAR / JKS / TLS / quota live outside the Jupyter process (`local-dev/llm-gateway-sidecar`).
- Settings UI shows OpenAI-compatible fields; admins lock provider via traitlets/env.

## Revisit if

- Sidecars are forbidden on user pods, or
- Product requires a first-class “Corp LLM” provider name in Settings without OpenAI-compatible labeling.

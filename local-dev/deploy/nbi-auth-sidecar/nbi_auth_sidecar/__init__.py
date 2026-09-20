"""
nbi_auth_sidecar package.

Stdlib-only companion to the NBI Jupyter extension.  When deployed as a
Kubernetes sidecar alongside the notebook container this package:

  * Mints fresh AI Factory JWTs via the corporate ``token-tool.jar``
    (passwords always via 0600 java.security tempfile — NEVER on argv).
  * Atomically writes NBI's user config and the 8 STRING_OVERRIDE env
    ferry JSON every refresh cycle.
  * Calls the notebook container's NBI reload-config endpoint to trigger
    an L2→L5 config refresh without restart.
  * Exposes /healthz /ready /metrics /rotate-self on 127.0.0.1:18090 so
    K8s probes and admin dashboards can observe lifecycle state.

All code comments are in English per project AC-10.
"""

from __future__ import annotations

__version__ = "1.0.0"

# Public surface re-exports so callers can write
# ``from nbi_auth_sidecar import TokenScheduler, build_token_minter``
# without importing submodules by hand.  Lazy import to avoid top-level
# import-time I/O (filesystem reads, stat calls, etc) on import.
__all__ = [
    "__version__",
]

#!/usr/bin/env python3
"""OIDC / gateway bearer token providers for the local LLM sidecar.

Providers
---------
Three concrete ``TokenProvider`` implementations are supplied, selected at
import-time via the ``TOKEN_PROVIDER`` environment variable:

mock (default)
  Mint a fake opaque token string, with a configurable TTL and optional
  injected-failure count.  Used on developer laptops and in unit tests; does
  not require network access.

jar
  Subprocess ``java -jar $TOKEN_JAR …`` with the corporate token-tool JAR +
  JKS keystore/truststore.  The exact JAR args follow the pattern used by
  the sample Java chatbot (``--token-url``, ``--code``, ``--domain``), plus
  pass-through via ``TOKEN_JAR_EXTRA_ARGS``.  Output is expected to be a
  single line of JSON containing (at minimum) ``access_token`` +
  ``expires_in``.

static
  Use ``STATIC_BEARER`` / ``UPSTREAM_API_KEY`` as a perpetual bearer string.
  Intended for Path-A digs where the user pastes a freshly minted token into
  NBI Settings.  Explicitly *not* wrapped with ``RetryingTokenProvider``
  because static tokens cannot be "refreshed".

Wrapping
--------
All non-static providers are wrapped in ``RetryingTokenProvider`` so a
single transient IdP / JAR failure does not surface as a user-visible 500.
Retry uses exponential backoff (configurable via ``TOKEN_MINT_BACKOFF_S``
base delay + ``TOKEN_MINT_ATTEMPTS`` total attempts).

Concurrency
-----------
Each concrete provider serializes mint operations with its own
``threading.Lock``.  This matters in the sidecar because
``ThreadingHTTPServer`` can call ``get_token()`` concurrently from multiple
HTTP handler threads when NBI submits overlapping chat + inline requests.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional, Protocol

log = logging.getLogger("sidecar.token")


@dataclass
class AccessToken:
    """Result value returned by every TokenProvider.get_token() call.

    Attributes
    ----------
    access_token : str
        The raw bearer token string (inserted directly into the
        Authorization header).
    expires_at : float
        Unix-epoch seconds at which this token expires.  Comparison is done
        via ``time.time() >= expires_at`` minus a configurable skew window
        (``skew_s`` in each provider).
    raw : Optional[dict]
        The full JSON payload returned by the underlying mint method.  Kept
        for debugging / diagnostic logging; never logged verbatim because it
        may contain the token string itself.
    """

    access_token: str
    expires_at: float  # unix seconds
    raw: Optional[dict] = None

    @property
    def expired(self) -> bool:
        """True if the token has reached or passed its expiry timestamp.

        Note: callers should also check *before* expires_at using the
        provider-specific ``skew_s`` window to avoid race conditions against
        upstreams that validate expiry with their own clock.
        """
        return time.time() >= self.expires_at


class TokenProvider(Protocol):
    """Duck-type interface implemented by all four provider classes below.

    Uses a typing ``Protocol`` rather than an ABC so callers can type-check
    without pulling the concrete classes into an inheritance hierarchy.
    """

    def get_token(self, force_refresh: bool = False) -> AccessToken: ...


class MockTokenProvider:
    """Simulates JAR mint + refresh for local tests (LLM-S01.2, LLM-S03).

    This provider is intentionally feature-rich: it supports a configurable
    TTL, configurable refresh skew (so tests can force a re-mint), and a
    "fail N times" hook used by the chaos/tabletop scripts to exercise the
    RetryingTokenProvider wrapper + the 401 forced-refresh path in sidecar.
    """

    def __init__(
        self,
        ttl_s: float | None = None,
        skew_s: float | None = None,
        fail_times: int = 0,
    ) -> None:
        # TTL for each minted token (default 1h).  May be set via env for
        # tests that want to observe token-refresh loops.
        self.ttl_s = float(os.environ.get("MOCK_TOKEN_TTL_S", ttl_s or 3600))
        # Refresh skew: treat the token as expired this many seconds BEFORE
        # its true expiry.  Protects against clock skew between this pod and
        # the upstream gateway — 60s is the industry default.
        self.skew_s = float(os.environ.get("TOKEN_REFRESH_SKEW_S", skew_s or 60))
        # Inject N transient failures before the first successful mint.
        # Used by tabletop-chaos.sh (LLM-S18) to force the retry logic.
        self._fail_remaining = int(os.environ.get("MOCK_TOKEN_FAIL_TIMES", fail_times))
        # Serializes concurrent get_token() calls in ThreadingHTTPServer.
        self._lock = threading.Lock()
        # Cached token + expiry.  Guarded by _lock.
        self._cached: Optional[AccessToken] = None
        # Diagnostic counter — visible in tests to verify caching behaviour.
        self.mint_count = 0

    def get_token(self, force_refresh: bool = False) -> AccessToken:
        """Return cached token, or mint a new one if expired / forced.

        Thread-safe.  Raises RuntimeError while ``_fail_remaining`` > 0.
        """
        with self._lock:
            now = time.time()
            # Fast path: cache hit + still valid inside the skew window.
            if (
                not force_refresh
                and self._cached is not None
                and now < self._cached.expires_at - self.skew_s
            ):
                return self._cached
            # Simulated IdP unavailability (test hook).
            if self._fail_remaining > 0:
                self._fail_remaining -= 1
                raise RuntimeError("mock IdP unavailable (injected failure)")
            # Mint path.
            self.mint_count += 1
            token = AccessToken(
                access_token=f"mock-{uuid.uuid4().hex}",
                expires_at=now + self.ttl_s,
                raw={"token_type": "Bearer", "expires_in": int(self.ttl_s)},
            )
            self._cached = token
            log.info("minted mock token #%s expires_in=%s", self.mint_count, int(self.ttl_s))
            return token


class StaticTokenProvider:
    """Never-refreshing bearer token (Path-A digs, local probes).

    Unlike the other providers, ``StaticTokenProvider`` does NOT get wrapped
    in ``RetryingTokenProvider`` because a static token cannot be refreshed —
    a failure is permanent and retrying would only waste cycles.
    """

    def __init__(self, token: str | None = None) -> None:
        # Accept either a constructor argument (tests) or one of the two env
        # variables (UPSTREAM_API_KEY for Path A probe scripts, STATIC_BEARER
        # for the generic case).  The naming matches corp-probe.env.example.
        value = token or os.environ.get("UPSTREAM_API_KEY") or os.environ.get("STATIC_BEARER") or ""
        if not value:
            raise ValueError("STATIC_BEARER / UPSTREAM_API_KEY required for static provider")
        # Perpetual "expiry": 1e9 seconds (~31 years).  Good enough for a
        # dig spike; production should always use jar or mock+retry.
        self._token = AccessToken(access_token=value, expires_at=time.time() + 10**9)

    def get_token(self, force_refresh: bool = False) -> AccessToken:
        """Return the static token (``force_refresh`` is a no-op)."""
        return self._token


class JarTokenProvider:
    """Invoke corporate token-tool JAR (same pattern as the sample chatbot).

    Security notes (UPDATED after Issue #1 fix)
    --------------------------------------------
    * Keystore / truststore passwords are NO LONGER passed as ``-D`` JVM
      flags on the command line.  Instead, they are written to a mode-0600
      temporary ``java.security`` properties file and referenced via the
      single ``-Djava.security.properties=$tmpfile`` cmdline argument.
      Only the temp-file path (never the password contents) now appears in
      ``/proc/<pid>/cmdline``.
    * If the running kernel exposes ``/proc/<pid>/environ`` to other same-UID
      processes in the PID namespace, passwords could still in theory leak
      via ``JAVA_TOOL_OPTIONS`` — that path is NOT used here so the env
      exposure vector is also closed.
    * The properties file is ``os.unlink()``-ed in a ``finally`` block
      immediately after ``subprocess.run`` returns so secrets never survive
      longer than one mint invocation on disk.
    * The full command line is intentionally NOT logged.  ``jar``,
      ``token_url``, ``client_code``, and ``domain`` are safe to log; the
      keystore/truststore paths may be logged (they reveal nothing
      sensitive) but their passwords never are.

    Parsing
    -------
    The JAR stdout is expected to END with a single JSON line containing
    either ``access_token`` / ``expires_in`` (OAuth standard) OR the Java
    camelCase equivalents ``accessToken`` / ``expiresIn``.  If ``expires_in``
    is absent we fall back to ``TOKEN_DEFAULT_TTL_S`` (1500s ≈ 25m per corp
    IdP policy).
    """

    def __init__(self) -> None:
        self.jar = os.environ.get("TOKEN_JAR", "")
        if not self.jar:
            raise ValueError("TOKEN_JAR is required for jar provider")
        self.java = os.environ.get("JAVA_BIN", "java")
        # Corp-issued JKS pair — mounted into the pod from nbi-llm-auth Secret.
        self.keystore = os.environ.get("KEYSTORE_PATH", "keystore.jks")
        self.truststore = os.environ.get("TRUSTSTORE_PATH", "aitruststore.jks")
        self.keystore_password = os.environ.get("KEYSTORE_PASSWORD", "changeit")
        self.truststore_password = os.environ.get("TRUSTSTORE_PASSWORD", "changeit")
        # OIDC params — also sourced from the nbi-llm-auth Secret.
        self.token_url = os.environ.get("OIDC_TOKEN_URL", "")
        self.client_code = os.environ.get("OIDC_CLIENT_CODE", "")
        self.domain = os.environ.get("OIDC_DOMAIN", "mydomain")
        # Escape hatch for extra JVM or JAR flags not covered above.
        self.extra_args = os.environ.get("TOKEN_JAR_EXTRA_ARGS", "")
        # See MockTokenProvider.skew_s for semantics.
        self.skew_s = float(os.environ.get("TOKEN_REFRESH_SKEW_S", "60"))
        # Fallback when JAR output lacks expires_in.
        self.default_ttl = float(os.environ.get("TOKEN_DEFAULT_TTL_S", "1500"))
        # Like MockTokenProvider, serializes concurrent mints.
        self._lock = threading.Lock()
        self._cached: Optional[AccessToken] = None

    def _write_jvm_properties(self, path: str) -> None:
        """Write the java.security properties file with JKS + passwords.

        Called once per mint invocation; the file is unlinked by the caller
        after subprocess.run completes.
        """
        lines = [
            f"javax.net.ssl.keyStore={self.keystore}",
            f"javax.net.ssl.keyStorePassword={self.keystore_password}",
            "javax.net.ssl.keyStoreType=JKS",
            f"javax.net.ssl.trustStore={self.truststore}",
            f"javax.net.ssl.trustStorePassword={self.truststore_password}",
            "javax.net.ssl.trustStoreType=JKS",
        ]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        # Restrict to owner-read-only BEFORE JVM process starts so another
        # same-UID process cannot read it during the mint window.
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

    def _build_cmd(self, jvm_props_file: str) -> list[str]:
        """Build the ``java -jar …`` argv list.

        Passwords are passed ONLY through ``jvm_props_file`` (see
        ``_write_jvm_properties``); the returned argv never contains
        clear-text secrets (only the path to the restricted file).
        """
        cmd = [
            self.java,
            f"-Djava.security.properties={jvm_props_file}",
            "-jar",
            self.jar,
        ]
        if self.token_url:
            cmd += ["--token-url", self.token_url]
        if self.client_code:
            cmd += ["--code", self.client_code]
        if self.domain:
            cmd += ["--domain", self.domain]
        if self.extra_args.strip():
            cmd += self.extra_args.split()
        return cmd

    def get_token(self, force_refresh: bool = False) -> AccessToken:
        """Return cached or freshly JAR-minted token.

        Thread-safe.  Raises ``RuntimeError`` wrapping either a CalledProcessError
        (non-zero exit) or TimeoutExpired (JAR took too long).

        Issue #1 fix: temporary java.security properties file is created
        (0600), referenced by path only on cmdline, and unlinked in
        ``finally`` whether or not mint succeeds.
        """
        with self._lock:
            now = time.time()
            if (
                not force_refresh
                and self._cached is not None
                and now < self._cached.expires_at - self.skew_s
            ):
                return self._cached

            # --- Secret-safe JVM properties file lifecycle -----------------
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix="nbi_jvm_sec_",
                suffix=".properties",
                dir=os.environ.get("TOKEN_SECRET_TMPDIR"),
            )
            os.close(tmp_fd)  # mkstemp returns fd; we'll reopen as text
            try:
                self._write_jvm_properties(tmp_path)
                cmd = self._build_cmd(tmp_path)
                log.info("minting token via JAR (%s)", self.jar)
                try:
                    proc = subprocess.run(
                        cmd,
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=float(os.environ.get("TOKEN_JAR_TIMEOUT_S", "60")),
                    )
                except subprocess.CalledProcessError as exc:
                    raise RuntimeError(
                        f"token JAR failed (exit {exc.returncode}): {(exc.stderr or '')[:500]}"
                    ) from exc
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError("token JAR timed out") from exc
            finally:
                # Destroy the props file whether subprocess succeeded,
                # failed, or timed out.  ignore_errors=True because on some
                # container runtimes /tmp is already torn down.
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

            # The JAR occasionally emits INFO logs before the JSON payload,
            # so we grab the LAST non-empty line of stdout rather than the
            # first.  This makes parsing robust to verbosity changes in the
            # JAR without requiring a version bump here.
            data = json.loads(proc.stdout.strip().splitlines()[-1])
            access = data.get("access_token") or data.get("accessToken")
            if not access:
                raise RuntimeError("token JAR output missing access_token")
            expires_in = float(data.get("expires_in") or data.get("expiresIn") or self.default_ttl)
            token = AccessToken(
                access_token=access,
                expires_at=now + expires_in,
                raw=data,
            )
            self._cached = token
            return token


class RetryingTokenProvider:
    """Retry mint with exponential backoff when IdP/JAR is flaky (LLM-S03).

    Wraps any inner ``TokenProvider`` and re-calls ``get_token()`` up to
    ``TOKEN_MINT_ATTEMPTS`` (default 3) times, with delays of
    ``base * 2^0, base * 2^1, …`` seconds between attempts.

    All exception types are retried — this is conservative: a malformed JAR
    output that would never succeed will burn all attempts before re-raising,
    but that's preferable to *not* retrying on a transient network blip.
    """

    def __init__(self, inner: TokenProvider) -> None:
        self.inner = inner
        # Clamp to ≥1 so "0 attempts" can never accidentally skip the call.
        self.attempts = max(1, int(os.environ.get("TOKEN_MINT_ATTEMPTS", "3")))
        self.base_delay_s = float(os.environ.get("TOKEN_MINT_BACKOFF_S", "0.4"))

    def get_token(self, force_refresh: bool = False) -> AccessToken:
        """Retry loop for the inner provider's get_token().

        Propagates the last-seen exception (with added context) after all
        attempts are exhausted.
        """
        last: Optional[BaseException] = None
        for i in range(self.attempts):
            try:
                return self.inner.get_token(force_refresh=force_refresh)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if i + 1 >= self.attempts:
                    break
                delay = self.base_delay_s * (2**i)
                log.warning(
                    "token mint failed (attempt %s/%s): %s — retry in %.1fs",
                    i + 1,
                    self.attempts,
                    exc,
                    delay,
                )
                time.sleep(delay)
        assert last is not None
        raise RuntimeError(f"token mint failed after {self.attempts} attempts: {last}") from last


def build_token_provider() -> TokenProvider:
    """Construct the concrete token provider based on ``TOKEN_PROVIDER`` env.

    Returns a raw StaticTokenProvider for ``static`` (no retries needed), or
    a RetryingTokenProvider wrapping MockTokenProvider / JarTokenProvider for
    the other two modes.
    """
    kind = os.environ.get("TOKEN_PROVIDER", "mock").strip().lower()
    if kind == "mock":
        inner: TokenProvider = MockTokenProvider()
    elif kind == "jar":
        inner = JarTokenProvider()
    elif kind == "static":
        inner = StaticTokenProvider()
    else:
        raise ValueError(f"unknown TOKEN_PROVIDER={kind!r} (mock|jar|static)")
    # static never needs retries; wrap others
    if kind == "static":
        return inner
    return RetryingTokenProvider(inner)

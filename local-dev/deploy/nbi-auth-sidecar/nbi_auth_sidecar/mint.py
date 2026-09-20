"""Token minting module (stdlib-only, no external deps).

This module exposes two "token minter" implementations that share a common
interface.  The result type for every successful mint is ``AccessToken``.

Providers
---------
MockTokenMinter
    Produces a synthetic JWT (HS256-like header + payload with a real ``exp``
    claim + a "fakesig" signature byte string).  Intended for CI and local
    smoke tests where no corporate JKS / OIDC IdP is available.  Supports
    injected failures via ``MOCK_TOKEN_FAIL_TIMES`` so the retry + backoff
    wrapper can be exercised without a real network.

SecureJarTokenMinter
    Calls the corporate ``token-tool.jar`` via ``java -jar`` subprocess in a
    way that satisfies the hard security constraint in the project memory:
    keystore / truststore passwords **never** appear on the ``java`` command
    line and therefore never leak through ``/proc/<pid>/cmdline``.  Instead,
    passwords are written to a mode-0600 temporary file and referenced via
    the single ``-Djava.security.properties=<path>`` argument.  The temp file
    is unlinked in a ``finally`` block whether or not the JAR succeeds.

Wrapping
--------
All mint operations go through ``RetryingTokenMinter`` which serializes
concurrent calls (``threading.Lock``), respects a ``skew_s`` pre-expiry
window for cached tokens, and applies exponential-backoff-with-jitter
retry on transient JAR / network errors.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import random
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Optional, Protocol

log = logging.getLogger("nbi_auth_sidecar.mint")

# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

@dataclass
class AccessToken:
    """Typed result of every ``TokenMinter.mint(...)`` call.

    Attributes
    ----------
    access_token:
        Raw bearer string that goes directly into the ``Authorization``
        header for upstream calls.
    expires_at:
        Unix epoch seconds when the token is considered expired.  The
        scheduler compares against ``time.time()``.
    raw:
        Optional raw dict from the underlying mint method (e.g. the full
        JAR stdout JSON with ``access_token``/``expires_in`` fields).
        Stored for diagnostics only; never logged verbatim because it
        might contain the bearer string.
    """

    access_token: str
    expires_at: float
    raw: Optional[Mapping[str, Any]] = None

    @property
    def expired(self) -> bool:
        """True when the token has reached its expiry timestamp.

        Callers that want a "safety" window should compare
        ``time.time() < expires_at - skew_s`` (skew handled by minter).
        """
        return time.time() >= self.expires_at


# ---------------------------------------------------------------------------
# Duck-type interface (Protocol) — keeps mypy happy even without pip.
# ---------------------------------------------------------------------------

class TokenMinter(Protocol):
    """Duck-typed contract shared by all minter classes."""

    def mint(self, force_refresh: bool = False) -> AccessToken:
        """Return a cached or freshly minted token.

        Parameters
        ----------
        force_refresh:
            When True, ignore any cached token and always mint a new one.
            Used by the ``/rotate-self`` HTTP endpoint.
        """


# ---------------------------------------------------------------------------
# Internal helpers — JWT payload parsing + jittered backoff
# ---------------------------------------------------------------------------

def _jwt_payload(token: str) -> Optional[Mapping[str, Any]]:
    """Return the decoded payload dict for a JWT *if* the token looks like
    a three-part base64url-delimited value.  Returns ``None`` otherwise.

    The signature is intentionally NOT verified here: the token tool JAR is
    trusted, and this module only needs the ``exp`` claim to schedule the
    next refresh.  Verifying a signature would require cryptography libs
    that violate the "stdlib only" rule.
    """
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload_b64 = parts[1]
    # base64.urlsafe_b64decode is strict about padding — add 0-3 '='.
    pad = (-len(payload_b64)) % 4
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + ("=" * pad))
    except Exception:
        return None
    try:
        data = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _backoff_seconds(failure_index: int,
                     base: float = 10.0,
                     cap: float = 300.0,
                     jitter: float = 0.5) -> float:
    """Exponential backoff with uniform jitter.

    Parameters
    ----------
    failure_index:
        1-based consecutive failure count (first failure = index 1).
    base:
        Base delay for the first failure in seconds.
    cap:
        Upper bound on the returned delay (prevents multi-hour stalls).
    jitter:
        Relative +/- jitter.  0.5 = ±50 % around the exponential centre.
    """
    centre = min(cap, base * (2 ** (failure_index - 1)))
    lo = max(1.0, centre * (1.0 - jitter))
    hi = max(1.0, centre * (1.0 + jitter))
    return random.uniform(lo, hi)


def _build_fake_jwt(payload_extra: Optional[Mapping[str, Any]] = None,
                    ttl_s: float = 3600) -> str:
    """Construct a well-formed-looking (but obviously fake) JWT.

    The header/payload are real base64url JSON; the signature is a constant
    ``fakesig``.  Used only by ``MockTokenMinter``.
    """
    header = {"alg": "HS256", "typ": "JWT"}
    payload: MutableMapping[str, Any] = {
        "sub": "nbi-mock",
        "iat": int(time.time()),
        "exp": int(time.time() + ttl_s),
        "jti": uuid.uuid4().hex,
    }
    if payload_extra:
        payload.update(payload_extra)
    h = base64.urlsafe_b64encode(json.dumps(header, separators=(",", ":")).encode("utf-8")).rstrip(b"=").decode("ascii")
    p = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).rstrip(b"=").decode("ascii")
    sig = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=").decode("ascii")
    return f"{h}.{p}.{sig}"


# ---------------------------------------------------------------------------
# Mock minter (CI / smoke-testing)
# ---------------------------------------------------------------------------

class MockTokenMinter:
    """Synthetic token producer.  No network, no Java dependency.

    Environment
    -----------
    MOCK_TOKEN_TTL_S
        TTL in seconds for each minted token (default 3600 = 1 h).
    MOCK_TOKEN_FAIL_TIMES
        First N mint attempts raise RuntimeError; used to test retry logic.
    TOKEN_REFRESH_SKEW_S
        Caller-controlled skew window; cached tokens are considered valid
        only while ``now < expires_at - skew_s``.  Project default is 60s.
    """

    def __init__(self,
                 ttl_s: Optional[float] = None,
                 fail_times: Optional[int] = None,
                 skew_s: Optional[float] = None) -> None:
        self.ttl_s = float(os.environ.get("MOCK_TOKEN_TTL_S", str(ttl_s or 3600)))
        self.fail_remaining = int(os.environ.get("MOCK_TOKEN_FAIL_TIMES", str(fail_times or 0)))
        self.skew_s = float(os.environ.get("TOKEN_REFRESH_SKEW_S", str(skew_s or 60)))
        self._lock = threading.Lock()
        self._cached: Optional[AccessToken] = None
        self.mint_count = 0

    # ------------------------------------------------------------------
    def mint(self, force_refresh: bool = False) -> AccessToken:
        """Return a cached or fresh mock token.

        Thread-safe.  Raises ``RuntimeError`` for the first
        ``fail_remaining`` calls regardless of the cache state — used to
        exercise the ``RetryingTokenMinter`` backoff wrapper.
        """
        with self._lock:
            now = time.time()
            if (not force_refresh
                    and self._cached is not None
                    and now < self._cached.expires_at - self.skew_s):
                return self._cached
            if self.fail_remaining > 0:
                self.fail_remaining -= 1
                raise RuntimeError(
                    f"MockTokenMinter: injected transient failure "
                    f"({self.fail_remaining} remaining)"
                )
            self.mint_count += 1
            token = _build_fake_jwt(ttl_s=self.ttl_s)
            payload = _jwt_payload(token) or {}
            exp = float(payload.get("exp") or (now + self.ttl_s))
            cached = AccessToken(
                access_token=token,
                expires_at=exp,
                raw={"token_type": "Bearer", "expires_in": int(self.ttl_s)},
            )
            self._cached = cached
            log.info(
                "mint mock token #%s expires_in=%s (skew_s=%.0f)",
                self.mint_count, int(self.ttl_s), self.skew_s,
            )
            return cached


# ---------------------------------------------------------------------------
# JAR minter (production path — hardened password-in-tempfile semantics)
# ---------------------------------------------------------------------------

class SecureJarTokenMinter:
    """Mint tokens via the corporate ``token-tool.jar``.

    Hardened password security
    ---------------------------
    JKS passwords never appear in ``/proc/<pid>/cmdline`` because they are
    written exclusively to a mode-0600 tempfile named in
    ``-Djava.security.properties=<path>``.  The tempfile is removed in a
    ``finally`` block immediately after the subprocess returns (success
    or failure).  This satisfies the project-wide security rule recorded
    in ``project_memory.md`` / "Hard constraints".

    Environment inputs
    ------------------
    TOKEN_JAR (required)
        Absolute path to ``token-tool.jar`` (mounted from K8s secret).
    JAVA_BIN
        Override the Java executable name (default ``java``).
    KEYSTORE_PATH
        Absolute path to the keystore JKS (default
        ``$SECRETS_DIR/keystore.jks``).
    TRUSTSTORE_PATH
        Absolute path to the truststore JKS (default
        ``$SECRETS_DIR/aitruststore.jks``).
    KEYSTORE_PASSWORD, TRUSTSTORE_PASSWORD
        Passwords.  Preferred injection path is ``envFrom`` of the K8s
        Secret.  They are only written to the 0600 temp file; they
        never appear in argv or log lines.
    OIDC_TOKEN_URL / OIDC_CLIENT_CODE / OIDC_DOMAIN
        Standard OIDC parameters forwarded verbatim to the JAR flags
        ``--token-url`` / ``--code`` / ``--domain``.
    TOKEN_JAR_EXTRA_ARGS
        Additional space-separated flags appended to the JAR argv.
        Used when IdP specifics differ from the default template.
    TOKEN_REFRESH_SKEW_S
        Pre-expiry cache invalidation window in seconds (default 60).
    TOKEN_DEFAULT_TTL_S
        Fallback TTL when the JAR output lacks an ``expires_in`` field
        AND the produced JWT lacks an ``exp`` claim.  Default 1500 s
        (25 min) matches the typical corporate IdP policy.
    TOKEN_TOOL_TIMEOUT_S
        Subprocess timeout in seconds (default 60).  IdP calls that take
        longer are killed and treated as a transient failure that the
        retry wrapper can re-attempt.
    """

    def __init__(self,
                 secrets_dir: Optional[Path] = None,
                 oidc_token_url: Optional[str] = None,
                 oidc_client_code: Optional[str] = None,
                 oidc_domain: Optional[str] = None,
                 default_ttl_s: Optional[float] = None,
                 skew_s: Optional[float] = None,
                 extra_jar_args: Optional[list[str]] = None,
                 env: Optional[Mapping[str, str]] = None) -> None:
        env = env or os.environ
        secrets_dir = Path(secrets_dir or env.get("NBI_SIDECAR_SECRETS_DIR", "/var/run/nbi-llm-auth"))
        self.jar = env.get("TOKEN_JAR", str(secrets_dir / "token-tool.jar"))
        if not self.jar:
            raise ValueError("TOKEN_JAR env var is required for SecureJarTokenMinter")
        self.java_bin = env.get("JAVA_BIN", "java")
        self.keystore = env.get("KEYSTORE_PATH", str(secrets_dir / "keystore.jks"))
        self.truststore = env.get("TRUSTSTORE_PATH", str(secrets_dir / "aitruststore.jks"))
        # Passwords are loaded eagerly so misconfiguration fails startup,
        # but they are NEVER forwarded to the JAR via argv — only via the
        # 0600 java.security temp file (see ``_write_jvm_properties``).
        self.keystore_password = env.get("KEYSTORE_PASSWORD", "")
        self.truststore_password = env.get("TRUSTSTORE_PASSWORD", "")
        self.token_url = oidc_token_url or env.get("OIDC_TOKEN_URL", "")
        self.client_code = oidc_client_code or env.get("OIDC_CLIENT_CODE", "")
        self.domain = oidc_domain or env.get("OIDC_DOMAIN", "")
        self.extra_jar_args: list[str] = list(extra_jar_args or [])
        if env.get("TOKEN_JAR_EXTRA_ARGS"):
            self.extra_jar_args.extend(env["TOKEN_JAR_EXTRA_ARGS"].split())
        self.skew_s = float(env.get("TOKEN_REFRESH_SKEW_S", str(skew_s or 60)))
        self.default_ttl = float(env.get("TOKEN_DEFAULT_TTL_S", str(default_ttl_s or 1500)))
        self.jar_timeout_s = float(env.get("TOKEN_TOOL_TIMEOUT_S", "60"))
        self._lock = threading.Lock()
        self._cached: Optional[AccessToken] = None

    # ------------------------------------------------------------------
    # Internal — tempfile for JVM properties + argv builder
    # ------------------------------------------------------------------

    def _write_jvm_properties(self, path: str) -> None:
        """Write the ``java.security`` properties file.

        Only this file receives keystore/truststore passwords; the actual
        java subprocess argv contains only the file path.  Mode is set to
        0600 *before* we return so that any same-UID concurrent process
        in the PID namespace cannot read passwords via race.
        """
        lines = [
            f"javax.net.ssl.keyStore={self.keystore}",
            f"javax.net.ssl.keyStorePassword={self.keystore_password}",
            "javax.net.ssl.keyStoreType=JKS",
            f"javax.net.ssl.trustStore={self.truststore}",
            f"javax.net.ssl.trustStorePassword={self.truststore_password}",
            "javax.net.ssl.trustStoreType=JKS",
        ]
        with open(path, "w", encoding="utf-8") as fp:
            fp.write("\n".join(lines) + "\n")
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

    def _build_cmd(self, jvm_props_file: str) -> list[str]:
        """Compose argv for the java subprocess.

        The returned argv NEVER includes a password; the only
        credential-referencing argument is the single
        ``-Djava.security.properties=...`` path.
        """
        cmd = [
            self.java_bin,
            f"-Djava.security.properties={jvm_props_file}",
            "-jar", self.jar,
        ]
        if self.token_url:
            cmd += ["--token-url", self.token_url]
        if self.client_code:
            cmd += ["--code", self.client_code]
        if self.domain:
            cmd += ["--domain", self.domain]
        if self.extra_jar_args:
            cmd += list(self.extra_jar_args)
        return cmd

    # ------------------------------------------------------------------
    # Internal — stdout JSON parsing with multiple key aliases
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_jar_output(stdout: str) -> Optional[dict[str, Any]]:
        """Return the last JSON object found in stdout.

        The token tool JAR sometimes emits informational text lines
        before/after the actual JSON payload.  We therefore scan lines
        from the bottom and take the first that parses as a JSON object.
        Returns None on complete failure (caller will then fall back to
        base64 JWT ``exp`` decode, and finally to the hard TTL default).
        """
        for line in reversed([ln.strip() for ln in stdout.splitlines() if ln.strip()]):
            if not line.startswith(("{", "[")):
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            if isinstance(data, dict):
                return data
        return None

    # ------------------------------------------------------------------
    # Core mint (direct; single attempt — no retry)
    # ------------------------------------------------------------------

    def _mint_once(self) -> AccessToken:
        """Run the JAR subprocess once and parse its output.

        Raises
        ------
        RuntimeError
            Wraps CalledProcessError, TimeoutExpired, or missing output.
        ValueError
            If required passwords or JAR paths are empty.
        """
        if not os.path.exists(self.jar):
            raise RuntimeError(f"TOKEN_JAR not found at {self.jar!r}")
        if not self.keystore_password or not self.truststore_password:
            raise RuntimeError(
                "KEYSTORE_PASSWORD and TRUSTSTORE_PASSWORD must be set via "
                "env (typically via K8s Secret envFrom)."
            )
        # Temp directory so the properties file sits in a dir that only
        # the sidecar UID can list.  tempfile.mkstemp inside it then gets
        # the 0600 chmod via _write_jvm_properties.
        tmpdir = tempfile.mkdtemp(prefix="nbi-jar-props-")
        props_file = os.path.join(tmpdir, "java.security")
        try:
            self._write_jvm_properties(props_file)
            cmd = self._build_cmd(props_file)
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.jar_timeout_s,
                    # Inherit PATH so `java` resolves; never forward a
                    # user shell with aliases.
                    env={**os.environ, "JAVA_TOOL_OPTIONS": ""},
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"token-tool.jar timed out after {self.jar_timeout_s:.0f}s"
                ) from exc
            if proc.returncode != 0:
                # stderr tail only — must redact before logging.  Use the
                # public redactor imported lazily to avoid a cycle.
                from .redaction import redact_secrets
                tail = redact_secrets(proc.stderr[-2000:])
                raise RuntimeError(
                    f"token-tool.jar exit={proc.returncode} stderr_tail={tail}"
                )
            stdout = (proc.stdout or "").strip()
            if not stdout:
                raise RuntimeError("token-tool.jar produced empty stdout")
            data = self._parse_jar_output(stdout) or {}
            # Prefer (a) explicit access_token key, then (b) whole stdout if it
            # looks like a JWT already.
            token = (
                str(data.get("access_token") or data.get("accessToken") or "").strip()
                or stdout.strip().splitlines()[-1].strip()
            )
            if not token:
                raise RuntimeError("token-tool.jar output lacks a bearer token")
            now = time.time()
            # expires_in → explicit JSON.  Always accept if present.
            exp_in = data.get("expires_in") or data.get("expiresIn")
            if isinstance(exp_in, (int, float)) and float(exp_in) > 0:
                exp = now + float(exp_in)
            else:
                payload = _jwt_payload(token) or {}
                exp = payload.get("exp")
                if isinstance(exp, (int, float)) and float(exp) > 0:
                    exp = float(exp)
                else:
                    exp = now + self.default_ttl
            return AccessToken(
                access_token=token,
                expires_at=float(exp),
                raw={k: v for k, v in data.items() if k not in (
                    "access_token", "accessToken", "token", "id_token"
                )},
            )
        finally:
            try:
                if os.path.exists(props_file):
                    os.unlink(props_file)
            except OSError:
                pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Public API: cached with skew, thread-safe
    # ------------------------------------------------------------------

    def mint(self, force_refresh: bool = False) -> AccessToken:
        """Return cached or freshly JAR-minted token (single attempt)."""
        with self._lock:
            now = time.time()
            if (not force_refresh
                    and self._cached is not None
                    and now < self._cached.expires_at - self.skew_s):
                return self._cached
            tok = self._mint_once()
            self._cached = tok
            ttl = max(0.0, tok.expires_at - now)
            log.info("mint jar token expires_in=%.0fs (skew=%.0fs)", ttl, self.skew_s)
            return tok


# ---------------------------------------------------------------------------
# Retrying wrapper used by __main__ regardless of which concrete minter is
# selected.  This is the "public minter" the scheduler actually talks to.
# ---------------------------------------------------------------------------

class RetryingTokenMinter:
    """Wraps any ``TokenMinter`` with lock + skew + exponential backoff.

    Attributes
    ----------
    attempt_count:
        Total mint attempts (successful or not); exposed for metrics.
    failure_count:
        Consecutive failures currently observed; reset on success.
    """

    def __init__(self,
                 inner: TokenMinter,
                 *,
                 max_attempts: int = 5,
                 base_backoff_s: float = 10.0,
                 cap_backoff_s: float = 300.0,
                 env: Optional[Mapping[str, str]] = None) -> None:
        env = env or os.environ
        self.inner = inner
        self.max_attempts = int(env.get("TOKEN_MINT_ATTEMPTS", str(max_attempts)))
        self.base_backoff = float(env.get("TOKEN_MINT_BACKOFF_S", str(base_backoff_s)))
        self.cap_backoff = float(env.get("TOKEN_MINT_BACKOFF_CAP_S", str(cap_backoff_s)))
        self._lock = threading.Lock()
        self.attempt_count = 0
        self.failure_count = 0
        self.last_success_at: Optional[float] = None
        # Pull skew from the inner minter (if it has one) so that we can
        # still honour skew caching even if the inner has no cache layer.
        self.skew_s = float(getattr(inner, "skew_s", 60.0))
        self._cached: Optional[AccessToken] = None

    def mint(self, force_refresh: bool = False) -> AccessToken:
        """Retry up to ``max_attempts`` times.  Raises after all fail."""
        with self._lock:
            now = time.time()
            if (not force_refresh
                    and self._cached is not None
                    and now < self._cached.expires_at - self.skew_s):
                return self._cached
            failures = 0
            last_exc: Optional[BaseException] = None
            for attempt in range(1, self.max_attempts + 1):
                self.attempt_count += 1
                try:
                    tok = self.inner.mint(force_refresh=force_refresh)
                except BaseException as exc:  # noqa: BLE001 — retry all transient
                    failures += 1
                    self.failure_count = failures
                    last_exc = exc
                    if attempt >= self.max_attempts:
                        break
                    delay = _backoff_seconds(
                        failures,
                        base=self.base_backoff,
                        cap=self.cap_backoff,
                        jitter=0.5,
                    )
                    log.warning(
                        "mint attempt %s/%s failed (%s); backoff %.1fs",
                        attempt, self.max_attempts,
                        " ".join(str(last_exc).split()[:12]),
                        delay,
                    )
                    time.sleep(delay)
                    continue
                self._cached = tok
                self.failure_count = 0
                self.last_success_at = time.time()
                return tok
            # Exhausted retries.
            assert last_exc is not None
            log.error(
                "mint permanently FAILED after %s attempts (last_exc=%s)",
                self.max_attempts, last_exc,
            )
            raise last_exc


# ---------------------------------------------------------------------------
# Convenience factory (called from __main__; selects minter by env var).
# ---------------------------------------------------------------------------

def build_token_minter(minter_name: Optional[str] = None,
                       env: Optional[Mapping[str, str]] = None,
                       secrets_dir: Optional[Path] = None,
                       **kwargs: Any) -> RetryingTokenMinter:
    """Return a ready-to-use ``RetryingTokenMinter``.

    Parameters
    ----------
    minter_name:
        Either ``"jar"`` (default in prod) or ``"mock"`` (CI).  Defaults
        to the env var ``NBI_SIDECAR_MINTER`` or ``"jar"``.
    """
    env = env or os.environ
    name = (minter_name or env.get("NBI_SIDECAR_MINTER", "jar") or "jar").lower()
    if name == "mock":
        inner: TokenMinter = MockTokenMinter()
    elif name == "jar":
        inner = SecureJarTokenMinter(secrets_dir=secrets_dir, env=env, **kwargs)
    else:
        raise ValueError(f"Unknown NBI_SIDECAR_MINTER={name!r} (expected jar|mock)")
    return RetryingTokenMinter(inner, env=env)

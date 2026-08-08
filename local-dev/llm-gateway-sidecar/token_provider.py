#!/usr/bin/env python3
"""OIDC / gateway bearer token providers for the local LLM sidecar.

Providers
---------
mock  — mint a fake JWT-like token with configurable TTL (default).
jar   — subprocess ``java -jar TOKEN_JAR …`` (corp token-tool pattern).
static — use UPSTREAM_API_KEY / STATIC_BEARER as-is (no refresh).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional, Protocol

log = logging.getLogger("sidecar.token")


@dataclass
class AccessToken:
    access_token: str
    expires_at: float  # unix seconds
    raw: Optional[dict] = None

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


class TokenProvider(Protocol):
    def get_token(self, force_refresh: bool = False) -> AccessToken: ...


class MockTokenProvider:
    """Simulates JAR mint + refresh for local tests (LLM-S01.2, LLM-S03)."""

    def __init__(
        self,
        ttl_s: float | None = None,
        skew_s: float | None = None,
        fail_times: int = 0,
    ) -> None:
        self.ttl_s = float(os.environ.get("MOCK_TOKEN_TTL_S", ttl_s or 3600))
        self.skew_s = float(os.environ.get("TOKEN_REFRESH_SKEW_S", skew_s or 60))
        self._fail_remaining = int(os.environ.get("MOCK_TOKEN_FAIL_TIMES", fail_times))
        self._lock = threading.Lock()
        self._cached: Optional[AccessToken] = None
        self.mint_count = 0

    def get_token(self, force_refresh: bool = False) -> AccessToken:
        with self._lock:
            now = time.time()
            if (
                not force_refresh
                and self._cached is not None
                and now < self._cached.expires_at - self.skew_s
            ):
                return self._cached
            if self._fail_remaining > 0:
                self._fail_remaining -= 1
                raise RuntimeError("mock IdP unavailable (injected failure)")
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
    def __init__(self, token: str | None = None) -> None:
        value = token or os.environ.get("UPSTREAM_API_KEY") or os.environ.get("STATIC_BEARER") or ""
        if not value:
            raise ValueError("STATIC_BEARER / UPSTREAM_API_KEY required for static provider")
        self._token = AccessToken(access_token=value, expires_at=time.time() + 10**9)

    def get_token(self, force_refresh: bool = False) -> AccessToken:
        return self._token


class JarTokenProvider:
    """Invoke corporate token-tool JAR (same pattern as the sample chatbot)."""

    def __init__(self) -> None:
        self.jar = os.environ.get("TOKEN_JAR", "")
        if not self.jar:
            raise ValueError("TOKEN_JAR is required for jar provider")
        self.java = os.environ.get("JAVA_BIN", "java")
        self.keystore = os.environ.get("KEYSTORE_PATH", "keystore.jks")
        self.truststore = os.environ.get("TRUSTSTORE_PATH", "aitruststore.jks")
        self.keystore_password = os.environ.get("KEYSTORE_PASSWORD", "changeit")
        self.truststore_password = os.environ.get("TRUSTSTORE_PASSWORD", "changeit")
        self.token_url = os.environ.get("OIDC_TOKEN_URL", "")
        self.client_code = os.environ.get("OIDC_CLIENT_CODE", "")
        self.domain = os.environ.get("OIDC_DOMAIN", "mydomain")
        self.extra_args = os.environ.get("TOKEN_JAR_EXTRA_ARGS", "")
        self.skew_s = float(os.environ.get("TOKEN_REFRESH_SKEW_S", "60"))
        self.default_ttl = float(os.environ.get("TOKEN_DEFAULT_TTL_S", "1500"))
        self._lock = threading.Lock()
        self._cached: Optional[AccessToken] = None

    def _build_cmd(self) -> list[str]:
        cmd = [
            self.java,
            f"-Djavax.net.ssl.keyStore={self.keystore}",
            f"-Djavax.net.ssl.keyStorePassword={self.keystore_password}",
            f"-Djavax.net.ssl.keyStoreType=JKS",
            f"-Djavax.net.ssl.trustStore={self.truststore}",
            f"-Djavax.net.ssl.trustStorePassword={self.truststore_password}",
            f"-Djavax.net.ssl.trustStoreType=JKS",
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
        with self._lock:
            now = time.time()
            if (
                not force_refresh
                and self._cached is not None
                and now < self._cached.expires_at - self.skew_s
            ):
                return self._cached
            cmd = self._build_cmd()
            log.info("minting token via JAR (%s)", self.jar)
            # Do not log passwords / full cmd with secrets in production logs — redact.
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
    """Retry mint with exponential backoff when IdP/JAR is flaky (LLM-S03)."""

    def __init__(self, inner: TokenProvider) -> None:
        self.inner = inner
        self.attempts = max(1, int(os.environ.get("TOKEN_MINT_ATTEMPTS", "3")))
        self.base_delay_s = float(os.environ.get("TOKEN_MINT_BACKOFF_S", "0.4"))

    def get_token(self, force_refresh: bool = False) -> AccessToken:
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

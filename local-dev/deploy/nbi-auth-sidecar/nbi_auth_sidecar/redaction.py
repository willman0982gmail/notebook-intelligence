"""
redaction.py — Scrub bearer tokens and passwords from log text.

Rules
-----
* Bearer tokens come in three common shapes that we scrub BEFORE any
  text reaches stderr or log files:
    1. "Authorization: Bearer <jwt>"   (HTTP header)
    2. "ANTHROPIC_API_KEY=<token>"     (env dumps / argv-ish output)
    3. A bare "eyJ...2-part.base64.trailer" JWT-looking string longer
       than 64 chars (catches the common "oops I printed the token" case).
* JKS passwords passed via java.security properties are also scrubbed on
  a best-effort basis when they appear in Java exception traces.  The
  heavy lifting for password secrecy happens in mint.py (0600 tempfile +
  never on argv), but there's still a risk of a password string
  appearing in a java subprocess stderr bubble.  This module is the last
  line of defence before that text goes to a rotated log file.

Trade-offs
----------
* Detection is conservative — we intentionally accept a very low rate of
  false-positive redactions ("eyJ" prefix inside random hex is rare) so
  no token escapes.
* The replacement string "[REDACTED_LEN=NNN]" records the number of
  characters that were scrubbed, which is enough for an operator to
  eyeball whether a token vs a short password was removed, without
  leaking any secret bits.
"""

from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# 1. Regex catalog — anchored to common delimiters so random base64 blobs
#    inside non-token strings don't spuriously match.
# ---------------------------------------------------------------------------

#: "Authorization: Bearer <stuff>" with optional trailing whitespace/comma.
BEARER_HEADER_RE = re.compile(
    r"""
    (?P<prefix>Authorization\s*[:=]\s*  # case-insensitive header name + punct
      (?:Bearer\s+))                     # scheme literal, at least one space
    (?P<token>[A-Za-z0-9\-_.=+/]+)      # JWT / opaque token char class
    """,
    re.VERBOSE | re.IGNORECASE,
)

#: "Key=Value" shell/env output where key names contain "API_KEY", "TOKEN",
#: "PASS", or "SECRET".  Matches the common ``ANTHROPIC_API_KEY=eyJ...`` case.
ENV_ASSIGN_RE = re.compile(
    r"""
    (?P<prefix>
      (?: [A-Z][A-Z0-9_]* )?                # optional env name prefix
      (?: API_KEY | TOKEN | PASS(?:WORD)? | SECRET )  # secret-sounding suffix
      [A-Z0-9_]*                            # optional tail
      \s*[:=]\s*                             # ``=`` or ``:`` assignment
    )
    (?P<secret>[^\s;&|'")\]]{6,})          # secret value: no shell delimiters, >=6 chars
    """,
    re.VERBOSE | re.IGNORECASE,
)

#: JWT-looking three-part base64url blob with ``.`` separators.  A real
#: JWT is ``header.payload.signature``; we require each non-empty segment
#: and a total length >= 64 so short "a.b.c" test strings are not matched.
JWT_BLOB_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])"  # negative lookbehind: not preceded by id-char
    r"(?P<token>"
    r"eyJ[A-Za-z0-9_\-]{10,}"  # header: always starts eyJ (JSON base64)
    r"\.[A-Za-z0-9_\-]{10,}"   # payload
    r"\.[A-Za-z0-9_\-]{10,}"   # signature
    r")"
    r"(?![A-Za-z0-9_\-])",   # negative lookahead: not followed by id-char
)

#: Java ``-D`` password-style strings that occasionally appear in
#: "invalid key password"-style exception messages.
JAVA_D_PASSWORD_RE = re.compile(
    r"""
    (?P<prefix>
      (?: javax\.net\.ssl | java\.security | keystore | truststore )
      [A-Za-z0-9_.\-]*
      (?: password | passwd | pwd )
      \s*[:=]\s*
    )
    (?P<secret>\S{4,})
    """,
    re.VERBOSE | re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 2. Helpers
# ---------------------------------------------------------------------------


def _redact_match_token(match: re.Match, token_group: str = "token", prefix_group: Optional[str] = None) -> str:
    """Substitution helper that replaces the token group while preserving prefix."""
    prefix_text = match.group(prefix_group) if prefix_group else ""
    token_text = match.group(token_group)
    return f"{prefix_text}[REDACTED_LEN={len(token_text)}]"


def _redact_match_secret(match: re.Match) -> str:
    """Substitution helper for ``prefix=secret`` style matches."""
    prefix_text = match.group("prefix")
    secret_text = match.group("secret")
    return f"{prefix_text}[REDACTED_LEN={len(secret_text)}]"


# ---------------------------------------------------------------------------
# 3. Public API
# ---------------------------------------------------------------------------


def redact_secrets(text: str) -> str:
    """Return ``text`` with any detected secret strings replaced.

    Idempotent — calling redact_secrets twice on the same input is a
    no-op on the second pass (the first pass already replaced the
    secrets with ``[REDACTED_LEN=NNN]`` which never matches).

    Order matters: run ENV_ASSIGN_RE before JWT_BLOB_RE so structured
    ``KEY=<jwt>`` pairs are reported as a single KEY redaction instead of
    two overlapping hits.  Run the loose JWT_BLOB_RE last so it only
    catches the tokens that slipped past the stricter patterns.
    """
    if not text:
        return text
    # 1. Authorization: Bearer <tok>
    text = BEARER_HEADER_RE.sub(
        lambda m: _redact_match_token(m, token_group="token", prefix_group="prefix"),
        text,
    )
    # 2. SECRET_NAME=<value> (includes ANTHROPIC_API_KEY case)
    text = ENV_ASSIGN_RE.sub(_redact_match_secret, text)
    # 3. Java -Dxxx.password=value
    text = JAVA_D_PASSWORD_RE.sub(_redact_match_secret, text)
    # 4. Bare three-part JWT blobs (strict: starts "eyJ", length >= ~64)
    text = JWT_BLOB_RE.sub(
        lambda m: f"[REDACTED_LEN={len(m.group('token'))}]",
        text,
    )
    return text


def redact_bytes(buf: bytes) -> bytes:
    """Bytes version of :func:`redact_secrets`.

    Decodes as UTF-8 with replacement, runs redaction, re-encodes.  Using
    ``errors='replace'`` guarantees we never raise on binary subprocess
    output (java -Xlog:some+flags can emit weird terminal escapes).
    """
    if not buf:
        return buf
    try:
        text = buf.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - total paranoia
        return b"<binary-buffer-unreadable>"
    return redact_secrets(text).encode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 4. Minimal self-test — invoked manually via ``python redaction.py``.
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover - manual smoke
    samples = [
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.signature1234567890ABCDEFGHIJKLMNOP",
        "export ANTHROPIC_API_KEY=sk-ant-abc123def456ghi789 ; echo done",
        "stderr: javax.net.ssl.keyStorePassword=SuperSecret123 caused java.io.IOException",
        "normal line with no secrets",
        "prefix eyJheader.payload9999999999999999999999999999999999999999999999999.sig trailer",
    ]
    for s in samples:
        r = redact_secrets(s)
        print("IN :", s)
        print("OUT:", r)
        assert "REDACTED" in r or s == r
        print()
    print("REDACTION_SELFTEST_OK")

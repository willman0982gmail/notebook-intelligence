#!/usr/bin/env python3
"""Fake token-tool JAR stand-in for local TOKEN_PROVIDER=jar tests (LLM-S01.2 / S03).

Usage (sidecar):
  TOKEN_PROVIDER=jar \\
  JAVA_BIN=python3 \\
  TOKEN_JAR=local-dev/llm-gateway-sidecar/fake_token_jar.py \\
  ./local-dev/start-sidecar.sh

The real JAR is invoked as: java [jvm-flags] -jar TOKEN_JAR …
When JAVA_BIN=python3, those jvm-flags are ignored by Python; only the script
path after -jar is executed if you wrap via this helper — for a closer match,
use the shell wrapper fake_token_jar.sh as JAVA_BIN.
"""

from __future__ import annotations

import json
import os
import time
import uuid

expires_in = int(os.environ.get("MOCK_TOKEN_TTL_S", "120"))
print(
    json.dumps(
        {
            "access_token": f"fake-jar-{uuid.uuid4().hex}",
            "expires_in": expires_in,
            "token_type": "Bearer",
            "minted_at": int(time.time()),
        }
    )
)

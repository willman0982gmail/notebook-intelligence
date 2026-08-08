#!/usr/bin/env python3
"""Regression tests for LLMQuotaHandler (LLM-S19 / S24)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from notebook_intelligence.extension import LLMQuotaHandler


def _handler():
    h = LLMQuotaHandler.__new__(LLMQuotaHandler)
    h.finish = MagicMock()
    h.set_status = MagicMock()
    # Bypass @tornado.web.authenticated (jupyter-server 2+ uses current_user).
    h._current_user = "test-user"
    return h


def test_llm_quota_handler_proxies_sidecar():
    h = _handler()
    payload = {
        "user_id": "alice",
        "plan_id": "standard",
        "used_tokens": 10,
        "limit_tokens": 100,
        "remaining_tokens": 90,
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        h.get()

    body = json.loads(h.finish.call_args[0][0])
    assert body["available"] is True
    assert body["plan_id"] == "standard"
    assert body["remaining_tokens"] == 90


def test_llm_quota_handler_soft_fail_when_down():
    h = _handler()
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        h.get()
    body = json.loads(h.finish.call_args[0][0])
    assert body["available"] is False
    assert "error" in body

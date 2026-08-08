#!/usr/bin/env python3
"""Unit tests for local-dev sidecar + quota (no Jupyter required).

Requires Python >= 3.12 (use conda env nbi-jl45 or local-dev/python.sh).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

if sys.version_info < (3, 12):
    raise SystemExit(
        f"ERROR: local-dev tests require Python >= 3.12 (got {sys.version}). "
        "Use: conda activate nbi-jl45  or  ./local-dev/python.sh"
    )

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "llm-gateway-sidecar"))
sys.path.insert(0, str(ROOT / "hub"))

from quota_store import FileQuotaStore, canonicalize_user  # noqa: E402
from token_provider import MockTokenProvider  # noqa: E402


def test_canonicalize():
    assert canonicalize_user("JDoe@Corp.COM") == "jdoe"
    assert canonicalize_user("alice") == "alice"


def test_mock_token_refresh():
    p = MockTokenProvider(ttl_s=1.0, skew_s=0.2)
    t1 = p.get_token()
    t2 = p.get_token()
    assert t1.access_token == t2.access_token
    assert p.mint_count == 1
    time.sleep(0.9)
    t3 = p.get_token()  # within skew window near expiry → refresh
    assert p.mint_count >= 2
    assert t3.access_token != t1.access_token


def test_token_mint_backoff_retries():
    """LLM-S03 — RetryingTokenProvider recovers after transient IdP failures."""
    from token_provider import RetryingTokenProvider

    inner = MockTokenProvider(ttl_s=3600, skew_s=60, fail_times=2)
    os.environ["TOKEN_MINT_ATTEMPTS"] = "4"
    os.environ["TOKEN_MINT_BACKOFF_S"] = "0.01"
    wrap = RetryingTokenProvider(inner)
    tok = wrap.get_token()
    assert tok.access_token.startswith("mock-")
    assert inner.mint_count == 1


def test_quota_plans_and_forge_ignored():
    os.environ.pop("QUOTA_DEFAULT_PLAN", None)
    with tempfile.TemporaryDirectory() as td:
        store = FileQuotaStore(Path(td) / "q.json")
        # intern group
        d = store.check("a@corp.com", 10, "databricks/gdp-gpt4o", groups=["interns"])
        assert d.allowed and d.plan_id == "intern", d
        # forged power hint must not escalate
        d2 = store.check(
            "bob",
            10,
            "databricks/gdp-gpt4o",
            groups=[],
            hint_plan="power",
        )
        assert d2.plan_id == "standard", d2
        # model deny on intern
        d3 = store.check(
            "intern1",
            10,
            "databricks/gdp-gpt4o-large",
            groups=["interns"],
        )
        assert not d3.allowed, d3


def test_quota_durable_and_429():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "q.json"
        store = FileQuotaStore(
            path,
            plans={
                "local": {
                    "id": "local",
                    "tokens_per_day": 100,
                    "requests_per_day": 50,
                    "models": ["*"],
                },
                "standard": {
                    "id": "standard",
                    "tokens_per_day": 100,
                    "requests_per_day": 50,
                    "models": ["*"],
                },
            },
        )
        os.environ["QUOTA_DEFAULT_PLAN"] = "local"
        assert store.check("u1", 10, "m", hint_plan="local").allowed
        store.commit("u1", 80, "m", feature="chat")
        store.commit("u1", 30, "m", feature="chat")
        # reload
        store2 = FileQuotaStore(path, plans=store.plans)
        snap = store2.snapshot("u1")
        assert snap["used_tokens"] == 110
        denied = store2.check("u1", 10, "m", hint_plan="local")
        assert not denied.allowed
        assert "quota exceeded" in denied.message.lower() or "exceeded" in denied.message.lower()


def test_pre_spawn_resolve_local():
    from pre_spawn_hook import canonicalize_user as cu
    from pre_spawn_hook import resolve_plan_local

    assert cu("X@Y.Z") == "x"
    assert resolve_plan_local("a", ["interns"])["id"] == "intern"
    assert resolve_plan_local("a", ["ml-platform"])["id"] == "power"
    assert resolve_plan_local("a", [])["id"] == "standard"


def test_fake_jar_token_provider():
    import subprocess

    env = os.environ.copy()
    root = ROOT
    env.update(
        {
            "HOST": "127.0.0.1",
            "PORT": "18090",
            "MODE": "mock",
            "TOKEN_PROVIDER": "jar",
            "JAVA_BIN": str(root / "llm-gateway-sidecar" / "fake_java.sh"),
            "TOKEN_JAR": str(root / "llm-gateway-sidecar" / "fake_token_jar.py"),
            "MOCK_TOKEN_TTL_S": "60",
            "QUOTA_BACKEND": "local",
            "QUOTA_DEFAULT_PLAN": "local",
            "NBI_LLM_USER": "jaruser",
            "NBI_LLM_PLAN": "local",
            "QUOTA_STORE_PATH": str(root / ".runtime" / "test-quota-jar.json"),
            "QUOTA_TOKENS_DAY": "50000",
        }
    )
    (root / ".runtime").mkdir(exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, str(root / "llm-gateway-sidecar" / "sidecar.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(40):
            try:
                with urllib.request.urlopen("http://127.0.0.1:18090/healthz", timeout=1) as r:
                    body = json.loads(r.read().decode())
                    assert body.get("token_provider") == "jar"
                    assert body.get("token_warm") is True
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        else:
            out = proc.stdout.read().decode() if proc.stdout else ""
            raise AssertionError(f"jar sidecar failed\n{out}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_soft_cap_and_feature_budgets():
    """LLM-S14.4 soft-cap (≥80%) + LLM-S22 per-feature caps."""
    with tempfile.TemporaryDirectory() as td:
        # Soft-cap only (no per-feature caps)
        store = FileQuotaStore(
            Path(td) / "q.json",
            plans={
                "standard": {
                    "id": "standard",
                    "tokens_per_day": 100,
                    "requests_per_day": 500,
                    "models": ["*"],
                },
            },
        )
        os.environ["QUOTA_DEFAULT_PLAN"] = "standard"
        d = store.check("alice", 10, "m", feature="chat")
        assert d.allowed and not d.soft_cap_hit
        store.commit("alice", 85, "m", feature="chat")
        soft = store.check("alice", 5, "m", feature="chat")
        assert soft.allowed and soft.soft_cap_hit, soft
        snap = store.snapshot("alice")
        assert snap["soft_cap_hit"] is True
        assert snap["remaining_tokens"] == 15

        store2 = FileQuotaStore(
            Path(td) / "q2.json",
            plans={
                "standard": {
                    "id": "standard",
                    "tokens_per_day": 10000,
                    "requests_per_day": 500,
                    "tokens_per_day_chat": 5000,
                    "tokens_per_day_inline": 40,
                    "models": ["*"],
                },
            },
        )
        store2.commit("bob", 38, "m", feature="inline")
        denied_inline = store2.check("bob", 5, "m", feature="inline")
        assert not denied_inline.allowed
        assert "inline" in (denied_inline.message or "").lower()


def test_two_plan_users():
    """LLM-S11.5 — intern vs standard via group rules."""
    os.environ.pop("QUOTA_DEFAULT_PLAN", None)
    with tempfile.TemporaryDirectory() as td:
        store = FileQuotaStore(Path(td) / "q.json")
        intern = store.check("a@corp.com", 10, "databricks/gdp-gpt4o", groups=["interns"])
        std = store.check("bob@corp.com", 10, "databricks/gdp-gpt4o", groups=[])
        assert intern.plan_id == "intern"
        assert std.plan_id == "standard"
        assert intern.limit_tokens < std.limit_tokens


def test_redact_secrets():
    from sidecar import redact_secrets

    s = redact_secrets('Authorization: Bearer sk-secret-token-xyz POST /v1')
    assert "sk-secret" not in s
    assert "[REDACTED]" in s


def test_usage_summary_endpoint():
    """LLM-S17.2 daily rollup via Quota Service."""
    import subprocess

    store_path = ROOT / ".runtime" / "test-quota-svc.json"
    store_path.parent.mkdir(exist_ok=True)
    store_path.unlink(missing_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "QUOTA_HOST": "127.0.0.1",
            "QUOTA_PORT": "18092",
            "QUOTA_STORE_PATH": str(store_path),
            "QUOTA_PLANS_PATH": str(ROOT / "quota_service" / "plans.json"),
        }
    )
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "quota_service" / "server.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(40):
            try:
                with urllib.request.urlopen("http://127.0.0.1:18092/healthz", timeout=1) as r:
                    assert r.status == 200
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        else:
            raise AssertionError("quota service failed to start")

        # commit via API
        for feat, toks in (("chat", 10), ("inline", 5), ("chat", 7)):
            req = urllib.request.Request(
                "http://127.0.0.1:18092/v1/quota/commit",
                data=json.dumps(
                    {
                        "username": "alice@corp.com",
                        "tokens": toks,
                        "model": "databricks/gdp-gpt4o",
                        "feature": feat,
                        "groups": ["interns"],
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                assert r.status == 200
        with urllib.request.urlopen("http://127.0.0.1:18092/v1/usage/summary", timeout=3) as r:
            body = json.loads(r.read().decode())
        summary = body["summary"]
        assert summary
        day = next(iter(summary))
        alice = summary[day]["alice"]
        assert alice["tokens"] == 22
        assert alice["by_feature"]["chat"] == 17
        assert alice["by_feature"]["inline"] == 5
        with urllib.request.urlopen("http://127.0.0.1:18092/metrics", timeout=3) as r:
            metrics = r.read().decode()
        assert "nbi_quota_tokens_window_total" in metrics
        assert 'user="alice"' in metrics
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_loopback_bind_refused():
    """LLM-S01.3 — sidecar refuses non-loopback without override."""
    import subprocess

    env = os.environ.copy()
    env.update(
        {
            "HOST": "0.0.0.0",
            "PORT": "18093",
            "MODE": "mock",
            "TOKEN_PROVIDER": "mock",
            "QUOTA_BACKEND": "local",
            "QUOTA_DEFAULT_PLAN": "local",
        }
    )
    env.pop("SIDECAR_ALLOW_NON_LOOPBACK", None)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "llm-gateway-sidecar" / "sidecar.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        out, _ = proc.communicate(timeout=5)
        text = (out or b"").decode()
        assert proc.returncode != 0
        assert "loopback" in text.lower()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_sidecar_http_smoke():
    """Spawn sidecar briefly and hit health + chat + inline + soft-cap metric."""
    import subprocess

    env = os.environ.copy()
    env.update(
        {
            "HOST": "127.0.0.1",
            "PORT": "18089",
            "MODE": "mock",
            "TOKEN_PROVIDER": "mock",
            "QUOTA_BACKEND": "local",
            "QUOTA_DEFAULT_PLAN": "local",
            "NBI_LLM_USER": "testuser",
            "NBI_LLM_PLAN": "local",
            "QUOTA_STORE_PATH": str(ROOT / ".runtime" / "test-quota.json"),
            "QUOTA_TOKENS_DAY": "50000",
        }
    )
    (ROOT / ".runtime").mkdir(exist_ok=True)
    (ROOT / ".runtime" / "test-quota.json").unlink(missing_ok=True)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "llm-gateway-sidecar" / "sidecar.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(40):
            try:
                with urllib.request.urlopen("http://127.0.0.1:18089/healthz", timeout=1) as r:
                    assert r.status == 200
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        else:
            out = proc.stdout.read().decode() if proc.stdout else ""
            raise AssertionError(f"sidecar did not start\n{out}")

        def chat(content: str, feature: str = "chat") -> dict:
            req = urllib.request.Request(
                "http://127.0.0.1:18089/v1/chat/completions",
                data=json.dumps(
                    {
                        "model": "databricks/gdp-gpt4o",
                        "messages": [{"role": "user", "content": content}],
                        "stream": False,
                    }
                ).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-NBI-Feature": feature,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode())

        body = chat("hi")
        assert body["choices"][0]["message"]["content"]
        # Multi-turn style payload (LLM-S10.4 local)
        req_mt = urllib.request.Request(
            "http://127.0.0.1:18089/v1/chat/completions",
            data=json.dumps(
                {
                    "model": "databricks/gdp-gpt4o",
                    "messages": [
                        {"role": "user", "content": "q1"},
                        {"role": "assistant", "content": "a1"},
                        {"role": "user", "content": "q2"},
                    ],
                    "stream": False,
                }
            ).encode(),
            headers={"Content-Type": "application/json", "X-NBI-Feature": "chat"},
            method="POST",
        )
        with urllib.request.urlopen(req_mt, timeout=5) as r:
            assert r.status == 200
        # Inline FIM-ish prompt (LLM-S09)
        inline = chat("prefix <|fim|> suffix", feature="inline")
        assert inline["choices"][0]["message"]["content"]
        with urllib.request.urlopen("http://127.0.0.1:18089/metrics", timeout=2) as r:
            text = r.read().decode()
            assert "nbi_llm_requests_total" in text
            assert "nbi_llm_soft_cap_hits_total" in text
            assert "nbi_llm_requests_by_feature_total" in text
            assert 'feature="inline"' in text
            assert 'feature="chat"' in text
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    tests = [
        test_canonicalize,
        test_mock_token_refresh,
        test_token_mint_backoff_retries,
        test_quota_plans_and_forge_ignored,
        test_quota_durable_and_429,
        test_soft_cap_and_feature_budgets,
        test_two_plan_users,
        test_redact_secrets,
        test_usage_summary_endpoint,
        test_loopback_bind_refused,
        test_pre_spawn_resolve_local,
        test_fake_jar_token_provider,
        test_sidecar_http_smoke,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}: {exc}")
    raise SystemExit(1 if failed else 0)

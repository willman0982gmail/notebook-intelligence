"""JupyterHub pre_spawn_hook: bind Hub login → NBI_LLM_* env (LLM-S11.3).

This hook is invoked by JupyterHub **every time** a user's singleuser server is
spawned OR resumed.  It bridges the Hub's identity layer (``spawner.user``) to
the sidecar's configuration layer by injecting a handful of environment
variables into the singleuser pod spec.

The hook is intentionally defensive:

* It never raises (an unhandled exception here would prevent Hub from spawning
  ANY user's notebook).  All failures degrade gracefully via the local
  ``resolve_plan_local`` fallback.
* It canonicalizes the username so that the quota counters key is stable
  regardless of case / email-domain suffix.
* When a Quota Service URL is provided, the hook uses it to resolve the
  **authoritative** plan (server-side overrides are applied by the service).
  If that HTTP call fails we fall back to a local group→plan map — UX hint
  values are slightly stale but enforcement in the sidecar/quota-service is
  still fully correct because they re-resolve server-side on every request.

Usage in jupyterhub_config.py
-----------------------------
::

    from pre_spawn_hook import make_pre_spawn_hook
    c.KubeSpawner.pre_spawn_hook = make_pre_spawn_hook(
        quota_service_url="http://nbi-quota.jhub.svc.cluster.local:8090",
    )
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, Callable, Optional

log = logging.getLogger("nbi.pre_spawn")


def canonicalize_user(username: str) -> str:
    """Stable identity key — mirrors ``quota_store.canonicalize_user``.

    Kept as a local copy (rather than an import of quota_store) so that the
    Hub pod image does not need to carry the entire llm-gateway-sidecar
    source tree — Hub only needs this one file on PYTHONPATH.

    NOTE: Cross-file sync counterpart — this function MUST produce byte-
    identical output to ``quota_store.canonicalize_user`` in
    ``local-dev/llm-gateway-sidecar/quota_store.py``.  If you edit either,
    edit BOTH and add an integration test that asserts equality for a
    matrix of inputs (empty / mixed case / email suffix / unicode).
    """
    u = (username or "").strip().lower()
    if "@" in u:
        u = u.split("@", 1)[0]
    # Issue #6b fix: unify empty-input sentinel with quota_store.
    # Previously this returned "" for anonymous/missing usernames while
    # quota_store returned "anonymous", causing two separate counter keys
    # to be created (the Hub wrote "" into NBI_LLM_USER, then the sidecar
    # re-canonicalized "" → "anonymous" on its own check/commit path,
    # silently losing quota attribution).
    return u or "anonymous"


def resolve_plan_via_http(
    base_url: str, username: str, groups: list[str], auth_state: dict
) -> dict[str, Any]:
    """Resolve a user's plan against the central Quota Service.

    The service is responsible for applying user overrides (from its own
    ``user_overrides.json``) and authoritative group rules.  We intentionally
    send ONLY the *names* of auth_state keys (not values) — enough for
    future audit correlation, but never the raw OIDC tokens / session secrets
    that live in auth_state.

    Timeout is 10s (configurable only by source edit here) to avoid stalling
    spawn for longer than a user will wait.  Failures propagate as exceptions
    so the caller can degrade to the local fallback.
    """
    body = json.dumps(
        {"username": username, "groups": groups, "auth_state_keys": list(auth_state.keys())}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/plans/resolve",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_plan_local(username: str, groups: list[str]) -> dict[str, Any]:
    """Fallback when Quota Service is unreachable (still inject identity).

    The local fallback must mirror the most common plan values — it is ONLY
    used for UX hint env vars (``NBI_LLM_PLAN``, limit hints).  Enforcement
    in sidecar+Quota Service re-resolves server-side, so the only user-visible
    effect of stale fallback values is a slightly-wrong badge BEFORE the
    first chat (the badge then refreshes via sidecar /quota call).
    """
    rules = {"interns": "intern", "ml-platform": "power"}
    for g in groups:
        if g in rules:
            pid = rules[g]
            break
    else:
        pid = "standard"
    catalog = {
        "intern": {"id": "intern", "tokens_per_day": 200000, "requests_per_day": 200},
        "standard": {"id": "standard", "tokens_per_day": 2000000, "requests_per_day": 2000},
        "power": {"id": "power", "tokens_per_day": 20000000, "requests_per_day": 20000},
    }
    return catalog[pid]


def make_pre_spawn_hook(
    quota_service_url: Optional[str] = None,
    *,
    nbi_auth_sidecar_config: Optional[dict[str, Any]] = None,
) -> Callable[[Any], None]:
    """Factory returning an ``async`` spawn hook suitable for KubeSpawner.

    Parameters
    ----------
    quota_service_url : Optional[str]
        Base URL of the centralized quota service.  When set, the hook
        attempts HTTP plan resolution and also injects ``QUOTA_BACKEND=http``
        into the pod so the sidecar uses the service (not a local file).
        When ``None`` / empty the hook operates fully offline — suitable for
        laptops and the default dig environment.
    nbi_auth_sidecar_config : Optional[dict[str, Any]]
        Configuration for the NBI auth sidecar injection block.  Setting this
        to a truthy dict is the "feature flag" that enables Task 9's
        idempotent injection:
          * Appends the ``nbi-auth-sidecar`` dedicated container to
            ``spawner.extra_containers`` (list-dedup by container name so
            repeated hook calls never double-inject).
          * BOTH containers (notebook + sidecar) receive envFrom on Secret
            ``nbi-llm-auth`` (JKS paths/passwords/URLs + OIDC creds).
          * Shared ``nbi-shared-tmp`` emptyDir volume mounted at ``/tmp`` so
            the runtime env ferry JSON and state file are visible across
            containers.
          * Secret ``nbi-llm-auth`` volume mounted at ``/var/run/nbi-llm-auth``
            read-only so the JVM subprocess reads JKS straight from the
            kubelet-managed tmpfs mount (no plaintext on disk).
          * K8s liveness + readiness probes on the sidecar HTTP surface with
            ``host: 127.0.0.1`` explicitly so probes reach the loopback-bound
            server even though the sidecar never exposes a Service.
          * Hardened securityContext: nonRoot UID1000, ALL capabilities dropped,
            seccompProfile=RuntimeDefault, allowPrivilegeEscalation=false.
          * Resource requests/limits kept modest (50m/256Mi → 500m/768Mi) to
            match the Java subprocess peak RSS (openjdk-17 JRE is ~180MB +
            Python is ~40MB → 256Mi request is comfortable).
        ``None`` / falsy dict means sidecar injection is SKIPPED entirely —
        this preserves 100% backward compatibility for existing
        pre_spawn_hook callers (Hub rollout that hasn't updated the config
        yet does not break).

        Supported keys (all optional because defaults mirror the Helm overlay
        ``values.yaml`` shape closely):
            image: str (default "ghcr.io/scbdev/notebook-intelligence/nbi-auth-sidecar:latest")
            image_pull_policy: str ("IfNotPresent" | "Always" | "Never")
            http_port: int (default 18090)
            secret_name: str (default "nbi-llm-auth")
            extra_env: dict[str, str] (merged into sidecar env)
            resources: dict (requests/limits override)
            probes_enabled: bool (default True)
            security_context: dict (override default hardening)
            inject_nbi_default_provider_env: bool (default True — emit 8
              STRING_OVERRIDE provider/model envs from the ``auth_env``
              sub-dict of nbi_auth_sidecar_config)

    Returns
    -------
    Callable[[Spawner], Awaitable[None]]
        Async function to be assigned to ``c.KubeSpawner.pre_spawn_hook``.
    """

    # --- Auth sidecar helper closures (only meaningful when enabled) -------
    sidecar_cfg = dict(nbi_auth_sidecar_config or {})
    sidecar_enabled = bool(sidecar_cfg)

    # Defaults that align with charts/nbi-auth-sidecar/values.yaml so admins
    # only have to set things ONCE (in Helm values) and the Python hook reads
    # the exact same structure.
    _sidecar_image = sidecar_cfg.get("image", "ghcr.io/scbdev/notebook-intelligence/nbi-auth-sidecar:latest")
    _sidecar_image_pull_policy = sidecar_cfg.get("image_pull_policy", "IfNotPresent")
    _sidecar_http_port = int(sidecar_cfg.get("http_port", 18090))
    _secret_name = sidecar_cfg.get("secret_name", "nbi-llm-auth")
    _shared_tmp_vol = "nbi-shared-tmp"
    _auth_secret_vol = "nbi-llm-auth-vol"
    _extra_sidecar_env: dict = dict(sidecar_cfg.get("extra_env") or {})
    _default_resources = {
        "requests": {"cpu": "50m", "memory": "256Mi"},
        "limits":   {"cpu": "500m", "memory": "768Mi"},
    }
    _resources = sidecar_cfg.get("resources") or _default_resources
    _probes_enabled = bool(sidecar_cfg.get("probes_enabled", True))
    _default_security_context = {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    _security_context = sidecar_cfg.get("security_context") or _default_security_context
    _inject_provider_env = bool(sidecar_cfg.get("inject_nbi_default_provider_env", True))
    # Provider/model defaults to inject via spawner.environment so the notebook
    # container's NBI sees them as OS env vars *even before* the first ferry
    # merge happens.  Keys mirror STRING_OVERRIDE_SPEC.
    _auth_env: dict = dict(sidecar_cfg.get("auth_env") or {})

    async def _inject_nbi_auth_sidecar(spawner: Any) -> None:
        """Mutate spawner pod spec to include auth sidecar.  Idempotent.

        Z2JK KubeSpawner exposes `spawner.extra_containers` as a list we can
        append to, `spawner.volumes` and `spawner.volume_mounts` as the
        notebook-container-level mounts, and `spawner.environment` as a dict
        of plain env vars.  Per-container envFrom is trickier — Z2JK only
        exposes it via the `KubeSpawner.extra_container_config` traitlet OR a
        direct `modify_pod_hook`-style dict walk.  We mutate the concrete
        containers-list element because extra_containers entries are dicts.
        """
        # ---- A. Volumes (shared tmp + secret mount) -----------------------
        # Ensure volumes list exists, mutate in place.
        existing_vols = list(getattr(spawner, "volumes", None) or [])
        vol_names = {v.get("name") for v in existing_vols if isinstance(v, dict)}
        if _shared_tmp_vol not in vol_names:
            existing_vols.append({"name": _shared_tmp_vol, "emptyDir": {}})
        if _auth_secret_vol not in vol_names:
            existing_vols.append({
                "name": _auth_secret_vol,
                "secret": {
                    "secretName": _secret_name,
                    "defaultMode": 0o400,  # read-only to container user
                    "optional": False,
                },
            })
        spawner.volumes = existing_vols

        # ---- B. Notebook container envFrom for secrets ------------------
        # Z2JK's KubeSpawner now supports `env_from` traitlet for notebook.
        cur_env_from = list(getattr(spawner, "env_from", None) or [])
        env_from_ref_names = []
        for ref in cur_env_from:
            if isinstance(ref, dict) and "secretRef" in ref:
                env_from_ref_names.append(ref["secretRef"].get("name"))
        if _secret_name not in env_from_ref_names:
            cur_env_from.append({"secretRef": {"name": _secret_name, "optional": False}})
        spawner.env_from = cur_env_from

        # ---- C. Notebook container volume mounts -------------------------
        existing_mounts = list(getattr(spawner, "volume_mounts", None) or [])
        mount_paths = {m.get("mountPath") for m in existing_mounts if isinstance(m, dict)}
        if "/tmp" not in mount_paths:
            existing_mounts.append({
                "name": _shared_tmp_vol,
                "mountPath": "/tmp",
            })
        spawner.volume_mounts = existing_mounts

        # ---- D. Inject 8 STRING_OVERRIDE provider envs into notebook -----
        if _inject_provider_env:
            # Emit exactly the 8 keys NBI looks for (STRING_OVERRIDE_SPEC).
            # Fallback defaults mirror prod AI Factory values so a blank
            # Helm config still produces a "just works" UX.
            provider = _auth_env.get("NBI_CHAT_MODEL_PROVIDER", "openai_compatible")
            chat = _auth_env.get("NBI_CHAT_MODEL_ID", "databricks/gdp-gpt4o")
            inline_provider = _auth_env.get("NBI_INLINE_COMPLETION_MODEL_PROVIDER", provider)
            inline = _auth_env.get("NBI_INLINE_COMPLETION_MODEL_ID", chat)
            claude_chat = _auth_env.get("NBI_CLAUDE_CHAT_MODEL", "")
            claude_inline = _auth_env.get("NBI_CLAUDE_INLINE_COMPLETION_MODEL", "")
            base_url = _auth_env.get(
                "ANTHROPIC_BASE_URL",
                "https://gateway.scaifactory.dev.azure.scbdev.net/v1",
            )
            provider_env = {
                "NBI_CHAT_MODEL_PROVIDER": provider,
                "NBI_CHAT_MODEL_ID": chat,
                "NBI_INLINE_COMPLETION_MODEL_PROVIDER": inline_provider,
                "NBI_INLINE_COMPLETION_MODEL_ID": inline,
                "NBI_CLAUDE_CHAT_MODEL": claude_chat,
                "NBI_CLAUDE_INLINE_COMPLETION_MODEL": claude_inline,
                "ANTHROPIC_BASE_URL": base_url,
                # ANTHROPIC_API_KEY is intentionally OMITTED here — the minted
                # token is written to /tmp/nbi-runtime-env.json AND
                # config.json by the sidecar.  Hard-coding a stale key here
                # would leak secrets into the pod manifest.
            }
            for k, v in provider_env.items():
                # Don't overwrite explicit values already present in
                # spawner.environment from other sources (e.g. per-profile
                # kubeSpawner.profile_list patch).
                # NOTE: Empty-string values (e.g. NBI_CLAUDE_CHAT_MODEL when
                # Claude is unused) are STILL written because the sidecar's
                # runtime_env ferry JSON (and the STRING_OVERRIDE re-apply on
                # every @property access) must see a deterministic key set.
                if k not in spawner.environment:
                    spawner.environment[k] = v

        # ---- E. Build + append sidecar container dict -------------------
        # Container env: explicit values + secret envFrom (envFrom on container
        # dict form: kubernetes "envFrom:" list inside the container spec).
        sidecar_env_list: list = []
        for k, v in _extra_sidecar_env.items():
            sidecar_env_list.append({"name": k, "value": str(v)})

        sidecar_container: dict[str, Any] = {
            "name": "nbi-auth-sidecar",
            "image": _sidecar_image,
            "imagePullPolicy": _sidecar_image_pull_policy,
            "env": sidecar_env_list,
            "envFrom": [{"secretRef": {"name": _secret_name, "optional": False}}],
            "volumeMounts": [
                # Shared /tmp with notebook container so S2 poller in notebook
                # process reads the runtime env ferry + state file.
                {"name": _shared_tmp_vol, "mountPath": "/tmp"},
                # JKS secret mount (read-only) for Java subprocess.
                {
                    "name": _auth_secret_vol,
                    "mountPath": "/var/run/nbi-llm-auth",
                    "readOnly": True,
                },
            ],
            "resources": _resources,
            "securityContext": _security_context,
            # tini as PID 1 + python main loop handle SIGTERM within 2s.
            # K8s default gracePeriodSeconds=30 is more than enough.
            "terminationMessagePolicy": "FallbackToLogsOnError",
            "terminationMessagePath": "/dev/termination-log",
        }

        if _probes_enabled:
            # Explicit `host: 127.0.0.1` — without this the kubelet tries the
            # pod IP, but our server binds loopback-only and would otherwise
            # always fail probes (AC-8 requirement).
            sidecar_container["livenessProbe"] = {
                "httpGet": {
                    "host": "127.0.0.1",
                    "path": "/healthz",
                    "port": _sidecar_http_port,
                    "scheme": "HTTP",
                },
                "initialDelaySeconds": 10,
                "periodSeconds": 20,
                "timeoutSeconds": 3,
                "failureThreshold": 4,
                "successThreshold": 1,
            }
            sidecar_container["readinessProbe"] = {
                "httpGet": {
                    "host": "127.0.0.1",
                    "path": "/ready",
                    "port": _sidecar_http_port,
                    "scheme": "HTTP",
                },
                "initialDelaySeconds": 15,
                "periodSeconds": 10,
                "timeoutSeconds": 3,
                "failureThreshold": 3,
                "successThreshold": 1,
            }

        # Idempotent append: if a container with the same name is already in
        # extra_containers (hook called twice, helm re-render, etc), REMOVE
        # the old entry first and append THIS fresh definition.  Otherwise
        # user pods end up with two sidecar containers and duplicate env
        # mounts that PVC refuses.
        old_list = list(getattr(spawner, "extra_containers", None) or [])
        deduped = [c for c in old_list if not (isinstance(c, dict) and c.get("name") == "nbi-auth-sidecar")]
        deduped.append(sidecar_container)
        spawner.extra_containers = deduped

        log.info(
            "nbi-auth-sidecar INJECTED: image=%s http=%d probes=%s secret=%s user=%s",
            _sidecar_image.rsplit(":", 1)[-1],
            _sidecar_http_port,
            _probes_enabled,
            _secret_name,
            canonicalize_user(getattr(spawner.user, "name", "")),
        )

    async def pre_spawn_hook(spawner: Any) -> None:
        # --- 1. Collect identity ----------------------------------------
        # auth_state may contain OIDC id_token / access_token or LDAP attrs.
        # We never log auth_state and only pass its KEY NAMES downstream.
        auth_state = {}
        try:
            auth_state = (await spawner.user.get_auth_state()) or {}
        except Exception:  # noqa: BLE001 — must never break spawn
            auth_state = {}

        # Hub group list — sorted for determinism in logs + Quota Service.
        groups: list[str] = []
        try:
            groups = sorted(g.name for g in spawner.user.groups)
        except Exception:  # noqa: BLE001
            groups = []

        username = canonicalize_user(spawner.user.name)

        # --- 2. Resolve plan (HTTP first, local fallback on any error) --
        plan: dict[str, Any]
        if quota_service_url:
            try:
                plan = resolve_plan_via_http(quota_service_url, username, groups, auth_state)
            except Exception as exc:  # noqa: BLE001
                log.warning("quota resolve failed (%s); using local fallback", exc)
                plan = resolve_plan_local(username, groups)
        else:
            plan = resolve_plan_local(username, groups)

        # --- 3. Inject env into singleuser pod --------------------------
        env = {
            # Identity — mirror naming used by the sidecar.  These values
            # are "informational inputs" to quota_store.resolve_plan(); the
            # service itself is the authoritative enforcement point.
            "NBI_LLM_USER": username,
            "NBI_LLM_GROUPS": ",".join(groups),
            "NBI_LLM_PLAN": plan.get("id", "standard"),
            # Daily limit hints — the quota badge reads its live values from
            # sidecar /quota, so these are only used for initial UI placeholder
            # text and local-test UX.
            "NBI_LLM_QUOTA_TOKENS_DAY": str(plan.get("tokens_per_day", "")),
            "NBI_LLM_QUOTA_REQ_DAY": str(plan.get("requests_per_day", "")),
            # Default provider (production overrides this to "jar" via the
            # singleuser image ENV; the default of "mock" lets plain
            # local-dev containers start without the JAR Secret).
            "TOKEN_PROVIDER": "mock",
            # Quota backend — http when a service URL is provided, local
            # file store otherwise (per-pod counters).
            "QUOTA_BACKEND": "http" if quota_service_url else "local",
        }
        # Only inject QUOTA_SERVICE_URL when we actually have one — keeps
        # the offline-dev scenario free from surprise "connection refused".
        if quota_service_url:
            env["QUOTA_SERVICE_URL"] = quota_service_url
        spawner.environment.update(env)
        log.info(
            "injected NBI_LLM_* for user=%s plan=%s groups=%s",
            username,
            plan.get("id"),
            groups,
        )

        # --- 4. NBI Auth Sidecar injection (Task 9, gated by feature flag) -
        if sidecar_enabled:
            try:
                await _inject_nbi_auth_sidecar(spawner)
            except Exception as exc:  # noqa: BLE001
                # NEVER break spawn on sidecar injection failure — degraded
                # UX (user has to manually configure provider + rotate in
                # Settings) is strictly preferred vs a failed launch that
                # pages the on-call.  Log loudly so the incident is visible
                # in Hub logs.
                log.error(
                    "NBI auth sidecar injection FAILED for user=%s (%s).  "
                    "Notebook will launch WITHOUT auto-provisioning; user must configure NBI manually.",
                    username,
                    exc,
                    exc_info=True,
                )

    return pre_spawn_hook

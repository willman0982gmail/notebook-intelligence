"""
config_writer.py — Atomic NBI config + runtime-env writers.

Responsibilities
----------------
1. Mirror NBI's native ``_atomic_write_json`` helper *exactly* so every
   file we produce is byte-for-byte equivalent to what
   ``NBIConfig.save()`` would have written.  This matters because the
   FS-poller fallback (S2) uses a 4-tuple ``(dev, ino, size, mtime_ns)``
   signature; writing the same bytes preserves user expectations.
2. Produce ``~/.jupyter/nbi/config.json`` with a pre-filled AI Factory
   provider stanza plus the 8 ``STRING_OVERRIDE_SPEC`` keys so both the
   UI and the property-override system see the same values.
3. Produce ``/tmp/nbi-runtime-env.json`` (mode ``0600``) with the *exact*
   8 STRING_OVERRIDE environment variables that ``apply_string_overrides``
   looks for.  The ``jupyter_server_config_nbi_reload.py`` monkey-patch
   merges this file into ``os.environ`` before every reload, which means
   Provider-property stale reads (the L3 cache in the 5-layer chain) are
   always served the latest values even when the mint cycle happened in
   a different process.
4. Validate the produced documents against a loose structural schema so
   typos in env-configuration surface as a clear bootstrap failure
   instead of a silent "chat returns 401" three hours later.

Hard rules
----------
* No third-party imports.  Python 3.11 stdlib only.
* All comments in English (AC-10).
* Secrets-bearing files (``/tmp/nbi-runtime-env.json``) are *always*
  written with explicit mode ``0o600`` — never rely on umask.
* ``config.json`` preserves pre-existing keys we do not understand.  We
  deep-merge into the on-disk value so a user that stashed MCP servers
  or rule-configuration there does not lose it on a refresh cycle.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import tempfile
from typing import Any, Mapping, MutableMapping, Optional

log = logging.getLogger("nbi_auth_sidecar.config_writer")

# ---------------------------------------------------------------------------
# 1. Atomic write — 1:1 copy of NBI's notebook_intelligence.config._atomic_write_json
#    Kept intentionally identical so behaviour, durability, and edge cases
#    (symlink resolution, mode preservation, POSIX dir fsync) match NBI.
# ---------------------------------------------------------------------------


def atomic_write_json(target: str, payload: dict, *, mode: Optional[int] = None) -> None:
    """Crash-safe replacement for ``open(target, 'w') + json.dump``.

    Plain truncating writes leave the destination empty (or partial) if the
    process is killed mid-write — the next launch then chokes on a corrupt
    config.  Write to a sibling tempfile, ``fsync``, and ``os.replace`` so
    the swap is atomic on POSIX and atomic-ish on Windows.

    Symlink-preserving: a user who symlinks ``~/.jupyter/nbi/config.json``
    to a shared config file expects ``save()`` to update the link's target,
    not replace the link itself.  Resolve via ``realpath`` first.

    Mode handling
    -------------
    * ``mode`` argument set: the file is written with exactly that mode,
      regardless of any existing mode.  Callers that handle secrets pass
      ``0o600`` so the file is never world-readable, even if a prior
      umask-default write or a manual chmod widened the perms.
    * ``mode=None`` (default): ``mkstemp`` returns a 0o600 file; if the
      existing target has different permissions (e.g. 0o644 for a shared
      install), re-apply them after the swap so the user's chmod isn't
      silently undone.

    Durability: ``fsync`` the tempfile, then ``fsync`` the target's parent
    directory on POSIX so the rename itself survives a crash.
    """
    real_target = os.path.realpath(target)
    target_dir = os.path.dirname(real_target) or "."
    existing_mode: Optional[int] = None
    try:
        existing_mode = stat.S_IMODE(os.stat(real_target).st_mode)
    except FileNotFoundError:
        pass
    fd, tmp_path = tempfile.mkstemp(
        prefix="." + os.path.basename(real_target) + ".",
        suffix=".tmp",
        dir=target_dir,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        # Decide what mode the post-rename file should carry:
        #   - explicit mode overrides everything (secrets case)
        #   - else preserve existing target mode (shared-install case)
        #   - else leave mkstemp's 0o600
        effective_mode = mode if mode is not None else existing_mode
        if effective_mode is not None:
            try:
                os.chmod(tmp_path, effective_mode)
            except OSError:
                # Some filesystems (FAT, certain network mounts) reject chmod;
                # the swap itself is still safe, just lose the mode bits.
                pass
        os.replace(tmp_path, real_target)
    except Exception:
        # Best-effort cleanup — if replace failed the tempfile is dangling.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    # ``os.replace`` makes the new inode visible, but until the directory
    # entry is fsynced a power-loss can still leave the rename unrecorded
    # on POSIX.  Windows has no ``O_RDONLY`` directory descriptor, so skip
    # there and rely on its rename semantics.
    if hasattr(os, "O_DIRECTORY"):
        try:
            dir_fd = os.open(target_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 2. NBI config layout constants — matched to the STRING_OVERRIDE_SPEC
#    entries in notebook_intelligence/extension.py so spelling is exact.
# ---------------------------------------------------------------------------

#: Mirror of notebook_intelligence.extension.STRING_OVERRIDE_SPEC — these
#: 8 names are the *only* keys the property-override system consults.
STRING_OVERRIDE_SPEC: tuple[tuple[str, str], ...] = (
    ("chat_model_provider", "NBI_CHAT_MODEL_PROVIDER"),
    ("chat_model_id", "NBI_CHAT_MODEL_ID"),
    ("inline_completion_model_provider", "NBI_INLINE_COMPLETION_MODEL_PROVIDER"),
    ("inline_completion_model_id", "NBI_INLINE_COMPLETION_MODEL_ID"),
    ("claude_chat_model", "NBI_CLAUDE_CHAT_MODEL"),
    ("claude_inline_completion_model", "NBI_CLAUDE_INLINE_COMPLETION_MODEL"),
    ("claude_api_key", "ANTHROPIC_API_KEY"),
    ("claude_base_url", "ANTHROPIC_BASE_URL"),
)

#: Provider monikers that the NBI frontend knows how to render.  The
#: AI Factory endpoint is OpenAI-compatible, so we use ``openai_compatible``
#: which is the native litellm-backed provider shipped with NBI.
KNOWN_PROVIDERS = frozenset(
    {
        "openai_compatible",
        "anthropic",
        "ollama",
        "github_copilot",
        "litellm_compatible",
    }
)


def _expand_home(path_template: str) -> str:
    """Resolve ``~`` and shell-style env vars in a path template.

    The sidecar runs as the same UID as the notebook container (1000) and
    shares ``/home/jovyan`` via PVC, so ``~`` expansion is deterministic.
    ``$HOME`` is honoured explicitly for admins that relocate the home
    directory to an unusual mount.
    """
    return os.path.expandvars(os.path.expanduser(path_template))


def default_user_config_file() -> str:
    """Return the canonical user-level NBI config path.

    Matches ``NBIConfig.user_config_file`` in the main package.
    """
    nbi_user_dir = os.path.join(os.path.expanduser("~"), ".jupyter", "nbi")
    return os.path.join(nbi_user_dir, "config.json")


def default_runtime_env_file() -> str:
    """Return the path used to ferry the 8 override env vars across processes.

    Lives on the shared ``/tmp`` empty-dir volume so both the sidecar and
    the notebook server can read it without crossing PVC boundaries.
    """
    return os.environ.get("NBI_RUNTIME_ENV_FILE", "/tmp/nbi-runtime-env.json")


# ---------------------------------------------------------------------------
# 3. Deep merge — preserve user data we don't own.
# ---------------------------------------------------------------------------


def _deep_merge(base: MutableMapping[str, Any], override: Mapping[str, Any]) -> MutableMapping[str, Any]:
    """Recursively merge ``override`` into ``base``, mutating and returning base.

    Rules:
      * dict-typed keys are merged recursively.
      * list-typed keys from ``override`` *replace* the base list — the
        NBI provider list is order-sensitive and we want the admin-supplied
        AI Factory entry to be in a known position.
      * all scalar values win.
    """
    for key, value in override.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(value, Mapping)
        ):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _load_json_if_exists(path: str) -> dict:
    """Return the parsed JSON at ``path`` or an empty dict if missing/invalid."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not parse existing %s — starting fresh: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# 4. NBI config.json writer
# ---------------------------------------------------------------------------


def build_nbi_user_config(
    *,
    provider: str,
    chat_model_id: str,
    inline_model_id: str,
    api_key: str,
    base_url: str,
    claude_chat_model: Optional[str] = None,
    claude_inline_model: Optional[str] = None,
    extra_config: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Return the NBI ``config.json`` dict for an AI Factory provision.

    The shape matches exactly what the NBI "Settings → Save" UI would write
    so users can later tweak values and we correctly diff the result.
    """
    # Providers stanza — openai_compatible supports arbitrary base_url +
    # bearer-token auth which is exactly what AI Factory exposes.
    provider_entry: dict[str, Any] = {
        "name": provider,
        "api_key": api_key,
        "base_url": base_url,
    }

    config: dict[str, Any] = {
        "chat_model_provider": provider,
        "chat_model_id": chat_model_id,
        "inline_completion_model_provider": provider,
        "inline_completion_model_id": inline_model_id,
        "llm_providers": {provider: provider_entry},
    }

    # Claude settings are a parallel namespace used by the "Claude mode"
    # participant.  We mirror the same token/base_url values so both
    # interaction modes pick up the rotated token simultaneously.
    claude_settings: dict[str, Any] = {
        "api_key": api_key,
        "base_url": base_url,
    }
    if claude_chat_model:
        claude_settings["chat_model"] = claude_chat_model
        config["claude_chat_model"] = claude_chat_model
    if claude_inline_model:
        claude_settings["inline_completion_model"] = claude_inline_model
        config["claude_inline_completion_model"] = claude_inline_model
    # Also set the 8th STRING_OVERRIDE key even though it's redundant with
    # the provider entry — property access re-reads it from environ via
    # apply_string_overrides and having it in the JSON keeps /capabilities
    # responses consistent.
    config["claude_api_key"] = api_key
    config["claude_base_url"] = base_url
    config["claude_settings"] = claude_settings

    if extra_config:
        _deep_merge(config, dict(extra_config))

    return config


def validate_nbi_config(payload: Mapping[str, Any]) -> list[str]:
    """Return a list of human-readable structural problems (empty = valid).

    Intentionally *loose*: we only validate fields we depend on for the
    "just-works" experience.  Unknown keys are explicitly allowed so
    future NBI versions can introduce new fields without us having to
    cut a sidecar release.
    """
    problems: list[str] = []

    # Top-level scalar keys the settings panel uses.
    for required_key in (
        "chat_model_provider",
        "chat_model_id",
        "inline_completion_model_provider",
        "inline_completion_model_id",
        "claude_api_key",
        "claude_base_url",
    ):
        value = payload.get(required_key)
        if not isinstance(value, str) or not value:
            problems.append(f"missing/empty scalar key: {required_key!r}")

    # Provider consistency — the scalar provider name must be registered.
    provider = payload.get("chat_model_provider")
    if isinstance(provider, str):
        if provider not in KNOWN_PROVIDERS:
            problems.append(
                f"chat_model_provider={provider!r} not in known set {sorted(KNOWN_PROVIDERS)}"
            )
        providers_block = payload.get("llm_providers") or {}
        if provider not in providers_block:
            problems.append(f"llm_providers missing entry for provider {provider!r}")
        else:
            entry = providers_block[provider]
            if not isinstance(entry.get("api_key"), str) or not entry["api_key"]:
                problems.append(f"llm_providers[{provider!r}].api_key missing")
            if not isinstance(entry.get("base_url"), str) or not entry["base_url"]:
                problems.append(f"llm_providers[{provider!r}].base_url missing")

    return problems


def write_nbi_user_config(
    payload: Mapping[str, Any],
    *,
    path: Optional[str] = None,
    merge_existing: bool = True,
) -> str:
    """Write an NBI user config dict to disk, optionally deep-merging existing data.

    Returns the absolute path that was written (useful for log lines).
    """
    target = _expand_home(path) if path else default_user_config_file()
    os.makedirs(os.path.dirname(target), exist_ok=True)

    if merge_existing:
        existing = _load_json_if_exists(target)
        merged: MutableMapping[str, Any] = _deep_merge(
            existing if isinstance(existing, dict) else {}, dict(payload)
        )
    else:
        merged = dict(payload)

    problems = validate_nbi_config(merged)
    if problems:
        # Surface immediately — a config that fails validation now is not
        # going to produce a functional chat experience, so we abort the
        # write and let the caller decide how to degrade.
        raise ValueError(
            "Refusing to write invalid NBI config: " + "; ".join(problems)
        )

    atomic_write_json(target, dict(merged))
    log.info("Wrote NBI user config: %s (provider=%s)", target, merged.get("chat_model_provider"))
    return target


# ---------------------------------------------------------------------------
# 5. Runtime env JSON writer — 8 keys, mode 0600, for cross-process ferry.
# ---------------------------------------------------------------------------


def build_runtime_env_map(
    *,
    provider: str,
    chat_model_id: str,
    inline_model_id: str,
    api_key: str,
    base_url: str,
    claude_chat_model: Optional[str] = None,
    claude_inline_model: Optional[str] = None,
) -> dict[str, str]:
    """Return a dict of EXACTLY the 8 STRING_OVERRIDE env vars.

    Ordering in the on-disk JSON follows STRING_OVERRIDE_SPEC so humans
    reading ``/tmp/nbi-runtime-env.json`` can cross-reference the spec
    table directly.  Values that are ``None`` are *still* written as the
    empty string — having all 8 keys present every time simplifies the
    consumer-side merge logic (no "key existed before, what now?" cases).
    """
    raw_values: dict[str, str] = {
        "NBI_CHAT_MODEL_PROVIDER": provider,
        "NBI_CHAT_MODEL_ID": chat_model_id,
        "NBI_INLINE_COMPLETION_MODEL_PROVIDER": provider,
        "NBI_INLINE_COMPLETION_MODEL_ID": inline_model_id,
        "NBI_CLAUDE_CHAT_MODEL": claude_chat_model or "",
        "NBI_CLAUDE_INLINE_COMPLETION_MODEL": claude_inline_model or "",
        "ANTHROPIC_API_KEY": api_key,
        "ANTHROPIC_BASE_URL": base_url,
    }
    # Emit in STRING_OVERRIDE_SPEC order for readability.
    ordered: dict[str, str] = {}
    for _setting_name, env_name in STRING_OVERRIDE_SPEC:
        ordered[env_name] = raw_values[env_name]
    return ordered


def validate_runtime_env_map(payload: Mapping[str, str]) -> list[str]:
    """Return problems list for runtime env dict — 8 keys + non-empty criticals."""
    problems: list[str] = []
    expected_env_names = {env_name for _, env_name in STRING_OVERRIDE_SPEC}
    actual_env_names = set(payload.keys())

    missing = expected_env_names - actual_env_names
    if missing:
        problems.append(f"missing env keys: {sorted(missing)}")
    extra = actual_env_names - expected_env_names
    if extra:
        # Not strictly a failure, but surprising — flag it loudly.
        problems.append(f"unexpected env keys (will be dropped on merge): {sorted(extra)}")

    for critical in {
        "NBI_CHAT_MODEL_PROVIDER",
        "NBI_CHAT_MODEL_ID",
        "NBI_INLINE_COMPLETION_MODEL_ID",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
    }:
        value = payload.get(critical)
        if not isinstance(value, str) or not value:
            problems.append(f"critical env key empty: {critical}")

    return problems


def write_runtime_env_json(
    payload: Mapping[str, str],
    *,
    path: Optional[str] = None,
) -> str:
    """Write the 8-key runtime env dict to disk with strict 0600 permissions.

    This file contains a bearer token (``ANTHROPIC_API_KEY``) so mode 0600
    is NON-NEGOTIABLE.  We do NOT preserve existing mode because a prior
    admin may have accidentally widened it.
    """
    target = _expand_home(path) if path else default_runtime_env_file()
    os.makedirs(os.path.dirname(target), exist_ok=True)

    problems = validate_runtime_env_map(payload)
    if problems:
        raise ValueError(
            "Refusing to write invalid runtime env JSON: " + "; ".join(problems)
        )

    ordered = {env_name: str(payload.get(env_name, "")) for _, env_name in STRING_OVERRIDE_SPEC}
    atomic_write_json(target, ordered, mode=0o600)
    # Defensive post-check — chmod can silently fail on weird filesystems.
    try:
        actual_mode = stat.S_IMODE(os.stat(target).st_mode)
        if actual_mode != 0o600:
            log.warning(
                "Runtime env file %s has mode %04o (expected 0600) — fs may ignore chmod",
                target,
                actual_mode,
            )
    except FileNotFoundError:
        pass
    log.info(
        "Wrote runtime env JSON: %s (keys=%s)",
        target,
        ", ".join(k for k in ordered.keys() if not k.endswith("API_KEY")),
    )
    return target


# ---------------------------------------------------------------------------
# 6. Convenience facade — single call produces both files from one token.
# ---------------------------------------------------------------------------


def write_all_from_token(
    *,
    access_token: str,
    provider: str = "openai_compatible",
    chat_model_id: str = "",
    inline_model_id: str = "",
    base_url: str = "",
    claude_chat_model: Optional[str] = None,
    claude_inline_model: Optional[str] = None,
    config_path: Optional[str] = None,
    runtime_env_path: Optional[str] = None,
    merge_existing_config: bool = True,
) -> tuple[str, str]:
    """Write both config.json and runtime env JSON in one step.

    Returns ``(written_config_path, written_runtime_env_path)``.
    """
    # Build NBI user config — contains the provider block plus the 8
    # STRING_OVERRIDE scalar keys.
    nbi_config = build_nbi_user_config(
        provider=provider,
        chat_model_id=chat_model_id,
        inline_model_id=inline_model_id,
        api_key=access_token,
        base_url=base_url,
        claude_chat_model=claude_chat_model,
        claude_inline_model=claude_inline_model,
    )
    written_cfg = write_nbi_user_config(
        nbi_config,
        path=config_path,
        merge_existing=merge_existing_config,
    )

    # Build runtime env ferry — *exactly* 8 keys.
    env_map = build_runtime_env_map(
        provider=provider,
        chat_model_id=chat_model_id,
        inline_model_id=inline_model_id,
        api_key=access_token,
        base_url=base_url,
        claude_chat_model=claude_chat_model,
        claude_inline_model=claude_inline_model,
    )
    written_env = write_runtime_env_json(env_map, path=runtime_env_path)
    return written_cfg, written_env


# ---------------------------------------------------------------------------
# 7. CLI entry — minimal smoke, useful for interactive debugging.
# ---------------------------------------------------------------------------


if __name__ == "__main__":  # pragma: no cover - manual smoke only
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if len(sys.argv) < 6:
        print(
            "usage: python config_writer.py <provider> <chat_model> <inline_model> "
            "<base_url> <token> [claude_chat] [claude_inline] [--merge|--no-merge]"
        )
        sys.exit(2)
    args_iter = iter(sys.argv[1:])
    _provider = next(args_iter)
    _chat_model = next(args_iter)
    _inline_model = next(args_iter)
    _base_url = next(args_iter)
    _token = next(args_iter)
    _claude_chat: Optional[str] = None
    _claude_inline: Optional[str] = None
    _merge = True
    for extra in args_iter:
        if extra == "--merge":
            _merge = True
        elif extra == "--no-merge":
            _merge = False
        elif _claude_chat is None:
            _claude_chat = extra
        else:
            _claude_inline = extra

    cfg_path, env_path = write_all_from_token(
        access_token=_token,
        provider=_provider,
        chat_model_id=_chat_model,
        inline_model_id=_inline_model,
        base_url=_base_url,
        claude_chat_model=_claude_chat,
        claude_inline_model=_claude_inline,
        merge_existing_config=_merge,
    )
    print(f"WROTE config={cfg_path} runtime_env={env_path}")

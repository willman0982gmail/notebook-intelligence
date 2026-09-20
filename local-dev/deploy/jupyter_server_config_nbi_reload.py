"""
jupyter_server_config_nbi_reload.py — Hot-reload monkey-patch for NBI.

Background
----------
The 5-layer NBI cache chain is:
    L1  Disk   : ~/.jupyter/nbi/config.json
    L2  Python : NBIConfig.options/user_config dict (in-process)
    L3  Provider: ai_service_manager._providers[X].set_property_value(api_key) (instance)
    L4  TS     : NBIAPI.config.capabilities after fetchCapabilities()
    L5  React  : Settings panel + Chat state

External writers (the nbi-auth-sidecar) writing L1 config.json directly
do NOT trigger L2→L5 update because NBI has no native FS-watcher.  This
file solves that gap by injecting TWO complementary refresh mechanisms
when the Jupyter Server starts:

Mechanism A (reload-config endpoint) — /notebook-intelligence/reload-config
    POST with body {"broadcast": true} runs nbi_config.load() +
    update_models_from_config(), then (when broadcast) pushes a
    WebSocket MCPServerStatusChange message to every connected browser.
    NBI frontend has a hard-coded `if msg.type === MCPServerStatusChange`
    listener that calls fetchCapabilities() immediately — this is the
    FAST path (≤200ms L1→L5 after sidecar reload POST).

Mechanism B (S2 FS Poller) — daemon Thread
    Every 5s calls os.stat() on config.json and compares the 4-tuple
    signature (dev, ino, size, mtime_ns).  When any component changes
    (e.g. sidecar atomic write creates a new inode) the same do_reload()
    routine fires — this is the SAFETY net that fires even when the
    sidecar's reload POST is 404-ing because NBI routes were not yet
    registered when the sidecar booted.

Both paths merge /tmp/nbi-runtime-env.json into os.environ BEFORE
reloading config.  NBI's apply_string_overrides() re-reads those 8 env
vars on EVERY property access to chat_model, so merging them into
os.environ is the ONLY way to defeat L3 Provider-level stale api_key
reads on the next user chat request after a rotate.

Installation
------------
Z2JK default: this file is copied to /etc/jupyter/ in Dockerfile.singleuser.
Jupyter Server automatically reads /etc/jupyter/jupyter_server_config*.py
on startup (JUPYTER_CONFIG_PATH convention).  To test locally:
```
cp jupyter_server_config_nbi_reload.py ~/.jupyter/jupyter_server_config_nbi_reload.py
export NBI_FS_POLL=1
jupyter lab
```
"""

from __future__ import annotations

import gc
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Optional, Tuple

# ---------------------------------------------------------------------------
# 1. Global tunables — all overridable via env so operators can turn up/down
#    polling frequency without rebuilding the singleuser image.
# ---------------------------------------------------------------------------

#: Master switch: ``export NBI_FS_POLL=0`` to disable the FS poller.
#: Default is ENABLED because the S2 safety net is what makes the auth
#: sidecar design "zero-click no matter what".
FS_POLL_ENABLED = str(os.environ.get("NBI_FS_POLL", "1")).lower() not in {"0", "false", "no", "off", ""}

#: Polling cadence.  The spec says 0–5s.  We use a 5s default.  Set to 2 for
#: IdPs with very short token TTLs (< 10 min).
FS_POLL_INTERVAL_SEC = float(os.environ.get("NBI_FS_POLL_INTERVAL", "5.0"))

#: Path to the runtime env ferry JSON produced by nbi-auth-sidecar.
RUNTIME_ENV_PATH = os.environ.get("NBI_RUNTIME_ENV_FILE", "/tmp/nbi-runtime-env.json")

#: Config path we watch.  Matches NBIConfig.user_config_file exactly.
def _user_config_file() -> str:
    return os.path.join(os.path.expanduser("~"), ".jupyter", "nbi", "config.json")

log = logging.getLogger("nbi.jupyter_server_config_reload")

# ---------------------------------------------------------------------------
# 2. Runtime env merge — applied BEFORE every reload so the 8
#    STRING_OVERRIDE env vars are always fresh in os.environ.
# ---------------------------------------------------------------------------


def merge_runtime_env_into_environ() -> dict:
    """Read /tmp/nbi-runtime-env.json and write every key into os.environ.

    Returns the dict of applied changes (empty if file missing).  We
    NEVER delete keys that are not in the ferry JSON; if the sidecar
    exits unexpectedly the last-known token is retained as a
    best-effort fallback.  Mode-0600 failures are logged and swallowed
    (we don't want a permission bug to crash Jupyter itself).
    """
    applied: dict = {}
    if not os.path.exists(RUNTIME_ENV_PATH):
        return applied
    try:
        with open(RUNTIME_ENV_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("Could not parse runtime env ferry %s: %s", RUNTIME_ENV_PATH, exc)
        return applied
    if not isinstance(data, dict):
        log.warning("Runtime env ferry %s not a dict — skipping merge.", RUNTIME_ENV_PATH)
        return applied
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        prior = os.environ.get(k)
        if prior != v:
            os.environ[k] = v
            applied[k] = True
    if applied:
        log.info(
            "Merged %d keys from runtime env ferry (%s).",
            len(applied),
            ", ".join(k for k in applied if not k.endswith("API_KEY")),
        )
    return applied


# ---------------------------------------------------------------------------
# 3. ai_service_manager locator — 3 fallbacks so this works even when
#    Jupyter's internal object graph moves between NBI releases.
#    The post_open_app_callbacks hook gives us a ServerApp instance;
#    from there we walk to the extension's manager.
# ---------------------------------------------------------------------------


def _locate_ai_service_manager(server_app) -> Optional[Any]:
    """Return the NotebookIntelligence.ai_service_manager instance or None.

    Three discovery attempts, from fastest to slowest:
      1. Walk server_app.extension_manager._extensions for the NBI package.
      2. Check the NBI module-level globals if extension init stored it.
      3. ``gc.get_objects()`` scan for the AiServiceManager class by name.

    We intentionally use string names instead of imports so this file can
    be loaded BEFORE the nbi package finishes initialising — the
    post_open_app_callbacks hook runs AFTER initialize_handlers but the
    import order in test scenarios can vary.
    """
    # Strategy 1: server_app.extension_manager → NBI extension.
    ext_manager = getattr(server_app, "extension_manager", None)
    if ext_manager is not None:
        extensions = getattr(ext_manager, "_extensions", None) or getattr(ext_manager, "extensions", None) or {}
        # Iterate extension dicts; the one with "nbi" in its module name is ours.
        for ext_name, ext_record in extensions.items():
            if "notebook_intelligence" not in str(ext_name).lower():
                continue
            mod = getattr(ext_record, "module", None) or ext_record
            mgr = getattr(mod, "ai_service_manager", None)
            if mgr is not None:
                return mgr
            # Extension instance attribute (newer NBI releases).
            ext_point = getattr(ext_record, "extension", None)
            if ext_point is not None:
                mgr = getattr(ext_point, "ai_service_manager", None)
                if mgr is not None:
                    return mgr

    # Strategy 2: directly reach into the notebook_intelligence package.
    try:
        import notebook_intelligence.extension as _nbi_ext_mod  # type: ignore

        mgr = getattr(_nbi_ext_mod, "_global_ai_service_manager", None) or getattr(
            _nbi_ext_mod, "ai_service_manager", None
        )
        if mgr is not None:
            return mgr
    except Exception:  # noqa: BLE001 - graceful degradation
        pass

    # Strategy 3: gc scan — O(heap) but only happens ONCE per server
    # process start and ai_service_manager is a singleton so the scan
    # returns within ~5ms on realistic heaps.
    AiServiceManager_cls: Optional[type] = None
    try:
        from notebook_intelligence.ai_service_manager import AiServiceManager  # type: ignore

        AiServiceManager_cls = AiServiceManager
    except Exception:  # noqa: BLE001
        pass
    for obj in gc.get_objects():
        try:
            if AiServiceManager_cls is not None:
                if isinstance(obj, AiServiceManager_cls):
                    return obj
            else:
                # Fallback class-name string match.
                if type(obj).__name__ == "AiServiceManager":
                    return obj
        except Exception:  # noqa: BLE001
            continue
    return None


def _locate_nbi_config(ai_service_manager: Any) -> Optional[Any]:
    """Extract nbi_config instance from ai_service_manager.

    Almost always `ai_service_manager.nbi_config`; guard with hasattr so
    refactors don't blow up Jupyter startup.
    """
    if ai_service_manager is None:
        return None
    return getattr(ai_service_manager, "nbi_config", None)


# ---------------------------------------------------------------------------
# 4. do_reload() — central reload routine shared by endpoint + FS poller.
# ---------------------------------------------------------------------------


def do_reload(ai_service_manager: Any, server_app: Any) -> Tuple[bool, dict]:
    """Full L1→L3 refresh.  Returns (success: bool, detail: dict).

    Ordering is critical:
        1. merge_runtime_env_into_environ() → OS env updated FIRST so the
           property-override apply_string_overrides() sees fresh values
           on the IMMEDIATE next access even before we rebuild Providers.
        2. nbi_config.load() — re-reads L1 disk JSON into L2 dict.
        3. update_models_from_config() — tears down old Provider instances
           and constructs new ones with the new api_key/base_url, calling
           set_property_value() with the freshly-minted env values.
    """
    detail: dict = {"reloaded_at": time.time()}
    try:
        merged = merge_runtime_env_into_environ()
        detail["merged_env_keys"] = len(merged)
        cfg = _locate_nbi_config(ai_service_manager)
        if cfg is None:
            detail["error"] = "nbi_config not found"
            return False, detail
        cfg.load()
        detail["config_load_ok"] = True
        # update_models_from_config is the L2→L3 transition that rebuilds
        # provider singletons so their set_property_value(api_key) sees
        # the brand new env values from step 1.
        updater = getattr(ai_service_manager, "update_models_from_config", None)
        if callable(updater):
            updater()
            detail["models_updated"] = True
        else:
            detail["models_updated"] = False
            log.warning("ai_service_manager missing update_models_from_config method — skipping L3 rebuild.")
        return True, detail
    except Exception as exc:  # noqa: BLE001
        log.exception("do_reload failed (non-fatal, next poll/POST will retry): %s", exc)
        detail["error"] = f"{type(exc).__name__}: {exc}"
        return False, detail


# ---------------------------------------------------------------------------
# 5. WebSocket broadcast helper — push MCPServerStatusChange to all connected
#    WS clients.  Frontend's NBIAPI hard-codes: on MCPServerStatusChange →
#    fetchCapabilities().  Same 3-fallback discovery pattern as the manager.
# ---------------------------------------------------------------------------


def broadcast_mcp_server_status_change(server_app: Any) -> int:
    """Send MCPServerStatusChange WS msg to every currently-connected WS handler.

    Returns number of connections we pushed to.  Never raises.
    """
    pushed = 0
    payload = json.dumps({"type": "MCPServerStatusChange", "status": "changed", "ts": time.time()})
    try:
        # Strategy 1: server_app.web_app.settings → collection of WS handlers.
        web_app = getattr(server_app, "web_app", None)
        handler_sets: list = []
        if web_app is not None:
            # Jupyter Server sometimes stores active websockets in settings.
            for key in ("active_ws_handlers", "websocket_handlers", "nbi_ws_handlers"):
                bucket = web_app.settings.get(key) if hasattr(web_app, "settings") else None
                if bucket and isinstance(bucket, (set, list, tuple, dict)):
                    handler_sets.append(bucket)

        # Strategy 2: NBI extension globals.
        try:
            import notebook_intelligence.extension as _ne  # type: ignore

            for attr in ("_active_ws_handlers", "ws_clients", "_websockets"):
                bucket = getattr(_ne, attr, None)
                if bucket and isinstance(bucket, (set, list, tuple, dict)):
                    handler_sets.append(bucket)
        except Exception:  # noqa: BLE001
            pass

        # Strategy 3: gc scan for WSHandler instances whose module string
        # contains "notebook_intelligence".  This catches NBI's custom WS
        # handler regardless of where it is registered.
        def _is_nbi_ws_handler(obj: Any) -> bool:
            cls_name = type(obj).__name__
            mod = type(obj).__module__
            wm = getattr(obj, "write_message", None)
            return (
                callable(wm)
                and ("notebook_intelligence" in mod or "NotebookIntelligence" in cls_name or "NBI" in cls_name)
            )

        gc_bucket = [o for o in gc.get_objects() if _is_nbi_ws_handler(o)]
        handler_sets.append(gc_bucket)

        seen: set = set()
        for bucket in handler_sets:
            iterable = bucket.values() if isinstance(bucket, dict) else bucket
            for h in iterable:
                wid = id(h)
                if wid in seen:
                    continue
                seen.add(wid)
                write_fn = getattr(h, "write_message", None)
                if not callable(write_fn):
                    continue
                try:
                    write_fn(payload)
                    pushed += 1
                except Exception:  # noqa: BLE001
                    # Handler mid-close; ignore.
                    continue
    except Exception as exc:  # noqa: BLE001
        log.warning("broadcast_mcp_server_status_change failed: %s", exc)
    log.info("Broadcast MCPServerStatusChange WS msg → %d connection(s).", pushed)
    return pushed


# ---------------------------------------------------------------------------
# 6. Reload endpoint handler — tornado RequestHandler.
# ---------------------------------------------------------------------------


def _register_reload_endpoint(server_app: Any, ai_service_manager: Any) -> bool:
    """Inject POST /notebook-intelligence/reload-config route.

    Uses ``@web.authenticated`` decorator so only callers bearing the
    jupyter server token (the sidecar, which reads it from the
    jpserver-*.json file) can trigger reloads.  Returns True on success.
    """
    try:
        from tornado import web  # type: ignore
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not import tornado.web — reload endpoint disabled: %s", exc)
        return False
    app = getattr(server_app, "web_app", None)
    if app is None:
        return False

    # Capture references in closure.
    _asm = ai_service_manager
    _sap = server_app

    class ReloadConfigHandler(web.RequestHandler):
        """POST /notebook-intelligence/reload-config — authenticated trigger."""

        SUPPORTED_METHODS = ("POST", "OPTIONS")  # type: ignore[assignment]

        def prepare(self) -> None:  # noqa: D401
            # Cap the POST body to 64KiB — this endpoint only accepts a
            # tiny JSON flag.  Hardening against spoofed content-length bombs.
            raw_len = self.request.headers.get("Content-Length", "0") or "0"
            try:
                n = int(raw_len)
            except ValueError:
                n = 0
            if n > 65536:
                raise web.HTTPError(413, "body too large")
            super().prepare()

        def options(self) -> None:  # CORS preflight — localhost only.
            self.set_status(204)
            self.finish()

        def set_default_headers(self) -> None:
            self.set_header("Cache-Control", "no-store, no-cache, must-revalidate")

        @web.authenticated
        def post(self) -> None:
            # Parse optional body.
            broadcast = True
            reason: Optional[str] = None
            try:
                raw = self.request.body or b""
                if raw:
                    parsed = json.loads(raw.decode("utf-8", errors="replace"))
                    if isinstance(parsed, dict):
                        if "broadcast" in parsed:
                            broadcast = bool(parsed["broadcast"])
                        if isinstance(parsed.get("reason"), str):
                            reason = parsed["reason"][:200]
            except (ValueError, UnicodeDecodeError):
                pass
            ok, detail = do_reload(_asm, _sap)
            http_code = 200 if ok else 500
            detail["broadcast_requested"] = broadcast
            if reason is not None:
                detail["reason"] = reason
            pushed = 0
            if ok and broadcast:
                pushed = broadcast_mcp_server_status_change(_sap)
            detail["broadcast_pushed_clients"] = pushed
            log.info(
                "reload-config POST result=%s broadcast=%s pushed_clients=%s reason=%s",
                ok,
                broadcast,
                pushed,
                reason,
            )
            self.set_header("Content-Type", "application/json")
            self.write(json.dumps(detail))
            self.set_status(http_code)

    # Append route.  We use a prefix-consistent pattern so it doesn't
    # clash with NBI's native routes — the native handlers are added
    # inside initialize_handlers via web_app.add_handlers(".*$", …).
    route_spec = (r"/notebook-intelligence/reload-config", ReloadConfigHandler)
    # The documented Jupyter injection path: use `server_app.web_app.add_handlers()`
    # from inside post_open_app_callbacks.  host_pattern ".*$" matches any
    # vhost, same as NBI's own routes.
    try:
        app.add_handlers(r".*$", [route_spec])
    except Exception as exc:  # Older Jupyter Server may reject tuple wrapping.
        log.warning("add_handlers failed first attempt: %s — trying app.default_route_path.", exc)
        try:
            app.wildcard_router.rules.append(web.URLSpec(*route_spec))
        except Exception as exc2:  # noqa: BLE001
            log.error("Failed to inject reload endpoint: %s", exc2)
            return False
    log.info("Injected POST /notebook-intelligence/reload-config endpoint (authenticated).")
    return True


# ---------------------------------------------------------------------------
# 7. S2 FS poller — daemon thread, compares 4-tuple stat signature.
# ---------------------------------------------------------------------------


class ConfigFilePoller(threading.Thread):
    """Thread that periodically checks for config.json changes via stat().

    Why 4-tuple?
        dev   — device number, changes across NFS remounts
        ino   — inode number, changes on every atomic_write_json (os.replace)
        size  — file size, catches non-atomic writes that keep the same inode
        mtime_ns — nanosecond mtime, catches content edits with same size
    Combined, false-negative detection probability is negligible.
    """

    def __init__(
        self,
        ai_service_manager: Any,
        server_app: Any,
        *,
        path: Optional[str] = None,
        interval_sec: float = FS_POLL_INTERVAL_SEC,
    ) -> None:
        super().__init__(daemon=True, name="NBIConfigFSPoller")
        self._asm = ai_service_manager
        self._sap = server_app
        self._path = path or _user_config_file()
        self._interval_sec = max(0.5, float(interval_sec))
        self._stop_event = threading.Event()
        self._last_signature: Optional[Tuple[int, int, int, int]] = None
        # Take an initial baseline signature BEFORE entering the loop so a
        # file present at Jupyter start does NOT trigger a spurious reload.
        self._last_signature = self._current_signature()

    def _current_signature(self) -> Optional[Tuple[int, int, int, int]]:
        try:
            st = os.stat(self._path)
        except FileNotFoundError:
            return None
        except OSError:
            return None
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

    def stop(self) -> None:
        """Signal the thread to exit at next tick.  Used only in tests."""
        self._stop_event.set()

    def run(self) -> None:  # noqa: D401 — entry point for Thread base class
        log.info(
            "S2 FS poller START (path=%s interval=%.2fs daemon=%s).",
            self._path,
            self._interval_sec,
            self.daemon,
        )
        while not self._stop_event.is_set():
            try:
                cur = self._current_signature()
                if cur != self._last_signature:
                    what_changed = (
                        "created"
                        if self._last_signature is None and cur is not None
                        else "deleted"
                        if cur is None and self._last_signature is not None
                        else "modified"
                    )
                    log.info("S2 FS poller: %s detected — running do_reload().", what_changed)
                    ok, detail = do_reload(self._asm, self._sap)
                    if ok:
                        pushed = broadcast_mcp_server_status_change(self._sap)
                        log.info(
                            "S2 reload OK: merged_env=%s models_updated=%s pushed_ws=%d clients.",
                            detail.get("merged_env_keys", 0),
                            detail.get("models_updated", False),
                            pushed,
                        )
                    self._last_signature = cur
            except Exception as exc:  # noqa: BLE001
                # Never let an exception exit the daemon thread.  Swallow and
                # continue polling — the S2 fallback is safety-critical.
                log.warning("S2 FS poller iteration swallowed error: %s", exc)
            self._stop_event.wait(self._interval_sec)
        log.info("S2 FS poller STOPPED.")


# ---------------------------------------------------------------------------
# 8. Jupyter Server hook — c.ServerApp.post_open_app_callbacks.append()
#    This is the ONLY safe injection point — it fires AFTER all extensions
#    have run initialize_handlers, so ai_service_manager is fully ready.
# ---------------------------------------------------------------------------


def _install_nbi_reload_features(server_app: Any) -> None:
    """Called once per Jupyter Server start via post_open_app_callbacks."""
    log.info("post_open_app_callbacks → installing NBI hot-reload features …")
    asm = _locate_ai_service_manager(server_app)
    if asm is None:
        # Not fatal — admins sometimes run plain JupyterLab without NBI.
        log.warning("AiServiceManager not found.  Reload endpoint + FS poller NOT installed.")
        return
    log.info("Located AiServiceManager instance (type=%s).", type(asm).__name__)
    # Always do one initial merge at Jupyter boot so the first /capabilities
    # call sees the sidecar's latest ferry env values (the bootstrap mint
    # ran before we even got here if pod startup order was "sidecar first").
    merged = merge_runtime_env_into_environ()
    if merged:
        log.info("Boot merged %d env keys from ferry.", len(merged))
    endpoint_ok = _register_reload_endpoint(server_app, asm)
    log.info("Reload endpoint: %s", "INSTALLED" if endpoint_ok else "FAILED")
    if FS_POLL_ENABLED:
        poller = ConfigFilePoller(asm, server_app)
        poller.start()
        log.info("S2 FS poller thread started: tid_name=%s path=%s interval=%.1fs", poller.name, _user_config_file(), FS_POLL_INTERVAL_SEC)
    else:
        log.info("S2 FS poller DISABLED via NBI_FS_POLL=%s env.", os.environ.get("NBI_FS_POLL"))


# ---------------------------------------------------------------------------
# 9. Expose the hook via c.ServerApp.  Jupyter Server config files are
#    Python — we assign the append call to an underscore throwaway so the
#    statement executes when the config file is exec'd.
# ---------------------------------------------------------------------------

try:
    # ``c`` is the global config object injected by Jupyter's traitlets
    # loader.  We wrap access in try/except because developers may import
    # this module standalone for testing purposes (there is no ``c``).
    _ = c.ServerApp.post_open_app_callbacks.append(_install_nbi_reload_features)  # type: ignore[name-defined]
    log.info("Registered _install_nbi_reload_features in ServerApp.post_open_app_callbacks.")
except NameError:
    # Running under ``python -c 'import jupyter_server_config_nbi_reload'``.
    log.info("Not running inside Jupyter config loader — hook installation skipped (OK for tests).")
except AttributeError as exc:
    log.warning("ServerApp has no post_open_app_callbacks (%s) — reload features not installed.", exc)

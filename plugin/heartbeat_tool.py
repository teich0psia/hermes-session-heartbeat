"""Current-conversation control via native HeartbeatManager and SessionDB.

The messaging gateway already starts a retry/recovery poller even without watches.
Persisting native state is sufficient there: it discovers the canonical route on its
next scan. Desktop uses its native DB poller; interactive CLI uses its cached
manager and native watchdog. No core replacements or additional scheduler.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from pathlib import Path

SCHEMA = {
    "name": "session_heartbeat",
    "description": (
        "Status/set/pause/resume/clear the existing Hermes Session Heartbeat of THIS "
        "conversation on messaging Gateway, Desktop or interactive CLI. Use only when the user authorized recurring "
        "work; not for invented tasks. Set replaces the prompt and interval and resets "
        "the timer. Minimum 60s. Pause retains the instruction; resume re-anchors; "
        "clear removes it. Requires a proven live native owner. CLI re-open requires explicit "
        "resume/set. API, cron, one-shot and subagents are unsupported. No session/profile ID input. "
        "Successful persistence does not prove a model turn or message delivery. "
        "CLI driver_arm_failed means state was saved but arming failed; same-owner retries remain "
        "fail-closed until CLI reopen, while status/pause/clear remain available."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "set", "pause", "resume", "clear"]},
            "interval": {"type": "string", "description": "Required with set: e.g. 90s, 10m, 2h, 1d (minimum 60s)."},
            "prompt": {"type": "string", "description": "Required with set: recurring instruction. No secrets."},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


class ControlError(Exception):
    def __init__(self, code, message, **details):
        self.code, self.details = code, details
        super().__init__(message)


class HeartbeatTool:
    def __init__(self, home: Path, ctx=None):
        self.home = home
        self.ctx = ctx
        # Serialize our own parallel tool calls; host slash commands retain host semantics.
        self.lock = threading.RLock()

    def __call__(self, args, session_id="", parent_agent=None, **kwargs):
        try:
            with self.lock:
                return json.dumps(self._control(args, session_id, parent_agent), ensure_ascii=False)
        except ControlError as exc:
            return json.dumps({"ok": False, "error": str(exc), "error_code": exc.code, **exc.details}, ensure_ascii=False)
        except (ImportError, AttributeError):
            return json.dumps({"ok": False, "error": "Required Hermes host API is unavailable.",
                               "error_code": "unsupported_host"})
        except Exception:
            # Do not echo arbitrary DB/config exception text (paths or secrets) to the model.
            return json.dumps({"ok": False, "error": "Heartbeat operation failed; inspect host storage/runtime.",
                               "error_code": "storage_error"})

    def _control(self, args, session_id, parent_agent):
        from hermes_cli.heartbeat import HeartbeatManager, HeartbeatState, parse_interval
        from hermes_constants import get_hermes_home
        from hermes_state_registry import acquire, release
        from gateway.session_context import get_session_env, NON_MESSAGING_SESSION_SURFACES
        from gateway.config import Platform
        from agent.delegation_context import is_delegated_child_process_context

        if not isinstance(args, dict) or set(args) - {"action", "interval", "prompt"}:
            raise ControlError("invalid_arguments", "Only action, interval and prompt are accepted.")
        action = args.get("action")
        if not isinstance(action, str) or action not in {"status", "set", "pause", "resume", "clear"}:
            raise ControlError("invalid_arguments", "Choose status/set/pause/resume/clear.")
        seconds = None
        if action == "set":
            interval, prompt = args.get("interval"), args.get("prompt")
            if not isinstance(interval, str) or not isinstance(prompt, str) or not prompt.strip():
                raise ControlError("invalid_arguments", "Set requires a valid interval string and nonempty prompt.")
            try:
                seconds = parse_interval(interval)
            except (ValueError, OverflowError):
                seconds = None
            if seconds is None or seconds < 60:
                raise ControlError("invalid_arguments", "Interval must be a duration such as 10m, at least 60s.")
        elif set(args) != {"action"}:
            raise ControlError("invalid_arguments", "interval and prompt are only accepted with set.")

        if get_hermes_home().resolve() != self.home:
            raise ControlError("profile_mismatch", "Tool belongs to a different profile home.")
        if is_delegated_child_process_context() or getattr(parent_agent, "_delegate_depth", 0):
            raise ControlError("unsupported_surface", "Subagents cannot control their parent's heartbeat.")
        if get_session_env("HERMES_CRON_SESSION", "") or get_session_env("HERMES_SINGLE_QUERY_SESSION", ""):
            raise ControlError("unsupported_surface", "One-shot/cron sessions have no supported idle driver.")
        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        messaging = platform not in NON_MESSAGING_SESSION_SURFACES and platform in {p.value for p in Platform}
        key = get_session_env("HERMES_SESSION_KEY", "")
        local = None
        if not messaging:
            from .host_surfaces import LocalDriver
            source = get_session_env("HERMES_SESSION_SOURCE", "")
            ui_sid = get_session_env("HERMES_UI_SESSION_ID", "")
            if platform not in {"", "cli", "desktop"} or source not in {"", "cli", "desktop"}:
                raise ControlError("unsupported_surface", "No supported native idle driver for this surface.")
            local = LocalDriver(self.home, self.ctx, session_id, key, source, ui_sid)
        context_sid = get_session_env("HERMES_SESSION_ID", "")
        # Gateway binds key/platform but leaves the id blank on cached-agent turns.
        # The executor still injects agent.session_id; a NONEMPTY disagreement is stale.
        if (not isinstance(session_id, str) or not session_id or (messaging and not key)
                or (context_sid and context_sid != session_id)):
            raise ControlError("missing_session_context", "A matching host dispatch/session identity is required.")
        db_path = self.home / "state.db"
        if not db_path.is_file():
            raise ControlError("storage_error", "Current profile has no persisted SessionDB.")
        db = acquire(db_path)
        try:
            owns = (lambda: local.owns(db)) if local else (lambda: self._owns_current_route(db, session_id, key, platform))
            if not owns():
                raise ControlError("unsupported_surface" if local else "not_current_route",
                                   "Current conversation has no proven live native owner in this profile.")
            meta_key = "heartbeat:" + session_id
            raw = db.get_meta(meta_key)  # strict read: native load silently turns failures into None
            previous = self._decode(raw, HeartbeatState)
            mgr = local.manager() if local else HeartbeatManager(session_id)
            if self._snapshot(mgr.state) != self._snapshot(previous):
                raise ControlError("storage_error", "Native heartbeat read disagrees with persisted state; retry.")
            changed = False
            expected = raw
            if action == "set":
                assert seconds is not None
                state = mgr.set(args["prompt"], seconds)
                expected, changed = state.to_json(), True
            elif action in {"pause", "resume"}:
                if previous is None:
                    raise ControlError("no_heartbeat", "No heartbeat is set; use set first.")
                state = getattr(mgr, action)()
                expected, changed = state.to_json(), True
            elif action == "clear" and previous is not None:
                # clear mutates the old state into the native tombstone, then drops manager.state.
                mgr.clear()
                changed = True
                tombstone = asdict(previous)
                tombstone["status"] = "cleared"
                expected = json.dumps(tombstone, ensure_ascii=False)
            actual = db.get_meta(meta_key)
            if actual != expected:
                raise ControlError("persistence_unverified", "State was not read back exactly; it may have failed or changed concurrently. Recheck status.")
            state = self._decode(actual, HeartbeatState)
            if not owns():
                raise ControlError("route_changed", "Conversation changed during the operation; no wakeup is promised.")
            if local:
                from .host_surfaces import WatchdogArmError
                try:
                    local.arm(action)
                except WatchdogArmError:
                    raise ControlError(
                        "driver_arm_failed",
                        "State was saved, but native CLI watchdog arming failed or previously failed on this owner. "
                        "Status/pause/clear remain available; reopen CLI and explicitly resume/set to retry.",
                        action=action, changed=changed, session_id=session_id,
                        heartbeat=self._snapshot(state), persisted=True,
                        driver=local.driver, driver_armed=False,
                    ) from None
                if not owns():
                    raise ControlError("route_changed", "Conversation changed while arming the native driver.")
            snapshot = self._snapshot(state)
            return {
                "ok": True, "action": action, "changed": changed, "session_id": session_id,
                "heartbeat": snapshot, "persisted": True,
                "next_due_in_seconds": (max(0, int((state.last_fired_at or state.created_at)
                                                  + state.interval_seconds - time.time()))
                                        if state and state.status == "active" else None),
                "driver": local.driver if local else "native_gateway_recovery_poller",
                "wakeup": ("native watchdog armed; idle FIFO admission is not a delivery acknowledgement"
                           if local and local.cli is not None and action in {"set", "resume"}
                           else "native driver owns idle admission; status does not arm a CLI watchdog; not a delivery acknowledgement"),
            }
        finally:
            release(db)

    def _owns_current_route(self, db, session_id, key, platform):
        row = db.get_session(session_id)
        if not row or row.get("ended_at") is not None or row.get("session_key") != key:
            return False
        # SessionStore's canonical namespace is its resolved sessions_dir. Do not
        # accept another store's route just because it names the same session id.
        raw = db.load_gateway_routing_entries(scope=str((self.home / "sessions").resolve())).get(key)
        try:
            route = json.loads(raw) if raw else None
        except (ValueError, TypeError):
            return False
        if (not isinstance(route, dict) or route.get("session_key") != key
                or route.get("suspended") or (route.get("origin") or {}).get("platform") != platform):
            return False
        owner = route.get("session_id")
        if owner == session_id:
            return True
        # Native compression publishes/rebinds/migrates inside run_conversation;
        # Gateway repoints only after it returns. Prove continuation from the CURRENT
        # owner to the live tip, never reverse-walk ancestry or write Gateway routing.
        owner_row = db.get_session(owner) if isinstance(owner, str) and owner else None
        return bool(owner_row and owner_row.get("session_key") == key
                    and owner_row.get("ended_at") is not None and owner_row.get("end_reason") == "compression"
                    and db.get_compression_tip(owner) == session_id)

    @staticmethod
    def _decode(raw, cls):
        if raw is None or raw == "":
            return None
        try:
            state = cls.from_json(raw)
            if state.status not in {"active", "paused", "cleared"}:
                raise ValueError("invalid status")
            if state.status != "cleared" and (not state.prompt or state.interval_seconds < 60):
                raise ValueError("invalid schedule")
            return None if state.status == "cleared" else state
        except Exception:
            raise ControlError("invalid_stored_state", "Stored heartbeat is invalid; do not overwrite it automatically.") from None

    @staticmethod
    def _snapshot(state):
        return asdict(state) if state is not None else None

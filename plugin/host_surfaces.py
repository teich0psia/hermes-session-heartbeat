"""Narrow private native-driver lookup; never import a UI server or replace methods.

A live owner is necessary, not merely a session id. Compression is authorized only
from that owner's current durable id to the same DB's native live tip.
"""
import os
from pathlib import Path
import sys


class WatchdogArmError(Exception):
    """Native start was not established; never repair its latch from the plugin."""


def _cli_lease_matches(lease, owner, sid, db, home):
    """Native settle leaves the original lease anchored before compression.

    Prove that exact registry lease and its native live tip, not generic ancestry.
    Read raw entries conservatively (no prune/transfer); a competing owner on the
    continuation invalidates this adapter proof even if its liveness is unknown.
    """
    from hermes_cli.active_sessions import ActiveSessionRegistryError, _FileLock, _read_entries
    anchor = getattr(lease, "session_id", None)
    if (not isinstance(anchor, str) or not anchor
            or not _lease_matches(lease, anchor, "cli")
            or not live_continuation(db, anchor, sid)
            or not live_continuation(db, owner, sid)):
        return False
    state_path, lock_path = home / "runtime/active_sessions.json", home / "runtime/active_sessions.lock"
    if (getattr(lease, "state_path", None) is None or getattr(lease, "lock_path", None) is None
            or Path(lease.state_path).resolve() != state_path
            or Path(lease.lock_path).resolve() != lock_path):
        return False
    try:
        with _FileLock(lock_path):
            entries = _read_entries(state_path, strict=True)
    except (OSError, ActiveSessionRegistryError):
        return False
    own = next((e for e in entries if e.get("lease_id") == lease.lease_id), None)
    return bool(own and not lease.released and own.get("session_id") == anchor
                and own.get("surface") == "cli" and own.get("pid") == os.getpid()
                and (own.get("metadata") or {}).get("live_session_id") == anchor
                and not any(e is not own and live_continuation(db, e["session_id"], sid)
                            for e in entries))


def live_continuation(db, owner, sid):
    row = db.get_session(sid)
    if not row or row.get("ended_at") is not None:
        return False
    if owner == sid:
        return True
    ancestor = db.get_session(owner)
    return bool(ancestor and ancestor.get("ended_at") is not None
                and ancestor.get("end_reason") == "compression"
                and db.get_compression_tip(owner) == sid
                and row.get("session_key") == ancestor.get("session_key"))


def _same_db(handle, home):
    path = getattr(handle, "db_path", None)
    return bool(path and Path(path).resolve() == home / "state.db")


def _lease_matches(lease, owner, surface, *, borrowed=False):
    from hermes_cli.active_sessions import ActiveSessionLease
    return bool(isinstance(lease, ActiveSessionLease) and not lease.released
                and lease.session_id == owner and lease.surface == surface
                and (lease.enabled or (borrowed and lease.lease_id.startswith("borrowed:"))))


class LocalDriver:
    def __init__(self, home, ctx, sid, key, source, ui_sid):
        self.home, self.ctx, self.sid = home, ctx, sid
        self.key, self.source, self.ui_sid = key, source, ui_sid
        self.server = self.record = self.cli = None
        if ui_sid:
            # Loaded only: importing server starts reapers and loads environment files.
            self.server = sys.modules.get("tui_gateway.server")
            sessions = getattr(self.server, "_sessions", None)
            lock = getattr(self.server, "_sessions_lock", None)
            if isinstance(sessions, dict) and lock is not None:
                with lock:
                    self.record = sessions.get(ui_sid)
            self.owner = (self.record or {}).get("session_key")
            self.agent = (self.record or {}).get("agent")
            self.driver = "native_desktop_notification_poller"
        else:
            manager = getattr(ctx, "_manager", None)
            self.cli = getattr(manager, "_cli_ref", None)
            self.owner = getattr(self.cli, "session_id", None)
            self.agent = getattr(self.cli, "agent", None)
            self.driver = "native_cli_heartbeat_watchdog"

    def owns(self, db):
        if not isinstance(self.owner, str) or not self.owner or not live_continuation(db, self.owner, self.sid):
            return False
        if self.ui_sid:
            record = self.record
            if self.server is None or not isinstance(record, dict) or self.source != "desktop":
                return False
            with self.server._sessions_lock:
                if self.server._sessions.get(self.ui_sid) is not record:
                    return False
                stop = record.get("_notif_stop")
                agent = record.get("agent")
                # Native records use None for the frozen launch home, not for
                # whichever profile happens to be active in this tool thread.
                launch_home = getattr(self.server, "_launch_home", None)
                record_home = record.get("profile_home")
                if record_home is None and callable(launch_home):
                    record_home = launch_home()
                return bool(record.get("session_key") == self.owner == self.key
                            and record.get("source") == "desktop"
                            and "profile_home" in record and record_home
                            and Path(record_home).resolve() == self.home
                            and not record.get("_finalized") and not record.get("_closing")
                            and record.get("running") is True
                            and stop is not None and callable(getattr(stop, "is_set", None)) and not stop.is_set()
                            and agent is self.agent and getattr(agent, "session_id", None) == self.sid
                            and not getattr(agent, "_delegate_depth", 0)
                            and _same_db(getattr(agent, "_session_db", None), self.home)
                            and _lease_matches(record.get("active_session_lease"), self.owner, "desktop", borrowed=True))
        cli = self.cli
        manager = getattr(self.ctx, "_manager", None)
        return bool(cli is not None and manager is not None and getattr(manager, "_cli_ref", None) is cli
                    and Path(manager.home_path).resolve() == self.home
                    and self.source in {"", "cli"} and not self.key
                    and getattr(cli, "session_id", None) == self.owner
                    and getattr(cli, "agent", None) is self.agent
                    and getattr(self.agent, "session_id", None) == self.sid
                    and not getattr(getattr(cli, "agent", None), "_delegate_depth", 0)
                    and not getattr(cli, "_single_query_mode", False)
                    and getattr(cli, "_should_exit", True) is False
                    and getattr(cli, "_agent_running", False) is True
                    and getattr(cli, "_interactive_turn", False) is True
                    and _same_db(getattr(cli, "_session_db", None), self.home)
                    and _same_db(getattr(self.agent, "_session_db", None), self.home)
                    and _cli_lease_matches(getattr(cli, "_active_session_lease", None), self.owner, self.sid, db, self.home)
                    and callable(getattr(cli, "_get_heartbeat_manager", None))
                    and callable(getattr(cli, "_start_heartbeat_watchdog", None))
                    and callable(getattr(getattr(cli, "_pending_input", None), "put", None)))

    def manager(self):
        from hermes_cli.heartbeat import HeartbeatManager
        # During compression the host still owns/repoints its cache after the turn.
        # Never prematurely rewrite cli.session_id or its manager cache.
        if self.cli is not None and self.owner == self.sid:
            mgr = self.cli._get_heartbeat_manager()
            if not isinstance(mgr, HeartbeatManager) or mgr.session_id != self.sid:
                raise AttributeError("Unsupported native CLI manager")
            return mgr
        return HeartbeatManager(self.sid)

    def arm(self, action):
        if self.cli is not None and action in {"set", "resume"}:
            # Plugin-owned observation, not a repair of the native start latch.
            # Keep it on the live CLI owner: compression, handler reload and a new
            # LocalDriver must not forget a failure of that owner's watchdog.
            if getattr(self.cli, "_session_heartbeat_arm_failed", False):
                raise WatchdogArmError()
            try:
                self.cli._start_heartbeat_watchdog()
                if not getattr(self.cli, "_heartbeat_watchdog_started", False):
                    raise WatchdogArmError()
            except Exception:
                self.cli._session_heartbeat_arm_failed = True
                raise WatchdogArmError() from None

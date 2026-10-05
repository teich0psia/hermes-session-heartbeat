"""Real host DB/manager/registry/poller; no model call, fake transport admission."""
import asyncio
import contextlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import os

import pytest
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource, SessionStore
from gateway.session_context import set_session_vars, clear_session_vars
from hermes_cli.heartbeat import HeartbeatManager, migrate_heartbeat_to_session
from hermes_cli.plugins import PluginManager
from hermes_state_registry import acquire, release
from tools.registry import registry


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: [session-heartbeat]\n", encoding="utf-8")
    shutil.copytree(os.environ["HERMES_TEST_PLUGIN"], home / "plugins/session-heartbeat")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    manager = PluginManager()
    manager.discover_and_load()
    assert registry.get_entry("session_heartbeat", scope=manager.scope_key)
    handler = registry.get_entry("session_heartbeat", scope=manager.scope_key).handler
    import sys
    module_file = Path(sys.modules[type(handler).__module__].__file__)
    assert module_file.is_relative_to(home / "plugins/session-heartbeat")
    config = GatewayConfig()
    store = SessionStore(home / "sessions", config)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="isolated-chat", thread_id="42", user_id="isolated-user", message_id="stale-anchor")
    entry = store.get_or_create_session(source)
    db = acquire(home / "state.db")
    def call(action, sid=None, **args):
        sid = sid or entry.session_id
        tokens = set_session_vars(platform="telegram", session_key=entry.session_key, session_id=sid, cron_session="")
        try:
            return json.loads(registry.dispatch("session_heartbeat", {"action": action, **args}, scope=manager.scope_key, session_id=sid))
        finally:
            clear_session_vars(tokens)
    yield SimpleNamespace(home=home, manager=manager, handler=handler, store=store, source=source, entry=entry, db=db, call=call, config=config)
    manager.unload()
    store.close_all_db_handles()
    release(db)


def test_controls_real_registration_persistence_and_native_readback(env):
    assert env.call("status")["heartbeat"] is None
    result = env.call("set", interval="1m", prompt="  Check meaningful changes  ")
    assert result["ok"] and result["heartbeat"]["prompt"] == "Check meaningful changes"
    assert HeartbeatManager(env.entry.session_id).state.interval_seconds == 60
    assert env.call("pause")["heartbeat"]["status"] == "paused"
    with patch("hermes_cli.heartbeat.time", SimpleNamespace(time=lambda: 12345678900)):
        result = env.call("resume")
    assert result["ok"] and result["heartbeat"]["last_fired_at"] == 12345678900
    assert not HeartbeatManager(env.entry.session_id).state.is_due(now=12345678900)
    result = env.call("set", interval="2m", prompt="Replacement")
    assert result["ok"] and result["heartbeat"]["fire_count"] == 0
    assert env.call("clear")["heartbeat"] is None
    assert json.loads(env.db.get_meta("heartbeat:" + env.entry.session_id))["status"] == "cleared"
    assert env.call("clear")["changed"] is False
    assert env.call("resume")["error_code"] == "no_heartbeat"
    print("CONTROL: status/set/replace/pause/resume/clear through host discovery + registry + SessionDB PASS")


@pytest.mark.parametrize("args", [
    {"action": "set", "interval": "59s", "prompt": "p"},
    {"action": "set", "interval": "60", "prompt": "p"},
    {"action": "set", "interval": True, "prompt": "p"},
    {"action": "set", "interval": "1m", "prompt": " "},
    {"action": "status", "session_id": "foreign"},
    {"action": "pause", "prompt": "p"},
    {"action": "unknown"},
    {"action": ["set"]},
])
def test_invalid_input_no_state_write(env, args):
    result = json.loads(env.handler(args, session_id=env.entry.session_id))
    assert result["error_code"] == "invalid_arguments"
    assert env.db.get_meta("heartbeat:" + env.entry.session_id) is None


def test_identity_surface_profile_and_archived_route_fail_closed(env, monkeypatch):
    from agent.delegation_context import delegated_child_context
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    tokens = set_session_vars(platform="telegram", session_key=env.entry.session_key, session_id=env.entry.session_id, cron_session="")
    try:
        with delegated_child_context(env.entry.session_id):
            assert json.loads(env.handler({"action": "status"}, session_id=env.entry.session_id))["error_code"] == "unsupported_surface"
        assert json.loads(env.handler({"action": "status"}, session_id="other"))["error_code"] == "missing_session_context"
        other = env.home.parent / "other-home"
        token = set_hermes_home_override(other)
        try:
            assert json.loads(env.handler({"action": "set", "interval": "1m", "prompt": "p"}, session_id=env.entry.session_id))["error_code"] == "profile_mismatch"
            assert not (other / "state.db").exists()
        finally:
            reset_hermes_home_override(token)
    finally:
        clear_session_vars(tokens)
    for platform in ("cli", "tui", "desktop", "api_server", "kanban", "unknown"):
        tokens = set_session_vars(platform=platform, session_key=env.entry.session_key, session_id=env.entry.session_id)
        try:
            assert json.loads(env.handler({"action": "set", "interval": "1m", "prompt": "p"}, session_id=env.entry.session_id))["error_code"] == "unsupported_surface"
        finally:
            clear_session_vars(tokens)
    env.db.end_session(env.entry.session_id, "session_reset")
    assert env.call("set", interval="1m", prompt="p")["error_code"] == "not_current_route"
    assert env.db.get_meta("heartbeat:" + env.entry.session_id) is None


def test_corrupt_and_failed_writes_never_acknowledge_success(env):
    key = "heartbeat:" + env.entry.session_id
    env.db.set_meta(key, "not-json")
    assert env.call("status")["error_code"] == "invalid_stored_state"
    assert env.call("set", interval="1m", prompt="p")["error_code"] == "invalid_stored_state"
    env.db.set_meta(key, "")
    # Deliberately suppress host write to exercise the native swallowed-write boundary.
    with patch("hermes_cli.heartbeat.save_heartbeat", lambda *a: None):
        assert env.call("set", interval="1m", prompt="p")["error_code"] == "persistence_unverified"
    assert env.db.get_meta(key) == ""


class RecordingAdapter:
    """Fake transport/runner entry. Native poll and routing are NOT mocked."""
    def __init__(self):
        self._message_handler = True
        self._active_sessions = {}
        self._session_tasks = {}
        self.events = []
    async def handle_message(self, event):
        self.events.append(event)
        event._heartbeat_execution_started = True  # fake runner entry, no model inference
        self._active_sessions[event.metadata["gateway_session_key"]] = object()


async def exercise_poller(env):
    from gateway.run import GatewayRunner
    from gateway.run_heartbeat_restore import restore_heartbeat_watches
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config, runner.session_store = env.config, env.store
    runner._heartbeat_watch, runner._background_tasks = {}, set()
    runner._profile_name_for_source = lambda source, adapter_profile=None: None
    runner._run_in_executor_with_context = asyncio.to_thread
    adapter = RecordingAdapter()
    runner._delivery_adapter_for = lambda source: adapter
    busy = True
    queue_depth = 0
    runner._is_session_running = lambda key: busy
    runner._queue_depth = lambda key, adapter=None: queue_depth
    # Accelerated poll cadence and injected clock; not a real sixty-second waiting test.
    clock = SimpleNamespace(time=lambda: 1000.0)
    with patch("hermes_cli.heartbeat.POLL_SECONDS", .01), patch("hermes_cli.heartbeat.time", clock):
        assert env.call("set", interval="1m", prompt="Native wake") ["ok"]
        clock.time = lambda: 1061.0
        runner._start_heartbeat_poller()  # real native task, starts with NO watch
        async def wait_for(predicate):
            for _ in range(200):
                if predicate():
                    return
                await asyncio.sleep(.01)
            raise AssertionError("native poller did not reach expected state")
        try:
            await wait_for(lambda: bool(runner._heartbeat_watch))
            assert not adapter.events
            assert HeartbeatManager(env.entry.session_id).state.fire_count == 0
            busy, queue_depth = False, 1
            await asyncio.sleep(.04)
            assert not adapter.events
            queue_depth = 0
            await wait_for(lambda: len(adapter.events) == 1)
            event = adapter.events[0]
            assert "Native wake" in event.text and not event.internal
            assert event.source.thread_id == "42" and event.source.message_id is None
            assert event._heartbeat_session_id == env.entry.session_id
            assert HeartbeatManager(env.entry.session_id).state.fire_count == 1
            adapter._active_sessions.clear()
            assert env.call("pause")["ok"]
            clock.time = lambda: 1200.0
            await asyncio.sleep(.04)
            assert len(adapter.events) == 1
            assert env.call("resume")["ok"]
            await asyncio.sleep(.04)
            assert len(adapter.events) == 1  # resume re-anchors, no stale immediate fire
            clock.time = lambda: 1261.0
            await wait_for(lambda: len(adapter.events) == 2)
            adapter._active_sessions.clear()
            assert env.call("clear")["ok"]
            clock.time = lambda: 1400.0
            await asyncio.sleep(.04)
            assert len(adapter.events) == 2
            assert not runner._heartbeat_watch
            # Native migration + routed compression child, then native reset boundary.
            assert env.call("set", interval="1m", prompt="Across compression")["ok"]
            old = env.entry.session_id
            child = old + "-compression"
            env.db.create_session(child, "telegram", session_key=env.entry.session_key)
            assert migrate_heartbeat_to_session(old, child)
            assert env.store.switch_session(env.entry.session_key, child, expected_session_id=old) is not None
            assert env.call("status", sid=child)["heartbeat"]["prompt"] == "Across compression"
            assert env.call("status", sid=old)["error_code"] == "not_current_route"
            await restore_heartbeat_watches(runner)
            clock.time = lambda: 1461.0
            await wait_for(lambda: len(adapter.events) == 3)
            assert adapter.events[-1]._heartbeat_session_id == child
            adapter._active_sessions.clear()
            env.store.reset_session(env.entry.session_key)
            clock.time = lambda: 1600.0
            await asyncio.sleep(.06)
            assert len(adapter.events) == 3
            assert HeartbeatManager(child).state is None
            assert not runner._heartbeat_watch
            print("NATIVE POLLER: empty-watch recovery -> busy/queue defer -> idle admission; pause/resume/clear; compression/reset PASS (fake transport + clock, no model)")
        finally:
            runner._heartbeat_poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner._heartbeat_poll_task


def test_native_gateway_reentry_without_new_inbound_message(env):
    asyncio.run(exercise_poller(env))

def test_same_session_id_is_isolated_between_two_scratch_homes(env):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    assert env.call("set", interval="1m", prompt="Home A")["ok"]
    home_b = env.home.parent / "home-b"
    home_b.mkdir()
    (home_b / "config.yaml").write_text("plugins:\n  enabled: [session-heartbeat]\n", encoding="utf-8")
    shutil.copytree(os.environ["HERMES_TEST_PLUGIN"], home_b / "plugins/session-heartbeat")
    token = set_hermes_home_override(home_b)
    db_b = acquire(home_b / "state.db")
    manager_b = None
    tokens = set_session_vars(platform="telegram", session_key=env.entry.session_key, session_id=env.entry.session_id, cron_session="")
    try:
        # Identical session id/key deliberately tests scope, not globally unique ids.
        db_b.create_session(env.entry.session_id, "telegram", session_key=env.entry.session_key)
        db_b.save_gateway_routing_entry(env.entry.session_key, json.dumps(env.entry.to_dict()), scope=str((home_b / "sessions").resolve()))
        manager_b = PluginManager()
        manager_b.discover_and_load()
        result = json.loads(registry.dispatch("session_heartbeat", {"action": "set", "interval": "2m", "prompt": "Home B"}, scope=manager_b.scope_key, session_id=env.entry.session_id))
        assert result["ok"]
        assert HeartbeatManager(env.entry.session_id).state.prompt == "Home B"
    finally:
        if manager_b:
            manager_b.unload()
        release(db_b)
        clear_session_vars(tokens)
        reset_hermes_home_override(token)
    assert env.call("status")["heartbeat"]["prompt"] == "Home A"
    print("PROFILE: same session id/key across two scratch HERMES_HOME stores remains isolated PASS")


def test_storage_read_failure_does_not_become_empty_status(env):
    with patch.object(env.db, "get_meta", side_effect=OSError("simulated read failure")):
        assert env.call("status")["error_code"] == "storage_error"


@contextlib.contextmanager
def native_turn(env):
    """Real Gateway binder, not the older fixture's always-published session id."""
    from gateway.run import GatewayRunner
    from gateway.session import build_session_context
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {}
    tokens = runner._set_session_env(build_session_context(env.source, env.config, env.entry))
    agent = SimpleNamespace(
        session_id=env.entry.session_id, _context_engine_tool_names=set(),
        _memory_manager=None, quiet_mode=False, valid_tool_names={"session_heartbeat"},
        enabled_toolsets=["session_heartbeat"], disabled_toolsets=[],
    )
    try:
        yield runner, agent
    finally:
        runner._clear_session_env(tokens)
        runner._shutdown_executor(drain_timeout=2)  # this scratch runner's pool only


def native_dispatch(agent, action, **args):
    from agent.tool_executor import _ToolCallRef, _resolve_sequential_dispatch
    ref = _ToolCallRef(name="session_heartbeat", args={"action": action, **args},
                          task_id=agent.session_id, call_id="regression-" + action, trace=[])
    return json.loads(_resolve_sequential_dispatch(agent, ref, []).execute(ref.args))


def publish_child(env, parent, child):
    assert env.db.try_acquire_compression_lock(parent, "offline-regression")
    try:
        env.db.publish_compression_child(
            parent_session_id=parent, child_session_id=child, source="telegram",
            model="offline-regression", model_config={}, system_prompt="offline-regression",
            compression_lock_holder="offline-regression",
            messages=[{"role": "user", "content": "Isolated durable handoff"}],
        )
    finally:
        env.db.release_compression_lock(parent, "offline-regression")
    assert migrate_heartbeat_to_session(parent, child)


@pytest.mark.parametrize("cached", [False, True], ids=["fresh", "cached"])
def test_real_gateway_binder_executor_dispatch_controls(env, cached):
    from agent.agent_init import _publish_session_id
    from gateway.session_context import get_session_env
    # Preserve multiplex-on, same-home support; cross-home remains a separate refusal.
    env.config.multiplex_profiles = True
    with native_turn(env) as (runner, agent):
        def worker():
            if cached:
                runner._init_cached_agent_for_turn(agent, 0)
                assert get_session_env("HERMES_SESSION_ID") == ""
            else:
                _publish_session_id(agent.session_id)
            results = []
            for action, args in [
                ("status", {}), ("set", {"interval": "1m", "prompt": "Native dispatch"}),
                ("pause", {}), ("resume", {}), ("clear", {}),
            ]:
                result = native_dispatch(agent, action, **args)
                assert result["ok"], result
                raw = env.db.get_meta("heartbeat:" + agent.session_id)
                persisted = json.loads(raw) if raw else None
                state = HeartbeatManager(agent.session_id).state
                if action in {"status", "clear"}:
                    assert state is None and result["heartbeat"] is None
                    if action == "clear":
                        assert persisted is not None
                        assert persisted["status"] == "cleared"
                else:
                    assert state is not None
                    assert state.to_json() == raw
                    assert persisted == result["heartbeat"]
                    assert state.status == ("paused" if action == "pause" else "active")
                results.append(action)
            if cached:
                assert get_session_env("HERMES_SESSION_ID") == ""
            return results
        assert asyncio.run(runner._run_in_executor_with_context(worker)) == ["status", "set", "pause", "resume", "clear"]
    print(f"NATIVE DISPATCH: {'cached blank-id' if cached else 'fresh published-id'} binder -> real context executor -> sequential executor -> model_tools -> registry; all controls + native DB readback PASS")


def test_compression_tip_clear_before_gateway_repoint_does_not_revive(env):
    from agent.agent_init import _publish_session_id
    assert env.call("set", interval="1m", prompt="Clear in compressed turn")["ok"]
    old = env.entry.session_id
    child = old + "-native-compression"
    with native_turn(env) as (_, agent):
        publish_child(env, old, child)
        agent.session_id = child
        _publish_session_id(child)
        assert env.db.get_session(old)["end_reason"] == "compression"
        assert env.db.get_session(child)["session_key"] == env.entry.session_key
        assert env.store.peek_session_id(env.entry.session_key) == old
        assert env.db.gateway_routing_entry_for_session(child) is None
        result = native_dispatch(agent, "clear")
        assert result["ok"] and result["changed"], result
        cleared = env.db.get_meta("heartbeat:" + child)
        assert json.loads(cleared)["status"] == "cleared"
        assert HeartbeatManager(child).state is None
        assert HeartbeatManager(old).state is None
        assert env.store.peek_session_id(env.entry.session_key) == old  # plugin cannot repoint
        assert env.store.switch_session(env.entry.session_key, child, expected_session_id=old)
        assert native_dispatch(agent, "status")["heartbeat"] is None
        assert not native_dispatch(agent, "clear")["changed"]
        assert env.db.get_meta("heartbeat:" + child) == cleared
        assert not migrate_heartbeat_to_session(old, child)
        assert env.db.get_meta("heartbeat:" + child) == cleared
        assert HeartbeatManager(child).state is None
    print("COMPRESSION: real lease/publication -> native migration -> child dispatch clear BEFORE repoint -> canonical repoint; child cleared, parent retired, no revival PASS (no compression model)")


def test_only_native_live_tip_not_ancestors_or_fork_can_write(env):
    from agent.agent_init import _publish_session_id
    assert env.call("set", interval="1m", prompt="Two in-turn compressions")["ok"]
    old = env.entry.session_id
    child, tip, fork = old + "-child", old + "-tip", old + "-fork"
    publish_child(env, old, child)
    publish_child(env, child, tip)
    env.db.create_session(fork, "telegram", parent_session_id=old,
                          session_key=env.entry.session_key, model_config={"_branched_from": old})
    HeartbeatManager(fork).set("Separate branch", 120)
    assert env.db.get_compression_tip(old) == tip
    keys = ["heartbeat:" + sid for sid in (old, child, tip, fork)]
    with native_turn(env) as (_, agent):
        for denied in (old, child, fork):
            before = [env.db.get_meta(key) for key in keys]
            agent.session_id = denied
            _publish_session_id(denied)
            result = native_dispatch(agent, "set", interval="1m", prompt="Must not write")
            assert result.get("error_code") == "not_current_route", result
            assert [env.db.get_meta(key) for key in keys] == before
        agent.session_id = tip
        _publish_session_id(tip)
        assert native_dispatch(agent, "status")["heartbeat"]["prompt"] == "Two in-turn compressions"
        assert native_dispatch(agent, "clear")["ok"]
        assert HeartbeatManager(tip).state is None
        fork_state = HeartbeatManager(fork).state
        assert fork_state is not None and fork_state.prompt == "Separate branch"
        assert env.store.peek_session_id(env.entry.session_key) == old
    print("NATIVE TOPOLOGY: current owner -> two published compression children; only live native tip accepted, ancestors/explicit branch refused without state writes PASS")


@pytest.mark.parametrize("boundary", [
    "explicit_mismatch", "delegated", "delegated_process", "missing_key", "other_key",
    "other_home", "suspended", "switch", "reset", "other_scope", "platform",
])
def test_native_compression_dispatch_boundaries_do_not_write(env, monkeypatch, boundary):
    from agent.agent_init import _publish_session_id
    from agent.delegation_context import delegated_child_context
    from gateway.session_context import scoped_current_session_id
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    assert env.call("set", interval="1m", prompt="Protected child")["ok"]
    other_source = SessionSource(platform=Platform.TELEGRAM, chat_id="unrelated", user_id="u")
    other = env.store.get_or_create_session(other_source)
    HeartbeatManager(other.session_id).set("Unrelated heartbeat", 120)
    home_b = env.home.parent / "different-home"
    old, child = env.entry.session_id, env.entry.session_id + "-boundary-child"
    publish_child(env, old, child)
    with native_turn(env) as (_, agent), contextlib.ExitStack() as stack:
        agent.session_id = child
        _publish_session_id(child)
        expected = "not_current_route"
        if boundary == "explicit_mismatch":
            stack.enter_context(scoped_current_session_id(old))
            expected = "missing_session_context"
        elif boundary == "delegated":
            stack.enter_context(delegated_child_context(child))
            expected = "unsupported_surface"
        elif boundary == "delegated_process":
            monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
            expected = "unsupported_surface"
        elif boundary in {"missing_key", "other_key", "platform"}:
            tokens = set_session_vars(platform="discord" if boundary == "platform" else "telegram",
                                      session_key="" if boundary == "missing_key" else
                                      other.session_key if boundary == "other_key" else env.entry.session_key,
                                      session_id=child, cron_session="")
            stack.callback(clear_session_vars, tokens)
            if boundary == "missing_key":
                expected = "missing_session_context"
        elif boundary == "other_home":
            token = set_hermes_home_override(home_b)
            stack.callback(reset_hermes_home_override, token)
            expected = "profile_mismatch"
        elif boundary == "suspended":
            route = env.entry.to_dict()
            route["suspended"] = True
            env.db.save_gateway_routing_entry(env.entry.session_key, json.dumps(route), scope=env.store._routing_scope())
        elif boundary == "switch":
            assert env.store.switch_session(env.entry.session_key, other.session_id, expected_session_id=old)
        elif boundary == "reset":
            env.store.reset_session(env.entry.session_key)
        elif boundary == "other_scope":
            rows = env.db.load_gateway_routing_entries(scope=env.store._routing_scope())
            env.db.replace_gateway_routing_entries({}, scope=env.store._routing_scope())
            env.db.replace_gateway_routing_entries(rows, scope=str(env.home / "different-sessions"))
        heartbeat_keys = ["heartbeat:" + sid for sid in (old, child, other.session_id)]
        before = [env.db.get_meta(key) for key in heartbeat_keys]
        routes_before = env.db.list_gateway_routing_rows()
        result = native_dispatch(agent, "clear")
        if boundary == "other_home":
            assert result == {"error": "Unknown tool: session_heartbeat"}  # host scope gate
            scoped_result = registry.dispatch("session_heartbeat", {"action": "clear"},
                                              scope=env.manager.scope_key, session_id=child)
            assert isinstance(scoped_result, str)
            result = json.loads(scoped_result)  # plugin gate
        assert result.get("error_code") == expected, result
        assert [env.db.get_meta(key) for key in heartbeat_keys] == before
        assert env.db.list_gateway_routing_rows() == routes_before
        if boundary == "other_home":
            assert not (home_b / "state.db").exists()
    print(f"NATIVE BOUNDARY: {boundary} refused; child/ancestor/unrelated heartbeat and routing unchanged PASS")


def test_suspended_or_unrouted_session_cannot_write(env):
    route = env.entry.to_dict()
    route["suspended"] = True
    scopes = {row["scope"] for row in env.db.list_gateway_routing_rows()}
    assert scopes
    for scope in scopes:
        env.db.save_gateway_routing_entry(env.entry.session_key, json.dumps(route), scope=scope)
    assert env.call("set", interval="1m", prompt="p")["error_code"] == "not_current_route"
    for scope in scopes:
        env.db.replace_gateway_routing_entries({}, scope=scope)
    assert env.call("set", interval="1m", prompt="p")["error_code"] == "not_current_route"
    assert env.db.get_meta("heartbeat:" + env.entry.session_id) is None

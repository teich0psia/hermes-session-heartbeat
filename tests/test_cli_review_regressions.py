"""Independent-review P1/P2: native callers retained, only lower I/O is harnessed."""
import json
import queue
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_heartbeat import env, native_dispatch, publish_child
from test_local_surfaces import cli, CLIHarness, agent_shell, executor, settle_cli, wait_for
from agent.agent_init import _publish_session_id
from gateway.session_context import scoped_current_session_id
from hermes_cli.active_sessions import try_acquire_active_session
from hermes_cli.heartbeat import HeartbeatManager


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr("hermes_cli.heartbeat.POLL_SECONDS", .01)


def compress(env, cli, suffix):
    old = cli.session_id
    child = old + suffix
    publish_child(env, old, child)
    cli.agent.session_id = child
    _publish_session_id(child)
    return old, child


def test_native_settlement_following_turn_five_controls_twice_and_child_watchdog(env, cli):
    anchor = cli.session_id
    clock = SimpleNamespace(time=lambda: 1000.0)
    with patch("hermes_cli.heartbeat.time", clock), executor() as run:
        assert run(lambda: native_dispatch(cli.agent, "set", interval="1m", prompt="Ancestor"))["ok"]
        for suffix in ["-child", "-grandchild"]:
            assert run(lambda: native_dispatch(cli.agent, "set", interval="1m", prompt="Migrate this schedule"))["ok"]
            cached = cli._get_heartbeat_manager()
            old, child = compress(env, cli, suffix)
            assert run(lambda: native_dispatch(cli.agent, "clear"))["ok"]
            assert cli.session_id == old and cli._heartbeat_manager is cached
            assert cli._active_session_lease.session_id == anchor
            settle_cli(cli)  # actual native SID assignment; no invented lease transfer
            assert cli.session_id == child and cli._active_session_lease.session_id == anchor
            for action, args in [("status", {}), ("set", {"interval": "1m", "prompt": child}),
                                 ("pause", {}), ("resume", {}), ("clear", {})]:
                result = run(lambda: native_dispatch(cli.agent, action, **args))
                assert result["ok"], result
                assert result["session_id"] == child
                assert cli._get_heartbeat_manager().session_id == child
                assert result["heartbeat"] == env.handler._snapshot(cli._get_heartbeat_manager().state)
            assert HeartbeatManager(old).state is None
            for stale in [old, anchor]:
                with scoped_current_session_id(stale):
                    assert not run(lambda: native_dispatch(cli.agent, "set", interval="1m", prompt="Refuse old"))["ok"]
        assert run(lambda: native_dispatch(cli.agent, "set", interval="1m", prompt="Child watchdog"))["ok"]
        child_mgr = cli._get_heartbeat_manager()
        cli._agent_running = False
        clock.time = lambda: 1061.0
        wait_for(lambda: not cli._pending_input.empty())
        assert "Child watchdog" in cli._pending_input.get_nowait()
        assert child_mgr.session_id == cli.agent.session_id and child_mgr.state.fire_count == 1
        assert HeartbeatManager(anchor).state is None
        cli._agent_running = True
        assert run(lambda: native_dispatch(cli.agent, "clear"))["ok"]
    print("P1 native settlement twice -> following ordinary executor five controls; original lease unchanged; cached watchdog uses child PASS")


@pytest.mark.parametrize("boundary", ["released", "foreign_child", "missing_anchor", "wrong_pid",
                                      "wrong_live_identity", "lease_home", "owner_ref", "agent_db", "branch", "corrupt_registry"])
def test_settled_cli_original_lease_proof_refusals_do_not_write(env, cli, boundary):
    anchor = cli.session_id
    HeartbeatManager(anchor).set("Protected", 60)
    _, child = compress(env, cli, "-settled")
    settle_cli(cli)
    lease = cli._active_session_lease
    path = lease.state_path
    foreign = None
    try:
        if boundary == "released":
            lease.release()
        elif boundary == "foreign_child":
            foreign, refusal = try_acquire_active_session(session_id=child, surface="cli", config={},
                                                          metadata={"live_session_id": "other-live-cli"})
            assert foreign is not None and refusal is None
        elif boundary in {"missing_anchor", "wrong_pid", "wrong_live_identity"}:
            raw = json.loads(path.read_text())
            own = next(e for e in raw["entries"] if e["lease_id"] == lease.lease_id)
            if boundary == "missing_anchor": raw["entries"].remove(own)
            elif boundary == "wrong_pid": own["pid"] = 1
            else: own["metadata"]["live_session_id"] = "other-cli"
            path.write_text(json.dumps(raw))  # scratch-only hostile registry input
        elif boundary == "lease_home":
            lease.state_path = env.home / "foreign/runtime/active_sessions.json"
        elif boundary == "owner_ref":
            env.manager._cli_ref = SimpleNamespace(session_id=child, agent=cli.agent)
        elif boundary == "agent_db":
            cli.agent._session_db = SimpleNamespace(db_path=env.home / "foreign/state.db")
        elif boundary == "branch":
            branch = child + "-branch"
            env.db.create_session(branch, "cli", parent_session_id=child, model_config={"_branched_from": child})
            cli.agent.session_id = branch
            cli.session_id = branch
            _publish_session_id(branch)
        else:
            path.write_text("not-json")
        before = {sid: env.db.get_meta("heartbeat:" + sid) for sid in [anchor, child, cli.agent.session_id]}
        registry_before = path.read_bytes()
        routes = env.db.list_gateway_routing_rows()
        result = native_dispatch(cli.agent, "set", interval="1m", prompt="Must refuse")
        assert not result["ok"], result
        assert {sid: env.db.get_meta("heartbeat:" + sid) for sid in before} == before
        assert env.db.list_gateway_routing_rows() == routes and path.read_bytes() == registry_before
    finally:
        if foreign: foreign.release()
        lease.state_path = path  # scratch fixture cleanup only
    print("P1 settled lease refusal", boundary, "PASS")


def test_native_thread_start_failure_retains_failclosed_across_retry_compression_reload(env, cli):
    try:
        with patch("threading.Thread.start", side_effect=RuntimeError("private injected failure")):
            first = native_dispatch(cli.agent, "set", interval="1m", prompt="Saved before arm")
        assert not first["ok"] and HeartbeatManager(cli.session_id).state is not None
        assert cli._heartbeat_watchdog_started is True
        # Reproduce independent review's original false-success path first.
        second = native_dispatch(cli.agent, "resume")
        assert not second["ok"], second
        for result in [first, second]:
            assert result["error_code"] == "driver_arm_failed"
            assert result["persisted"] is True and result["driver_armed"] is False
            assert result["heartbeat"]["status"] == "active"
            assert "private injected" not in result["error"]
        assert native_dispatch(cli.agent, "status")["ok"]
        assert native_dispatch(cli.agent, "pause")["ok"]
        old, child = compress(env, cli, "-failure-child")
        settle_cli(cli)
        assert cli._active_session_lease.session_id == old
        old_handler = env.handler
        env.manager.unload()
        env.manager.discover_and_load()  # native plugin reload in scratch, not production
        from tools.registry import registry
        env.handler = registry.get_entry("session_heartbeat", scope=env.manager.scope_key).handler
        assert env.handler is not old_handler and env.manager._cli_ref is cli
        for action, args in [("resume", {}), ("set", {"interval": "2m", "prompt": "Still unarmed"})]:
            result = native_dispatch(cli.agent, action, **args)
            assert result["error_code"] == "driver_arm_failed" and result["persisted"]
            assert result["session_id"] == child and result["driver_armed"] is False
        assert native_dispatch(cli.agent, "clear")["ok"]
        assert native_dispatch(cli.agent, "status")["heartbeat"] is None
        assert cli._heartbeat_watchdog_started is True  # never repaired by plugin
        print("P2", json.dumps(dict(first=first, retry=second, compressed_reload="failclosed", readonly_pause_clear="available")))
    finally:
        # Native Thread.start failed, so no thread can clear its latch at exit.
        # Test-only fixture teardown; production adapter never changes this field.
        cli._heartbeat_watchdog_started = False


def test_cli_save_failure_is_not_an_arm_failure(env, cli):
    with patch("hermes_cli.heartbeat.save_heartbeat", lambda *a: None):
        result = native_dispatch(cli.agent, "set", interval="1m", prompt="Not saved")
    assert result["error_code"] == "persistence_unverified"
    assert "persisted" not in result and "driver_armed" not in result
    assert HeartbeatManager(cli.session_id).state is None
    assert not getattr(cli, "_heartbeat_watchdog_started", False)
    assert not getattr(cli, "_session_heartbeat_arm_failed", False)
    print("P2 save failure remains distinct from persisted=true / driver_arm_failed PASS")


def test_arm_failure_is_owner_scoped_not_thread_name_or_new_cli(env, cli):
    other = None
    try:
        with patch("threading.Thread.start", side_effect=RuntimeError("lower start failure")):
            assert not native_dispatch(cli.agent, "set", interval="1m", prompt="Failed owner")["ok"]
        other = CLIHarness()
        other.__dict__.update({k: v for k, v in cli.__dict__.items() if k not in {
            "_heartbeat_manager", "_active_session_lease", "_session_heartbeat_arm_failed", "_heartbeat_watchdog_started"}})
        other.session_id = "independent-live-cli"
        env.db.create_session(other.session_id, "cli")
        other.agent = agent_shell(other.session_id, env.db)
        other._pending_input = queue.Queue()
        other._active_session_lease, refusal = try_acquire_active_session(
            session_id=other.session_id, surface="cli", config={}, metadata={"live_session_id": other.session_id})
        assert refusal is None
        env.manager._cli_ref = other
        _publish_session_id(other.session_id)
        assert native_dispatch(other.agent, "set", interval="1m", prompt="Other owner")["ok"]
        assert other._heartbeat_watchdog_started
        assert native_dispatch(other.agent, "resume")["ok"]  # normal idempotent native arm
        assert native_dispatch(other.agent, "clear")["ok"]
        env.manager._cli_ref = cli
        _publish_session_id(cli.session_id)
        assert any(t.name == "heartbeat-watchdog" for t in threading.enumerate())
        result = native_dispatch(cli.agent, "resume")
        assert not result["ok"] and result["error_code"] == "driver_arm_failed"
        assert native_dispatch(cli.agent, "clear")["ok"]
    finally:
        cli._heartbeat_watchdog_started = False  # scratch failed-start cleanup, no real thread
        if other:
            other._should_exit = True
            wait_for(lambda: not other._heartbeat_watchdog_started)
            other._active_session_lease.release()
        env.manager._cli_ref = cli
    print("P2 other real native watchdog cannot authorize failed owner; new owner normal/idempotent arming PASS")

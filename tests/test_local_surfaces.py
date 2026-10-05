"""Isolated native binders, ordinary executor/registry, DB and idle drivers.

UI/CLI/agent shells and model/transport are harnesses, not real rendered sessions.
Entry-point functions are compiled unmodified from the selected host AST to avoid
server reapers/dotenv/bootstrap; split functions use native method_ctx.rebind.
"""
import ast
import asyncio
import contextlib
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest
from test_heartbeat import env, native_dispatch, publish_child
from gateway.session_context import clear_session_vars, get_session_env, scoped_current_session_id
from hermes_cli.active_sessions import try_acquire_active_session, transfer_active_session
from hermes_cli.heartbeat import HeartbeatManager, migrate_heartbeat_to_session
from hermes_cli.cli_loops_mixin import CLILoopsMixin
from hermes_cli.cli_tui_runtime_mixin import CLITuiRuntimeMixin
from tui_gateway import session_notifications as notifications
from tui_gateway.method_ctx import rebind

HOST = Path(os.environ["HERMES_SOURCE"])


def native_function(path, name, globals_):
    tree = ast.parse((HOST / path).read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(HOST / path), "exec"), globals_)
    return globals_[name]


def settle_cli(cli):
    """Unmodified native settle + YOLO transfer; only stream/display sinks are shells."""
    from hermes_cli.cli_session_mixin import CLISessionMixin
    cli._prompt_start_time = None
    cli.conversation_history = []
    cli._flush_stream = lambda: None
    cli._write_terminal_breadcrumb = lambda: None
    cli._transfer_session_yolo = lambda a, b: CLISessionMixin._transfer_session_yolo(cli, a, b)
    settle = native_function("hermes_cli/cli_chat_turn_mixin.py", "_chat_settle_turn", dict(time=time, sys=sys))
    settle(cli, SimpleNamespace(result={"messages": []}, use_streaming_tts=False))


def agent_shell(sid, db):
    return SimpleNamespace(session_id=sid, _session_db=db, _delegate_depth=0,
                           _context_engine_tool_names=set(), _memory_manager=None,
                           quiet_mode=False, valid_tool_names={"session_heartbeat"},
                           enabled_toolsets=["session_heartbeat"], disabled_toolsets=[])


@contextlib.contextmanager
def executor():
    from gateway.run import GatewayRunner
    runner = GatewayRunner.__new__(GatewayRunner)
    try:
        yield lambda fn: asyncio.run(runner._run_in_executor_with_context(fn))
    finally:
        runner._shutdown_executor(drain_timeout=2)


class CLIHarness(CLILoopsMixin, CLITuiRuntimeMixin):
    def _tui_idle_tick(self):
        pass  # model/config housekeeping below consumer boundary


@pytest.fixture
def cli(env, monkeypatch):
    from hermes_cli import plugins
    monkeypatch.setitem(plugins._plugin_managers_by_home, env.home.resolve(), env.manager)
    monkeypatch.setattr(plugins, "_plugin_manager", env.manager)
    monkeypatch.setenv("HERMES_DEFER_AGENT_STARTUP", "1")
    cli = CLIHarness()
    cli.config = {}
    cli.session_id = "local-cli"
    env.db.create_session(cli.session_id, "cli")
    cli._session_db = env.db
    cli.agent = agent_shell(cli.session_id, env.db)
    cli._single_query_mode = False
    cli._active_session_lease, refusal = try_acquire_active_session(
        session_id=cli.session_id, surface="cli", config={}, metadata={"live_session_id": cli.session_id})
    assert refusal is None
    attach = native_function("hermes_cli/cli_tui_mixin.py", "_tui_init_run_state",
                             dict(queue=queue, threading=threading, os=os))
    attach(cli)
    assert env.manager._cli_ref is cli  # actual invocation-time native attach
    cli._agent_running = cli._interactive_turn = True
    clear_session_vars([])
    from agent.agent_init import _publish_session_id
    _publish_session_id(cli.session_id)  # CLI has no Gateway key/platform binder
    yield cli
    cli._should_exit = True
    deadline = time.monotonic() + 2
    while getattr(cli, "_heartbeat_watchdog_started", False) and time.monotonic() < deadline:
        time.sleep(.01)
    assert not getattr(cli, "_heartbeat_watchdog_started", False)
    cli._active_session_lease.release()
    clear_session_vars([])


@pytest.fixture
def desktop(env, monkeypatch):
    sid, key = "ui-runtime", "local-desktop"
    env.db.create_session(key, "desktop")
    agent = agent_shell(key, env.db)
    lease, refusal = try_acquire_active_session(session_id=key, surface="desktop", config={},
                                              registry_home=env.home, metadata={"live_session_id": sid})
    assert refusal is None
    # A server-global facade only; never import entry-point startup code.
    server = ModuleType("tui_gateway.server")
    server._sessions, server._sessions_lock = {}, threading.RLock()
    server._hermes_home = server._HERMES_HOME_AT_IMPORT = env.home
    from hermes_constants import get_process_hermes_home
    server._launch_home = native_function("tui_gateway/server.py", "_launch_home",
                                          dict(vars(server), Path=Path, get_process_hermes_home=get_process_hermes_home))
    monkeypatch.setitem(sys.modules, "tui_gateway.server", server)
    init_globals = dict(vars(server), time=time, threading=threading,
                        _resolve_session_source=lambda s: s, _completion_cwd=lambda: str(env.home),
                        _load_show_reasoning=lambda: False, _load_tool_progress_mode=lambda: "off",
                        current_transport=lambda: None, _stdio_transport=None,
                        _transport_auth_user_id=lambda t: None,
                        _session_todo_state=lambda *a: None, _hydrate_session_cwd=lambda *a: None,
                        _register_session_cwd=lambda *a: None, _wire_session_agent=lambda *a: None,
                        _start_session_services=lambda *a: None, _emit=lambda *a: None,
                        _session_info=lambda *a: {}, _schedule_mcp_late_refresh=lambda *a: None)
    init = native_function("tui_gateway/server.py", "_init_session", init_globals)
    init(sid, key, agent, [], source="desktop")
    record = server._sessions[sid]
    assert record["profile_home"] is None  # actual native default, not a generous fixture
    record.update(running=True, _notif_stop=threading.Event(), active_session_lease=lease)
    lookup = native_function("tui_gateway/server.py", "_session_for_key", vars(server))
    g = dict(contextlib=contextlib, _session_for_key=lookup,
             _resolve_session_platform=lambda: "desktop", _current_profile_name=lambda: "default",
             _session_source=lambda s: s["source"], profile_name_for_home=lambda h: "default",
             _methods_browser_control=SimpleNamespace(_is_authenticated_identity=lambda i: False))
    bind = native_function("tui_gateway/server.py", "_set_session_context", g)
    tokens = bind(key, ui_session_id=sid)
    assert get_session_env("HERMES_SESSION_PLATFORM") == ""
    assert get_session_env("HERMES_UI_SESSION_ID") == sid
    yield SimpleNamespace(sid=sid, record=record, server=server, bind=bind, agent=agent)
    clear_session_vars(tokens)
    lease.release()


@pytest.mark.parametrize("surface", ["cli", "desktop"])
@pytest.mark.parametrize("cached", [False, True])
def test_native_binding_ordinary_dispatch_five_controls(env, request, surface, cached):
    shell = request.getfixturevalue(surface)
    agent = shell.agent
    # Cached turns retain their native owner and executor, not a special parent_agent kwarg.
    if cached:
        assert native_dispatch(agent, "status")["ok"]
    with patch("hermes_cli.heartbeat.POLL_SECONDS", .01), executor() as run:
        assert not getattr(shell, "_heartbeat_watchdog_started", False)
        for action, args in [("status", {}), ("set", {"interval": "1m", "prompt": "Native controls"}),
                             ("pause", {}), ("resume", {}), ("clear", {})]:
            result = run(lambda: native_dispatch(agent, action, **args))
            assert result["ok"], result
            assert result["driver"] == ("native_cli_heartbeat_watchdog" if surface == "cli" else "native_desktop_notification_poller")
            state = HeartbeatManager(agent.session_id).state
            assert result["heartbeat"] == (json.loads(state.to_json()) if state else None)
            if surface == "cli":
                assert shell._get_heartbeat_manager().state is state or (
                    result["heartbeat"] == env.handler._snapshot(shell._get_heartbeat_manager().state))
                assert bool(getattr(shell, "_heartbeat_watchdog_started", False)) == (action != "status")
        assert not native_dispatch(agent, "clear")["changed"]
    print(f"LOCAL DISPATCH {surface} {'cached' if cached else 'fresh'}: native binder/attach -> context executor -> sequential -> model_tools -> registry; 5 controls PASS")


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("native driver timeout")


def test_cli_native_watchdog_fifo_selfclear_and_cold_resume(env, cli):
    cached = cli._get_heartbeat_manager()
    clock = SimpleNamespace(time=lambda: 1000.0)
    with patch("hermes_cli.heartbeat.time", clock), patch("hermes_cli.heartbeat.POLL_SECONDS", .01):
        assert native_dispatch(cli.agent, "set", interval="1m", prompt="Idle FIFO")["ok"]
        assert cli._get_heartbeat_manager() is cached
        clock.time = lambda: 1061.0
        time.sleep(.04)
        assert cli._pending_input.empty() and cached.state.fire_count == 0
        cli._agent_running = False
        cli._pending_input.put("human first")
        time.sleep(.04)
        assert cli._pending_input.qsize() == 1 and cached.state.fire_count == 0
        assert cli._pending_input.get_nowait() == "human first"
        wait_for(lambda: cli._pending_input.qsize() == 1)
        assert cached.state.fire_count == 1
        admitted = []
        def consume(text):
            # Real native FIFO consumer; model boundary shell performs self-clear.
            admitted.append(text)
            cli._agent_running = cli._interactive_turn = True
            assert native_dispatch(cli.agent, "clear")["ok"]
            cli._should_exit = True
        cli._tui_process_one_input = consume
        cli._tui_process_loop()
        assert len(admitted) == 1 and "Idle FIFO" in admitted[0]
        assert cached.state is None and HeartbeatManager(cli.session_id).state is None
        wait_for(lambda: not cli._heartbeat_watchdog_started)
        cli._should_exit = False
        cli._agent_running = cli._interactive_turn = True
        # Re-open shell: state persists but attach alone does not arm.
        HeartbeatManager(cli.session_id).set("Saved on close", 60)
        del cli._heartbeat_manager
        assert native_dispatch(cli.agent, "status")["ok"]
        assert not cli._heartbeat_watchdog_started
        assert native_dispatch(cli.agent, "resume")["ok"]
        assert cli._heartbeat_watchdog_started
        assert cli._get_heartbeat_manager().state.last_fired_at == 1061.0
        assert native_dispatch(cli.agent, "clear")["ok"]
        cli._agent_running = False
        clock.time = lambda: 1400.0
        time.sleep(.05)
        assert cli._pending_input.empty()
    print("CLI DRIVER: cache identity; unarmed set -> watchdog; busy/queued defer -> actual FIFO consumer self-clear; cold status unarmed/explicit resume arms PASS")


def native_ui_driver(env, ui, submit):
    @contextlib.contextmanager
    def admission(s):
        with s["history_lock"]:
            yield not s.get("_finalized")  # retirement below claim boundary
    @contextlib.contextmanager
    def session_db(s):
        yield env.db
    g = dict(contextlib=contextlib, threading=threading, time=time, sys=sys,
             _session_turn_admission=admission, _session_db=session_db, _get_db=lambda: env.db,
             _emit=lambda *a, **k: None, _run_prompt_submit=submit)
    for name in ["_notif_claim_turn", "_notif_release_turn", "_notif_gateway_owns_heartbeat",
                 "_notif_log_failure", "_maybe_fire_tui_heartbeat_tick"]:
        g[name] = rebind(getattr(notifications, name), g)
    return g["_maybe_fire_tui_heartbeat_tick"]


def test_desktop_native_poll_fresh_db_selfclear_and_gateway_view(env, desktop):
    calls = []
    def submit(rid, sid, record, prompt):
        calls.append(prompt)
        assert record["running"] is True
        assert native_dispatch(desktop.agent, "clear")["ok"]
        record["running"] = False
        return True
    fire = native_ui_driver(env, desktop, submit)
    clock = SimpleNamespace(time=lambda: 2000.0)
    with patch("hermes_cli.heartbeat.time", clock):
        assert native_dispatch(desktop.agent, "set", interval="1m", prompt="Desktop idle")["ok"]
        clock.time = lambda: 2061.0
        fire(desktop.sid, desktop.record)
        assert calls == []  # busy native claim
        desktop.record["running"] = False
        fire(desktop.sid, desktop.record)
        assert len(calls) == 1 and "Desktop idle" in calls[0]
        clock.time = lambda: 2300.0
        fire(desktop.sid, desktop.record)
        assert len(calls) == 1
        # A canonical Gateway owner remains authoritative even when UI is open.
        desktop.record["running"] = True
        assert native_dispatch(desktop.agent, "set", interval="1m", prompt="Gateway owns")["ok"]
        route = dict(session_key="telegram:private:viewer", session_id=desktop.agent.session_id,
                     origin=dict(platform="telegram", chat_id="viewer"), suspended=False)
        env.db.save_gateway_routing_entry(route["session_key"], json.dumps(route), scope=str(env.home / "sessions"))
        desktop.record["running"] = False
        clock.time = lambda: 2400.0
        fire(desktop.sid, desktop.record)
        assert len(calls) == 1 and HeartbeatManager(desktop.agent.session_id).state.fire_count == 0
    print("DESKTOP DRIVER: actual native tick fresh DB -> busy defer -> idle submit/self-clear -> no next turn; Gateway viewer skip PASS (submit/model harness)")


@pytest.mark.parametrize("surface", ["cli", "desktop"])
@pytest.mark.parametrize("action", ["clear", "set"])
def test_local_compression_before_and_after_owner_reanchor(env, request, surface, action):
    from agent.agent_init import _publish_session_id
    shell = request.getfixturevalue(surface)
    agent = shell.agent
    old, child = agent.session_id, agent.session_id + "-compression"
    with patch("hermes_cli.heartbeat.POLL_SECONDS", .01):
        assert native_dispatch(agent, "set", interval="1m", prompt="Before compression")["ok"]
        if surface == "cli":
            cached = shell._get_heartbeat_manager()
        publish_child(env, old, child)
        agent.session_id = child
        _publish_session_id(child)
        result = native_dispatch(agent, action, **({"interval": "2m", "prompt": "Child replacement"} if action == "set" else {}))
        assert result["ok"], result
        assert HeartbeatManager(old).state is None
        if surface == "cli":
            assert shell.session_id == old and shell._heartbeat_manager is cached
            settle_cli(shell)  # native settlement changes SID, not the original lease
            assert shell.session_id == child and shell._active_session_lease.session_id == old
        else:
            assert shell.record["session_key"] == old
            assert transfer_active_session(shell.record["active_session_lease"], session_id=child)
            shell.record["session_key"] = child  # UI post-turn re-anchor boundary harness
            shell.bind(child, ui_session_id=shell.sid)
        assert native_dispatch(agent, "status")["heartbeat"] == result["heartbeat"]
        assert not migrate_heartbeat_to_session(old, child)
        if action == "clear":
            assert HeartbeatManager(child).state is None
        else:
            assert HeartbeatManager(child).state.prompt == "Child replacement"
        # Stale ancestor and unrelated explicit branch are never authorized.
        with scoped_current_session_id(old):
            assert not native_dispatch(agent, "clear")["ok"]
    print(f"LOCAL COMPRESSION {surface}/{action}: native publication/migration; pre-reanchor child control leaves owner/cache unchanged; reanchor preserves state PASS")


@pytest.mark.parametrize("surface,boundary", [
    (s, b) for s in ["cli", "desktop"] for b in
    ["closed", "switched", "ended", "home", "lease", "delegated", "branch"]
] + [("cli", "oneshot"), ("cli", "no_ref"), ("desktop", "no_server"), ("desktop", "record_replaced")])
def test_local_owner_refusals_no_write(env, request, monkeypatch, surface, boundary):
    from agent.delegation_context import delegated_child_context
    shell = request.getfixturevalue(surface)
    agent = shell.agent
    old = agent.session_id
    HeartbeatManager(old).set("Protected", 60)
    # CLI normally caches through set; here no cache has been built yet.
    if boundary == "closed":
        if surface == "cli": shell._should_exit = True
        else: shell.record["_closing"] = True  # native close-before-finalize window
    elif boundary == "switched":
        if surface == "cli": shell.session_id = "different-current"
        else: shell.record["session_key"] = "different-current"
    elif boundary == "ended": env.db.end_session(old, "session_reset")
    elif boundary == "home":
        if surface == "cli": shell._session_db = SimpleNamespace(db_path=env.home / "other/state.db")
        else: shell.record["profile_home"] = str(env.home / "other")
    elif boundary == "lease":
        lease = shell._active_session_lease if surface == "cli" else shell.record["active_session_lease"]
        lease.release()
    elif boundary == "branch":
        branch = old + "-branch"
        env.db.create_session(branch, surface, parent_session_id=old, model_config={"_branched_from": old})
        agent.session_id = branch
        from agent.agent_init import _publish_session_id
        _publish_session_id(branch)
    elif boundary == "oneshot": shell._single_query_mode = True
    elif boundary == "no_ref": env.manager._cli_ref = None
    elif boundary == "no_server": monkeypatch.delitem(sys.modules, "tui_gateway.server")
    elif boundary == "record_replaced":
        shell.server._sessions[shell.sid] = {**shell.record, "agent": agent_shell("replacement", env.db)}
    before = env.db.get_meta("heartbeat:" + old)
    routes = env.db.list_gateway_routing_rows()
    with delegated_child_context(agent.session_id) if boundary == "delegated" else contextlib.nullcontext():
        result = native_dispatch(agent, "set", interval="1m", prompt="Must refuse")
    assert not result["ok"], result
    assert env.db.get_meta("heartbeat:" + old) == before
    assert env.db.list_gateway_routing_rows() == routes
    if agent.session_id != old:
        assert env.db.get_meta("heartbeat:" + agent.session_id) is None
    print(f"LOCAL REFUSAL {surface}/{boundary}: original/other state and routes unchanged PASS")


def test_compute_host_native_borrow_not_delegation(env, desktop):
    from tui_gateway.session_lifecycle import _install_borrowed_lease
    from tui_gateway.compute_host import ComputeHost
    # Actual child session reconciliation and vouch adoption, not a delegated agent.
    frame = dict(sid=desktop.sid, source="desktop", profile_home=str(env.home),
                 active_session_lease={"session_id": desktop.agent.session_id, "lease_id": "parent-owned"})
    host = ComputeHost.__new__(ComputeHost)
    host._transport = object()
    record = host._ensure_server_session(desktop.server, frame)
    assert record is desktop.record
    del record["active_session_lease"]
    _install_borrowed_lease(desktop.sid, record, frame)
    assert not record["active_session_lease"].enabled
    with patch("hermes_cli.heartbeat.POLL_SECONDS", .01):
        assert native_dispatch(desktop.agent, "set", interval="1m", prompt="Compute native owner")["ok"]
        assert native_dispatch(desktop.agent, "clear")["ok"]
    print("COMPUTE HOST: native existing-record reconciliation + borrowed lease permits ordinary child control; no delegation-marker bypass PASS (no full child launch)")


def test_desktop_notification_loop_discovers_tool_state(env, desktop):
    submitted, errors = [], []
    stop = desktop.record["_notif_stop"]
    def submit(rid, sid, record, prompt):
        submitted.append(prompt)
        tokens = desktop.bind(record["session_key"], ui_session_id=sid)
        try:
            assert native_dispatch(desktop.agent, "clear")["ok"]
        finally:
            clear_session_vars(tokens)
            record["running"] = False
        stop.set()
        return True
    fire = native_ui_driver(env, desktop, submit)
    g = dict(time=time, _BOT_DELIVERY_POLL_SECONDS=.01, _LOOP_POLL_SECONDS=.01,
             _KANBAN_POLL_SECONDS=.01, _poll_bot_live_delivery_guarded=lambda *a: None,
             _maybe_fire_tui_loop_tick=lambda *a: None, _maybe_fire_tui_heartbeat_tick=fire,
             _notif_poll_kanban=lambda *a: None, _notif_handle_ready=lambda *a: None,
             _notif_log_failure=lambda *a: errors.append(a))
    poll = rebind(notifications._notification_poller_scoped_loop, g)
    clock = SimpleNamespace(time=lambda: 1000.0)
    with patch("hermes_cli.heartbeat.time", clock):
        assert native_dispatch(desktop.agent, "set", interval="1m", prompt="Loop discovered")["ok"]
        clock.time = lambda: 1061.0
        desktop.record["running"] = False
        thread = threading.Thread(target=poll, args=(stop, desktop.sid, desktop.record))
        thread.start()
        try:
            wait_for(stop.is_set)  # submit callback completed its clear before teardown
        finally:
            stop.set()
            thread.join(timeout=2)
        assert not thread.is_alive() and not errors
        assert HeartbeatManager(desktop.agent.session_id).state is None
    print("DESKTOP LOOP: actual scoped notification poller -> fresh DB tool state -> native claim -> submit/self-clear PASS (unrelated notification sinks mocked)")


def test_native_two_ui_pollers_race_is_not_a_plugin_atomicity_guarantee(env, desktop):
    """Document inherited parent/compute-child race, never assert exactly-once."""
    from hermes_cli import heartbeat
    clock = SimpleNamespace(time=lambda: 1000.0)
    barrier = threading.Barrier(2)
    real_load = heartbeat.load_heartbeat
    submitted, errors = [], []
    def synchronized_load(sid):
        state = real_load(sid)
        barrier.wait(timeout=2)  # timing only: both REAL loads precede either save
        return state
    def submit(rid, sid, record, prompt):
        submitted.append((sid, prompt))
        record["running"] = False
        return True
    sibling = {**desktop.record, "history_lock": threading.Lock(), "running": False}
    desktop.record["running"] = False
    fire = native_ui_driver(env, desktop, submit)
    with patch("hermes_cli.heartbeat.time", clock):
        HeartbeatManager(desktop.agent.session_id).set("Native race", 60)
        clock.time = lambda: 1061.0
        def tick(sid, record):
            try: fire(sid, record)
            except BaseException as exc: errors.append(exc)
        with patch("hermes_cli.heartbeat.load_heartbeat", synchronized_load):
            threads = [threading.Thread(target=tick, args=(str(i), r))
                       for i, r in enumerate([desktop.record, sibling])]
            for th in threads: th.start()
            for th in threads: th.join(timeout=3)
            assert not any(th.is_alive() for th in threads) and not errors
        assert len(submitted) == 2
        assert real_load(desktop.agent.session_id).fire_count == 1
    print("NATIVE LIMIT REPRODUCED: two idle UI/compute-owner shells, synchronized real DB loads -> 2 submit admissions for one due tick (persisted fire_count=1); host exactly-once NOT guaranteed; no core fix/monkeypatch applied")

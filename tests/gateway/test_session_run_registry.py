"""SessionRunSync publishes heartbeats and honors foreign cancel requests (behaviour contract)."""
import os
import time
from types import SimpleNamespace

import pytest

from gateway.session_run_registry import SessionRunSync, _mutate_registry, _read_registry, _registry_file


def _fake_runner(agents):
    """agents: session_key -> state.db session id, mirroring the gateway's in-flight turns."""
    states = {key: SimpleNamespace(turn=SimpleNamespace(
        ctx=SimpleNamespace(session_id=sid, session_key=key))) for key, sid in agents.items()}
    runner = SimpleNamespace(
        _agents=dict(agents), _states=states, interrupted=[])
    runner._snapshot_running_agents = lambda: dict(runner._agents)
    runner._peek_session_state = lambda key: runner._states.get(key)

    def _interrupt(session_key, **_kw):
        runner.interrupted.append(session_key)
        del runner._agents[session_key]  # the turn ends; the next tick drops the entry
    runner._interrupt_running_turn = _interrupt
    return runner


@pytest.fixture()
def registry_path(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.session_run_registry._registry_file", lambda: tmp_path / "state" / "session-runs.json")
    return tmp_path / "state" / "session-runs.json"


def test_heartbeat_and_clear_on_end(registry_path):
    runner = _fake_runner({"agent:main:local:dm:a": "local-aaa"})
    sync = SessionRunSync(runner)
    sync._tick()
    entry = _read_registry()["local-aaa"]
    assert entry["origin"] == "desktop" and entry["pid"] == os.getpid()
    assert time.time() * 1000 - entry["heartbeat_at"] < 5_000

    runner._agents.clear()
    sync._tick()
    assert "local-aaa" not in _read_registry()


def test_honors_foreign_cancel_request(registry_path):
    runner = _fake_runner({"agent:main:local:dm:a": "local-aaa"})
    sync = SessionRunSync(runner)
    sync._tick()
    # Another client (web) stamps a cooperative cancel on our fresh entry.
    now = time.time() * 1000
    _mutate_registry(lambda registry: registry["local-aaa"].update({"cancel_requested_at": now}))
    sync._tick()
    assert runner.interrupted == ["agent:main:local:dm:a"]
    entry = _read_registry().get("local-aaa")
    if entry is not None:  # acknowledged in place; dropped by the next tick after the turn ended
        assert entry.get("cancel_acknowledged_at")
    sync._tick()
    assert "local-aaa" not in _read_registry()


def test_does_not_touch_foreign_entries(registry_path):
    now = time.time() * 1000
    _mutate_registry(lambda registry: registry.update({"local-other": {
        "origin": "studio", "pid": 999_999, "started_at": 1, "heartbeat_at": now,
        "cancel_requested_at": now}}))
    runner = _fake_runner({"agent:main:local:dm:b": "local-bbb"})
    SessionRunSync(runner)._tick()
    registry = _read_registry()
    assert registry["local-other"]["origin"] == "studio"
    assert not registry["local-other"].get("cancel_acknowledged_at")


def test_cancel_run_id_guard_skips_stale_request(registry_path):
    runner = _fake_runner({"agent:main:local:dm:a": "local-aaa"})
    sync = SessionRunSync(runner)
    sync._tick()
    # A cancel aimed at the PREVIOUS run survives on the entry (run boundary between ticks),
    # but the new run published a different run_id: the guard must skip it.
    _mutate_registry(lambda registry: registry["local-aaa"].update({
        "cancel_requested_at": time.time() * 1000,
        "cancel_run_id": "agent:old-run", "run_id": "agent:main:local:dm:a"}))
    sync._tick()
    assert runner.interrupted == []
    # Legacy entries without cancel_run_id (older web build) are honored as-is.
    _mutate_registry(lambda registry: registry["local-aaa"].pop("cancel_run_id"))
    sync._tick()
    assert runner.interrupted == ["agent:main:local:dm:a"]


def test_agent_change_publishes_fresh_identity(registry_path):
    runner = _fake_runner({"agent:main:local:dm:a": "local-aaa"})
    sync = SessionRunSync(runner)
    sync._tick()
    run_id_1 = _read_registry()["local-aaa"]["run_id"]
    # Turn 1 ends and turn 2 starts within one tick window: same key, NEW agent object.
    import types
    runner._agents["agent:main:local:dm:a"] = object()  # replaces the tracked agent
    sync._tick()
    entry = _read_registry()["local-aaa"]
    assert entry["run_id"] != run_id_1
    assert not entry.get("cancel_requested_at") and not entry.get("cancel_run_id")


"""Cross-process session run registry — the gateway's side of the desktop<->web run sync.

Web (studio) and the desktop gateway run sessions in separate processes; the only
shared medium is the Hermes root directory. This module publishes one heartbeat
entry per in-flight gateway turn into ``<hermes root>/state/session-runs.json``
and honors cooperative cancel requests stamped on those entries by the other
side. Nobody kills another process's run: the owner interrupts its own turn.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

SYNC_INTERVAL_S = 2.5
ENTRY_FRESH_MS = 15_000
WRITE_RETRIES = 3

_active_sync: SessionRunSync | None = None
_active_sync_lock = threading.Lock()


def set_active_run_sync(sync: SessionRunSync) -> None:
    global _active_sync
    with _active_sync_lock:
        _active_sync = sync


def stop_active_run_sync() -> None:
    global _active_sync
    with _active_sync_lock:
        sync = _active_sync
        _active_sync = None
    if sync is not None:
        sync.stop()


def _registry_file() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root() / "state" / "session-runs.json"


def _read_registry() -> dict[str, Any]:
    try:
        parsed = json.loads(_registry_file().read_text(encoding="utf-8-sig"))
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_registry(registry: dict[str, Any]) -> None:
    path = _registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(registry), encoding="utf-8")
    os.replace(tmp, path)


def _mutate_registry(mutator: Callable[[dict[str, Any]], Any]) -> Any:
    for _attempt in range(WRITE_RETRIES):
        registry = _read_registry()
        result = mutator(registry)
        try:
            _write_registry(registry)
            return result
        except OSError:
            continue  # lost the rename race; retry with a fresh read
    return None


class SessionRunSync:
    """Publishes heartbeats for this gateway's running turns and honors foreign cancel requests."""

    def __init__(self, runner: Any) -> None:
        self._runner = runner
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: dict[str, float] = {}
        self._agent_ids: dict[str, int] = {}

    def start(self) -> None:
        if self._thread is not None:
            return
        from agent.memory_provider import spawn_context_thread
        # Context-bound so the sync thread keeps the launching profile's scope (HX012).
        self._thread = spawn_context_thread(self._loop, name="session-run-sync")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(SYNC_INTERVAL_S):
            try:
                self._tick()
            except Exception:
                logger.debug("session-run-sync tick failed", exc_info=True)

    def _tick(self) -> None:
        runner = self._runner
        now = time.time() * 1000
        # Snapshot: session_key -> state.db session id (fall back to the key itself when a turn
        # has not minted its id yet, e.g. before the tracking task promoted the agent).
        snapshot: dict[str, str] = {}
        agent_ids: dict[str, int] = {}
        try:
            for session_key, agent in runner._snapshot_running_agents().items():
                state = runner._peek_session_state(session_key)
                ctx = getattr(getattr(state, "turn", None), "ctx", None) if state else None
                registry_key = str(getattr(ctx, "session_id", "") or "") or session_key
                snapshot[registry_key] = session_key
                agent_ids[registry_key] = id(agent)
        except Exception:
            logger.debug("session-run-sync snapshot failed", exc_info=True)

        def publish(registry: dict[str, Any]) -> None:
            # Drop our own dead entries and stamp heartbeats for live ones.
            for key in [k for k, v in registry.items()
                        if v.get("origin") == "desktop" and v.get("pid") == os.getpid() and k not in snapshot]:
                del registry[key]
            for key, session_key in snapshot.items():
                entry = registry.get(key)
                # Fresh tracking OR a different in-flight agent object = a NEW run: publish a
                # clean identity so a cancel request aimed at the previous run can never kill
                # this one (run_id changes, and the consumer's cancel_run_id guard skips it).
                same_run = (key in self._started_at and entry
                            and entry.get("origin") == "desktop" and entry.get("pid") == os.getpid()
                            and self._agent_ids.get(key) == agent_ids[key])
                if same_run:
                    entry["heartbeat_at"] = now
                else:
                    registry[key] = {
                        "origin": "desktop", "pid": os.getpid(),
                        # session_key alone repeats across runs; fold in the agent object id
                        # so each in-flight turn has its own identity for cancel targeting.
                        "run_id": f"{session_key}#{agent_ids[key] % 1_000_000_000}",
                        "started_at": now, "heartbeat_at": now,
                    }
                self._started_at[key] = registry[key]["started_at"]
                self._agent_ids[key] = agent_ids[key]
            for key in list(self._started_at):
                if key not in snapshot:
                    del self._started_at[key]
                    self._agent_ids.pop(key, None)

        _mutate_registry(publish)

        # Cooperative cancel: honor requests stamped on OUR entries, using our own interrupt path.
        registry = _read_registry()
        for key, session_key in snapshot.items():
            entry = registry.get(key)
            if not entry or entry.get("pid") != os.getpid():
                continue
            requested = entry.get("cancel_requested_at")
            if not requested or entry.get("cancel_acknowledged_at"):
                continue
            if now - float(entry.get("heartbeat_at") or 0) > ENTRY_FRESH_MS:
                continue
            # Run-targeting guard: a cancel stamped against a PREVIOUS run (its cancel_run_id
            # no longer matches ours) must not kill this one. Entries without cancel_run_id
            # predate run targeting (older web build) and are honored as-is.
            cancel_run_id = entry.get("cancel_run_id")
            if cancel_run_id and entry.get("run_id") and cancel_run_id != entry.get("run_id"):
                continue
            logger.info("Honoring cross-client cancel request for session %s", key)
            try:
                runner._interrupt_running_turn(
                    session_key, interrupt_reason="cancelled_from_another_client",
                    invalidation_reason="cancelled_from_another_client")
            except Exception:
                logger.warning("Cross-client cancel failed for %s", key, exc_info=True)

            def ack(reg: dict[str, Any]) -> None:
                own = reg.get(key)
                if own and own.get("pid") == os.getpid() and own.get("cancel_requested_at") == requested:
                    own["cancel_acknowledged_at"] = time.time() * 1000

            _mutate_registry(ack)

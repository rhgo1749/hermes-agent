"""Regression tests for stale/replayed background-process notifications.

The UI poller may receive an event after a long foreground turn, or receive a replay of an
already-admitted process completion. Such events must not wake the same conversation again.
"""

from __future__ import annotations

import threading

from tools.process_registry import ProcessRegistry, ProcessSession
from tui_gateway import server


def notification_rig(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(server, "_background_notifications_off", lambda _session: False)
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    turns: list[tuple[str, dict]] = []

    def submit(_rid, _sid, session, text, **kwargs):
        turns.append((text, kwargs))
        with session["history_lock"]:
            session["running"] = False
        return True

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    session = {"history_lock": threading.RLock(), "running": False, "history": []}
    registry = ProcessRegistry()
    return session, registry, turns


def test_tui_drops_queued_heartbeat_after_process_exit(monkeypatch, tmp_path):
    """A still-running heartbeat queued during work is stale once its process has exited."""
    session, registry, turns = notification_rig(monkeypatch, tmp_path)
    process = ProcessSession(
        id="proc_finished",
        command="build-and-verify",
        task_id="session-task",
        started_at=10.0,
        exited=True,
        exit_code=0,
        output_buffer="verified\n",
    )
    process._completion_event.set()
    registry._finished[process.id] = process
    event = {
        "type": "heartbeat",
        "session_id": process.id,
        "session_key": "agent:main:desktop:session",
        "started_at": process.started_at,
        "seq": 1,
        "interval": 60,
        "elapsed": 60,
        "command": process.command,
        "output": "verified\n",
    }

    server.__dict__["_notif_handle_ready"](
        "sid", session, [event], set(), registry, lambda _event: "heartbeat text", None, owned=True,
    )

    assert turns == []
    assert session["running"] is False


def test_tui_suppresses_replayed_process_completion_wake(monkeypatch, tmp_path):
    """The same process-session completion must not start a second model turn."""
    session, registry, turns = notification_rig(monkeypatch, tmp_path)
    process = ProcessSession(
        id="proc_duplicate",
        command="build-and-verify",
        task_id="session-task",
        started_at=20.0,
        exited=True,
        exit_code=0,
        output_buffer="verified\n",
    )
    process._completion_event.set()
    registry._finished[process.id] = process
    event = {
        "type": "completion",
        "session_id": process.id,
        "session_key": "agent:main:desktop:session",
        "started_at": process.started_at,
        "command": process.command,
        "exit_code": 0,
        "completion_reason": "exited",
        "output": "verified\n",
    }
    emitted: set = set()

    for _ in range(2):
        server.__dict__["_notif_handle_ready"](
            "sid", session, [dict(event)], emitted, registry, lambda _event: "completion text", None, owned=True,
        )

    assert len(turns) == 1
    new_epoch = {**event, "started_at": 21.0}
    server.__dict__["_notif_handle_ready"](
        "sid", session, [new_epoch], emitted, registry, lambda _event: "completion text", None, owned=True,
    )
    assert len(turns) == 2


def test_shared_heartbeat_liveness_fence_rejects_finished_process():
    """Every official consumer uses one process-incarnation check for queued heartbeats."""
    registry = ProcessRegistry()
    process = ProcessSession(
        id="proc_finished_drain",
        command="build-and-verify",
        task_id="session-task",
        started_at=30.0,
        exited=True,
        exit_code=0,
        output_buffer="verified\n",
    )
    process._completion_event.set()
    registry._finished[process.id] = process
    event = {
        "type": "heartbeat",
        "session_id": process.id,
        "session_key": "agent:main:desktop:session",
        "started_at": process.started_at,
        "seq": 1,
        "command": process.command,
        "output": "verified\n",
    }
    from tools.process_registry_notifications import heartbeat_process_is_current

    assert not heartbeat_process_is_current(event, registry)


def test_heartbeat_liveness_fence_distinguishes_process_incarnations():
    from tools.process_registry_notifications import heartbeat_process_is_current

    registry = ProcessRegistry()
    process = ProcessSession(
        id="proc_reused",
        command="next-run",
        task_id="session-task",
        started_at=42.0,
        heartbeat_seconds=60,
    )
    registry._running[process.id] = process

    old_event = {"type": "heartbeat", "session_id": process.id, "started_at": 41.0, "seq": 1}
    current_event = {"type": "heartbeat", "session_id": process.id, "started_at": 42.0, "seq": 1}

    assert not heartbeat_process_is_current(old_event, registry)
    assert heartbeat_process_is_current(current_event, registry)
    assert not heartbeat_process_is_current({**current_event, "started_at": None}, registry)
    assert not heartbeat_process_is_current({**current_event, "seq": None}, registry)


def test_distinct_heartbeat_sequences_and_identical_watch_matches_remain_deliverable(monkeypatch, tmp_path):
    session, registry, turns = notification_rig(monkeypatch, tmp_path)
    process = ProcessSession(
        id="proc_live_followups",
        command="build-and-verify",
        task_id="session-task",
        started_at=43.0,
        heartbeat_seconds=60,
    )
    registry._running[process.id] = process
    emitted: set = set()

    for seq in (2, 1, 2):
        event = {
            "type": "heartbeat", "session_id": process.id, "started_at": process.started_at,
            "seq": seq, "command": process.command, "output": f"line-{seq}\n",
        }
        server.__dict__["_notif_handle_ready"](
            "sid", session, [event], emitted, registry, lambda _event: "heartbeat text", None, owned=True,
        )

    watch_match = {
        "type": "watch_match", "session_id": process.id, "command": process.command,
        "pattern": "READY", "output": "READY\n", "suppressed": 0,
    }
    for _ in range(2):
        server.__dict__["_notif_handle_ready"](
            "sid", session, [dict(watch_match)], emitted, registry, lambda _event: "watch text", None, owned=True,
        )

    assert len(turns) == 4


def test_completion_beats_stale_heartbeat_in_either_queue_order(monkeypatch, tmp_path):
    for event_types in (("heartbeat", "completion"), ("completion", "heartbeat")):
        session, registry, turns = notification_rig(monkeypatch, tmp_path)
        process = ProcessSession(
            id=f"proc_race_{event_types[0]}",
            command="build-and-verify",
            task_id="session-task",
            started_at=44.0,
            exited=True,
            exit_code=0,
            output_buffer="verified\n",
        )
        process._completion_event.set()
        registry._finished[process.id] = process
        heartbeat = {
            "type": "heartbeat", "session_id": process.id, "started_at": process.started_at,
            "seq": 1, "command": process.command, "output": "verified\n",
        }
        completion = {
            "type": "completion", "session_id": process.id, "started_at": process.started_at,
            "command": process.command, "exit_code": 0, "completion_reason": "exited", "output": "verified\n",
        }
        events = {"heartbeat": heartbeat, "completion": completion}

        server.__dict__["_notif_handle_ready"](
            "sid", session, [events[event_type] for event_type in event_types], set(), registry,
            lambda event: f"{event['type']} text", None, owned=True,
        )

        assert len(turns) == 1
        assert turns[0][0] == "completion text"


def test_completion_winning_after_liveness_read_prevents_second_tui_turn(monkeypatch, tmp_path):
    """A heartbeat that loses turn admission to completion is rechecked and dropped on replay."""
    session, registry, turns = notification_rig(monkeypatch, tmp_path)
    process = ProcessSession(
        id="proc_tui_admission_race",
        command="build-and-verify",
        task_id="session-task",
        started_at=45.0,
        heartbeat_seconds=60,
    )
    registry._running[process.id] = process
    heartbeat = {
        "type": "heartbeat", "session_id": process.id, "started_at": process.started_at,
        "session_key": "agent:main:desktop:session", "seq": 1,
        "command": process.command, "output": "still running\n",
    }
    completion = {
        "type": "completion", "session_id": process.id, "started_at": process.started_at,
        "session_key": heartbeat["session_key"], "command": process.command,
        "exit_code": 0, "completion_reason": "exited", "output": "done\n",
    }

    from tools.process_registry_notifications import heartbeat_process_is_current as real_liveness_check

    liveness_read = threading.Event()
    release_heartbeat = threading.Event()
    gated_once = threading.Event()
    errors = []

    def gated_liveness_check(evt, current_registry=None):
        current = real_liveness_check(evt, current_registry)
        if evt.get("type") == "heartbeat" and current and not gated_once.is_set():
            gated_once.set()
            liveness_read.set()
            if not release_heartbeat.wait(5):
                raise AssertionError("test did not release the heartbeat liveness barrier")
        return current

    monkeypatch.setattr(
        "tools.process_registry_notifications.heartbeat_process_is_current", gated_liveness_check,
    )

    # Model submission keeps the completion turn active until the test releases it.
    def submit(_rid, _sid, _session, text, **kwargs):
        turns.append((text, kwargs))
        return True

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    emitted = set()

    def drain_heartbeat():
        try:
            server.__dict__["_notif_handle_ready"](
                "sid", session, [heartbeat], emitted, registry, lambda _event: "heartbeat text", [], owned=True,
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=drain_heartbeat)
    thread.start()
    try:
        assert liveness_read.wait(5), "heartbeat did not reach the deterministic liveness barrier"
        with process._lock:
            process.exited = True
            process.exit_code = 0
            process.completion_reason = "exited"
        registry._running.pop(process.id)
        registry._finished[process.id] = process
        process._completion_event.set()

        server.__dict__["_notif_handle_event"](
            "sid", session, completion, set(), registry, lambda _event: "completion text", None, owned=True,
        )
        assert len(turns) == 1 and turns[0][0] == "completion text"
        assert session["running"] is True
    finally:
        release_heartbeat.set()
        thread.join(5)

    assert not thread.is_alive()
    assert errors == []
    assert turns[0][0] == "completion text"
    assert len(turns) == 1
    assert registry.completion_queue.qsize() == 1

    # The completion turn finishes; the requeued heartbeat is now checked against exited state.
    with session["history_lock"]:
        session["running"] = False
    requeued = registry.completion_queue.get_nowait()
    server.__dict__["_notif_handle_ready"](
        "sid", session, [requeued], emitted, registry, lambda _event: "heartbeat text", [], owned=True,
    )

    assert turns == [("completion text", turns[0][1])]
    assert registry.completion_queue.empty()

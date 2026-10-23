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

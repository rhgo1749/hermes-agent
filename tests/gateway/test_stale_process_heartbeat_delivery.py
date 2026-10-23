"""Gateway-side regression for heartbeats queued before process completion."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock

import tools.process_registry as process_registry_module
from gateway.run import GatewayRunner
from tools.process_registry import ProcessRegistry, ProcessSession


def test_gateway_drops_queued_heartbeat_after_process_exit(monkeypatch):
    registry = ProcessRegistry()
    process = ProcessSession(
        id="proc_gateway_finished",
        command="build-and-verify",
        task_id="session-task",
        started_at=50.0,
        exited=True,
        exit_code=0,
        output_buffer="verified\n",
    )
    process._completion_event.set()
    registry._finished[process.id] = process
    event = {
        "type": "heartbeat",
        "session_id": process.id,
        "session_key": "agent:main:telegram:dm:123",
        "started_at": process.started_at,
        "seq": 1,
        "interval": 60,
        "elapsed": 60,
        "command": process.command,
        "output": "verified\n",
    }
    registry.completion_queue.put(event)
    monkeypatch.setattr(process_registry_module, "process_registry", registry)

    runner = object.__new__(GatewayRunner)
    runner._completion_event_scope = lambda evt: contextlib.nullcontext()
    runner._load_background_notifications_mode = lambda: "concise"
    runner._inject_watch_notification = AsyncMock(return_value=True)

    asyncio.run(runner._drain_watch_notifications(registry.completion_queue))

    runner._inject_watch_notification.assert_not_awaited()
    assert registry.completion_queue.empty()

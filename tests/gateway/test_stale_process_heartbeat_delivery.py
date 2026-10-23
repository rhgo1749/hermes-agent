"""Gateway-side regression for heartbeats queued before process completion."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections import OrderedDict
from types import SimpleNamespace
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


def test_gateway_does_not_admit_heartbeat_after_completion_wins_the_admission_race(monkeypatch):
    """A completion that wins during async route resolution fences the stale heartbeat admission."""
    registry = ProcessRegistry()
    process = ProcessSession(
        id="proc_gateway_admission_race",
        command="build-and-verify",
        task_id="session-task",
        started_at=51.0,
        heartbeat_seconds=60,
    )
    registry._running[process.id] = process
    heartbeat = {
        "type": "heartbeat",
        "session_id": process.id,
        "session_key": "agent:main:telegram:dm:123",
        "started_at": process.started_at,
        "seq": 1,
        "interval": 60,
        "elapsed": 60,
        "command": process.command,
        "output": "still running\n",
    }
    completion = {
        "type": "completion",
        "session_id": process.id,
        "session_key": heartbeat["session_key"],
        "started_at": process.started_at,
        "command": process.command,
        "exit_code": 0,
        "completion_reason": "exited",
        "output": "done\n",
    }
    registry.completion_queue.put(heartbeat)
    monkeypatch.setattr(process_registry_module, "process_registry", registry)

    runner = object.__new__(GatewayRunner)
    runner._completion_event_scope = lambda evt: contextlib.nullcontext()
    runner._load_background_notifications_mode = lambda: "concise"
    runner._completion_delivery_lock = threading.Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 16
    runner._settle_durable_claim = lambda kind, delegation_id, claim_id: None

    from gateway.config import Platform
    from gateway.session import SessionSource
    import gateway.wake as wake_module

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="42")
    heartbeat_source_started = threading.Event()
    release_heartbeat_source = threading.Event()
    completion_preflight_started = asyncio.Event()
    admitted: list[str] = []

    def build_source(evt):
        if evt.get("type") == "heartbeat":
            heartbeat_source_started.set()
            if not release_heartbeat_source.wait(5):
                raise AssertionError("test did not release the heartbeat route-resolution barrier")
        return source

    async def admit(_adapter, event):
        admitted.append(event.text)
        event._gateway_accepted = True

    async def completion_preflight(evt: dict):
        completion_preflight_started.set()
        return runner._CompletionClaim()

    runner._build_process_event_source = build_source
    runner._resolve_injection_adapter = lambda platform_name, source=None: SimpleNamespace()
    runner._preflight_completion_delivery = completion_preflight
    monkeypatch.setattr(wake_module, "adapter_supports_push", lambda _adapter: True)
    monkeypatch.setattr(wake_module, "admit_internal_event", admit)

    async def exercise():
        heartbeat_drain = asyncio.create_task(
            runner._drain_watch_notifications(registry.completion_queue))
        try:
            # Liveness passed; route resolution now yields before adapter admission.
            reached = await asyncio.to_thread(heartbeat_source_started.wait, 5)
            assert reached, "heartbeat did not reach the route-resolution barrier"
            with process._lock:
                process.exited = True
                process.exit_code = 0
                process.completion_reason = "exited"
            registry._running.pop(process.id)
            registry._finished[process.id] = process
            process._completion_event.set()

            completion_task = asyncio.create_task(
                runner._deliver_completion_notification("completion", completion))
            await asyncio.wait_for(completion_preflight_started.wait(), timeout=5)
            release_heartbeat_source.set()
            assert await asyncio.wait_for(completion_task, timeout=5) is True
            await asyncio.wait_for(heartbeat_drain, timeout=5)
        finally:
            release_heartbeat_source.set()

    asyncio.run(exercise())

    assert len(admitted) == 1
    assert "completion" in admitted[0]
    assert "heartbeat #" not in admitted[0]


def test_gateway_serializes_inflight_heartbeat_before_completion(monkeypatch):
    """Once heartbeat adapter admission starts, completion cannot overtake it."""
    registry = ProcessRegistry()
    process = ProcessSession(
        id="proc_gateway_inflight_admission",
        command="build-and-verify",
        task_id="session-task",
        started_at=52.0,
        heartbeat_seconds=60,
    )
    registry._running[process.id] = process
    heartbeat = {
        "type": "heartbeat", "session_id": process.id, "started_at": process.started_at,
        "session_key": "agent:main:telegram:dm:124", "seq": 1,
        "command": process.command, "output": "new output\n",
    }
    completion = {
        "type": "completion", "session_id": process.id, "started_at": process.started_at,
        "session_key": heartbeat["session_key"], "command": process.command,
        "exit_code": 0, "completion_reason": "exited", "output": "final output\n",
    }
    registry.completion_queue.put(heartbeat)
    monkeypatch.setattr(process_registry_module, "process_registry", registry)

    runner = object.__new__(GatewayRunner)
    runner._completion_event_scope = lambda evt: contextlib.nullcontext()
    runner._load_background_notifications_mode = lambda: "concise"
    runner._completion_delivery_lock = threading.Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 16
    runner._settle_durable_claim = lambda kind, delegation_id, claim_id: None

    from gateway.config import Platform
    from gateway.session import SessionSource
    import gateway.wake as wake_module

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="124", user_id="42")
    heartbeat_at_admission = asyncio.Event()
    release_heartbeat = asyncio.Event()
    completion_preflight_started = asyncio.Event()
    admitted: list[str] = []

    def build_source(evt):
        return source

    async def admit(_adapter, event):
        if "heartbeat #" in event.text:
            heartbeat_at_admission.set()
            await release_heartbeat.wait()
        admitted.append(event.text)
        event._gateway_accepted = True

    async def completion_preflight(evt: dict):
        completion_preflight_started.set()
        return runner._CompletionClaim()

    runner._build_process_event_source = build_source
    runner._resolve_injection_adapter = lambda platform_name, source=None: SimpleNamespace()
    runner._preflight_completion_delivery = completion_preflight
    monkeypatch.setattr(wake_module, "adapter_supports_push", lambda _adapter: True)
    monkeypatch.setattr(wake_module, "admit_internal_event", admit)

    async def exercise():
        heartbeat_drain = asyncio.create_task(
            runner._drain_watch_notifications(registry.completion_queue))
        try:
            await asyncio.wait_for(heartbeat_at_admission.wait(), timeout=5)
            with process._lock:
                process.exited = True
                process.exit_code = 0
                process.completion_reason = "exited"
            registry._running.pop(process.id)
            registry._finished[process.id] = process
            process._completion_event.set()

            completion_task = asyncio.create_task(
                runner._deliver_completion_notification("completion", completion))
            await asyncio.wait_for(completion_preflight_started.wait(), timeout=5)
            release_heartbeat.set()
            assert await asyncio.wait_for(completion_task, timeout=5) is True
            await asyncio.wait_for(heartbeat_drain, timeout=5)
        finally:
            release_heartbeat.set()

    asyncio.run(exercise())

    assert len(admitted) == 2
    assert "heartbeat #" in admitted[0]
    assert "completion" in admitted[1]

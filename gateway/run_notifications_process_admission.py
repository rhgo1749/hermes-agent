"""Gateway admission ordering for background-process heartbeat/completion notifications."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, cast

logger = logging.getLogger("gateway.run")


class GatewayProcessNotificationAdmissionMixin:
    """Serialize one process incarnation's heartbeat and terminal completion admissions."""

    def _process_notification_admission_lock(self, evt: dict) -> asyncio.Lock:
        """Lock one process incarnation so a completion cannot be followed by its old heartbeat."""
        session_id = str(evt.get("session_id") or "")
        session = None
        if session_id:
            from tools.process_registry import process_registry
            session = process_registry.get(session_id)
        if session is None:
            lock = self.__dict__.get("_orphan_process_notice_admission_lock")
            if lock is None:
                lock = self.__dict__["_orphan_process_notice_admission_lock"] = asyncio.Lock()
            return cast(asyncio.Lock, lock)
        lock = session.__dict__.get("_gateway_notification_admission_lock")
        if lock is None:
            lock = session.__dict__["_gateway_notification_admission_lock"] = asyncio.Lock()
        return cast(asyncio.Lock, lock)

    async def _drain_watch_notifications(self, completion_queue) -> None:
        """Drain watch events; a heartbeat is revalidated at its serialized admission point."""
        from gateway.run import _drain_gateway_watch_events, _format_gateway_process_notification

        runner = cast(Any, self)
        watch_events = _drain_gateway_watch_events(completion_queue)
        for evt in watch_events:
            async with runner._completion_event_scope(evt):
                from tools.process_registry_notifications import (
                    heartbeat_process_is_current,
                    should_surface_notification,
                )

                if not should_surface_notification(evt) or not heartbeat_process_is_current(evt):
                    continue
                if runner._load_background_notifications_mode() == "off":
                    continue
                synth_text = _format_gateway_process_notification(evt)
                if not synth_text:
                    continue
                try:
                    delivered = await runner._admit_process_notification(synth_text, evt)
                except Exception:
                    logger.exception("Watch notification injection error")
                    delivered = False
            if delivered is False:
                completion_queue.put(evt)

    async def _admit_process_notification(
        self, synth_text: str, evt: dict, *, raise_not_accepted: bool = False,
    ) -> Optional[bool]:
        """Serialize process completions with heartbeat admission for the same process session."""
        runner = cast(Any, self)
        evt_type = str(evt.get("type") or "")
        if evt_type not in {"heartbeat", "completion"}:
            return await runner._inject_watch_notification(
                synth_text, evt, raise_not_accepted=raise_not_accepted)
        async with runner._process_notification_admission_lock(evt):
            if evt_type == "heartbeat":
                from tools.process_registry_notifications import heartbeat_process_is_current

                if not heartbeat_process_is_current(evt):
                    return None
            return await runner._inject_watch_notification(
                synth_text, evt, raise_not_accepted=raise_not_accepted)

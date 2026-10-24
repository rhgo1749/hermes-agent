"""Process-notification identity and duplicate turn suppression for the TUI poller."""

from __future__ import annotations

from typing import Any


def notification_turn_key(evt: dict[str, Any]) -> tuple[Any, ...] | None:
    """Stable process-event identity; watch matches may legitimately repeat identical output."""
    evt_type = evt.get("type", "completion")
    session_id = str(evt.get("session_id") or "")
    if not session_id:
        return None
    if evt_type == "completion":
        return evt_type, session_id, evt.get("started_at")
    if evt_type == "heartbeat" and evt.get("seq") is not None:
        return evt_type, session_id, evt.get("started_at"), evt["seq"]
    return None


def dedupe_notification_turns(
    session: dict[str, Any], notifications: list[tuple[dict[str, Any], str]],
) -> tuple[set[tuple[Any, ...]], list[tuple[dict[str, Any], str]]]:
    """Drop already-delivered process turns while retaining distinct heartbeat sequences."""
    delivered = session.setdefault("_notification_turn_delivered", set())
    seen: set[tuple[Any, ...]] = set()
    unique: list[tuple[dict[str, Any], str]] = []
    for event, text in notifications:
        key = notification_turn_key(event)
        if key is not None and (key in delivered or key in seen):
            continue
        if key is not None:
            seen.add(key)
        unique.append((event, text))
    return delivered, unique

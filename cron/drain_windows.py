"""Durable record of a PLANNED gateway drain blackout, one file per cron store.

While the gateway drains (graceful restart/shutdown request, or an external drain), the
in-process ticker's ``can_dispatch`` gate skips the due scan entirely: during the blackout
no due job fires, retires, or advances — the scan itself is frozen. A one-shot whose run
time falls inside that blackout used to be classified by the successor process's first scan
as "scheduler down past the window" and retired without ever running (incident 2026-10-13,
job ``dde61f9a3066``: due 20:08:59 inside a 19.6-minute drain, retired 20:24:51 with only a
local tombstone and no notification).

The first gated tick stamps the blackout start here; the first allowed due scan closes it,
so a window records exactly the scan-freeze interval. The due scan
(``jobs._retire_expired_oneshot``) then distinguishes a drain-deferred one-shot (window
covers the stored run time AND no dispatch receipts) from ordinary expiry, and restores the
former as due exactly once. Malformed or missing window files fail closed to the legacy
retire policy — a corrupt record never causes a fire.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Blackout history cap per store; only open/closed bookkeeping matters, not an archive.
_MAX_WINDOWS = 20

# How far BEFORE the stamp a run time may sit and still count as drain-deferred. The healthy
# ticker scans every 60s, so a run time missed by MORE than ~1.5 ticks had already been passed
# over by pre-drain scans — that lateness is scheduler-down/host-sleep, not the drain's doing.
# Deliberately smaller than ONESHOT_GRACE_SECONDS so this path can never be more forgiving
# than the ordinary late-fire window.
_PRE_FREEZE_WINDOW_SECONDS = 90


def _windows_file() -> Path:
    from cron.jobs import _current_cron_store
    return _current_cron_store().cron_dir / "drain_windows.json"


def _load_windows() -> List[Dict[str, Any]]:
    path = _windows_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning("cron.drain_windows: unreadable %s; ignoring (legacy retire policy applies)", path)
        return []
    if not isinstance(data, list):
        return []
    return [w for w in data if isinstance(w, dict) and isinstance(w.get("started_at"), str)]


def _save_windows(windows: List[Dict[str, Any]]) -> None:
    from utils import atomic_write_text
    path = _windows_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Owner-only like jobs.json: window records are scheduler state, not world-readable.
    atomic_write_text(path, json.dumps(windows[-_MAX_WINDOWS:]), tmp_prefix=".drain_windows_", mode=0o600)


def mark_drain_window(reason: str) -> Optional[Dict[str, Any]]:
    """Stamp an open blackout for the ACTIVE cron store, once per blackout (idempotent).

    Called from the gated tick sites, which already run inside the store's context, so every
    profile a multiplex ticker serves gets its own record. Never raises into the tick.
    """
    from cron.jobs import _hermes_now, _jobs_lock, _machine_id
    with _jobs_lock():
        windows = _load_windows()
        open_window = next((w for w in windows if not w.get("ended_at")), None)
        if open_window is not None:
            return open_window
        entry = {
            "id": uuid.uuid4().hex[:12],
            "started_at": _hermes_now().isoformat(),
            "reason": reason,
            "by": _machine_id(),
        }
        windows.append(entry)
        _save_windows(windows)
        logger.info("Cron dispatch blackout window %s stamped at %s (%s)",
                    entry["id"], entry["started_at"], reason)
        return entry


def covering_drain_window(instant: str, now: datetime) -> Optional[Dict[str, Any]]:
    """Return the blackout window that could have swallowed ``instant`` (an overdue
    one-shot's stored run time), or None.

    Coverage starts ``_PRE_FREEZE_WINDOW_SECONDS`` before the stamp (see that constant: it is
    what a healthy ticker could still have fired, NOT the full grace window) and ends at the
    close stamp, or ``now`` while open (the blackout lasts until the first allowed scan, which
    is this scan). A CLOSED window only counts within ``ONESHOT_GRACE_SECONDS`` of its own
    close: the recovery scan itself, plus the crash-before-save retry immediately after it.
    Without that recency cap a weeks-old window would keep reviving manually-edited or
    host-sleep-stale one-shots whose timestamps happen to land inside it — those must stay
    retired under the legacy policy.
    """
    from cron.jobs import ONESHOT_GRACE_SECONDS
    try:
        due = datetime.fromisoformat(instant)
    except (TypeError, ValueError):
        return None
    if due.tzinfo is None:
        return None
    for window in _load_windows():
        try:
            start = datetime.fromisoformat(window["started_at"])
        except ValueError:
            continue
        if start.tzinfo is None:
            continue
        ended = window.get("ended_at")
        try:
            end = datetime.fromisoformat(ended) if isinstance(ended, str) else now
        except ValueError:
            end = now
        if isinstance(ended, str) and now > end + timedelta(seconds=ONESHOT_GRACE_SECONDS):
            continue  # stale record; the blackout it describes is long over
        if start - timedelta(seconds=_PRE_FREEZE_WINDOW_SECONDS) <= due <= min(end, now):
            return window
    return None


def close_open_drain_windows(now: datetime) -> int:
    """Close open blackouts at the first allowed due scan; returns how many were closed.

    The close is what bounds the window: a successor that crashes mid-drain simply leaves the
    window open until some scan runs, and that scan IS the end of the blackout. A cancellation
    (drain ends without restart) closes on that process's next allowed tick — no gateway-side
    close hook to miss.
    """
    from cron.jobs import _jobs_lock
    with _jobs_lock():
        windows = _load_windows()
        closed = 0
        for window in windows:
            if not window.get("ended_at"):
                window["ended_at"] = now.isoformat()
                closed += 1
        if closed:
            _save_windows(windows)
            logger.info("Cron dispatch blackout window(s) closed at %s: %d", now.isoformat(), closed)
        return closed

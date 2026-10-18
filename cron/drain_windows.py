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
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Blackout history cap per store; only open/closed bookkeeping matters, not an archive.
_MAX_WINDOWS = 20

# How far BEFORE the stamp a run time may sit and still count as drain-deferred. The healthy
# ticker scans every 60s, so a run time missed by MORE than ~1.5 ticks had already been passed
# over by pre-drain scans — that lateness is scheduler-down/host-sleep, not the drain's doing.
# Deliberately smaller than ONESHOT_GRACE_SECONDS so this path can never be more forgiving
# than the ordinary late-fire window.
_PRE_FREEZE_WINDOW_SECONDS = 90

# How long an OPEN blackout may be trusted at all. A real drain lasts as long as the operator's
# drain timeout plus process restart time (19.6 minutes on 2026-10-13); a window still open
# after 4 hours has never been seen by any allowed scan, i.e. the store was simply offline on
# its own — expiries there belong to the legacy grace policy, not to a recovery from a
# blackout no process is running any more. Refusals get an explicit diagnostic in
# ``jobs._retire_expired_oneshot``; ``close_open_drain_windows`` also marks such a window
# ``stale`` so it stays untrusted after the close.
_OPEN_WINDOW_MAX_AGE_SECONDS = 4 * 3600


def _windows_file() -> Path:
    from cron.jobs import _current_cron_store
    return _current_cron_store().cron_dir / "drain_windows.json"


def _load_windows() -> list[dict[str, Any]]:
    path = _windows_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning("cron.drain_windows: unreadable %s; ignoring (legacy retire policy applies)", path)
        return []
    if not isinstance(data, list):
        return []
    return [w for w in data if isinstance(w, dict) and isinstance(w.get("started_at"), str)]


def _save_windows(windows: list[dict[str, Any]]) -> None:
    from utils import atomic_write_text
    path = _windows_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Owner-only like jobs.json: window records are scheduler state, not world-readable.
    atomic_write_text(path, json.dumps(windows[-_MAX_WINDOWS:]), tmp_prefix=".drain_windows_", mode=0o600)


def mark_drain_window(reason: str) -> Optional[dict[str, Any]]:
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


def covering_drain_window(instant: str, now: datetime) -> Optional[dict[str, Any]]:
    """Return the blackout window that could have swallowed ``instant`` (an overdue
    one-shot's stored run time) AND is still trusted, or None.

    Coverage starts ``_PRE_FREEZE_WINDOW_SECONDS`` before the stamp (see that constant: it is
    what a healthy ticker could still have fired, NOT the full grace window) and ends at the
    close stamp, or ``now`` while open. A CLOSED window only counts within
    ``ONESHOT_GRACE_SECONDS`` of its own close (the recovery scan itself plus the
    crash-before-save retry still count); an OPEN window only counts while its age is under
    ``_OPEN_WINDOW_MAX_AGE_SECONDS``. Without those caps a weeks-old or abandoned window would
    keep reviving manually-edited, host-sleep-stale, or long-dead one-shots — those must stay
    retired under the legacy policy.
    """
    return _matching_drain_windows(instant, now)[0]


def stale_covering_drain_window(instant: str, now: datetime) -> Optional[dict[str, Any]]:
    """A window that WOULD cover ``instant`` but matches only after failing its own
    staleness cap. The missed-oneshot retire calls this to make an abandoned-blackout refusal
    explicit (log + diagnostic note) instead of leaving it as plain silent expiry."""
    return _matching_drain_windows(instant, now)[1]


def _matching_drain_windows(instant: str, now: datetime):
    """Return ``(fresh, stale)`` windows matching ``instant``, each or None."""
    from cron.jobs import ONESHOT_GRACE_SECONDS
    try:
        due = datetime.fromisoformat(instant)
    except (TypeError, ValueError):
        return None, None
    if due.tzinfo is None:
        return None, None
    fresh = stale = None
    for window in _load_windows():
        bounds = _window_bounds(window, now)
        if bounds is None:
            continue
        start, end, ended_raw = bounds
        if not (start - timedelta(seconds=_PRE_FREEZE_WINDOW_SECONDS) <= due <= min(end, now)):
            continue
        too_old = bool(window.get("stale"))
        if ended_raw is None:
            too_old = too_old or (now - start).total_seconds() > _OPEN_WINDOW_MAX_AGE_SECONDS
        else:
            too_old = too_old or (now > end + timedelta(seconds=ONESHOT_GRACE_SECONDS))
        if too_old:
            stale = stale or window
        else:
            fresh = fresh or window
    return fresh, stale


def _window_bounds(window: dict[str, Any], now: datetime):
    """Parse a record to ``(start, end, ended_raw)``; None when malformed. An open window's
    ``end`` is ``now`` (the blackout lasts until the first allowed scan, which is the scan
    asking)."""
    try:
        start = datetime.fromisoformat(window["started_at"])
    except (ValueError, TypeError):
        return None
    if start.tzinfo is None:
        return None
    ended = window.get("ended_at")
    try:
        end = datetime.fromisoformat(ended) if isinstance(ended, str) else now
    except ValueError:
        end = now
    return start, end, (ended if isinstance(ended, str) else None)


def mark_drain_windows_for_drain(reason: str, profile_homes: Optional[list[Any]] = None) -> int:
    """Stamp the blackout for every store the ticker serves AT THE MOMENT a drain is requested,
    not at the first gated cron tick.

    A process that dies before its next cron tick never reaches the gated tick's stamp, and a
    one-shot due inside that unrecorded gap falls back to silent legacy expiry (the
    2026-10-13 incident shape). Stamping at drain start closes that gap. ``profile_homes``
    mirrors the gateway's multiplex ticker list (``(name, home)`` pairs); None or empty stamps
    this process' active store only. Idempotent per store (the gated tick's own stamp then
    no-ops), and never raises into the shutdown path. Returns how many stores were stamped.
    """
    from cron.jobs import use_cron_store
    entries: list[Any] = list(profile_homes) if profile_homes else [None]
    marked = 0
    for entry in entries:
        if isinstance(entry, (tuple, list)) and len(entry) == 2:
            raw = entry[1]
        elif entry is None or isinstance(entry, (str, Path)):
            raw = entry
        else:
            continue
        home = None if raw is None else str(raw)
        try:
            if home is None:
                window = mark_drain_window(reason)
            else:
                with use_cron_store(home):
                    window = mark_drain_window(reason)
            if window is not None:
                marked += 1
        except Exception:
            logger.debug("cron.drain_windows: drain-start stamp failed for %s", home,
                         exc_info=True)
    return marked


def close_open_drain_windows(now: datetime) -> int:
    """Close open blackouts at the first allowed due scan; returns how many were closed.

    The close is what bounds the window: a successor that crashes mid-drain simply leaves the
    window open until some scan runs, and that scan IS the end of the blackout. A cancellation
    (drain ends without restart) closes on that process's next allowed tick — no gateway-side
    close hook to miss. A window that was still open past the trust cap is additionally marked
    ``stale`` so it cannot revive jobs even inside the post-close recency grace (e.g. a job
    whose run time is edited backwards into the abandoned window right after the close).
    """
    from cron.jobs import _jobs_lock
    with _jobs_lock():
        windows = _load_windows()
        closed = 0
        for window in windows:
            if not window.get("ended_at"):
                bounds = _window_bounds(window, now)
                if bounds is not None and (now - bounds[0]).total_seconds() > _OPEN_WINDOW_MAX_AGE_SECONDS:
                    window["stale"] = True
                window["ended_at"] = now.isoformat()
                closed += 1
        if closed:
            _save_windows(windows)
            logger.info("Cron dispatch blackout window(s) closed at %s: %d", now.isoformat(), closed)
        return closed

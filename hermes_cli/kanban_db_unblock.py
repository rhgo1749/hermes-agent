"""Unblock mutations with an optional exact capability-block event CAS."""
from __future__ import annotations

import sqlite3
import time
from typing import Any


def _matches_current_capability_block(
    conn: sqlite3.Connection,
    task_id: str,
    expected_block_event_id: int,
    kb: Any,
) -> bool:
    """Check the exact current capability hold before any unblock side effect.

    The caller holds ``write_txn``; a concurrent block cannot land between this
    check and the status transition.
    """
    task = conn.execute(
        "SELECT status, block_kind FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if task is None or task["status"] != "blocked" or task["block_kind"] != "capability":
        return False

    event = conn.execute(
        "SELECT id, payload FROM task_events "
        "WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if event is None or event["id"] != expected_block_event_id:
        return False
    return kb._json_dict(event["payload"]).get("kind") == "capability"


def unblock_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    expected_block_event_id: int | None = None,
) -> bool:
    """Resume a blocked/scheduled task, optionally bound to one capability hold.

    With ``expected_block_event_id``, the task must still be blocked by that
    exact latest capability event. A stale or stronger hold returns ``False``
    with no task, run, or event changes. The check and legacy unblock body share
    one IMMEDIATE transaction. Omitting the precondition preserves the existing
    operator-unblock behavior.
    """
    from hermes_cli import kanban_db as kb

    if expected_block_event_id is not None and (
        type(expected_block_event_id) is not int or expected_block_event_id <= 0
    ):
        raise ValueError("expected_block_event_id must be a positive integer")

    now = int(time.time())
    with kb.write_txn(conn):
        if expected_block_event_id is not None and not _matches_current_capability_block(
            conn, task_id, expected_block_event_id, kb,
        ):
            return False

        resume_status = (
            kb._resume_status_from_events(conn, task_id)
            if kb._task_status(conn, task_id) == "blocked"
            else "ready"
        )
        kb._reclaim_dangling_run(
            conn, task_id, statuses=("blocked", "scheduled"), now=now,
            note="invariant recovery on unblock",
        )
        landing_status = kb._landing_status_after_parents(conn, task_id)
        new_status = (
            "review"
            if landing_status == "ready" and resume_status == "review"
            else landing_status
        )
        # Keep block recurrence memory; only successful completion clears it.
        # A deliberate unblock starts a fresh dispatch-failure budget.
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "consecutive_failures = 0, last_failure_error = NULL "
            "WHERE id = ? AND status IN ('blocked', 'scheduled')",
            (new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        kb._append_event(
            conn, task_id, "unblocked",
            (
                {"status": new_status, "resume_status": resume_status}
                if new_status != "ready" or resume_status != "ready"
                else None
            ),
        )
        return True

"""Narrow operator CAS for a specific block-loop triage incident.

This is intentionally separate from ``unblock_task``: a triage row is not a
blocked row, and recovery must bind the exact block-loop event, ended run,
board, current parent set, and expected landing status atomically. H4V3 keeps
this as a temporary fork carry: submit the equivalent narrow CAS upstream,
and remove this implementation once upstream exposes the same contract.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Sequence

# Only known non-state observations may follow the block-loop event before the
# operator's CAS. Unknown event kinds fail closed until their semantics are
# reviewed. Worker activity is deliberately not in this allowlist.
_NON_STATE_EVENTS = frozenset({
    "commented", "assigned", "edited", "reprioritized", "linked", "unlinked",
    "parent_linked", "parent_unlinked", "attached", "attachment_removed",
    "model_override_set", "investigation_search_admission",
    "investigation_search_candidate_admitted", "investigation_search_selector_admitted",
})


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def recover_triage_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    expected_board: str,
    expected_status: str,
    expected_block_loop_event_id: int,
    expected_latest_event_id: int,
    expected_run_id: int,
    expected_parent_ids: Sequence[str],
    expected_landing_status: str,
    reason: str,
    actor: str,
) -> tuple[bool, dict[str, Any] | str]:
    """CAS one explicitly reviewed capability block-loop row out of triage.

    No content, assignment, dependency, run, claim, or counter is rewritten.
    The caller must name the selected board and the exact Task/event/run/parent
    snapshot it just read. ``ready`` versus ``todo`` is calculated under the
    same write transaction and must match the caller's expectation.
    """
    from hermes_cli import kanban_db as kb

    if not isinstance(task_id, str) or not task_id:
        return False, "invalid_task_id"
    if not isinstance(expected_board, str) or not expected_board:
        return False, "invalid_board"
    try:
        if kb._normalize_board_slug(expected_board) != expected_board:
            return False, "board_not_canonical"
    except ValueError:
        return False, "invalid_board"
    if expected_status != "triage":
        return False, "expected_status_must_be_triage"
    if not _positive_int(expected_block_loop_event_id):
        return False, "invalid_block_loop_event_id"
    if not _positive_int(expected_latest_event_id):
        return False, "invalid_latest_event_id"
    if not _positive_int(expected_run_id):
        return False, "invalid_run_id"
    if expected_landing_status not in {"ready", "todo"}:
        return False, "invalid_landing_status"
    if (not isinstance(expected_parent_ids, (list, tuple))
            or any(not isinstance(parent_id, str) or not parent_id for parent_id in expected_parent_ids)
            or len(set(expected_parent_ids)) != len(expected_parent_ids)):
        return False, "invalid_parent_ids"
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        return False, "invalid_reason"
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 100:
        return False, "invalid_actor"

    expected_parents = sorted(expected_parent_ids)
    audit_reason = str(kb.redact_review_value(reason.strip())).strip()
    audit_actor = str(kb.redact_review_value(actor.strip())).strip()
    if not audit_reason or not audit_actor:
        return False, "invalid_redacted_audit"

    with kb.write_txn(conn):
        try:
            expected_db = Path(kb.kanban_db_path(board=expected_board)).resolve()
            db_rows = conn.execute("PRAGMA database_list").fetchall()
            main_db = next((Path(row["file"]).resolve() for row in db_rows if row["name"] == "main"), None)
        except (OSError, KeyError, ValueError):
            return False, "board_identity_unavailable"
        if main_db is None or main_db != expected_db:
            return False, "board_identity_mismatch"

        task = conn.execute(
            "SELECT id, status, assignee, block_kind, block_recurrences, "
            "current_run_id, claim_lock, claim_expires, worker_pid, worker_started_at "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if task is None:
            return False, "task_not_found_on_board"
        if task["status"] != expected_status:
            return False, "task_status_changed"
        if task["block_kind"] != "capability":
            return False, "task_is_not_capability_triage"
        if any(task[field] is not None for field in (
            "current_run_id", "claim_lock", "claim_expires", "worker_pid", "worker_started_at",
        )):
            return False, "active_task_ownership_present"

        parents = [row[0] for row in conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
            (task_id,),
        )]
        if parents != expected_parents:
            return False, "parent_set_changed"

        block_event = conn.execute(
            "SELECT id, kind, run_id, payload FROM task_events WHERE task_id = ? AND id = ?",
            (task_id, expected_block_loop_event_id),
        ).fetchone()
        if block_event is None or block_event["kind"] != "block_loop_detected":
            return False, "block_loop_event_changed"
        payload = kb._json_dict(block_event["payload"])
        if (payload.get("kind") != "capability"
                or payload.get("limit") != kb.BLOCK_RECURRENCE_LIMIT
                or type(payload.get("recurrences")) is not int
                or payload.get("recurrences") < kb.BLOCK_RECURRENCE_LIMIT
                or payload.get("recurrences") != task["block_recurrences"]
                or block_event["run_id"] != expected_run_id):
            return False, "block_loop_event_evidence_mismatch"

        latest_event = conn.execute(
            "SELECT id, kind FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if latest_event is None or latest_event["id"] != expected_latest_event_id:
            return False, "latest_event_changed"
        if expected_latest_event_id < expected_block_loop_event_id:
            return False, "latest_event_precedes_block_loop"
        later_rows = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? AND id > ? ORDER BY id",
            (task_id, expected_block_loop_event_id),
        ).fetchall()
        if any(row["kind"] not in _NON_STATE_EVENTS for row in later_rows):
            return False, "newer_state_or_unknown_event"

        latest_run = conn.execute(
            "SELECT id, status, outcome, ended_at FROM task_runs "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if (latest_run is None or latest_run["id"] != expected_run_id
                or latest_run["ended_at"] is None
                or latest_run["status"] != "blocked"
                or latest_run["outcome"] != "blocked"):
            return False, "latest_ended_run_changed"
        active_run = conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id = ? "
            "AND (ended_at IS NULL OR status = 'running') LIMIT 1",
            (task_id,),
        ).fetchone()
        if active_run is not None:
            return False, "active_run_present"

        landing_status = kb._landing_status_after_parents(conn, task_id)
        if landing_status != expected_landing_status:
            return False, "parent_gate_or_landing_status_changed"
        if landing_status not in {"ready", "todo"}:
            return False, "unsupported_landing_status"

        updated = conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ? AND status = 'triage' "
            "AND current_run_id IS NULL AND claim_lock IS NULL "
            "AND claim_expires IS NULL AND worker_pid IS NULL AND worker_started_at IS NULL",
            (landing_status, task_id),
        )
        if updated.rowcount != 1:
            return False, "task_compare_and_swap_lost"

        kb._append_event(
            conn,
            task_id,
            "triage_recovered",
            {
                "actor": audit_actor,
                "reason": audit_reason,
                "board": expected_board,
                "task_id": task_id,
                "from_status": "triage",
                "to_status": landing_status,
                "block_loop_event_id": expected_block_loop_event_id,
                "expected_latest_event_id": expected_latest_event_id,
                "run_id": expected_run_id,
                "block_kind": "capability",
                "block_recurrences_preserved": task["block_recurrences"],
                "parent_ids": parents,
            },
        )
        event_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    return True, {
        "board": expected_board,
        "task_id": task_id,
        "from_status": "triage",
        "status": landing_status,
        "block_loop_event_id": expected_block_loop_event_id,
        "run_id": expected_run_id,
        "event_id": event_id,
        "block_recurrences_preserved": task["block_recurrences"],
        "parent_ids": parents,
    }

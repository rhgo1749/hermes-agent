"""Atomic review handoffs; shared authority helpers remain late-bound on kanban_db."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


def _park_precondition_reason(park, reviewer, expected_status, expected_run_id):
    if not park:
        return "expected_status is supported only for parked review" if expected_status is not None else None
    if reviewer is not None:
        return "parked review cannot name a reviewer profile"
    if expected_status not in {"ready", "running"}:
        return "parked review requires expected_status='ready' or 'running'"
    if expected_status == "running" and expected_run_id is None:
        return "parking a running task requires expected_run_id"
    if expected_status == "ready" and expected_run_id is not None:
        return "parking a ready task must not include expected_run_id"
    return None


def request_review(
    conn: sqlite3.Connection, task_id: str, *, summary: Optional[str] = None,
    metadata: Optional[dict] = None, reviewer: Optional[str] = None,
    expected_run_id: Optional[int] = None, force: bool = False, with_reason: bool = False,
    park: bool = False, expected_status: Optional[str] = None,
):
    """``running``/``ready`` -> ``review``; never touches block recurrence accounting.

    Implementer and reviewer are recorded on the event so requested changes
    route back to the right profile; ``reviewer`` reassigns the task, and on
    re-review defaults to the latest ``changes_requested`` provenance. A live
    claim is only cleared with proof of ownership (``expected_run_id``) or
    ``force=True``. Returns ``bool``, or ``(ok, reason)`` with ``with_reason``.

    ``park=True`` is the H4V3 compatibility seam for hermes-github-kanban #6
    (upstream owner NousResearch/hermes-agent#107718): an atomic
    parked-review handoff that lands the card in ``review`` with the live
    claim cleared AND the assignee nulled, so no reviewer/assignee is
    spawnable by the dispatcher's review lane until a trusted rework
    (:func:`reopen_review_task`) or an explicit operator assignment.
    Implementer provenance still rides the ``review_requested`` event;
    ``reviewer`` must not be named together with ``park``. Parked handoffs
    must provide ``expected_status`` (``ready`` or ``running``); running
    handoffs additionally require ``expected_run_id``. Those preconditions
    are included in the same UPDATE CAS that clears the claim and assignee.
    Deletable once upstream ships an equivalent parked-review primitive.

    ``metadata["artifacts"]`` names the handoff's deliverable
    files; a review handoff is the last implementer transition, and the
    *reviewer's* completion is what cleans the managed scratch workspace up, so
    the files are staged into the task's durable attachments dir here and the
    staged paths ride the ``review_requested`` payload for the notifier to
    upload. A declared artifact that cannot be preserved raises
    :class:`ArtifactPreservationError`, rolling the whole transition back: the
    task stays ``running`` and retryable, with no attachments and no event.
    """

    from hermes_cli import kanban_db as kb

    def _ret(ok: bool, reason: Optional[str] = None):
        return (ok, reason) if with_reason else ok

    reason = _park_precondition_reason(park, reviewer, expected_status, expected_run_id)
    if reason is not None:
        return _ret(False, reason)
    summary = kb.redact_review_value(summary)
    metadata = kb.redact_review_value(metadata)
    # Declared (metadata["artifacts"]) and prose-referenced files
    # must be durable BEFORE anything can clean the scratch workspace up: for a
    # review-bound card the reviewer's completion is the cleanup trigger.
    metadata = kb._merge_completion_prose_artifacts(conn, task_id, metadata, summary=summary, result=None)
    now = int(time.time())
    # Staged copies live outside the txn: a rollback after staging must not
    # leave orphans that make the retry stage ``name_1.ext`` beside them.
    staged_copies: list[Path] = []
    try:
        with kb.write_txn(conn):
            if not kb._parents_satisfied(conn, task_id):
                return _ret(False, "parent dependencies are not satisfied")
            trow = conn.execute(
                "SELECT assignee, status, claim_lock, current_run_id, worker_pid, "
                "worker_started_at FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            if trow is None:
                return _ret(False, "task not found")
            # Refuse to clear a live worker's claim without proof of ownership
            # (expected_run_id) or an explicit human override (force=True);
            # the same fence as complete_task (_claim_is_live).
            if expected_run_id is None and not force and kb._claim_is_live(trow):
                return _ret(
                    False, "task is running under a live claim; pass expected_run_id "
                    "(worker ownership) or force=True (explicit operator "
                    "override) instead of clearing the live run's claim",
                )
            # Parked review deliberately does NOT adopt prior-reviewer
            # provenance as an assignee: naming any profile here would make
            # the row spawnable by the review lane, which is exactly what the
            # H4V3 #6 parked review state forbids. The dispatcher skips unassigned
            # review rows (kanban_db_dispatch review lane -> skipped_unassigned).
            if reviewer is None and not park:
                prior_reviewer = kb._prior_reviewer(conn, task_id)
                if prior_reviewer is False:
                    return _ret(
                        False, "re-review has no durable reviewer provenance (the "
                        "latest changes_requested event is missing or "
                        "malformed); pass reviewer= explicitly",
                    )
                reviewer = prior_reviewer
            reviewer = kb._canonical_assignee(reviewer)
            # The actor is the run that did the work. ``assignee`` is the actor
            # only while a worker holds the card; on a never-claimed card it is
            # whoever the operator assigned -- possibly the reviewer itself,
            # which is what ``kanban create --assignee <reviewer>`` followed by
            # ``request-review`` produces. Recording the reviewer as its own
            # implementer is worse than recording nothing: request_changes()
            # routes on this field, and it already refuses a handoff that
            # carries no implementer provenance.
            implementer = None
            if trow["current_run_id"] is not None:
                arow = conn.execute(
                    "SELECT profile FROM task_runs WHERE id = ?",
                    (trow["current_run_id"],),
                ).fetchone()
                implementer = arow["profile"] if arow else None
            if implementer is None and trow["assignee"] != reviewer:
                implementer = trow["assignee"]
            # H4V3 #6 parked review: clear the assignee in the SAME CAS UPDATE
            # as the status flip (claim fields are already cleared there), so
            # the parked row is never observable as "review with a spawnable
            # assignee". Legacy callers (reviewer named) keep the reassign
            # semantics exactly as before.
            run_guard = "" if expected_run_id is None else " AND current_run_id = ?"
            status_guard = (
                " AND status = ?" if expected_status is not None
                else " AND status IN ('running', 'ready')"
            )
            if park:
                assignee_sql = ", assignee = NULL"
                params: tuple[Any, ...] = (
                    task_id,
                    *((expected_status,) if expected_status is not None else ()),
                    *(() if expected_run_id is None else (int(expected_run_id),)),
                )
            else:
                assignee_sql = ", assignee = ?" if reviewer is not None else ""
                params = (
                    *(() if reviewer is None else (reviewer,)), task_id,
                    *((expected_status,) if expected_status is not None else ()),
                    *(() if expected_run_id is None else (int(expected_run_id),)),
                )
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status        = 'review',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL
                """ + assignee_sql + """
                 WHERE id = ?
                """ + status_guard + run_guard,
                params,
            )
            if cur.rowcount != 1:
                return _ret(
                    False, "task is not in running/ready (or expected_run_id did not match the current run)",
                )
            if isinstance(metadata, dict):
                staged_copies = kb._stage_completion_artifacts(
                    conn, task_id, metadata, now, uploaded_by="kanban_request_review",
                )
            run_id = kb._end_or_synthesize_run(
                conn, task_id, outcome="review_requested", status="review",
                summary=summary, metadata=metadata, synthesize=bool(summary or metadata),
                profile=implementer,
            )
            payload: dict = {
                "summary": kb._first_line(summary, 400) or None,
                "implementer": implementer,
                "reviewer": reviewer,
            }
            if park:
                # Durable marker so a fresh public read-back (kanban_show /
                # CLI) can distinguish an H4V3 #6 parked review from a legacy
                # reviewer-assigned review; reopen restores the implementer.
                payload["parked"] = True
            staged = kb._cleaned_artifact_paths(metadata)
            if staged:
                payload["artifacts"] = staged
            kb._append_event(conn, task_id, "review_requested", payload, run_id=run_id)
    except Exception:
        if staged_copies:
            kb._discard_staged_copies(staged_copies, staged_copies[0].parent)
        raise
    return _ret(True)

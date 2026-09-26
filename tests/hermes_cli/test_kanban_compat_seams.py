"""H4V3 compatibility seams for hermes-github-kanban Issue #6.

Three carry-patch seams on the Hermes core Kanban authority boundary:

1. ``create_task(idempotency_key=...)`` is authority-atomic: the idempotency
   re-check runs INSIDE the existing ``BEGIN IMMEDIATE`` write transaction, so
   concurrent same-key creators serialise on the write lock and the losers
   return the winner's committed id instead of inserting duplicates
   (upstream owner NousResearch/hermes-agent#107718 K14).
2. ``request_review(park=True)`` lands ``ready``/``running`` -> ``review``
   atomically with the live claim cleared AND no runnable assignee, while the
   implementer provenance still rides the ``review_requested`` event.
3. ``reopen_review_task`` (existing core FSM, now also exposed publicly as the
   ``kanban_reopen_review`` tool) reopens the same parked root to ready/todo,
   restoring the implementer, and fails closed on any state other than
   ``review``.

Each seam is deletable: removing the marked blocks restores upstream
behaviour. Tests assert final observable authority state, not call counts.
"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


# ---------------------------------------------------------------------------
# Seam 1: authority-atomic same-idempotency-key create
# ---------------------------------------------------------------------------

def _same_key_writer(db_path: str, home: str, key: str, barrier, results) -> None:
    """One concurrent creator in its own process.

    The barrier sits inside a wrapper around ``write_txn`` — i.e. every writer
    has already read the pre-txn idempotency fast path (saw nothing) and is
    parked at the transaction boundary. Releasing the barrier forces the exact
    K14 race: without the in-transaction re-check, every writer inserts.
    """
    os.environ["HERMES_HOME"] = home
    os.environ["HERMES_KANBAN_DB"] = db_path
    from hermes_cli import kanban_db as kb_child
    from hermes_cli import kanban_db_connect as kbc_child

    kb_child._INITIALIZED_PATHS.clear()
    conn = kbc_child.connect(Path(db_path))
    real_write_txn = kb_child.write_txn

    def gated_write_txn(c, **kwargs):
        barrier.wait(timeout=30)
        return real_write_txn(c, **kwargs)

    kb_child.write_txn = gated_write_txn
    try:
        tid = kb_child.create_task(
            conn, title=f"same-key wake {key}", assignee="builder", idempotency_key=key,
        )
        results.put(tid)
    finally:
        conn.close()


def test_concurrent_same_key_create_yields_exactly_one_active_task(kanban_home):
    """Barrier-controlled multi-process writers; exactly one active task.

    Eight processes pass the pre-txn idempotency fast path together (the
    forced race window upstream K14 describes), then serialise on BEGIN
    IMMEDIATE. The authority-side re-check makes every loser return the
    winner's id. Asserts final authority state: one row for the key, one
    ``created`` event per insert, and every writer's return value equals the
    single committed id.
    """
    db_path = str(kb.kanban_db_path(board="default"))
    home = str(kanban_home)
    key = "github-issue-6-reconcile"

    ctx = multiprocessing.get_context("spawn")
    n_writers = 8
    barrier = ctx.Barrier(n_writers)
    results = ctx.Queue()
    procs = [
        ctx.Process(target=_same_key_writer, args=(db_path, home, key, barrier, results))
        for _ in range(n_writers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"writer exited {p.exitcode}"

    returned = [results.get(timeout=5) for _ in range(n_writers)]
    assert len(set(returned)) == 1, f"same-key create returned {len(set(returned))} distinct ids: {returned}"

    with kbc.connect() as conn:
        rows = conn.execute(
            "SELECT id, status FROM tasks WHERE idempotency_key = ? AND status != 'archived'",
            (key,),
        ).fetchall()
        assert len(rows) == 1, f"{len(rows)} active tasks share idempotency key {key!r}"
        assert rows[0]["id"] == returned[0]
        created = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? AND kind = 'created'",
            (returned[0],),
        ).fetchone()
        assert created["n"] == 1


def test_same_key_create_sequential_still_returns_existing(kanban_home):
    """Preserved API semantics: a second sequential create returns the same id."""
    with kbc.connect() as conn:
        first = kb.create_task(conn, title="wake", assignee="builder", idempotency_key="dup-key")
        second = kb.create_task(conn, title="wake again", assignee="builder", idempotency_key="dup-key")
        assert first == second
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE idempotency_key = 'dup-key'"
        ).fetchone()["n"]
        assert n == 1


# ---------------------------------------------------------------------------
# Seam 2: atomic parked-review handoff
# ---------------------------------------------------------------------------

def _running_claimed_task(conn, *, assignee: str = "builder") -> tuple[str, int]:
    tid = kb.create_task(conn, title="issue 6 root", assignee=assignee)
    assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
    # This process stands in for the spawned worker so _claim_is_live is True.
    kbd._set_worker_pid(conn, tid, os.getpid())
    run_id = kb._current_run_id(conn, tid)
    assert run_id is not None
    return tid, run_id


def _task(conn, task_id: str):
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task


def _review_requested_payload(conn, tid: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'review_requested' "
        "ORDER BY id DESC LIMIT 1", (tid,),
    ).fetchone()
    assert row is not None
    return kb._json_or(row["payload"], {}) if row["payload"] else {}


def test_parked_review_clears_claim_and_assignee_and_keeps_provenance(conn):
    """ready/running -> review atomically: no live claim, no runnable assignee,
    implementer provenance preserved on the review_requested event."""
    tid, run_id = _running_claimed_task(conn)

    assert kb.request_review(
        conn, tid, summary="impl done", park=True,
    ) is False
    assert kb.request_review(
        conn, tid, summary="impl done", park=True, expected_status="running",
    ) is False
    assert _task(conn, tid).status == "running"

    assert kb.request_review(
        conn, tid, summary="impl done", park=True, expected_status="running",
        expected_run_id=run_id,
    ) is True

    task = _task(conn, tid)
    assert task.status == "review"
    assert task.claim_lock is None
    assert task.claim_expires is None
    assert task.worker_pid is None
    assert task.assignee is None, "parked review must not leave a runnable assignee"

    payload = _review_requested_payload(conn, tid)
    assert payload.get("implementer") == "builder"
    assert payload.get("parked") is True
    assert payload.get("reviewer") is None

    # The closed run survived as review_requested (implementer attribution).
    run = conn.execute(
        "SELECT outcome, profile FROM task_runs WHERE id = ?", (run_id,),
    ).fetchone()
    assert run["outcome"] == "review_requested"
    assert run["profile"] == "builder"


def test_parked_review_is_not_spawnable(kanban_home, monkeypatch):
    """A parked root is skipped by the dispatcher's review lane — even with a
    configured default_assignee — until it is reopened."""
    import hermes_cli.config as cfgmod

    # Installed profiles: "builder" and "review-bot" stand in for real profiles.
    monkeypatch.setattr(
        kbd, "_profile_exists_fn", lambda: (lambda name: name in {"builder", "review-bot"})
    )
    monkeypatch.setattr(
        cfgmod, "load_config", lambda *a, **k: {"kanban": {"review_dispatch": True}}
    )
    with kbc.connect() as conn:
        tid, run_id = _running_claimed_task(conn)
        assert kb.request_review(
            conn, tid, summary="parking on open PR", park=True, expected_status="running",
            expected_run_id=run_id,
        ) is True

        assert kbd.has_spawnable_review(conn) is False
        res = kbd.dispatch_once(conn, dry_run=True, default_assignee="review-bot",
                                spawn_fn=lambda *a, **k: None)
        assert tid not in [s[0] for s in res.spawned]
        assert tid in res.skipped_unassigned
        assert _task(conn, tid).status == "review"


def test_parked_review_refuses_to_name_a_reviewer(conn):
    """Guard: park=True cannot smuggle a reviewer profile onto the parked row."""
    tid = kb.create_task(conn, title="park+reviewer", assignee="builder")
    assert kb.request_review(
        conn, tid, summary="x", park=True, reviewer="review-bot",
    ) is False
    assert _task(conn, tid).status == "ready"


def test_legacy_request_review_semantics_unchanged(conn):
    """Without park=, review still reassigns to the named reviewer (regression
    guard for the preserved contract)."""
    tid = kb.create_task(conn, title="legacy review", assignee="builder")
    assert kb.request_review(conn, tid, summary="done", reviewer="review-bot") is True
    task = _task(conn, tid)
    assert task.status == "review"
    assert task.assignee == "review-bot"
    payload = _review_requested_payload(conn, tid)
    assert payload.get("parked") is None
    assert payload.get("reviewer") == "review-bot"


# ---------------------------------------------------------------------------
# Seam 3: guarded parked-review reopen (trusted rework)
# ---------------------------------------------------------------------------

def _parked_root(conn) -> str:
    tid, run_id = _running_claimed_task(conn)
    assert kb.request_review(
        conn, tid, summary="parked on unresolved PR", park=True,
        expected_status="running", expected_run_id=run_id,
    ) is True
    return tid


def test_reopen_restores_same_task_to_ready_with_implementer(conn):
    """The same parked root returns to ready with the implementer restored —
    no new task, root/PR history (events/comments) preserved."""
    tid = _parked_root(conn)
    kb.add_comment(conn, tid, "reconciler", "PR https://github.com/o/r/pull/6 merged-plan ready")
    events_before = conn.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ?", (tid,),
    ).fetchone()["n"]

    assert kb.reopen_review_task(conn, tid) is True

    task = _task(conn, tid)
    assert task.status == "ready"
    assert task.assignee == "builder"
    assert task.claim_lock is None and task.worker_pid is None and task.current_run_id is None
    events_after = conn.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ?", (tid,),
    ).fetchone()["n"]
    assert events_after == events_before + 1  # only review_reopened appended
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,),
    ).fetchall()]
    assert kinds.count("created") == 1
    assert "review_reopened" in kinds
    # Reopened root is dispatchable again for the implementer.
    assert kb.claim_task(conn, tid) is not None


def test_reopen_rolls_back_when_atomic_rework_comment_fails(conn):
    """A failed rework-comment insert must not expose a ready task to dispatch."""
    tid = _parked_root(conn)
    conn.execute(
        """
        CREATE TRIGGER reject_reopen_comment
        BEFORE INSERT ON task_comments
        BEGIN
            SELECT RAISE(ABORT, 'forced rework comment failure');
        END
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced rework comment failure"):
        kb.reopen_review_task(
            conn, tid, reason="PR merged; resume work", author="reconciler",
        )

    assert _task(conn, tid).status == "review"
    assert not any(event.kind == "review_reopened" for event in kb.list_events(conn, tid))
    assert kb.list_comments(conn, tid) == []


def test_reopen_parent_gated_lands_todo(conn):
    """Trusted rework while a parent is open lands parent-gated todo, not ready."""
    parent = kb.create_task(conn, title="parent", assignee="planner")
    tid = _parked_root(conn)
    # link_tasks accepts a review child (only running children are rejected);
    # the open parent edge makes the reopen landing parent-gated.
    kb.link_tasks(conn, parent_id=parent, child_id=tid)
    assert kb.reopen_review_task(conn, tid) is True
    assert _task(conn, tid).status == "todo"


def test_reopen_fails_closed_on_stale_or_invalid_state(conn):
    """Stale/duplicate rework wakes fail closed: only a live ``review`` row
    reopens; ready/done/unknown all refuse without mutating state."""
    tid = _parked_root(conn)
    assert kb.reopen_review_task(conn, tid) is True  # first wake consumes the park
    # Duplicate wake: the card is ready now — refuse, do not re-fire.
    assert kb.reopen_review_task(conn, tid) is False
    assert _task(conn, tid).status == "ready"

    done_root = kb.create_task(conn, title="done root", assignee="builder")
    kb.request_review(conn, done_root, summary="r", reviewer="review-bot")
    assert kb.complete_task(conn, done_root, result="merged") is True
    assert kb.reopen_review_task(conn, done_root) is False
    assert _task(conn, done_root).status == "done"

    assert kb.reopen_review_task(conn, "t_missing00") is False

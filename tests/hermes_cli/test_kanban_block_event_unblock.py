"""Atomic unblock preconditions for a specific capability-block event."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _blocked(conn, *, reason: str, kind: str = "capability") -> tuple[str, int]:
    task_id = kb.create_task(conn, title="conditional-unblock", assignee="worker")
    claimed = kb.claim_task(conn, task_id, claimer="worker")
    assert claimed is not None and claimed.current_run_id is not None
    assert kb.block_task(
        conn, task_id, reason=reason, kind=kind, expected_run_id=claimed.current_run_id,
    )
    event = [event for event in kb.list_events(conn, task_id) if event.kind == "blocked"][-1]
    assert event.payload and event.payload["kind"] == kind
    return task_id, event.id


def _snapshot(conn, task_id: str) -> tuple:
    task = tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
    events = [tuple(row) for row in conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,),
    )]
    runs = [tuple(row) for row in conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,),
    )]
    return task, events, runs


def test_conditional_unblock_releases_only_the_current_capability_event(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        task_id, block_event_id = _blocked(conn, reason="fixed validation requested")

        assert kb.unblock_task(conn, task_id, expected_block_event_id=block_event_id) is True
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "ready"
        assert kb.list_events(conn, task_id)[-1].kind == "unblocked"


def test_stale_conditional_unblock_preserves_a_newer_human_block(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        task_id, original_event_id = _blocked(conn, reason="validation request")
        assert kb.unblock_task(conn, task_id) is True

        claimed = kb.claim_task(conn, task_id, claimer="worker")
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.block_task(
            conn, task_id, reason="operator decision required", kind="needs_input",
            expected_run_id=claimed.current_run_id,
        )
        latest_block = [event for event in kb.list_events(conn, task_id) if event.kind == "blocked"][-1]
        assert latest_block.id != original_event_id
        assert latest_block.payload and latest_block.payload["kind"] == "needs_input"
        before = _snapshot(conn, task_id)

        assert kb.unblock_task(conn, task_id, expected_block_event_id=original_event_id) is False

        assert _snapshot(conn, task_id) == before
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "blocked"


def test_conditional_unblock_rejects_an_event_from_another_task(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        first_id, _first_event_id = _blocked(conn, reason="first")
        second_id, second_event_id = _blocked(conn, reason="second")
        before = _snapshot(conn, first_id)

        assert kb.unblock_task(conn, first_id, expected_block_event_id=second_event_id) is False

        assert _snapshot(conn, first_id) == before
        task = kb.get_task(conn, second_id)
        assert task is not None and task.status == "blocked"


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2"])
def test_conditional_unblock_requires_a_positive_integer_event_id(kanban_home: Path, value) -> None:
    with kbc.connect_closing() as conn:
        task_id, _event_id = _blocked(conn, reason="validation request")
        before = _snapshot(conn, task_id)

        with pytest.raises(ValueError, match="expected_block_event_id"):
            kb.unblock_task(conn, task_id, expected_block_event_id=value)

        assert _snapshot(conn, task_id) == before

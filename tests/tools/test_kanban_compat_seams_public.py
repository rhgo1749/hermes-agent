"""Public tool-surface coverage for the H4V3 #6 compatibility seams.

The hermes-github-kanban reconciler plugin may only use registered Hermes
tools (no private DB import, no CLI shell-out). These tests drive the seams
through ``tools.registry`` dispatch — the same surface a plugin's
``dispatch_tool`` reaches:

* ``kanban_park_review`` lands the parked review state without broadening the
  worker-visible ``kanban_request_review`` schema;
* ``kanban_reopen_review`` reopens it for trusted rework, records the reason,
  and fails closed on stale wakes;
* ``kanban_reopen_review`` is orchestrator-routed (hidden from dispatcher
  workers) and refuses delegate_task children.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def board_env(tmp_path, monkeypatch):
    """Isolated board, orchestrator context (no HERMES_KANBAN_TASK env)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    import tools.kanban_tools  # register actual handlers
    return home


def _dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Registry dispatch (the plugin-visible surface) -> parsed result."""
    from tools.registry import registry
    raw = registry.dispatch(name, args)
    return json.loads(raw) if isinstance(raw, str) else raw


def _make_ready_task(title: str) -> str:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=title, assignee="builder",
                             workspace_kind="scratch")
        conn.commit()
    return tid


def _make_running_task(title: str) -> tuple[str, int]:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=title, assignee="builder",
                             workspace_kind="scratch")
        assert kb.claim_task(conn, tid) is not None
        run_id = kb._current_run_id(conn, tid)
        # This process stands in for the spawned worker (live claim).
        kbd._set_worker_pid(conn, tid, os.getpid())
        conn.commit()
    assert run_id is not None
    return tid, run_id


def _state(tid: str) -> dict[str, Any]:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        return {"status": task.status, "assignee": task.assignee,
                "claim_lock": task.claim_lock, "worker_pid": task.worker_pid}


def test_public_park_preserves_live_claim_fence(board_env):
    """An orchestrator without run-ownership proof cannot park a card a live
    worker holds: the existing fence applies unchanged to the park path."""
    tid, run_id = _make_running_task("live worker card")
    refused = _dispatch("kanban_park_review", {
        "task_id": tid, "summary": "park",
        "expected_status": "running"})
    assert refused.get("error") and "expected_run_id" in refused["error"], refused
    assert _state(tid)["status"] == "running"

    parked = _dispatch("kanban_park_review", {
        "task_id": tid, "summary": "park",
        "expected_status": "running", "expected_run_id": run_id})
    assert parked.get("ok"), parked
    state = _state(tid)
    assert state["status"] == "review" and state["assignee"] is None
    assert state["claim_lock"] is None and state["worker_pid"] is None


def test_public_park_and_reopen_round_trip(board_env):
    """ready -> parked review (no claim, no assignee) -> reopen -> ready with
    the implementer restored, through registry dispatch only."""
    tid = _make_ready_task("issue 6 root")

    parked = _dispatch("kanban_park_review", {
        "task_id": tid, "summary": "PR open; parking per issue #6",
        "expected_status": "ready"})
    assert parked.get("ok"), parked
    assert parked["status"] == "review"
    state = _state(tid)
    assert state["status"] == "review"
    assert state["assignee"] is None
    assert state["claim_lock"] is None and state["worker_pid"] is None

    reopened = _dispatch("kanban_reopen_review", {
        "task_id": tid, "reason": "PR merged; rework the follow-up"})
    assert reopened.get("ok"), reopened
    assert reopened["status"] == "ready"
    assert reopened["assignee"] == "builder"
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        comments = kb.list_comments(conn, tid)
    assert any("CHANGES REQUESTED" in c.body and "merged" in c.body for c in comments)


def test_park_tool_refuses_undeclared_reviewer_argument(board_env):
    """The dedicated park operation cannot assign a runnable reviewer."""
    tid = _make_ready_task("conflict")
    result = _dispatch("kanban_park_review", {
        "task_id": tid, "summary": "x", "expected_status": "ready",
        "reviewer": "review-bot"})
    assert result.get("error") and "unknown parameter" in result["error"], result
    assert _state(tid)["status"] == "ready"


def test_reopen_review_fails_closed_on_stale_wake(board_env):
    """Duplicate/reordered rework wakes refuse without mutating state."""
    tid = _make_ready_task("stale wake root")
    assert _dispatch("kanban_park_review", {
        "task_id": tid, "summary": "park",
        "expected_status": "ready"}).get("ok")
    first = _dispatch("kanban_reopen_review", {"task_id": tid})
    assert first.get("ok") and first["status"] == "ready"
    stale = _dispatch("kanban_reopen_review", {"task_id": tid})
    assert stale.get("error") and "not in review" in stale["error"], stale
    assert _state(tid)["status"] == "ready"
    missing = _dispatch("kanban_reopen_review", {"task_id": "t_nope00000"})
    assert missing.get("error"), missing


def test_reopen_review_is_orchestrator_only_for_workers(board_env, monkeypatch):
    """A dispatcher-spawned worker (HERMES_KANBAN_TASK set) is refused."""
    from tools import kanban_tools as kt
    tid, _run_id = _make_running_task("worker card")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    result = json.loads(kt._handle_reopen_review({"task_id": tid}))
    assert result.get("error") and "orchestrator-only" in result["error"], result
    assert _state(tid)["status"] == "running"


def test_park_review_is_orchestrator_only_for_workers(board_env, monkeypatch):
    """Control-plane parking rejects dispatcher workers and delegate children."""
    from tools import kanban_tools as kt
    tid, run_id = _make_running_task("worker card cannot park itself")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    args = {
        "task_id": tid, "summary": "unresolved external work",
        "expected_status": "running", "expected_run_id": run_id}
    result = json.loads(kt._handle_park_review(args))
    assert result.get("error") and "orchestrator-only" in result["error"], result

    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(board_env))
    child_result = _dispatch("kanban_park_review", args)
    assert child_result.get("error") and "delegate_task child" in child_result["error"], child_result
    assert _state(tid)["status"] == "running"


def test_reopen_review_refuses_delegate_task_children(board_env, monkeypatch):
    """A delegate_task child lineage may not drive board mutations."""
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(board_env))
    result = json.loads(kt._handle_reopen_review({"task_id": "t_anything0"}))
    assert result.get("error") and "delegate_task child" in result["error"], result


def test_new_tool_names_in_orchestrator_registry(board_env):
    """The seam tool is registered, orchestrator-gated, and schema-declared —
    plumbing invariant (not a tool-count snapshot)."""
    from tools import kanban_tools as kt
    from tools.registry import registry
    from toolsets import _HERMES_CORE_TOOLS
    names = {name for name, _s, _h, _e in kt._TOOLS}
    assert {"kanban_park_review", "kanban_reopen_review"} <= names
    assert {"kanban_park_review", "kanban_reopen_review"} <= set(_HERMES_CORE_TOOLS)
    assert {"kanban_park_review", "kanban_reopen_review"} <= kt._ORCHESTRATOR_TOOLS
    reopen_schema = registry.get_schema("kanban_reopen_review")
    assert reopen_schema is not None
    assert reopen_schema["parameters"]["required"] == ["task_id"]
    park_schema = registry.get_schema("kanban_park_review")
    assert park_schema is not None
    assert park_schema["parameters"]["required"] == ["task_id", "summary", "expected_status"]
    assert "expected_run_id" in park_schema["parameters"]["properties"]
    review_schema = registry.get_schema("kanban_request_review")
    assert review_schema is not None
    review_properties = review_schema["parameters"]["properties"]
    assert "park" not in review_properties

"""Kanban retry workers reuse durable Hermes conversation state."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_state import SessionDB


def _task(workspace: str, *, run_id: int = 2) -> kb.Task:
    return kb.Task(
        id="t_resume123",
        title="resume me",
        body="keep the investigation context",
        assignee="investigator",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=1,
        completed_at=None,
        workspace_kind="worktree",
        workspace_path=workspace,
        claim_lock="host:1",
        claim_expires=999,
        tenant=None,
        branch_name="wt/t_resume123",
        current_run_id=run_id,
    )


def test_resume_session_matches_exact_task_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "investigator"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(
        kbd,
        "_previous_resumable_worker_attempt",
        lambda _task, _board: (0, 9_999_999_999, "crashed"),
    )

    db = SessionDB(db_path=profile / "state.db")
    try:
        db.create_session("wrong", source="kanban")
        db.append_message("wrong", "user", "work kanban task t_other")
        db.create_session("right", source="kanban")
        db.append_message("right", "user", "work kanban task t_resume123")
        db.append_message("right", "assistant", "I already found the important evidence.")
        db.end_session("right", "crashed")
    finally:
        db.close()

    assert kbd._resume_session_for_worker(
        _task(str(tmp_path)), str(profile), board=None,
    ) == "right"


def test_default_spawn_passes_resolved_resume_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "investigator"
    workspace = tmp_path / "ws"
    root.mkdir()
    profile.mkdir(parents=True)
    workspace.mkdir()
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    (profile / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda _home: None)
    monkeypatch.setattr(kbd, "_retag_legacy_worker_sessions", lambda _root: None)
    monkeypatch.setattr(
        kbd,
        "_resume_session_for_worker",
        lambda _task, _profile_home, *, board: "20260919_120000_deadbe",
    )
    monkeypatch.setattr(kbd, "_restart_safe_worker_argv", lambda _task, cmd: cmd)
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: tmp_path / "logs")

    captured: list[str] = []

    class _Proc:
        pid = 4321

    def fake_popen(cmd, **kwargs):
        captured.extend(cmd)
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    pid = kbd._default_spawn(_task(str(workspace)), str(workspace))

    assert pid == 4321
    resume_i = captured.index("--resume")
    assert captured[resume_i : resume_i + 3] == [
        "--resume", "20260919_120000_deadbe", "--no-restore-cwd",
    ]
    assert resume_i < captured.index("chat")

"""Exact operator-only CAS recovery for a block-loop triage task.

Regression for the Re-Bound #151 triage recovery: unlike generic promote or
unblock, recovery must bind one board, task, latest block-loop event, ended
run, parent set, and expected landing status in one write transaction.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import threading

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _looped_task(conn, *, board="default", assignee="kanban-main", block_kind="capability"):
    parent = kb.create_task(conn, title="finished parent", assignee="setup")
    assert kb.complete_task(conn, parent, result="parent finished")
    task_id = kb.create_task(
        conn,
        title="preserve this title",
        body="preserve this body and rework provenance",
        assignee=assignee,
        parents=[parent],
    )
    kb.recompute_ready(conn)
    assert kb.get_task(conn, task_id).status == "ready"

    for attempt in range(2):
        claimed = kb.claim_task(conn, task_id, claimer=f"worker-{attempt}")
        assert claimed is not None
        assert kb.block_task(
            conn,
            task_id,
            reason="bounded-context source unavailable",
            kind=block_kind,
            expected_run_id=claimed.current_run_id,
        )
        if attempt == 0:
            assert kb.get_task(conn, task_id).status == "blocked"
            assert kb.unblock_task(conn, task_id)
        else:
            assert kb.get_task(conn, task_id).status == "triage"

    kb.add_comment(conn, task_id, author="reviewer", body="preserve prior comment")
    kb.get_task(conn, task_id)
    events = kb.list_events(conn, task_id)
    block_event = next(e for e in reversed(events) if e.kind == "block_loop_detected")
    run = kb.latest_run(conn, task_id)
    assert run is not None and run.id == block_event.run_id and run.ended_at is not None
    parent_ids = [r[0] for r in conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id=? ORDER BY parent_id", (task_id,)
    )]
    return {
        "board": board,
        "task_id": task_id,
        "block_kind": block_kind,
        "parent_ids": parent_ids,
        "event_id": block_event.id,
        "latest_event_id": events[-1].id,
        "run_id": run.id,
        "landing_status": "ready",
    }


def _recover(conn, evidence, **overrides):
    args = {
        "task_id": evidence["task_id"],
        "expected_board": evidence["board"],
        "expected_status": "triage",
        "expected_block_kind": evidence.get("block_kind", "capability"),
        "expected_block_loop_event_id": evidence["event_id"],
        "expected_latest_event_id": evidence["latest_event_id"],
        "expected_run_id": evidence["run_id"],
        "expected_parent_ids": evidence["parent_ids"],
        "expected_landing_status": evidence["landing_status"],
        "reason": "operator verified the repair and exact current rework proof",
        "actor": "test-operator",
    }
    if evidence.get("resolution") is not None:
        args["resolution"] = evidence["resolution"]
    args.update(overrides)
    return kb.recover_triage_task(conn, **args)


def _snapshot(conn, task_id):
    return {
        "task": dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()),
        "events": [dict(row) for row in conn.execute(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
        )],
        "runs": [dict(row) for row in conn.execute(
            "SELECT * FROM task_runs WHERE task_id=? ORDER BY id", (task_id,)
        )],
        "comments": [dict(row) for row in conn.execute(
            "SELECT * FROM task_comments WHERE task_id=? ORDER BY id", (task_id,)
        )],
        "links": [tuple(row) for row in conn.execute(
            "SELECT parent_id, child_id FROM task_links "
            "WHERE parent_id=? OR child_id=? ORDER BY parent_id, child_id",
            (task_id, task_id),
        )],
    }


def test_exact_triage_cas_preserves_every_existing_record_and_recursions(kanban_home):
    with kbc.connect_closing(board="default") as conn:
        evidence = _looped_task(conn)
        before = _snapshot(conn, evidence["task_id"])
        assert before["task"]["block_recurrences"] == 2
        assert before["task"]["block_kind"] == "capability"

        ok, result = _recover(conn, evidence)

        assert ok is True
        assert result["board"] == "default"
        assert result["task_id"] == evidence["task_id"]
        assert result["status"] == "ready"
        assert result["block_loop_event_id"] == evidence["event_id"]
        assert result["run_id"] == evidence["run_id"]
        after = _snapshot(conn, evidence["task_id"])
        assert {k: v for k, v in after["task"].items() if k != "status"} == {
            k: v for k, v in before["task"].items() if k != "status"
        }
        assert after["task"]["status"] == "ready"
        assert after["events"][:-1] == before["events"]
        assert len(after["events"]) == len(before["events"]) + 1
        assert after["events"][-1]["kind"] == "triage_recovered"
        audit = json.loads(after["events"][-1]["payload"])
        assert audit == {
            "actor": "test-operator",
            "reason": "operator verified the repair and exact current rework proof",
            "from_status": "triage",
            "to_status": "ready",
            "board": "default",
            "task_id": evidence["task_id"],
            "block_loop_event_id": evidence["event_id"],
            "expected_latest_event_id": evidence["latest_event_id"],
            "run_id": evidence["run_id"],
            "block_kind": "capability",
            "block_recurrences_preserved": 2,
            "parent_ids": evidence["parent_ids"],
        }
        assert after["runs"] == before["runs"]
        assert after["comments"] == before["comments"]
        assert after["links"] == before["links"]


def test_needs_input_triage_requires_and_audits_explicit_resolution(kanban_home):
    with kbc.connect_closing(board="default") as conn:
        evidence = _looped_task(conn, block_kind="needs_input")
        evidence["resolution"] = "Maintainer authorized bounded rework; visual approval remains pending."
        before = _snapshot(conn, evidence["task_id"])
        assert before["task"]["status"] == "triage"
        assert before["task"]["block_kind"] == "needs_input"

        ok, result = _recover(conn, evidence)

        assert ok is True
        assert isinstance(result, dict)
        assert result["status"] == "ready"
        after = _snapshot(conn, evidence["task_id"])
        assert {k: v for k, v in after["task"].items() if k != "status"} == {
            k: v for k, v in before["task"].items() if k != "status"
        }
        assert after["events"][:-1] == before["events"]
        assert after["events"][-1]["kind"] == "triage_recovered"
        audit = json.loads(after["events"][-1]["payload"])
        assert audit["block_kind"] == "needs_input"
        assert audit["resolution"] == evidence["resolution"]
        assert audit["reason"] == "operator verified the repair and exact current rework proof"
        assert after["runs"] == before["runs"]
        assert after["comments"] == before["comments"]
        assert after["links"] == before["links"]


@pytest.mark.parametrize("resolution", [None, "", "   "])
def test_needs_input_triage_refuses_missing_resolution_without_mutation(kanban_home, resolution):
    with kbc.connect_closing(board="default") as conn:
        evidence = _looped_task(conn, block_kind="needs_input")
        before = _snapshot(conn, evidence["task_id"])

        ok, reason = _recover(conn, evidence, resolution=resolution)

        assert ok is False and "resolution" in reason
        assert _snapshot(conn, evidence["task_id"]) == before


def test_needs_input_cas_rejects_stale_snapshot_without_mutation(kanban_home):
    with kbc.connect_closing(board="default") as conn:
        evidence = _looped_task(conn, block_kind="needs_input")
        evidence["resolution"] = "Maintainer authorized bounded rework."
        before = _snapshot(conn, evidence["task_id"])
        stale_values = (
            {"expected_block_loop_event_id": evidence["event_id"] + 1},
            {"expected_latest_event_id": evidence["event_id"]},
            {"expected_run_id": evidence["run_id"] + 1},
            {"expected_parent_ids": []},
            {"expected_landing_status": "todo"},
        )

        for changes in stale_values:
            ok, reason = _recover(conn, evidence, **changes)
            assert ok is False and isinstance(reason, str)
            assert _snapshot(conn, evidence["task_id"]) == before


def test_needs_input_cas_rejects_active_claim_without_mutation(kanban_home):
    with kbc.connect_closing(board="default") as conn:
        evidence = _looped_task(conn, block_kind="needs_input")
        evidence["resolution"] = "Maintainer authorized bounded rework."
        conn.execute(
            "UPDATE tasks SET claim_lock='live-claim' WHERE id=?",
            (evidence["task_id"],),
        )
        before = _snapshot(conn, evidence["task_id"])

        ok, reason = _recover(conn, evidence)

        assert ok is False and "ownership" in reason
        assert _snapshot(conn, evidence["task_id"]) == before


@pytest.mark.parametrize("block_kind", ["capability", "needs_input"])
@pytest.mark.parametrize(
    ("field", "value"),
    [("claim_expires", 2_000_000_000), ("worker_pid", 4242)],
)
def test_triage_cas_rejects_unreaped_run_ownership_without_mutation(
    kanban_home, block_kind, field, value,
):
    with kbc.connect_closing(board="default") as conn:
        evidence = _looped_task(conn, block_kind=block_kind)
        if block_kind == "needs_input":
            evidence["resolution"] = "Maintainer authorized bounded rework."
        conn.execute(
            f"UPDATE task_runs SET {field}=? WHERE id=?",
            (value, evidence["run_id"]),
        )
        before = _snapshot(conn, evidence["task_id"])

        ok, reason = _recover(conn, evidence)

        assert ok is False and isinstance(reason, str)
        assert _snapshot(conn, evidence["task_id"]) == before


def test_triage_cas_rejects_block_kind_mismatch_without_mutation(kanban_home):
    with kbc.connect_closing(board="default") as conn:
        evidence = _looped_task(conn, block_kind="needs_input")
        evidence["resolution"] = "Maintainer authorized bounded rework."
        before = _snapshot(conn, evidence["task_id"])

        ok, reason = _recover(conn, evidence, expected_block_kind="capability")

        assert ok is False and isinstance(reason, str)
        assert _snapshot(conn, evidence["task_id"]) == before


def test_capability_triage_refuses_needs_input_resolution_field(kanban_home):
    with kbc.connect_closing(board="default") as conn:
        evidence = _looped_task(conn)
        before = _snapshot(conn, evidence["task_id"])

        ok, reason = _recover(conn, evidence, resolution="not a capability resolution")

        assert ok is False and "resolution" in reason
        assert _snapshot(conn, evidence["task_id"]) == before


def test_triage_cas_rejects_stale_event_and_run_ids_without_mutation(kanban_home):
    with kbc.connect_closing() as conn:
        evidence = _looped_task(conn)
        before = _snapshot(conn, evidence["task_id"])
        for changes in (
            {"expected_block_loop_event_id": evidence["event_id"] + 1},
            {"expected_latest_event_id": evidence["event_id"]},
            {"expected_run_id": evidence["run_id"] + 1},
        ):
            ok, reason = _recover(conn, evidence, **changes)
            assert ok is False and isinstance(reason, str)
            assert _snapshot(conn, evidence["task_id"]) == before


def test_triage_cas_rejects_newer_state_change_but_ignores_audited_comment(kanban_home):
    with kbc.connect_closing() as conn:
        evidence = _looped_task(conn)
        before = _snapshot(conn, evidence["task_id"])
        kb._append_event(conn, evidence["task_id"], "status", {"status": "triage"})
        latest_id = conn.execute(
            "SELECT MAX(id) FROM task_events WHERE task_id=?", (evidence["task_id"],)
        ).fetchone()[0]
        after_new_state = _snapshot(conn, evidence["task_id"])
        ok, reason = _recover(conn, evidence, expected_latest_event_id=latest_id)
        assert ok is False and isinstance(reason, str)
        assert _snapshot(conn, evidence["task_id"]) == after_new_state
        assert before["task"]["status"] == "triage"


def test_triage_cas_rejects_wrong_status_board_card_and_parent_set(kanban_home):
    kb.create_board("other")
    with kbc.connect_closing(board="default") as conn:
        evidence = _looped_task(conn)
        before = _snapshot(conn, evidence["task_id"])
        cases = (
            {"expected_status": "blocked"},
            {"expected_board": "other"},
            {"task_id": "t_nonexistent"},
            {"expected_parent_ids": []},
        )
        for changes in cases:
            ok, reason = _recover(conn, evidence, **changes)
            assert ok is False and isinstance(reason, str)
            assert _snapshot(conn, evidence["task_id"]) == before


def test_triage_cas_rejects_changed_parent_gate_and_expected_landing_status(kanban_home):
    with kbc.connect_closing() as conn:
        evidence = _looped_task(conn)
        parent_id = evidence["parent_ids"][0]
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (parent_id,))
        before = _snapshot(conn, evidence["task_id"])
        ok, reason = _recover(conn, evidence, expected_landing_status="ready")
        assert ok is False and isinstance(reason, str)
        assert _snapshot(conn, evidence["task_id"]) == before
        ok, result = _recover(conn, evidence, expected_landing_status="todo")
        assert ok is True and result["status"] == "todo"


def test_triage_cas_rejects_active_task_or_run_ownership_without_mutation(kanban_home):
    task_fields = (
        ("claim_lock", "lease"),
        ("claim_expires", 2_000_000_000),
        ("current_run_id", 999),
        ("worker_pid", 4242),
    )
    for field, value in task_fields:
        with kbc.connect_closing() as conn:
            evidence = _looped_task(conn)
            conn.execute(f"UPDATE tasks SET {field}=? WHERE id=?", (value, evidence["task_id"]))
            before = _snapshot(conn, evidence["task_id"])
            ok, reason = _recover(conn, evidence)
            assert ok is False and isinstance(reason, str)
            assert _snapshot(conn, evidence["task_id"]) == before

    run_fields = (
        ("ended_at", None),
        ("status", "running"),
    )
    for field, value in run_fields:
        with kbc.connect_closing() as conn:
            evidence = _looped_task(conn)
            conn.execute(f"UPDATE task_runs SET {field}=? WHERE id=?", (value, evidence["run_id"]))
            before = _snapshot(conn, evidence["task_id"])
            ok, reason = _recover(conn, evidence)
            assert ok is False and isinstance(reason, str)
            assert _snapshot(conn, evidence["task_id"]) == before


def test_triage_cas_recovers_despite_reclaimed_worker_fingerprint_residue(kanban_home):
    # Live incident shape: a reclaimed run leaves the forensic worker_started_at
    # fingerprint ("session:pid|boot") on the triage row while worker_pid, claim
    # and current_run_id are NULL. Residue is not active ownership; the ended/
    # active task_runs checks carry that load instead.
    with kbc.connect_closing() as conn:
        evidence = _looped_task(conn)
        conn.execute(
            "UPDATE tasks SET worker_started_at=? WHERE id=?",
            ("897bfc55-040b-49b2-b12c-e7df5bdb0fa4:2343|5351661", evidence["task_id"]),
        )
        ok, result = _recover(conn, evidence)
        assert ok is True
        assert result["status"] == "ready"
        residue = conn.execute(
            "SELECT worker_started_at FROM tasks WHERE id=?", (evidence["task_id"],)
        ).fetchone()[0]
        assert residue == "897bfc55-040b-49b2-b12c-e7df5bdb0fa4:2343|5351661"


def test_triage_cas_race_allows_only_one_recovery_and_replay_is_noop(kanban_home):
    with kbc.connect_closing() as conn:
        evidence = _looped_task(conn)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def attempt():
        try:
            barrier.wait()
            with kbc.connect_closing(board=evidence["board"]) as conn:
                results.append(_recover(conn, evidence))
        except Exception as exc:  # surfaced below; a lock race must not be hidden
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert sum(ok for ok, _ in results) == 1

    with kbc.connect_closing(board=evidence["board"]) as conn:
        after_first = _snapshot(conn, evidence["task_id"])
        ok, reason = _recover(conn, evidence)
        assert ok is False and isinstance(reason, str)
        assert _snapshot(conn, evidence["task_id"]) == after_first
        assert sum(e["kind"] == "triage_recovered" for e in after_first["events"]) == 1


def test_recovery_cli_requires_explicit_board_and_event_bound_arguments():
    parser = argparse.ArgumentParser(prog="hermes")
    kanban_cli.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args([
        "kanban", "--board", "re-bound", "recover-triage", "t_fdec8e64",
        "--expected-board", "re-bound",
        "--expected-status", "triage",
        "--expected-block-kind", "needs_input",
        "--expected-block-loop-event-id", "17413",
        "--expected-latest-event-id", "17413",
        "--expected-run-id", "786",
        "--expected-parent-ids", '["t_b396726d","t_dc1cee4b"]',
        "--expected-landing-status", "ready",
        "--reason", "verified exact event and ended run",
        "--resolution", "maintainer supplied bounded rework direction",
    ])
    assert args.kanban_action == "recover-triage"
    assert args.board == args.expected_board == "re-bound"
    assert args.task_id == "t_fdec8e64"
    assert args.expected_status == "triage"
    assert args.expected_block_kind == "needs_input"
    assert args.expected_block_loop_event_id == 17413
    assert args.expected_latest_event_id == 17413
    assert args.expected_run_id == 786
    assert args.resolution == "maintainer supplied bounded rework direction"


@pytest.mark.parametrize("block_kind", ["capability", "needs_input"])
@pytest.mark.parametrize(
    "context_env",
    ["HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_CRON_SESSION"],
)
def test_recovery_cli_rejects_non_operator_context_before_cas(
    kanban_home, monkeypatch, block_kind, context_env,
):
    with kbc.connect_closing() as conn:
        evidence = _looped_task(conn, block_kind=block_kind)
        before = _snapshot(conn, evidence["task_id"])
    resolution = "Maintainer authorized bounded rework." if block_kind == "needs_input" else None
    args = argparse.Namespace(
        board=evidence["board"], task_id=evidence["task_id"],
        expected_board=evidence["board"], expected_status="triage",
        expected_block_kind=block_kind,
        expected_block_loop_event_id=evidence["event_id"],
        expected_latest_event_id=evidence["latest_event_id"],
        expected_run_id=evidence["run_id"],
        expected_parent_ids_json=json.dumps(evidence["parent_ids"]),
        expected_landing_status="ready", reason="operator approved",
        resolution=resolution, json=True,
    )
    monkeypatch.setenv(context_env, "1")
    assert kanban_cli._cmd_recover_triage(args) != 0
    with kbc.connect_closing() as conn:
        assert _snapshot(conn, evidence["task_id"]) == before


def test_recovery_cli_records_needs_input_resolution(kanban_home, capsys):
    with kbc.connect_closing(board="default") as conn:
        evidence = _looped_task(conn, block_kind="needs_input")
    resolution = "Maintainer authorized bounded rework; visual approval remains pending."
    args = argparse.Namespace(
        board=evidence["board"], task_id=evidence["task_id"],
        expected_board=evidence["board"], expected_status="triage",
        expected_block_kind="needs_input",
        expected_block_loop_event_id=evidence["event_id"],
        expected_latest_event_id=evidence["latest_event_id"],
        expected_run_id=evidence["run_id"],
        expected_parent_ids_json=json.dumps(evidence["parent_ids"]),
        expected_landing_status="ready", reason="operator reviewed the hold",
        resolution=resolution, json=True,
    )

    assert kanban_cli._cmd_recover_triage(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    assert payload["block_kind"] == "needs_input"
    assert payload["resolution"] == resolution
    with kbc.connect_closing(board="default") as conn:
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? "
            "AND kind='triage_recovered'",
            (evidence["task_id"],),
        ).fetchone()
        assert json.loads(event["payload"])["resolution"] == resolution

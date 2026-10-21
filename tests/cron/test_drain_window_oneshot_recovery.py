"""Regression: one-shot cron jobs deferred by a PLANNED gateway drain blackout.

Incident 2026-10-13 (job ``dde61f9a3066``): a one-shot due inside a graceful-restart drain
was never processed (the ``can_dispatch`` gate freezes the whole due scan), and the successor
process's first scan retired it for exceeding the 120s grace window — zero runs, no delivery,
only a local tombstone. These tests pin the fixed contract: the blackout is recorded per cron
store, and the successor scan restores a drain-deferred one-shot as due exactly once, while
legacy expiry, receipts (claims / executions ledger), recurring catch-up and manual runs are
unchanged. No dispatcher, no model: the due-scan admission decision (``get_due_jobs``) is the
fixed surface; dispatch itself already runs behind ``run_claim``.
"""
from datetime import UTC, datetime, timedelta

import pytest

from cron import jobs as cj

T0 = datetime(2026, 10, 13, 11, 0, 0, tzinfo=UTC)  # 20:00 KST, incident shape


def _oneshot(jid, run_at, **extra):
    job = {
        "id": jid, "name": jid, "prompt": "verify",
        "schedule": {"kind": "once", "run_at": run_at.isoformat()},
        "next_run_at": run_at.isoformat(), "last_run_at": None,
        "enabled": True, "state": "scheduled",
        "repeat": {"times": 1, "completed": 0},
        "deliver": "origin", "run_claim": None, "fire_claim": None,
    }
    job.update(extra)
    return job


def _interval(jid, next_run, minutes=30):
    return {
        "id": jid, "name": jid, "prompt": "watch",
        "schedule": {"kind": "interval", "minutes": minutes, "display": f"every {minutes}m"},
        "next_run_at": next_run.isoformat(), "last_run_at": None,
        "enabled": True, "state": "scheduled",
        "repeat": {"times": None, "completed": 0},
        "deliver": "origin", "run_claim": None, "fire_claim": None,
    }


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Isolated cron store + fixed clock + isolated executions ledger."""
    clock = {"v": T0}
    monkeypatch.setattr(cj, "_hermes_now", lambda: clock["v"])
    from cron import executions
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    with cj.use_cron_store(tmp_path):
        yield clock


def _windows(tmp_path):
    import json
    path = tmp_path / "cron" / "drain_windows.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _ledger_row(job_id, instant, status):
    from cron.executions import _transaction
    from cron.occurrences import scheduled_instant
    canonical = scheduled_instant(instant)
    claimed = (datetime.fromisoformat(canonical) - timedelta(seconds=30)).isoformat()
    finished = (datetime.fromisoformat(canonical) + timedelta(seconds=30)).isoformat()
    with _transaction() as conn:
        conn.execute(
            "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at, "
            "finished_at, scheduled_instant) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"exec-{job_id}-{status}", str(job_id), "test", "test-proc", 0, status,
             claimed, finished if status == "completed" else None, canonical))


def _scan():
    """Emulate the admitted SCHEDULER tick scan — the only scan allowed to close a blackout."""
    return cj.get_due_jobs(close_drain_windows=True)


def test_drain_deferred_oneshot_is_recovered_and_admitted_once(store, tmp_path):
    clock = store
    run_at = T0 + timedelta(seconds=539)  # incident due 20:08:59
    cj.save_jobs([_oneshot("c9-verify", run_at)])

    # Drain starts before the due time; the gated tick stamps the blackout.
    clock["v"] = T0 + timedelta(seconds=256)
    from cron.drain_windows import mark_drain_window
    mark_drain_window("gateway drain")

    # Successor scan, 20 min later: the occurrence was never claimed or dispatched, so it is
    # restored as due — not retired, no tombstone.
    clock["v"] = T0 + timedelta(seconds=1200)
    due = [d["id"] for d in _scan()]
    assert due == ["c9-verify"]
    stored = {j["id"]: j for j in cj.load_jobs()}
    assert "c9-verify" in stored
    assert stored["c9-verify"]["run_claim"], "admission must stamp the at-most-once run claim"
    assert not (tmp_path / "cron" / "output" / "c9-verify").exists()

    # Repeated scans never re-admit: the live run_claim (stamped under the jobs lock during the
    # same scan) blocks the second dispatch — fire exactly once.
    assert [d["id"] for d in _scan()] == []

    # The scan that ran with dispatch allowed closed the blackout.
    closed = _windows(tmp_path)
    assert len(closed) == 1 and closed[0]["ended_at"]


def test_normal_expiry_without_window_is_unchanged(store, tmp_path):
    clock = store
    cj.save_jobs([_oneshot("plain-late", T0 - timedelta(seconds=600))])
    clock["v"] = T0
    assert [d["id"] for d in _scan()] == []
    assert "plain-late" not in {j["id"] for j in cj.load_jobs()}
    diags = list((tmp_path / "cron" / "output" / "plain-late").glob("*.md"))
    assert diags and "outside grace window" in diags[0].read_text(encoding="utf-8")


def test_window_does_not_resurrect_dispatched_attempt(store, tmp_path):
    """Ledger receipt (crashed attempt, claim since reaped) blocks recovery: an uncertain
    external effect is never re-run; the legacy tombstone retire runs instead."""
    clock = store
    run_at = T0 + timedelta(seconds=539)
    cj.save_jobs([_oneshot("half-ran", run_at)])
    clock["v"] = T0 + timedelta(seconds=256)
    from cron.drain_windows import mark_drain_window
    mark_drain_window("gateway drain")
    _ledger_row("half-ran", run_at.isoformat(), "unknown")
    clock["v"] = T0 + timedelta(seconds=1200)
    assert [d["id"] for d in _scan()] == []
    assert "half-ran" not in {j["id"] for j in cj.load_jobs()}
    assert list((tmp_path / "cron" / "output" / "half-ran").glob("*.md"))


def test_live_claim_skips_even_inside_window(store):
    clock = store
    run_at = T0 + timedelta(seconds=539)
    cj.save_jobs([_oneshot("in-flight", run_at,
                           run_claim={"at": (T0 + timedelta(seconds=1195)).isoformat(),
                                      "by": "test:1"})])
    clock["v"] = T0 + timedelta(seconds=256)
    from cron.drain_windows import mark_drain_window
    mark_drain_window("gateway drain")
    clock["v"] = T0 + timedelta(seconds=1200)
    assert [d["id"] for d in _scan()] == []
    assert "in-flight" in {j["id"] for j in cj.load_jobs()}  # record kept, never fired twice


def test_completed_occurrence_is_never_recovered(store, tmp_path):
    clock = store
    run_at = T0 + timedelta(seconds=539)
    cj.save_jobs([_oneshot("already-done", run_at)])
    clock["v"] = T0 + timedelta(seconds=256)
    from cron.drain_windows import mark_drain_window
    mark_drain_window("gateway drain")
    _ledger_row("already-done", run_at.isoformat(), "completed")
    clock["v"] = T0 + timedelta(seconds=1200)
    assert [d["id"] for d in _scan()] == []
    # Dedup skips the slot on the completed receipt; nothing re-dispatches and nothing retires.
    assert not (tmp_path / "cron" / "output" / "already-done").exists()
    assert "already-done" in {j["id"] for j in cj.load_jobs()}


def test_repeated_restart_cycles_fire_exactly_once(store):
    clock = store
    run_at = T0 + timedelta(seconds=539)
    cj.save_jobs([_oneshot("flappy", run_at)])
    from cron.drain_windows import close_open_drain_windows, mark_drain_window
    mark_drain_window("gateway drain")
    clock["v"] = T0 + timedelta(seconds=1200)
    assert [d["id"] for d in _scan()] == ["flappy"]  # one admission
    # Restart loops: new blackouts stamp, later scans must not re-admit the same occurrence.
    for step in range(1, 4):
        mark_drain_window("gateway drain")
        clock["v"] = T0 + timedelta(seconds=1200 + 120 * step)
        assert [d["id"] for d in _scan()] == []
    stored = {j["id"]: j for j in cj.load_jobs()}
    assert stored["flappy"]["run_claim"]  # exactly one durable admission remains


def test_interval_catchup_and_manual_run_unchanged_under_window(store):
    clock = store
    manual_at = T0 + timedelta(seconds=500)
    rec = _interval("watch", T0 + timedelta(seconds=781))
    cj.save_jobs([rec, _oneshot("manual-ok", manual_at, manual_run_at=manual_at.isoformat())])
    clock["v"] = T0 + timedelta(seconds=400)
    from cron.drain_windows import mark_drain_window
    mark_drain_window("gateway drain")
    clock["v"] = T0 + timedelta(seconds=1200)
    due = [d["id"] for d in _scan()]
    assert "watch" in due and "manual-ok" in due
    assert {j["id"] for j in cj.load_jobs()} >= {"watch", "manual-ok"}


def test_gated_tick_stamps_the_window(store, tmp_path, monkeypatch):
    """Wiring: the skip branch of the admitted tick records the blackout (store-scoped)."""
    clock = store
    from cron import scheduler as _sched
    from cron import scheduler_tick
    cron_dir = tmp_path / "cron"
    for name, value in [
        ("_should_yield_tick_to_fresh_gateway", lambda: None),
        ("_get_lock_paths", lambda: (cron_dir, cron_dir / "tick.lock")),
        ("_ensure_cron_dir", lambda p: None),
        ("_acquire_tick_lock", lambda f: 999),
        ("_release_tick_lock", lambda fd: None),
        ("_maybe_reap_dead_owners", lambda: None),
        ("_maybe_run_worktree_maintenance", lambda: None),
    ]:
        monkeypatch.setattr(_sched, name, value)
    import cron.bot_chat_legacy as legacy
    monkeypatch.setattr(legacy, "drain_legacy_pending", lambda: None)
    assert scheduler_tick._tick_admitted(verbose=False, adapters=None, loop=None, sync=True,
                                         can_dispatch=lambda: False) == 0
    windows = _windows(tmp_path)
    assert windows and windows[0]["started_at"] == clock["v"].isoformat()


# --- Review-round guards (independent review 2026-10-13, request_changes) ---


def test_stale_closed_window_does_not_revive_ancient_job(store, tmp_path):
    """A run time inside a window that closed DAYS ago is ordinary expiry (manual edit /
    host-sleep shape), not drain debt: closed windows only count within one grace window of
    their own close, so this retires exactly like the no-window case."""
    clock = store
    from cron.drain_windows import mark_drain_window
    mark_drain_window("gateway drain")
    clock["v"] = T0 + timedelta(seconds=300)
    _scan()  # the admitted scan closes the blackout
    # A week later, a job whose stored run time lands inside that long-closed window.
    clock["v"] = T0 + timedelta(days=7)
    cj.save_jobs([_oneshot("ancient", T0 + timedelta(seconds=100))])
    assert [d["id"] for d in _scan()] == []
    assert "ancient" not in {j["id"] for j in cj.load_jobs()}
    diags = list((tmp_path / "cron" / "output" / "ancient").glob("*.md"))
    assert diags and "outside grace window" in diags[0].read_text(encoding="utf-8")


def test_side_caller_get_due_jobs_does_not_close_the_window(store, tmp_path):
    """CLI/diagnostic get_due_jobs() calls must not end a live blackout: an early close would
    split the window and hide the freeze the successor's recovery depends on."""
    clock = store
    from cron.drain_windows import mark_drain_window
    mark_drain_window("gateway drain")
    clock["v"] = T0 + timedelta(seconds=300)
    cj.get_due_jobs()  # side call: scan runs, blackout stays open
    w = _windows(tmp_path)
    assert w and not w[0].get("ended_at"), "side call closed a live blackout"
    _scan()
    w = _windows(tmp_path)
    assert w and w[0].get("ended_at"), "admitted scan must close the blackout"


def test_second_recovery_attempt_retires_with_diagnostic(store, tmp_path):
    """Crash between recovery and any ledger receipt: the outcome is unprovable, so the next
    eligible scan chooses at-most-once — retire with the standard diagnostic instead of
    re-dispatching forever behind a poison dispatcher."""
    clock = store
    run_at = T0 + timedelta(seconds=539)
    cj.save_jobs([_oneshot("poisoned", run_at)])
    clock["v"] = T0 + timedelta(seconds=256)
    from cron.drain_windows import mark_drain_window
    mark_drain_window("gateway drain")
    clock["v"] = T0 + timedelta(seconds=1200)
    assert [d["id"] for d in _scan()] == ["poisoned"]  # recovery #1 (run_claim stamped)
    jobs_now = cj.load_jobs()
    assert jobs_now[0]["drain_recoveries"] == 1, "the recovery marker must persist"
    # Simulate crash + claim reap: claim gone, still no ledger row for the occurrence.
    for j in jobs_now:
        j["run_claim"] = None
    cj.save_jobs(jobs_now)
    clock["v"] = T0 + timedelta(seconds=1290)  # inside the closed window's recency cap
    assert [d["id"] for d in _scan()] == []  # recovery #2 refused
    assert "poisoned" not in {j["id"] for j in cj.load_jobs()}
    assert list((tmp_path / "cron" / "output" / "poisoned").glob("*.md"))


def test_pre_freeze_offset_is_bounded_below_grace(store, tmp_path):
    """A run time MORE than ~1 tick (90s) before the stamp was already missed by pre-drain
    scans — ordinary scheduler lateness, NOT drain debt. The old full-grace (120s) back-off
    would have revived it; the cap keeps the legacy retire."""
    clock = store
    run_at = T0 + timedelta(seconds=400)
    cj.save_jobs([_oneshot("pre-late", run_at)])
    clock["v"] = T0 + timedelta(seconds=500)  # due 100s before the stamp (> 90s cap)
    from cron.drain_windows import mark_drain_window
    mark_drain_window("gateway drain")
    clock["v"] = T0 + timedelta(seconds=1300)
    assert [d["id"] for d in _scan()] == []
    assert "pre-late" not in {j["id"] for j in cj.load_jobs()}
    diags = list((tmp_path / "cron" / "output" / "pre-late").glob("*.md"))
    assert diags and "outside grace window" in diags[0].read_text(encoding="utf-8")


# --- Pre-merge safety review round 3 (2026-10-14): abandoned blackout + pre-tick exit ---


def test_abandoned_open_window_does_not_revive_ancient_job(store, tmp_path, caplog):
    """Drain starts, the process dies, the window never closes. Days later a scan must NOT
    revive the one-shot due inside that abandoned blackout: an OPEN window is only trusted up
    to a safety cap, and the refusal must be an explicit missed-job diagnostic naming the
    stale window — not a plain silent expiry."""
    import logging
    clock = store
    from cron.drain_windows import mark_drain_window
    mark_drain_window("gateway drain")  # open since T0, never closed
    cj.save_jobs([_oneshot("abandoned", T0 + timedelta(seconds=120))])
    clock["v"] = T0 + timedelta(days=3)
    with caplog.at_level(logging.WARNING, logger="cron.jobs"):
        assert [d["id"] for d in _scan()] == [], "a 3-day-old blackout must not auto-recover"
    assert "abandoned" not in {j["id"] for j in cj.load_jobs()}
    diags = list((tmp_path / "cron" / "output" / "abandoned").glob("*.md"))
    assert diags and "outside grace window" in diags[0].read_text(encoding="utf-8")
    assert any("drain window" in r.getMessage().lower() and "stale" in r.getMessage().lower()
               for r in caplog.records), "the recovery refusal must be explicit"


def test_two_hour_drain_still_recovers_under_open_window_cap(store, tmp_path):
    """The cap must keep the real thing: ~20-minute drains (the 2026-10-13 shape) and even a
    2-hour planned blackout are legitimate while the process comes back — far under the
    abandoned-window cap, so recovery still applies."""
    clock = store
    run_at = T0 + timedelta(seconds=539)
    cj.save_jobs([_oneshot("slow-drain", run_at)])
    from cron.drain_windows import mark_drain_window
    mark_drain_window("gateway drain")
    clock["v"] = T0 + timedelta(hours=2)
    assert [d["id"] for d in _scan()] == ["slow-drain"]


def test_drain_start_stamps_before_any_cron_tick(store, tmp_path):
    """Process exits right after request_restart(), before the next cron tick ever runs: the
    gated tick's stamp never happened, so the drain START itself must stamp eagerly — the
    occurrence due inside that gap is recovered by the successor scan. The gated tick's own
    stamp is idempotent inside the same blackout (one window per drain cycle)."""
    clock = store
    from cron.drain_windows import mark_drain_window, mark_drain_windows_for_drain
    assert mark_drain_windows_for_drain("restart-requested") == 1
    mark_drain_window("gateway drain")  # same blackout, gated tick later: no second window
    assert len(_windows(tmp_path)) == 1
    cj.save_jobs([_oneshot("gap-job", T0 + timedelta(seconds=60))])
    clock["v"] = T0 + timedelta(seconds=1200)  # successor process, first scan
    assert [d["id"] for d in _scan()] == ["gap-job"]
    assert _windows(tmp_path)[0].get("ended_at"), "the recovery scan must close the blackout"


def test_drain_start_marks_every_multiplexed_store(tmp_path):
    """A multiplex ticker serves several profile stores; the drain-start stamp must record for
    every store it visits (same (name, home) pairs the ticker gets), not just the process' own."""
    import json
    from cron.drain_windows import mark_drain_windows_for_drain
    homes = [("p1", tmp_path / "p1"), ("p2", tmp_path / "p2")]
    assert mark_drain_windows_for_drain("restart-requested", profile_homes=homes) == 2
    for _name, home in homes:
        data = json.loads((home / "cron" / "drain_windows.json").read_text(encoding="utf-8"))
        assert len(data) == 1 and not data[0].get("ended_at")


# --- Production integration guard: the pre-existing store-outage recovery contract
#     (store_health.outage_covers / end_recovery_window) and the new drain-blackout
#     recovery must coexist, each occurrence firing exactly once. ---


def test_outage_recovery_and_drain_recovery_coexist_exactly_once(store, tmp_path):
    """A one-shot covered by BOTH a recorded store outage and a drain blackout stays due via
    the outage gate (checked first, untouched production semantics), takes its normal-path
    run_claim, and the blackout path must never revive it again afterwards."""
    import errno
    clock = store
    run_at = T0 + timedelta(seconds=539)
    cj.save_jobs([_oneshot("both-covers", run_at)])
    from cron import store_health
    from cron.drain_windows import mark_drain_window
    clock["v"] = T0 + timedelta(seconds=256)
    mark_drain_window("gateway drain")
    store_health.note_unwritable(OSError(errno.ENOSPC, "no space left"), "test", "scan",
                                 cron_dir=tmp_path / "cron")
    # outage_covers compares epoch seconds; align the outage with the simulated clock.
    key = store_health._key(tmp_path / "cron")
    with store_health._lock:
        store_health._degraded[key].since = (T0 + timedelta(seconds=300)).timestamp()
    clock["v"] = T0 + timedelta(seconds=1200)
    assert [d["id"] for d in _scan()] == ["both-covers"]
    stored = {j["id"]: j for j in cj.load_jobs()}
    assert stored["both-covers"]["run_claim"], "normal due path must stamp the at-most-once claim"
    assert not stored["both-covers"].get("drain_recoveries"), \
        "the outage gate answered first; the drain marker must not also be spent"
    # Later scans: live claim blocks every path — no double fire, no double revive.
    assert [d["id"] for d in _scan()] == []
    assert "both-covers" in {j["id"] for j in cj.load_jobs()}
    store_health._degraded.pop(key, None)

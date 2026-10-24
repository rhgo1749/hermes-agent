"""One malformed legacy ``cron/bot_chat_pending`` record must not stop the profile's cron.

``drain_legacy_pending`` runs on every tick before ``get_due_jobs()``. A record missing a
field (or any per-record failure) is set aside as ``ambiguous`` evidence, and the tick
still fires the due job beside it.
"""
import json
from datetime import timedelta

import cron.jobs as J
import cron.scheduler as S
from cron import scheduler_authority


def test_corrupt_legacy_record_is_isolated_and_due_job_still_fires(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "scripts").mkdir(parents=True)
    pending = home / "cron" / "bot_chat_pending"
    pending.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name, value in (("HERMES_DIR", home), ("CRON_DIR", home / "cron"),
                        ("JOBS_FILE", home / "cron" / "jobs.json"),
                        ("OUTPUT_DIR", home / "cron" / "output")):
        monkeypatch.setattr(J, name, value)
    monkeypatch.setattr(S, "_hermes_home", home)
    monkeypatch.setattr(S, "_sweep_mcp_orphans", lambda: None)
    monkeypatch.setattr(scheduler_authority, "reconcile_pending", lambda *, allow_connect=True: None)
    S._running_job_ids.clear()

    corrupt = pending / "corrupt.json"  # a queued record with no ``home``/``content``
    corrupt.write_text(json.dumps({"id": "legacy-1", "status": "queued"}), encoding="utf-8")
    script = home / "scripts" / "fire.sh"
    script.write_text("#!/bin/sh\necho fired\n", encoding="utf-8")
    script.chmod(0o755)
    job = J.create_job(prompt=None, schedule="every 1h", name="script", script="fire.sh",
                       no_agent=True, deliver="local")
    stored = J.load_jobs()
    stored[0]["next_run_at"] = (J._hermes_now() - timedelta(minutes=1)).replace(microsecond=0).isoformat()
    J.save_jobs(stored)

    assert S.tick(verbose=False) == 1
    assert J.get_job(job["id"])["last_status"] == "ok"
    record = json.loads(corrupt.read_text(encoding="utf-8"))
    assert record["status"] == "ambiguous" and record["error"]


def test_legacy_queued_receipts_transfer_in_order_once_and_leave_claimed_alone(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from cron import bot_chat_legacy
    from tools import bot_live_delivery as mailbox

    source = tmp_path / "source"
    target = tmp_path / "original" / "profiles" / "beta"
    moved_target = tmp_path / "renamed" / "profiles" / "beta"
    pending = source / "cron" / "bot_chat_pending"
    target.mkdir(parents=True)
    moved_target.mkdir(parents=True)
    (target / "state.db").touch()
    (moved_target / "state.db").touch()
    pending.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(source))
    monkeypatch.setattr(bot_chat_legacy, "get_hermes_home", lambda: source)
    monkeypatch.setattr("cron.scheduler_delivery.bot_chat_message", lambda _job, content: content)
    warning_policy = iter([True, False])  # setting changed after an admitted warning was deferred
    monkeypatch.setattr("gateway.warning_notifications.warning_notifications_enabled",
                        lambda *_: next(warning_policy))
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective", lambda _path: {})
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda _home: {"session_id": "bot"})
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda _name: moved_target)

    legacy = [
        dict(id="f" * 64, status="queued", job={"id": "older", "name": "Older"},
             content="diagnostic", home=str(target), for_failure=True, sequence=1),
        dict(id="a" * 64, status="claimed", job={"id": "uncertain", "name": "Uncertain"},
             content="possibly sent", home=str(target), for_failure=False, sequence=2),
        dict(id="b" * 64, status="queued", job={"id": "newer", "name": "Newer"},
             content="report", home=str(target), for_failure=False, sequence=3),
        dict(id="c" * 64, status="queued", job={"id": "muted", "name": "Muted"},
             content="obsolete diagnostic", home=str(target), for_failure=True, sequence=4),
    ]
    for record in legacy:
        (pending / f"{record['id']}.json").write_text(json.dumps(record), encoding="utf-8")

    handed_off = []

    def deliver(home, _owner, message, *, delivery_id, notification_category="result"):
        handed_off.append((Path(home).resolve(), delivery_id, notification_category, message))
        return {"status": "queued", "message": message, "delivery_id": delivery_id,
                "notification_category": notification_category}

    monkeypatch.setattr(mailbox, "deliver_to_live_owner", deliver)
    bot_chat_legacy.drain_legacy_pending()
    bot_chat_legacy.drain_legacy_pending()

    assert [(item[1], item[2]) for item in handed_off] == [
        ("f" * 64, "diagnostic"), ("b" * 64, "result")]
    assert all(item[0] == target.resolve() for item in handed_off)
    assert [item[3] for item in handed_off] == ["diagnostic", "report"]
    migrated = [json.loads((pending / f"{record['id']}.json").read_text(encoding="utf-8"))
                for record in legacy]
    assert [record["status"] for record in migrated] == [
        "transferred", "claimed", "transferred", "suppressed"]
    assert [record.get("receipt_status") for record in (migrated[0], migrated[2])] == ["queued", "queued"]

"""Legacy Bot Chat receipts transfer once and retain terminal evidence."""

import json


def test_legacy_receipts_only_transfer_queued_rows_once(tmp_path, monkeypatch):
    from cron.bot_chat_legacy import drain_legacy_pending
    from cron import scheduler_delivery
    from tools import bot_live_delivery

    home = tmp_path / "profile"
    home.mkdir()
    (home / "state.db").write_text("", encoding="utf-8")
    pending = home / "cron" / "bot_chat_pending"
    pending.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    statuses = ("queued", "claimed", "ambiguous", "transferred", "settled", "suppressed")
    originals = {}
    for sequence, status in enumerate(statuses):
        delivery_id = f"receipt-{status}"
        record = {
            "id": delivery_id,
            "status": status,
            "job": {"id": "daily", "name": "Daily"},
            "content": f"payload-{status}",
            "profile": "bot",
            "home": str(home),
            "for_failure": False,
            "sequence": sequence,
        }
        originals[delivery_id] = record
        (pending / f"{delivery_id}.json").write_text(json.dumps(record), encoding="utf-8")

    calls = []
    monkeypatch.setattr(bot_live_delivery, "find_canonical_live_owner", lambda _home: {"session_id": "bot-chat"})
    monkeypatch.setattr(scheduler_delivery, "bot_chat_message", lambda job, content: {"content": content})

    def transfer(_home, owner, message, *, delivery_id, notification_category="result"):
        calls.append((delivery_id, owner["session_id"], message["content"], notification_category))
        return {"status": "queued", "delivery_id": delivery_id}

    monkeypatch.setattr(bot_live_delivery, "deliver_to_live_owner", transfer)

    drain_legacy_pending()
    drain_legacy_pending()

    assert calls == [("receipt-queued", "bot-chat", "payload-queued", "result")]
    for delivery_id, original in originals.items():
        saved = json.loads((pending / f"{delivery_id}.json").read_text(encoding="utf-8"))
        assert saved["id"] == delivery_id
        if original["status"] == "queued":
            assert saved["status"] == "transferred"
            assert saved["receipt_status"] == "queued"
        else:
            assert saved == original

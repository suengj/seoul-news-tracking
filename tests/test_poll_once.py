from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.collector import CollectionResult
from app.commands import poll_once
from app.database import Database
from app.models import TelegramStatus
from app.telegram_sender import TelegramSendOutcome

SEOUL_TZ = ZoneInfo("Asia/Seoul")


class FakeSender:
    """Stand-in for TelegramSender that never touches the network."""

    def __init__(self, settings):
        self.settings = settings
        self.sent_texts: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def send_plain_text(
        self,
        text,
        *,
        chat_id=None,
        reply_to_message_id=None,
        reply_markup=None,
        enforce_send_enabled=True,
    ):
        self.sent_texts.append(text)
        self.sent_chat_ids = getattr(self, "sent_chat_ids", [])
        self.sent_chat_ids.append(chat_id)
        return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_SENT, message_ids=["999"])

    @property
    def sent_source_ids(self) -> list[str]:
        # build_template_alert_message always places `record.original_body`
        # verbatim on its own line; this fixture's bodies are "본문 {source_id}".
        ids = []
        for text in self.sent_texts:
            for line in text.splitlines():
                if line.startswith("본문 "):
                    ids.append(line.removeprefix("본문 "))
        return ids


@pytest.fixture
def env_setup(tmp_path, monkeypatch):
    db_path = tmp_path / "poll_once_test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat")
    monkeypatch.setenv("TELEGRAM_SEND_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    return db_path


def _records(*ids):
    out = []
    for i, source_id in enumerate(ids):
        out.append(_make_bare_record(source_id, i))
    return out


def _make_bare_record(source_id: str, offset: int):
    from app.models import DisasterMessageRecord

    return DisasterMessageRecord(
        source_id=source_id,
        sender_or_region="서울특별시 테스트구",
        sent_at=datetime(2026, 7, 13, 9, offset, tzinfo=SEOUL_TZ),
        original_body=f"본문 {source_id}",
        source_url="https://safecity.seoul.go.kr/news/dist/dust/newsDistDustList.page",
        detected_at=datetime(2026, 7, 13, 9, offset, tzinfo=SEOUL_TZ),
        raw_payload={"disstrSmsSn": source_id},
    )


def _fake_fetch(records):
    def _fetch(**kwargs):
        return CollectionResult(
            records=records,
            method="direct_json_endpoint:/disstr/selectDisstrSms.do",
            fetched_count=len(records),
            full_text_confirmed=True,
        )

    return _fetch


def _add_active_subscription(db_path, *, user_id=111, chat_id=111):
    """v0.4.0: automatic delivery fans out to active personal subscriptions,
    not a single broadcast chat — so a recipient must exist to receive."""
    db = Database(db_path)
    db.set_subscription_status(user_id=user_id, chat_id=chat_id, status="active")
    db.close()


def test_dry_run_makes_no_db_writes_and_sends_nothing(env_setup, monkeypatch, capsys):
    monkeypatch.setattr(poll_once, "fetch_records", _fake_fetch(_records("DS1", "DS2")))
    monkeypatch.setattr(poll_once, "TelegramSender", FakeSender)

    rc = poll_once.main(["--dry-run"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "new records          : 2" in out
    assert "dry-run" in out

    db = Database(env_setup)
    assert db.is_empty
    db.close()


def test_first_run_establishes_baseline_without_send(env_setup, monkeypatch, capsys):
    monkeypatch.setattr(poll_once, "fetch_records", _fake_fetch(_records("DS1", "DS2")))
    fake_sender_holder = {}

    def sender_factory(settings):
        sender = FakeSender(settings)
        fake_sender_holder["sender"] = sender
        return sender

    monkeypatch.setattr(poll_once, "TelegramSender", sender_factory)

    rc = poll_once.main(["--send"])
    assert rc == 0

    db = Database(env_setup)
    assert not db.is_empty
    assert db.known_source_ids() == {"DS1", "DS2"}
    db.close()

    # Baseline run: nothing should have been sent even though --send was passed.
    assert "sender" not in fake_sender_holder


def test_new_records_sent_after_baseline_established(env_setup, monkeypatch):
    # Step 1: establish baseline.
    monkeypatch.setattr(poll_once, "fetch_records", _fake_fetch(_records("DS1", "DS2")))
    monkeypatch.setattr(poll_once, "TelegramSender", FakeSender)
    assert poll_once.main(["--send"]) == 0

    # v0.4.0: register an active personal subscription so the new record has
    # a recipient to fan out to.
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111")
    _add_active_subscription(env_setup)

    # Step 2: next poll sees an overlapping window (DS2 duplicate) plus one new record.
    senders = []

    def sender_factory(settings):
        s = FakeSender(settings)
        senders.append(s)
        return s

    monkeypatch.setattr(poll_once, "fetch_records", _fake_fetch(_records("DS2", "DS3")))
    monkeypatch.setattr(poll_once, "TelegramSender", sender_factory)
    assert poll_once.main(["--send"]) == 0

    assert len(senders) == 1
    assert senders[0].sent_source_ids == ["DS3"]
    # chat_id is stored/returned as TEXT (subscription table affinity)
    assert senders[0].sent_chat_ids == ["111"]

    db = Database(env_setup)
    assert db.known_source_ids() == {"DS1", "DS2", "DS3"}
    # exactly one per-recipient delivery (for DS3), marked sent
    deliveries = db._conn.execute("SELECT * FROM telegram_deliveries").fetchall()
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "sent"
    db.close()


def test_notify_existing_sends_baseline_on_first_run(env_setup, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111")
    monkeypatch.setattr(poll_once, "fetch_records", _fake_fetch(_records("DS1", "DS2")))
    senders = []

    def sender_factory(settings):
        s = FakeSender(settings)
        senders.append(s)
        return s

    monkeypatch.setattr(poll_once, "TelegramSender", sender_factory)
    # An active subscription must exist before this run for baseline delivery.
    _add_active_subscription(env_setup)

    rc = poll_once.main(["--send", "--notify-existing"])
    assert rc == 0
    assert len(senders) == 1
    assert set(senders[0].sent_source_ids) == {"DS1", "DS2"}
    assert senders[0].sent_chat_ids == ["111", "111"]

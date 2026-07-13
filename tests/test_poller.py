from __future__ import annotations

import signal
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import app.commands.poll_once as poll_once_mod
from app.collector import CollectionResult, CollectorError
from app.database import Database
from app.models import DisasterMessageRecord, TelegramStatus
from app.poller import Poller
from app.telegram_sender import TelegramSendOutcome

SEOUL_TZ = ZoneInfo("Asia/Seoul")


class FakeSender:
    def __init__(self, settings):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def send_record(self, record):
        return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_SENT, message_ids=["1"])


def _record(source_id: str) -> DisasterMessageRecord:
    now = datetime.now(tz=SEOUL_TZ)
    return DisasterMessageRecord(
        source_id=source_id,
        sender_or_region="서울특별시 테스트구",
        sent_at=now,
        original_body=f"본문 {source_id}",
        source_url="https://safecity.seoul.go.kr/news/dist/dust/newsDistDustList.page",
        detected_at=now,
        raw_payload={},
    )


def _fake_fetch_counting(counter: dict):
    def _fetch(**kwargs):
        counter["n"] += 1
        return CollectionResult(
            records=[_record(f"DS{counter['n']}")],
            method="m",
            fetched_count=1,
            full_text_confirmed=True,
        )

    return _fetch


@pytest.fixture
def settings(tmp_path, make_settings):
    return make_settings(database_path=tmp_path / "poller.db", poll_interval_seconds=5)


def test_poller_skips_http_when_paused(settings, monkeypatch):
    counter = {"n": 0}
    monkeypatch.setattr(poll_once_mod, "fetch_records", _fake_fetch_counting(counter))
    monkeypatch.setattr(poll_once_mod, "TelegramSender", FakeSender)

    db = Database(settings.database_path)
    db.pause_polling(actor_user_id=1)
    db.close()

    poller = Poller(settings)
    poller.run_forever(max_cycles=1)

    assert counter["n"] == 0


def test_poller_resumes_after_resume(settings, monkeypatch):
    counter = {"n": 0}
    monkeypatch.setattr(poll_once_mod, "fetch_records", _fake_fetch_counting(counter))
    monkeypatch.setattr(poll_once_mod, "TelegramSender", FakeSender)

    db = Database(settings.database_path)
    db.pause_polling(actor_user_id=1)
    db.close()

    poller = Poller(settings)
    poller.run_forever(max_cycles=1)
    assert counter["n"] == 0

    db = Database(settings.database_path)
    db.resume_polling(actor_user_id=1)
    db.close()

    poller.run_forever(max_cycles=1)
    assert counter["n"] == 1


def test_poller_first_cycle_establishes_baseline_without_sending(settings, monkeypatch):
    counter = {"n": 0}
    monkeypatch.setattr(poll_once_mod, "fetch_records", _fake_fetch_counting(counter))
    sent = []

    class RecordingSender(FakeSender):
        def send_record(self, record):
            sent.append(record.source_id)
            return super().send_record(record)

    monkeypatch.setattr(poll_once_mod, "TelegramSender", RecordingSender)

    poller = Poller(settings)
    poller.run_forever(max_cycles=1)

    assert counter["n"] == 1
    assert sent == []  # baseline: collected but not sent

    db = Database(settings.database_path)
    assert not db.is_empty
    db.close()


def test_poller_continues_after_transient_error(settings, monkeypatch):
    calls = {"n": 0}

    def flaky_fetch(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise CollectorError("simulated transient failure")
        return CollectionResult(
            records=[_record("DSX")], method="m", fetched_count=1, full_text_confirmed=True
        )

    monkeypatch.setattr(poll_once_mod, "fetch_records", flaky_fetch)
    monkeypatch.setattr(poll_once_mod, "TelegramSender", FakeSender)

    poller = Poller(settings)
    # First cycle fails (collector error), must not raise / must not crash the poller.
    poller.run_forever(max_cycles=1)
    assert poller._consecutive_errors == 1

    # Second cycle succeeds.
    poller.run_forever(max_cycles=1)
    assert poller._consecutive_errors == 0
    assert calls["n"] == 2


def test_poller_records_poll_error_in_system_state(settings, monkeypatch):
    def failing_fetch(**kwargs):
        raise CollectorError("boom: simulated failure")

    monkeypatch.setattr(poll_once_mod, "fetch_records", failing_fetch)
    monkeypatch.setattr(poll_once_mod, "TelegramSender", FakeSender)

    poller = Poller(settings)
    poller.run_forever(max_cycles=1)

    db = Database(settings.database_path)
    state = db.get_system_state()
    assert state.last_poll_error is not None
    assert "boom" in state.last_poll_error
    db.close()


def test_poller_backoff_after_error_is_bounded(settings, monkeypatch):
    def failing_fetch(**kwargs):
        raise CollectorError("always fails")

    monkeypatch.setattr(poll_once_mod, "fetch_records", failing_fetch)
    monkeypatch.setattr(poll_once_mod, "TelegramSender", FakeSender)

    poller = Poller(settings)
    for _ in range(10):
        wait = poller._run_one_cycle()
    from app.poller import POLLER_MAX_BACKOFF_SECONDS

    assert wait <= POLLER_MAX_BACKOFF_SECONDS


def test_poller_stop_halts_run_forever(settings, monkeypatch):
    counter = {"n": 0}
    monkeypatch.setattr(poll_once_mod, "fetch_records", _fake_fetch_counting(counter))
    monkeypatch.setattr(poll_once_mod, "TelegramSender", FakeSender)

    poller = Poller(settings)
    poller.stop()  # simulate Ctrl+C before the loop even starts
    poller.run_forever()  # must return immediately, not hang
    assert counter["n"] == 0


def test_poller_runs_cleanup_when_due(settings, monkeypatch):
    counter = {"n": 0}
    monkeypatch.setattr(poll_once_mod, "fetch_records", _fake_fetch_counting(counter))
    monkeypatch.setattr(poll_once_mod, "TelegramSender", FakeSender)

    db = Database(settings.database_path)
    old_record = _record("OLDEXPIRED")
    old_record.sent_at = datetime.now(tz=SEOUL_TZ) - timedelta(days=999)
    db.insert(old_record, is_baseline=True)
    db.close()

    poller = Poller(settings)
    poller.run_forever(max_cycles=1)

    db = Database(settings.database_path)
    state = db.get_system_state()
    assert state.last_cleanup_at is not None
    db.close()


def test_install_signal_handlers_wires_sigint_and_sigterm_to_stop(settings):
    poller = Poller(settings)
    original_int, original_term = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
    try:
        poller.install_signal_handlers()
        assert signal.getsignal(signal.SIGINT) == poller.stop
        assert signal.getsignal(signal.SIGTERM) == poller.stop

        assert poller._running is True
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)  # simulate Ctrl+C
        assert poller._running is False
    finally:
        signal.signal(signal.SIGINT, original_int)
        signal.signal(signal.SIGTERM, original_term)

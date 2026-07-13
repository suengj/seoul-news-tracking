from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.commands import cleanup_database, database_status
from app.database import Database

SEOUL_TZ = ZoneInfo("Asia/Seoul")


@pytest.fixture
def env_setup(tmp_path, monkeypatch):
    db_path = tmp_path / "cleanup_test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("MESSAGE_RETENTION_DAYS", "90")
    monkeypatch.setenv("RUN_HISTORY_RETENTION_DAYS", "14")
    monkeypatch.setenv("TOMBSTONE_RETENTION_DAYS", "365")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    return db_path


def _insert_old_record(db_path, days_old: int, source_id: str = "OLD1"):
    db = Database(db_path)
    from app.models import DisasterMessageRecord

    now = datetime.now(tz=SEOUL_TZ)
    record = DisasterMessageRecord(
        source_id=source_id,
        sender_or_region="서울특별시 테스트구",
        sent_at=now - timedelta(days=days_old),
        original_body="오래된 문자",
        source_url="https://safecity.seoul.go.kr/news/dist/dust/newsDistDustList.page",
        detected_at=now - timedelta(days=days_old),
        raw_payload={},
    )
    db.insert(record)
    db.close()


def test_cleanup_dry_run_reports_without_modifying(env_setup, capsys):
    _insert_old_record(env_setup, days_old=200)

    rc = cleanup_database.main(["--dry-run"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "message rows eligible for deletion       : 1" in out
    assert "no changes made" in out.lower()

    db = Database(env_setup)
    assert not db.is_empty  # nothing was actually deleted
    db.close()


def test_cleanup_default_is_dry_run(env_setup, capsys):
    _insert_old_record(env_setup, days_old=200)
    rc = cleanup_database.main([])
    assert rc == 0
    db = Database(env_setup)
    assert not db.is_empty
    db.close()


def test_cleanup_confirm_actually_deletes(env_setup, capsys):
    _insert_old_record(env_setup, days_old=200)

    rc = cleanup_database.main(["--confirm"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "cleanup executed" in out.lower()

    db = Database(env_setup)
    assert db.is_empty
    db.close()


def test_database_status_reports_expected_fields(env_setup, capsys):
    _insert_old_record(env_setup, days_old=5, source_id="RECENT1")

    rc = database_status.main()
    assert rc == 0

    out = capsys.readouterr().out
    assert "total message rows          : 1" in out
    assert "MESSAGE_RETENTION_DAYS      = 90" in out
    assert "polling enabled              : True" in out


def test_database_status_no_secrets_in_output(env_setup, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SUPER_SECRET_TOKEN_XYZ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "SECRET_CHAT_ID_123")
    rc = database_status.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "SUPER_SECRET_TOKEN_XYZ" not in out
    assert "SECRET_CHAT_ID_123" not in out

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.commands import export_history_sample, validate_history_db
from app.history_database import open_history_database
from app.history_models import HistoricalRawRecord


def _make_record(source_id: str) -> HistoricalRawRecord:
    return HistoricalRawRecord(
        source="test",
        source_id=source_id,
        sent_at_raw="2026/07/13 10:00:00",
        sent_at=None,
        sender_raw="관리자",
        region_raw="구",
        title_raw="title",
        body_raw=f"본문 {source_id}",
        list_url="https://example.test/list",
        detail_url=f"https://example.test/detail?sn={source_id}",
        raw_payload={},
        source_page=1,
        source_position=1,
    )


@pytest.fixture
def history_db_env(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "history.db"
    monkeypatch.setenv("HISTORY_DATABASE_PATH", str(db_path))
    return db_path


def test_validate_missing_db_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HISTORY_DATABASE_PATH", str(tmp_path / "does-not-exist.db"))
    exit_code = validate_history_db.main()
    assert exit_code == 1
    assert "FAILED" in capsys.readouterr().out


def test_validate_reports_ok_for_clean_db(history_db_env, capsys):
    with open_history_database(history_db_env) as db:
        for i in range(3):
            db.insert_record(_make_record(str(i)))

    exit_code = validate_history_db.main()
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "OK: no duplicate" in out
    assert "unique record count      : 3" in out


def test_export_sample_writes_jsonl(history_db_env, tmp_path):
    with open_history_database(history_db_env) as db:
        for i in range(5):
            db.insert_record(_make_record(str(i)))

    output_path = tmp_path / "sample.jsonl"
    exit_code = export_history_sample.main(["--count", "3", "--output", str(output_path)])
    assert exit_code == 0

    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for line in lines:
        row = json.loads(line)
        assert "body_raw" in row and row["body_raw"].startswith("본문")

from __future__ import annotations

import sqlite3

import pytest

from app.commands.analyze_template_patterns import analyze


@pytest.fixture
def history_db(tmp_path):
    path = tmp_path / "history_raw.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE historical_raw_messages (
            internal_id INTEGER PRIMARY KEY AUTOINCREMENT,
            body_raw TEXT NOT NULL
        )
        """
    )
    bodies = [
        "금일 05:30 공주시 홍수주의보 발령, 대피 바랍니다. [공주시]",
        "오늘 15시 부로 관내에 발효 중이던 호우주의보가 해제되었습니다. [예천군]",
        "폭염주의보 발효 중. 건강관리에 유의하세요. [곡성군]",
        "열대야로 높은 기온이 이어지겠습니다. [경산시]",
        "금일 17:00 열대야주의보 발효 ▲건강관리 유의 [연천군]",
    ]
    conn.executemany(
        "INSERT INTO historical_raw_messages (body_raw) VALUES (?)", [(b,) for b in bodies]
    )
    conn.commit()
    conn.close()
    return path


def test_analyze_reports_expected_candidate_counts(history_db):
    report = analyze(history_db)
    counts = {t["template_id"]: t["candidate_count"] for t in report["templates"]}
    assert counts["FLOOD_ADVISORY_ISSUED"] == 1
    assert counts["HEAVY_RAIN_CLEARED"] == 1
    assert counts["HEATWAVE_ADVISORY_ISSUED"] == 1
    assert counts["TROPICAL_NIGHT_ADVISORY_ISSUED"] == 1
    assert report["total_records"] == 5


def test_analyze_does_not_modify_the_database(history_db):
    before = history_db.stat().st_mtime_ns
    analyze(history_db)
    conn = sqlite3.connect(history_db)
    count = conn.execute("SELECT COUNT(*) FROM historical_raw_messages").fetchone()[0]
    conn.close()
    assert count == 5
    # Opened read-only: file contents/size must be unchanged.
    after = history_db.stat().st_mtime_ns
    assert before == after


def test_analyze_missing_database_raises_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        analyze(tmp_path / "does_not_exist.db")


def test_analyze_never_returns_the_full_dataset(history_db):
    report = analyze(history_db)
    for entry in report["templates"]:
        assert len(entry["examples"]) <= 10

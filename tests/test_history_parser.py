from __future__ import annotations

from pathlib import Path

import pytest

from app.history_parser import (
    HistoryParseError,
    parse_detail_page,
    parse_list_page,
    parse_sent_at,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "history"


def _read(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_parse_list_page_extracts_rows_in_order():
    parsed = parse_list_page(_read("list_page.html"))
    assert parsed.total_count == 3
    assert [row.source_id for row in parsed.rows] == ["1003", "1002", "1001"]
    assert parsed.rows[0].cell_date == "2026/07/13 12:00:00"
    assert parsed.rows[0].position == 1
    assert "테스트 재난문자 세 번째" in parsed.rows[0].list_body_text


def test_parse_list_page_empty_returns_no_rows():
    parsed = parse_list_page(_read("list_page_empty.html"))
    assert parsed.rows == []
    assert parsed.total_count == 3


def test_parse_list_page_skips_row_without_sn():
    html = """
    <table class="other-type-table"><tbody>
      <tr>
        <td class="cell-no">1</td>
        <td class="board-list-new cell-subject"><a href="/disaster-data/disasterNotificationDetail">no id here</a></td>
        <td class="cell-date">2026/07/13 10:00:00</td>
      </tr>
    </tbody></table>
    """
    parsed = parse_list_page(html)
    assert parsed.rows == []


def test_parse_detail_page_extracts_all_fields_and_preserves_linebreak():
    detail = parse_detail_page(_read("detail_page.html"))
    assert detail.title_raw == "2026/07/13 10:00:00[테스트특별시 테스트구]"
    assert detail.region_raw == "테스트특별시 테스트구"
    assert detail.sender_raw == "관리자"
    assert detail.reg_date_raw == "2026/07/13 10:00:00"
    assert "\n" in detail.body_raw
    assert detail.body_raw.splitlines() == [
        "테스트 재난문자 첫 번째 본문입니다. 줄바꿈 확인:",
        "두 번째 줄입니다. [테스트구]",
    ]


def test_parse_detail_page_missing_body_raises():
    with pytest.raises(HistoryParseError):
        parse_detail_page(_read("detail_page_no_body.html"))


def test_parse_detail_page_empty_body_raises():
    html = """
    <div class="view-header2"><div class="title">2026/07/13 10:00:00[X]</div></div>
    <div class="view-body view-bodyH"><p>   </p></div>
    """
    with pytest.raises(HistoryParseError):
        parse_detail_page(html)


def test_parse_sent_at_valid():
    dt = parse_sent_at("2026/07/13 10:00:00")
    assert dt is not None
    assert dt.year == 2026 and dt.hour == 10
    assert dt.tzinfo is not None


def test_parse_sent_at_invalid_returns_none():
    assert parse_sent_at("not a date") is None
    assert parse_sent_at(None) is None
    assert parse_sent_at("") is None

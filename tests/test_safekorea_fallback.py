from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup

from app.safekorea_fallback import (
    SafeKoreaRuntimeError,
    SafeKoreaSchemaError,
    _extract_body,
    fetch_records,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("app.safekorea_fallback.time.sleep", lambda _seconds: None)


@pytest.fixture
def settings(make_settings):
    return make_settings(safekorea_max_pages=3, safekorea_request_delay_seconds=0)


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_seoul_filtered_list_parses_all_rows(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_fixture_text("safekorea_list.html"))

    records = fetch_records(settings, transport=_transport(handler))
    assert {r.source_id for r in records} == {
        "SAFEKOREA:900001",
        "SAFEKOREA:900002",
        "SAFEKOREA:900003",
    }
    assert records == sorted(records, key=lambda r: r.sent_at)


def test_stable_bbs_sn_used_as_source_id(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_fixture_text("safekorea_list.html"))

    records = fetch_records(settings, transport=_transport(handler))
    ids = {r.raw_payload["bbsSn"] for r in records}
    assert ids == {"900001", "900002", "900003"}


def test_full_body_present_without_detail_fetch(settings):
    """The list HTML already contains the complete body — fetch_records must
    never issue more than one request per page (no detail requests)."""
    request_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        request_count["n"] += 1
        return httpx.Response(200, text=_fixture_text("safekorea_list.html"))

    records = fetch_records(settings, transport=_transport(handler))
    assert request_count["n"] == 1
    body_lengths = {r.source_id: len(r.original_body) for r in records}
    assert all(length > 0 for length in body_lengths.values())


def test_extract_body_inserts_newline_for_br():
    """A row whose body contains <br> tags must keep the lines separated —
    get_text(strip=True) on the raw string would otherwise concatenate them
    with no separator at all (see safekorea_fallback._extract_body)."""
    soup = BeautifulSoup("<a>line one<br/>line two<br/>line three</a>", "html.parser")
    link = soup.find("a")
    assert _extract_body(link) == "line one\nline two\nline three"


def test_multi_region_seoul_record_included(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_fixture_text("safekorea_list.html"))

    records = fetch_records(settings, transport=_transport(handler))
    multi = next(r for r in records if r.source_id == "SAFEKOREA:900003")
    assert "서울특별시 테스트구" in multi.sender_or_region


_DATED_ROW_TEMPLATE = (
    '<tr><td>강풍</td><td class="tit">'
    "<a href=\"javascript:onSubmit('{sn}');\">본문{sn}</a>"
    "<p> ㆍ&nbsp;발송일시 : {sent_at} ㆍ&nbsp;긴급단계 : 안전안내"
    "  ㆍ&nbsp;송출지역 : 서울특별시 테스트구 </p></td></tr>"
)


def test_records_older_than_start_date_lower_bound_are_dropped(settings):
    """Same defensive guard as app/mois_api.py's crtDt check, for SafeKorea's
    analogous startDate/endDate window params: if the site ever returns a
    row outside the requested window (the exact failure class observed live
    on the MOIS primary — see app/mois_api.py), it must be dropped rather
    than auto-delivered as a fresh alert. The "fresh" row's date is computed
    from the real clock (1 day back) so this test doesn't itself depend on a
    pinned clock and stays valid regardless of when it runs."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    fresh_dt = datetime.now(tz=ZoneInfo("Asia/Seoul")) - timedelta(days=1)
    page_html = (
        '<div class="board-list"><table><tbody>'
        + _DATED_ROW_TEMPLATE.format(sn="900020", sent_at="2023/09/16 11:42:02")
        + _DATED_ROW_TEMPLATE.format(sn="900021", sent_at=fresh_dt.strftime("%Y/%m/%d %H:%M:%S"))
        + "</tbody></table></div>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page_html)

    records = fetch_records(settings, transport=_transport(handler))
    assert [r.source_id for r in records] == ["SAFEKOREA:900021"]


def test_empty_result_is_not_an_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_fixture_text("safekorea_list_empty.html"))

    records = fetch_records(settings, transport=_transport(handler))
    assert records == []


def test_pagination_stops_on_short_page(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["currentPage"]
        if page == "1":
            return httpx.Response(200, text=_fixture_text("safekorea_list.html"))
        return httpx.Response(200, text=_fixture_text("safekorea_list_empty.html"))

    records = fetch_records(settings, transport=_transport(handler))
    assert len(records) == 3


# Plain str.format() (not an f-string) so the embedded single-quoted JS
# literal inside a double-quoted HTML attribute never needs a backslash
# escape — Python <3.12 disallows backslashes inside f-strings.
_ROW_TEMPLATE = (
    '<tr><td>강풍</td><td class="tit">'
    "<a href=\"javascript:onSubmit('{sn}');\">본문{sn}</a>"
    "<p> ㆍ&nbsp;발송일시 : 2026/07/14 10:00:0{minute} ㆍ&nbsp;긴급단계 : 안전안내"
    "  ㆍ&nbsp;송출지역 : 서울특별시 테스트구 </p></td></tr>"
)


def test_repeated_page_loop_guard(settings):
    """A page that always returns the same 10 full rows (never shrinking, no
    empty marker) must be treated as a pagination-loop hard failure, not
    looped on until max_pages silently truncates."""
    full_page_html = (
        '<div class="board-list"><table><tbody>'
        + "".join(_ROW_TEMPLATE.format(sn=i, minute=i) for i in range(10))
        + "</tbody></table></div>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=full_page_html)

    with pytest.raises(SafeKoreaRuntimeError):
        fetch_records(settings, transport=_transport(handler))


def test_max_page_guard(settings, monkeypatch):
    call_count = {"n": 0}

    def make_page(page_no: str) -> str:
        return (
            '<div class="board-list"><table><tbody>'
            + "".join(_ROW_TEMPLATE.format(sn=f"{page_no}-{j}", minute=j) for j in range(10))
            + "</tbody></table></div>"
        )

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        page = request.url.params["currentPage"]
        return httpx.Response(200, text=make_page(page))

    with pytest.raises(SafeKoreaRuntimeError):
        fetch_records(settings, transport=_transport(handler))
    # settings.safekorea_max_pages == 3
    assert call_count["n"] == 3


def test_malformed_sent_at_raises_schema_error(settings):
    html = _fixture_text("safekorea_list.html").replace("2026/07/14 17:13:08", "not-a-date")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    with pytest.raises(SafeKoreaSchemaError):
        fetch_records(settings, transport=_transport(handler))


def test_missing_region_raises_schema_error(settings):
    html = _fixture_text("safekorea_list.html").replace(
        "송출지역 : 서울특별시 테스트구", "송출지역 : "
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    with pytest.raises(SafeKoreaSchemaError):
        fetch_records(settings, transport=_transport(handler))


def test_http_403_raises_runtime_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with pytest.raises(SafeKoreaRuntimeError):
        fetch_records(settings, transport=_transport(handler))


def test_http_429_raises_runtime_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    with pytest.raises(SafeKoreaRuntimeError):
        fetch_records(settings, transport=_transport(handler))


def test_challenge_page_raises_runtime_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>captcha required</body></html>")

    with pytest.raises(SafeKoreaRuntimeError):
        fetch_records(settings, transport=_transport(handler))


def test_timeout_retries_then_succeeds(settings):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise httpx.TimeoutException("boom")
        return httpx.Response(200, text=_fixture_text("safekorea_list_empty.html"))

    records = fetch_records(settings, transport=_transport(handler))
    assert records == []
    assert attempts["n"] == 2


def test_request_delay_used_between_pages(settings, monkeypatch):
    sleep_calls = []
    monkeypatch.setattr("app.safekorea_fallback.time.sleep", lambda s: sleep_calls.append(s))

    full_page_html = (
        '<div class="board-list"><table><tbody>'
        + "".join(_ROW_TEMPLATE.format(sn=i, minute=i) for i in range(10))
        + "</tbody></table></div>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["currentPage"]
        if page == "1":
            return httpx.Response(200, text=full_page_html)  # full page -> pagination continues
        return httpx.Response(200, text=_fixture_text("safekorea_list_empty.html"))

    from dataclasses import replace

    fetch_records(
        replace(settings, safekorea_request_delay_seconds=0.5), transport=_transport(handler)
    )
    assert 0.5 in sleep_calls

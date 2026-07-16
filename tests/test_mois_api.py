from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.mois_api import (
    MoisAuthError,
    MoisConfigError,
    MoisRuntimeError,
    MoisSchemaError,
    fetch_records,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("app.mois_api.time.sleep", lambda _seconds: None)


@pytest.fixture
def settings(make_settings):
    return make_settings(safetydata_service_key="TEST_KEY", safetydata_num_of_rows=20)


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_missing_key_raises_config_error(make_settings):
    settings = make_settings(safetydata_service_key="")
    with pytest.raises(MoisConfigError):
        fetch_records(settings)


def test_missing_key_message_never_includes_a_key_value(make_settings):
    settings = make_settings(safetydata_service_key="")
    with pytest.raises(MoisConfigError) as exc_info:
        fetch_records(settings)
    assert "TEST_KEY" not in str(exc_info.value)


def test_key_is_never_logged_by_our_own_code(settings, caplog):
    """app.mois_api itself must never log the key. httpx's own request-line
    logger (which embeds the full query string, key included) is a separate,
    already-mitigated concern — every real command calls
    app.logging_config.configure_logging(), which pins "httpx"/"httpcore" to
    WARNING for exactly this reason (see the httpx/Telegram-token log leak
    fix); reproduce that here rather than testing raw unconfigured logging."""
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load_fixture("mois_success.json"))

    caplog.set_level("DEBUG")
    fetch_records(settings, transport=_transport(handler))
    assert "TEST_KEY" not in caplog.text


def test_configure_logging_suppresses_httpx_url_logging_end_to_end(settings, caplog):
    """Full regression: app.logging_config.configure_logging() (called by
    every real command) must, by itself, keep the service key out of logs —
    guards against someone loosening the httpx/httpcore level in
    app/logging_config.py without realizing it reopens this leak for the new
    v0.5.0 MOIS/SafeKorea clients too."""
    from app.logging_config import configure_logging

    configure_logging("DEBUG")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load_fixture("mois_success.json"))

    caplog.set_level("DEBUG")
    fetch_records(settings, transport=_transport(handler))
    assert "TEST_KEY" not in caplog.text


def test_key_never_appears_in_source_url_or_raw_payload(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load_fixture("mois_success.json"))

    records = fetch_records(settings, transport=_transport(handler))
    for record in records:
        assert "TEST_KEY" not in record.source_url
        assert "serviceKey" not in record.source_url
        assert "TEST_KEY" not in json.dumps(record.raw_payload, ensure_ascii=False)


def test_success_response_filters_to_seoul_and_sorts_by_sent_at(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load_fixture("mois_success.json"))

    records = fetch_records(settings, transport=_transport(handler))

    # SN 900004 (부산광역시) must be excluded; the other 3 are Seoul-targeted.
    assert [r.source_id for r in records] == ["MOIS:900001", "MOIS:900002", "MOIS:900003"]
    # Sorted ascending by sent_at (page order in the fixture is not sorted).
    assert records[0].sent_at < records[1].sent_at < records[2].sent_at


def test_multi_region_seoul_record_is_included(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load_fixture("mois_success.json"))

    records = fetch_records(settings, transport=_transport(handler))
    multi = next(r for r in records if r.source_id == "MOIS:900003")
    assert "서울특별시 테스트구" in multi.sender_or_region


def test_valid_empty_response_returns_empty_list_not_an_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load_fixture("mois_empty.json"))

    records = fetch_records(settings, transport=_transport(handler))
    assert records == []


def test_result_code_failure_raises_auth_error_for_key_rejection(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load_fixture("mois_error.json"))

    with pytest.raises(MoisAuthError):
        fetch_records(settings, transport=_transport(handler))


def test_http_400_raises_runtime_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    with pytest.raises(MoisRuntimeError):
        fetch_records(settings, transport=_transport(handler))


def test_http_401_raises_auth_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(MoisAuthError):
        fetch_records(settings, transport=_transport(handler))


def test_http_403_raises_auth_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with pytest.raises(MoisAuthError):
        fetch_records(settings, transport=_transport(handler))


def test_http_429_honors_retry_after_then_succeeds(settings):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, text="slow down")
        return httpx.Response(200, json=_load_fixture("mois_empty.json"))

    records = fetch_records(settings, transport=_transport(handler))
    assert records == []
    assert attempts["n"] == 2


def test_http_429_exhausted_retries_raises(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    with pytest.raises(MoisRuntimeError):
        fetch_records(settings, transport=_transport(handler))


def test_http_500_retries_then_succeeds(settings):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            return httpx.Response(500, text="server error")
        return httpx.Response(200, json=_load_fixture("mois_empty.json"))

    records = fetch_records(settings, transport=_transport(handler))
    assert records == []
    assert attempts["n"] == 2


def test_http_500_exhausted_retries_raises(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    with pytest.raises(MoisRuntimeError):
        fetch_records(settings, transport=_transport(handler))


def test_timeout_retries_then_succeeds(settings):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise httpx.TimeoutException("boom")
        return httpx.Response(200, json=_load_fixture("mois_empty.json"))

    records = fetch_records(settings, transport=_transport(handler))
    assert records == []
    assert attempts["n"] == 2


def test_timeout_exhausted_retries_raises(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("boom")

    with pytest.raises(MoisRuntimeError):
        fetch_records(settings, transport=_transport(handler))


def test_invalid_json_raises_runtime_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json", headers={"content-type": "application/json"})

    with pytest.raises(MoisRuntimeError):
        fetch_records(settings, transport=_transport(handler))


def test_non_json_content_type_raises_runtime_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<html>maintenance</html>", headers={"content-type": "text/html"}
        )

    with pytest.raises(MoisRuntimeError):
        fetch_records(settings, transport=_transport(handler))


@pytest.mark.parametrize("missing_field", ["SN", "CRT_DT", "MSG_CN", "RCPTN_RGN_NM"])
def test_missing_required_field_raises_schema_error(settings, missing_field):
    payload = _load_fixture("mois_success.json")
    del payload["body"][0][missing_field]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(MoisSchemaError):
        fetch_records(settings, transport=_transport(handler))


def test_unparseable_crt_dt_raises_schema_error(settings):
    payload = _load_fixture("mois_success.json")
    payload["body"][0]["CRT_DT"] = "not-a-date"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(MoisSchemaError):
        fetch_records(settings, transport=_transport(handler))


def test_body_not_a_list_raises_schema_error(settings):
    payload = _load_fixture("mois_success.json")
    payload["body"] = {"unexpected": "shape"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(MoisSchemaError):
        fetch_records(settings, transport=_transport(handler))


def test_pagination_combines_pages(settings):
    page1 = {
        "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE", "errorMsg": None},
        "numOfRows": 2,
        "pageNo": 1,
        "totalCount": 3,
        "body": [
            {
                "SN": 1,
                "CRT_DT": "2026/07/14 10:00:00",
                "MSG_CN": "본문1",
                "RCPTN_RGN_NM": "서울특별시 테스트구",
                "EMRG_STEP_NM": "안전안내",
                "DST_SE_NM": "강풍",
                "REG_YMD": "2026/07/14 10:00:00",
                "MDFCN_YMD": "2026/07/14 10:00:00",
            },
            {
                "SN": 2,
                "CRT_DT": "2026/07/14 11:00:00",
                "MSG_CN": "본문2",
                "RCPTN_RGN_NM": "서울특별시 테스트구",
                "EMRG_STEP_NM": "안전안내",
                "DST_SE_NM": "강풍",
                "REG_YMD": "2026/07/14 11:00:00",
                "MDFCN_YMD": "2026/07/14 11:00:00",
            },
        ],
    }
    page2 = {
        "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE", "errorMsg": None},
        "numOfRows": 2,
        "pageNo": 2,
        "totalCount": 3,
        "body": [
            {
                "SN": 3,
                "CRT_DT": "2026/07/14 12:00:00",
                "MSG_CN": "본문3",
                "RCPTN_RGN_NM": "서울특별시 테스트구",
                "EMRG_STEP_NM": "안전안내",
                "DST_SE_NM": "강풍",
                "REG_YMD": "2026/07/14 12:00:00",
                "MDFCN_YMD": "2026/07/14 12:00:00",
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page_no = request.url.params["pageNo"]
        return httpx.Response(200, json=page1 if page_no == "1" else page2)

    records = fetch_records(
        replace(settings, safetydata_num_of_rows=2), transport=_transport(handler)
    )
    assert [r.source_id for r in records] == ["MOIS:1", "MOIS:2", "MOIS:3"]


def test_repeated_page_loop_guard(settings):
    page = {
        "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE", "errorMsg": None},
        "numOfRows": 1,
        "pageNo": 1,
        "totalCount": 999,
        "body": [
            {
                "SN": 1,
                "CRT_DT": "2026/07/14 10:00:00",
                "MSG_CN": "본문1",
                "RCPTN_RGN_NM": "서울특별시 테스트구",
                "EMRG_STEP_NM": "안전안내",
                "DST_SE_NM": "강풍",
                "REG_YMD": "2026/07/14 10:00:00",
                "MDFCN_YMD": "2026/07/14 10:00:00",
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        # Server bug: always returns the same single-item page regardless of
        # pageNo, and claims a huge totalCount — a real pagination loop.
        return httpx.Response(200, json=page)

    with pytest.raises(MoisRuntimeError):
        fetch_records(replace(settings, safetydata_num_of_rows=1), transport=_transport(handler))

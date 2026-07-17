from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.collector import (
    METHOD_MOIS_API,
    METHOD_SAFEKOREA_FALLBACK,
    CollectorError,
    fetch_records,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("app.mois_api.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("app.safekorea_fallback.time.sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    """Pin "now" so crt_dt (today - lookback_days) lines up with
    mois_success.json's embedded 2026/07/14 timestamps regardless of the
    real wall clock — see the matching fixture in test_mois_api.py."""
    import datetime as dt

    monkeypatch.setattr(
        "app.mois_api._kst_today",
        lambda: dt.datetime(2026, 7, 15, 12, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=9))),
    )


@pytest.fixture
def settings(make_settings):
    return make_settings(
        safetydata_service_key="TEST_KEY",
        safekorea_fallback_enabled=True,
        safekorea_max_pages=3,
    )


def _mois_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _success_mois_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_load_json("mois_success.json"))


def _empty_mois_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_load_json("mois_empty.json"))


def _failing_mois_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_load_json("mois_error.json"))


def _success_safekorea_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=_text("safekorea_list.html"))


def _empty_safekorea_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=_text("safekorea_list_empty.html"))


def _failing_safekorea_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(403, text="forbidden")


# -- primary success -> no fallback ------------------------------------------


def test_primary_success_with_records_uses_api_no_fallback(settings):
    fallback_called = {"n": 0}

    def safekorea_handler(request: httpx.Request) -> httpx.Response:
        fallback_called["n"] += 1
        return httpx.Response(200, text=_text("safekorea_list.html"))

    result = fetch_records(
        settings,
        mois_transport=_mois_transport(_success_mois_handler),
        safekorea_transport=_mois_transport(safekorea_handler),
    )
    assert result.method == METHOD_MOIS_API
    assert result.fallback_used is False
    assert fallback_called["n"] == 0
    assert len(result.records) == 3


def test_primary_success_zero_records_is_noop_no_fallback(settings):
    fallback_called = {"n": 0}

    def safekorea_handler(request: httpx.Request) -> httpx.Response:
        fallback_called["n"] += 1
        return httpx.Response(200, text=_text("safekorea_list.html"))

    result = fetch_records(
        settings,
        mois_transport=_mois_transport(_empty_mois_handler),
        safekorea_transport=_mois_transport(safekorea_handler),
    )
    assert result.method == METHOD_MOIS_API
    assert result.fallback_used is False
    assert fallback_called["n"] == 0
    assert result.records == []


def test_primary_success_zero_seoul_records_is_noop_no_fallback(settings):
    """All Seoul-filtered out (e.g. only non-Seoul items came back) is still
    a successful no-op, not a trigger for fallback."""
    non_seoul_only = _load_json("mois_success.json")
    non_seoul_only["body"] = [
        item for item in non_seoul_only["body"] if item["SN"] == 900004
    ]  # keep only the 부산광역시 record
    non_seoul_only["totalCount"] = 1
    fallback_called = {"n": 0}

    def mois_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=non_seoul_only)

    def safekorea_handler(request: httpx.Request) -> httpx.Response:
        fallback_called["n"] += 1
        return httpx.Response(200, text=_text("safekorea_list.html"))

    result = fetch_records(
        settings,
        mois_transport=_mois_transport(mois_handler),
        safekorea_transport=_mois_transport(safekorea_handler),
    )
    assert result.records == []
    assert fallback_called["n"] == 0


# -- configuration failure -> fail fast, no fallback -------------------------


def test_missing_key_fails_fast_never_calls_fallback(settings):
    fallback_called = {"n": 0}

    def safekorea_handler(request: httpx.Request) -> httpx.Response:
        fallback_called["n"] += 1
        return httpx.Response(200, text=_text("safekorea_list.html"))

    with pytest.raises(CollectorError):
        fetch_records(
            replace(settings, safetydata_service_key=""),
            safekorea_transport=_mois_transport(safekorea_handler),
        )
    assert fallback_called["n"] == 0


# -- primary hard failure -> fallback -----------------------------------------


def test_primary_hard_failure_triggers_fallback_success(settings):
    result = fetch_records(
        settings,
        mois_transport=_mois_transport(_failing_mois_handler),
        safekorea_transport=_mois_transport(_success_safekorea_handler),
    )
    assert result.method == METHOD_SAFEKOREA_FALLBACK
    assert result.fallback_used is True
    assert result.primary_error_category == "auth_failed"
    assert len(result.records) == 3


def test_primary_hard_failure_triggers_fallback_valid_empty(settings):
    result = fetch_records(
        settings,
        mois_transport=_mois_transport(_failing_mois_handler),
        safekorea_transport=_mois_transport(_empty_safekorea_handler),
    )
    assert result.method == METHOD_SAFEKOREA_FALLBACK
    assert result.fallback_used is True
    assert result.records == []


def test_primary_failure_and_fallback_failure_fails_closed(settings):
    with pytest.raises(CollectorError):
        fetch_records(
            settings,
            mois_transport=_mois_transport(_failing_mois_handler),
            safekorea_transport=_mois_transport(_failing_safekorea_handler),
        )


def test_fallback_disabled_fails_closed_without_attempting_fallback(settings):
    fallback_called = {"n": 0}

    def safekorea_handler(request: httpx.Request) -> httpx.Response:
        fallback_called["n"] += 1
        return httpx.Response(200, text=_text("safekorea_list.html"))

    with pytest.raises(CollectorError):
        fetch_records(
            replace(settings, safekorea_fallback_enabled=False),
            mois_transport=_mois_transport(_failing_mois_handler),
            safekorea_transport=_mois_transport(safekorea_handler),
        )
    assert fallback_called["n"] == 0


@pytest.mark.parametrize(
    "mois_handler,expected_category",
    [
        (_failing_mois_handler, "auth_failed"),
    ],
)
def test_error_category_recorded_on_fallback(settings, mois_handler, expected_category):
    result = fetch_records(
        settings,
        mois_transport=_mois_transport(mois_handler),
        safekorea_transport=_mois_transport(_success_safekorea_handler),
    )
    assert result.primary_error_category == expected_category


def test_timeout_triggers_fallback(settings):
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("boom")

    result = fetch_records(
        settings,
        mois_transport=_mois_transport(timeout_handler),
        safekorea_transport=_mois_transport(_success_safekorea_handler),
    )
    assert result.fallback_used is True
    assert result.primary_error_category == "timeout"


def test_schema_error_triggers_fallback(settings):
    payload = _load_json("mois_success.json")
    del payload["body"][0]["SN"]

    def schema_bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    result = fetch_records(
        settings,
        mois_transport=_mois_transport(schema_bad_handler),
        safekorea_transport=_mois_transport(_success_safekorea_handler),
    )
    assert result.fallback_used is True
    assert result.primary_error_category == "schema_error"


# -- never calls the legacy Seoul SafeCity source -----------------------------


def test_never_requests_legacy_safecity_domain(settings):
    def guard(request: httpx.Request) -> httpx.Response:
        assert "safecity.seoul.go.kr" not in str(request.url)
        return httpx.Response(200, json=_load_json("mois_success.json"))

    fetch_records(settings, mois_transport=_mois_transport(guard))

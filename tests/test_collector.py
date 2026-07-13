from __future__ import annotations


import httpx
import pytest

from app.collector import (
    BlockedOrChallengedError,
    CollectorError,
    EmptyWidgetError,
    fetch_records,
)
from app.config import SOURCE_API_URL


def _sample_payload():
    return {
        "sms": [
            {
                "disstrSmsSn": "DS1",
                "orgnlSn": "1",
                "disstrDate": "2026/07/13 09:00:00",
                "lctnNm": "서울특별시 테스트구",
                "smsMsg": "본문 내용",
            }
        ]
    }


def _transport(handler):
    return httpx.MockTransport(handler)


def test_fetch_records_success_path():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("newsDistDustList.page"):
            return httpx.Response(200, text="<html></html>", headers={"set-cookie": "JSESSIONID=abc; Path=/"})
        if str(request.url) == SOURCE_API_URL:
            assert request.headers.get("x-requested-with") == "XMLHttpRequest"
            return httpx.Response(
                200,
                json=_sample_payload(),
                headers={"content-type": "application/json;charset=UTF-8"},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    result = fetch_records(transport=_transport(handler))
    assert result.fetched_count == 1
    assert result.full_text_confirmed is True
    assert result.records[0].original_body == "본문 내용"


def test_fetch_records_raises_on_403_bootstrap():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    with pytest.raises(BlockedOrChallengedError):
        fetch_records(transport=_transport(handler))


def test_fetch_records_raises_on_403_data_call():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("newsDistDustList.page"):
            return httpx.Response(200, text="<html></html>", headers={"set-cookie": "JSESSIONID=abc; Path=/"})
        return httpx.Response(403, text="Forbidden")

    with pytest.raises(BlockedOrChallengedError):
        fetch_records(transport=_transport(handler))


def test_fetch_records_raises_on_empty_widget():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("newsDistDustList.page"):
            return httpx.Response(200, text="<html></html>", headers={"set-cookie": "JSESSIONID=abc; Path=/"})
        return httpx.Response(200, json={"sms": []}, headers={"content-type": "application/json;charset=UTF-8"})

    with pytest.raises(EmptyWidgetError):
        fetch_records(transport=_transport(handler))


def test_fetch_records_raises_on_missing_cookie():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html></html>")

    with pytest.raises(CollectorError):
        fetch_records(transport=_transport(handler))


def test_fetch_records_raises_on_non_json_content_type():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("newsDistDustList.page"):
            return httpx.Response(200, text="<html></html>", headers={"set-cookie": "JSESSIONID=abc; Path=/"})
        return httpx.Response(200, text="<html>not json</html>", headers={"content-type": "text/html"})

    with pytest.raises(CollectorError):
        fetch_records(transport=_transport(handler))


def test_fetch_records_raises_on_schema_change():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("newsDistDustList.page"):
            return httpx.Response(200, text="<html></html>", headers={"set-cookie": "JSESSIONID=abc; Path=/"})
        return httpx.Response(
            200,
            json={"unexpected_key": []},
            headers={"content-type": "application/json;charset=UTF-8"},
        )

    with pytest.raises(CollectorError):
        fetch_records(transport=_transport(handler))

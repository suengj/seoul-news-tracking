from __future__ import annotations

import httpx
import pytest

from app.history_collector import (
    HistoryCollectorError,
    RateLimitedError,
    build_client,
    fetch_detail_page,
    fetch_list_page,
)


def _client(handler):
    return build_client(timeout_seconds=5, transport=httpx.MockTransport(handler))


def _no_sleep(_seconds: float) -> None:
    pass


def test_fetch_list_page_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["currentPage"] == "1"
        assert request.url.params["cntPerPage"] == "100"
        return httpx.Response(200, text="<html>ok</html>")

    with _client(handler) as client:
        result = fetch_list_page(
            client, current_page=1, cnt_per_page=100, page_size=10, delay_seconds=0, max_retries=2,
            sleep_fn=_no_sleep,
        )
    assert result.html == "<html>ok</html>"
    assert result.retry_count == 0


def test_fetch_detail_page_retries_then_succeeds_on_timeout():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.TimeoutException("boom")
        return httpx.Response(200, text="<html>detail</html>")

    with _client(handler) as client:
        result = fetch_detail_page(
            client, source_id="1", delay_seconds=0, max_retries=3, sleep_fn=_no_sleep
        )
    assert result.html == "<html>detail</html>"
    assert result.retry_count == 2
    assert attempts["n"] == 3


def test_fetch_raises_rate_limited_after_exhausting_retries_on_429():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    with _client(handler) as client:
        with pytest.raises(RateLimitedError) as exc_info:
            fetch_list_page(
                client, current_page=1, cnt_per_page=100, page_size=10, delay_seconds=0,
                max_retries=2, sleep_fn=_no_sleep,
            )
    assert exc_info.value.status_code == 429


def test_fetch_raises_rate_limited_on_403():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with _client(handler) as client:
        with pytest.raises(RateLimitedError) as exc_info:
            fetch_detail_page(
                client, source_id="1", delay_seconds=0, max_retries=1, sleep_fn=_no_sleep
            )
    assert exc_info.value.status_code == 403


def test_fetch_recovers_after_transient_429():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, text="<html>ok</html>")

    with _client(handler) as client:
        result = fetch_list_page(
            client, current_page=1, cnt_per_page=100, page_size=10, delay_seconds=0,
            max_retries=2, sleep_fn=_no_sleep,
        )
    assert result.html == "<html>ok</html>"
    assert result.retry_count == 1


def test_fetch_raises_collector_error_on_other_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    with _client(handler) as client:
        with pytest.raises(HistoryCollectorError):
            fetch_list_page(
                client, current_page=1, cnt_per_page=100, page_size=10, delay_seconds=0,
                max_retries=1, sleep_fn=_no_sleep,
            )


def test_sleep_fn_invoked_on_retry_backoff():
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    with _client(handler) as client:
        with pytest.raises(RateLimitedError):
            fetch_list_page(
                client, current_page=1, cnt_per_page=100, page_size=10, delay_seconds=0,
                max_retries=2, sleep_fn=sleeps.append,
            )
    assert len(sleeps) == 2  # one cooldown sleep between each of the 3 attempts

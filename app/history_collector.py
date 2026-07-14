"""Sequential, polite HTTP client for the safetydata.go.kr historical archive.

No concurrency, no unrelated asset downloads (only the two HTML endpoints
documented in docs/history_source_discovery.md are ever requested). Retries
are bounded with exponential backoff; HTTP 403/429 gets a longer cooldown
rather than being hammered, per docs/history_backfill.md.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from app.config import HISTORY_DETAIL_URL, HISTORY_LIST_URL

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 SeoulNewsTracking-HistoryBackfill/0.1"
)

RATE_LIMIT_COOLDOWN_SECONDS = 30.0
SleepFn = Callable[[float], None]


class HistoryCollectorError(RuntimeError):
    """Raised for any condition that must abort this fetch."""


class RateLimitedError(HistoryCollectorError):
    """Raised when HTTP 403/429 persisted after all retries."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"rate limited / blocked after retries: HTTP {status_code}")


def build_client(
    *, timeout_seconds: float, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    timeout = httpx.Timeout(
        connect=timeout_seconds, read=timeout_seconds, write=timeout_seconds, pool=timeout_seconds
    )
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
        transport=transport,
    )


def _sleep_and_log(seconds: float, sleep_fn: SleepFn, reason: str) -> None:
    logger.warning("%s — sleeping %.1fs before retry", reason, seconds)
    sleep_fn(seconds)


def _get_with_retries(
    client: httpx.Client,
    url: str,
    *,
    params: dict | None,
    delay_seconds: float,
    max_retries: int,
    sleep_fn: SleepFn = time.sleep,
) -> tuple[str, int]:
    """GET with bounded retries. Returns (response_text, retry_count_used).

    Raises RateLimitedError if 403/429 persists past max_retries.
    Raises HistoryCollectorError for any other non-200 status or transport
    failure that persists past max_retries.
    """
    retry_count = 0
    last_exc: Exception | None = None
    last_status: int | None = None

    for attempt in range(max_retries + 1):
        try:
            response = client.get(url, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            retry_count += 1
            if attempt < max_retries:
                _sleep_and_log(
                    delay_seconds * (2**attempt), sleep_fn, f"request to {url} failed: {exc}"
                )
            continue

        if response.status_code == 200:
            return response.text, retry_count

        last_status = response.status_code
        if response.status_code in (403, 429):
            retry_count += 1
            if attempt < max_retries:
                _sleep_and_log(
                    RATE_LIMIT_COOLDOWN_SECONDS * (attempt + 1),
                    sleep_fn,
                    f"HTTP {response.status_code} from {url}",
                )
            continue

        raise HistoryCollectorError(f"unexpected HTTP {response.status_code} from {url}")

    if last_status in (403, 429):
        raise RateLimitedError(last_status)
    raise HistoryCollectorError(f"request to {url} failed after {max_retries} retries: {last_exc}")


@dataclass
class FetchListResult:
    html: str
    retry_count: int


def fetch_list_page(
    client: httpx.Client,
    *,
    current_page: int,
    cnt_per_page: int,
    page_size: int,
    delay_seconds: float,
    max_retries: int,
    sleep_fn: SleepFn = time.sleep,
) -> FetchListResult:
    text, retry_count = _get_with_retries(
        client,
        HISTORY_LIST_URL,
        params={
            "currentPage": current_page,
            "cntPerPage": cnt_per_page,
            "pageSize": page_size,
        },
        delay_seconds=delay_seconds,
        max_retries=max_retries,
        sleep_fn=sleep_fn,
    )
    return FetchListResult(html=text, retry_count=retry_count)


@dataclass
class FetchDetailResult:
    html: str
    retry_count: int


def fetch_detail_page(
    client: httpx.Client,
    *,
    source_id: str,
    delay_seconds: float,
    max_retries: int,
    sleep_fn: SleepFn = time.sleep,
) -> FetchDetailResult:
    text, retry_count = _get_with_retries(
        client,
        HISTORY_DETAIL_URL,
        params={"sn": source_id},
        delay_seconds=delay_seconds,
        max_retries=max_retries,
        sleep_fn=sleep_fn,
    )
    return FetchDetailResult(html=text, retry_count=retry_count)

"""Lightweight HTTP collector for the Seoul SafeCity 재난문자 widget.

Reproduces exactly the two-request flow documented in
docs/source_discovery.md:

1. GET the dashboard page once to obtain a session cookie (JSESSIONID).
2. POST to /disstr/selectDisstrSms.do with that cookie plus
   `X-Requested-With: XMLHttpRequest` to fetch the widget's JSON data.

No browser automation. No unrelated assets (images/CSS/map tiles) are ever
requested.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.config import SOURCE_API_URL, SOURCE_PAGE_URL
from app.models import DisasterMessageRecord
from app.parser import SourceDataError, SourceSchemaError, parse_response

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 SeoulNewsTracking/0.1"
)

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
MAX_RETRIES = 2
BACKOFF_SECONDS = 1.5

SEOUL_TZ = ZoneInfo("Asia/Seoul")


class CollectorError(RuntimeError):
    """Raised for any condition that must abort collection rather than send incomplete data."""


class BlockedOrChallengedError(CollectorError):
    """Raised on HTTP 403/429 or a detected login/challenge page."""


class EmptyWidgetError(CollectorError):
    """Raised when the widget legitimately reports zero records.

    Not necessarily an error in isolation (a day can genuinely have zero
    messages) — callers decide how to treat it; kept as a distinct type so
    the caller can log it explicitly rather than silently treating it as
    "no new records".
    """


@dataclass
class CollectionResult:
    records: list[DisasterMessageRecord]
    method: str
    fetched_count: int
    full_text_confirmed: bool


def _build_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=READ_TIMEOUT, pool=READ_TIMEOUT)
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
        transport=transport,
    )


def _looks_like_challenge_page(text: str) -> bool:
    lowered = text.lower()
    markers = ("captcha", "access denied", "잠시 후 다시 시도", "로그인이 필요", "<title>403")
    return any(marker in lowered for marker in markers)


def _bootstrap_session(client: httpx.Client) -> None:
    """GET the dashboard page once so the server issues a JSESSIONID cookie."""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.get(SOURCE_PAGE_URL)
        except httpx.TimeoutException as exc:
            last_exc = exc
            logger.warning("session bootstrap timed out (attempt %d): %s", attempt + 1, exc)
            continue

        if response.status_code in (403, 429):
            raise BlockedOrChallengedError(
                f"session bootstrap blocked: HTTP {response.status_code}"
            )
        if response.status_code != 200:
            last_exc = CollectorError(f"session bootstrap failed: HTTP {response.status_code}")
            continue
        if not client.cookies.get("JSESSIONID"):
            raise CollectorError("session bootstrap did not yield a JSESSIONID cookie")
        return

    raise CollectorError(f"session bootstrap failed after retries: {last_exc}")


def fetch_records(
    *, source_url: str = SOURCE_PAGE_URL, transport: httpx.BaseTransport | None = None
) -> CollectionResult:
    """Fetch the current 재난문자 widget records via the confirmed JSON endpoint.

    Raises CollectorError (or a subclass) on anything that should abort the
    run: blocked/challenged, non-JSON content-type, schema mismatch, or a
    malformed record. Never returns partial/best-effort data.

    `transport` is exposed only so tests can inject an `httpx.MockTransport`
    instead of hitting the network; production code should never pass it.
    """
    with _build_client(transport) as client:
        _bootstrap_session(client)

        last_exc: Exception | None = None
        response: httpx.Response | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = client.post(
                    SOURCE_API_URL,
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                break
            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning("data fetch timed out (attempt %d): %s", attempt + 1, exc)

        if response is None:
            raise CollectorError(f"data fetch failed after retries: {last_exc}")

        if response.status_code in (403, 429):
            raise BlockedOrChallengedError(f"data fetch blocked: HTTP {response.status_code}")
        if response.status_code != 200:
            raise CollectorError(f"data fetch failed: HTTP {response.status_code}")

        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            if _looks_like_challenge_page(response.text):
                raise BlockedOrChallengedError(
                    "data fetch returned a non-JSON challenge/login page"
                )
            raise CollectorError(f"unexpected content-type: {content_type!r}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise CollectorError(f"data fetch returned invalid JSON: {exc}") from exc

    detected_at = datetime.now(tz=SEOUL_TZ)

    try:
        records = parse_response(payload, source_url=source_url, detected_at=detected_at)
    except (SourceSchemaError, SourceDataError) as exc:
        raise CollectorError(f"response failed schema/data validation: {exc}") from exc

    if not records:
        raise EmptyWidgetError("widget returned zero records")

    return CollectionResult(
        records=records,
        method="direct_json_endpoint:/disstr/selectDisstrSms.do",
        fetched_count=len(records),
        full_text_confirmed=True,
    )

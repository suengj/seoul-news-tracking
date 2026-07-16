"""국민안전24 재난문자 조회 HTML — conditional fallback collector.

Used only within the same poll cycle after a MOIS primary hard runtime
failure (see app/collector.py). Confirmed live contract — see
docs/safekorea_html_fallback_plan.md:

- The list page is fully server-rendered HTML; no JavaScript execution or
  browser automation is required.
- Every list row already contains the complete message body, 발송일시,
  긴급단계, and 송출지역 — no detail-page fetch is needed in normal operation.
- `bbsSn` (from `onSubmit('<id>')`) is a stable identifier and was
  empirically confirmed to equal the MOIS API's `SN` for the same message
  (both systems share the same underlying 긴급재난문자 record), which is the
  basis for cross-source dedup (see app/models.py `strip_source_namespace`).
- `sbLawArea1=1100000000` (Seoul) was confirmed not to drop multi-region
  Seoul records in the inspected window; client-side `is_seoul_recipient()`
  is still applied to every row regardless.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

from app.config import SAFEKOREA_BASE_URL, SAFEKOREA_SEOUL_LAW_AREA, Settings
from app.models import DisasterMessageRecord, is_seoul_recipient

logger = logging.getLogger(__name__)

SEOUL_TZ = ZoneInfo("Asia/Seoul")

ROW_SELECTOR = "div.board-list table tbody tr"
PAGINATION_SELECTOR = "div.pagination"
EMPTY_MARKER_TEXT = "데이터가 존재하지 않습니다"
SENT_AT_FORMAT = "%Y/%m/%d %H:%M:%S"
LOOKBACK_WINDOW_DAYS = 7
PAGE_SIZE = 10

MAX_RETRIES = 2
BACKOFF_BASE_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 8.0

USER_AGENT = "Mozilla/5.0 (compatible; SeoulNewsTracking/0.5; +https://github.com/suengj/seoul-news-tracking)"

CHALLENGE_MARKERS = ("captcha", "access denied", "잠시 후 다시 시도", "로그인이 필요", "<title>403")


class SafeKoreaError(RuntimeError):
    """Base class for all SafeKorea fallback collection errors."""


class SafeKoreaRuntimeError(SafeKoreaError):
    """Hard runtime failure (network, HTTP, challenge page, pagination fault)."""


class SafeKoreaSchemaError(SafeKoreaRuntimeError):
    """A row is missing a required field or has an unparseable value.

    Fail-closed: a single malformed row aborts the whole fallback attempt
    rather than silently sending a partial/guessed record.
    """


def _kst_window() -> tuple[str, str]:
    today = datetime.now(tz=SEOUL_TZ)
    start = today - timedelta(days=LOOKBACK_WINDOW_DAYS)
    return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def _build_params(*, page: int, start_date: str, end_date: str) -> dict[str, str]:
    return {
        "menuSn": "34",
        "bbsSn": "",
        "currentPage": str(page),
        "firstYn": "",
        "searchType": "",
        "cOcrcType": "",
        "dsstrSeId": "",
        "sbLawArea1": SAFEKOREA_SEOUL_LAW_AREA,
        "sbLawArea2": "",
        "sbLawArea3": "",
        "keyword": "",
        "startDate": start_date,
        "endDate": end_date,
        "readYn": "Y",
    }


def _looks_like_challenge_page(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def _row_is_empty_marker(row: Tag) -> bool:
    return EMPTY_MARKER_TEXT in row.get_text(strip=True)


def _extract_body(link: Tag) -> str:
    """Full message body from the row's <a> text, with any <br> becoming a
    newline (some rows carry the newline as a literal character already;
    either way no summarization/truncation is applied). get_text's own
    separator is used rather than replacing <br> tags with a literal "\\n"
    first — get_text(strip=True) strips each text fragment individually
    before joining with an empty separator, which would silently discard a
    manually-inserted "\\n" node and concatenate multi-line bodies with no
    separator at all."""
    return link.get_text("\n", strip=True)


def _extract_meta(paragraph: Tag) -> dict[str, str | None]:
    text = paragraph.get_text(" ", strip=True)
    sent_at_raw: str | None = None
    emergency_step: str | None = None
    region_raw: str | None = None
    for segment in text.split("ㆍ"):
        segment = segment.strip()
        if segment.startswith("발송일시"):
            sent_at_raw = segment.split(":", 1)[-1].strip()
        elif segment.startswith("긴급단계"):
            emergency_step = segment.split(":", 1)[-1].strip()
        elif segment.startswith("송출지역"):
            region_raw = segment.split(":", 1)[-1].strip()
    return {"sent_at": sent_at_raw, "emergency_step": emergency_step, "region": region_raw}


def _parse_sent_at(raw: str) -> datetime:
    try:
        naive = datetime.strptime(raw.strip(), SENT_AT_FORMAT)
    except (ValueError, AttributeError) as exc:
        raise SafeKoreaSchemaError(f"unparseable 발송일시: {raw!r}") from exc
    return naive.replace(tzinfo=SEOUL_TZ)


def _row_to_record(row: Tag, *, detected_at: datetime) -> DisasterMessageRecord:
    disaster_type_cell = row.select_one("td")
    link = row.select_one("td.tit a")
    meta_paragraph = row.select_one("td.tit p")

    if link is None or meta_paragraph is None:
        raise SafeKoreaSchemaError("row missing body link or metadata paragraph")

    href = link.get("href", "")
    if "onSubmit(" not in href:
        raise SafeKoreaSchemaError(f"row link href does not match onSubmit(...) pattern: {href!r}")
    bbs_sn = href.split("onSubmit(", 1)[1].split(")")[0].strip("'\" ;")
    if not bbs_sn:
        raise SafeKoreaSchemaError("row missing bbsSn")

    body = _extract_body(link)
    if not body:
        raise SafeKoreaSchemaError(f"row {bbs_sn} has an empty message body")

    meta = _extract_meta(meta_paragraph)
    if not meta["sent_at"]:
        raise SafeKoreaSchemaError(f"row {bbs_sn} missing 발송일시")
    if not meta["region"]:
        raise SafeKoreaSchemaError(f"row {bbs_sn} missing 송출지역")

    sent_at = _parse_sent_at(meta["sent_at"])
    disaster_type = disaster_type_cell.get_text(strip=True) if disaster_type_cell else ""

    raw_payload = {
        "bbsSn": bbs_sn,
        "disaster_type": disaster_type,
        "sent_at": meta["sent_at"],
        "emergency_step": meta["emergency_step"],
        "region": meta["region"],
    }

    return DisasterMessageRecord(
        source_id=f"SAFEKOREA:{bbs_sn}",
        sender_or_region=meta["region"],
        sent_at=sent_at,
        original_body=body,
        source_url=SAFEKOREA_BASE_URL,
        detected_at=detected_at,
        raw_payload=raw_payload,
    )


def _build_client(settings: Settings, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=settings.safekorea_request_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
        transport=transport,
    )


def _request_page(client: httpx.Client, params: dict[str, str]) -> str:
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.get(SAFEKOREA_BASE_URL, params=params)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_exc = exc
            backoff = min(BACKOFF_BASE_SECONDS * (2**attempt), MAX_BACKOFF_SECONDS)
            logger.warning(
                "SafeKorea fallback request failed (attempt %d/%d): %s; backing off %.1fs",
                attempt + 1,
                MAX_RETRIES + 1,
                type(exc).__name__,
                backoff,
            )
            if attempt < MAX_RETRIES:
                time.sleep(backoff)
            continue

        if response.status_code in (403, 429):
            raise SafeKoreaRuntimeError(f"SafeKorea fallback blocked: HTTP {response.status_code}")
        if response.status_code != 200:
            raise SafeKoreaRuntimeError(f"SafeKorea fallback failed: HTTP {response.status_code}")

        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            raise SafeKoreaRuntimeError(
                f"SafeKorea fallback unexpected content-type: {content_type!r}"
            )

        if _looks_like_challenge_page(response.text):
            raise SafeKoreaRuntimeError("SafeKorea fallback returned a challenge/login page")

        return response.text

    raise SafeKoreaRuntimeError(f"SafeKorea fallback request failed after retries: {last_exc}")


def fetch_records(
    settings: Settings, *, transport: httpx.BaseTransport | None = None
) -> list[DisasterMessageRecord]:
    """Fetch, paginate, validate, and Seoul-filter current SafeKorea records.

    Raises SafeKoreaRuntimeError/SafeKoreaSchemaError on any hard failure —
    callers must treat that as "fallback failed" (fail-closed if the
    primary also failed). Returns an empty list for a valid empty window.
    A single malformed row aborts the whole attempt rather than sending a
    partial/best-effort result.

    `transport` is exposed only so tests can inject an httpx.MockTransport;
    production code never passes it.
    """
    start_date, end_date = _kst_window()
    detected_at = datetime.now(tz=SEOUL_TZ)

    all_records: list[DisasterMessageRecord] = []
    previous_page_ids: set[str] | None = None

    with _build_client(settings, transport) as client:
        for page in range(1, settings.safekorea_max_pages + 1):
            if page > 1:
                time.sleep(settings.safekorea_request_delay_seconds)

            html = _request_page(
                client, _build_params(page=page, start_date=start_date, end_date=end_date)
            )
            soup = BeautifulSoup(html, "html.parser")
            rows = soup.select(ROW_SELECTOR)

            if not rows:
                raise SafeKoreaSchemaError(
                    f"SafeKorea fallback page {page}: no rows and no empty marker"
                )

            if len(rows) == 1 and _row_is_empty_marker(rows[0]):
                break

            page_ids = set()
            page_records: list[DisasterMessageRecord] = []
            for row in rows:
                record = _row_to_record(row, detected_at=detected_at)
                bbs_sn = record.raw_payload["bbsSn"]
                page_ids.add(bbs_sn)
                page_records.append(record)

            if previous_page_ids is not None and page_ids and page_ids == previous_page_ids:
                raise SafeKoreaRuntimeError(
                    f"SafeKorea fallback pagination loop detected at page {page}"
                )
            previous_page_ids = page_ids

            all_records.extend(page_records)

            if len(rows) < PAGE_SIZE:
                break
        else:
            raise SafeKoreaRuntimeError(
                f"SafeKorea fallback pagination exceeded max page guard ({settings.safekorea_max_pages})"
            )

    seoul_records = [r for r in all_records if is_seoul_recipient(r.sender_or_region)]
    seoul_records.sort(key=lambda r: r.sent_at)

    logger.info(
        "SafeKorea fallback collection: fetched=%d seoul=%d window=%s..%s",
        len(all_records),
        len(seoul_records),
        start_date,
        end_date,
    )

    return seoul_records

"""HTML parsing for the safetydata.go.kr disaster-message archive.

Confirmed structure documented in docs/history_source_discovery.md. Extracts
raw field values only — no classification, no summarization, no inference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

SEOUL_TZ = ZoneInfo("Asia/Seoul")

_SN_RE = re.compile(r"[?&]sn=(\d+)")
_REGION_BRACKET_RE = re.compile(r"\[(.*)\]\s*$", re.DOTALL)
_SENT_AT_FORMAT = "%Y/%m/%d %H:%M:%S"


class HistoryParseError(ValueError):
    """Raised when a page's structure does not match the confirmed schema."""


@dataclass
class ListRow:
    source_id: str
    detail_href: str
    list_body_text: str
    cell_date: str
    position: int


@dataclass
class ParsedListPage:
    total_count: int | None
    rows: list[ListRow]


def parse_list_page(html: str) -> ParsedListPage:
    """Parse one `disasterNotification` list page.

    Rows with unrecognized structure (missing sn, missing date) are skipped
    rather than raising, since a single malformed row must not abort an
    otherwise-good page; callers count skipped rows as malformed.
    """
    soup = BeautifulSoup(html, "html.parser")

    total_count = None
    count_el = soup.select_one("p.board-count span")
    if count_el and count_el.get_text(strip=True).isdigit():
        total_count = int(count_el.get_text(strip=True))

    rows: list[ListRow] = []
    body_rows = soup.select("table.other-type-table tbody tr")
    for position, tr in enumerate(body_rows, start=1):
        anchor = tr.select_one("td.cell-subject a")
        date_cell = tr.select_one("td.cell-date")
        if anchor is None or date_cell is None:
            continue
        href = anchor.get("href", "")
        match = _SN_RE.search(href)
        if not match:
            continue
        rows.append(
            ListRow(
                source_id=match.group(1),
                detail_href=href,
                list_body_text=anchor.get_text(strip=True),
                cell_date=date_cell.get_text(strip=True),
                position=position,
            )
        )

    return ParsedListPage(total_count=total_count, rows=rows)


@dataclass
class ParsedDetailPage:
    title_raw: str | None
    region_raw: str | None
    sender_raw: str | None
    reg_date_raw: str | None
    body_raw: str


def parse_detail_page(html: str) -> ParsedDetailPage:
    """Parse one `disasterNotificationDetail` page.

    Raises HistoryParseError if the body container is missing or empty —
    callers must treat that as malformed and must not insert a record with a
    truncated/absent body.
    """
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("div.view-header2 .title")
    title_raw = title_el.get_text(strip=True) if title_el else None

    region_raw = None
    if title_raw:
        region_match = _REGION_BRACKET_RE.search(title_raw)
        if region_match:
            region_raw = region_match.group(1).strip()

    sender_raw = None
    reg_date_raw = None
    for item in soup.select("div.list-info-item2"):
        label_el = item.select_one(".list-info-title")
        value_el = item.select_one(".list-info-desc")
        if not label_el or not value_el:
            continue
        label = label_el.get_text(strip=True)
        value = value_el.get_text(strip=True)
        if label == "작성자":
            sender_raw = value
        elif label == "등록일":
            reg_date_raw = value

    body_el = soup.select_one("div.view-body")
    if body_el is None:
        raise HistoryParseError("detail page is missing the view-body container")

    for br in body_el.find_all("br"):
        br.replace_with("\n")
    body_raw = body_el.get_text().strip()

    if not body_raw:
        raise HistoryParseError("detail page body is empty")

    return ParsedDetailPage(
        title_raw=title_raw,
        region_raw=region_raw,
        sender_raw=sender_raw,
        reg_date_raw=reg_date_raw,
        body_raw=body_raw,
    )


def extract_sn_from_url(url: str) -> str | None:
    query = parse_qs(urlparse(url).query)
    values = query.get("sn")
    return values[0] if values else None


def parse_sent_at(raw: str | None) -> datetime | None:
    """Best-effort Asia/Seoul parse of the site's `yyyy/MM/dd HH:mm:ss` timestamps.

    Returns None (never raises) on anything that doesn't match — `sent_at_raw`
    remains the source of truth regardless.
    """
    if not raw:
        return None
    try:
        return datetime.strptime(raw, _SENT_AT_FORMAT).replace(tzinfo=SEOUL_TZ)
    except ValueError:
        return None

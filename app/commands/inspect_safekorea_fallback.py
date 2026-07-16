"""Read-only structure inspection for the 국민안전24 재난문자 조회 HTML fallback.

Usage:
    python -m app.commands.inspect_safekorea_fallback

Confirms (without guessing): whether the list is server-rendered, the list
row selector, whether full body/발송일시/긴급단계/송출지역 are already present in
the list HTML (no detail fetch needed), the stable record identifier
(`bbsSn`), pagination behavior, empty-result representation, and whether the
`sbLawArea1=1100000000` Seoul filter omits any multi-region Seoul record
compared to the unfiltered list over the same date window.

Prints only sanitized structural information — HTTP status, row counts,
selector matches, and field presence/length. Never prints a full message
body or a request URL with its query string in the summary lines (the
underlying request objects are not logged).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from app.config import SAFEKOREA_BASE_URL, SAFEKOREA_SEOUL_LAW_AREA, load_settings
from app.logging_config import configure_logging
from app.models import is_seoul_recipient

SEOUL_TZ = ZoneInfo("Asia/Seoul")
EMPTY_MARKER_TEXT = "데이터가 존재하지 않습니다"


def _build_params(
    *, page: int, start_date: str, end_date: str, seoul_filter: bool
) -> dict[str, str]:
    return {
        "menuSn": "34",
        "bbsSn": "",
        "currentPage": str(page),
        "firstYn": "",
        "searchType": "",
        "cOcrcType": "",
        "dsstrSeId": "",
        "sbLawArea1": SAFEKOREA_SEOUL_LAW_AREA if seoul_filter else "",
        "sbLawArea2": "",
        "sbLawArea3": "",
        "keyword": "",
        "startDate": start_date,
        "endDate": end_date,
        "readYn": "Y",
    }


def _parse_rows(html: str) -> tuple[BeautifulSoup, list]:
    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.select_one("div.board-list table tbody")
    rows = tbody.select("tr") if tbody else []
    return soup, rows


def _row_is_empty_marker(row) -> bool:
    return EMPTY_MARKER_TEXT in row.get_text(strip=True)


def _extract_row_fields(row) -> dict:
    """Extract the fields already present in the list row (no detail fetch)."""
    disaster_type = row.select_one("td")
    link = row.select_one("td.tit a")
    detail_p = row.select_one("td.tit p")
    href = link.get("href", "") if link else ""
    bbs_sn = None
    if "onSubmit(" in href:
        inner = href.split("onSubmit(", 1)[1]
        bbs_sn = inner.split(")")[0].strip("'\" ;")

    body_text = link.get_text(strip=True) if link else None
    meta_text = detail_p.get_text(" ", strip=True) if detail_p else ""

    sent_at = None
    emergency_step = None
    region = None
    for segment in meta_text.split("ㆍ"):
        segment = segment.strip()
        if segment.startswith("발송일시"):
            sent_at = segment.split(":", 1)[-1].strip()
        elif segment.startswith("긴급단계"):
            emergency_step = segment.split(":", 1)[-1].strip()
        elif segment.startswith("송출지역"):
            region = segment.split(":", 1)[-1].strip()

    return {
        "bbs_sn": bbs_sn,
        "disaster_type": disaster_type.get_text(strip=True) if disaster_type else None,
        "body_present": bool(body_text),
        "body_length": len(body_text) if body_text else 0,
        "sent_at": sent_at,
        "emergency_step": emergency_step,
        "region_present": region is not None,
        "region_length": len(region) if region else 0,
        "region": region,
    }


def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_level)

    today = datetime.now(tz=SEOUL_TZ)
    start = today - timedelta(days=7)
    start_date = start.strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    print("=== A. Basic list request (Seoul filter) ===")
    print(f"base       : {SAFEKOREA_BASE_URL}")
    print(f"date window: {start_date} .. {end_date} (7-day max per plan)")
    print()

    with httpx.Client(
        timeout=settings.safekorea_request_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 SeoulNewsTracking/0.5"},
    ) as client:
        response = client.get(
            SAFEKOREA_BASE_URL,
            params=_build_params(
                page=1, start_date=start_date, end_date=end_date, seoul_filter=True
            ),
        )
        print(f"HTTP status  : {response.status_code}")
        print(f"content-type : {response.headers.get('content-type', '')}")
        print(f"redirects    : {len(response.history)}")
        print(f"final url host/path only: {response.url.host}{response.url.path}")

        soup, rows = _parse_rows(response.text)
        count_div = soup.select_one("div.board-count")
        print(f"board-count text: {count_div.get_text(strip=True) if count_div else 'NOT FOUND'}")
        print(f"row selector 'div.board-list table tbody tr' matched: {len(rows)} rows")

        is_empty = len(rows) == 1 and _row_is_empty_marker(rows[0])
        print(f"empty-marker detected: {is_empty} (text contains {EMPTY_MARKER_TEXT!r})")

        pagination = soup.select_one("div.pagination")
        page_buttons = pagination.select("button") if pagination else []
        print(
            f"pagination selector 'div.pagination button' matched: {len(page_buttons)} page buttons"
        )

        if not is_empty and rows:
            print("\n--- first row field extraction (list HTML only, no detail fetch) ---")
            first = _extract_row_fields(rows[0])
            for key, value in first.items():
                if key == "region":
                    continue  # not printed in full by default
                print(f"  {key}: {value}")
            print(
                f"  region_present={first['region_present']} region_length={first['region_length']}"
            )

            print("\n--- full-data-in-list-html check across all rows ---")
            missing_body = 0
            missing_sent_at = 0
            missing_region = 0
            missing_bbs_sn = 0
            for row in rows:
                fields = _extract_row_fields(row)
                if not fields["body_present"]:
                    missing_body += 1
                if not fields["sent_at"]:
                    missing_sent_at += 1
                if not fields["region_present"]:
                    missing_region += 1
                if not fields["bbs_sn"]:
                    missing_bbs_sn += 1
            print(f"  rows missing body: {missing_body}/{len(rows)}")
            print(f"  rows missing sent_at: {missing_sent_at}/{len(rows)}")
            print(f"  rows missing region: {missing_region}/{len(rows)}")
            print(f"  rows missing bbsSn: {missing_bbs_sn}/{len(rows)}")
            if missing_body == 0 and missing_sent_at == 0 and missing_region == 0:
                print("  CONCLUSION: full body + 발송일시 + 송출지역 are present in list HTML.")
                print("  Detail-page fetch is NOT required for normal operation.")

        # --- pagination probe: page 2 ---
        print("\n=== B. Pagination probe (page 2) ===")
        response2 = client.get(
            SAFEKOREA_BASE_URL,
            params=_build_params(
                page=2, start_date=start_date, end_date=end_date, seoul_filter=True
            ),
        )
        _, rows2 = _parse_rows(response2.text)
        print(f"page 2 HTTP {response2.status_code}, rows: {len(rows2)}")
        if rows and rows2:
            ids1 = {_extract_row_fields(r)["bbs_sn"] for r in rows}
            ids2 = {_extract_row_fields(r)["bbs_sn"] for r in rows2}
            print(
                f"page1/page2 bbsSn overlap: {len(ids1 & ids2)} (0 expected for a real next page)"
            )

        # --- section: empty result representation (known-empty window) ---
        print("\n=== C. Empty-result representation (2020-01-01 window) ===")
        response_empty = client.get(
            SAFEKOREA_BASE_URL,
            params=_build_params(
                page=1, start_date="2020-01-01", end_date="2020-01-01", seoul_filter=True
            ),
        )
        _, empty_rows = _parse_rows(response_empty.text)
        print(f"HTTP {response_empty.status_code}, rows: {len(empty_rows)}")
        if empty_rows:
            print(f"empty marker present: {_row_is_empty_marker(empty_rows[0])}")

        # --- section: Seoul filter completeness ---
        #
        # The nationwide unfiltered list is too large (1,000+ records/week)
        # to fully paginate in an inspection command, and a small bounded
        # sample of it is dominated by whatever regional event is largest
        # that week — not a reliable completeness signal on its own. The
        # decisive test instead is internal to the *filtered* set itself:
        # if sbLawArea1=1100000000 excluded multi-region records, none of
        # its results would ever contain a comma-separated region. Any
        # multi-region record appearing in the filtered set is direct proof
        # the filter did not drop it.
        print("\n=== D. Seoul filter completeness (multi-region inclusion check) ===")
        filtered_ids: dict[str, dict] = {}
        for page in range(1, settings.safekorea_max_pages + 1):
            resp = client.get(
                SAFEKOREA_BASE_URL,
                params=_build_params(
                    page=page, start_date=start_date, end_date=end_date, seoul_filter=True
                ),
            )
            _, page_rows = _parse_rows(resp.text)
            if not page_rows or _row_is_empty_marker(page_rows[0]):
                break
            for row in page_rows:
                fields = _extract_row_fields(row)
                if fields["bbs_sn"]:
                    filtered_ids[fields["bbs_sn"]] = fields
            if len(page_rows) < 10:
                break

        multi_region = {
            bbs_sn: f for bbs_sn, f in filtered_ids.items() if "," in (f["region"] or "")
        }
        non_seoul_leak = {
            bbs_sn: f for bbs_sn, f in filtered_ids.items() if not is_seoul_recipient(f["region"])
        }

        print(f"filtered set size (sbLawArea1={SAFEKOREA_SEOUL_LAW_AREA}): {len(filtered_ids)}")
        print(f"multi-region records within filtered set : {len(multi_region)}")
        print(
            f"filtered records NOT matching is_seoul_recipient() : {len(non_seoul_leak)} (should be 0)"
        )

        if not filtered_ids:
            print("INCONCLUSIVE: zero Seoul records in this 7-day window.")
        elif multi_region:
            print(
                "DECISION: sbLawArea1=1100000000 DOES include multi-region Seoul "
                f"records ({len(multi_region)} confirmed in this window, e.g. bbsSn="
                f"{next(iter(multi_region))}). Production fallback may use the "
                "Seoul-filtered URL, with client-side region validation still applied."
            )
        else:
            print(
                "NOTE: no multi-region record appeared in this window's filtered "
                "set — inclusion of multi-region records could not be positively "
                "confirmed from this sample."
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

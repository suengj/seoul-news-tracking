"""Read-only inspection of the historical archive source.

Fetches one list page and one detail page, prints what was found. Performs
no database writes. Useful for confirming the collection method documented
in docs/history_source_discovery.md still works.

Usage: python -m app.commands.inspect_history_source
"""

from __future__ import annotations

import sys

from app.config import HISTORY_DETAIL_URL, HISTORY_LIST_URL, load_settings
from app.history_backfill import DEFAULT_CNT_PER_PAGE, DEFAULT_PAGE_SIZE
from app.history_collector import HistoryCollectorError, build_client, fetch_detail_page, fetch_list_page
from app.history_parser import parse_detail_page, parse_list_page
from app.logging_config import configure_logging


def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_level)

    print(f"list endpoint   : GET {HISTORY_LIST_URL}")
    print(f"detail endpoint : GET {HISTORY_DETAIL_URL}?sn=<id>")

    client = build_client(timeout_seconds=settings.history_request_timeout_seconds)
    try:
        list_result = fetch_list_page(
            client,
            current_page=1,
            cnt_per_page=DEFAULT_CNT_PER_PAGE,
            page_size=DEFAULT_PAGE_SIZE,
            delay_seconds=settings.history_request_delay_seconds,
            max_retries=settings.history_max_retries,
        )
        parsed_list = parse_list_page(list_result.html)
        print(f"reported total records : {parsed_list.total_count}")
        print(f"rows on page 1         : {len(parsed_list.rows)}")

        if not parsed_list.rows:
            print("FAILED: list page returned zero rows", file=sys.stderr)
            return 1

        sample = parsed_list.rows[0]
        print(f"sample source_id       : {sample.source_id}")
        print(f"sample sent_at_raw     : {sample.cell_date}")
        print(f"sample list body       : {sample.list_body_text[:80]}")

        detail_result = fetch_detail_page(
            client,
            source_id=sample.source_id,
            delay_seconds=settings.history_request_delay_seconds,
            max_retries=settings.history_max_retries,
        )
        detail = parse_detail_page(detail_result.html)
        print(f"detail title_raw       : {detail.title_raw}")
        print(f"detail region_raw      : {detail.region_raw}")
        print(f"detail sender_raw      : {detail.sender_raw}")
        print(f"detail body_raw        : {detail.body_raw[:120]}")
    except HistoryCollectorError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

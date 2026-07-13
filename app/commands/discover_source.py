"""Read-only inspection command: fetch the widget once and report what was found.

Does not write to the database and does not send anything to Telegram.
Useful for confirming the collection method still works without affecting
stored state.

Usage: python -m app.commands.discover_source
"""

from __future__ import annotations

import sys

from app.collector import CollectorError, fetch_records
from app.commands._shared import record_preview
from app.config import SOURCE_API_URL, SOURCE_PAGE_URL, load_settings
from app.logging_config import configure_logging


def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_level)

    print(f"target page   : {SOURCE_PAGE_URL}")
    print(f"data endpoint : POST {SOURCE_API_URL}")

    try:
        result = fetch_records()
    except CollectorError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"selected method     : {result.method}")
    print(f"records fetched      : {result.fetched_count}")
    print(f"full text confirmed  : {result.full_text_confirmed}")
    print("sample records:")
    for record in result.records[:5]:
        print(f"  {record_preview(record)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Export a small random sample of historical raw records for manual inspection.

Usage:
    python -m app.commands.export_history_sample --count 100 \
        --output data/exports/history_sample.jsonl

This is for manual inspection only — do not export all records unless
explicitly requested. Output rows are plain JSON Lines, one raw record per
line, straight from `historical_raw_messages` (no transformation).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import load_settings
from app.history_database import open_history_database
from app.logging_config import configure_logging


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100, help="number of records to sample")
    parser.add_argument(
        "--output", type=Path, default=Path("data/exports/history_sample.jsonl"), help="output path"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)

    output_path = args.output
    if not output_path.is_absolute():
        output_path = Path.cwd() / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open_history_database(settings.history_database_path) as db:
        rows = db.sample_records(args.count)

    with output_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(dict(row), ensure_ascii=False) + "\n")

    print(f"wrote {len(rows)} sample records to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

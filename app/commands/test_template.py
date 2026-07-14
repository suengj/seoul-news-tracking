"""Manually run the template recommendation pipeline against one message.

Usage:
    python -m app.commands.test_template --message-id 42
    python -m app.commands.test_template --text "test message" \
        --region "서울특별시" --sent-at "2026-07-14T12:00:00+09:00"

Read-only: never writes to any database, never sends to Telegram. Prints
every rule candidate (score, matched/missing/conflict signals), the picked
recommendation (or UNKNOWN), extracted slots, and the rendered draft or the
reason rendering failed.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from app.config import load_settings
from app.database import open_database
from app.template_extractors import extract_slots
from app.template_renderer import render_template
from app.template_rules import recommend_template, suggest_templates


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--message-id", type=int, help="internal_id in the real-time messages table"
    )
    parser.add_argument("--text", help="ad-hoc message text (used with --region/--sent-at)")
    parser.add_argument("--region", default="", help="sender_or_region for --text mode")
    parser.add_argument("--sent-at", help="ISO 8601 timestamp for --text mode (default: now)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()

    if args.message_id is not None:
        with open_database(settings.database_path) as db:
            record = db.get_by_internal_id(args.message_id)
        if record is None:
            print(f"FAILED: no message with internal_id={args.message_id}", file=sys.stderr)
            return 1
        message_text = record.original_body
        region = record.sender_or_region
        sent_at = record.sent_at
    elif args.text is not None:
        message_text = args.text
        region = args.region
        sent_at = (
            datetime.fromisoformat(args.sent_at) if args.sent_at else datetime.now().astimezone()
        )
    else:
        print("FAILED: pass either --message-id or --text", file=sys.stderr)
        return 1

    print("message text:")
    print(f"  {message_text}")
    print(f"region: {region!r}  sent_at: {sent_at.isoformat()}")
    print()

    candidates = suggest_templates(message_text, region, sent_at)
    print("candidates (sorted by rule_score):")
    for c in candidates:
        print(f"  {c.template_id:<32} score={c.rule_score:.2f}")
        print(f"    matched  : {c.matched_signals or '(none)'}")
        print(f"    missing  : {c.missing_signals or '(none)'}")
        print(f"    conflict : {c.conflict_signals or '(none)'}")
    print()

    recommended, _ = recommend_template(
        message_text, region, sent_at, threshold=settings.template_recommend_threshold
    )
    if recommended is None:
        print(f"recommendation: UNKNOWN (threshold={settings.template_recommend_threshold})")
        return 0

    print(f"recommendation: {recommended.template_id} (score={recommended.rule_score:.2f})")
    extraction = extract_slots(recommended.template_id, message_text, region, sent_at)
    print("extracted slots:")
    for name, slot in extraction.extracted_slots.items():
        print(f"  {name}: value={slot.value!r} source={slot.source} confidence={slot.confidence}")
    if extraction.missing_required_slots:
        print(f"missing required slots: {extraction.missing_required_slots}")

    render_result = render_template(recommended.template_id, extraction.extracted_slots)
    print()
    if render_result.success:
        print("rendered draft:")
        print(render_result.rendered_text)
    else:
        print(f"render FAILED: {render_result.validation_errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

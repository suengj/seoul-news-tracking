"""Controlled Telegram delivery test — never uses a real historical disaster message.

Requires both:
  - TELEGRAM_SEND_ENABLED=true in the environment
  - the --confirm flag on the command line

so a test send can never happen by accident.

Usage: python -m app.commands.send_telegram_test --confirm
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import load_settings
from app.logging_config import configure_logging
from app.models import TelegramStatus
from app.telegram_sender import TelegramError, TelegramSender, escape_markdown_v2

SEOUL_TZ = ZoneInfo("Asia/Seoul")

TEST_MESSAGE_TEMPLATE = (
    "\\[Seoul News Tracking \\- 연결 테스트\\]\n\n"
    "이 메시지는 실제 재난문자가 아닌, 배포 연결 확인용 테스트 메시지입니다\\.\n"
    "생성 시각: {timestamp}"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="required to actually send; without it the command only validates config",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)

    if not settings.telegram_configured:
        print("FAILED: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured.", file=sys.stderr)
        return 1

    if not settings.telegram_send_enabled:
        print(
            "TELEGRAM_SEND_ENABLED is false; refusing to send. "
            "Set TELEGRAM_SEND_ENABLED=true to allow a real test send.",
            file=sys.stderr,
        )
        return 1

    if not args.confirm:
        print(
            "Config looks valid and sending is enabled, but --confirm was not passed. "
            "Nothing was sent."
        )
        return 0

    timestamp = escape_markdown_v2(datetime.now(tz=SEOUL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"))
    text = TEST_MESSAGE_TEMPLATE.format(timestamp=timestamp)

    with TelegramSender(settings) as sender:
        try:
            outcome = sender.send_test_text(text)
        except TelegramError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1

    if outcome.status == TelegramStatus.TELEGRAM_SENT:
        print(f"OK: test message sent, message_id(s)={outcome.combined_message_id}")
        return 0

    print(f"FAILED: {outcome.error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

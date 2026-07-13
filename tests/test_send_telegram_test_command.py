from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.commands import send_telegram_test
from app.telegram_sender import escape_markdown_v2
from tests.test_telegram_sender import assert_no_unescaped_markdown_v2

SEOUL_TZ = ZoneInfo("Asia/Seoul")


def test_test_message_template_fully_escaped():
    """Regression test: a real live send once failed with Telegram HTTP 400
    ("Character '-' is reserved") because the timestamp interpolated into
    this template was not escaped. Confirm the actual construction path
    (escape then format, as done in main()) is safe."""
    timestamp = escape_markdown_v2(
        datetime(2026, 7, 13, 9, 0, 0, tzinfo=SEOUL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    )
    text = send_telegram_test.TEST_MESSAGE_TEMPLATE.format(timestamp=timestamp)
    assert_no_unescaped_markdown_v2(text)


def test_test_message_is_not_a_real_disaster_message():
    timestamp = escape_markdown_v2("2026-07-13 09:00:00 KST")
    text = send_telegram_test.TEST_MESSAGE_TEMPLATE.format(timestamp=timestamp)
    assert "연결 테스트" in text
    assert "실제 재난문자가 아닌" in text

"""Telegram delivery for Part 1: send the complete original message, nothing else.

No approval/rejection/editing/auto-posting — this module only knows how to
turn a DisasterMessageRecord (or an ad-hoc test string) into one or more
Telegram messages and send them, honoring TELEGRAM_SEND_ENABLED.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from app.config import Settings
from app.models import DisasterMessageRecord, TelegramStatus
from app.template_extractors import SlotValue
from app.template_renderer import RenderResult, load_templates
from app.template_rules import TemplateSuggestion

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MESSAGE_LIMIT = 4096
MAX_SEND_RETRIES = 2
RETRY_BACKOFF_SECONDS = 2.0

# Characters MarkdownV2 requires escaping outside of intentional entities.
# https://core.telegram.org/bots/api#markdownv2-style
_MARKDOWN_V2_SPECIAL = set(r"_*[]()~`>#+-=|{}.!\\")


class TelegramError(RuntimeError):
    pass


class TelegramPermanentError(TelegramError):
    """Not worth retrying: bad token/chat id, malformed request, etc."""


class TelegramTemporaryError(TelegramError):
    """Worth a limited number of retries: network hiccup, 429, 5xx."""


@dataclass
class TelegramSendOutcome:
    status: TelegramStatus
    message_ids: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def combined_message_id(self) -> str | None:
        return ",".join(self.message_ids) if self.message_ids else None


def escape_markdown_v2(text: str) -> str:
    """Escape a dynamic value for safe inclusion in a Telegram MarkdownV2 message."""
    return "".join(("\\" + ch) if ch in _MARKDOWN_V2_SPECIAL else ch for ch in text)


def format_timestamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def build_message(record: DisasterMessageRecord) -> str:
    """Build the Part 1 Telegram message: original text preserved, dynamic fields escaped."""
    lines = [
        "\\[서울안전누리 신규 재난문자\\]",
        "",
        f"발송지역/기관: {escape_markdown_v2(record.sender_or_region)}",
        f"발송시각: {escape_markdown_v2(format_timestamp(record.sent_at))}",
        "",
        "원문:",
        escape_markdown_v2(record.original_body),
        "",
        "출처:",
        escape_markdown_v2(record.source_url),
        "",
        "수집시각:",
        escape_markdown_v2(format_timestamp(record.detected_at)),
    ]
    return "\n".join(lines)


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split text into <= limit chunks, preferring newline boundaries.

    Never splits immediately after a trailing backslash, which would break
    a MarkdownV2 escape sequence across chunks.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        while split_at > 0 and remaining[split_at - 1] == "\\":
            split_at -= 1
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


class TelegramSender:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=10.0)
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "TelegramSender":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def send_record(self, record: DisasterMessageRecord) -> TelegramSendOutcome:
        return self._send_text(build_message(record))

    def send_test_text(self, text: str) -> TelegramSendOutcome:
        return self._send_text(text)

    def _send_text(self, message: str) -> TelegramSendOutcome:
        if not self.settings.telegram_send_enabled:
            logger.info("TELEGRAM_SEND_ENABLED=false; skipping send")
            return TelegramSendOutcome(
                status=TelegramStatus.TELEGRAM_PENDING,
                error="send disabled (TELEGRAM_SEND_ENABLED=false)",
            )
        if not self.settings.telegram_configured:
            raise TelegramPermanentError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured")

        chunks = split_message(message)
        message_ids: list[str] = []
        try:
            for chunk in chunks:
                message_ids.append(self._send_chunk(chunk))
        except TelegramError as exc:
            return TelegramSendOutcome(
                status=TelegramStatus.TELEGRAM_FAILED,
                message_ids=message_ids,
                error=str(exc),
            )
        return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_SENT, message_ids=message_ids)

    def _send_chunk(self, text: str) -> str:
        url = f"{TELEGRAM_API_BASE}/bot{self.settings.telegram_bot_token}/sendMessage"
        last_error: str = "unknown error"

        for attempt in range(MAX_SEND_RETRIES + 1):
            try:
                response = self._client.post(
                    url,
                    data={
                        "chat_id": self.settings.telegram_chat_id,
                        "text": text,
                        "parse_mode": "MarkdownV2",
                        "disable_web_page_preview": True,
                    },
                )
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = f"network error: {exc}"
                logger.warning("Telegram send attempt %d failed: %s", attempt + 1, last_error)
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            if response.status_code == 200:
                data = response.json()
                return str(data["result"]["message_id"])

            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning("Telegram send attempt %d temporary failure: %s", attempt + 1, last_error)
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            # Permanent failure (e.g. 400 bad request, 401/403 bad credentials).
            raise TelegramPermanentError(f"HTTP {response.status_code}: {response.text[:200]}")

        raise TelegramTemporaryError(f"send failed after {MAX_SEND_RETRIES + 1} attempts: {last_error}")

    def send_plain_text(
        self, text: str, reply_markup: dict | None = None
    ) -> TelegramSendOutcome:
        """Send plain (unformatted) text — used for the template pipeline so
        rendered template/original text is delivered byte-for-byte with no
        MarkdownV2 escaping. `reply_markup` (an inline keyboard dict), if
        given, is attached only to the last chunk."""
        if not self.settings.telegram_send_enabled:
            logger.info("TELEGRAM_SEND_ENABLED=false; skipping send")
            return TelegramSendOutcome(
                status=TelegramStatus.TELEGRAM_PENDING,
                error="send disabled (TELEGRAM_SEND_ENABLED=false)",
            )
        if not self.settings.telegram_configured:
            raise TelegramPermanentError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured")

        chunks = split_message(text)
        message_ids: list[str] = []
        try:
            for i, chunk in enumerate(chunks):
                markup = reply_markup if i == len(chunks) - 1 else None
                message_ids.append(self._send_plain_chunk(chunk, markup))
        except TelegramError as exc:
            return TelegramSendOutcome(
                status=TelegramStatus.TELEGRAM_FAILED, message_ids=message_ids, error=str(exc)
            )
        return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_SENT, message_ids=message_ids)

    def _send_plain_chunk(self, text: str, reply_markup: dict | None) -> str:
        url = f"{TELEGRAM_API_BASE}/bot{self.settings.telegram_bot_token}/sendMessage"
        payload: dict = {"chat_id": self.settings.telegram_chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

        last_error = "unknown error"
        for attempt in range(MAX_SEND_RETRIES + 1):
            try:
                response = self._client.post(url, data=payload)
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = f"network error: {exc}"
                logger.warning("Telegram send attempt %d failed: %s", attempt + 1, last_error)
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            if response.status_code == 200:
                return str(response.json()["result"]["message_id"])

            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning("Telegram send attempt %d temporary failure: %s", attempt + 1, last_error)
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            raise TelegramPermanentError(f"HTTP {response.status_code}: {response.text[:200]}")

        raise TelegramTemporaryError(f"send failed after {MAX_SEND_RETRIES + 1} attempts: {last_error}")

    def answer_callback_query(
        self, callback_query_id: str, *, text: str = "", show_alert: bool = False
    ) -> None:
        if not self.settings.telegram_send_enabled or not self.settings.telegram_configured:
            return
        url = f"{TELEGRAM_API_BASE}/bot{self.settings.telegram_bot_token}/answerCallbackQuery"
        try:
            self._client.post(
                url,
                data={"callback_query_id": callback_query_id, "text": text, "show_alert": show_alert},
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning("answerCallbackQuery failed: %s", exc)

    def get_updates(self, *, offset: int | None, timeout: int) -> list[dict]:
        """Long-poll `getUpdates`. Only ever called by the interactive bot
        command — never during ordinary collection/send runs."""
        url = f"{TELEGRAM_API_BASE}/bot{self.settings.telegram_bot_token}/getUpdates"
        params: dict = {"timeout": timeout, "allowed_updates": json.dumps(["callback_query"])}
        if offset is not None:
            params["offset"] = offset
        response = self._client.get(
            url, params=params, timeout=httpx.Timeout(connect=5.0, read=timeout + 10.0, write=10.0, pool=10.0)
        )
        if response.status_code != 200:
            raise TelegramTemporaryError(f"getUpdates HTTP {response.status_code}: {response.text[:200]}")
        return response.json().get("result", [])


# --- template pipeline: keyboard/callback-data and message formatting ------
#
# Kept in this module (rather than a new one) since it is still just
# "turn data into a Telegram message/keyboard" — the same responsibility
# `build_message`/`split_message` already have above.

# Compact callback_data codes ("tpl:{message_id}:{code}"), well under
# Telegram's 64-byte callback_data limit.
TEMPLATE_SHORT_CODES: dict[str, str] = {
    "FLOOD_ADVISORY_ISSUED": "flood_adv",
    "HEAVY_RAIN_CLEARED": "rain_clr",
    "HEAVY_RAIN_DOWNGRADED": "rain_down",
    "HEAVY_RAIN_MULTI_LEVEL_ISSUED": "rain_multi",
    "HEATWAVE_UPGRADED": "heat_up",
    "HEATWAVE_ADVISORY_ISSUED": "heat_adv",
    "TROPICAL_NIGHT_ADVISORY_ISSUED": "trop_night",
    "ORIGINAL_ONLY": "orig",
}
_SHORT_CODE_TO_TEMPLATE_ID = {code: tid for tid, code in TEMPLATE_SHORT_CODES.items()}

# Button row layout from the task spec, by template_id (row 1 is built
# separately since its labels are meta-actions, not per-template labels).
_BUTTON_ROWS: list[list[str]] = [
    ["FLOOD_ADVISORY_ISSUED", "HEAVY_RAIN_CLEARED"],
    ["HEAVY_RAIN_DOWNGRADED", "HEAVY_RAIN_MULTI_LEVEL_ISSUED"],
    ["HEATWAVE_UPGRADED", "HEATWAVE_ADVISORY_ISSUED"],
    ["TROPICAL_NIGHT_ADVISORY_ISSUED"],
]


def make_callback_data(message_id: int, template_id: str) -> str:
    code = TEMPLATE_SHORT_CODES[template_id]
    return f"tpl:{message_id}:{code}"


def parse_callback_data(data: str) -> tuple[int, str] | None:
    """Returns `(message_id, template_id)`, or `None` if malformed/unrecognized."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "tpl":
        return None
    try:
        message_id = int(parts[1])
    except ValueError:
        return None
    template_id = _SHORT_CODE_TO_TEMPLATE_ID.get(parts[2])
    if template_id is None:
        return None
    return message_id, template_id


def build_keyboard(message_id: int, recommended_template_id: str | None) -> dict:
    templates = load_templates()
    rows: list[list[dict]] = []

    row1 = []
    if recommended_template_id is not None:
        row1.append(
            {"text": "✅ 권장 포맷", "callback_data": make_callback_data(message_id, recommended_template_id)}
        )
    row1.append({"text": "📄 원문", "callback_data": make_callback_data(message_id, "ORIGINAL_ONLY")})
    rows.append(row1)

    for template_ids in _BUTTON_ROWS:
        row = [
            {
                "text": templates[tid].button_label,
                "callback_data": make_callback_data(message_id, tid),
            }
            for tid in template_ids
        ]
        rows.append(row)

    return {"inline_keyboard": rows}


def build_template_alert_message(
    record: DisasterMessageRecord,
    recommended: TemplateSuggestion | None,
    render_result: RenderResult | None,
) -> str:
    templates = load_templates()
    display_name = templates[recommended.template_id].display_name if recommended else "없음"
    score_text = f"{recommended.rule_score:.2f}" if recommended else "N/A"
    matched_text = ", ".join(recommended.matched_signals) if recommended and recommended.matched_signals else "(권장 템플릿 없음)"

    if recommended is None:
        draft_text = "권장 템플릿 없음"
    elif render_result is not None and render_result.success:
        draft_text = render_result.rendered_text or ""
    elif render_result is not None:
        reason = ", ".join(render_result.validation_errors or render_result.missing_slots)
        draft_text = f"템플릿 생성 실패: {reason}"
    else:
        draft_text = "템플릿 생성 실패"

    lines = [
        "[서울안전누리 신규 재난문자]",
        "",
        f"발송지역/기관: {record.sender_or_region}",
        f"발송시각: {format_timestamp(record.sent_at)}",
        "",
        "원문:",
        record.original_body,
        "",
        "권장 템플릿:",
        display_name,
        "",
        "Rule score:",
        score_text,
        "",
        "일치 근거:",
        matched_text,
        "",
        "권장 문안:",
        draft_text,
    ]
    return "\n".join(lines)


def build_mismatch_message(
    template_id: str,
    missing_slots: list[str],
    confirmed_slots: dict[str, SlotValue],
    original_body: str,
) -> str:
    templates = load_templates()
    display_name = templates[template_id].display_name if template_id in templates else template_id
    missing_text = ", ".join(missing_slots) if missing_slots else "(없음)"
    confirmed_text = (
        "\n".join(f"- {name}: {slot.value}" for name, slot in confirmed_slots.items())
        if confirmed_slots
        else "(없음)"
    )
    lines = [
        "[템플릿 생성 불가]",
        "",
        "선택 포맷:",
        display_name,
        "",
        "누락 필드:",
        missing_text,
        "",
        "확인된 값:",
        confirmed_text,
        "",
        "원문:",
        original_body,
    ]
    return "\n".join(lines)

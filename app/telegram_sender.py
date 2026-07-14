"""Telegram delivery: original-message alerts, bot command replies, and the
Service v1 template-selection / preview / confirm / cancel / AI flow.

No approval/rejection/editing/auto-posting happens here — this module only
turns data into one or more Telegram messages (optionally with an inline
keyboard) and sends them. `TELEGRAM_SEND_ENABLED` gates automatic outbound
alerts (`send_record`) but never a direct reply to something an operator
just clicked or typed (`send_text`/`send_plain_text`).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.models import DisasterMessageRecord, TelegramStatus
from app.template_renderer import (
    automation_templates,
    get_template,
    resolve_template_id,
)
from app.template_rules import TemplateSuggestion

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MESSAGE_LIMIT = 4096
MAX_SEND_RETRIES = 2
RETRY_BACKOFF_SECONDS = 2.0
CALLBACK_ACK_TIMEOUT_SECONDS = 3.0
HISTORY_REGION_LABEL_MAX = 24
_SEOUL_TZ = ZoneInfo("Asia/Seoul")

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
    """The original Part 1 MarkdownV2 alert (kept for `send_telegram_test`/back-compat)."""
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

    # -- outbound sends --------------------------------------------------

    def send_record(self, record: DisasterMessageRecord) -> TelegramSendOutcome:
        """Automatic new-alert notification — honors TELEGRAM_SEND_ENABLED."""
        return self._send(build_message(record), enforce_send_enabled=True, parse_mode="MarkdownV2")

    def send_test_text(self, text: str) -> TelegramSendOutcome:
        """Controlled connectivity test — also honors TELEGRAM_SEND_ENABLED."""
        return self._send(text, enforce_send_enabled=True, parse_mode="MarkdownV2")

    def send_text(
        self,
        text: str,
        *,
        chat_id: str | int | None = None,
        reply_to_message_id: int | None = None,
    ) -> TelegramSendOutcome:
        """Direct reply to an inbound Telegram command (/latest, /status, ...).

        Deliberately does NOT honor TELEGRAM_SEND_ENABLED: that flag gates
        automatic outbound alert notifications, not a direct response to a
        user who just explicitly messaged the bot.
        """
        return self._send(
            text,
            enforce_send_enabled=False,
            parse_mode="MarkdownV2",
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
        )

    def send_plain_text(
        self,
        text: str,
        *,
        chat_id: str | int | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
        enforce_send_enabled: bool = True,
    ) -> TelegramSendOutcome:
        """Send plain (unformatted) text — used for the entire initial-alert /
        template-selection/preview/confirm/cancel/AI flow so rendered
        template/original text is delivered byte-for-byte with no MarkdownV2
        escaping. `reply_markup`, if given, is attached only to the last chunk.

        Two distinct delivery concepts share this one method (see
        docs/service_v1.md "broadcast vs interactive delivery"):

        - automatic broadcast (a genuinely new SafeCity message): caller
          passes the configured `chat_id` explicitly and leaves
          `enforce_send_enabled=True` (the default) so TELEGRAM_SEND_ENABLED
          still gates it.
        - interactive reply (/latest, ordinary text, any callback): caller
          MUST pass the inbound `chat_id` explicitly (never the default) and
          `enforce_send_enabled=False`, since a direct reply to something an
          operator just clicked or typed must never be silently dropped by
          TELEGRAM_SEND_ENABLED and must never fall back to the configured
          broadcast chat.
        """
        return self._send(
            text,
            enforce_send_enabled=enforce_send_enabled,
            parse_mode=None,
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
        )

    def _send(
        self,
        text: str,
        *,
        enforce_send_enabled: bool,
        parse_mode: str | None,
        chat_id: str | int | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
    ) -> TelegramSendOutcome:
        if enforce_send_enabled and not self.settings.telegram_send_enabled:
            logger.info("TELEGRAM_SEND_ENABLED=false; skipping send")
            return TelegramSendOutcome(
                status=TelegramStatus.TELEGRAM_PENDING,
                error="send disabled (TELEGRAM_SEND_ENABLED=false)",
            )
        if not enforce_send_enabled and chat_id is None:
            raise TelegramPermanentError("interactive send requires explicit chat_id")
        if not self.settings.telegram_bot_token:
            raise TelegramPermanentError("TELEGRAM_BOT_TOKEN not configured")
        if enforce_send_enabled and not self.settings.telegram_configured:
            raise TelegramPermanentError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured")

        target_chat_id = chat_id if chat_id is not None else self.settings.telegram_chat_id
        send_started = time.monotonic()
        chunks = split_message(text)
        message_ids: list[str] = []
        attempts_total = 0
        try:
            for i, chunk in enumerate(chunks):
                is_last = i == len(chunks) - 1
                message_id, attempts = self._send_chunk(
                    chunk,
                    chat_id=target_chat_id,
                    parse_mode=parse_mode,
                    # Only the first chunk threads as a reply; only the
                    # last chunk carries the inline keyboard.
                    reply_to_message_id=reply_to_message_id if i == 0 else None,
                    reply_markup=reply_markup if is_last else None,
                )
                message_ids.append(message_id)
                attempts_total += attempts
        except TelegramError as exc:
            return TelegramSendOutcome(
                status=TelegramStatus.TELEGRAM_FAILED, message_ids=message_ids, error=str(exc)
            )
        send_ms = max(0, int((time.monotonic() - send_started) * 1000))
        logger.info(
            "Telegram sendMessage chat_id=%s chunks=%s attempts=%s elapsed_ms=%s",
            target_chat_id,
            len(chunks),
            attempts_total,
            send_ms,
        )
        return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_SENT, message_ids=message_ids)

    def _send_chunk(
        self,
        text: str,
        *,
        chat_id: str | int,
        parse_mode: str | None,
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
    ) -> tuple[str, int]:
        url = f"{TELEGRAM_API_BASE}/bot{self.settings.telegram_bot_token}/sendMessage"
        payload: dict = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
            payload["disable_web_page_preview"] = True
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

        last_error: str = "unknown error"
        for attempt in range(MAX_SEND_RETRIES + 1):
            try:
                response = self._client.post(url, data=payload)
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = f"network error: {exc}"
                logger.warning("Telegram send attempt %d failed: %s", attempt + 1, last_error)
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            if response.status_code == 200:
                data = response.json()
                return str(data["result"]["message_id"]), attempt + 1

            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning(
                    "Telegram send attempt %d temporary failure: %s", attempt + 1, last_error
                )
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            # Permanent failure (e.g. 400 bad request, 401/403 bad credentials).
            raise TelegramPermanentError(f"HTTP {response.status_code}: {response.text[:200]}")

        raise TelegramTemporaryError(
            f"send failed after {MAX_SEND_RETRIES + 1} attempts: {last_error}"
        )

    # -- inbound: callbacks and getUpdates --------------------------------

    def answer_callback_query(
        self, callback_query_id: str, *, text: str = "", show_alert: bool = False
    ) -> None:
        """Acknowledge an inline-button press promptly.

        Interactive acknowledgement — never gated by TELEGRAM_SEND_ENABLED.
        Requires a bot token only. Failures are logged; callers continue.
        """
        if not callback_query_id:
            return
        if not self.settings.telegram_bot_token:
            logger.warning("answerCallbackQuery skipped: TELEGRAM_BOT_TOKEN not set")
            return
        url = f"{TELEGRAM_API_BASE}/bot{self.settings.telegram_bot_token}/answerCallbackQuery"
        try:
            self._client.post(
                url,
                data={
                    "callback_query_id": callback_query_id,
                    "text": text,
                    "show_alert": show_alert,
                },
                timeout=CALLBACK_ACK_TIMEOUT_SECONDS,
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
            logger.warning("answerCallbackQuery failed: %s", exc)

    def get_updates(self, *, offset: int | None, timeout: int) -> list[dict]:
        """Long-poll `getUpdates`. One offset sequence for the whole bot — both
        ordinary message updates and callback_query updates come through here."""
        url = f"{TELEGRAM_API_BASE}/bot{self.settings.telegram_bot_token}/getUpdates"
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        response = self._client.get(
            url,
            params=params,
            timeout=httpx.Timeout(connect=5.0, read=timeout + 10.0, write=10.0, pool=10.0),
        )
        if response.status_code != 200:
            raise TelegramTemporaryError(
                f"getUpdates HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.json().get("result", [])


# --- Service v1 template flow: keyboard/callback-data and message text -----
#
# Kept in this module (rather than a new one) since it is still just "turn
# data into a Telegram message/keyboard" — the same responsibility
# `build_message`/`split_message` already have above. Orchestration (DB
# reads/writes, calling the extractor/renderer/AI client) lives in
# app/template_flow.py.

# Compact two-stage selection callbacks (category -> template).
# Forms stay well under Telegram's 64-byte callback_data limit:
#   cat:{message_id}:{HW|HT|TN|FL}
#   tpl:{message_id}:{template_id}
#   back:{message_id}
#   preview:{preview_id}:{confirm|cancel|ai}

CATEGORY_META: dict[str, dict[str, str]] = {
    "HW": {"emoji": "☔", "label": "호우"},
    "HT": {"emoji": "🔥", "label": "폭염"},
    "TN": {"emoji": "🌙", "label": "열대야"},
    "FL": {"emoji": "🌊", "label": "홍수"},
}
CATEGORY_ORDER = ("HW", "HT", "TN", "FL")

# Service v1 short codes embedded in already-sent Telegram messages.
# New menus emit canonical Excel IDs; these remain readable so old inline
# buttons keep working after the v0.2.0 catalog migration.
_LEGACY_SHORT_CODE_TO_TEMPLATE_ID: dict[str, str] = {
    "flood_adv": "FL-01",
    "rain_clr": "HW-05",
    "rain_down": "HW-04",
    "rain_multi": "HEAVY_RAIN_MULTI_LEVEL_ISSUED",
    "heat_up": "HT-03",
    "heat_adv": "HT-01",
    "trop_night": "TN-01",
    "orig": "ORIGINAL_ONLY",
}

_EXTRACTION_METHOD_LABELS = {"rule": "Rule", "ai": "AI"}


def make_callback_data(message_id: int, template_id: str) -> str:
    canonical = resolve_template_id(template_id)
    return f"tpl:{message_id}:{canonical}"


def parse_callback_data(data: str) -> tuple[int, str] | None:
    """Returns `(message_id, template_id)`, or `None` if malformed/unrecognized.

    Accepts canonical Excel IDs (`HW-05`), legacy aliases
    (`HEAVY_RAIN_CLEARED`), and pre-v0.2.0 short codes (`rain_clr`).
    """
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "tpl":
        return None
    try:
        message_id = int(parts[1])
    except ValueError:
        return None
    raw = parts[2]
    template_id = _LEGACY_SHORT_CODE_TO_TEMPLATE_ID.get(raw, raw)
    if get_template(template_id) is None:
        return None
    return message_id, resolve_template_id(template_id)


def make_category_callback_data(message_id: int, category_code: str) -> str:
    return f"cat:{message_id}:{category_code}"


def parse_category_callback_data(data: str) -> tuple[int, str] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "cat":
        return None
    try:
        message_id = int(parts[1])
    except ValueError:
        return None
    if parts[2] not in CATEGORY_META:
        return None
    return message_id, parts[2]


def make_back_callback_data(message_id: int) -> str:
    return f"back:{message_id}"


def parse_back_callback_data(data: str) -> int | None:
    parts = data.split(":")
    if len(parts) != 2 or parts[0] != "back":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def make_history_callback_data(internal_id: int) -> str:
    return f"hist:{internal_id}"


def parse_history_callback_data(data: str) -> int | None:
    parts = data.split(":")
    if len(parts) != 2 or parts[0] != "hist":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def format_history_button_label(record: DisasterMessageRecord) -> str:
    """`MM/DD HH:MM · {region}` in KST — never includes the disaster body."""
    sent = record.sent_at
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=_SEOUL_TZ)
    else:
        sent = sent.astimezone(_SEOUL_TZ)
    stamp = sent.strftime("%m/%d %H:%M")
    region = " ".join((record.sender_or_region or "").split())
    if len(region) > HISTORY_REGION_LABEL_MAX:
        region = region[: HISTORY_REGION_LABEL_MAX - 1] + "…"
    return f"{stamp} · {region}" if region else stamp


def build_history_list_message(count: int) -> str:
    return f"[최근 재난문자 {count}건]\n\n원하는 발송시각을 선택해 주세요."


def build_history_keyboard(records: list[DisasterMessageRecord]) -> dict:
    """One record per row; callback_data is only `hist:{internal_id}`."""
    rows: list[list[dict]] = []
    for record in records:
        if record.internal_id is None:
            continue
        rows.append(
            [
                {
                    "text": format_history_button_label(record),
                    "callback_data": make_history_callback_data(record.internal_id),
                }
            ]
        )
    return {"inline_keyboard": rows}


def build_history_missing_message() -> str:
    return (
        "[기록을 찾을 수 없습니다]\n\n"
        "보관기간 만료 또는 삭제로 인해 해당 메시지를 조회할 수 없습니다.\n"
        "다시 /history를 실행해 주세요."
    )


def make_preview_callback_data(preview_id: int, action: str) -> str:
    return f"preview:{preview_id}:{action}"


def parse_preview_callback_data(data: str) -> tuple[int, str] | None:
    """Returns `(preview_id, action)` where action in confirm|cancel|ai, or `None`."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "preview":
        return None
    try:
        preview_id = int(parts[1])
    except ValueError:
        return None
    if parts[2] not in ("confirm", "cancel", "ai"):
        return None
    return preview_id, parts[2]


def _chunk_buttons(buttons: list[dict], per_row: int = 2) -> list[list[dict]]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def build_selection_keyboard(message_id: int) -> dict:
    """Stage-1 keyboard: disaster categories + original-message button."""
    present = {t.category_code for t in automation_templates()}
    cat_buttons: list[dict] = []
    for code in CATEGORY_ORDER:
        if code not in present:
            continue
        meta = CATEGORY_META[code]
        cat_buttons.append(
            {
                "text": f"{meta['emoji']} {meta['label']}",
                "callback_data": make_category_callback_data(message_id, code),
            }
        )
    rows = _chunk_buttons(cat_buttons, per_row=2)
    original = get_template("ORIGINAL_ONLY")
    if original is not None and original.enabled:
        rows.append(
            [
                {
                    "text": original.button_label or "📄 원문",
                    "callback_data": make_callback_data(message_id, "ORIGINAL_ONLY"),
                }
            ]
        )
    return {"inline_keyboard": rows}


def build_category_keyboard(message_id: int, category_code: str) -> dict:
    """Stage-2 keyboard: enabled automation templates in one category."""
    templates = automation_templates(category_code=category_code)
    buttons = [
        {
            "text": t.button_label or t.display_name,
            "callback_data": make_callback_data(message_id, t.id),
        }
        for t in templates
    ]
    rows = _chunk_buttons(buttons, per_row=2)
    nav: list[dict] = [{"text": "← 뒤로", "callback_data": make_back_callback_data(message_id)}]
    original = get_template("ORIGINAL_ONLY")
    if original is not None and original.enabled:
        nav.append(
            {
                "text": original.button_label or "📄 원문",
                "callback_data": make_callback_data(message_id, "ORIGINAL_ONLY"),
            }
        )
    rows.append(nav)
    return {"inline_keyboard": rows}


def build_category_select_message(category_code: str) -> str:
    meta = CATEGORY_META.get(category_code, {"emoji": "", "label": category_code})
    return f"{meta['emoji']} {meta['label']} 템플릿을 선택해 주세요."


def build_template_alert_message(
    record: DisasterMessageRecord, recommended: TemplateSuggestion | None
) -> str:
    """Section 5 initial alert: the full original message plus a purely
    secondary, non-actionable rule-engine hint — never a "recommended"
    button and never the primary action."""
    lines = [
        "[서울안전누리 신규 재난문자]",
        "",
        f"발송지역/기관: {record.sender_or_region}",
        f"발송시각: {format_timestamp(record.sent_at)}",
        "",
        "원문:",
        record.original_body,
        "",
        "사용할 템플릿을 선택해 주세요.",
    ]
    if recommended is not None:
        template = get_template(recommended.template_id)
        display_name = template.display_name if template is not None else recommended.template_id
        lines += ["", f"(실험적 추천: {display_name}, score={recommended.rule_score:.2f} — 참고용)"]
    return "\n".join(lines)


def build_preview_keyboard(preview_id: int, *, complete: bool, ai_enabled: bool) -> dict:
    row: list[dict] = []
    if complete:
        row.append(
            {
                "text": "✅ 최종 OK",
                "callback_data": make_preview_callback_data(preview_id, "confirm"),
            }
        )
    row.append(
        {"text": "↩️ 취소", "callback_data": make_preview_callback_data(preview_id, "cancel")}
    )
    if ai_enabled:
        row.append(
            {"text": "🤖 AI로 작성", "callback_data": make_preview_callback_data(preview_id, "ai")}
        )
    return {"inline_keyboard": [row]}


def build_confirmed_preview_keyboard(preview_id: int) -> dict:
    """AI preview keyboard: only OK/Cancel — AI never re-offers itself (section 11)."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ 최종 OK",
                    "callback_data": make_preview_callback_data(preview_id, "confirm"),
                },
                {
                    "text": "↩️ 취소",
                    "callback_data": make_preview_callback_data(preview_id, "cancel"),
                },
            ]
        ]
    }


def _format_confirmed_slots(extracted_slots: dict) -> str:
    if not extracted_slots:
        return "(없음)"
    return "\n".join(f"- {name}: {slot.value}" for name, slot in extracted_slots.items())


def build_preview_complete_message(
    template_id: str, extraction_method: str, extracted_slots: dict, rendered_text: str
) -> str:
    template = get_template(template_id)
    display_name = template.display_name if template is not None else template_id
    method_label = _EXTRACTION_METHOD_LABELS.get(extraction_method, extraction_method)
    lines = [
        "[템플릿 초안]",
        "",
        "선택 포맷:",
        display_name,
    ]
    if template is not None and template.review_status == "review_required":
        lines += ["", "⚠️ 부서 검수 필요"]
    lines += [
        "",
        "추출 방식:",
        method_label,
        "",
        "확인된 값:",
        _format_confirmed_slots(extracted_slots),
        "",
        "작성 문안:",
        "",
        rendered_text,
    ]
    return "\n".join(lines)


def build_preview_incomplete_message(
    template_id: str, extracted_slots: dict, missing_slots: list[str], original_body: str
) -> str:
    template = get_template(template_id)
    display_name = template.display_name if template is not None else template_id
    missing_text = ", ".join(missing_slots) if missing_slots else "(없음)"
    lines = [
        "[템플릿 작성 미완료]",
        "",
        "선택 포맷:",
        display_name,
    ]
    if template is not None and template.review_status == "review_required":
        lines += ["", "⚠️ 부서 검수 필요"]
    lines += [
        "",
        "확인된 값:",
        _format_confirmed_slots(extracted_slots),
        "",
        "누락 필드:",
        missing_text,
        "",
        "원문:",
        original_body,
    ]
    return "\n".join(lines)


def build_confirmation_message(template_id: str, final_rendered_text: str) -> str:
    template = get_template(template_id)
    display_name = template.display_name if template is not None else template_id
    lines = [
        "[최종 확정 완료]",
        "",
        "포맷:",
        display_name,
        "",
        "작성 문안:",
        "",
        final_rendered_text,
    ]
    return "\n".join(lines)


def build_cancel_message(original_body: str) -> str:
    lines = ["[선택 취소]", "", "다른 템플릿을 선택해 주세요.", "", "원문:", original_body]
    return "\n".join(lines)


def build_confirm_failed_message() -> str:
    lines = [
        "[최종 확정 실패]",
        "",
        "DB 저장 중 오류가 발생했습니다.",
        "같은 초안에서 `최종 OK`를 다시 눌러 주세요.",
    ]
    return "\n".join(lines)


def build_stale_preview_message() -> str:
    lines = [
        "[사용할 수 없는 초안]",
        "",
        "더 최신 초안이 있거나 이미 취소된 초안입니다.",
        "최신 Telegram 메시지의 버튼을 사용해 주세요.",
    ]
    return "\n".join(lines)


def build_unavailable_request_message() -> str:
    """Shown when a callback's originating chat/user doesn't match the
    preview it targets — never resurrects or reveals the preview's content
    to the mismatched caller."""
    lines = ["[사용할 수 없는 요청]", "", "이 초안이 생성된 Telegram 대화에서 다시 시도해 주세요."]
    return "\n".join(lines)

"""Telegram inbound handling via long polling — the single bot process.

Local-testing transport only (see docs/local_runtime.md for the future
webhook migration note). One `getUpdates` offset sequence handles both:

- ordinary messages: authorize against TELEGRAM_ALLOWED_USER_IDS (never by
  chat ID alone), reply to `/latest`, ordinary text (same as `/latest`),
  `/status`, `/pause`, `/resume`, `/help`, and an optional dev-only
  `/shutdown`
- `callback_query` updates (template selection, preview confirm/cancel/AI) —
  delegated to `app.template_flow.dispatch_callback`, which does its own
  authorization and duplicate-callback checks

Never crashes the loop on malformed/unsupported update types. This module
does not decide *when* to poll Seoul SafeCity — that is app/poller.py's
job. The two communicate only through the shared SQLite `system_state` row
(polling_enabled) and the `messages` table.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.database import Database
from app.models import TelegramStatus
from app.process_lock import SingleInstanceLock
from app.telegram_sender import (
    TELEGRAM_API_BASE,
    TelegramSender,
    escape_markdown_v2,
    format_timestamp,
)
from app.template_flow import dispatch_callback

__all__ = [
    "SingleInstanceLock",
    "TelegramBotRunner",
    "TelegramPollError",
    "build_latest_reply",
    "build_status_reply",
]

logger = logging.getLogger(__name__)

GETUPDATES_TIMEOUT_SECONDS = 25
GETUPDATES_LIMIT = 20
GETUPDATES_READ_TIMEOUT = GETUPDATES_TIMEOUT_SECONDS + 10
MAX_BACKOFF_SECONDS = 30.0
INITIAL_BACKOFF_SECONDS = 1.0

UNAUTHORIZED_REPLY = escape_markdown_v2("이 봇을 사용할 권한이 없습니다.")
HELP_TEXT = escape_markdown_v2(
    "사용 가능한 명령어:\n"
    "/latest - 가장 최근 수집된 재난문자 조회\n"
    "/status - 시스템 상태 조회\n"
    "/pause - 자동 수집 및 알림 일시정지\n"
    "/resume - 자동 수집 및 알림 재개\n"
    "/help - 도움말\n"
    "일반 텍스트 메시지를 보내도 /latest와 동일하게 동작합니다."
)
NO_MESSAGES_REPLY = escape_markdown_v2("아직 저장된 재난문자가 없습니다.")
SHUTDOWN_DISABLED_REPLY = escape_markdown_v2(
    "개발용 종료 명령이 비활성화되어 있습니다 (LOCAL_SHUTDOWN_COMMAND_ENABLED=false)."
)
SHUTDOWN_ACCEPTED_REPLY = escape_markdown_v2(
    "개발용 종료 명령을 수신했습니다. 봇 프로세스를 종료합니다."
)


class TelegramPollError(RuntimeError):
    """A getUpdates call failed; the caller should back off and retry."""


@dataclass
class DispatchOutcome:
    """What happened for one update — used only for tests/observability."""

    handled: bool
    authorized: bool | None
    command: str | None


SEOUL_TZ = ZoneInfo("Asia/Seoul")


def build_status_reply(db: Database, settings: Settings, *, now: datetime | None = None) -> str:
    now = now or datetime.now(tz=SEOUL_TZ)
    state = db.get_system_state()
    report = db.status_report()

    lines = ["\\[Seoul News Tracking 상태\\]", ""]

    if not state.polling_enabled:
        lines.append(f"수집 상태: {escape_markdown_v2('일시정지')}")
        if state.paused_at:
            lines.append(f"중지 시각: {escape_markdown_v2(format_timestamp(state.paused_at))}")
        if state.paused_by is not None:
            lines.append(f"중지 요청자: {escape_markdown_v2(str(state.paused_by))}")
    else:
        stale = False
        if state.last_successful_poll_at is not None:
            elapsed_minutes = (now - state.last_successful_poll_at).total_seconds() / 60
            stale = elapsed_minutes > settings.status_stale_after_minutes
            status_word = "점검 필요" if stale else "실행 중"
            lines.append(f"수집 상태: {escape_markdown_v2(status_word)}")
            lines.append(
                f"마지막 정상 수집: {escape_markdown_v2(format_timestamp(state.last_successful_poll_at))}"
            )
            if stale:
                lines.append(
                    escape_markdown_v2(f"마지막 정상 수집 후 {elapsed_minutes:.0f}분 경과")
                )
            else:
                lines.append(escape_markdown_v2(f"최근 수집 이후: {elapsed_minutes:.0f}분"))
        else:
            lines.append(f"수집 상태: {escape_markdown_v2('실행 중 (아직 수집 이력 없음)')}")

    if state.last_new_message_at:
        lines.append(
            f"마지막 신규 문자: {escape_markdown_v2(format_timestamp(state.last_new_message_at))}"
        )
    lines.append(escape_markdown_v2(f"저장된 문자: {report['total_messages']}건"))
    lines.append(escape_markdown_v2(f"DB 보관기간: {settings.message_retention_days}일"))
    lines.append(escape_markdown_v2(f"실행이력 보관기간: {settings.run_history_retention_days}일"))
    lines.append(
        escape_markdown_v2(
            f"최근 오류: {state.last_poll_error if state.last_poll_error else '없음'}"
        )
    )

    return "\n".join(lines)


def build_latest_reply(db: Database, settings: Settings, *, now: datetime | None = None) -> str:
    now = now or datetime.now(tz=SEOUL_TZ)
    record = db.get_latest_record()
    if record is None:
        return NO_MESSAGES_REPLY

    state = db.get_system_state()
    if state.last_successful_poll_at is not None:
        elapsed_minutes = (now - state.last_successful_poll_at).total_seconds() / 60
        if elapsed_minutes > settings.status_stale_after_minutes:
            data_state = f"마지막 정상 수집 후 {elapsed_minutes:.0f}분 경과 (점검 필요)"
        else:
            data_state = f"최근 수집 정상 (마지막 정상 수집 {elapsed_minutes:.0f}분 전)"
    else:
        data_state = "수집 이력 없음"

    lines = [
        "\\[가장 최근 수집된 재난문자\\]",
        "",
        f"발송지역/기관: {escape_markdown_v2(record.sender_or_region)}",
        f"발송시각: {escape_markdown_v2(format_timestamp(record.sent_at))}",
        "",
        "원문:",
        escape_markdown_v2(record.original_body),
        "",
        "수집시각:",
        escape_markdown_v2(format_timestamp(record.detected_at)),
        "",
        "데이터 상태:",
        escape_markdown_v2(data_state),
        "",
        "출처:",
        escape_markdown_v2(record.source_url),
    ]
    return "\n".join(lines)


class TelegramBotRunner:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        sender: TelegramSender | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.settings = settings
        self.db = db
        self.sender = sender or TelegramSender(settings)
        self._owns_sender = sender is None
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=5.0, read=GETUPDATES_READ_TIMEOUT, write=10.0, pool=GETUPDATES_READ_TIMEOUT
            ),
            transport=transport,
        )
        self._offset = 0
        self._running = True

    def close(self) -> None:
        self._client.close()
        if self._owns_sender:
            self.sender.close()

    def stop(self) -> None:
        self._running = False

    def run_forever(self, *, max_iterations: int | None = None) -> None:
        """Run the getUpdates loop until stopped. `max_iterations` is for tests only."""
        logger.info("Telegram bot: starting long polling")
        backoff = INITIAL_BACKOFF_SECONDS
        iterations = 0
        while self._running:
            try:
                updates = self.get_updates()
                backoff = INITIAL_BACKOFF_SECONDS
            except TelegramPollError as exc:
                logger.warning("getUpdates failed, backing off %.1fs: %s", backoff, exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    break
                continue

            for update in updates:
                try:
                    self.dispatch(update)
                except Exception:
                    logger.exception(
                        "unhandled error processing update_id=%s", update.get("update_id")
                    )
                finally:
                    self._offset = update["update_id"] + 1

            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break

    def get_updates(self) -> list[dict[str, Any]]:
        url = f"{TELEGRAM_API_BASE}/bot{self.settings.telegram_bot_token}/getUpdates"
        try:
            response = self._client.get(
                url,
                params={
                    "offset": self._offset,
                    "timeout": GETUPDATES_TIMEOUT_SECONDS,
                    "limit": GETUPDATES_LIMIT,
                },
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise TelegramPollError(f"network error: {exc}") from exc

        if response.status_code == 409:
            raise TelegramPollError(
                "HTTP 409 Conflict: another getUpdates long-poll is active for this bot token"
            )
        if response.status_code != 200:
            raise TelegramPollError(f"HTTP {response.status_code}: {response.text[:200]}")

        data = response.json()
        if not data.get("ok"):
            raise TelegramPollError(f"Telegram API returned ok=false: {data}")
        return data.get("result", [])

    def dispatch(self, update: dict[str, Any]) -> DispatchOutcome:
        if not isinstance(update, dict):
            logger.debug("ignoring non-dict update: %r", type(update))
            return DispatchOutcome(handled=False, authorized=None, command=None)

        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            dispatch_callback(self.db, self.settings, self.sender, callback_query)
            return DispatchOutcome(handled=True, authorized=None, command="callback_query")

        message = update.get("message")
        if not isinstance(message, dict):
            ignored_keys = set(update) - {"update_id"}
            logger.debug("ignoring non-message update (keys=%s)", ignored_keys)
            return DispatchOutcome(handled=False, authorized=None, command=None)

        chat = message.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        message_id = message.get("message_id")
        from_user = message.get("from")
        user_id = from_user.get("id") if isinstance(from_user, dict) else None
        text = message.get("text")

        if chat_id is None or user_id is None:
            logger.debug("ignoring malformed message update (missing chat/from id)")
            return DispatchOutcome(handled=False, authorized=None, command=None)

        if not isinstance(text, str):
            logger.debug("ignoring non-text message from user_id=%s", user_id)
            return DispatchOutcome(handled=False, authorized=None, command=None)

        authorized = user_id in self.settings.telegram_allowed_user_ids
        stripped = text.strip()
        command = stripped.split()[0].lower() if stripped else ""

        if not authorized:
            logger.warning(
                "rejected unauthorized user_id=%s command=%r", user_id, command or "(text)"
            )
            self._reply(chat_id, message_id, UNAUTHORIZED_REPLY)
            return DispatchOutcome(handled=True, authorized=False, command=command)

        reply_text = self._handle_authorized(stripped, command, user_id)
        self._reply(chat_id, message_id, reply_text)
        return DispatchOutcome(handled=True, authorized=True, command=command)

    def _handle_authorized(self, stripped_text: str, command: str, user_id: int) -> str:
        if command == "/latest":
            return build_latest_reply(self.db, self.settings)
        if command == "/status":
            return build_status_reply(self.db, self.settings)
        if command == "/pause":
            return self._handle_pause(user_id)
        if command == "/resume":
            return self._handle_resume(user_id)
        if command == "/help":
            return HELP_TEXT
        if command == "/shutdown":
            return self._handle_shutdown(user_id)
        # Ordinary text behaves the same as /latest.
        return build_latest_reply(self.db, self.settings)

    def _handle_pause(self, user_id: int) -> str:
        changed = self.db.pause_polling(actor_user_id=user_id)
        self.db.record_control_event(
            "pause", actor_user_id=user_id, detail="via telegram" if changed else "already paused"
        )
        logger.info("pause requested by user_id=%s (changed=%s)", user_id, changed)
        if changed:
            return escape_markdown_v2("자동 수집 및 알림을 일시정지했습니다.")
        return escape_markdown_v2("이미 일시정지 상태입니다.")

    def _handle_resume(self, user_id: int) -> str:
        changed = self.db.resume_polling(actor_user_id=user_id)
        self.db.record_control_event(
            "resume", actor_user_id=user_id, detail="via telegram" if changed else "already active"
        )
        logger.info("resume requested by user_id=%s (changed=%s)", user_id, changed)
        if changed:
            return escape_markdown_v2("자동 수집 및 알림을 재개했습니다.")
        return escape_markdown_v2("이미 실행 중입니다.")

    def _handle_shutdown(self, user_id: int) -> str:
        if not self.settings.local_shutdown_command_enabled:
            return SHUTDOWN_DISABLED_REPLY
        logger.warning("local dev shutdown requested by user_id=%s", user_id)
        self.stop()
        return SHUTDOWN_ACCEPTED_REPLY

    def _reply(self, chat_id: int, message_id: int | None, text: str) -> None:
        outcome = self.sender.send_text(text, chat_id=chat_id, reply_to_message_id=message_id)
        if outcome.status == TelegramStatus.TELEGRAM_FAILED:
            logger.error("failed to send Telegram reply to chat_id=%s: %s", chat_id, outcome.error)

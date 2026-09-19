"""Telegram inbound handling via long polling — the single bot process.

Local-testing transport only (see docs/local_runtime.md for the future
webhook migration note). One `getUpdates` offset sequence handles both:

- ordinary messages: authorize against TELEGRAM_ALLOWED_USER_IDS (never by
  chat ID alone), reply to `/latest`, ordinary text (same as `/latest`),
  `/status`, `/subscribe`, `/unsubscribe`, `/mute`, `/unmute`, `/pause`
  (alias of `/mute`), `/resume` (alias of `/unmute`), `/help`, and an
  optional dev-only `/shutdown`
- `callback_query` updates (template selection, preview confirm/cancel/AI) —
  delegated to `app.template_flow.dispatch_callback`, which does its own
  authorization, private-chat, and duplicate-callback checks

Every one of the above is a **personal interactive reply**: it always targets
the chat_id the inbound message/callback actually came from, never the legacy
`TELEGRAM_CHAT_ID` — see docs/independent_operator_model.md.

Operations are strictly private-chat only (v0.4.0): a command or button used
in a group/supergroup/channel is rejected with a hint and never processed or
rerouted. Every authorized operator is an equal, independent entity — each
subscribes from their own private chat and receives every alert
independently. On-demand AI runs on a bounded worker pool so one operator's
AI request never blocks another operator's non-AI command.

Never crashes the loop on malformed/unsupported update types. This module
does not decide *when* to poll Seoul SafeCity — that is app/poller.py's job.
The two communicate only through the shared SQLite tables.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.database import Database
from app.models import TelegramStatus
from app.process_lock import SingleInstanceLock
from app.telegram_routing import (
    chat_type_of,
    elapsed_ms_since,
    log_telegram_route,
    normalize_bot_command,
)
from app.telegram_sender import (
    TELEGRAM_API_BASE,
    TelegramSender,
    escape_markdown_v2,
    format_timestamp,
)
from app.template_flow import dispatch_callback, send_history_list, send_latest_alert
from app.version import get_version

__all__ = [
    "SingleInstanceLock",
    "TelegramBotRunner",
    "TelegramPollError",
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
    "사용 가능한 명령어\n"
    "\n"
    "[조회]\n"
    "/latest - 가장 최근 재난문자 조회\n"
    "/history - 최근 재난문자 10건 선택\n"
    "/status - 내 알림 상태 및 공통 수집 상태 조회\n"
    "/stauts - /status 호환 별칭\n"
    "\n"
    "[내 알림 설정]\n"
    "/subscribe - 이 개인 채팅으로 자동 알림 수신 시작\n"
    "/unsubscribe - 자동 알림 수신 중지 (구독 해제)\n"
    "/mute - 자동 알림 일시 음소거\n"
    "/unmute - 음소거 해제\n"
    "/pause - /mute 와 동일 (개인 음소거)\n"
    "/resume - /unmute 와 동일 (음소거 해제)\n"
    "\n"
    "[기타]\n"
    "/help - 도움말\n"
    "\n"
    "안내\n"
    "- 모든 운영 명령과 버튼은 봇과의 1:1 개인 채팅에서만 동작합니다.\n"
    "- 운영자는 각자 독립적으로 알림을 받고, 템플릿/미리보기/AI/최종 결정을 각자 관리합니다.\n"
    "- 일반 텍스트 메시지를 보내도 /latest와 동일하게 동작합니다."
)
SHUTDOWN_DISABLED_REPLY = escape_markdown_v2(
    "개발용 종료 명령이 비활성화되어 있습니다 (LOCAL_SHUTDOWN_COMMAND_ENABLED=false)."
)
SHUTDOWN_ACCEPTED_REPLY = escape_markdown_v2(
    "개발용 종료 명령을 수신했습니다. 봇 프로세스를 종료합니다."
)
PRIVATE_CHAT_ONLY_REPLY = escape_markdown_v2(
    "운영 명령과 버튼은 봇과의 1:1 개인 채팅에서만 동작합니다. 그룹/채널에서는 처리되지 않습니다."
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


_PERSONAL_STATUS_LABEL = {
    "active": "수신 중",
    "muted": "일시정지 (음소거)",
    "unsubscribed": "구독 해제",
    "none": "미등록",
}

# v0.5.0: collector.METHOD_MOIS_API / collector.METHOD_SAFEKOREA_FALLBACK
# mapped to the shared /status display label (no URL, no secret).
_SOURCE_METHOD_LABEL = {
    "mois_safetydata_api": "행정안전부 API",
    "safekorea_html_fallback": "국민안전24 fallback",
    "none": "수집 실패 (원천 없음)",
}
_SOURCE_ERROR_LABEL = {
    "auth_failed": "인증 실패",
    "http_error": "HTTP 오류",
    "rate_limited": "요청 제한",
    "schema_error": "응답 형식 오류",
    "timeout": "시간 초과",
    "unknown": "알 수 없는 오류",
}


def _safe_runtime_identity(value: str) -> str:
    """Return a short configured label, never a path or multiline detail."""
    candidate = str(value).strip()
    if (
        not candidate
        or len(candidate) > 64
        or any(ord(char) < 32 for char in candidate)
        or "/" in candidate
        or "\\" in candidate
    ):
        return "unconfigured"
    return candidate


def _format_iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return format_timestamp(datetime.fromisoformat(value))
    except ValueError:
        return None


def build_status_reply(
    db: Database,
    settings: Settings,
    *,
    user_id: int | None = None,
    chat_id: int | None = None,
    now: datetime | None = None,
) -> str:
    """Two independent sections: the caller's OWN personal alert status and
    the shared common-collection status. Never exposes another operator's
    IDs or state — only the requesting user's own subscription/delivery is
    read (v0.4.0)."""
    now = now or datetime.now(tz=SEOUL_TZ)
    state = db.get_system_state()
    report = db.status_report()

    lines: list[str] = ["\\[내 알림 상태\\]", ""]
    if user_id is None:
        lines.append(escape_markdown_v2("알림 수신: 확인 불가 (사용자 식별 실패)"))
    else:
        status = db.subscription_status_for(user_id)
        lines.append(escape_markdown_v2(f"알림 수신: {_PERSONAL_STATUS_LABEL.get(status, status)}"))
        sub = db.get_subscription_by_user(user_id)
        lines.append(
            escape_markdown_v2(f"개인 채팅 등록: {'등록됨' if sub is not None else '미등록'}")
        )
        latest = db.get_latest_delivery_for_user(user_id)
        sent_ts = _format_iso(latest["sent_at"]) if latest is not None else None
        lines.append(escape_markdown_v2(f"최근 자동 발송: {sent_ts if sent_ts else '없음'}"))
        if latest is not None and latest["status"] == "failed":
            # Delivery errors can originate in an HTTP response and are not a
            # safe operator-facing diagnostic surface. Keep /status generic.
            lines.append(escape_markdown_v2("최근 발송 오류: 있음"))
        else:
            lines.append(escape_markdown_v2("최근 발송 오류: 없음"))

    lines.append("")
    lines.append("\\[공통 수집 상태\\]")
    lines.append(
        escape_markdown_v2(f"배포: {_safe_runtime_identity(settings.deployment_label)}")
    )
    lines.append(escape_markdown_v2(f"실행 모드: {_safe_runtime_identity(settings.runtime_mode)}"))
    lines.append(escape_markdown_v2(f"버전: {get_version()}"))

    if not state.polling_enabled:
        lines.append(f"수집 상태: {escape_markdown_v2('일시정지')}")
        if state.paused_at:
            lines.append(f"중지 시각: {escape_markdown_v2(format_timestamp(state.paused_at))}")
        # Intentionally NOT shown: paused_by/resumed_by. The pause timestamp may
        # appear, but never the actor's Telegram user id — a /status reply must
        # not leak another operator's identifier (v0.4.1). The attribution is
        # preserved in the DB (system_state.paused_by) and internal logs for
        # administrative audit only.
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

    # v0.5.0 source observability (see docs/live_source_migration_mois_api_plan.md
    # §14). Never shows the service key, a request URL/query string, or the
    # message body — only the source label and a short sanitized error category.
    source_label = _SOURCE_METHOD_LABEL.get(state.last_collection_source, "알 수 없음")
    lines.append(escape_markdown_v2(f"최근 수집 원천: {source_label if source_label else '없음'}"))
    lines.append(
        escape_markdown_v2(
            "Primary 최근 오류: "
            f"{_SOURCE_ERROR_LABEL.get(state.last_primary_error_category, '알 수 없음') if state.last_primary_error_category else '없음'}"
        )
    )

    lines.append(escape_markdown_v2(f"저장된 문자: {report['total_messages']}건"))
    lines.append(escape_markdown_v2(f"DB 보관기간: {settings.message_retention_days}일"))
    lines.append(escape_markdown_v2(f"실행이력 보관기간: {settings.run_history_retention_days}일"))
    # last_poll_error is retained for local administrative diagnostics, but it
    # may contain an exception message, URL, path, or other host detail.
    lines.append(escape_markdown_v2(f"최근 오류: {'있음' if state.last_poll_error else '없음'}"))

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
        # Bounded pool for on-demand AI generation so one operator's AI call
        # never blocks another operator's non-AI commands (v0.4.0).
        self._ai_executor = ThreadPoolExecutor(
            max_workers=settings.telegram_ai_workers,
            thread_name_prefix="ai-preview",
        )
        # One-time subscription bootstrap from historical previews / legacy
        # TELEGRAM_CHAT_ID (no-op once telegram_subscriptions is populated).
        try:
            seeded = db.seed_subscriptions_if_empty(
                allowed_user_ids=settings.telegram_allowed_user_ids,
                legacy_chat_id=settings.telegram_chat_id or None,
            )
            if seeded:
                logger.info("seeded %d initial personal subscription(s)", seeded)
        except Exception:
            logger.exception("subscription bootstrap failed (continuing)")
        self._offset = db.get_telegram_update_offset()
        self._running = True
        logger.info(
            "Telegram bot starting: version=%s persisted_offset=%d ai_workers=%d",
            get_version(),
            self._offset,
            settings.telegram_ai_workers,
        )

    def close(self) -> None:
        # Let in-flight AI tasks finish (bounded, short) before tearing down.
        self._ai_executor.shutdown(wait=True)
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
                    self.db.set_telegram_update_offset(self._offset)

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
        except httpx.TransportError as exc:
            raise TelegramPollError(f"network error: {exc}") from exc

        if response.status_code == 409:
            raise TelegramPollError(
                "HTTP 409 Conflict: another getUpdates long-poll is active for this bot token"
            )
        if response.status_code != 200:
            raise TelegramPollError(f"HTTP {response.status_code}: {response.text[:200]}")

        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramPollError(f"invalid JSON response: {exc}") from exc
        if not data.get("ok"):
            raise TelegramPollError(f"Telegram API returned ok=false: {data}")
        return data.get("result", [])

    def dispatch(self, update: dict[str, Any]) -> DispatchOutcome:
        started = time.monotonic()
        update_id = update.get("update_id") if isinstance(update, dict) else None

        if not isinstance(update, dict):
            logger.debug("ignoring non-dict update: %r", type(update))
            return DispatchOutcome(handled=False, authorized=None, command=None)

        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            dispatch_callback(
                self.db,
                self.settings,
                self.sender,
                callback_query,
                ai_executor=self._ai_executor,
            )
            return DispatchOutcome(handled=True, authorized=None, command="callback_query")

        message = update.get("message")
        if not isinstance(message, dict):
            ignored_keys = set(update) - {"update_id"}
            logger.debug("ignoring non-message update (keys=%s)", ignored_keys)
            return DispatchOutcome(handled=False, authorized=None, command=None)

        chat = message.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        chat_type = chat_type_of(chat if isinstance(chat, dict) else None)
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
        command = normalize_bot_command(stripped)
        command_category = command if command.startswith("/") else "(text)"
        threshold = self.settings.telegram_slow_interaction_ms

        if not authorized:
            logger.warning(
                "rejected unauthorized user_id=%s command=%r", user_id, command or "(text)"
            )
            self._reply(chat_id, message_id, UNAUTHORIZED_REPLY)
            log_telegram_route(
                action=command_category,
                delivery_mode="interactive",
                outcome="unauthorized",
                elapsed_ms=elapsed_ms_since(started),
                chat_type=chat_type,
                update_id=update_id,
                operator_user_id=user_id,
                routed_chat_id=chat_id,
                slow_threshold_ms=threshold,
            )
            return DispatchOutcome(handled=True, authorized=False, command=command)

        # Operations are private-chat only. An authorized operator in a group/
        # supergroup/channel is told to use a private chat — never processed,
        # never rerouted to a private chat.
        if chat_type != "private":
            self._reply(chat_id, message_id, PRIVATE_CHAT_ONLY_REPLY)
            log_telegram_route(
                action=command_category,
                delivery_mode="interactive",
                outcome="group_rejected",
                elapsed_ms=elapsed_ms_since(started),
                chat_type=chat_type,
                update_id=update_id,
                operator_user_id=user_id,
                routed_chat_id=chat_id,
                slow_threshold_ms=threshold,
            )
            return DispatchOutcome(handled=True, authorized=True, command=command)

        # Every authorized private interaction touches this operator's own
        # subscription (created active on first contact; a muted/unsubscribed
        # operator is never silently reactivated).
        try:
            self.db.register_or_touch_subscription(
                user_id=user_id,
                chat_id=chat_id,
                chat_type="private",
                registration_source="message",
            )
        except Exception:
            logger.exception("failed to touch subscription for user_id=%s", user_id)

        reply_text = self._handle_authorized(stripped, command, user_id, chat_id, message_id)
        if reply_text is not None:
            self._reply(chat_id, message_id, reply_text)
        log_telegram_route(
            action=command_category if command.startswith("/") else "latest_text",
            delivery_mode="interactive",
            outcome="handled",
            elapsed_ms=elapsed_ms_since(started),
            chat_type=chat_type,
            update_id=update_id,
            operator_user_id=user_id,
            routed_chat_id=chat_id,
            slow_threshold_ms=threshold,
        )
        return DispatchOutcome(handled=True, authorized=True, command=command)

    def _handle_authorized(
        self, stripped_text: str, command: str, user_id: int, chat_id: int, message_id: int | None
    ) -> str | None:
        if command == "/latest":
            self._send_latest_alert(chat_id)
            return None
        if command == "/history":
            self._send_history(chat_id)
            return None
        if command in ("/status", "/stauts"):
            return build_status_reply(self.db, self.settings, user_id=user_id, chat_id=chat_id)
        if command == "/subscribe":
            return self._handle_subscribe(user_id, chat_id)
        if command == "/unsubscribe":
            return self._handle_unsubscribe(user_id, chat_id)
        # /pause and /resume are personal aliases of /mute and /unmute — they
        # no longer touch the shared polling state (v0.4.0).
        if command in ("/mute", "/pause"):
            return self._handle_mute(user_id, chat_id)
        if command in ("/unmute", "/resume"):
            return self._handle_unmute(user_id, chat_id)
        if command == "/help":
            return HELP_TEXT
        if command == "/shutdown":
            return self._handle_shutdown(user_id)
        # Ordinary text (and unrecognized slash commands other than the ones
        # above) behaves the same as /latest.
        self._send_latest_alert(chat_id)
        return None

    def _send_latest_alert(self, chat_id: int) -> None:
        outcome = send_latest_alert(self.db, self.settings, self.sender, chat_id=chat_id)
        if outcome.status == TelegramStatus.TELEGRAM_FAILED:
            logger.error("failed to send /latest alert to chat_id=%s: %s", chat_id, outcome.error)

    def _send_history(self, chat_id: int) -> None:
        outcome = send_history_list(self.db, self.settings, self.sender, chat_id=chat_id)
        if outcome.status == TelegramStatus.TELEGRAM_FAILED:
            logger.error("failed to send /history to chat_id=%s: %s", chat_id, outcome.error)

    def _handle_subscribe(self, user_id: int, chat_id: int) -> str:
        """Start (or re-activate) personal automatic delivery to THIS chat."""
        before = self.db.subscription_status_for(user_id)
        self.db.set_subscription_status(user_id=user_id, chat_id=chat_id, status="active")
        logger.info("subscribe by user_id=%s (was=%s)", user_id, before)
        if before == "active":
            return escape_markdown_v2("이미 자동 알림을 수신 중입니다.")
        return escape_markdown_v2("이 개인 채팅으로 자동 알림을 받도록 설정했습니다.")

    def _handle_unsubscribe(self, user_id: int, chat_id: int) -> str:
        """Stop personal automatic delivery entirely (구독 해제)."""
        before = self.db.subscription_status_for(user_id)
        self.db.set_subscription_status(user_id=user_id, chat_id=chat_id, status="unsubscribed")
        logger.info("unsubscribe by user_id=%s (was=%s)", user_id, before)
        if before in ("none", "unsubscribed"):
            return escape_markdown_v2("이미 자동 알림을 받고 있지 않습니다.")
        return escape_markdown_v2(
            "자동 알림 수신을 해제했습니다. 다시 받으려면 /subscribe 를 보내주세요."
        )

    def _handle_mute(self, user_id: int, chat_id: int) -> str:
        """Temporarily silence personal automatic delivery (개인 음소거).

        Does NOT touch the shared polling state — only this operator's own
        subscription. Other operators keep receiving alerts."""
        before = self.db.subscription_status_for(user_id)
        self.db.set_subscription_status(user_id=user_id, chat_id=chat_id, status="muted")
        logger.info("mute by user_id=%s (was=%s)", user_id, before)
        if before == "muted":
            return escape_markdown_v2("이미 음소거 상태입니다.")
        return escape_markdown_v2("자동 알림을 음소거했습니다. 해제하려면 /unmute 를 보내주세요.")

    def _handle_unmute(self, user_id: int, chat_id: int) -> str:
        """Resume personal automatic delivery after a mute."""
        before = self.db.subscription_status_for(user_id)
        self.db.set_subscription_status(user_id=user_id, chat_id=chat_id, status="active")
        logger.info("unmute by user_id=%s (was=%s)", user_id, before)
        if before == "active":
            return escape_markdown_v2("이미 자동 알림을 수신 중입니다.")
        return escape_markdown_v2("자동 알림 음소거를 해제했습니다.")

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

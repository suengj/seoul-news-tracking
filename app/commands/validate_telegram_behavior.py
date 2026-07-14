"""Offline Telegram routing / acknowledgment validation (no network).

Usage:
    python -m app.commands.validate_telegram_behavior
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.database import Database
from app.models import DisasterMessageRecord, TelegramStatus
from app.telegram_bot import TelegramBotRunner
from app.telegram_sender import (
    TelegramSendOutcome,
    TelegramSender,
    make_callback_data,
    make_category_callback_data,
    make_history_callback_data,
    make_preview_callback_data,
)
from app.template_flow import dispatch_callback, send_history_list, send_initial_alert

SEOUL_TZ = ZoneInfo("Asia/Seoul")
BROADCAST = "100"
USER_A, CHAT_A = 201, 200
USER_B, CHAT_B = 202, 300


def _settings(**overrides) -> Settings:
    defaults = dict(
        telegram_bot_token="TEST_TOKEN",
        telegram_chat_id=BROADCAST,
        telegram_allowed_user_ids=(USER_A, USER_B),
        telegram_send_enabled=True,
        database_path=Path("unused.db"),
        log_level="INFO",
        poll_interval_seconds=300,
        status_stale_after_minutes=15,
        message_retention_days=90,
        run_history_retention_days=14,
        store_successful_noop_runs=False,
        cleanup_interval_hours=24,
        tombstone_retention_days=365,
        local_shutdown_command_enabled=False,
        telegram_slow_interaction_ms=2000,
    )
    defaults.update(overrides)
    return Settings(**defaults)


class RecordingSender:
    def __init__(self, *, settings: Settings):
        self.settings = settings
        self.sent: list[dict] = []
        self.answered: list[str] = []

    def answer_callback_query(self, callback_query_id, *, text="", show_alert=False):
        self.answered.append(callback_query_id)

    def send_plain_text(
        self,
        text,
        *,
        chat_id=None,
        reply_to_message_id=None,
        reply_markup=None,
        enforce_send_enabled=True,
    ):
        if enforce_send_enabled and not self.settings.telegram_send_enabled:
            return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_PENDING)
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "enforce_send_enabled": enforce_send_enabled,
            }
        )
        return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_SENT, message_ids=["1"])


def _record(source_id: str, body: str = "본문") -> DisasterMessageRecord:
    return DisasterMessageRecord(
        source_id=source_id,
        sender_or_region="서울특별시 테스트구",
        sent_at=datetime(2026, 7, 14, 12, 0, tzinfo=SEOUL_TZ),
        original_body=body,
        source_url="https://example.invalid/",
        detected_at=datetime(2026, 7, 14, 12, 1, tzinfo=SEOUL_TZ),
        raw_payload={"disstrSmsSn": source_id},
    )


def _message_update(update_id, user_id, text, *, chat_id):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id},
            "text": text,
        },
    }


def _cb(data, *, user_id, chat_id, cbq_id):
    return {
        "id": cbq_id,
        "from": {"id": user_id},
        "message": {"chat": {"id": chat_id, "type": "private"}},
        "data": data,
    }


def main() -> int:
    results: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        results.append((name, ok))

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "validate.db"
        db = Database(db_path)
        settings = _settings(database_path=db_path)
        settings_disabled = _settings(database_path=db_path, telegram_send_enabled=False)

        mid_a = db.insert(_record("VA", "호우주의보 해제 [테스트구]"))
        mid_b = db.insert(_record("VB", "폭염주의보 발효 [테스트구]"))

        sender = RecordingSender(settings=settings)
        send_initial_alert(
            sender,
            db,
            settings,
            db.get_by_internal_id(mid_a),
            target_chat_id=settings.telegram_chat_id,
            persist_suggestion=True,
            enforce_send_enabled=True,
        )
        check(
            "Broadcast isolation",
            bool(sender.sent)
            and sender.sent[0]["chat_id"] == BROADCAST
            and sender.sent[0]["enforce_send_enabled"] is True,
        )

        sent: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = dict(httpx.QueryParams(request.content.decode()))
            sent.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

        transport = httpx.MockTransport(handler)
        bot = TelegramBotRunner(
            settings,
            db,
            sender=TelegramSender(settings, client=httpx.Client(transport=transport)),
            transport=transport,
        )
        bot.dispatch(_message_update(1, USER_A, "/latest", chat_id=CHAT_A))
        check("User A private routing", len(sent) == 1 and sent[0]["chat_id"] == str(CHAT_A))

        sent.clear()
        bot.dispatch(_message_update(2, USER_B, "hello", chat_id=CHAT_B))
        check("User B private routing", len(sent) == 1 and sent[0]["chat_id"] == str(CHAT_B))

        before = db._conn.execute("SELECT COUNT(*) AS c FROM template_suggestions").fetchone()["c"]
        fake = RecordingSender(settings=settings)
        send_history_list(db, settings, fake, chat_id=CHAT_A)
        fake_b = RecordingSender(settings=settings)
        send_history_list(db, settings, fake_b, chat_id=CHAT_B)
        after_list = db._conn.execute("SELECT COUNT(*) AS c FROM template_suggestions").fetchone()[
            "c"
        ]
        check(
            "History routing",
            len(fake.sent) == 1
            and fake.sent[0]["chat_id"] == CHAT_A
            and fake.sent[0]["enforce_send_enabled"] is False
            and len(fake_b.sent) == 1
            and fake_b.sent[0]["chat_id"] == CHAT_B
            and before == after_list,
        )

        fake_btn = RecordingSender(settings=settings)
        dispatch_callback(
            db,
            settings,
            fake_btn,
            _cb(make_history_callback_data(mid_b), user_id=USER_B, chat_id=CHAT_B, cbq_id="h1"),
        )
        after_btn = db._conn.execute("SELECT COUNT(*) AS c FROM template_suggestions").fetchone()[
            "c"
        ]
        check(
            "History button routing",
            bool(fake_btn.answered)
            and all(s["chat_id"] == CHAT_B for s in fake_btn.sent)
            and after_list == after_btn,
        )

        fake_a = RecordingSender(settings=settings)
        dispatch_callback(
            db,
            settings,
            fake_a,
            _cb(make_callback_data(mid_a, "HW-05"), user_id=USER_A, chat_id=CHAT_A, cbq_id="t1"),
        )
        preview_a = db._conn.execute(
            "SELECT preview_id FROM template_previews WHERE selected_by = ? "
            "ORDER BY preview_id DESC LIMIT 1",
            (USER_A,),
        ).fetchone()
        fake_cross = RecordingSender(settings=settings)
        if preview_a is not None:
            dispatch_callback(
                db,
                settings,
                fake_cross,
                _cb(
                    make_preview_callback_data(preview_a["preview_id"], "confirm"),
                    user_id=USER_B,
                    chat_id=CHAT_B,
                    cbq_id="x1",
                ),
            )
        check(
            "Cross-user Preview rejection",
            preview_a is not None
            and all(s["chat_id"] == CHAT_B for s in fake_cross.sent)
            and any("사용할 수 없는 요청" in (s["text"] or "") for s in fake_cross.sent)
            and db.get_decision_by_message_id(mid_a) is None,
        )

        fake_cat = RecordingSender(settings=settings)
        dispatch_callback(
            db,
            settings,
            fake_cat,
            _cb(
                make_category_callback_data(mid_a, "HW"),
                user_id=USER_A,
                chat_id=CHAT_A,
                cbq_id="c1",
            ),
        )
        check(
            "User A private category path",
            all(s["chat_id"] == CHAT_A for s in fake_cat.sent),
        )

        answered: list[str] = []

        def ack_handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("answerCallbackQuery"):
                answered.append("ok")
            return httpx.Response(200, json={"ok": True, "result": True})

        real = TelegramSender(
            settings_disabled, client=httpx.Client(transport=httpx.MockTransport(ack_handler))
        )
        real.answer_callback_query("cb-disabled")
        check("Callback acknowledgement", bool(answered))

        db.set_telegram_update_offset(99)
        db2 = Database(db_path)
        check("Offset persistence", db2.get_telegram_update_offset() == 99)
        db2.close()
        db.close()

    print("Telegram behavior validation")
    failed = 0
    for name, ok in results:
        print(f"- {name}: {'PASS' if ok else 'FAIL'}")
        if not ok:
            failed += 1
    print()
    print(f"Result: {'PASS' if failed == 0 else 'FAIL'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

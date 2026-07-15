"""Offline validation of the independent-operator Telegram model (no network).

Every authorized operator is an equal, independent entity (v0.4.0). This
script exercises the full model against synthetic identities — operator A
(user 201 / chat 200), operator B (user 202 / chat 300) and a group chat
(-400) — with no network, OpenAI, SafeCity, or GitHub access. It prints an
`Independent operator validation` PASS/FAIL block and exits non-zero on any
failure so CI/local checks can gate on it.

Usage:
    python -m app.commands.validate_telegram_behavior
"""

from __future__ import annotations

import tempfile
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app import template_flow
from app.ai_client import AIGenerationResult
from app.commands import poll_once
from app.config import Settings
from app.database import Database
from app.models import DisasterMessageRecord, TelegramStatus
from app.telegram_bot import (
    PRIVATE_CHAT_ONLY_REPLY,
    TelegramBotRunner,
    build_status_reply,
)
from app.telegram_sender import (
    TelegramSendOutcome,
    make_callback_data,
    make_preview_callback_data,
)
from app.template_flow import dispatch_callback

SEOUL_TZ = ZoneInfo("Asia/Seoul")

# Legacy TELEGRAM_CHAT_ID — retained only for migration/bootstrap, never the
# operational automatic-delivery target.
LEGACY_CHAT = "100"
USER_A, CHAT_A = 201, 200
USER_B, CHAT_B = 202, 300
GROUP_CHAT = -400  # negative id == group/supergroup, never a private operator


def _settings(**overrides) -> Settings:
    defaults = dict(
        telegram_bot_token="TEST_TOKEN",
        telegram_chat_id=LEGACY_CHAT,
        telegram_allowed_user_ids=(USER_A, USER_B),
        telegram_send_enabled=True,
        database_path=Path("unused.db"),
        log_level="ERROR",
        poll_interval_seconds=300,
        status_stale_after_minutes=15,
        message_retention_days=90,
        run_history_retention_days=14,
        store_successful_noop_runs=False,
        cleanup_interval_hours=24,
        tombstone_retention_days=365,
        local_shutdown_command_enabled=False,
        telegram_slow_interaction_ms=2000,
        telegram_ai_workers=2,
    )
    defaults.update(overrides)
    return Settings(**defaults)


class RecordingSender:
    """Offline TelegramSender stand-in. Records every send and callback ack,
    honours the global TELEGRAM_SEND_ENABLED switch for automatic sends, and
    can inject a per-chat failure to simulate one recipient failing while the
    others succeed."""

    def __init__(self, settings: Settings | None = None, *, fail_chat_ids=None):
        self.settings = settings
        self.sent: list[dict] = []
        self.answered: list[str] = []
        self.fail_chat_ids = {str(c) for c in (fail_chat_ids or ())}

    # context-manager + lifecycle parity with the real sender
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def close(self) -> None:
        pass

    def answer_callback_query(self, callback_query_id, *, text="", show_alert=False):
        self.answered.append(callback_query_id)

    def send_text(self, text, *, chat_id=None, reply_to_message_id=None, reply_markup=None):
        return self.send_plain_text(
            text,
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
            enforce_send_enabled=False,
        )

    def send_plain_text(
        self,
        text,
        *,
        chat_id=None,
        reply_to_message_id=None,
        reply_markup=None,
        enforce_send_enabled=True,
    ):
        if (
            enforce_send_enabled
            and self.settings is not None
            and not self.settings.telegram_send_enabled
        ):
            return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_PENDING)
        if str(chat_id) in self.fail_chat_ids:
            return TelegramSendOutcome(
                status=TelegramStatus.TELEGRAM_FAILED, error="injected failure"
            )
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "enforce_send_enabled": enforce_send_enabled,
            }
        )
        return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_SENT, message_ids=["1"])

    @property
    def sent_chat_ids(self) -> list:
        return [s["chat_id"] for s in self.sent]

    @property
    def sent_texts(self) -> list[str]:
        return [s["text"] for s in self.sent]


def _record(source_id: str, body: str = "호우주의보 해제 [테스트구]") -> DisasterMessageRecord:
    return DisasterMessageRecord(
        source_id=source_id,
        sender_or_region="서울특별시 테스트구",
        sent_at=datetime(2026, 7, 14, 12, 0, tzinfo=SEOUL_TZ),
        original_body=body,
        source_url="https://example.invalid/",
        detected_at=datetime(2026, 7, 14, 12, 1, tzinfo=SEOUL_TZ),
        raw_payload={"disstrSmsSn": source_id},
    )


def _message(update_id, user_id, text, *, chat_id, chat_type="private") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id},
            "text": text,
        },
    }


def _cb(data, *, user_id, chat_id, cbq_id, chat_type="private") -> dict:
    return {
        "id": cbq_id,
        "from": {"id": user_id},
        "message": {"chat": {"id": chat_id, "type": chat_type}},
        "data": data,
    }


def _latest_preview_for(db: Database, user_id: int):
    return db._conn.execute(
        "SELECT * FROM template_previews WHERE selected_by = ? ORDER BY preview_id DESC LIMIT 1",
        (user_id,),
    ).fetchone()


# --------------------------------------------------------------------------
# Check groups
# --------------------------------------------------------------------------


def _check_subscriptions_and_commands(db_path: Path, check) -> None:
    """Dual auto-register, personal mute/unmute/unsubscribe/subscribe,
    pause/resume aliases (no shared-polling impact), group rejection,
    private /status content, removed-allowed-user exclusion, no
    silent reactivation."""
    db = Database(db_path)
    settings = _settings(database_path=db_path)
    bot = TelegramBotRunner(settings, db, sender=RecordingSender(settings))
    try:
        # Dual auto-register: two operators each get an independent active sub.
        bot.dispatch(_message(1, USER_A, "/status", chat_id=CHAT_A))
        bot.dispatch(_message(2, USER_B, "hello", chat_id=CHAT_B))
        check(
            "Dual auto-register (A+B independent active subscriptions)",
            db.subscription_status_for(USER_A) == "active"
            and db.subscription_status_for(USER_B) == "active"
            and len(db.list_active_subscriptions((USER_A, USER_B))) == 2,
        )

        # Personal mute only affects the muting operator.
        bot.dispatch(_message(3, USER_A, "/mute", chat_id=CHAT_A))
        active_after_mute = {s.user_id for s in db.list_active_subscriptions((USER_A, USER_B))}
        check(
            "Personal /mute isolates one operator",
            db.subscription_status_for(USER_A) == "muted"
            and db.subscription_status_for(USER_B) == "active"
            and active_after_mute == {USER_B},
        )

        # /pause is a personal alias of /mute and must NOT touch shared polling.
        polling_before = db.is_polling_enabled()
        bot.dispatch(_message(4, USER_B, "/pause", chat_id=CHAT_B))
        check(
            "/pause == personal mute; shared polling untouched",
            db.subscription_status_for(USER_B) == "muted"
            and db.is_polling_enabled() == polling_before is True,
        )

        # /unmute + /resume restore active independently.
        bot.dispatch(_message(5, USER_A, "/unmute", chat_id=CHAT_A))
        bot.dispatch(_message(6, USER_B, "/resume", chat_id=CHAT_B))
        check(
            "/unmute and /resume restore active",
            db.subscription_status_for(USER_A) == "active"
            and db.subscription_status_for(USER_B) == "active",
        )

        # Unsubscribe then subscribe.
        bot.dispatch(_message(7, USER_B, "/unsubscribe", chat_id=CHAT_B))
        unsub_ok = db.subscription_status_for(USER_B) == "unsubscribed" and [
            s.user_id for s in db.list_active_subscriptions((USER_A, USER_B))
        ] == [USER_A]
        bot.dispatch(_message(8, USER_B, "/subscribe", chat_id=CHAT_B))
        check(
            "/unsubscribe excludes then /subscribe re-activates",
            unsub_ok and db.subscription_status_for(USER_B) == "active",
        )

        # No silent reactivation: a muted operator stays muted on ordinary
        # interaction (only explicit /unmute|/subscribe may reactivate).
        db.set_subscription_status(user_id=USER_B, chat_id=CHAT_B, status="muted")
        bot.dispatch(_message(9, USER_B, "/status", chat_id=CHAT_B))
        check(
            "Ordinary interaction never reactivates a muted operator",
            db.subscription_status_for(USER_B) == "muted",
        )
        db.set_subscription_status(user_id=USER_B, chat_id=CHAT_B, status="active")

        # Group message rejection: acknowledged with the private-only reply,
        # never processed, and no subscription created for the group.
        group_sender = RecordingSender(settings)
        bot2 = TelegramBotRunner(settings, db, sender=group_sender)
        try:
            outcome = bot2.dispatch(
                _message(10, USER_A, "/latest", chat_id=GROUP_CHAT, chat_type="supergroup")
            )
        finally:
            bot2.close()
        check(
            "Group message rejected (private-chat-only, not processed)",
            outcome.handled is True
            and group_sender.sent_chat_ids == [GROUP_CHAT]
            and group_sender.sent_texts[-1] == PRIVATE_CHAT_ONLY_REPLY
            and db.get_subscription_by_chat(GROUP_CHAT) is None,
        )

        # Removed-allowed-user exclusion: a still-active row for a user no
        # longer in TELEGRAM_ALLOWED_USER_IDS is excluded from fan-out.
        active_for_a_only = db.list_active_subscriptions((USER_A,))
        check(
            "Removed allowed-user excluded from active subscriptions",
            [s.user_id for s in active_for_a_only] == [USER_A],
        )

        # Personal /status shows the caller's own state + shared collection,
        # never another operator's ids.
        status_text = build_status_reply(db, settings, user_id=USER_A, chat_id=CHAT_A)
        check(
            "Personal /status shows own + shared sections",
            "내 알림 상태" in status_text
            and "공통 수집 상태" in status_text
            and str(USER_B) not in status_text
            and str(CHAT_B) not in status_text,
        )
    finally:
        bot.close()
        db.close()


def _check_fan_out_and_retry(db_path: Path, check, monkey_sender) -> None:
    """Fan-out to two recipients, independent A-success/B-fail, retry of only
    the failed recipient, aggregate messages.telegram_status derivation, no
    backfill for a later subscriber, and no TELEGRAM_CHAT_ID fallback when
    there are no subscriptions."""
    db = Database(db_path)
    settings = _settings(database_path=db_path)

    # Two active operators.
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="active")
    db.set_subscription_status(user_id=USER_B, chat_id=CHAT_B, status="active")

    mid = db.insert(_record("FAN1"))
    record = db.get_by_internal_id(mid)

    # First fan-out: B fails, A succeeds — one recipient never blocks another.
    monkey_sender["sender"] = RecordingSender(settings, fail_chat_ids=[CHAT_B])
    poll_once._fan_out_and_retry(db, settings, [record], is_baseline_run=False)
    deliveries = {d["user_id_snapshot"]: d["status"] for d in db.get_deliveries_for_message(mid)}
    check(
        "Fan-out creates independent per-recipient deliveries",
        len(deliveries) == 2 and USER_A in deliveries and USER_B in deliveries,
    )
    check(
        "A succeeds while B fails (independent outcomes)",
        deliveries.get(USER_A) == "sent" and deliveries.get(USER_B) == "failed",
    )
    check(
        "Aggregate telegram_status reflects a failed recipient",
        db.get_by_internal_id(mid).telegram_status
        in (TelegramStatus.TELEGRAM_FAILED, TelegramStatus.TELEGRAM_PENDING),
    )

    # Retry pass targets ONLY B's failed delivery (driven by
    # list_retryable_deliveries, never messages.telegram_status).
    retryable = db.list_retryable_deliveries()
    monkey_sender["sender"] = RecordingSender(settings)  # now B recovers
    poll_once._fan_out_and_retry(db, settings, [], is_baseline_run=False)
    deliveries2 = {d["user_id_snapshot"]: d["status"] for d in db.get_deliveries_for_message(mid)}
    check(
        "Retry set contains only the failed recipient",
        [r["user_id_snapshot"] for r in retryable] == [USER_B],
    )
    check(
        "Failed recipient recovers on retry; both sent; aggregate sent",
        deliveries2.get(USER_A) == "sent"
        and deliveries2.get(USER_B) == "sent"
        and db.get_by_internal_id(mid).telegram_status == TelegramStatus.TELEGRAM_SENT,
    )

    # No backfill: a NEW record delivers to current subscribers only; an
    # operator who subscribes afterwards gets no delivery for older messages.
    db.set_subscription_status(user_id=USER_B, chat_id=CHAT_B, status="unsubscribed")
    mid2 = db.insert(_record("FAN2"))
    monkey_sender["sender"] = RecordingSender(settings)
    poll_once._fan_out_and_retry(db, settings, [db.get_by_internal_id(mid2)], is_baseline_run=False)
    db.set_subscription_status(user_id=USER_B, chat_id=CHAT_B, status="active")  # re-subscribe late
    later = {d["user_id_snapshot"] for d in db.get_deliveries_for_message(mid2)}
    check(
        "No backfill: later subscriber gets no delivery for earlier messages",
        later == {USER_A},
    )

    # No TELEGRAM_CHAT_ID fallback: with zero active subscriptions a new
    # record is stored but delivered to nobody.
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="unsubscribed")
    db.set_subscription_status(user_id=USER_B, chat_id=CHAT_B, status="unsubscribed")
    mid3 = db.insert(_record("FAN3"))
    monkey_sender["sender"] = RecordingSender(settings)
    poll_once._fan_out_and_retry(db, settings, [db.get_by_internal_id(mid3)], is_baseline_run=False)
    check(
        "No subscriptions => stored, no delivery, no legacy-chat fallback",
        db.get_deliveries_for_message(mid3) == []
        and monkey_sender["sender"].sent == []
        and db.is_known(_record("FAN3")),
    )

    db.close()


def _check_preview_and_decision_isolation(db_path: Path, check) -> None:
    """Preview and Final-OK decisions are independent per operator+chat:
    coexistence, in-place reconfirm, and cross-operator rejection."""
    db = Database(db_path)
    settings = _settings(database_path=db_path)

    # A body whose rule extraction yields a COMPLETE HW-05 preview, so Final OK
    # actually writes a decision (an incomplete preview cannot be confirmed).
    complete_body = "오늘 15시 부로 관내에 발효 중이던 호우주의보가 해제되었습니다. [예천군]"
    mid = db.insert(_record("ISO1", complete_body))

    # A and B each select a template on the SAME message from their own chat.
    sender_a = RecordingSender(settings)
    dispatch_callback(
        db,
        settings,
        sender_a,
        _cb(make_callback_data(mid, "HW-05"), user_id=USER_A, chat_id=CHAT_A, cbq_id="ia1"),
    )
    sender_b = RecordingSender(settings)
    dispatch_callback(
        db,
        settings,
        sender_b,
        _cb(make_callback_data(mid, "HW-05"), user_id=USER_B, chat_id=CHAT_B, cbq_id="ib1"),
    )
    preview_a = _latest_preview_for(db, USER_A)
    preview_b = _latest_preview_for(db, USER_B)
    check(
        "Independent previews per operator+chat",
        preview_a is not None
        and preview_b is not None
        and preview_a["preview_id"] != preview_b["preview_id"]
        and preview_a["interaction_chat_id"] == str(CHAT_A)
        and preview_b["interaction_chat_id"] == str(CHAT_B)
        and sender_a.sent_chat_ids == [CHAT_A]
        and sender_b.sent_chat_ids == [CHAT_B],
    )

    # Cross-operator confirm is rejected — B cannot act on A's preview.
    sender_cross = RecordingSender(settings)
    dispatch_callback(
        db,
        settings,
        sender_cross,
        _cb(
            make_preview_callback_data(preview_a["preview_id"], "confirm"),
            user_id=USER_B,
            chat_id=CHAT_B,
            cbq_id="ix1",
        ),
    )
    check(
        "Cross-operator confirm rejected",
        any("사용할 수 없는 요청" in (t or "") for t in sender_cross.sent_texts)
        and db.get_decision_for_operator(mid, USER_A, CHAT_A) is None,
    )

    # Each operator confirms their own preview → two coexisting decisions.
    dispatch_callback(
        db,
        settings,
        RecordingSender(settings),
        _cb(
            make_preview_callback_data(preview_a["preview_id"], "confirm"),
            user_id=USER_A,
            chat_id=CHAT_A,
            cbq_id="ic1",
        ),
    )
    dispatch_callback(
        db,
        settings,
        RecordingSender(settings),
        _cb(
            make_preview_callback_data(preview_b["preview_id"], "confirm"),
            user_id=USER_B,
            chat_id=CHAT_B,
            cbq_id="ic2",
        ),
    )
    decisions = db.list_decisions_for_message(mid)
    check(
        "Coexisting per-operator decisions (UNIQUE per operator+chat)",
        len(decisions) == 2
        and db.get_decision_for_operator(mid, USER_A, CHAT_A) is not None
        and db.get_decision_for_operator(mid, USER_B, CHAT_B) is not None,
    )

    # Reconfirm is idempotent for one operator — updates in place, never a
    # second decision row.
    dispatch_callback(
        db,
        settings,
        RecordingSender(settings),
        _cb(
            make_preview_callback_data(preview_a["preview_id"], "confirm"),
            user_id=USER_A,
            chat_id=CHAT_A,
            cbq_id="ic3",
        ),
    )
    check(
        "Reconfirm is idempotent (no duplicate decision)",
        len(db.list_decisions_for_message(mid)) == 2,
    )

    db.close()


def _check_ai_independence(db_path: Path, check) -> None:
    """AI result is delivered only to the requesting operator's chat, AI
    audit rows carry that chat, an AI failure is isolated (rule preview stays
    confirmable), and one operator's blocked AI never blocks another
    operator's non-AI callback."""
    db = Database(db_path)
    settings = _settings(database_path=db_path, ai_enabled=True, openai_api_key="test-key")

    original_generate = template_flow.generate_slots
    original_sender_cls = template_flow.TelegramSender
    try:
        # --- AI success routed only to A -----------------------------------
        mid = db.insert(_record("AI1"))
        dispatch_callback(
            db,
            settings,
            RecordingSender(settings),
            _cb(make_callback_data(mid, "HW-05"), user_id=USER_A, chat_id=CHAT_A, cbq_id="ai_sel"),
        )
        preview_a = _latest_preview_for(db, USER_A)

        template_flow.generate_slots = lambda **_kw: AIGenerationResult(
            status="succeeded", slots={}, raw_response_json="{}"
        )
        ai_sender = RecordingSender(settings)
        dispatch_callback(
            db,
            settings,
            ai_sender,
            _cb(
                make_preview_callback_data(preview_a["preview_id"], "ai"),
                user_id=USER_A,
                chat_id=CHAT_A,
                cbq_id="ai_go",
            ),
            ai_executor=None,  # inline for deterministic assertion
        )
        ai_gen = db._conn.execute(
            "SELECT interaction_chat_id FROM ai_generations ORDER BY ai_generation_id DESC LIMIT 1"
        ).fetchone()
        check(
            "AI result routed only to the requesting operator's chat",
            bool(ai_sender.sent)
            and all(s["chat_id"] == CHAT_A for s in ai_sender.sent)
            and CHAT_B not in ai_sender.sent_chat_ids,
        )
        check(
            "AI audit row carries the operator's interaction_chat_id",
            ai_gen is not None and ai_gen["interaction_chat_id"] == str(CHAT_A),
        )

        # --- AI failure isolation ------------------------------------------
        mid2 = db.insert(_record("AI2"))
        dispatch_callback(
            db,
            settings,
            RecordingSender(settings),
            _cb(make_callback_data(mid2, "HW-05"), user_id=USER_A, chat_id=CHAT_A, cbq_id="af_sel"),
        )
        preview_fail = _latest_preview_for(db, USER_A)
        template_flow.generate_slots = lambda **_kw: AIGenerationResult(
            status="api_failed", error="boom"
        )
        dispatch_callback(
            db,
            settings,
            RecordingSender(settings),
            _cb(
                make_preview_callback_data(preview_fail["preview_id"], "ai"),
                user_id=USER_A,
                chat_id=CHAT_A,
                cbq_id="af_go",
            ),
            ai_executor=None,
        )
        after = db.get_preview(preview_fail["preview_id"])
        check(
            "AI failure leaves the rule preview confirmable (isolated)",
            after is not None
            and after.status == "rule_preview"
            and db.get_decision_for_operator(mid2, USER_A, CHAT_A) is None,
        )

        # --- Non-blocking: B responds while A's AI task is blocked ---------
        started_evt = threading.Event()
        release_evt = threading.Event()

        def _blocking_generate(**_kw):
            started_evt.set()
            release_evt.wait(timeout=10)
            return AIGenerationResult(status="succeeded", slots={}, raw_response_json="{}")

        template_flow.generate_slots = _blocking_generate
        # The AI worker opens its own TelegramSender(settings) — keep it offline.
        template_flow.TelegramSender = lambda *a, **k: RecordingSender(settings)

        mid3 = db.insert(_record("AI3"))
        dispatch_callback(
            db,
            settings,
            RecordingSender(settings),
            _cb(make_callback_data(mid3, "HW-05"), user_id=USER_A, chat_id=CHAT_A, cbq_id="nb_sel"),
        )
        preview_block = _latest_preview_for(db, USER_A)

        from concurrent.futures import ThreadPoolExecutor

        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-test")
        try:
            dispatch_callback(
                db,
                settings,
                RecordingSender(settings),
                _cb(
                    make_preview_callback_data(preview_block["preview_id"], "ai"),
                    user_id=USER_A,
                    chat_id=CHAT_A,
                    cbq_id="nb_go",
                ),
                ai_executor=executor,
            )
            # Wait until the worker is genuinely inside the (blocked) AI call.
            worker_blocked = started_evt.wait(timeout=5)

            # While A's AI is blocked, B's non-AI callback must complete.
            mid_b = db.insert(_record("AI3B"))
            b_sender = RecordingSender(settings)
            dispatch_callback(
                db,
                settings,
                b_sender,
                _cb(
                    make_callback_data(mid_b, "HW-05"),
                    user_id=USER_B,
                    chat_id=CHAT_B,
                    cbq_id="nb_b",
                ),
                ai_executor=executor,
            )
            b_ok = _latest_preview_for(db, USER_B) is not None and b_sender.sent_chat_ids == [
                CHAT_B
            ]
            check(
                "B's non-AI callback completes while A's AI is blocked",
                worker_blocked and b_ok,
            )
        finally:
            release_evt.set()
            executor.shutdown(wait=True)
    finally:
        template_flow.generate_slots = original_generate
        template_flow.TelegramSender = original_sender_cls
        db.close()


def _check_no_fallback_and_ack(db_path: Path, check) -> None:
    """A malformed callback with no chat id is acknowledged but never routed
    to the legacy TELEGRAM_CHAT_ID, and every callback stops the spinner."""
    db = Database(db_path)
    settings = _settings(database_path=db_path)
    mid = db.insert(_record("NF1"))

    sender = RecordingSender(settings)
    malformed = {
        "id": "nf-cb",
        "from": {"id": USER_A},
        "message": {},  # no chat -> no interaction_chat_id
        "data": make_callback_data(mid, "HW-05"),
    }
    dispatch_callback(db, settings, sender, malformed)
    check(
        "Missing-chat callback acked but never routed to legacy chat",
        sender.answered == ["nf-cb"]
        and sender.sent == []
        and LEGACY_CHAT not in [str(c) for c in sender.sent_chat_ids],
    )
    db.close()


def _check_legacy_decision_migration(db_path: Path, check) -> None:
    """A pre-v0.4.0 template_decisions row (message-only UNIQUE, no
    interaction_chat_id) migrates to the per-operator schema, defaulting the
    recovered chat to the literal 'legacy' and staying readable."""
    # 1. Full current schema, then a message + a preview with NULL chat.
    db = Database(db_path)
    mid = db.insert(_record("LEG1"))
    preview_id = db.insert_preview(
        message_id=mid,
        selected_template_id="HW-05",
        selected_by=USER_A,
        extraction_method="rule",
        extracted_slots_json="{}",
        rendered_text="렌더",
        missing_slots_json="[]",
        status="rule_preview",
        interaction_chat_id=None,  # pre-v0.3.0 preview had no recorded chat
    )
    # 2. Replace template_decisions with the OLD format and insert one row.
    db._conn.executescript(
        """
        DROP TABLE template_decisions;
        CREATE TABLE template_decisions (
            decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL UNIQUE,
            preview_id INTEGER NOT NULL,
            final_template_id TEXT NOT NULL,
            final_slots_json TEXT NOT NULL,
            final_rendered_text TEXT NOT NULL,
            generation_method TEXT NOT NULL,
            confirmed_by INTEGER NOT NULL,
            confirmed_at TEXT NOT NULL,
            source_id_snapshot TEXT,
            sender_or_region_snapshot TEXT,
            sent_at_snapshot TEXT,
            original_body_snapshot TEXT
        );
        """
    )
    db._conn.execute(
        "INSERT INTO template_decisions (message_id, preview_id, final_template_id, "
        "final_slots_json, final_rendered_text, generation_method, confirmed_by, confirmed_at) "
        "VALUES (?, ?, 'HW-05', '{}', '렌더', 'rule', ?, ?)",
        (mid, preview_id, USER_A, datetime.now(tz=SEOUL_TZ).isoformat()),
    )
    db._conn.commit()
    db.close()

    # 3. Reopen -> migration rebuilds the table.
    db2 = Database(db_path)
    decision = db2.get_decision_for_operator(mid, USER_A, "legacy")
    all_for_msg = db2.list_decisions_for_message(mid)
    check(
        "Legacy decision migrates to 'legacy' chat and stays readable",
        decision is not None
        and decision["interaction_chat_id"] == "legacy"
        and len(all_for_msg) == 1
        and all_for_msg[0]["final_template_id"] == "HW-05",
    )
    db2.close()


def _check_seed_and_counts(db_path: Path, check) -> None:
    """Subscription bootstrap seeds active peers from historical previews for
    authorized users on private chats, and subscription_counts aggregates
    with no raw ids."""
    db = Database(db_path)
    mid = db.insert(_record("SEED1"))
    # Historical preview owned by A on a private (positive) chat.
    db.insert_preview(
        message_id=mid,
        selected_template_id="HW-05",
        selected_by=USER_A,
        extraction_method="rule",
        extracted_slots_json="{}",
        rendered_text="렌더",
        missing_slots_json="[]",
        status="rule_preview",
        interaction_chat_id=str(CHAT_A),
    )
    # Also a historical preview from a GROUP chat, which must never be seeded.
    db.insert_preview(
        message_id=mid,
        selected_template_id="HW-05",
        selected_by=USER_B,
        extraction_method="rule",
        extracted_slots_json="{}",
        rendered_text="렌더",
        missing_slots_json="[]",
        status="rule_preview",
        interaction_chat_id=str(GROUP_CHAT),
    )
    seeded = db.seed_subscriptions_if_empty(allowed_user_ids=(USER_A, USER_B), legacy_chat_id=None)
    counts = db.subscription_counts()
    check(
        "Seed bootstraps only private authorized preview owners",
        seeded == 1
        and db.subscription_status_for(USER_A) == "active"
        and db.get_subscription_by_chat(GROUP_CHAT) is None
        and counts.get("active", 0) == 1,
    )
    # Idempotent: a second call on a non-empty table seeds nothing.
    check(
        "Seed is idempotent on a populated table",
        db.seed_subscriptions_if_empty(allowed_user_ids=(USER_A, USER_B), legacy_chat_id=None) == 0,
    )
    db.close()


def main() -> int:
    results: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        results.append((name, bool(ok)))

    with tempfile.TemporaryDirectory() as tmp:
        # poll_once._fan_out_and_retry constructs TelegramSender(settings)
        # internally; redirect it to a controllable offline recorder so the
        # fan-out/retry production path runs with no network.
        monkey_sender: dict[str, RecordingSender] = {}
        original_poll_sender = poll_once.TelegramSender
        poll_once.TelegramSender = lambda settings: monkey_sender["sender"]
        try:
            _check_subscriptions_and_commands(Path(tmp) / "cmds.db", check)
            _check_fan_out_and_retry(Path(tmp) / "fanout.db", check, monkey_sender)
        finally:
            poll_once.TelegramSender = original_poll_sender

        _check_preview_and_decision_isolation(Path(tmp) / "iso.db", check)
        _check_ai_independence(Path(tmp) / "ai.db", check)
        _check_no_fallback_and_ack(Path(tmp) / "nofb.db", check)
        _check_legacy_decision_migration(Path(tmp) / "legacy.db", check)
        _check_seed_and_counts(Path(tmp) / "seed.db", check)

    print("Independent operator validation")
    failed = 0
    for name, ok in results:
        print(f"- {name}: {'PASS' if ok else 'FAIL'}")
        if not ok:
            failed += 1
    print()
    print(f"Checks: {len(results)}  Passed: {len(results) - failed}  Failed: {failed}")
    print(f"Result: {'PASS' if failed == 0 else 'FAIL'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

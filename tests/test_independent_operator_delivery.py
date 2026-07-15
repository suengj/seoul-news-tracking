"""Independent per-recipient automatic delivery (v0.4.0).

Automatic alerts fan out to every active personal subscription, one
`telegram_deliveries` row per recipient. One recipient's failure never blocks
another; retries target only that recipient's failed/pending row; there is no
single primary recipient and no TELEGRAM_CHAT_ID fallback. These tests drive
the real `poll_once._fan_out_and_retry` path with an offline sender.
"""

from __future__ import annotations

import pytest

from app.commands import poll_once
from app.database import Database
from app.models import TelegramStatus
from app.telegram_sender import TelegramSendOutcome

USER_A, CHAT_A = 201, 200
USER_B, CHAT_B = 202, 300


class FakeSender:
    """Offline sender for the fan-out path. Fails only for injected chats."""

    def __init__(self, settings=None, *, fail_chat_ids=None):
        self.settings = settings
        self.sent_chat_ids: list = []
        self.fail_chat_ids = {str(c) for c in (fail_chat_ids or ())}

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def close(self):
        pass

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
            return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_FAILED, error="injected")
        self.sent_chat_ids.append(chat_id)
        return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_SENT, message_ids=["1"])


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "delivery.db")
    yield database
    database.close()


@pytest.fixture
def settings(make_settings, tmp_path):
    return make_settings(
        telegram_allowed_user_ids=(USER_A, USER_B),
        telegram_send_enabled=True,
        database_path=tmp_path / "delivery.db",
    )


@pytest.fixture
def patch_sender(monkeypatch):
    holder: dict = {"sender": FakeSender()}
    monkeypatch.setattr(poll_once, "TelegramSender", lambda settings: holder["sender"])
    return holder


def _activate_both(db):
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="active")
    db.set_subscription_status(user_id=USER_B, chat_id=CHAT_B, status="active")


# --- delivery DB primitives --------------------------------------------------


def test_create_delivery_if_missing_dedups_per_subscription(db, make_record):
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="active")
    sub = db.get_subscription_by_user(USER_A)
    mid = db.insert(make_record(source_id="D1"))
    first = db.create_delivery_if_missing(message_id=mid, subscription=sub)
    second = db.create_delivery_if_missing(message_id=mid, subscription=sub)
    assert first is not None
    assert second is None  # UNIQUE(message_id, subscription_id)
    assert len(db.get_deliveries_for_message(mid)) == 1


def test_mark_delivery_sent_and_failed(db, make_record):
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="active")
    sub = db.get_subscription_by_user(USER_A)
    mid = db.insert(make_record(source_id="D2"))
    did = db.create_delivery_if_missing(message_id=mid, subscription=sub)

    db.mark_delivery_failed(did, error="net")
    row = db.get_deliveries_for_message(mid)[0]
    assert row["status"] == "failed" and row["attempts"] == 1 and row["error"] == "net"

    db.mark_delivery_sent(did, telegram_message_id="99")
    row = db.get_deliveries_for_message(mid)[0]
    assert row["status"] == "sent" and row["telegram_message_id"] == "99" and row["error"] is None


def test_list_retryable_excludes_baseline_and_inactive(db, make_record):
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="active")
    sub = db.get_subscription_by_user(USER_A)

    baseline_mid = db.insert(make_record(source_id="B1"), is_baseline=True)
    normal_mid = db.insert(make_record(source_id="N1"))
    db.create_delivery_if_missing(message_id=baseline_mid, subscription=sub)
    normal_did = db.create_delivery_if_missing(message_id=normal_mid, subscription=sub)
    db.mark_delivery_failed(normal_did, error="x")

    retryable = db.list_retryable_deliveries()
    assert [r["message_id"] for r in retryable] == [normal_mid]

    # Muting the subscription removes it from the retry set.
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="muted")
    assert db.list_retryable_deliveries() == []


def test_update_message_aggregate_status_derivation(db, make_record):
    _activate_both(db)
    sub_a = db.get_subscription_by_user(USER_A)
    sub_b = db.get_subscription_by_user(USER_B)
    mid = db.insert(make_record(source_id="AGG"))
    da = db.create_delivery_if_missing(message_id=mid, subscription=sub_a)
    dbid = db.create_delivery_if_missing(message_id=mid, subscription=sub_b)

    db.mark_delivery_sent(da, telegram_message_id="1")
    db.mark_delivery_failed(dbid, error="x")
    db.update_message_aggregate_status(mid)
    assert db.get_by_internal_id(mid).telegram_status in (
        TelegramStatus.TELEGRAM_FAILED,
        TelegramStatus.TELEGRAM_PENDING,
    )

    db.mark_delivery_sent(dbid, telegram_message_id="2")
    db.update_message_aggregate_status(mid)
    assert db.get_by_internal_id(mid).telegram_status == TelegramStatus.TELEGRAM_SENT


def test_get_latest_delivery_for_user(db, make_record):
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="active")
    sub = db.get_subscription_by_user(USER_A)
    mid1 = db.insert(make_record(source_id="L1"))
    mid2 = db.insert(make_record(source_id="L2"))
    d1 = db.create_delivery_if_missing(message_id=mid1, subscription=sub)
    d2 = db.create_delivery_if_missing(message_id=mid2, subscription=sub)
    db.mark_delivery_sent(d1, telegram_message_id="1")
    db.mark_delivery_sent(d2, telegram_message_id="2")
    latest = db.get_latest_delivery_for_user(USER_A)
    assert latest["message_id"] == mid2


# --- fan-out + retry through poll_once ---------------------------------------


def test_fan_out_creates_one_delivery_per_active_subscription(
    db, settings, patch_sender, make_record
):
    _activate_both(db)
    patch_sender["sender"] = FakeSender(settings)
    mid = db.insert(make_record(source_id="F1"))
    sent, failed = poll_once._fan_out_and_retry(
        db, settings, [db.get_by_internal_id(mid)], is_baseline_run=False
    )
    statuses = {d["user_id_snapshot"]: d["status"] for d in db.get_deliveries_for_message(mid)}
    assert sent == 2 and failed == 0
    assert statuses == {USER_A: "sent", USER_B: "sent"}


def test_one_recipient_failure_never_blocks_another(db, settings, patch_sender, make_record):
    _activate_both(db)
    patch_sender["sender"] = FakeSender(settings, fail_chat_ids=[CHAT_B])
    mid = db.insert(make_record(source_id="F2"))
    poll_once._fan_out_and_retry(db, settings, [db.get_by_internal_id(mid)], is_baseline_run=False)
    statuses = {d["user_id_snapshot"]: d["status"] for d in db.get_deliveries_for_message(mid)}
    assert statuses[USER_A] == "sent"
    assert statuses[USER_B] == "failed"


def test_retry_targets_only_failed_recipient(db, settings, patch_sender, make_record):
    _activate_both(db)
    patch_sender["sender"] = FakeSender(settings, fail_chat_ids=[CHAT_B])
    mid = db.insert(make_record(source_id="F3"))
    poll_once._fan_out_and_retry(db, settings, [db.get_by_internal_id(mid)], is_baseline_run=False)

    # Retry pass with a healthy sender: only B's failed delivery is retried.
    retryable = db.list_retryable_deliveries()
    assert [r["user_id_snapshot"] for r in retryable] == [USER_B]

    patch_sender["sender"] = FakeSender(settings)
    poll_once._fan_out_and_retry(db, settings, [], is_baseline_run=False)
    statuses = {d["user_id_snapshot"]: d["status"] for d in db.get_deliveries_for_message(mid)}
    assert statuses == {USER_A: "sent", USER_B: "sent"}
    assert db.get_by_internal_id(mid).telegram_status == TelegramStatus.TELEGRAM_SENT


def test_no_subscriptions_stores_without_delivery_and_no_fallback(
    db, settings, patch_sender, make_record
):
    patch_sender["sender"] = FakeSender(settings)
    mid = db.insert(make_record(source_id="F4"))
    poll_once._fan_out_and_retry(db, settings, [db.get_by_internal_id(mid)], is_baseline_run=False)
    assert db.get_deliveries_for_message(mid) == []
    assert patch_sender["sender"].sent_chat_ids == []  # no legacy-chat fallback
    assert db.get_by_internal_id(mid).telegram_status == TelegramStatus.TELEGRAM_PENDING


def test_no_backfill_for_a_later_subscriber(db, settings, patch_sender, make_record):
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="active")
    patch_sender["sender"] = FakeSender(settings)
    mid = db.insert(make_record(source_id="F5"))
    poll_once._fan_out_and_retry(db, settings, [db.get_by_internal_id(mid)], is_baseline_run=False)

    # B subscribes only afterwards — must not receive the earlier message.
    db.set_subscription_status(user_id=USER_B, chat_id=CHAT_B, status="active")
    poll_once._fan_out_and_retry(db, settings, [], is_baseline_run=False)
    recipients = {d["user_id_snapshot"] for d in db.get_deliveries_for_message(mid)}
    assert recipients == {USER_A}


def test_global_send_switch_off_leaves_deliveries_pending(db, settings, patch_sender, make_record):
    _activate_both(db)
    off = settings.__class__(**{**settings.__dict__, "telegram_send_enabled": False})
    patch_sender["sender"] = FakeSender(off)
    mid = db.insert(make_record(source_id="F6"))
    poll_once._fan_out_and_retry(db, off, [db.get_by_internal_id(mid)], is_baseline_run=False)
    statuses = {d["status"] for d in db.get_deliveries_for_message(mid)}
    assert statuses == {"pending"}  # master switch off: retry later, not failed

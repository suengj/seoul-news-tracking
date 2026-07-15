"""Personal Telegram subscription storage (v0.4.0 independent operators).

Every authorized operator is an equal, independent subscription. These tests
cover registration/touch semantics, explicit status transitions, active-set
filtering, aggregate counts, and the one-time bootstrap seeding — all offline.
"""

from __future__ import annotations

import pytest

from app.database import Database

USER_A, CHAT_A = 201, 200
USER_B, CHAT_B = 202, 300
GROUP_CHAT = -400


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "subs.db")
    yield database
    database.close()


def test_register_creates_active_on_first_private_contact(db):
    sub = db.register_or_touch_subscription(user_id=USER_A, chat_id=CHAT_A, chat_type="private")
    assert sub.status == "active"
    assert sub.chat_id == str(CHAT_A)
    assert sub.chat_type == "private"
    assert db.subscription_status_for(USER_A) == "active"


def test_register_never_reactivates_muted_or_unsubscribed(db):
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="muted")
    db.register_or_touch_subscription(user_id=USER_A, chat_id=CHAT_A, chat_type="private")
    assert db.subscription_status_for(USER_A) == "muted"

    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="unsubscribed")
    db.register_or_touch_subscription(user_id=USER_A, chat_id=CHAT_A, chat_type="private")
    assert db.subscription_status_for(USER_A) == "unsubscribed"


def test_register_touches_last_seen_without_changing_status(db):
    first = db.register_or_touch_subscription(user_id=USER_A, chat_id=CHAT_A, chat_type="private")
    later = db.register_or_touch_subscription(user_id=USER_A, chat_id=CHAT_A, chat_type="private")
    assert later.status == "active"
    assert later.last_seen_at >= first.last_seen_at


def test_new_non_private_contact_is_not_activated(db):
    sub = db.register_or_touch_subscription(
        user_id=USER_A, chat_id=GROUP_CHAT, chat_type="supergroup"
    )
    assert sub.status == "unsubscribed"


def test_status_for_unknown_user_is_none(db):
    assert db.subscription_status_for(99999) == "none"


def test_set_status_transitions(db):
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="active")
    assert db.subscription_status_for(USER_A) == "active"
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="muted")
    assert db.subscription_status_for(USER_A) == "muted"
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="unsubscribed")
    assert db.subscription_status_for(USER_A) == "unsubscribed"
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="active")
    assert db.subscription_status_for(USER_A) == "active"


def test_list_active_excludes_muted_unsubscribed_and_groups(db):
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="active")
    db.set_subscription_status(user_id=USER_B, chat_id=CHAT_B, status="muted")
    # A group row can only exist as non-private; force one in directly.
    db.register_or_touch_subscription(user_id=303, chat_id=GROUP_CHAT, chat_type="supergroup")

    active = db.list_active_subscriptions((USER_A, USER_B, 303))
    assert [s.user_id for s in active] == [USER_A]


def test_list_active_filters_to_allowed_users(db):
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="active")
    db.set_subscription_status(user_id=USER_B, chat_id=CHAT_B, status="active")
    # B removed from the allowed set — a stale active row must be excluded.
    assert [s.user_id for s in db.list_active_subscriptions((USER_A,))] == [USER_A]
    assert {s.user_id for s in db.list_active_subscriptions((USER_A, USER_B))} == {USER_A, USER_B}


def test_get_subscription_by_user_and_chat(db):
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="active")
    assert db.get_subscription_by_user(USER_A).chat_id == str(CHAT_A)
    assert db.get_subscription_by_chat(CHAT_A).user_id == USER_A
    assert db.get_subscription_by_user(99999) is None
    assert db.get_subscription_by_chat(-1) is None


def test_subscription_counts_aggregate(db):
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="active")
    db.set_subscription_status(user_id=USER_B, chat_id=CHAT_B, status="muted")
    counts = db.subscription_counts()
    assert counts.get("active") == 1
    assert counts.get("muted") == 1


def _seed_preview(db, make_record, *, user_id, chat_id):
    mid = db.insert(make_record(source_id=f"S{user_id}-{chat_id}"))
    db.insert_preview(
        message_id=mid,
        selected_template_id="HW-05",
        selected_by=user_id,
        extraction_method="rule",
        extracted_slots_json="{}",
        rendered_text="렌더",
        missing_slots_json="[]",
        status="rule_preview",
        interaction_chat_id=str(chat_id),
    )


def test_seed_from_private_previews_only(db, make_record):
    _seed_preview(db, make_record, user_id=USER_A, chat_id=CHAT_A)
    _seed_preview(db, make_record, user_id=USER_B, chat_id=GROUP_CHAT)  # group -> never seeded

    created = db.seed_subscriptions_if_empty(allowed_user_ids=(USER_A, USER_B), legacy_chat_id=None)
    assert created == 1
    assert db.subscription_status_for(USER_A) == "active"
    assert db.get_subscription_by_chat(GROUP_CHAT) is None


def test_seed_legacy_chat_only_when_exactly_one_unseeded_allowed_user(db):
    # No previews at all: the single allowed user can safely inherit the
    # legacy private chat id.
    created = db.seed_subscriptions_if_empty(allowed_user_ids=(USER_A,), legacy_chat_id="200")
    assert created == 1
    assert db.get_subscription_by_chat(200).user_id == USER_A


def test_seed_does_not_guess_legacy_owner_with_multiple_users(db):
    created = db.seed_subscriptions_if_empty(
        allowed_user_ids=(USER_A, USER_B), legacy_chat_id="200"
    )
    assert created == 0  # two candidates -> never guess an owner


def test_seed_skips_negative_legacy_chat(db):
    created = db.seed_subscriptions_if_empty(allowed_user_ids=(USER_A,), legacy_chat_id="-400")
    assert created == 0


def test_seed_is_idempotent_on_populated_table(db):
    db.set_subscription_status(user_id=USER_A, chat_id=CHAT_A, status="active")
    assert db.seed_subscriptions_if_empty(allowed_user_ids=(USER_A,), legacy_chat_id="200") == 0

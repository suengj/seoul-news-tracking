from __future__ import annotations

import json

import pytest

from app.ai_client import AIGenerationResult
from app.database import Database
from app.models import TelegramStatus
from app.telegram_sender import (
    TelegramSendOutcome,
    make_callback_data,
    make_preview_callback_data,
)
from app.template_extractors import SlotValue
from app.template_flow import dispatch_callback

ALLOWED_USER_ID = 111
OTHER_USER_ID = 999


class FakeSender:
    def __init__(self, fail: bool = False):
        self.sent_texts: list[str] = []
        self.sent_keyboards: list[dict | None] = []
        self.sent_chat_ids: list[object] = []
        self.sent_reply_to_message_ids: list[object] = []
        self.sent_enforce_send_enabled: list[bool] = []
        self.answered: list[str] = []
        self.fail = fail

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
        if self.fail:
            return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_FAILED, error="boom")
        self.sent_texts.append(text)
        self.sent_keyboards.append(reply_markup)
        self.sent_chat_ids.append(chat_id)
        self.sent_reply_to_message_ids.append(reply_to_message_id)
        self.sent_enforce_send_enabled.append(enforce_send_enabled)
        return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_SENT, message_ids=["1"])


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "flow.db")
    yield database
    database.close()


@pytest.fixture
def stored_message_id(db, make_record):
    record = make_record(
        source_id="DS1",
        body="오늘 15시 부로 관내에 발효 중이던 호우주의보가 해제되었습니다. [예천군]",
        sender="예천군",
    )
    return db.insert(record)


DEFAULT_CHAT_ID = 100


def _tpl_callback(
    message_id, template_id, *, user_id=ALLOWED_USER_ID, cbq_id="cbq1", chat_id=DEFAULT_CHAT_ID
) -> dict:
    return {
        "id": cbq_id,
        "from": {"id": user_id},
        "message": {"chat": {"id": chat_id}},
        "data": make_callback_data(message_id, template_id),
    }


def _preview_callback(
    preview_id, action, *, user_id=ALLOWED_USER_ID, cbq_id="cbq-p1", chat_id=DEFAULT_CHAT_ID
) -> dict:
    return {
        "id": cbq_id,
        "from": {"id": user_id},
        "message": {"chat": {"id": chat_id}},
        "data": make_preview_callback_data(preview_id, action),
    }


def _labels(keyboard: dict) -> list[str]:
    return [b["text"] for row in keyboard["inline_keyboard"] for b in row]


# --- template selection -> preview -------------------------------------------


def test_template_selection_creates_action_and_complete_preview(
    db, stored_message_id, make_settings
):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED"))

    assert sender.answered == ["cbq1"]
    assert len(sender.sent_texts) == 1
    assert "[템플릿 초안]" in sender.sent_texts[0]
    assert "호우특보는" in sender.sent_texts[0]

    action = db._conn.execute("SELECT * FROM template_actions").fetchone()
    assert action["selected_template_id"] == "HEAVY_RAIN_CLEARED"
    assert action["selected_by"] == ALLOWED_USER_ID
    assert action["status"] == "preview_created"

    preview = db._conn.execute("SELECT * FROM template_previews").fetchone()
    assert preview["status"] == "rule_preview"
    assert preview["extraction_method"] == "rule"

    labels = _labels(sender.sent_keyboards[0])
    assert "✅ 최종 OK" in labels
    assert "↩️ 취소" in labels


def test_incomplete_selection_hides_final_ok(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    # The stored message has no river-name token — FLOOD_ADVISORY_ISSUED
    # (requires 하천명) must re-run extraction against the original body and
    # come back incomplete rather than inventing one.
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "FLOOD_ADVISORY_ISSUED")
    )

    assert len(sender.sent_texts) == 1
    text = sender.sent_texts[0]
    assert "[템플릿 작성 미완료]" in text
    assert "누락 필드:" in text
    assert "하천명" in text
    assert "원문:" in text

    labels = _labels(sender.sent_keyboards[0])
    assert "✅ 최종 OK" not in labels
    assert "↩️ 취소" in labels

    action = db._conn.execute("SELECT * FROM template_actions").fetchone()
    assert action["status"] == "preview_incomplete"


def test_original_only_selection_renders_body_verbatim(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(db, settings, sender, _tpl_callback(stored_message_id, "ORIGINAL_ONLY"))

    assert len(sender.sent_texts) == 1
    assert (
        "오늘 15시 부로 관내에 발효 중이던 호우주의보가 해제되었습니다. [예천군]"
        in sender.sent_texts[0]
    )
    labels = _labels(sender.sent_keyboards[0])
    assert "✅ 최종 OK" in labels


def test_disabled_ai_hides_ai_button(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,), ai_enabled=False)
    sender = FakeSender()
    dispatch_callback(db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED"))
    assert "🤖 AI로 작성" not in _labels(sender.sent_keyboards[0])


def test_enabled_ai_shows_ai_button(db, stored_message_id, make_settings):
    settings = make_settings(
        telegram_allowed_user_ids=(ALLOWED_USER_ID,), ai_enabled=True, openai_api_key="sk-test"
    )
    sender = FakeSender()
    dispatch_callback(db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED"))
    assert "🤖 AI로 작성" in _labels(sender.sent_keyboards[0])


# --- authorization / safety --------------------------------------------------


def test_unauthorized_callback_is_rejected_without_sending(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(
        db,
        settings,
        sender,
        _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED", user_id=OTHER_USER_ID),
    )
    # Still acknowledged (so Telegram's spinner stops), but nothing sent or stored.
    assert sender.answered == ["cbq1"]
    assert sender.sent_texts == []
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_actions").fetchone()["c"] == 0


def test_missing_message_id_is_rejected_safely(db, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(db, settings, sender, _tpl_callback(999999, "HEAVY_RAIN_CLEARED"))
    assert sender.sent_texts == []
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_actions").fetchone()["c"] == 0


def test_duplicate_template_selection_callback_processed_only_once(
    db, stored_message_id, make_settings
):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    callback = _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED", cbq_id="dup-1")
    dispatch_callback(db, settings, sender, callback)
    dispatch_callback(db, settings, sender, callback)

    assert len(sender.sent_texts) == 1
    count = db._conn.execute("SELECT COUNT(*) AS c FROM template_actions").fetchone()["c"]
    assert count == 1


def test_send_failure_marks_action_and_preview_failed(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender(fail=True)
    dispatch_callback(db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED"))

    action = db._conn.execute("SELECT * FROM template_actions").fetchone()
    assert action["status"] == "failed"
    assert action["error"] == "boom"
    preview = db._conn.execute("SELECT * FROM template_previews").fetchone()
    assert preview["status"] == "failed"
    # DB still perfectly queryable afterwards.
    assert db._conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"] == 1


# --- interaction_chat_id routing / preview ownership ------------------------


def test_missing_message_chat_id_is_rejected_safely(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    callback = _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED")
    del callback["message"]  # malformed: no way to know the routing chat
    dispatch_callback(db, settings, sender, callback)

    # Still acknowledged (spinner stops), but nothing sent and no action taken.
    assert sender.answered == ["cbq1"]
    assert sender.sent_texts == []
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_actions").fetchone()["c"] == 0


def test_template_selection_response_routes_to_interaction_chat_id(
    db, stored_message_id, make_settings
):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED", chat_id=300)
    )
    assert sender.sent_chat_ids == [300]
    assert sender.sent_enforce_send_enabled == [False]

    preview = db._conn.execute("SELECT * FROM template_previews").fetchone()
    assert preview["interaction_chat_id"] == "300"


def test_confirm_from_different_chat_is_rejected(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED", chat_id=200)
    )
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    # A confirm callback whose own message lives in a different chat (300)
    # must never be honored, even though the user_id is authorized.
    dispatch_callback(db, settings, sender, _preview_callback(preview_id, "confirm", chat_id=300))

    assert "[사용할 수 없는 요청]" in sender.sent_texts[-1]
    assert sender.sent_chat_ids[-1] == 300  # rejection goes to the caller, not the original chat
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_decisions").fetchone()["c"] == 0
    assert db.get_preview(preview_id).status == "rule_preview"  # untouched


def test_cancel_from_different_chat_is_rejected(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED", chat_id=200)
    )
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    dispatch_callback(db, settings, sender, _preview_callback(preview_id, "cancel", chat_id=300))

    assert "[사용할 수 없는 요청]" in sender.sent_texts[-1]
    assert db.get_preview(preview_id).status == "rule_preview"  # not cancelled


def test_ai_from_different_chat_is_rejected(db, stored_message_id, make_settings):
    settings = make_settings(
        telegram_allowed_user_ids=(ALLOWED_USER_ID,), ai_enabled=True, openai_api_key="sk-test"
    )
    sender = FakeSender()
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED", chat_id=200)
    )
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    dispatch_callback(db, settings, sender, _preview_callback(preview_id, "ai", chat_id=300))

    assert "[사용할 수 없는 요청]" in sender.sent_texts[-1]
    assert db._conn.execute("SELECT COUNT(*) AS c FROM ai_generations").fetchone()["c"] == 0


def test_confirm_from_different_authorized_user_is_rejected(db, stored_message_id, make_settings):
    other_authorized_user = 222
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID, other_authorized_user))
    sender = FakeSender()
    dispatch_callback(
        db,
        settings,
        sender,
        _tpl_callback(
            stored_message_id, "HEAVY_RAIN_CLEARED", chat_id=200, user_id=ALLOWED_USER_ID
        ),
    )
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    # Same chat_id is impossible for two different users' own private chats
    # in practice, but even if it happened, a different (also authorized)
    # user must not be able to confirm someone else's draft.
    dispatch_callback(
        db,
        settings,
        sender,
        _preview_callback(preview_id, "confirm", chat_id=200, user_id=other_authorized_user),
    )

    assert "[사용할 수 없는 요청]" in sender.sent_texts[-1]
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_decisions").fetchone()["c"] == 0


def test_selecting_a_different_template_creates_a_new_preview(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED", cbq_id="c1")
    )
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "FLOOD_ADVISORY_ISSUED", cbq_id="c2")
    )
    previews = db._conn.execute("SELECT * FROM template_previews ORDER BY preview_id").fetchall()
    assert len(previews) == 2
    assert previews[0]["selected_template_id"] == "HEAVY_RAIN_CLEARED"
    assert previews[1]["selected_template_id"] == "FLOOD_ADVISORY_ISSUED"


# --- preview lifecycle: latest-preview-only / supersede policy ----------------


def test_new_selection_supersedes_prior_active_preview(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED", cbq_id="c1")
    )
    first_preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "FLOOD_ADVISORY_ISSUED", cbq_id="c2")
    )

    first = db.get_preview(first_preview_id)
    assert first.status == "superseded"


def test_superseded_preview_cannot_be_confirmed(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED", cbq_id="c1")
    )
    first_preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "FLOOD_ADVISORY_ISSUED", cbq_id="c2")
    )

    dispatch_callback(db, settings, sender, _preview_callback(first_preview_id, "confirm"))

    assert "[사용할 수 없는 초안]" in sender.sent_texts[-1]
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_decisions").fetchone()["c"] == 0


def test_superseded_preview_cannot_be_cancelled(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED", cbq_id="c1")
    )
    first_preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "FLOOD_ADVISORY_ISSUED", cbq_id="c2")
    )

    dispatch_callback(db, settings, sender, _preview_callback(first_preview_id, "cancel"))

    assert "[사용할 수 없는 초안]" in sender.sent_texts[-1]
    preview = db.get_preview(first_preview_id)
    assert preview.status == "superseded"  # unchanged, not "cancelled"


def test_confirmed_preview_cannot_be_cancelled(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED"))
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]
    dispatch_callback(db, settings, sender, _preview_callback(preview_id, "confirm", cbq_id="ok-1"))

    dispatch_callback(
        db, settings, sender, _preview_callback(preview_id, "cancel", cbq_id="cancel-1")
    )

    assert "[사용할 수 없는 초안]" in sender.sent_texts[-1]
    assert db.get_preview(preview_id).status == "confirmed"
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_decisions").fetchone()["c"] == 1


def test_latest_preview_remains_usable_after_supersede(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED", cbq_id="c1")
    )
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "ORIGINAL_ONLY", cbq_id="c2")
    )
    latest_preview_id = db._conn.execute(
        "SELECT preview_id FROM template_previews ORDER BY preview_id DESC LIMIT 1"
    ).fetchone()["preview_id"]

    dispatch_callback(db, settings, sender, _preview_callback(latest_preview_id, "confirm"))

    decision = db._conn.execute("SELECT * FROM template_decisions").fetchone()
    assert decision["final_template_id"] == "ORIGINAL_ONLY"


# --- preview confirm ----------------------------------------------------------


def test_confirm_creates_decision_and_marks_preview_confirmed(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED"))
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    dispatch_callback(db, settings, sender, _preview_callback(preview_id, "confirm"))

    assert "[최종 확정 완료]" in sender.sent_texts[-1]
    decision = db._conn.execute("SELECT * FROM template_decisions").fetchone()
    assert decision["message_id"] == stored_message_id
    assert decision["final_template_id"] == "HEAVY_RAIN_CLEARED"
    assert decision["generation_method"] == "rule"
    assert decision["confirmed_by"] == ALLOWED_USER_ID

    preview = db._conn.execute(
        "SELECT status FROM template_previews WHERE preview_id = ?", (preview_id,)
    ).fetchone()
    assert preview["status"] == "confirmed"


def test_confirm_is_idempotent_on_repeat_click(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED"))
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    dispatch_callback(db, settings, sender, _preview_callback(preview_id, "confirm", cbq_id="ok-1"))
    dispatch_callback(db, settings, sender, _preview_callback(preview_id, "confirm", cbq_id="ok-2"))

    count = db._conn.execute("SELECT COUNT(*) AS c FROM template_decisions").fetchone()["c"]
    assert count == 1
    # Second click (different callback_query_id, so not filtered as an exact
    # duplicate) still just resends the same confirmation, no error.
    assert sender.sent_texts[-1] == sender.sent_texts[-2]


def test_confirm_rejected_when_preview_incomplete(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "FLOOD_ADVISORY_ISSUED")
    )
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    dispatch_callback(db, settings, sender, _preview_callback(preview_id, "confirm"))

    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_decisions").fetchone()["c"] == 0
    # No confirmation message appended beyond the original incomplete preview.
    assert len(sender.sent_texts) == 1


def test_confirm_db_failure_sends_failure_message_and_keeps_preview_retryable(
    db, stored_message_id, make_settings, monkeypatch
):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED"))
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    monkeypatch.setattr(
        db, "upsert_decision", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("disk full"))
    )
    dispatch_callback(
        db, settings, sender, _preview_callback(preview_id, "confirm", cbq_id="fail-1")
    )

    assert "[최종 확정 실패]" in sender.sent_texts[-1]
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_decisions").fetchone()["c"] == 0
    preview = db._conn.execute(
        "SELECT status FROM template_previews WHERE preview_id = ?", (preview_id,)
    ).fetchone()
    assert preview["status"] == "rule_preview"  # untouched — still confirmable

    # Retry (DB restored) succeeds without needing a fresh preview.
    monkeypatch.undo()
    dispatch_callback(
        db, settings, sender, _preview_callback(preview_id, "confirm", cbq_id="fail-2")
    )
    assert "[최종 확정 완료]" in sender.sent_texts[-1]
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_decisions").fetchone()["c"] == 1


def test_confirm_status_update_failure_still_reports_success_since_decision_is_safe(
    db, stored_message_id, make_settings, monkeypatch
):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED"))
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    monkeypatch.setattr(
        db, "update_preview_status", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked"))
    )
    dispatch_callback(db, settings, sender, _preview_callback(preview_id, "confirm"))

    # The authoritative decision exists — never claim a failed confirmation
    # just because the (non-authoritative) preview bookkeeping update failed.
    assert "[최종 확정 완료]" in sender.sent_texts[-1]
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_decisions").fetchone()["c"] == 1


def test_confirmed_decision_contains_source_snapshots(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED"))
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]
    dispatch_callback(db, settings, sender, _preview_callback(preview_id, "confirm"))

    decision = db._conn.execute("SELECT * FROM template_decisions").fetchone()
    assert decision["source_id_snapshot"] == "DS1"
    assert decision["sender_or_region_snapshot"] == "예천군"
    assert decision["sent_at_snapshot"]
    assert "호우주의보가 해제되었습니다" in decision["original_body_snapshot"]

    # Survives the source message being deleted by retention cleanup.
    db.cleanup_execute(
        message_retention_days=0, run_history_retention_days=14, tombstone_retention_days=365
    )
    assert db._conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"] == 0
    row = db.get_decision_by_message_id(stored_message_id)
    assert row is not None
    assert "호우주의보가 해제되었습니다" in row["original_body_snapshot"]


def test_duplicate_confirm_callback_processed_only_once(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED"))
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    callback = _preview_callback(preview_id, "confirm", cbq_id="confirm-dup")
    dispatch_callback(db, settings, sender, callback)
    dispatch_callback(db, settings, sender, callback)

    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_decisions").fetchone()["c"] == 1


# --- preview cancel ------------------------------------------------------------


def test_cancel_marks_cancelled_and_resends_selector(db, stored_message_id, make_settings):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,))
    sender = FakeSender()
    dispatch_callback(db, settings, sender, _tpl_callback(stored_message_id, "HEAVY_RAIN_CLEARED"))
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    dispatch_callback(db, settings, sender, _preview_callback(preview_id, "cancel"))

    preview = db._conn.execute(
        "SELECT status FROM template_previews WHERE preview_id = ?", (preview_id,)
    ).fetchone()
    assert preview["status"] == "cancelled"

    cancel_text = sender.sent_texts[-1]
    assert "[선택 취소]" in cancel_text
    assert "원문:" in cancel_text
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_decisions").fetchone()["c"] == 0

    labels = _labels(sender.sent_keyboards[-1])
    assert len(labels) == 8  # the full template selector, shown again


# --- AI path -------------------------------------------------------------------


def test_ai_button_hidden_reply_when_disabled_but_clicked_anyway(
    db, stored_message_id, make_settings
):
    settings = make_settings(telegram_allowed_user_ids=(ALLOWED_USER_ID,), ai_enabled=False)
    sender = FakeSender()
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "FLOOD_ADVISORY_ISSUED")
    )
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    dispatch_callback(db, settings, sender, _preview_callback(preview_id, "ai"))
    assert "비활성화" in sender.sent_texts[-1]
    assert db._conn.execute("SELECT COUNT(*) AS c FROM ai_generations").fetchone()["c"] == 0


def test_ai_success_creates_ai_preview_with_confirm_and_cancel_only(
    db, stored_message_id, make_settings, monkeypatch
):
    settings = make_settings(
        telegram_allowed_user_ids=(ALLOWED_USER_ID,), ai_enabled=True, openai_api_key="sk-test"
    )
    sender = FakeSender()
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "FLOOD_ADVISORY_ISSUED")
    )
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    def _fake_generate_slots(**kwargs):
        assert kwargs["template_id"] == "FLOOD_ADVISORY_ISSUED"
        return AIGenerationResult(
            status="succeeded",
            slots={
                "기준시각": SlotValue(
                    value="15시", source="ai", evidence="15시 부로", confidence=0.7
                ),
                "하천명": SlotValue(value="예천천", source="ai", evidence="예천천", confidence=0.7),
            },
        )

    monkeypatch.setattr("app.template_flow.generate_slots", _fake_generate_slots)

    dispatch_callback(db, settings, sender, _preview_callback(preview_id, "ai"))

    ai_gen = db._conn.execute("SELECT * FROM ai_generations").fetchone()
    assert ai_gen["status"] == "succeeded"

    ai_previews = db._conn.execute(
        "SELECT * FROM template_previews WHERE extraction_method = 'ai'"
    ).fetchall()
    assert len(ai_previews) == 1
    assert ai_previews[0]["status"] == "ai_preview"

    text = sender.sent_texts[-1]
    assert "AI" in text
    labels = _labels(sender.sent_keyboards[-1])
    assert labels == ["✅ 최종 OK", "↩️ 취소"]


def test_ai_failure_keeps_original_preview_and_allows_cancel(
    db, stored_message_id, make_settings, monkeypatch
):
    settings = make_settings(
        telegram_allowed_user_ids=(ALLOWED_USER_ID,), ai_enabled=True, openai_api_key="sk-test"
    )
    sender = FakeSender()
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "FLOOD_ADVISORY_ISSUED")
    )
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    def _fake_generate_slots(**kwargs):
        return AIGenerationResult(
            status="validation_failed", missing_slots=["하천명"], error="not found"
        )

    monkeypatch.setattr("app.template_flow.generate_slots", _fake_generate_slots)
    dispatch_callback(db, settings, sender, _preview_callback(preview_id, "ai"))

    ai_gen = db._conn.execute("SELECT * FROM ai_generations").fetchone()
    assert ai_gen["status"] == "validation_failed"
    # No new preview row for a failed AI attempt.
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_previews").fetchone()["c"] == 1
    # The Rule preview is left active, not superseded — a failed AI call
    # must not block confirming/cancelling the original Rule preview.
    assert db.get_preview(preview_id).status == "rule_preview"

    text = sender.sent_texts[-1]
    assert "[템플릿 작성 미완료]" in text
    labels = _labels(sender.sent_keyboards[-1])
    assert "✅ 최종 OK" not in labels
    assert "↩️ 취소" in labels

    # Still confirmable via a fresh AI attempt / cancel afterward — proven by
    # cancel succeeding normally (not rejected as stale).
    dispatch_callback(
        db, settings, sender, _preview_callback(preview_id, "cancel", cbq_id="cancel-after-ai-fail")
    )
    assert "[선택 취소]" in sender.sent_texts[-1]


def test_successful_ai_preview_supersedes_rule_preview(
    db, stored_message_id, make_settings, monkeypatch
):
    settings = make_settings(
        telegram_allowed_user_ids=(ALLOWED_USER_ID,), ai_enabled=True, openai_api_key="sk-test"
    )
    sender = FakeSender()
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "FLOOD_ADVISORY_ISSUED")
    )
    rule_preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    def _fake_generate_slots(**kwargs):
        return AIGenerationResult(
            status="succeeded",
            slots={
                "기준시각": SlotValue(
                    value="15시", source="ai", evidence="15시 부로", confidence=0.7
                ),
                "하천명": SlotValue(value="예천천", source="ai", evidence="예천천", confidence=0.7),
            },
        )

    monkeypatch.setattr("app.template_flow.generate_slots", _fake_generate_slots)
    dispatch_callback(db, settings, sender, _preview_callback(rule_preview_id, "ai"))

    assert db.get_preview(rule_preview_id).status == "superseded"

    # The old Rule preview's own buttons no longer work.
    dispatch_callback(
        db, settings, sender, _preview_callback(rule_preview_id, "confirm", cbq_id="stale-confirm")
    )
    assert "[사용할 수 없는 초안]" in sender.sent_texts[-1]


def test_ai_preview_confirm_records_ai_generation_method(
    db, stored_message_id, make_settings, monkeypatch
):
    settings = make_settings(
        telegram_allowed_user_ids=(ALLOWED_USER_ID,), ai_enabled=True, openai_api_key="sk-test"
    )
    sender = FakeSender()
    dispatch_callback(
        db, settings, sender, _tpl_callback(stored_message_id, "FLOOD_ADVISORY_ISSUED")
    )
    rule_preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    def _fake_generate_slots(**kwargs):
        return AIGenerationResult(
            status="succeeded",
            slots={
                "기준시각": SlotValue(value="15시", source="ai", evidence="15시", confidence=0.7),
                "하천명": SlotValue(value="예천천", source="ai", evidence="예천천", confidence=0.7),
            },
        )

    monkeypatch.setattr("app.template_flow.generate_slots", _fake_generate_slots)
    dispatch_callback(db, settings, sender, _preview_callback(rule_preview_id, "ai", cbq_id="ai-1"))

    ai_preview_id = db._conn.execute(
        "SELECT preview_id FROM template_previews WHERE extraction_method = 'ai'"
    ).fetchone()["preview_id"]

    dispatch_callback(
        db, settings, sender, _preview_callback(ai_preview_id, "confirm", cbq_id="ai-confirm-1")
    )

    decision = db._conn.execute("SELECT * FROM template_decisions").fetchone()
    assert decision["generation_method"] == "ai"
    assert json.loads(decision["final_slots_json"])["하천명"]["value"] == "예천천"

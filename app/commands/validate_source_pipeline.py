"""Offline validation of the v0.5.0 MOIS/SafeKorea source pipeline (no network).

Exercises the full collection path — MOIS API parsing, Seoul recipient
filtering, valid-empty handling, the SafeKorea HTML fallback, cross-source
deduplication, source cutover safety, and independent two-user Telegram
fan-out — entirely against `httpx.MockTransport` and sanitized fixtures
under `tests/fixtures/`. No real request is ever made to SafetyData,
국민안전24, Telegram, OpenAI, or the retired Seoul SafeCity domain.

Usage:
    python -m app.commands.validate_source_pipeline
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.collector import METHOD_MOIS_API, METHOD_SAFEKOREA_FALLBACK, fetch_records
from app.commands import poll_once
from app.commands.poll_once import run_poll_cycle
from app.config import Settings
from app.database import Database
from app.models import is_seoul_recipient
from app.telegram_sender import TelegramSendOutcome

SEOUL_TZ = ZoneInfo("Asia/Seoul")
FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"

FORBIDDEN_DOMAIN = "safecity.seoul.go.kr"


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _settings(**overrides) -> Settings:
    defaults = dict(
        telegram_bot_token="TEST_TOKEN",
        telegram_chat_id="",
        telegram_allowed_user_ids=(201, 202),
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
        safetydata_service_key="TEST_KEY",
        safetydata_num_of_rows=20,
        safetydata_lookback_days=1,
        safekorea_fallback_enabled=True,
        safekorea_request_timeout_seconds=15.0,
        safekorea_max_pages=3,
        safekorea_request_delay_seconds=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _guard_no_legacy_domain(request: httpx.Request) -> None:
    if FORBIDDEN_DOMAIN in str(request.url):
        raise AssertionError(f"forbidden legacy request: {request.url}")


class RecordingSender:
    """Offline TelegramSender stand-in — same shape as
    validate_telegram_behavior.RecordingSender, kept local and minimal since
    this validator only needs single-message send outcomes."""

    def __init__(self, settings: Settings | None = None, *, fail_chat_ids=None):
        self.settings = settings
        self.fail_chat_ids = set(fail_chat_ids or ())
        self.sent: list[tuple[str, str]] = []  # (chat_id, source_id)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def send_plain_text(self, text, *, chat_id=None, **kwargs):
        from app.models import TelegramStatus

        if str(chat_id) in self.fail_chat_ids:
            return TelegramSendOutcome(
                status=TelegramStatus.TELEGRAM_FAILED, error="injected failure"
            )
        self.sent.append((str(chat_id), text))
        return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_SENT, message_ids=["1"])


def _check_mois_api_parsing(check) -> None:
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        _guard_no_legacy_domain(request)
        return httpx.Response(200, json=_load_json("mois_success.json"))

    result = fetch_records(settings, mois_transport=httpx.MockTransport(handler))
    check(
        "MOIS API parsing",
        len(result.records) == 3 and all(r.source_id.startswith("MOIS:") for r in result.records),
    )


def _check_seoul_recipient_filtering(check) -> None:
    ok = (
        is_seoul_recipient("서울특별시") is True
        and is_seoul_recipient("서울특별시 구로구") is True
        and is_seoul_recipient("경기도 광명시,경기도 시흥시,서울특별시 구로구") is True
        and is_seoul_recipient("경기도 광명시,경기도 시흥시") is False
        and is_seoul_recipient(None) is False
        and is_seoul_recipient("서울 근처") is False
    )
    check("Seoul recipient filtering", ok)


def _check_valid_empty_handling(check) -> None:
    settings = _settings()
    fallback_calls = {"n": 0}

    def mois_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load_json("mois_empty.json"))

    def safekorea_handler(request: httpx.Request) -> httpx.Response:
        fallback_calls["n"] += 1
        return httpx.Response(200, text=_text("safekorea_list.html"))

    result = fetch_records(
        settings,
        mois_transport=httpx.MockTransport(mois_handler),
        safekorea_transport=httpx.MockTransport(safekorea_handler),
    )
    check(
        "Valid empty handling",
        result.records == []
        and result.method == METHOD_MOIS_API
        and result.fallback_used is False
        and fallback_calls["n"] == 0,
    )


def _check_safekorea_fallback(check) -> None:
    settings = _settings()

    def mois_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load_json("mois_error.json"))

    def safekorea_handler(request: httpx.Request) -> httpx.Response:
        _guard_no_legacy_domain(request)
        return httpx.Response(200, text=_text("safekorea_list.html"))

    result = fetch_records(
        settings,
        mois_transport=httpx.MockTransport(mois_handler),
        safekorea_transport=httpx.MockTransport(safekorea_handler),
    )
    check(
        "SafeKorea fallback",
        result.method == METHOD_SAFEKOREA_FALLBACK
        and result.fallback_used is True
        and len(result.records) == 3,
    )


def _check_cross_source_dedup(db_path: Path, check) -> None:
    db = Database(db_path)
    from datetime import datetime

    from app.models import DisasterMessageRecord

    now = datetime.now(tz=SEOUL_TZ)
    mois_record = DisasterMessageRecord(
        source_id="MOIS:261088",
        sender_or_region="서울특별시 노원구",
        sent_at=now,
        original_body="본문",
        source_url="https://www.safetydata.go.kr/disaster-data/view?dataSn=228",
        detected_at=now,
        raw_payload={},
    )
    db.insert(mois_record)

    safekorea_equivalent = DisasterMessageRecord(
        source_id="SAFEKOREA:261088",
        sender_or_region="서울특별시 노원구",
        sent_at=now,
        original_body="[노원구] 본문",  # SafeKorea's org-name-prefixed body
        source_url="https://www.safekorea.go.kr/safekorea-kor/ctim/cmsg/calamitySms.do",
        detected_at=now,
        raw_payload={},
    )
    already_known = db.is_known(safekorea_equivalent)

    different_message = DisasterMessageRecord(
        source_id="SAFEKOREA:999999",
        sender_or_region="서울특별시 강남구",
        sent_at=now,
        original_body="완전히 다른 신규 메시지",
        source_url="https://www.safekorea.go.kr/safekorea-kor/ctim/cmsg/calamitySms.do",
        detected_at=now,
        raw_payload={},
    )
    genuinely_new = not db.is_known(different_message)

    db.close()
    check("Cross-source deduplication", already_known and genuinely_new)


def _check_source_cutover_safety(db_path: Path, check, monkeypatch_fetch) -> None:
    from app.commands import source_cutover_mois

    settings = _settings(database_path=db_path)
    db = Database(db_path)
    from app.models import DisasterMessageRecord

    now_ = None
    from datetime import datetime

    now_ = datetime.now(tz=SEOUL_TZ)
    db.insert(
        DisasterMessageRecord(
            source_id="LEGACY1",
            sender_or_region="서울특별시 테스트구",
            sent_at=now_,
            original_body="레거시 데이터",
            source_url="https://safecity.seoul.go.kr/legacy",
            detected_at=now_,
            raw_payload={},
        )
    )
    db.pause_polling(actor_user_id=1)
    db.close()

    from app.collector import CollectionResult

    new_record = DisasterMessageRecord(
        source_id="MOIS:1",
        sender_or_region="서울특별시 테스트구",
        sent_at=now_,
        original_body="신규 baseline 후보",
        source_url="https://www.safetydata.go.kr/disaster-data/view?dataSn=228",
        detected_at=now_,
        raw_payload={},
    )

    def fake_fetch(settings, **kwargs):
        return CollectionResult(
            records=[new_record], method=METHOD_MOIS_API, fetched_count=1, full_text_confirmed=True
        )

    original = source_cutover_mois.fetch_records
    source_cutover_mois.fetch_records = fake_fetch
    try:
        rc1 = source_cutover_mois.run_bootstrap(settings)
        rc2 = source_cutover_mois.run_bootstrap(settings)  # idempotency
    finally:
        source_cutover_mois.fetch_records = original

    db2 = Database(db_path)
    deliveries = db2._conn.execute("SELECT COUNT(*) c FROM telegram_deliveries").fetchone()["c"]
    suggestions = db2._conn.execute("SELECT COUNT(*) c FROM template_suggestions").fetchone()["c"]
    ids_after = db2.known_source_ids()
    completed = db2.is_source_bootstrap_completed()
    db2.close()

    check(
        "Source cutover safety",
        rc1 == 0
        and rc2 == 0
        and deliveries == 0
        and suggestions == 0
        and ids_after == {"LEGACY1", "MOIS:1"}
        and completed is True,
    )


def _check_independent_fan_out(db_path: Path, check) -> None:
    settings = _settings(database_path=db_path)
    sender = RecordingSender()

    original_sender = poll_once.TelegramSender
    poll_once.TelegramSender = lambda settings: sender

    def mois_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load_json("mois_success.json"))

    original_fetch = poll_once.fetch_records
    poll_once.fetch_records = lambda: fetch_records(
        settings, mois_transport=httpx.MockTransport(mois_handler)
    )

    try:
        db = Database(db_path)
        db.register_or_touch_subscription(user_id=201, chat_id=201, chat_type="private")
        db.register_or_touch_subscription(user_id=202, chat_id=202, chat_type="private")
        db.close()

        # First cycle establishes baseline (no send), second cycle should be
        # a genuinely-new-record delivery — but since baseline already
        # absorbed these exact records, seed a fresh set via a second fetch
        # with a different SN to exercise real fan-out.
        run_poll_cycle(settings, send=True, notify_existing=False)

        second_payload = _load_json("mois_success.json")
        for item in second_payload["body"]:
            # Change SN, sent time, and body so raw_hash also differs from
            # the baseline batch — SN alone is not part of raw_hash.
            item["SN"] = item["SN"] + 1000
            item["CRT_DT"] = item["CRT_DT"].replace("2026/07/14", "2026/07/15")
            item["MSG_CN"] = item["MSG_CN"] + " (second cycle)"
        poll_once.fetch_records = lambda: fetch_records(
            settings,
            mois_transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=second_payload)
            ),
        )
        run_poll_cycle(settings, send=True, notify_existing=False)
    finally:
        poll_once.TelegramSender = original_sender
        poll_once.fetch_records = original_fetch

    recipients = {chat_id for chat_id, _source_id in sender.sent}
    check(
        "Independent Telegram fan-out",
        recipients == {"201", "202"} and len(sender.sent) == 2 * 3,
    )


def _check_legacy_safecity_disabled(check) -> None:
    import ast

    # A bare domain mention in a comment/docstring (e.g. explaining why the
    # old collector was retired) is documentation, not a runtime call — only
    # an actual URL literal (scheme + domain) indicates real usage.
    forbidden_url_fragment = f"://{FORBIDDEN_DOMAIN}"

    app_dir = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for path in [
        app_dir / "collector.py",
        app_dir / "mois_api.py",
        app_dir / "safekorea_fallback.py",
    ]:
        source = path.read_text(encoding="utf-8")
        if forbidden_url_fragment in source:
            offenders.append(str(path))
        try:
            ast.parse(source)
        except SyntaxError:
            offenders.append(f"{path} (syntax error)")

    from app import config as config_module

    has_legacy_constants = hasattr(config_module, "SOURCE_PAGE_URL") or hasattr(
        config_module, "SOURCE_API_URL"
    )

    check(
        "Legacy Seoul SafeCity disabled",
        not offenders and not has_legacy_constants,
    )


def main() -> int:
    results: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        results.append((name, bool(ok)))

    with tempfile.TemporaryDirectory() as tmp:
        _check_mois_api_parsing(check)
        _check_seoul_recipient_filtering(check)
        _check_valid_empty_handling(check)
        _check_safekorea_fallback(check)
        _check_cross_source_dedup(Path(tmp) / "dedup.db", check)
        _check_source_cutover_safety(Path(tmp) / "cutover.db", check, None)
        _check_independent_fan_out(Path(tmp) / "fanout.db", check)
        _check_legacy_safecity_disabled(check)

    print("Source pipeline validation")
    print()
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

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.ai_client import generate_slots

SEOUL_TZ = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 7, 14, 15, 0, tzinfo=SEOUL_TZ)

MESSAGE_TEXT = "금일 15시 부로 예천천에 발효 중이던 홍수주의보가 발령되었습니다."
REQUIRED = ["기준시각", "하천명"]
OPTIONAL: list[str] = []


def _slot_entry(name: str, value: str, evidence: str):
    return SimpleNamespace(name=name, value=value, evidence=evidence)


def _completion(*, template_id="FLOOD_ADVISORY_ISSUED", slots=(), missing_slots=(), refusal=None):
    parsed = None
    if refusal is None:
        parsed = SimpleNamespace(
            template_id=template_id, slots=list(slots), missing_slots=list(missing_slots)
        )
    message = SimpleNamespace(parsed=parsed, refusal=refusal)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=42, completion_tokens=17)
    return SimpleNamespace(choices=[choice], usage=usage, model_dump_json=lambda: "{}")


class FakeClient:
    def __init__(self, *, completion=None, exc: Exception | None = None):
        self._completion = completion
        self._exc = exc
        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        return self._completion


def _call(client, settings, **overrides):
    kwargs = dict(
        template_id="FLOOD_ADVISORY_ISSUED",
        message_text=MESSAGE_TEXT,
        sender_or_region="",
        sent_at=NOW,
        required_slots=REQUIRED,
        optional_slots=OPTIONAL,
        existing_rule_slots={},
        settings=settings,
        client=client,
    )
    kwargs.update(overrides)
    return generate_slots(**kwargs)


def test_missing_api_key_returns_api_failed_without_calling_client(make_settings):
    settings = make_settings(ai_enabled=True, openai_api_key="")
    result = generate_slots(
        template_id="FLOOD_ADVISORY_ISSUED",
        message_text=MESSAGE_TEXT,
        sender_or_region="",
        sent_at=NOW,
        required_slots=REQUIRED,
        optional_slots=OPTIONAL,
        existing_rule_slots={},
        settings=settings,
        client=None,
    )
    assert result.status == "api_failed"
    assert "OPENAI_API_KEY" in result.error


def test_default_settings_never_have_a_real_key_loaded(make_settings):
    # No .env is ever readable during tests (see conftest's autouse
    # _never_load_the_real_dotenv fixture) — this just documents that a
    # plain Settings() has no key unless a test explicitly supplies one.
    settings = make_settings()
    assert settings.openai_api_key == ""
    assert settings.ai_configured is False


def test_network_exception_returns_api_failed(make_settings):
    settings = make_settings(ai_enabled=True, openai_api_key="sk-test")
    client = FakeClient(exc=RuntimeError("connection reset"))
    result = _call(client, settings)
    assert result.status == "api_failed"
    assert "connection reset" in result.error


def test_model_refusal_returns_api_failed(make_settings):
    settings = make_settings(ai_enabled=True, openai_api_key="sk-test")
    client = FakeClient(completion=_completion(refusal="cannot help with that"))
    result = _call(client, settings)
    assert result.status == "api_failed"
    assert "refus" in result.error.lower()


def test_wrong_template_id_returns_validation_failed(make_settings):
    settings = make_settings(ai_enabled=True, openai_api_key="sk-test")
    client = FakeClient(
        completion=_completion(
            template_id="HEAVY_RAIN_CLEARED",
            slots=[_slot_entry("기준시각", "15시", "15시 부로")],
        )
    )
    result = _call(client, settings)
    assert result.status == "validation_failed"
    assert "template_id" in result.error


def test_undeclared_slot_is_rejected(make_settings):
    settings = make_settings(ai_enabled=True, openai_api_key="sk-test")
    client = FakeClient(
        completion=_completion(
            slots=[
                _slot_entry("기준시각", "15시", "15시 부로"),
                _slot_entry("하천명", "예천천", "예천천"),
                _slot_entry("완전히없는슬롯", "값", "값"),
            ]
        )
    )
    result = _call(client, settings)
    assert result.status == "validation_failed"
    assert "undeclared" in result.error


def test_missing_evidence_is_rejected(make_settings):
    settings = make_settings(ai_enabled=True, openai_api_key="sk-test")
    client = FakeClient(
        completion=_completion(slots=[_slot_entry("기준시각", "15시", "")])
    )
    result = _call(client, settings)
    assert result.status == "validation_failed"
    assert "evidence" in result.error


def test_evidence_not_found_in_original_text_is_rejected(make_settings):
    settings = make_settings(ai_enabled=True, openai_api_key="sk-test")
    client = FakeClient(
        completion=_completion(
            slots=[_slot_entry("기준시각", "15시", "이 문구는 원문에 없습니다")]
        )
    )
    result = _call(client, settings)
    assert result.status == "validation_failed"
    assert "evidence" in result.error


def test_missing_required_slot_returns_validation_failed(make_settings):
    settings = make_settings(ai_enabled=True, openai_api_key="sk-test")
    client = FakeClient(
        completion=_completion(
            slots=[_slot_entry("기준시각", "15시", "15시 부로")], missing_slots=["하천명"]
        )
    )
    result = _call(client, settings)
    assert result.status == "validation_failed"
    assert "하천명" in result.error
    assert "하천명" in result.missing_slots


def test_successful_extraction_returns_succeeded_with_slots(make_settings):
    settings = make_settings(ai_enabled=True, openai_api_key="sk-test")
    client = FakeClient(
        completion=_completion(
            slots=[
                _slot_entry("기준시각", "15시", "15시 부로"),
                _slot_entry("하천명", "예천천", "예천천"),
            ]
        )
    )
    result = _call(client, settings)
    assert result.status == "succeeded"
    assert result.slots["기준시각"].value == "15시"
    assert result.slots["기준시각"].source == "ai"
    assert result.slots["하천명"].value == "예천천"
    assert result.missing_slots == []
    assert result.input_tokens == 42
    assert result.output_tokens == 17


def test_client_constructed_with_configured_timeout_and_retries(make_settings, monkeypatch):
    settings = make_settings(
        ai_enabled=True, openai_api_key="sk-test", openai_timeout_seconds=12.5, openai_max_retries=4
    )
    captured = {}

    def _fake_openai(**kwargs):
        captured.update(kwargs)
        return FakeClient(completion=_completion(slots=[]))

    monkeypatch.setattr("app.ai_client.OpenAI", _fake_openai)
    _call(None, settings)  # client=None -> forces _get_client() to build one via OpenAI(...)
    assert captured["api_key"] == "sk-test"
    assert captured["timeout"] == 12.5
    assert captured["max_retries"] == 4

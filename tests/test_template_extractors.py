from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.template_extractors import extract_slots

SEOUL_TZ = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 7, 14, 15, 0, tzinfo=SEOUL_TZ)


def test_region_prefers_structured_sender_field():
    result = extract_slots(
        "HW-05",
        "오늘 15시 부로 관내에 발효 중이던 호우주의보가 해제되었습니다. [예천군]",
        "예천군",
        NOW,
    )
    assert result.extracted_slots["지역"].value == "예천군"
    assert result.extracted_slots["지역"].source == "sender_or_region"


def test_region_falls_back_to_trailing_bracket_when_no_structured_field():
    result = extract_slots(
        "HW-05",
        "오늘 15시 부로 관내에 발효 중이던 호우주의보가 해제되었습니다. [예천군]",
        "",
        NOW,
    )
    assert result.extracted_slots["지역"].value == "예천군"
    assert result.extracted_slots["지역"].source == "message_body"


def test_river_name_extraction():
    result = extract_slots(
        "FL-01",
        "오늘 12:40 도림천 신대방역·신림역 인근 침수주의보 발령 기준.",
        "",
        NOW,
    )
    assert result.extracted_slots["하천지점"].value == "도림천"


def test_time_anchor_requires_explicit_anchor_word():
    # No 기준/부로/현재 anchor directly after the time -> must not be extracted.
    result = extract_slots(
        "FL-01",
        "금일 05:30 공주시 홍수주의보 발령, 대피 바랍니다.",
        "공주시",
        NOW,
    )
    assert "기준일시" not in result.extracted_slots
    assert "기준일시" in result.missing_required_slots


def test_time_anchor_does_not_truncate_colon_form():
    result = extract_slots(
        "HT-01",
        "7월11일 14:00시 기준 담양군 폭염경보 발령",
        "담양군",
        NOW,
    )
    assert result.extracted_slots["기준일시"].value == "7월11일 14:00시"


def test_release_time_handles_particle_between_time_and_anchor():
    result = extract_slots(
        "HW-05",
        "금일 18시30분을 기하여 하동군에 발효된 호우주의보가 해제되었습니다.",
        "하동군",
        NOW,
    )
    assert result.extracted_slots["해제일시"].value == "18시30분"
    assert result.required_slots_complete


def test_missing_required_slot_is_reported_not_invented():
    result = extract_slots(
        "FL-01",
        "금일 05:30 공주시 홍수주의보 발령, 대피 바랍니다.",
        "공주시",
        NOW,
    )
    assert result.required_slots_complete is False
    assert set(result.missing_required_slots) == {"기준일시", "하천지점"}
    # No slot value is fabricated for a field that isn't in the text.
    assert "하천지점" not in result.extracted_slots


def test_announcement_time_falls_back_to_sent_at_when_no_body_anchor():
    result = extract_slots(
        "TN-01",
        "금일 17:00 열대야주의보 발효 중입니다.",
        "연천군",
        NOW,
    )
    assert result.extracted_slots["발효일시"].source == "sent_at"
    assert result.required_slots_complete


def test_multi_level_labeled_region_lists():
    # The extractor now returns only the raw labeled values — no fixed
    # "☔ 홍수주의보\n..." prose. The heading lives in the YAML template's
    # {#홍수지역}...{/홍수지역} conditional block (see app/template_renderer.py).
    text = "20:00 기준 세종시 호우특보 발효\n호우경보 : 부강면, 연서면\n호우주의보 : 조치원읍, 연동면\n홍수주의보 : 금강 일대"
    result = extract_slots("HEAVY_RAIN_MULTI_LEVEL_ISSUED", text, "세종특별자치시", NOW)
    assert result.extracted_slots["경보지역"].value == "부강면, 연서면"
    assert result.extracted_slots["주의보지역"].value == "조치원읍, 연동면"
    assert result.extracted_slots["홍수지역"].value == "금강 일대"


def test_multi_level_optional_slots_absent_when_not_labeled():
    text = "20:00 기준 세종시 호우특보 발효\n호우경보 : 부강면\n호우주의보 : 조치원읍"
    result = extract_slots("HEAVY_RAIN_MULTI_LEVEL_ISSUED", text, "세종특별자치시", NOW)
    assert "홍수지역" not in result.extracted_slots
    assert result.required_slots_complete


def test_original_only_always_complete():
    result = extract_slots("ORIGINAL_ONLY", "아무 원문 텍스트.", "서울특별시", NOW)
    assert result.required_slots_complete
    assert result.extracted_slots["원문"].value == "아무 원문 텍스트."
    assert result.extracted_slots["원문"].source == "original_message"


def test_unknown_template_id_returns_validation_error():
    result = extract_slots("NOT_A_REAL_TEMPLATE", "text", "region", NOW)
    assert result.required_slots_complete is False
    assert result.validation_errors


# --- river extraction priority order (spec section 14) -----------------------


def test_river_explicit_label_takes_priority():
    result = extract_slots(
        "FL-01",
        "하천명: 탄천, 12:00 기준 홍수주의보 발령. 인근 청계천도 모니터링 중입니다.",
        "",
        NOW,
    )
    assert result.extracted_slots["하천지점"].value == "탄천"
    assert result.extracted_slots["하천지점"].source == "explicit_label"


def test_river_dictionary_match_when_no_label_or_nearby_suffix():
    # "도림천" appears, but not immediately near a "홍수주의보" occurrence in
    # this text (there is no 홍수주의보 keyword at all here) -> falls through
    # to the dictionary tier.
    result = extract_slots("FL-01", "오늘 12:40 기준 도림천 인근 침수 위험이 있습니다.", "", NOW)
    assert result.extracted_slots["하천지점"].value == "도림천"
    assert result.extracted_slots["하천지점"].source == "dictionary_match"


def test_river_regex_false_positive_health_word_is_excluded():
    # "건강관리" ends in 강 and would match the bare suffix regex, but it must
    # never be returned as a "river" — this was the flagged PR #3 issue.
    result = extract_slots("FL-01", "12:00 기준 홍수주의보 발령. 건강관리에 유의하세요.", "", NOW)
    assert "하천지점" not in result.extracted_slots


def test_river_regex_still_matches_legitimate_name_near_keyword():
    result = extract_slots("FL-01", "12:00 기준 석성천인근 홍수주의보 발령되었습니다.", "", NOW)
    assert result.extracted_slots["하천지점"].value == "석성천"

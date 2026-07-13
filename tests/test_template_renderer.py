from __future__ import annotations

from app.template_extractors import SlotValue
from app.template_renderer import get_template, load_templates, render_template


def _slot(value: str, source: str = "message_body") -> SlotValue:
    return SlotValue(value=value, source=source, evidence=value, confidence=1.0)


def test_all_nine_templates_load():
    templates = load_templates()
    expected = {
        "FLOOD_ADVISORY_ISSUED",
        "HEAVY_RAIN_CLEARED",
        "HEAVY_RAIN_DOWNGRADED",
        "HEAVY_RAIN_MULTI_LEVEL_ISSUED",
        "HEATWAVE_UPGRADED",
        "HEATWAVE_ADVISORY_ISSUED",
        "TROPICAL_NIGHT_ADVISORY_ISSUED",
        "ORIGINAL_ONLY",
        "UNKNOWN",
    }
    assert expected <= set(templates)


def test_render_preserves_emoji_and_line_breaks():
    result = render_template(
        "FLOOD_ADVISORY_ISSUED",
        {"기준시각": _slot("14:00"), "하천명": _slot("도림천")},
    )
    assert result.success
    assert "📢 홍수주의보 발효 안내" in result.rendered_text
    assert "☔ 홍수주의보\n도림천" in result.rendered_text
    assert "(14:00 기준)" in result.rendered_text


def test_render_fails_on_missing_required_slot():
    result = render_template("FLOOD_ADVISORY_ISSUED", {"기준시각": _slot("14:00")})
    assert not result.success
    assert result.missing_slots == ["하천명"]
    assert result.rendered_text is None


def test_render_never_leaves_unresolved_required_placeholder():
    result = render_template("HEAVY_RAIN_CLEARED", {})
    assert not result.success
    assert "{" not in (result.rendered_text or "")


def test_optional_conditional_block_cleanly_omitted_when_absent():
    # No 홍수지역 slot at all -> the entire "{#홍수지역}☔ 홍수주의보\n...{/홍수지역}"
    # span (heading included) must be dropped, not left as a naked heading.
    result = render_template(
        "HEAVY_RAIN_MULTI_LEVEL_ISSUED",
        {
            "지역": _slot("세종특별자치시"),
            "기준시각": _slot("20:00"),
            "경보지역": _slot("부강면"),
            "주의보지역": _slot("조치원읍"),
        },
    )
    assert result.success
    assert "홍수주의보" not in result.rendered_text
    assert "{" not in result.rendered_text
    # No stray 3+ blank-line runs left behind by the omitted block.
    assert "\n\n\n" not in result.rendered_text


def test_optional_conditional_block_included_when_present():
    result = render_template(
        "HEAVY_RAIN_MULTI_LEVEL_ISSUED",
        {
            "지역": _slot("세종특별자치시"),
            "기준시각": _slot("20:00"),
            "경보지역": _slot("부강면"),
            "주의보지역": _slot("조치원읍"),
            "홍수지역": _slot("금강 일대"),
        },
    )
    assert result.success
    assert "☔ 홍수주의보\n금강 일대" in result.rendered_text


def test_all_three_multi_level_headings_omitted_when_all_absent():
    # No empty "☔ 호우경보"/"☔ 호우주의보" headings left behind either — this
    # was the flagged PR #3 review issue.
    result = render_template(
        "HEAVY_RAIN_MULTI_LEVEL_ISSUED",
        {"지역": _slot("세종특별자치시"), "기준시각": _slot("20:00")},
    )
    assert result.success
    assert "호우경보" not in result.rendered_text
    assert "호우주의보" not in result.rendered_text


def test_heatwave_upgraded_maintained_region_sentence_is_yaml_owned():
    # The extractor only ever returns the raw 유지지역 value (see
    # app/template_extractors.py) — the fixed sentence lives entirely in
    # the YAML template's conditional block.
    result = render_template(
        "HEATWAVE_UPGRADED",
        {
            "발표일시": _slot("18:00"),
            "지역": _slot("서울특별시"),
            "유지지역": _slot("강남구"),
        },
    )
    assert result.success
    assert "강남구은(는) 폭염주의보가 유지됩니다." in result.rendered_text


def test_heatwave_upgraded_omits_maintained_block_when_absent():
    result = render_template(
        "HEATWAVE_UPGRADED", {"발표일시": _slot("18:00"), "지역": _slot("서울특별시")}
    )
    assert result.success
    assert "유지됩니다" not in result.rendered_text
    assert "{" not in result.rendered_text


def test_unknown_template_id_is_rejected():
    result = render_template("NOT_A_TEMPLATE", {})
    assert not result.success
    assert result.validation_errors


def test_original_only_passes_body_through_unaltered():
    body = "본문 그대로.\n줄바꿈도 유지됩니다. ▲항목1 ▲항목2"
    result = render_template("ORIGINAL_ONLY", {"원문": _slot(body, source="original_message")})
    assert result.success
    assert result.rendered_text == body


def test_get_template_returns_none_for_unknown_id():
    assert get_template("NOPE") is None

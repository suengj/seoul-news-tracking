from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.template_rules import TEMPLATE_IDS, recommend_template, suggest_templates

SEOUL_TZ = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=SEOUL_TZ)


def test_suggest_templates_covers_all_seven_template_ids():
    suggestions = suggest_templates("아무 의미 없는 문장입니다.", "서울특별시", NOW)
    assert {s.template_id for s in suggestions} == set(TEMPLATE_IDS)


def test_suggest_templates_sorted_by_score_descending():
    text = "20:00 기준 세종시 호우경보 호우주의보 발효. 하천 수위 상승 우려."
    suggestions = suggest_templates(text, "세종특별자치시", NOW)
    scores = [s.rule_score for s in suggestions]
    assert scores == sorted(scores, reverse=True)


def test_flood_advisory_scores_high_when_signals_present():
    text = "금일 05:30 공주시 홍수주의보 발령. 안전에 유의하세요."
    suggestions = {s.template_id: s for s in suggest_templates(text, "공주시", NOW)}
    assert suggestions["FLOOD_ADVISORY_ISSUED"].rule_score == 1.0
    assert not suggestions["FLOOD_ADVISORY_ISSUED"].conflict_signals


def test_heavy_rain_cleared_detected():
    text = "오늘 15시 부로 관내에 발효 중이던 호우주의보가 해제되었습니다."
    suggestions = {s.template_id: s for s in suggest_templates(text, "예천군", NOW)}
    assert suggestions["HEAVY_RAIN_CLEARED"].rule_score == 1.0


def test_heavy_rain_downgraded_detected():
    text = "호우경보가 호우주의보로 하향 변경되었습니다."
    suggestions = {s.template_id: s for s in suggest_templates(text, "과천시", NOW)}
    assert suggestions["HEAVY_RAIN_DOWNGRADED"].rule_score == 1.0
    # Not also flagged as a fresh multi-level issuance (하향 conflicts with it).
    assert suggestions["HEAVY_RAIN_MULTI_LEVEL_ISSUED"].conflict_signals


def test_heavy_rain_multi_level_issued_detected():
    text = "20:00 기준 세종시 호우경보 및 호우주의보 발효 중입니다."
    suggestions = {s.template_id: s for s in suggest_templates(text, "세종특별자치시", NOW)}
    assert suggestions["HEAVY_RAIN_MULTI_LEVEL_ISSUED"].rule_score == 1.0
    assert not suggestions["HEAVY_RAIN_MULTI_LEVEL_ISSUED"].conflict_signals


def test_heatwave_upgraded_detected():
    # Requires both 폭염주의보 AND 폭염경보 (not just the warning alone) plus a
    # transition word, so a genuine advisory-to-warning upgrade is required.
    text = "폭염주의보에서 폭염경보로 상향 변경되었습니다."
    suggestions = {s.template_id: s for s in suggest_templates(text, "안양시", NOW)}
    assert suggestions["HEATWAVE_UPGRADED"].rule_score == 1.0


def test_heatwave_upgraded_requires_advisory_not_just_warning():
    # 폭염경보 alone (no 폭염주의보 mention) must not score a full match —
    # this was the PR #3 review issue: the old rule only required 폭염경보 +
    # a transition word, which could false-positive on a plain warning.
    text = "폭염경보로 상향 변경되었습니다."
    suggestions = {s.template_id: s for s in suggest_templates(text, "안양시", NOW)}
    assert suggestions["HEATWAVE_UPGRADED"].rule_score < 1.0


def test_heatwave_advisory_issued_detected_and_excludes_upgrade_wording():
    text = "폭염주의보 발효 중. 건강관리에 유의하세요."
    suggestions = {s.template_id: s for s in suggest_templates(text, "곡성군", NOW)}
    assert suggestions["HEATWAVE_ADVISORY_ISSUED"].rule_score == 1.0
    assert not suggestions["HEATWAVE_ADVISORY_ISSUED"].conflict_signals

    # Same wording plus 폭염경보 must conflict (it's an upgrade, not a plain advisory).
    text_with_warning = "폭염주의보 발효 중이며 일부 지역 폭염경보 발령."
    suggestions2 = {s.template_id: s for s in suggest_templates(text_with_warning, "곡성군", NOW)}
    assert suggestions2["HEATWAVE_ADVISORY_ISSUED"].conflict_signals


def test_tropical_night_requires_explicit_advisory_phrase():
    explicit = "금일 17:00 열대야주의보 발효 중입니다."
    generic = "열대야로 높은 기온이 이어지겠습니다. 건강관리에 유의하세요."

    explicit_scores = {s.template_id: s.rule_score for s in suggest_templates(explicit, "연천군", NOW)}
    generic_scores = {s.template_id: s.rule_score for s in suggest_templates(generic, "경산시", NOW)}

    assert explicit_scores["TROPICAL_NIGHT_ADVISORY_ISSUED"] == 1.0
    # Generic 무더위/열대야 wording without the explicit "주의보" phrase must not match.
    assert generic_scores["TROPICAL_NIGHT_ADVISORY_ISSUED"] == 0.0


def test_recommend_template_never_picks_a_conflicted_candidate():
    # Fully matches HEAVY_RAIN_DOWNGRADED's required wording, but also says
    # 해제 (fully cleared) — a hard conflict for that template, so it must
    # never be the recommendation even though its raw score is high.
    text = "호우경보가 호우주의보로 하향 변경되었으며 해제되었습니다."
    recommended, _ = recommend_template(text, "과천시", NOW, threshold=0.5)
    assert recommended is None or recommended.template_id != "HEAVY_RAIN_DOWNGRADED"


def test_recommend_template_below_threshold_is_unknown():
    text = "폭염경보 발령. 야외활동 자제."
    recommended, candidates = recommend_template(text, "담양군", NOW, threshold=0.85)
    assert recommended is None
    assert any(c.template_id == "HEATWAVE_ADVISORY_ISSUED" for c in candidates)


def test_recommend_template_requires_extractable_slots():
    # High score but river name can't be found -> must fall back to None (UNKNOWN).
    text = "금일 05:30 공주시 홍수주의보 발령, 대피 바랍니다."
    recommended, candidates = recommend_template(text, "", NOW, threshold=0.85)
    top = next(c for c in candidates if c.template_id == "FLOOD_ADVISORY_ISSUED")
    assert top.rule_score == 1.0
    assert recommended is None


def test_recommend_template_succeeds_when_slots_extractable():
    text = "오늘 15시 부로 관내에 발효 중이던 호우주의보가 해제되었습니다."
    recommended, _ = recommend_template(text, "예천군", NOW, threshold=0.85)
    assert recommended is not None
    assert recommended.template_id == "HEAVY_RAIN_CLEARED"

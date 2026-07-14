"""Deterministic, non-statistical template recommendation rules.

`rule_score` is a hand-authored match ratio (matched required signal groups /
total required signal groups) — it is **not** a statistical probability and
involves no machine learning, embeddings, or LLM calls. See
`docs/template_pattern_analysis.md` for the historical wording this rule set
was sanity-checked against.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.template_extractors import extract_slots

TEMPLATE_IDS: tuple[str, ...] = (
    "FL-01",
    "HW-05",
    "HW-04",
    "HEAVY_RAIN_MULTI_LEVEL_ISSUED",  # legacy-hidden only; kept for scoring continuity
    "HT-03",
    "HT-01",
    "TN-01",
)


@dataclass
class TemplateSuggestion:
    template_id: str
    rule_score: float
    matched_signals: list[str] = field(default_factory=list)
    missing_signals: list[str] = field(default_factory=list)
    conflict_signals: list[str] = field(default_factory=list)


@dataclass
class _RuleSpec:
    template_id: str
    # Each group is (label, checker). A group is "matched" if checker(text) is True.
    groups: list[tuple[str, Callable[[str], bool]]]
    conflicts: list[tuple[str, Callable[[str], bool]]]


def _contains(*terms: str) -> Callable[[str], bool]:
    def check(text: str) -> bool:
        return any(term in text for term in terms)

    return check


_RULES: list[_RuleSpec] = [
    _RuleSpec(
        template_id="FL-01",
        groups=[
            ("contains 홍수주의보", _contains("홍수주의보")),
            ("contains 발령 or 발효", _contains("발령", "발효")),
        ],
        conflicts=[("contains 해제 (already cleared)", _contains("해제"))],
    ),
    _RuleSpec(
        template_id="HW-05",
        groups=[
            ("contains 호우", _contains("호우")),
            ("contains 해제", _contains("해제")),
        ],
        conflicts=[
            (
                "wording suggests another warning remains active",
                _contains("유지되고 있", "지속됩니다", "일부 지역은"),
            )
        ],
    ),
    _RuleSpec(
        template_id="HW-04",
        groups=[
            ("contains 호우경보", _contains("호우경보")),
            ("contains 호우주의보", _contains("호우주의보")),
            ("contains 하향, 변경, or 대치", _contains("하향", "변경", "대치")),
        ],
        conflicts=[("contains 해제 (fully cleared, not downgraded)", _contains("해제"))],
    ),
    _RuleSpec(
        template_id="HEAVY_RAIN_MULTI_LEVEL_ISSUED",
        groups=[
            ("contains 호우경보", _contains("호우경보")),
            ("contains 호우주의보", _contains("호우주의보")),
            ("contains 발효 or 발령", _contains("발효", "발령")),
        ],
        conflicts=[
            ("contains 해제 or 하향 (not a fresh multi-level issuance)", _contains("해제", "하향"))
        ],
    ),
    _RuleSpec(
        template_id="HT-03",
        groups=[
            ("contains 폭염주의보", _contains("폭염주의보")),
            ("contains 폭염경보", _contains("폭염경보")),
            ("contains 상향, 변경, or 대치", _contains("상향", "변경", "대치")),
        ],
        conflicts=[("contains 해제 or 하향", _contains("해제", "하향"))],
    ),
    _RuleSpec(
        template_id="HT-01",
        groups=[
            ("contains 폭염주의보", _contains("폭염주의보")),
            ("contains 발효 or 발령", _contains("발효", "발령")),
        ],
        conflicts=[
            ("contains 폭염경보 (this is an upgrade, not a plain advisory)", _contains("폭염경보"))
        ],
    ),
    _RuleSpec(
        template_id="TN-01",
        groups=[
            (
                "contains 열대야주의보 (explicit advisory phrase, not generic 무더위 wording)",
                _contains("열대야주의보"),
            ),
        ],
        conflicts=[("contains 해제", _contains("해제"))],
    ),
]


def _score_rule(spec: _RuleSpec, message_text: str) -> TemplateSuggestion:
    matched = [label for label, check in spec.groups if check(message_text)]
    missing = [label for label, check in spec.groups if not check(message_text)]
    conflicts = [label for label, check in spec.conflicts if check(message_text)]
    total = len(spec.groups)
    score = (len(matched) / total) if total else 0.0
    if conflicts:
        score = max(0.0, score - 0.5 * len(conflicts))
    return TemplateSuggestion(
        template_id=spec.template_id,
        rule_score=round(score, 4),
        matched_signals=matched,
        missing_signals=missing,
        conflict_signals=conflicts,
    )


def suggest_templates(
    message_text: str, sender_or_region: str, sent_at: datetime
) -> list[TemplateSuggestion]:
    """Score every known template against the message text.

    `sender_or_region` and `sent_at` are accepted for interface symmetry with
    `extract_slots` but the current rule set only inspects `message_text` —
    the seven templates are distinguished entirely by wording, not by region
    or time of day.
    """
    suggestions = [_score_rule(spec, message_text) for spec in _RULES]
    suggestions.sort(key=lambda s: s.rule_score, reverse=True)
    return suggestions


def recommend_template(
    message_text: str,
    sender_or_region: str,
    sent_at: datetime,
    *,
    threshold: float,
) -> tuple[TemplateSuggestion | None, list[TemplateSuggestion]]:
    """Pick the top rule-scored, conflict-free, fully-extractable candidate.

    Returns `(None, candidates)` — meaning UNKNOWN — when no candidate clears
    the threshold, has a conflict, or is missing a required slot.
    """
    candidates = suggest_templates(message_text, sender_or_region, sent_at)
    for candidate in candidates:
        if candidate.conflict_signals:
            continue
        if candidate.rule_score < threshold:
            continue
        extraction = extract_slots(candidate.template_id, message_text, sender_or_region, sent_at)
        if extraction.required_slots_complete:
            return candidate, candidates
    return None, candidates

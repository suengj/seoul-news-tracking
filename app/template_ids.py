"""Canonical Excel template IDs and legacy-alias resolution.

Business IDs from the workbook (HW-01, HT-03, …) are canonical. Historical
Service v1 IDs remain readable via a small alias map so old DB rows and
callbacks still resolve without a bulk rewrite.
"""

from __future__ import annotations

# Confirmed mappings from the Excel migration. Do not add
# HEAVY_RAIN_MULTI_LEVEL_ISSUED → HW-08: the meanings differ (see
# docs/template_engine.md).
LEGACY_ALIAS_TO_CANONICAL: dict[str, str] = {
    "FLOOD_ADVISORY_ISSUED": "FL-01",
    "HEAVY_RAIN_CLEARED": "HW-05",
    "HEAVY_RAIN_DOWNGRADED": "HW-04",
    "HEATWAVE_UPGRADED": "HT-03",
    "HEATWAVE_ADVISORY_ISSUED": "HT-01",
    "TROPICAL_NIGHT_ADVISORY_ISSUED": "TN-01",
}

# Kept as a hidden compatibility definition (conditional-block wording that
# has no exact Excel counterpart). Not shown in Telegram menus.
LEGACY_HIDDEN_TEMPLATE_IDS: frozenset[str] = frozenset({"HEAVY_RAIN_MULTI_LEVEL_ISSUED"})

SYSTEM_TEMPLATE_IDS: frozenset[str] = frozenset({"ORIGINAL_ONLY", "UNKNOWN"})

EXPECTED_WORKBOOK_TOTAL = 21
EXPECTED_AUTOMATION_COUNT = 19
EXPECTED_REFERENCE_COUNT = 2


def resolve_template_id(template_id: str) -> str:
    """Return the canonical Excel ID when `template_id` is a known legacy alias.

    Already-canonical IDs (and system/legacy-hidden IDs) pass through unchanged.
    """
    return LEGACY_ALIAS_TO_CANONICAL.get(template_id, template_id)

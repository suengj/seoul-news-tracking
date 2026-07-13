"""Read-only wording analysis of the historical raw archive.

Usage:
    python -m app.commands.analyze_template_patterns

Reads `data/history_raw.db` (see `docs/history_backfill.md`) in SQLite
read-only mode and writes `docs/template_pattern_analysis.md` plus a small
`artifacts/template_analysis/summary.json`. Never writes to the historical
database, never exports the full 5,005-record dataset, and never contacts
any live source.

The 5,005 historical records are *nationwide* raw disaster messages used
only to inspect wording variations for the seven templates in
`config/message_templates.yaml`. They are not treated as ground-truth labels
and are not related to Seoul/non-Seoul filtering — see
`docs/template_pattern_analysis.md` for the full caveat.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app.config import load_settings

MAX_EXAMPLES = 10
EXAMPLE_MAX_CHARS = 160


@dataclass
class KeywordGroup:
    """One `LIKE`-based candidate query for a template concept."""

    template_id: str
    description: str
    must_contain_any: list[str] = field(default_factory=list)
    must_contain_all: list[str] = field(default_factory=list)
    must_not_contain_any: list[str] = field(default_factory=list)
    notes: str = ""


# Search concepts taken directly from the task spec (section 4). Keyword
# matches are candidate examples only, never ground-truth labels.
GROUPS: list[KeywordGroup] = [
    KeywordGroup(
        template_id="FLOOD_ADVISORY_ISSUED",
        description="홍수주의보 발령/발효 (하천 관련)",
        must_contain_all=["홍수주의보"],
        must_contain_any=["발령", "발효"],
        notes="proposed rule phrases: '홍수주의보' + ('발령'|'발효'); river name token "
        "(...천|...강) preferred but not required for candidate search",
    ),
    KeywordGroup(
        template_id="HEAVY_RAIN_CLEARED",
        description="호우특보 해제",
        must_contain_all=["호우", "해제"],
        notes="proposed rule phrases: '호우' + '해제'; conflict when another warning "
        "is still described as active in the same message",
    ),
    KeywordGroup(
        template_id="HEAVY_RAIN_DOWNGRADED",
        description="호우경보 -> 호우주의보 하향",
        must_contain_all=["호우경보", "호우주의보"],
        must_contain_any=["하향", "변경", "대치"],
        notes="proposed rule phrases: both '호우경보' and '호우주의보' present + "
        "('하향'|'변경'|'대치')",
    ),
    KeywordGroup(
        template_id="HEAVY_RAIN_MULTI_LEVEL_ISSUED",
        description="호우경보 + 호우주의보 (+ 홍수주의보) 동시 발효",
        must_contain_all=["호우경보", "호우주의보"],
        must_contain_any=["발효", "발령"],
        notes="proposed rule phrases: both '호우경보' and '호우주의보' present + "
        "('발효'|'발령'); '홍수주의보' optional additional block",
    ),
    KeywordGroup(
        template_id="HEATWAVE_UPGRADED",
        description="폭염주의보 -> 폭염경보 상향",
        must_contain_all=["폭염경보"],
        must_contain_any=["상향", "변경", "대치"],
        notes="proposed rule phrases: '폭염경보' + ('상향'|'변경'|'대치')",
    ),
    KeywordGroup(
        template_id="HEATWAVE_ADVISORY_ISSUED",
        description="폭염주의보 발효/발령 (경보 아님)",
        must_contain_all=["폭염주의보"],
        must_contain_any=["발효", "발령"],
        must_not_contain_any=["폭염경보"],
        notes="proposed rule phrases: '폭염주의보' + ('발효'|'발령'), must not also "
        "mention '폭염경보' (that combination belongs to HEATWAVE_UPGRADED)",
    ),
    KeywordGroup(
        template_id="TROPICAL_NIGHT_ADVISORY_ISSUED",
        description="열대야주의보 발효 (명시적 주의보만, 일반 무더위 문구 제외)",
        must_contain_all=["열대야주의보"],
        notes="proposed rule phrases: '열대야주의보' explicitly present; generic "
        "'열대야'/'무더위' wording without '열대야주의보' must NOT be inferred as this "
        "template (see 'ambiguous' examples below)",
    ),
]

# A softer, broader query per template concept, used only to surface
# ambiguous/conflicting/UNKNOWN-leaning examples (never used for scoring).
BROAD_TERMS: dict[str, list[str]] = {
    "FLOOD_ADVISORY_ISSUED": ["홍수주의보"],
    "HEAVY_RAIN_CLEARED": ["호우", "특보", "해제"],
    "HEAVY_RAIN_DOWNGRADED": ["호우경보", "호우주의보"],
    "HEAVY_RAIN_MULTI_LEVEL_ISSUED": ["호우경보", "호우주의보", "홍수주의보"],
    "HEATWAVE_UPGRADED": ["폭염주의보", "폭염경보"],
    "HEATWAVE_ADVISORY_ISSUED": ["폭염주의보"],
    "TROPICAL_NIGHT_ADVISORY_ISSUED": ["열대야"],
}


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(
            f"historical database not found at {db_path}. Run backfill_history first "
            "(see docs/history_backfill.md)."
        )
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _like_clause(column: str, terms: list[str], negate: bool = False) -> tuple[str, list[str]]:
    op = "NOT LIKE" if negate else "LIKE"
    joiner = " AND " if negate else " OR "
    parts = [f"{column} {op} ?" for _ in terms]
    params = [f"%{term}%" for term in terms]
    return "(" + joiner.join(parts) + ")", params


def _short(text: str, limit: int = EXAMPLE_MAX_CHARS) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "..."


def _count_and_examples(
    conn: sqlite3.Connection, where_sql: str, params: list[str]
) -> tuple[int, list[str]]:
    (count,) = conn.execute(
        f"SELECT COUNT(*) FROM historical_raw_messages WHERE {where_sql}", params
    ).fetchone()
    rows = conn.execute(
        f"SELECT body_raw FROM historical_raw_messages WHERE {where_sql} "
        "ORDER BY internal_id ASC LIMIT ?",
        [*params, MAX_EXAMPLES],
    ).fetchall()
    return count, [_short(row["body_raw"]) for row in rows]


def _wording_variation_counts(conn: sqlite3.Connection, where_sql: str, params: list[str]) -> dict[str, int]:
    """Count a fixed set of connector/transition words within the matched set."""
    variation_terms = ["발효", "발령", "해제", "상향", "하향", "변경", "대치", "기준", "부로"]
    counts: dict[str, int] = {}
    for term in variation_terms:
        clause, term_params = _like_clause("body_raw", [term])
        (n,) = conn.execute(
            f"SELECT COUNT(*) FROM historical_raw_messages WHERE ({where_sql}) AND {clause}",
            [*params, *term_params],
        ).fetchone()
        if n:
            counts[term] = n
    return counts


_RIVER_RE = re.compile(r"[가-힣]{1,8}(?:천|강)(?=[가-힣]{0,4}(?:지점|인근|부근|\s|$))")
_TIME_ANCHOR_RE = re.compile(
    r"(?:\d{1,2}월\s*\d{1,2}일\s*)?\d{1,2}[:시]\s*\d{0,2}분?\s*(?:기준|부로|현재)"
)


def _sample_field_evidence(examples: list[str]) -> dict[str, int]:
    river_hits = sum(1 for ex in examples if _RIVER_RE.search(ex))
    time_hits = sum(1 for ex in examples if _TIME_ANCHOR_RE.search(ex))
    region_hits = sum(1 for ex in examples if "[" in ex and "]" in ex)
    return {"river_name_token": river_hits, "time_anchor_phrase": time_hits, "bracketed_region": region_hits}


def _ambiguous_examples(conn: sqlite3.Connection, template_id: str) -> list[str]:
    """Cases containing the broad terms but with an explicit still-active/mixed signal."""
    terms = BROAD_TERMS.get(template_id, [])
    if not terms:
        return []
    broad_clause, broad_params = _like_clause("body_raw", terms)
    # Messages that mention both an issue-style and a clear-style word are ambiguous
    # for CLEARED vs ISSUED style templates.
    mixed_clause = "(body_raw LIKE ? AND body_raw LIKE ?)"
    mixed_params = ["%발효%", "%해제%"]
    rows = conn.execute(
        f"SELECT body_raw FROM historical_raw_messages WHERE ({broad_clause}) AND {mixed_clause} "
        "ORDER BY internal_id ASC LIMIT ?",
        [*broad_params, *mixed_params, MAX_EXAMPLES],
    ).fetchall()
    return [_short(row["body_raw"]) for row in rows]


def _unknown_leaning_examples(conn: sqlite3.Connection, template_id: str) -> list[str]:
    """Broad-term matches that fail every specific rule keyword combo for this concept."""
    group = next((g for g in GROUPS if g.template_id == template_id), None)
    broad_terms = BROAD_TERMS.get(template_id, [])
    if group is None or not broad_terms:
        return []
    broad_clause, broad_params = _like_clause("body_raw", broad_terms)
    where_sql = f"({broad_clause})"
    params = list(broad_params)
    if group.must_contain_all:
        for term in group.must_contain_all:
            where_sql += " AND body_raw NOT LIKE ?"
            params.append(f"%{term}%")
    elif group.must_contain_any:
        clause, term_params = _like_clause("body_raw", group.must_contain_any, negate=True)
        where_sql += f" AND {clause}"
        params.extend(term_params)
    rows = conn.execute(
        f"SELECT body_raw FROM historical_raw_messages WHERE {where_sql} "
        "ORDER BY internal_id ASC LIMIT ?",
        [*params, MAX_EXAMPLES],
    ).fetchall()
    return [_short(row["body_raw"]) for row in rows]


def analyze(db_path: Path) -> dict:
    conn = _open_readonly(db_path)
    try:
        (total,) = conn.execute("SELECT COUNT(*) FROM historical_raw_messages").fetchone()
        report: dict = {"total_records": total, "templates": []}
        for group in GROUPS:
            where_parts: list[str] = []
            params: list[str] = []
            if group.must_contain_all:
                for term in group.must_contain_all:
                    where_parts.append("body_raw LIKE ?")
                    params.append(f"%{term}%")
            if group.must_contain_any:
                clause, term_params = _like_clause("body_raw", group.must_contain_any)
                where_parts.append(clause)
                params.extend(term_params)
            if group.must_not_contain_any:
                clause, term_params = _like_clause(
                    "body_raw", group.must_not_contain_any, negate=True
                )
                where_parts.append(clause)
                params.extend(term_params)
            where_sql = " AND ".join(where_parts) if where_parts else "1=1"

            count, examples = _count_and_examples(conn, where_sql, params)
            variations = _wording_variation_counts(conn, where_sql, params)
            field_evidence = _sample_field_evidence(examples)
            ambiguous = _ambiguous_examples(conn, group.template_id)
            unknown_leaning = _unknown_leaning_examples(conn, group.template_id)

            report["templates"].append(
                {
                    "template_id": group.template_id,
                    "description": group.description,
                    "search_terms": {
                        "must_contain_all": group.must_contain_all,
                        "must_contain_any": group.must_contain_any,
                        "must_not_contain_any": group.must_not_contain_any,
                    },
                    "candidate_count": count,
                    "examples": examples,
                    "wording_variation_counts": variations,
                    "field_evidence_in_sample": field_evidence,
                    "ambiguous_or_conflicting_examples": ambiguous,
                    "unknown_leaning_examples": unknown_leaning,
                    "notes": group.notes,
                }
            )
        return report
    finally:
        conn.close()


def _render_markdown(report: dict) -> str:
    lines = [
        "# Template Pattern Analysis (Historical Reference Only)",
        "",
        "Generated by `python -m app.commands.analyze_template_patterns` against the "
        f"read-only historical archive (`data/history_raw.db`, {report['total_records']:,} "
        "nationwide raw records).",
        "",
        "## Scope and caveats",
        "",
        "- These 5,005 records are **nationwide** disaster messages, not Seoul-only, and "
        "are **not** the production input source. Production input is the existing "
        "Seoul SafeCity real-time collector, which is already Seoul-related by "
        "construction — no Seoul/non-Seoul classifier is built anywhere in this project.",
        "- Keyword matches below are **candidate examples only**, never ground-truth "
        "labels. No message here was manually labeled with a correct template.",
        "- This analysis exists solely to sanity-check the deterministic rules in "
        "`app/template_rules.py` and extractors in `app/template_extractors.py` against "
        "real wording variety, and to flag phrases that should stay `UNKNOWN`.",
        "- The historical database is opened read-only (`mode=ro`) and is never modified "
        "by this command. No full-dataset export is produced.",
        "",
    ]
    for entry in report["templates"]:
        lines.append(f"## {entry['template_id']}")
        lines.append("")
        lines.append(f"{entry['description']}")
        lines.append("")
        terms = entry["search_terms"]
        term_bits = []
        if terms["must_contain_all"]:
            term_bits.append("all of " + ", ".join(f"`{t}`" for t in terms["must_contain_all"]))
        if terms["must_contain_any"]:
            term_bits.append("any of " + ", ".join(f"`{t}`" for t in terms["must_contain_any"]))
        if terms["must_not_contain_any"]:
            term_bits.append("none of " + ", ".join(f"`{t}`" for t in terms["must_not_contain_any"]))
        lines.append("**Search terms**: " + "; ".join(term_bits))
        lines.append("")
        lines.append(f"**Historical candidate count**: {entry['candidate_count']}")
        lines.append("")
        lines.append(
            "**Wording variation counts** (within candidates): "
            + (", ".join(f"{k}={v}" for k, v in entry["wording_variation_counts"].items()) or "(none)")
        )
        lines.append("")
        lines.append(
            "**Field evidence in sampled examples**: "
            + ", ".join(f"{k}={v}/{len(entry['examples'])}" for k, v in entry["field_evidence_in_sample"].items())
        )
        lines.append("")
        lines.append(f"**Proposed rule phrases**: {entry['notes']}")
        lines.append("")
        lines.append(f"**Representative examples** (up to {MAX_EXAMPLES}):")
        if entry["examples"]:
            for ex in entry["examples"]:
                lines.append(f"- {ex}")
        else:
            lines.append("- (none found in historical archive)")
        lines.append("")
        lines.append("**Ambiguous / conflicting examples** (mixed 발효+해제 wording):")
        if entry["ambiguous_or_conflicting_examples"]:
            for ex in entry["ambiguous_or_conflicting_examples"]:
                lines.append(f"- {ex}")
        else:
            lines.append("- (none found)")
        lines.append("")
        lines.append("**Examples that should remain UNKNOWN** (broad term match, specific rule fails):")
        if entry["unknown_leaning_examples"]:
            for ex in entry["unknown_leaning_examples"]:
                lines.append(f"- {ex}")
        else:
            lines.append("- (none found)")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    try:
        report = analyze(settings.history_database_path)
    except FileNotFoundError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    project_root = settings.history_database_path.resolve().parent.parent
    docs_path = project_root / "docs" / "template_pattern_analysis.md"
    docs_path.parent.mkdir(parents=True, exist_ok=True)
    docs_path.write_text(_render_markdown(report), encoding="utf-8")

    summary_path = project_root / "artifacts" / "template_analysis" / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "total_records": report["total_records"],
        "templates": [
            {
                "template_id": t["template_id"],
                "candidate_count": t["candidate_count"],
                "wording_variation_counts": t["wording_variation_counts"],
            }
            for t in report["templates"]
        ],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"total historical records : {report['total_records']:,}")
    for entry in report["templates"]:
        print(f"  {entry['template_id']:<32} candidates={entry['candidate_count']}")
    print(f"wrote {docs_path}")
    print(f"wrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Load `config/message_templates.yaml` and render a template's fixed text.

No Jinja2, no string-eval — plain literal `{slot_name}` substitution, plus
one small conditional-block marker `{#slot}...{/slot}` (see
`_apply_conditional_blocks`) so an optional heading + value can be omitted
as a complete unit rather than leaving a naked heading behind.

Runtime source of truth is the generated YAML (Excel is human-managed and
synchronized offline via `app.commands.sync_templates_from_excel`). This
module only fills placeholders and validates that every required one
resolved — it never rewrites safety wording.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.config import PROJECT_ROOT
from app.template_extractors import SlotValue
from app.template_ids import (
    LEGACY_ALIAS_TO_CANONICAL,
    LEGACY_HIDDEN_TEMPLATE_IDS,
    SYSTEM_TEMPLATE_IDS,
)
from app.template_ids import resolve_template_id as resolve_alias

DEFAULT_TEMPLATES_PATH = PROJECT_ROOT / "config" / "message_templates.yaml"

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
_BLOCK_RE = re.compile(r"\{#(\w+)\}(.*?)\{/\1\}", re.DOTALL)


@dataclass
class TemplateDef:
    id: str
    display_name: str
    button_label: str
    enabled: bool
    required_slots: list[str]
    optional_slots: list[str]
    text: str
    category_code: str = ""
    disaster_type: str = ""
    level: str = ""
    state: str = ""
    section: str = ""
    trigger_condition: str = ""
    review_status: str = "confirmed"
    review_label: str = ""
    automation: bool = True
    aliases: list[str] = field(default_factory=list)
    menu_order: int = 0
    legacy: bool = False
    hidden: bool = False
    system: bool = False


@dataclass
class RenderResult:
    template_id: str
    success: bool
    rendered_text: str | None = None
    missing_slots: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)


_cache: dict[Path, dict[str, TemplateDef]] = {}
_alias_index: dict[Path, dict[str, str]] = {}


def clear_template_cache() -> None:
    _cache.clear()
    _alias_index.clear()


def resolve_template_id(template_id: str, config_path: Path | None = None) -> str:
    """Resolve a legacy alias (or pass-through) to the registry's canonical id."""
    path = config_path or DEFAULT_TEMPLATES_PATH
    _load_all(path)
    aliased = _alias_index.get(path, {}).get(template_id)
    if aliased is not None:
        return aliased
    return resolve_alias(template_id)


def _entry_to_def(entry: dict, *, system: bool = False, legacy: bool = False) -> TemplateDef:
    tid = entry["id"]
    is_system = system or tid in SYSTEM_TEMPLATE_IDS
    is_legacy = legacy or tid in LEGACY_HIDDEN_TEMPLATE_IDS
    default_automation = not is_system and not is_legacy
    return TemplateDef(
        id=tid,
        display_name=entry.get("display_name", tid),
        button_label=entry.get("button_label", ""),
        enabled=bool(entry.get("enabled", True)),
        required_slots=list(entry.get("required_slots") or []),
        optional_slots=list(entry.get("optional_slots") or []),
        text=entry.get("text") or "",
        category_code=entry.get("category_code", ""),
        disaster_type=entry.get("disaster_type", ""),
        level=entry.get("level", ""),
        state=entry.get("state", ""),
        section=entry.get("section", ""),
        trigger_condition=entry.get("trigger_condition", ""),
        review_status=entry.get("review_status", "confirmed"),
        review_label=entry.get("review_label", ""),
        automation=bool(entry.get("automation", default_automation)),
        aliases=list(entry.get("aliases") or []),
        menu_order=int(entry.get("menu_order") or 0),
        legacy=bool(entry.get("legacy", is_legacy)),
        hidden=bool(entry.get("hidden", is_legacy or (is_system and tid == "UNKNOWN"))),
        system=is_system,
    )


def _load_all(config_path: Path) -> dict[str, TemplateDef]:
    cached = _cache.get(config_path)
    if cached is not None:
        return cached

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    templates: dict[str, TemplateDef] = {}
    alias_map: dict[str, str] = dict(LEGACY_ALIAS_TO_CANONICAL)

    for entry in raw.get("templates", []) or []:
        template = _entry_to_def(entry)
        templates[template.id] = template
        for alias in template.aliases:
            alias_map[alias] = template.id

    for entry in raw.get("system_templates", []) or []:
        template = _entry_to_def(entry, system=True)
        templates[template.id] = template
        for alias in template.aliases:
            alias_map[alias] = template.id

    for entry in raw.get("legacy_templates", []) or []:
        template = _entry_to_def(entry, legacy=True)
        templates[template.id] = template
        for alias in template.aliases:
            alias_map[alias] = template.id

    _cache[config_path] = templates
    _alias_index[config_path] = alias_map
    return templates


def load_templates(config_path: Path | None = None) -> dict[str, TemplateDef]:
    return _load_all(config_path or DEFAULT_TEMPLATES_PATH)


def get_template(template_id: str, config_path: Path | None = None) -> TemplateDef | None:
    path = config_path or DEFAULT_TEMPLATES_PATH
    templates = load_templates(path)
    canonical = resolve_template_id(template_id, path)
    return templates.get(canonical) or templates.get(template_id)


def automation_templates(
    *, category_code: str | None = None, config_path: Path | None = None
) -> list[TemplateDef]:
    """Enabled automation business templates, optionally filtered by category."""
    templates = load_templates(config_path)
    rows = [
        t
        for t in templates.values()
        if t.automation and t.enabled and not t.system and not t.legacy and not t.hidden
    ]
    if category_code is not None:
        rows = [t for t in rows if t.category_code == category_code]
    rows.sort(key=lambda t: (t.menu_order, t.id))
    return rows


def category_codes(config_path: Path | None = None) -> list[str]:
    order = ("HW", "HT", "TN", "FL")
    present = {t.category_code for t in automation_templates(config_path=config_path)}
    return [code for code in order if code in present]


def _squeeze_blank_lines(text: str) -> str:
    """Collapse 3+ consecutive newlines to exactly 2 (one blank line), so an
    omitted optional block never leaves a stack of empty lines behind."""
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n")


def _apply_conditional_blocks(text: str, slots: dict[str, SlotValue]) -> str:
    """Drop each `{#slot}...{/slot}` span entirely when `slot` is missing or
    empty; otherwise keep the inner text (markers stripped) so its own
    `{slot}` placeholder still resolves normally afterward."""

    def _replace(match: re.Match[str]) -> str:
        slot_name, inner = match.group(1), match.group(2)
        value = slots.get(slot_name)
        if value is None or not (value.value or "").strip():
            return ""
        return inner

    return _BLOCK_RE.sub(_replace, text)


def render_template(
    template_id: str,
    slots: dict[str, SlotValue],
    config_path: Path | None = None,
) -> RenderResult:
    template = get_template(template_id, config_path)
    if template is None:
        return RenderResult(
            template_id=template_id,
            success=False,
            validation_errors=[f"unknown template_id: {template_id!r}"],
        )

    canonical_id = template.id
    working_text = _apply_conditional_blocks(template.text, slots)

    referenced = set(_PLACEHOLDER_RE.findall(working_text))
    referenced = {tok for tok in referenced if not tok.startswith("#") and not tok.startswith("/")}
    known = set(template.required_slots) | set(template.optional_slots)
    unknown_tokens = sorted(referenced - known)
    if unknown_tokens:
        return RenderResult(
            template_id=canonical_id,
            success=False,
            validation_errors=[
                f"template text references undeclared slot(s): {', '.join(unknown_tokens)}"
            ],
        )

    missing_required = [
        slot
        for slot in template.required_slots
        if slot not in slots or not (slots[slot].value or "").strip()
    ]
    if missing_required:
        return RenderResult(
            template_id=canonical_id,
            success=False,
            missing_slots=missing_required,
            validation_errors=[f"missing required slot(s): {', '.join(missing_required)}"],
        )

    rendered = working_text
    for token in referenced:
        value = slots[token].value if token in slots else ""
        rendered = rendered.replace(f"{{{token}}}", value)

    return RenderResult(
        template_id=canonical_id,
        success=True,
        rendered_text=_squeeze_blank_lines(rendered),
    )

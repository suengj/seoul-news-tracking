"""Load `config/message_templates.yaml` and render a template's fixed text.

No Jinja2, no string-eval — plain literal `{slot_name}` substitution. The
YAML file is the human-managed source of truth for wording; this module only
fills in placeholders and validates that every required one resolved. It
never rewrites, summarizes, or otherwise alters the template author's wording
or the extracted slot values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.config import PROJECT_ROOT
from app.template_extractors import SlotValue

DEFAULT_TEMPLATES_PATH = PROJECT_ROOT / "config" / "message_templates.yaml"

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


@dataclass
class TemplateDef:
    id: str
    display_name: str
    button_label: str
    enabled: bool
    required_slots: list[str]
    optional_slots: list[str]
    text: str


@dataclass
class RenderResult:
    template_id: str
    success: bool
    rendered_text: str | None = None
    missing_slots: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)


_cache: dict[Path, dict[str, TemplateDef]] = {}


def _load_all(config_path: Path) -> dict[str, TemplateDef]:
    cached = _cache.get(config_path)
    if cached is not None:
        return cached

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    templates: dict[str, TemplateDef] = {}
    for entry in raw.get("templates", []):
        template = TemplateDef(
            id=entry["id"],
            display_name=entry.get("display_name", entry["id"]),
            button_label=entry.get("button_label", ""),
            enabled=bool(entry.get("enabled", True)),
            required_slots=list(entry.get("required_slots") or []),
            optional_slots=list(entry.get("optional_slots") or []),
            text=entry.get("text") or "",
        )
        templates[template.id] = template
    _cache[config_path] = templates
    return templates


def load_templates(config_path: Path | None = None) -> dict[str, TemplateDef]:
    return _load_all(config_path or DEFAULT_TEMPLATES_PATH)


def get_template(template_id: str, config_path: Path | None = None) -> TemplateDef | None:
    return load_templates(config_path).get(template_id)


def _squeeze_blank_lines(text: str) -> str:
    """Collapse 3+ consecutive newlines to exactly 2 (one blank line), so an
    omitted optional block never leaves a stack of empty lines behind."""
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n")


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

    referenced = set(_PLACEHOLDER_RE.findall(template.text))
    known = set(template.required_slots) | set(template.optional_slots)
    unknown_tokens = sorted(referenced - known)
    if unknown_tokens:
        return RenderResult(
            template_id=template_id,
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
            template_id=template_id,
            success=False,
            missing_slots=missing_required,
            validation_errors=[f"missing required slot(s): {', '.join(missing_required)}"],
        )

    rendered = template.text
    for token in referenced:
        value = slots[token].value if token in slots else ""
        rendered = rendered.replace(f"{{{token}}}", value)

    return RenderResult(
        template_id=template_id,
        success=True,
        rendered_text=_squeeze_blank_lines(rendered),
    )

"""Inactive placeholder for a future AI fallback. Not called anywhere.

AI may later run only when the deterministic rule result is `UNKNOWN` or a
required slot is missing after `app.template_rules` / `app.template_extractors`
have already tried. Any future implementation must:

- return structured JSON only (template_id, slots, confidence) — no free text
- attach an `evidence` excerpt from the original message to every extracted
  value, exactly like `app.template_extractors.SlotValue`
- have its output validated (required slots, no unresolved placeholders)
  through `app.template_renderer` before ever being used
- never post or render the final official Telegram message directly — a
  human or the existing rule/renderer path always mediates delivery

No AI package is installed and no external API is called from this module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def suggest_template(message_text: str, sender_or_region: str, sent_at: datetime) -> Any:
    raise NotImplementedError("ai_fallback.suggest_template is an inactive future placeholder")


def extract_slots(template_id: str, message_text: str, sender_or_region: str, sent_at: datetime) -> Any:
    raise NotImplementedError("ai_fallback.extract_slots is an inactive future placeholder")

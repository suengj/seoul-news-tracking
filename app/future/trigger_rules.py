"""Future work: event-trigger / classification rules.

Expected responsibility (not implemented in Part 1): classify incoming
records (e.g. 폭염/호우/홍수/열대야) and decide which downstream action a
record should trigger. Part 1 forwards every new record without
classification or filtering and must not call anything in this module.
"""

from __future__ import annotations

from typing import Protocol

from app.models import DisasterMessageRecord


class TriggerRule(Protocol):
    def matches(self, record: DisasterMessageRecord) -> bool:
        ...


def evaluate_triggers(record: DisasterMessageRecord) -> list[str]:
    # TODO(part2+): implement disaster-category classification and trigger matching.
    raise NotImplementedError("trigger_rules is a Part 2+ placeholder")

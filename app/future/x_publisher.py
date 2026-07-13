"""Future work: X (Twitter) publishing.

Expected responsibility (not implemented in Part 1): publish an approved,
formatted record to X via the X API. Part 1 has no X dependency at all and
must not call anything in this module.
"""

from __future__ import annotations

from typing import Protocol

from app.models import DisasterMessageRecord


class XPublisher(Protocol):
    def publish(self, record: DisasterMessageRecord, formatted_text: str) -> str:
        ...


def publish_to_x(record: DisasterMessageRecord, formatted_text: str) -> str:
    # TODO(part2+): implement X API posting once approval_workflow exists.
    raise NotImplementedError("x_publisher is a Part 2+ placeholder")

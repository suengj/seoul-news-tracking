"""Future work: automatic message formatting/rewriting.

Expected responsibility (not implemented in Part 1): take a
DisasterMessageRecord and produce an alternate, human-friendlier or
platform-specific rendering (e.g. a shortened X-post variant) without
altering the stored `original_body`. Part 1 forwards `original_body`
verbatim to Telegram and must not call anything in this module.
"""

from __future__ import annotations

from typing import Protocol

from app.models import DisasterMessageRecord


class MessageFormatter(Protocol):
    def format(self, record: DisasterMessageRecord) -> str:
        ...


def format_for_future_channel(record: DisasterMessageRecord) -> str:
    # TODO(part2+): implement platform-specific formatting/rewriting.
    raise NotImplementedError("message_formatter is a Part 2+ placeholder")

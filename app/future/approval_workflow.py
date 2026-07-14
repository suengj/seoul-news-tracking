"""Future work: Telegram approve/reject workflow.

Expected responsibility (not implemented in Part 1): use
TELEGRAM_ALLOWED_USER_IDS to gate an approve/reject flow for outgoing posts
before they go anywhere beyond Telegram (e.g. before X publishing). Part 1
only does outgoing delivery to one configured chat and must not call
anything in this module.
"""

from __future__ import annotations

from typing import Protocol

from app.models import DisasterMessageRecord


class ApprovalWorkflow(Protocol):
    def request_approval(self, record: DisasterMessageRecord) -> None: ...

    def handle_response(self, user_id: int, approved: bool) -> None: ...


def request_approval(record: DisasterMessageRecord, allowed_user_ids: tuple[int, ...]) -> None:
    # TODO(part2+): implement approve/reject via Telegram callback buttons.
    raise NotImplementedError("approval_workflow is a Part 2+ placeholder")

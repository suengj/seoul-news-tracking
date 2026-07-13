"""Future work: production scheduling / deployment (Cloudflare, VPS, cron).

Expected responsibility (not implemented in Part 1): run `poll_once`
on a recurring schedule in a hosted environment, with process supervision
and alerting. Part 1 is invoked manually/by an external one-off scheduler
of the operator's choosing and must not call anything in this module.
"""

from __future__ import annotations


def run_scheduled_polling() -> None:
    # TODO(part2+): implement recurring scheduled execution (cron/VPS/Cloudflare Worker).
    raise NotImplementedError("scheduler is a Part 2+ placeholder")

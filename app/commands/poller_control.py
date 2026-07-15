"""Local administrative control for the shared SafeCity poller.

This is the ONLY explicit control over the shared collector's persisted
`system_state.polling_enabled` flag. It exists because, before v0.4.0, the
Telegram `/pause` and `/resume` commands toggled that shared flag; since
v0.4.0 those Telegram commands are personal mute/unmute aliases and no longer
touch the shared collector. A database migrated from an older version can
therefore still carry `polling_enabled = 0` from a legacy Telegram `/pause`,
silently stopping collection for everyone while operators remain correctly
subscribed. This command lets a local administrator inspect and, if needed,
re-enable the shared poller.

Usage:
    python -m app.commands.poller_control status
    python -m app.commands.poller_control resume
    python -m app.commands.poller_control pause

This is a LOCAL command run on the host, not a Telegram command — it is
deliberately NOT exposed through the Telegram bot or `/help`. `pause`/`resume`
here manage the shared collector for everyone; they are NOT a personal
Telegram mute (use `/mute` in a private chat for that). Control actions are
recorded in run_history under the non-user administrative actor id 0.

Reads/writes only the local SQLite database — no Telegram, OpenAI, or Seoul
SafeCity network access, and no Telegram configuration is required.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from app.config import load_settings
from app.database import SEOUL_TZ, Database, SystemState, open_database
from app.logging_config import configure_logging

# Non-user administrative actor marker recorded for local control events, so
# an operator's real Telegram user id is never used or stored for these.
ADMIN_ACTOR_USER_ID = 0


def _health_label(state: SystemState, *, stale_after_minutes: int, now: datetime) -> str:
    """Derive a single, identifier-free health label.

    - disabled: the shared collector is paused (polling_enabled = 0)
    - stale:    enabled but no successful poll within stale_after_minutes
                (or none ever recorded) — the process may be stuck/not running
    - healthy:  enabled and a successful poll landed within the window
    """
    if not state.polling_enabled:
        return "disabled"
    last = state.last_successful_poll_at
    if last is None:
        return "stale"
    age_minutes = (now - last).total_seconds() / 60.0
    return "healthy" if age_minutes <= stale_after_minutes else "stale"


def cmd_status(db: Database, *, stale_after_minutes: int, now: datetime | None = None) -> int:
    """Print the shared collector state. Read-only; never modifies the DB.

    Prints only non-sensitive fields — no bot token, API key, or any Telegram
    user/chat identifier (paused_by/resumed_by are intentionally omitted)."""
    now = now or datetime.now(tz=SEOUL_TZ)
    state = db.get_system_state()
    health = _health_label(state, stale_after_minutes=stale_after_minutes, now=now)
    print("shared SafeCity poller state:")
    print(f"  polling_enabled        : {state.polling_enabled}")
    print(f"  last_successful_poll_at: {_fmt(state.last_successful_poll_at)}")
    print(f"  last_new_message_at    : {_fmt(state.last_new_message_at)}")
    print(f"  last_poll_error        : {state.last_poll_error or '(none)'}")
    print(f"  health                 : {health}")
    return 0


def cmd_resume(db: Database, *, now: datetime | None = None) -> int:
    """Idempotently enable the shared collector (local admin action).

    Preserves historical pause metadata (paused_at/paused_by are left intact);
    resume_polling records resumed_at and the admin actor marker."""
    now = now or datetime.now(tz=SEOUL_TZ)
    changed = db.resume_polling(actor_user_id=ADMIN_ACTOR_USER_ID, now=now)
    db.record_control_event(
        "resume", actor_user_id=ADMIN_ACTOR_USER_ID, detail="local poller_control resume"
    )
    if changed:
        print("shared poller RESUMED: polling_enabled changed false -> true.")
    else:
        print("shared poller already enabled: no change (polling_enabled was already true).")
    return 0


def cmd_pause(db: Database, *, now: datetime | None = None) -> int:
    """Idempotently pause the shared collector (local admin action).

    This is a LOCAL administrative command that stops collection for everyone —
    it is NOT a personal Telegram mute (use /mute in a private chat for that)."""
    now = now or datetime.now(tz=SEOUL_TZ)
    changed = db.pause_polling(actor_user_id=ADMIN_ACTOR_USER_ID, now=now)
    db.record_control_event(
        "pause", actor_user_id=ADMIN_ACTOR_USER_ID, detail="local poller_control pause"
    )
    print(
        "this is a LOCAL administrative command (shared collector), not a personal Telegram mute."
    )
    if changed:
        print("shared poller PAUSED: polling_enabled changed true -> false.")
    else:
        print("shared poller already paused: no change (polling_enabled was already false).")
    return 0


def _fmt(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "(never)"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("status", "resume", "pause"),
        help="inspect (status), enable (resume), or disable (pause) the shared SafeCity poller",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)

    with open_database(settings.database_path) as db:
        if args.action == "status":
            return cmd_status(db, stale_after_minutes=settings.status_stale_after_minutes)
        if args.action == "resume":
            return cmd_resume(db)
        return cmd_pause(db)


if __name__ == "__main__":
    raise SystemExit(main())

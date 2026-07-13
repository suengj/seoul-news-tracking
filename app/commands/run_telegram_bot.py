"""Run the Telegram bot: long-poll for inbound commands and reply.

Handles /latest, ordinary text (same as /latest), /status, /pause, /resume,
/help, and an optional dev-only /shutdown (see LOCAL_SHUTDOWN_COMMAND_ENABLED
in .env.example). Refuses to start a second instance for the same bot token
(local file lock; Telegram's own API also rejects a second concurrent
getUpdates long-poll with HTTP 409). Stops cleanly on Ctrl+C or SIGTERM.

Usage: python -m app.commands.run_telegram_bot
"""

from __future__ import annotations

import logging
import signal
import sys

from app.config import load_settings
from app.database import Database
from app.logging_config import configure_logging
from app.process_lock import SingleInstanceLock
from app.telegram_bot import TelegramBotRunner

logger = logging.getLogger(__name__)


def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_level)

    if not settings.telegram_configured:
        print("FAILED: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured.", file=sys.stderr)
        return 1
    if not settings.telegram_allowed_user_ids:
        print(
            "WARNING: TELEGRAM_ALLOWED_USER_IDS is empty; no one will be authorized "
            "to use bot commands.",
            file=sys.stderr,
        )

    lock_path = settings.database_path.parent / "run_telegram_bot.lock"
    try:
        lock = SingleInstanceLock(lock_path)
        lock.acquire()
    except RuntimeError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    db = Database(settings.database_path)
    bot = TelegramBotRunner(settings, db)

    def _stop(*_signal_args: object) -> None:
        bot.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        bot.run_forever()
    finally:
        bot.close()
        db.close()
        lock.release()
        logger.info("Telegram bot stopped cleanly")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

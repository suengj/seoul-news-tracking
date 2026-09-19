"""Run the Telegram bot: long-poll for inbound commands and template-flow callbacks.

Handles /latest, ordinary text (same as /latest), /status, /pause, /resume,
/help, an optional dev-only /shutdown, and every inline-button callback
(template selection, preview confirm/cancel/AI) — one process, one
`getUpdates` offset sequence. Refuses to start a second instance for the
same bot token and fence identity (a process-lifetime local file lock;
Telegram's own API also rejects a second concurrent getUpdates long-poll with
HTTP 409). Stops cleanly on Ctrl+C or
SIGTERM.

Usage: python -m app.commands.run_telegram_bot
"""

from __future__ import annotations

import logging
import signal
import sys

from app.config import load_settings
from app.cutover_fence import (
    FENCE_REFUSAL_EXIT_CODE,
    CutoverFenceError,
    CutoverFenceStore,
)
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

    # Keep the historical database-directory lock for same-database callers,
    # and add the authoritative identity lock below.  The latter is the lock
    # that prevents two consumers with different DATABASE_PATH values from
    # sharing one token/fence/host identity.
    legacy_lock_path = settings.database_path.parent / "run_telegram_bot.lock"
    legacy_lock = None
    consumer_lock = None
    try:
        legacy_lock = SingleInstanceLock(legacy_lock_path)
        legacy_lock.acquire()
    except RuntimeError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    try:
        if settings.cutover_fence_path is None:
            raise CutoverFenceError(
                "FENCE_NOT_CONFIGURED",
                "CUTOVER_FENCE_PATH is not configured; refusing to start getUpdates",
            )
        if not settings.cutover_host_id or settings.cutover_host_id == "unconfigured":
            raise CutoverFenceError(
                "HOST_ID_NOT_CONFIGURED",
                "CUTOVER_HOST_ID is not configured; refusing to start getUpdates",
            )
        fence = CutoverFenceStore(
            settings.cutover_fence_path,
            token=settings.telegram_bot_token,
            host_id=settings.cutover_host_id,
        )
        consumer_lock = SingleInstanceLock(fence.consumer_lock_path)
        consumer_lock.acquire()
        fence.claim()
    except CutoverFenceError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        if consumer_lock is not None:
            consumer_lock.release()
        legacy_lock.release()
        return FENCE_REFUSAL_EXIT_CODE
    except ValueError as exc:
        print(
            "FAILED: CUTOVER_FENCE_REFUSED[HOST_ID_MALFORMED]: "
            f"{exc}",
            file=sys.stderr,
        )
        if consumer_lock is not None:
            consumer_lock.release()
        legacy_lock.release()
        return FENCE_REFUSAL_EXIT_CODE
    except OSError as exc:
        print(
            "FAILED: CUTOVER_FENCE_REFUSED[FENCE_FILESYSTEM_UNAVAILABLE]: "
            f"{exc}",
            file=sys.stderr,
        )
        if consumer_lock is not None:
            consumer_lock.release()
        legacy_lock.release()
        return FENCE_REFUSAL_EXIT_CODE
    except RuntimeError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        legacy_lock.release()
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
        consumer_lock.release()
        legacy_lock.release()
        logger.info("Telegram bot stopped cleanly")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

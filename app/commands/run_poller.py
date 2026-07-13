"""Run the local recurring Seoul SafeCity poller.

Polls at POLL_INTERVAL_SECONDS, skips the HTTP request entirely while
polling is paused (via Telegram /pause or a prior run), sends genuinely-new
records to Telegram, and runs retention cleanup no more than once per
CLEANUP_INTERVAL_HOURS. Stops cleanly on Ctrl+C (SIGINT) or SIGTERM.

Usage: python -m app.commands.run_poller
"""

from __future__ import annotations

from app.config import load_settings
from app.logging_config import configure_logging
from app.poller import Poller


def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_level)

    poller = Poller(settings)
    poller.install_signal_handlers()
    poller.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

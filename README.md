# Seoul News Tracking

Collects the 재난문자 (disaster message) records shown in the Seoul SafeCity
dashboard widget, deduplicates them locally, and forwards genuinely new
records — verbatim, unmodified — to a single Telegram chat. A Telegram bot
also answers `/latest`, `/status`, `/pause`, and `/resume` so collection can
be inspected and remotely paused/resumed without shell access.

Scope so far: collection, dedup, plain relay to Telegram, inbound Telegram
commands, local recurring polling, and SQLite retention/cleanup — all for
**local testing**. No classification, rewriting, approval workflow,
Cloudflare/VPS deployment, or X posting. See `docs/source_discovery.md` for
how the data source was investigated and confirmed, `docs/local_runtime.md`
for how the local processes work, `docs/database_retention.md` for
retention/cleanup, and `docs/part1_completion_report.md` for what was
actually run and verified.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then fill in TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / TELEGRAM_ALLOWED_USER_IDS
```

`.env` is never committed (see `.gitignore`). See `docs/telegram_setup.md`
for how to obtain a bot token, chat ID, and your Telegram user ID.

## Commands

```bash
# --- one-shot collection ---

# Read-only: fetch once, report the method and a preview. No DB writes, no sends.
python -m app.commands.discover_source

# First run only: store current records as a non-notifying baseline.
python -m app.commands.establish_baseline

# Preview a poll cycle: no DB writes, no sends.
python -m app.commands.poll_once --dry-run

# Real poll: store new records, send genuinely-new ones to Telegram.
python -m app.commands.poll_once --send

# Testing only: also send baseline records if this run establishes the baseline.
python -m app.commands.poll_once --send --notify-existing

# Controlled Telegram connectivity test (never a real disaster message).
# Requires TELEGRAM_SEND_ENABLED=true in the environment AND --confirm.
python -m app.commands.send_telegram_test --confirm

# --- local continuous runtime (see docs/local_runtime.md) ---

python -m app.commands.run_poller          # recurring poll loop, Ctrl+C to stop
python -m app.commands.run_telegram_bot    # inbound /latest /status /pause /resume, Ctrl+C to stop
python -m app.commands.run_local           # both together, Ctrl+C to stop both

# --- database maintenance (see docs/database_retention.md) ---

python -m app.commands.database_status
python -m app.commands.cleanup_database --dry-run
python -m app.commands.cleanup_database --confirm
```

## Tests

```bash
python -m pytest
```

Tests run entirely against local fixtures and mocked HTTP transports — no
live network calls (a `conftest.py` fixture also structurally prevents any
test from loading the real `.env`). Controlled live fetches/sends were run
separately and are recorded in `docs/part1_completion_report.md`.

## Data model

Each record stores: `internal_id`, `source_id`, `sender_or_region`,
`sent_at` (tz-aware Asia/Seoul), `original_body` (verbatim), `source_url`,
`detected_at`, `raw_hash`, `telegram_status`, `telegram_message_id`, and the
sanitized `raw_payload`. See `app/models.py` and `app/database.py`.

A `system_state` table tracks polling on/off, who paused/resumed and when,
and last-poll health. A `tombstones` table (source_id + raw_hash only, no
message body) prevents re-notification after retention cleanup deletes an
old message. See `docs/database_retention.md`.

## Project layout

```
app/            config, models, collector, parser, database, telegram_sender,
                telegram_bot (inbound), poller (recurring loop), process_lock
app/commands/   CLI entry points (one-shot + long-running + maintenance)
app/future/     inactive placeholders for later work (never imported at runtime)
docs/           source discovery, architecture, local runtime, retention,
                Telegram setup, completion report
artifacts/      small sanitized evidence from source discovery (no secrets)
tests/          fixture-based tests, no live network dependency
```

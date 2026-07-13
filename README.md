# Seoul News Tracking — Part 1

Collects the 재난문자 (disaster message) records shown in the Seoul SafeCity
dashboard widget, deduplicates them locally, and forwards genuinely new
records — verbatim, unmodified — to a single Telegram chat.

Part 1 scope only: collection, dedup, plain relay to Telegram, and a local
history. No classification, rewriting, approval workflow, scheduling,
Cloudflare/VPS deployment, or X posting. See `docs/source_discovery.md` for
how the data source was investigated and confirmed, and
`docs/part1_completion_report.md` for what was actually run and verified.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then fill in TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
```

`.env` is never committed (see `.gitignore`). See `docs/telegram_setup.md`
for how to obtain a bot token and chat ID.

## Commands

```bash
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
```

### Historical backfill (separate dataset)

A one-time, resumable backfill of 10,000 unique raw historical records from
the public archive at `safetydata.go.kr`, stored in its own SQLite database
(`data/history_raw.db`, separate from the real-time store above). No
filtering, classification, or rewriting — raw data only, for later
reprocessing. See `docs/history_source_discovery.md`,
`docs/history_backfill.md`, `docs/history_database.md`, and
`docs/history_collection_report.md`.

```bash
python -m app.commands.inspect_history_source
python -m app.commands.backfill_history --target-count 10000 --delay-seconds 1.5
python -m app.commands.backfill_history --resume
python -m app.commands.backfill_history --status
python -m app.commands.validate_history_db
python -m app.commands.export_history_sample --count 100 --output data/exports/history_sample.jsonl
```

## Tests

```bash
python -m pytest
```

Tests run entirely against local fixtures — no live network calls. A single
controlled live fetch was run separately and is recorded in
`docs/part1_completion_report.md`.

## Data model

Each record stores: `internal_id`, `source_id`, `sender_or_region`,
`sent_at` (tz-aware Asia/Seoul), `original_body` (verbatim), `source_url`,
`detected_at`, `raw_hash`, `telegram_status`, `telegram_message_id`, and the
sanitized `raw_payload`. See `app/models.py` and `app/database.py`.

## Project layout

```
app/            application code (config, models, collector, parser, database, telegram_sender)
app/commands/   CLI entry points
app/future/     inactive placeholders for Part 2+ (never imported by Part 1 runtime)
app/history_*.py  historical backfill collector (separate dataset, see docs/history_*.md)
docs/           source discovery, architecture, Telegram setup, completion report
artifacts/      small sanitized evidence from source discovery (no secrets)
tests/          fixture-based tests, no live network dependency
```

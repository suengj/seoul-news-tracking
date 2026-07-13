# Seoul News Tracking — Service v1

Collects the 재난문자 (disaster message) records shown in the Seoul SafeCity
dashboard widget, deduplicates them locally, and delivers them to Telegram
with a **human-first** template workflow: an operator always picks the
template, previews the rendered draft, and explicitly confirms before
anything counts as a final result. See `docs/service_v1.md` for the full
product description, and `docs/part1_completion_report.md` for what was
actually run and verified.

No automatic template selection, no automatic publishing anywhere, no X
posting, no server/VPS deployment yet (see "Known limitations" below and
`docs/local_runtime.md`).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then fill in TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
```

`.env` is never committed (see `.gitignore`). See `docs/telegram_setup.md`
for how to obtain a bot token and chat ID. On-demand AI extraction
(`AI_ENABLED`/`OPENAI_API_KEY`) is optional and off by default — see
"On-demand AI" below.

## Commands

```bash
# Read-only: fetch once, report the method and a preview. No DB writes, no sends.
python -m app.commands.discover_source

# First run only: store current records as a non-notifying baseline.
python -m app.commands.establish_baseline

# One poll cycle: --dry-run previews only; --send stores + delivers via the
# template-selection flow (see below).
python -m app.commands.poll_once --dry-run
python -m app.commands.poll_once --send

# The full local runtime: one recurring poller (POLL_INTERVAL_SECONDS,
# default 300s) + one unified Telegram bot process, together, until Ctrl+C.
python -m app.commands.run_local

# Or run either piece on its own:
python -m app.commands.run_poller
python -m app.commands.run_telegram_bot

# Database size/health, and manual retention cleanup.
python -m app.commands.database_status
python -m app.commands.cleanup_database --dry-run
python -m app.commands.cleanup_database --confirm

# Controlled Telegram connectivity test (never a real disaster message).
python -m app.commands.send_telegram_test --confirm
```

See `docs/local_runtime.md` for the poller/bot process model, pause/resume,
and SQLite concurrency details.

### Service v1: template selection, preview, confirm/cancel/AI

Every new message `poll_once` sends gets 8 equal-weight inline buttons (the
7 templates + "📄 원문") — **no button is auto-recommended**; a rule engine
still runs but only ever shows as a small secondary hint line. Selecting a
template creates a preview (Rule-extracted, re-run against the original
message every time); the operator then taps ✅ 최종 OK, ↩️ 취소, or (if
enabled) 🤖 AI로 작성. Only ✅ 최종 OK ever writes a `template_decisions` row
— the future automation ground truth. See `docs/service_v1.md` and
`docs/telegram_template_flow.md` for the full flow, and
`docs/template_engine.md` for the deterministic rule/extraction/rendering
logic itself.

```bash
# Read-only wording analysis of the historical archive (never modifies it,
# never exports the full dataset) — informs the rules, is not itself used
# at runtime.
python -m app.commands.analyze_template_patterns

# Manually run the extraction/rendering pipeline against one message.
python -m app.commands.test_template --message-id 42
python -m app.commands.test_template --text "..." --region "서울특별시" \
    --sent-at "2026-07-14T12:00:00+09:00"
```

### On-demand AI (optional, off by default)

`AI_ENABLED=false` by default — the "🤖 AI로 작성" button is hidden entirely
until both `AI_ENABLED=true` and a real `OPENAI_API_KEY` are set. When used,
AI only extracts slots for the template the operator already picked (it
never chooses a template, never rewrites the fixed YAML wording, never
publishes anything) and every result is validated (declared slots only,
evidence required and checked against the original text) before being
rendered through the same deterministic renderer the Rule path uses. See
`docs/service_v1.md`.

### Historical backfill (separate dataset)

A one-time, resumable backfill of 10,000 unique raw historical records from
the public archive at `safetydata.go.kr`, stored in its own SQLite database
(`data/history_raw.db`, separate from the real-time store above, never read
by the live runtime). No filtering, classification, or rewriting — raw data
only. See `docs/history_source_discovery.md`, `docs/history_backfill.md`,
`docs/history_database.md`, and `docs/history_collection_report.md`.

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

Tests run entirely against local fixtures/mocks — no live Telegram,
OpenAI, or Seoul SafeCity network calls. A single controlled live fetch/
send was run separately and is recorded in `docs/part1_completion_report.md`.

## Data model

Core: `messages` (each record's `internal_id`, `source_id`,
`sender_or_region`, `sent_at`, `original_body` verbatim, `source_url`,
`detected_at`, `raw_hash`, `telegram_status`, `telegram_message_id`,
`is_baseline`), `run_history`, `tombstones`, `system_state`. See
`app/models.py` and `app/database.py`.

Service v1 template flow (see `docs/service_v1.md`,
`docs/database_retention.md`): `template_suggestions` (rule-engine hint,
informational only), `template_actions` (one row per selection button
press), `template_previews` (one row per shown preview — the addressable
object confirm/cancel/AI act on), `template_decisions` (one row per
message, `UPSERT`ed only by an explicit ✅ 최종 OK — the authoritative
result), `ai_generations` (one row per on-demand AI attempt, never the API
key). The last three are never pruned by retention cleanup, even after
their source `messages` row is deleted.

## Project layout

```
app/            config, models, collector, parser, database, telegram_sender, poller, telegram_bot, process_lock
app/commands/   CLI entry points
app/future/     inactive placeholders for later work (never imported by the live runtime)
app/history_*.py  historical backfill collector (separate dataset, see docs/history_*.md)
app/template_rules.py, app/template_extractors.py, app/template_renderer.py
                deterministic rule scoring / slot extraction / YAML rendering (docs/template_engine.md)
app/template_flow.py  Service v1 orchestration: selection -> preview -> confirm/cancel/AI
app/ai_client.py      on-demand OpenAI slot extraction (only called from template_flow)
config/message_templates.yaml   the 7 templates + ORIGINAL_ONLY/UNKNOWN (human-managed SSOT)
config/entity_dictionary.yaml   small helper dictionary (e.g. river names) for extraction
docs/           product/architecture docs, source discovery, Telegram setup, completion report
artifacts/      small sanitized evidence from source discovery (no secrets)
tests/          fixture/mock-based tests, no live network dependency
```

## Known limitations

See `docs/service_v1.md` and `docs/local_runtime.md` for the full list.
Notably: no server/VPS deployment yet (long-polling only, single local
SQLite file), live AI extraction is validated with mocks only until a real
`OPENAI_API_KEY` is supplied, and exactly-one-active-preview isn't
hard-enforced (an older preview's buttons remain technically clickable).

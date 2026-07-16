# Seoul News Tracking — Service v1

Collects 재난문자 (disaster message) records for Seoul from the official
행정안전부(MOIS) SafetyData Open API (`DSSP-IF-00247`), with a conditional
국민안전24 HTML fallback used only when the API has a hard failure in the same
poll cycle, deduplicates them locally, and delivers them to Telegram with a
**human-first** template workflow: an operator always picks the template,
previews the rendered draft, and explicitly confirms before anything counts
as a final result. See `docs/service_v1.md` for the full product
description, `docs/live_source_migration_mois_api_plan.md` and
`docs/mois_api_contract_confirmed.md` for the live-source contract, and
`docs/part1_completion_report.md` for what was actually run and verified.

The previous Seoul SafeCity `JSESSIONID`/XHR collector is retired as of
**v0.5.0** — see `docs/source_discovery.md` (marked RETIRED) and
`docs/source_cutover_runbook.md`.

No automatic template selection, no automatic publishing anywhere, no X
posting, no server/VPS deployment yet (see "Known limitations" below and
`docs/local_runtime.md`).

Since **v0.4.0** every authorized operator is an equal, independent entity:
the shared collector (MOIS API / SafeKorea fallback since v0.5.0) and
`messages` DB stay shared, but automatic delivery, commands, previews,
decisions, AI, and mute/subscribe state are fully personal. There is no
primary operator and no default chat — see
`docs/independent_operator_model.md`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then fill in TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS
```

`.env` is never committed (see `.gitignore`). See `docs/telegram_setup.md`
for how to obtain a bot token. On-demand AI extraction
(`AI_ENABLED`/`OPENAI_API_KEY`) is optional and off by default — see
"On-demand AI" below.

`TELEGRAM_ALLOWED_USER_IDS` are the operators; each starts their own personal
automatic delivery simply by messaging the bot in a private chat (or with
`/subscribe`). `TELEGRAM_SEND_ENABLED` is the global master switch for all
automatic delivery. `TELEGRAM_CHAT_ID` is **legacy migration/bootstrap only** —
it is no longer the automatic target and is never an interactive fallback.
Every command and button is private-chat only and always replies to the chat
it came from — see `docs/independent_operator_model.md`,
`docs/service_v1.md`, and `docs/telegram_routing_validation.md`. Current
version: see `docs/versioning.md` (also shown in `/status` and the bot's
startup log).

## Commands

```bash
# Read-only, secret-safe contract inspection (requires SAFETYDATA_SERVICE_KEY
# in .env). Run before any live-source change; never guesses the contract.
python -m app.commands.inspect_mois_api
python -m app.commands.inspect_safekorea_fallback

# Explicit, idempotent source cutover: registers the currently-visible
# MOIS/SafeKorea window as a known baseline so migrating an existing
# database never resends historical messages. Required once before the
# Poller will deliver on a pre-existing (non-empty) database.
python -m app.commands.source_cutover_mois --inspect
python -m app.commands.source_cutover_mois --bootstrap

# First run only (fresh database): store current records as a non-notifying baseline.
python -m app.commands.establish_baseline

# One poll cycle: --dry-run previews only; --send stores + delivers via the
# template-selection flow (see below).
python -m app.commands.poll_once --dry-run
python -m app.commands.poll_once --send

# The full local runtime: one recurring poller (POLL_INTERVAL_SECONDS,
# default 300s) + one unified Telegram bot process, together, until Ctrl+C.
python -m app.commands.run_local

# Offline routing / source-pipeline validation (no network)
python -m app.commands.validate_telegram_behavior
python -m app.commands.validate_source_pipeline

# Template workbook sync
python -m app.commands.sync_templates_from_excel --check
python -m app.commands.sync_templates_from_excel --write

# Or run either piece on its own:
python -m app.commands.run_poller
python -m app.commands.run_telegram_bot

# Local-only admin control of the SHARED collector (MOIS API / SafeKorea
# fallback since v0.5.0). This is the only explicit shared-poller control; it
# is NOT a Telegram command and is not exposed via /help. `pause` here stops
# collection for everyone (not a personal mute). Useful to re-enable a
# collector left paused by a legacy pre-v0.4.0 Telegram /pause after
# migrating an older database, or to pause before a source cutover.
python -m app.commands.poller_control status
python -m app.commands.poller_control resume
python -m app.commands.poller_control pause

# Database size/health, and manual retention cleanup.
python -m app.commands.database_status
python -m app.commands.cleanup_database --dry-run
python -m app.commands.cleanup_database --confirm

# Controlled Telegram connectivity test (never a real disaster message).
python -m app.commands.send_telegram_test --confirm
```

See `docs/local_runtime.md` for the poller/bot process model, personal
mute/subscribe (`/pause` and `/resume` are personal `/mute`/`/unmute`
aliases), and SQLite concurrency details. The shared collector is
controlled only by the local `poller_control` command above — Telegram
commands never start/stop collection for other operators, and `/status` never
exposes who paused the shared collector (v0.4.1). Automatic retry deliveries
are filtered by the current `TELEGRAM_ALLOWED_USER_IDS`, so a removed user is
never retried.

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

Tests run entirely against local fixtures/mocks — no live Telegram, OpenAI,
SafetyData, 국민안전24, or (retired) Seoul SafeCity network calls. A single
controlled live fetch/send was run separately and is recorded in
`docs/part1_completion_report.md`.

## Data model

Core: `messages` (each record's `internal_id`, `source_id`,
`sender_or_region`, `sent_at`, `original_body` verbatim, `source_url`,
`detected_at`, `raw_hash`, `telegram_status`, `telegram_message_id`,
`is_baseline`), `run_history`, `tombstones`, `system_state`. Since v0.4.0
`messages.telegram_status`/`telegram_message_id` are backward-compatible
aggregates derived from `telegram_deliveries`. See `app/models.py` and
`app/database.py`.

Independent operators (v0.4.0, see `docs/independent_operator_model.md`):
`telegram_subscriptions` (one equal, independent subscription per authorized
operator — `active`/`muted`/`unsubscribed`), `telegram_deliveries` (one
per-recipient automatic-delivery row per `(message, subscription)`, the source
of truth for delivery/retry).

Service v1 template flow (see `docs/service_v1.md`,
`docs/database_retention.md`): `template_suggestions` (rule-engine hint,
informational only), `template_actions` (one row per selection button press,
with `interaction_chat_id`), `template_previews` (one row per shown preview —
the addressable object confirm/cancel/AI act on, scoped to the operator+chat),
`template_decisions` (one row per **operator+chat** for a message —
`UNIQUE(message_id, confirmed_by, interaction_chat_id)`, `UPSERT`ed only by an
explicit ✅ 최종 OK — the authoritative result, including an immutable snapshot
of the source message's id/sender-region/sent-time/full body at confirmation
time), `ai_generations` (one row per on-demand AI attempt, with
`interaction_chat_id`, never the API key). The last three are never pruned by
retention cleanup, even after their source `messages` row is deleted — and
`template_decisions`' own snapshot means the full original text is never lost
either.

## Project layout

```
app/            config, models, collector, mois_api, safekorea_fallback, database, telegram_sender, poller, telegram_bot, process_lock
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
SQLite file), and live AI extraction is validated with mocks only until a
real `OPENAI_API_KEY` is supplied. Only the latest preview per
(message, operator) is confirmable/cancellable — selecting a new template,
or a successful AI generation, marks the prior active preview `superseded`;
acting on a stale preview's buttons is rejected, never silently ignored
(see `docs/telegram_template_flow.md`).

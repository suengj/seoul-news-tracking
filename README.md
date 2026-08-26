# Seoul News Tracking

**Monitor Seoul disaster alerts, review them in Telegram, and publish with human approval.**

A local Python service that collects Seoul-targeted 재난문자 (disaster SMS) from official government sources, deduplicates them in SQLite, and delivers them to authorized operators via Telegram. Every outbound message goes through a **human-first** workflow: pick a template, preview, then explicitly confirm.

| | |
|---|---|
| **Version** | 0.5.0 |
| **License** | MIT |
| **Runtime** | Python 3.11+, local machine (no VPS required) |
| **Primary source** | [MOIS SafetyData API](https://www.safetydata.go.kr/disaster-data/view?dataSn=228) (`DSSP-IF-00247`) |
| **Fallback** | 국민안전24 HTML (only on primary API hard failure, same poll cycle) |

---

## What it does

```
Government APIs          Local service              Operators (Telegram)
─────────────────        ─────────────              ──────────────────
MOIS SafetyData API  →   Poll + dedupe (SQLite)  →  Personal alert delivery
SafeKorea HTML (*)   →   Template engine         →  Select → Preview → Confirm
                         Retention + audit log   →  Optional AI slot fill
```

(\*) Fallback runs only when the primary API fails in the same poll cycle — not on empty results.

**Design principles**

- **Human in the loop** — no auto-selected templates, no auto-publishing, no silent confirmations.
- **Independent operators** — each authorized user gets their own delivery, commands, previews, and mute state. No primary/sub-operator model.
- **Deterministic templates** — rule-based scoring and YAML rendering; optional OpenAI slot extraction only when an operator clicks “AI로 작성”.
- **Fail-safe defaults** — `TELEGRAM_SEND_ENABLED=false` by default; `.env` is never committed.

---

## Quick start

### 1. Install

```bash
git clone https://github.com/suengj/seoul-news-tracking.git
cd seoul-news-tracking
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

### 2. Configure `.env`

| Variable | Required | Notes |
|----------|----------|-------|
| `TELEGRAM_BOT_TOKEN` | Yes | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_ALLOWED_USER_IDS` | Yes | Comma-separated Telegram **user** IDs (operators) |
| `SAFETYDATA_SERVICE_KEY` | Yes | [SafetyData Open API](https://www.safetydata.go.kr/) service key |
| `TELEGRAM_SEND_ENABLED` | — | `false` until you are ready to deliver alerts |
| `OPENAI_API_KEY` | — | Optional; only for on-demand AI slot extraction |

See [`docs/telegram_setup.md`](docs/telegram_setup.md) for token and chat ID setup.

### 3. First run

```bash
# Fresh database: register current messages as baseline (no Telegram send)
python -m app.commands.establish_baseline

# Start poller + Telegram bot (default poll interval: 300s)
python -m app.commands.run_local
```

Operators message the bot in a **private chat** to subscribe. Set `TELEGRAM_SEND_ENABLED=true` when you want automatic delivery to active subscriptions.

### 4. Verify

```bash
python -m app.commands.send_telegram_test --confirm   # needs TELEGRAM_SEND_ENABLED=true
python -m pytest
```

---

## Operator workflow

When a new alert arrives:

1. **Receive** — original message text with 8 equal-weight template buttons (7 templates + “원문”).
2. **Select** — tap a template; the system re-extracts slots from the original text.
3. **Preview** — rendered draft with ✅ Confirm / ↩️ Cancel / (optional) 🤖 AI.
4. **Confirm** — only ✅ writes the authoritative `template_decisions` record.

A small “실험적 추천” hint may appear, but it is never a button and never auto-selected.

Details: [`docs/service_v1.md`](docs/service_v1.md) · [`docs/telegram_template_flow.md`](docs/telegram_template_flow.md)

---

## Common commands

| Command | Purpose |
|---------|---------|
| `python -m app.commands.run_local` | Run poller + Telegram bot together |
| `python -m app.commands.poll_once --dry-run` | One poll cycle, no send |
| `python -m app.commands.poll_once --send` | One poll cycle with delivery |
| `python -m app.commands.source_cutover_mois --bootstrap` | Migrate existing DB to MOIS source (once) |
| `python -m app.commands.poller_control status` | Shared collector pause/resume (local CLI only) |
| `python -m app.commands.database_status` | DB size and health |
| `python -m app.commands.sync_templates_from_excel --check` | Validate template workbook → YAML sync |

Full CLI reference: [`docs/local_runtime.md`](docs/local_runtime.md) and `app/commands/`.

### macOS background service (optional)

Copy and edit the launchd example (paths are machine-specific, not in the repo):

```bash
cp scripts/launchd/com.user.seoulnews-runlocal.plist.example \
   scripts/launchd/com.user.seoulnews-runlocal.plist
# edit paths, then install — see plist header comment
```

### Template workbook (local only)

The Seoul City Excel workbook (`templates/서울시_재난특보_X템플릿.xlsx`) is **gitignored** — keep your copy locally. Runtime uses the generated [`config/message_templates.yaml`](config/message_templates.yaml). Sync with:

```bash
python -m app.commands.sync_templates_from_excel --write
```

---

## Project structure

```
app/
  mois_api.py, safekorea_fallback.py   # Live data sources (v0.5.0)
  poller.py, telegram_bot.py           # Recurring collection + bot
  template_flow.py                     # Select → preview → confirm flow
  database.py, models.py               # SQLite persistence
app/commands/                          # CLI entry points
config/
  message_templates.yaml               # Runtime template definitions
  entity_dictionary.yaml               # Extraction helpers
docs/                                  # Architecture, setup, runbooks
tests/                                 # Fixture-based tests (no live network)
```

---

## Documentation

| Topic | Doc |
|-------|-----|
| Product flow | [`docs/service_v1.md`](docs/service_v1.md) |
| Local runtime & processes | [`docs/local_runtime.md`](docs/local_runtime.md) |
| Telegram setup | [`docs/telegram_setup.md`](docs/telegram_setup.md) |
| Independent operators | [`docs/independent_operator_model.md`](docs/independent_operator_model.md) |
| MOIS API migration | [`docs/live_source_migration_mois_api_plan.md`](docs/live_source_migration_mois_api_plan.md) |
| Template engine | [`docs/template_engine.md`](docs/template_engine.md) |
| Historical backfill | [`docs/history_backfill.md`](docs/history_backfill.md) |

---

## Known limitations

- **Local only** — long-polling Telegram bot, single SQLite file; no VPS/cloud deployment yet.
- **No auto-publish** — X/Twitter and other channels are placeholders under `app/future/`.
- **Latest preview only** — acting on superseded preview buttons is rejected explicitly.
- **Seoul filter** — only messages targeting Seoul (서울특별시) are collected.

---

## Contributing

Issues and pull requests are welcome. Run `python -m pytest` before submitting. Do not commit `.env`, database files, the local launchd plist, or the Seoul City Excel workbook.

---

## License

MIT — see [LICENSE](LICENSE).

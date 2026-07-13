# Architecture

## Flow

```
 [Seoul SafeCity]
        │  1) GET newsDistDustList.page  (session bootstrap: obtain JSESSIONID)
        │  2) POST /disstr/selectDisstrSms.do  (X-Requested-With: XMLHttpRequest)
        ▼
 app/collector.py  ──►  app/parser.py  (validate schema, parse full-text records)
        │
        ▼
 app/database.py  (SQLite: dedup by source_id/raw_hash/tombstone; history; system_state)
        │
        ▼  (new, non-baseline records only, when send is enabled)
 app/telegram_sender.py  ──►  Telegram Bot API sendMessage

 [Telegram user]
        │  /latest /status /pause /resume /help
        ▼
 app/telegram_bot.py  (long-poll getUpdates, authorize, dispatch)
        │                       │
        ▼                       ▼
 app/database.py (read)   app/database.py (system_state: pause/resume)
```

One-shot CLI commands (`discover_source`, `establish_baseline`, `poll_once`,
`send_telegram_test`, `database_status`, `cleanup_database`) each run once
and exit. Two long-running local processes layer on top for continuous
local testing: `app/poller.py` (recurring collect/dedup/send loop, pause-
aware) and `app/telegram_bot.py` (inbound command handling) — see
`docs/local_runtime.md`. There is still no production scheduler/VPS/
Cloudflare deployment (deferred — see `app/future/scheduler.py`).

## Modules

- `app/config.py` — loads `.env`/environment into a typed `Settings`.
  Never logs secret values.
- `app/models.py` — `DisasterMessageRecord` (the normalized schema) and
  `TelegramStatus` (`collected` → `telegram_pending`/`telegram_sent`/
  `telegram_failed`).
- `app/collector.py` — the only module that talks to Seoul SafeCity. Owns
  the two-request session-bootstrap + JSON-fetch flow documented in
  `docs/source_discovery.md`, plus timeouts, limited retries, and hard
  failure on 403/429/challenge pages/schema drift.
- `app/parser.py` — validates the JSON shape and turns each `sms[]` entry
  into a `DisasterMessageRecord`, rejecting anything missing the documented
  full-text field, timestamp, or sender.
- `app/database.py` — SQLite persistence: `messages` (dedup key: `source_id`
  OR `raw_hash`, plus `tombstones` for deleted-but-remembered records),
  `run_history` (collection/delivery history, with noop suppression and
  retention), and `system_state` (pause/resume, last-poll health,
  last-cleanup timestamp — a single-row table). WAL mode, `busy_timeout`,
  and bounded write retries make it safe for two local processes to share.
- `app/telegram_sender.py` — builds the outbound new-alert message format,
  escapes MarkdownV2, splits long messages safely, retries only
  network/429/5xx failures a bounded number of times, and separately
  supports direct inbound-command replies (arbitrary chat id +
  reply-to-message, independent of `TELEGRAM_SEND_ENABLED`).
- `app/telegram_bot.py` — long-polls `getUpdates`, authorizes by Telegram
  user ID against `TELEGRAM_ALLOWED_USER_IDS`, and dispatches
  `/latest`/`/status`/`/pause`/`/resume`/`/help`/dev-only `/shutdown`. Never
  initiates a SafeCity request itself.
- `app/poller.py` — the recurring local collect/dedup/send loop; checks
  `system_state.polling_enabled` every cycle, skips the SafeCity request
  while paused, and triggers retention cleanup on its own schedule.
- `app/process_lock.py` — a small POSIX advisory-file single-instance lock
  used by `run_telegram_bot` and `run_local` to refuse a duplicate worker.
- `app/commands/*` — thin CLI wrappers; see README for usage. All side
  effects (DB writes, Telegram sends) are explicit and gated by flags.
- `app/future/*` — inactive stubs for later responsibilities (AI drafting,
  classification, approval workflow, X publishing, production scheduling).
  A static test (`tests/test_future_placeholders.py`) asserts no runtime
  module imports them.

## Why SQLite, why this dedup strategy

Part 1's scale (a handful of records per poll, infrequent polling) does not
justify a server process or external database. SQLite gives transactional
dedup and a queryable history with zero operational overhead.

`source_id` (the API's `disstrSmsSn`) is the primary dedup key because it is
stable and provided by the source. The SHA-256 `raw_hash` (sender + sent_at +
full body) is stored on every record and checked as an OR-condition so a
future response that ever omits `disstrSmsSn` still dedups correctly, and so
identical content arriving under a different ID (unlikely, but possible if
the source ever reissues a serial) is still caught.

## Why no browser automation in the runtime path

`docs/source_discovery.md` confirms the widget's own JSON endpoint returns
the complete message text with a stable ID, and is reachable with two plain
HTTP requests (one GET for a session cookie, one POST for data). This is
strictly lighter than driving a headless browser and satisfies the
"simplest proven method" requirement. Browser automation remains the
documented fallback if the endpoint ever stops working.

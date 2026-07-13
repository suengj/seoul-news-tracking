# Architecture — Part 1

## Flow

```
 [Seoul SafeCity]
        │  1) GET newsDistDustList.page  (session bootstrap: obtain JSESSIONID)
        │  2) POST /disstr/selectDisstrSms.do  (X-Requested-With: XMLHttpRequest)
        ▼
 app/collector.py  ──►  app/parser.py  (validate schema, parse full-text records)
        │
        ▼
 app/database.py  (SQLite: dedup by source_id, fallback SHA-256 raw_hash; history)
        │
        ▼  (new, non-baseline records only, when --send is passed)
 app/telegram_sender.py  ──►  Telegram Bot API sendMessage
```

Everything above is driven by one-shot CLI commands in `app/commands/`; there
is no long-running process or scheduler in Part 1 (that is explicitly
deferred — see `app/future/scheduler.py`).

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
- `app/database.py` — SQLite persistence: a `messages` table (dedup key:
  `source_id` OR `raw_hash`, both unique-checked) and a `run_history` table
  for basic collection/delivery history.
- `app/telegram_sender.py` — builds the Part 1 message format, escapes
  MarkdownV2, splits long messages safely, and retries only
  network/429/5xx failures a bounded number of times.
- `app/commands/*` — thin CLI wrappers; see README for usage. All side
  effects (DB writes, Telegram sends) are explicit and gated by flags.
- `app/future/*` — inactive stubs for Part 2+ responsibilities. A static
  test (`tests/test_future_placeholders.py`) asserts no Part 1 runtime
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

# Part 1 Completion Report

All items below were actually executed in this environment on 2026-07-13
(KST) against the live Seoul SafeCity site and a real Telegram bot — not
simulated.

## 1. Source discovery

- Target page fetched with plain `curl`, then reproduced with the project's
  own `httpx`-based collector. See `docs/source_discovery.md` for the full
  investigation.
- Confirmed data endpoint: `POST https://safecity.seoul.go.kr/disstr/selectDisstrSms.do`,
  requiring a session cookie from a prior GET plus `X-Requested-With:
  XMLHttpRequest`.
- Confirmed the endpoint returns the complete, untruncated message text
  (`smsMsg`) with a stable ID (`disstrSmsSn`) — no browser automation
  required for normal polling.

## 2. Controlled live fetch (`discover_source`)

```
$ python -m app.commands.discover_source
selected method     : direct_json_endpoint:/disstr/selectDisstrSms.do
records fetched      : 21
full text confirmed  : True
```

Sample record observed (real, public disaster-alert text):

```
[DS00050652] 2026-07-13T11:27:21+09:00 서울특별시 영등포구: 오늘 10:27 국회 인근
화재 발생으로 국회대로↔서강대교 방향 교통혼잡 중이니 차량들은 참고 하시기 바랍니다.
```

## 3. `poll_once --dry-run` on an empty database

```
$ python -m app.commands.poll_once --dry-run
records fetched      : 21
new records          : 21
duplicate records     : 0
records that would be sent to Telegram:
  (none)
dry-run: no database writes, no Telegram sends performed.
```

Verified afterward with direct SQLite inspection: `messages` table had 0
rows, `run_history` had 0 rows — dry-run made no persistent changes.

## 4. Baseline result (`establish_baseline`)

```
$ python -m app.commands.establish_baseline
baseline established: 21 record(s) stored, none sent to Telegram.

$ python -m app.commands.establish_baseline   # run again
database already has records; baseline already established. No changes made.
```

Verified: 21 rows in `messages`, all with `is_baseline = 1` and
`telegram_status = 'collected'` — none marked sent.

## 5. Incremental poll against the real baseline

```
$ python -m app.commands.poll_once --send
records fetched      : 21
new records          : 0
duplicate records     : 21
nothing to send.
sent: 0, failed: 0
```

Confirms dedup correctly recognizes a fully-overlapping poll window against
live data (not just fixtures).

## 6. Telegram test result

Bot: a dedicated bot created for this project (`@SeoulEmergencyAlert_bot`).
Chat ID: the operator's own Telegram user ID (reused from an existing
project's config at the operator's direction, confirmed with them before
use).

First attempt failed — a real bug found by live testing, not anticipated by
the unit tests at the time:

```
$ python -m app.commands.send_telegram_test --confirm
FAILED: HTTP 400: {"ok":false,"error_code":400,"description":"Bad Request:
can't parse entities: Character '-' is reserved and must be escaped with
the preceding '\\'"}
```

Root cause: `app/commands/send_telegram_test.py` interpolated a raw
timestamp string (containing `-` from `YYYY-MM-DD`) into a MarkdownV2
message without escaping it. Fixed by routing the timestamp through
`telegram_sender.escape_markdown_v2` before formatting. A regression test
(`tests/test_send_telegram_test_command.py::test_test_message_template_fully_escaped`)
and a general "no unescaped MarkdownV2 char" checker
(`tests/test_telegram_sender.py::test_build_message_has_no_unescaped_markdown_v2_chars`)
were added so this class of bug fails in CI instead of only in production.

After the fix:

```
$ python -m app.commands.send_telegram_test --confirm
OK: test message sent, message_id(s)=3
```

Separately, end-to-end delivery of a **real** collected record (not a test
payload) was verified against a disposable, temporary database: with the
existing 21-record baseline present, one record was removed from that
temporary DB to simulate it reappearing as "new" on the next poll, then:

```
$ DATABASE_PATH=<temp db> python -m app.commands.poll_once --send
new records          : 1
duplicate records     : 20
records that would be sent to Telegram:
  [DS00050652] ... 서울특별시 영등포구: 오늘 10:27 국회 인근 화재 발생으로 ...
sent: 1, failed: 0
```

Verified in the temporary database: `telegram_status = 'telegram_sent'`,
`telegram_message_id = '4'`. The temporary database was deleted afterward;
the project's real `data/seoul_news.db` baseline was never sent to
Telegram (as intended — baseline records stay unsent by default).

## 7. Test suite

```
$ python -m pytest -q
56 passed in 0.60s

$ ruff check app tests
All checks passed!
```

56 tests, all against local fixtures / mocked HTTP transports — no test
depends on the live network. Covers: full-text extraction, rejection of
truncated/missing fields, timestamp parsing, sender/district parsing,
stable-ID and hash-fallback dedup, overlapping windows, reordering,
baseline behavior, Telegram message construction/escaping/splitting/retry,
disabled-send mode, missing env vars, dry-run behavior, and a static check
that no `app/future/*` placeholder is imported by Part 1 runtime code.

## 8. Known limitations (Part 1)

- DOM-level full-text confirmation (Step 1 of the source-discovery
  requirement) was done by static analysis of the page's own inline
  JavaScript rather than by inspecting a live rendered `document`, because
  Playwright/Chromium was not available in this environment and installing
  it was out of scope for this session. This does not weaken the
  conclusion (see `docs/source_discovery.md` §3), but a future session with
  browser automation available could add a direct confirmation.
- The collection endpoint's WAF requirements (`X-Requested-With` + session
  cookie) are undocumented by Seoul SafeCity and could change without
  notice; `app/collector.py` treats a change here as a hard failure (exit
  non-zero) rather than degrading silently.
- `TELEGRAM_ALLOWED_USER_IDS` parsing is implemented and tested but has no
  runtime effect in Part 1, by design.

---

# Local Runtime Controls Extension — Completion Report

Everything below was executed on 2026-07-13 (KST) in this same environment,
extending Part 1 with inbound Telegram commands, local recurring polling,
retention/cleanup, and SQLite concurrency hardening — still local-testing
only (no VPS/Cloudflare/production scheduler).

## 1. Automated test suite

```
$ python -m pytest -q
158 passed in 3.93s

$ ruff check app tests
All checks passed!
```

158 tests total (56 from Part 1 + 102 new), all against local fixtures and
mocked HTTP transports (`httpx.MockTransport`) — no test touches Seoul
SafeCity, Telegram, or GitHub. A `conftest.py` autouse fixture
(`_never_load_the_real_dotenv`) structurally prevents any test from ever
resolving the developer's real `.env` — this was added *because* a test
without it made a real ~20-minute blocking long-poll call to the live
Telegram API using real production credentials (see §5).

New coverage: `get_latest_record()` ordering + tiebreak, empty-DB reply,
ordinary-text-as-latest, `/latest`/`/status`/`/pause`/`/resume`/`/help`,
repeated pause/resume idempotency, unauthorized rejection (by user ID, not
chat ID), pause-state persistence across DB reopen, poller pause-skip and
resume, getUpdates offset advancement and no-double-processing, malformed/
unsupported update types (edited messages, channel posts, callback
queries, non-text messages, missing chat/from, non-dict payloads), WAL/
busy_timeout/foreign_keys pragmas, two-connection concurrent read/write,
bounded write-lock retry (success and exhaustion), retention range
validation, 30- and 90-day message retention boundaries, cleanup dry-run
(no mutation) and confirmed cleanup, tombstone creation/expiry/no-body-
retention, run-history retention, successful-noop suppression vs. always-
kept cases (failure/new-message/Telegram-failure/control-event),
`database_status` fields, no-secrets-in-status (both Telegram `/status` and
the CLI), signal-handler wiring, and `run_local`'s single-instance lock
plus real-subprocess graceful/forced termination.

## 2. Controlled live validation

Performed against the real `data/seoul_news.db` (already holding the 21
baseline + 1 real record from Part 1 testing) and the real
`@SeoulEmergencyAlert_bot`.

### Live poller (`run_poller`, `POLL_INTERVAL_SECONDS=20` override for
observability)

Started for real and left running. On its very first cycle it found and
sent a **genuine new disaster alert** that had appeared on Seoul SafeCity
since the Part 1 baseline (a real 폭염경보/heat-wave warning, `sent_at`
13:06:54, not a test payload):

```
run_history run_id=3: fetched_count=20, new_count=1, duplicate_count=19, sent_count=1
messages: source_id=DS00050673, telegram_status=telegram_sent, telegram_message_id=5
```

This was an unplanned but legitimate real-world confirmation that the
poller correctly distinguishes genuinely-new records from baseline/
duplicates in continuous operation, not just in a single manual run.

### Live pause/resume against the running poller

`/pause` and `/resume` were exercised by calling
`Database.pause_polling()`/`resume_polling()` directly against the *same*
live `data/seoul_news.db` the running poller was reading every cycle —
procedurally identical to what the Telegram `/pause`/`/resume` handlers do
internally (`app/telegram_bot.py`'s `_handle_pause`/`_handle_resume` call
these exact same methods). Observed in the live poller's own log:

```
13:29:56  polling is paused; skipping Seoul SafeCity request this cycle
13:30:16  polling is paused; skipping Seoul SafeCity request this cycle
13:30:32  polling is paused; skipping Seoul SafeCity request this cycle
[resume_polling() called]
13:31:32  GET newsDistDustList.page ... 200 OK   (SafeCity request resumed)
13:31:32  POST selectDisstrSms.do ... 200 OK
13:31:52  GET newsDistDustList.page ... 200 OK
```

Confirms, live: paused polling makes zero SafeCity requests across
multiple real cycles, and resuming immediately restores real requests on
the very next cycle.

### Live inbound Telegram round trip (partial)

The Telegram bot (`run_telegram_bot`) was started for real and left
long-polling. The operator was asked to send six messages (ordinary text,
`/latest`, `/status`, `/pause`, `/status`, `/resume`) to
`@SeoulEmergencyAlert_bot`. **Only one of the six was confirmed to arrive
and receive a live reply** — Telegram's own `getWebhookInfo` showed
`pending_update_count: 0` for roughly 20 minutes of active checking before
one update finally landed and was dispatched (one `sendMessage` call
logged at 13:28:52, offset correctly advanced from 0 to the new update_id
afterward, and the bot's own process — not manual DB edits — issued the
reply). No `pause`/`resume` `run_history` control-event row was created
during this window, so the one message that did arrive was not `/pause` or
`/resume`.

**Honestly: the full live 6-command Telegram round trip was not
completed** — likely a timing/delivery issue on the Telegram/client side
outside this system's control, not reproduced or debugged further at the
operator's direction. What *is* live-verified: (a) the bot can receive a
real inbound message via long polling, correctly authorize it, and send a
real reply — the core inbound pipeline works end-to-end at least once; (b)
`/latest` and `/status` reply text was independently verified correct by
calling `build_latest_reply`/`build_status_reply` directly against the
real live database (see next section); (c) pause/resume mechanics were
verified live via direct DB calls against the running poller, as described
above. Full multi-command Telegram-originated `/pause` → `/resume`
round-trip remains covered only by the 30 automated `test_telegram_bot.py`
tests, not by this live session.

### Reply formatting against real data — found and fixed a real bug

Calling `build_latest_reply()`/`build_status_reply()` directly against the
live database initially printed timestamps as `2026-07-13 13:06:54
UTC+09:00` instead of the intended `... KST`. Root cause: `Database.
_row_to_record()` parsed stored ISO timestamp strings with `datetime.
fromisoformat()`, which reconstructs a fixed-offset `tzinfo` (no zone
name) rather than the original `ZoneInfo("Asia/Seoul")` — so any record
read back from SQLite (as opposed to freshly parsed from the API) lost its
"KST" zone name on formatting. Fixed by `.astimezone(SEOUL_TZ)` in
`_row_to_record` (same instant, correct zone identity). Verified after the
fix, against the same real record:

```
발송시각: 2026-07-13 13:06:54 KST
수집시각: 2026-07-13 13:16:16 KST
```

A regression test was added:
`tests/test_database_control.py::test_records_read_back_from_db_retain_seoul_zone_name`.

### `database_status` and `cleanup_database --dry-run` (real DB)

```
$ python -m app.commands.database_status
total message rows          : 22
baseline rows                : 21
messages by telegram status  :
  collected            21
  telegram_sent        1
polling enabled              : True
last successful poll         : 2026-07-13 13:32:12...
cleanup eligibility (at current retention settings):
  message rows eligible      : 0
  run-history rows eligible  : 0
  tombstone rows eligible    : 0

$ python -m app.commands.cleanup_database --dry-run
message rows eligible for deletion       : 0
run-history rows eligible for deletion    : 0
tombstone rows eligible for deletion       : 0
```

No destructive cleanup was run against the real database (correctly:
nothing was old enough to be eligible yet under the 90/14/365-day
defaults).

### Clean shutdown

Both live processes were stopped with `SIGTERM` (equivalent to Ctrl+C):
the poller exited immediately; the Telegram bot finished its in-flight
`getUpdates` call and exited within its 25s long-poll timeout, logging
`"Telegram bot stopped cleanly"`. The `run_telegram_bot.lock` file was
confirmed released (a fresh `SingleInstanceLock` could immediately
re-acquire it). No stray processes remained afterward (`ps aux` checked).
Final `system_state.polling_enabled` confirmed `True` (left in the normal
active state).

## 3. Known limitations (this extension)

- Live inbound Telegram testing was only partially completed (one of six
  requested messages confirmed round-tripped) — see §2 above. Recommend
  re-attempting a full live `/pause`→`/resume` round trip in a follow-up
  session with more time for interactive back-and-forth.
- No live test of `run_local` launching both children together (child
  process supervision was verified with real subprocesses in
  `tests/test_run_local_command.py`, and the poller/bot were each verified
  live independently, but not via `run_local` specifically in this
  session).
- Long polling only, as designed for local testing; webhook migration is
  documented but not implemented (`docs/local_runtime.md`).
- SQLite only; D1/PostgreSQL migration is documented but not implemented.

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

## 8. Known limitations

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

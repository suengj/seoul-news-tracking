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
- `TELEGRAM_ALLOWED_USER_IDS` parsing was implemented and tested here with
  no runtime effect yet. It is now load-bearing (see §9 below) as the
  authorization check for every Telegram command and every inline-button
  callback.

## 9. Service v1: integrated local runtime + human-first template flow (added 2026-07-14)

Integrates two previously-separate, unreconciled branches
(`feature/local-runtime-controls`: recurring poller, unified command bot,
SQLite hardening, retention; `feature/template-inline-v1`: YAML templates,
deterministic rule/extraction/rendering engine) into one
`feature/service-v1-manual-template-flow` branch, and changes the product
shape to human-first: an operator always picks the template, previews it,
and explicitly confirms — nothing is auto-selected or auto-published. Full
detail in `docs/service_v1.md`, `docs/telegram_template_flow.md`,
`docs/template_engine.md`, and `docs/database_retention.md`.

Summary of what changed:

- `app/config.py`, `app/database.py`: merged `Settings`/`Database` (WAL,
  `busy_timeout`, `foreign_keys=ON`, retry-on-locked writes, retention
  cleanup, `system_state`) plus five new/ported tables:
  `template_suggestions`, `template_actions`, `template_previews`,
  `template_decisions`, `ai_generations`, and a generalized
  `processed_callback_queries` duplicate-callback guard. `POLL_INTERVAL_SECONDS`
  default changed 60s → 300s, `STATUS_STALE_AFTER_MINUTES` 5 → 15.
- `app/poller.py`, `app/process_lock.py`, `app/commands/run_poller.py`,
  `app/commands/run_local.py`: the recurring poller + single-instance
  locking + combined local runner, ported from the local-runtime branch.
- `app/telegram_bot.py`: one unified `TelegramBotRunner` — one `getUpdates`
  offset sequence handles both `/latest /status /pause /resume /help` and
  every inline-button callback (routed to `app/template_flow.py`). The
  previously-separate, callback-only bot process was retired.
- `app/template_flow.py` (new): the actual select → preview →
  confirm/cancel/AI state machine and duplicate-callback guard.
- `app/ai_client.py` (new, replaces the inactive `app/ai_fallback.py`
  stub): on-demand OpenAI structured-output slot extraction, called only
  from an explicit "AI로 작성" tap; validates template_id/declared
  slots/evidence before ever reaching the renderer.
- `app/telegram_sender.py`: consolidated the MarkdownV2 command-reply path
  and the plain-text template-flow path onto one retry-capable `_send`
  core; new Service v1 message/keyboard builders (8-button selector, no
  auto-recommended button; preview/confirm/cancel/incomplete message
  formats; `preview:{id}:confirm|cancel|ai` callback data).
- `app/template_rules.py`: `HEATWAVE_UPGRADED` now requires **both**
  폭염주의보 and 폭염경보 (plus a transition word), not just the warning —
  a flagged review fix.
- `app/template_renderer.py`: added a minimal `{#slot}...{/slot}`
  conditional-block marker so an omitted optional value drops its heading
  too — fixes `HEAVY_RAIN_MULTI_LEVEL_ISSUED`'s empty `☔ 호우경보`/
  `☔ 호우주의보` headings and removes the need for `HEATWAVE_UPGRADED`'s
  extractor to build its own Korean sentence (another flagged review fix —
  extractors return raw values only now).
- `app/template_extractors.py`: river-name extraction reordered to
  explicit-label → windowed-regex-near-keyword → dictionary
  (`config/entity_dictionary.yaml`, new) → conservative global regex, with
  a small false-positive blocklist (`건강`) so a common non-river word
  ending in 강/천 can no longer become a false "final" answer.
- `app/commands/poll_once.py`, `database_status.py`, `cleanup_database.py`:
  ported/merged onto the new base; `poll_once` now sends the 8-button
  selector instead of auto-recommending, and a `template_suggestions`
  storage failure can no longer look like (or cause) a failed delivery.
- `.env.example`, `pyproject.toml`: new polling/retention/AI env vars;
  added `openai`, `pydantic`, `PyYAML` dependencies.

### Local controlled validation (mocked — no live network)

```
$ python -m pytest -q
288 passed in 4.94s

$ ruff check app tests
All checks passed!
```

`python -m app.commands.test_template` spot checks after the rule/extractor
changes (representative text per template, see `docs/template_engine.md`):
`FLOOD_ADVISORY_ISSUED` (river via windowed-regex tier), `HEATWAVE_UPGRADED`
(now correctly requires 폭염주의보+폭염경보+상향 together), and
`HEAVY_RAIN_MULTI_LEVEL_ISSUED` (경보/주의보 headings render only when their
region list was actually extracted) all produced correct, complete drafts.

### Live testing status

- Live Telegram send/button-click behavior and live OpenAI extraction were
  **not** exercised this session — validated with mocks only
  (`tests/test_telegram_bot.py`, `tests/test_template_flow.py`,
  `tests/test_ai_client.py`). `AI_ENABLED=false` by default, and the real
  `OPENAI_API_KEY` is provided separately by the operator; live AI
  validation is pending that key, per `docs/service_v1.md`.
- PR #1 and PR #3 (the two source branches) are superseded by the merged
  Service v1 PR and closed with a comment pointing to it.

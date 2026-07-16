# Part 1 Completion Report

> **Historical record — the Seoul SafeCity source described below was
> retired in v0.5.0.** Live collection now uses the 행정안전부(MOIS)
> SafetyData API with a 국민안전24 HTML fallback; see
> `docs/live_source_migration_mois_api_plan.md`,
> `docs/mois_api_contract_confirmed.md`, and `docs/source_cutover_runbook.md`
> for the current source. This report is preserved unmodified as a record of
> what was actually verified for the original Part 1 source.

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

## 10. Service v1 runtime hardening (added 2026-07-14)

Four targeted fixes to the merged Service v1 flow, requested after the
initial merge, on branch `feature/service-v1-runtime-hardening`:

1. **Confirm handler never reports success on DB failure**
   (`app/template_flow.py::_handle_preview_confirm`): the decision is now
   written *before* the preview is marked confirmed, and the `[최종 확정
   완료]` reply is only sent after the decision write succeeds. A decision
   write failure sends `[최종 확정 실패]` and leaves the preview untouched
   (still confirmable via the same button); a failure in the follow-up
   preview-status update alone still reports success, since the
   authoritative decision already exists safely.
2. **Decision source snapshots** (`app/database.py`): `template_decisions`
   gained `source_id_snapshot`/`sender_or_region_snapshot`/
   `sent_at_snapshot`/`original_body_snapshot`, populated from the
   `messages` row at confirmation time, with an automatic `ALTER TABLE`
   migration (`Database._migrate_schema`) for databases created before
   these columns existed. The message → confirmed-template training pair
   now survives message retention cleanup with its full original text
   intact, not just a `message_id`.
3. **Active-preview / supersede policy** (`app/database.py::
   supersede_active_previews`, `app/template_flow.py`): selecting a new
   template, or a successful AI generation, marks the previously-active
   preview (`rule_preview`/`ai_preview`) for that `(message_id,
   selected_by)` as `superseded`. Confirm/cancel/AI on a superseded,
   cancelled, or failed preview now replies `[사용할 수 없는 초안]` instead
   of silently no-opping. A failed AI attempt does not supersede the
   source Rule preview.
4. **Stronger AI value/evidence validation**
   (`app/ai_client.py::_value_supported_by_evidence`): the returned value
   must now be a normalized substring of its own evidence (not merely
   "evidence exists somewhere in the message") — `value="한강"` next to
   `evidence="중랑천에 홍수주의보가 발령되었습니다"` now correctly fails.
   List-like values are split conservatively and each item checked against
   the evidence or the full message; a pure time value gets deterministic
   normalization (`18:00` == `18시`) with no semantic inference.

### Automated test result

```
$ python -m pytest -q
307 passed in 5.10s

$ ruff check app tests
All checks passed!
```

`ruff format --check app tests` reports the same 36 pre-existing files as
before this branch (unrelated to these changes — verified via
`git stash`/re-check); no new formatting drift was introduced, and a
repo-wide reformat was out of scope for this task.

### Controlled local validation (mocked Telegram/OpenAI, real SQLite)

Ran the section-8 scripted flow end-to-end against a temporary on-disk
SQLite database (not a live Telegram chat):

```
1. source message                          -> stored, message_id=1
2. select template                          -> rule preview_id=1, status=rule_preview
3. Rule preview shown                       -> "[템플릿 초안]..." sent
4. select another template                  -> second preview_id=2 created
5. first preview becomes superseded         -> preview 1 status=superseded
6. old preview confirmation is rejected     -> "[사용할 수 없는 초안]"
7. latest preview confirms successfully     -> "[최종 확정 완료]"
8. decision row contains source snapshots   -> source_id/sender/sent_at/body all present
9. cleanup removes the source message       -> messages remaining=0
10. decision still has the original body    -> original_body_snapshot intact after cleanup
11. mismatched AI value/evidence rejected   -> status=validation_failed
```

All 11 steps passed. Live Telegram button-clicks and live OpenAI calls were
still not exercised this session (mocks only, per the operator's earlier
"merge now, mocks-only" decision for the base Service v1 PR) — see the
final report for current status.

## 0.1.1: multi-user routing / privacy fix

**Root cause**: every interactive send path (`/latest`, ordinary text, and
every template/preview callback handler) called
`TelegramSender.send_plain_text(text, reply_markup=...)` with no `chat_id`,
which silently fell back to the configured broadcast `TELEGRAM_CHAT_ID`.
`/latest`/text additionally never replied to the requester at all
(`TelegramBotRunner._handle_authorized` returned `None` for them). One
shared cause explained both reported symptoms plus a third, previously
unreported one (callback preview/confirm/cancel/AI responses also
defaulting to the broadcast chat) — confirmed live in the running
service's logs (`sendMessage` firing on every inbound `getUpdates` offset
advance) before any code change was made.

**Fix**: `TelegramSender.send_plain_text` gained explicit `chat_id`,
`reply_to_message_id`, and `enforce_send_enabled` parameters.
`app.template_flow.send_initial_alert`/`send_latest_alert` gained
`target_chat_id`/`persist_suggestion`/`enforce_send_enabled` so the
automatic poller path (`target_chat_id=TELEGRAM_CHAT_ID`,
`persist_suggestion=True`, `enforce_send_enabled=True`) and the
interactive `/latest`/text path (inbound `chat_id`, `persist_suggestion=
False`, `enforce_send_enabled=False`) share one rendering function with
different delivery/persistence behavior. Every callback handler in
`app.template_flow.dispatch_callback` now extracts `interaction_chat_id`
from `callback_query.message.chat.id` and routes its entire response
there, failing closed (acknowledge + drop, no default-chat fallback) if
that field is missing. `template_previews.interaction_chat_id` (new,
migration-safe column) binds each preview to the chat/user that created
it; confirm/cancel/AI now reject any callback whose current chat_id/user_id
doesn't match with `[사용할 수 없는 요청]`, never acting on or revealing the
mismatched preview. `system_state.telegram_update_offset` (new column)
persists the `getUpdates` offset so a restart can't replay already-handled
updates. See `docs/service_v1.md` "Broadcast vs. interactive delivery" and
`CHANGELOG.md` for the full writeup.

**Tests**: a 15-scenario cross-chat regression suite (broadcast chat vs.
two authorized operators in separate chats vs. an unauthorized chat) was
added across `tests/test_telegram_bot.py` and `tests/test_template_flow.py`,
alongside the existing rendering-parity/duplicate-callback/AI-path
coverage — see the final report for pass counts.

## 0.3.0: `/history` + routing validation

Added `/history`, delivery-mode/latency logs, offline
`validate_telegram_behavior`, and callback-ack not gated by Broadcast.
Cross-user private-chat isolation remains enforced; shared-group visibility
is documented as expected Telegram behavior (see
`docs/telegram_routing_validation.md`). Live two-user private-chat check is
required before tagging `v0.3.0`.

## 0.4.0: independent Telegram operators

Every authorized operator became an equal, independent entity. The SafeCity
collector and `messages` DB stay shared; delivery, commands, previews,
decisions, AI, and mute/subscribe state are now fully personal. There is no
primary/default chat concept — `TELEGRAM_CHAT_ID` degrades to legacy
migration/bootstrap only.

- **Personal subscriptions** (`telegram_subscriptions`): every authorized
  private interaction registers/touches a subscription; a bootstrap seeds
  peers from historical private previews (and, only when unambiguous, the
  legacy `TELEGRAM_CHAT_ID`). Never seeds groups or guesses an owner.
- **Per-recipient automatic delivery** (`telegram_deliveries`): the poller
  fans a new alert out to every active personal subscription with independent
  per-recipient delivery/retry; one recipient's failure never blocks another;
  no `TELEGRAM_CHAT_ID` fallback and no backfill for later subscribers.
  `messages.telegram_status`/`telegram_message_id` are kept as derived
  aggregates for compatibility.
- **Private-chat-only operation**: operational commands and buttons are
  rejected in groups with `[개인 채팅에서 사용해 주세요] …` (acked, never
  processed, never rerouted).
- **Personal commands**: `/subscribe`, `/unsubscribe`, `/mute`, `/unmute`;
  `/pause`→`/mute` and `/resume`→`/unmute` aliases that no longer touch shared
  polling. `/status` shows separate personal and shared-collection sections.
- **Preview/decision isolation**: previews scoped by
  `(message_id, selected_by, interaction_chat_id)`; decisions unique per
  `(message_id, confirmed_by, interaction_chat_id)` (existing DBs migrated in
  place, idempotently). `template_actions`/`ai_generations` carry
  `interaction_chat_id`.
- **Non-blocking AI**: a bounded `ThreadPoolExecutor` (`TELEGRAM_AI_WORKERS`,
  default 2) runs AI generation off the poll loop; one operator's slow AI
  never blocks another's button. AI results route only to the requesting
  operator's chat; `[AI 요청 처리 불가]` on submit failure.
- **Migration** validated on a copy of the production DB (all message/preview/
  decision/AI/action rows preserved; new columns/tables added; old decisions
  readable; idempotent). The offline validator was extended to 28
  independent-operator checks (synthetic operators A/B + a group) and prints an
  `Independent operator validation` PASS/FAIL block.

See `docs/independent_operator_model.md` for the full model. A live
two-operator private-chat check is required before tagging `v0.4.0`.

## 0.4.1: poller-control and delivery-retry hardening

A compact production hotfix for two narrow v0.4.0 defects, plus an explicit
local command for the shared collector. The v0.4.0 independent-operator
architecture is unchanged (no schema, config, or Telegram-command-contract
change).

- **Retry authorization filtering**: automatic *new* deliveries already used
  `list_active_subscriptions(allowed_user_ids)`, but *retries* used
  `list_retryable_deliveries()` with no allow-list check, so a failed/pending
  delivery could be retried after the user was removed from
  `TELEGRAM_ALLOWED_USER_IDS`. `list_retryable_deliveries` now takes
  `allowed_user_ids` and filters with a parameterized `IN (...)` clause (empty
  list ⇒ no rows, no invalid SQL, no `TELEGRAM_CHAT_ID` fallback); the poller
  passes `settings.telegram_allowed_user_ids`. Historical delivery rows are
  kept for audit, just not resent.
- **`/status` privacy**: the shared-collector section no longer prints the
  `paused_by` operator id (the `중지 요청자` line was removed). The pause
  timestamp may still show, without any actor identity. `system_state`'s
  `paused_by`/`resumed_by` columns and internal logs are unchanged (audit
  retained).
- **Local shared-collector control** (`app/commands/poller_control.py`):
  `status` (read-only, identifier-free), `resume`, `pause` — idempotent,
  recording control events under the non-user administrative actor id `0`. It
  is the only explicit shared-poller control, is not a Telegram command, and is
  not in `/help`. It exists to re-enable a collector that a legacy pre-v0.4.0
  Telegram `/pause` left disabled after migrating an older DB.
- The offline validator gained `Retry authorization filtering`, `Status
  privacy`, `Personal/global pause separation`, and `Local poller control`
  checks (32 checks total, all PASS). New unit tests cover the poller-control
  command, the ten retry-authorization cases, and `/status` privacy.

A live two-operator private-chat check is required before tagging `v0.4.1`.

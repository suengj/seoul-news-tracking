# Telegram Template Flow (wiring detail)

How Service v1's select -> preview -> confirm/cancel/AI flow is actually
wired over Telegram. See `docs/service_v1.md` for the product-level
picture and `docs/template_engine.md` for the rule/extraction/rendering
logic itself.

See `docs/history_command.md` for `/history` and
`docs/telegram_routing_validation.md` for Broadcast vs interactive diagnosis.

## Excel catalog (v0.2.0)

Business templates come from `templates/서울시_재난특보_X템플릿.xlsx`
and are generated into `config/message_templates.yaml` with
`python -m app.commands.sync_templates_from_excel --write`.
The live bot never opens the workbook; it only loads the YAML.

Initial keyboard is two-stage:
1. Category buttons (`cat:{message_id}:HW|HT|TN|FL`) + `📄 원문`
2. Per-category automation templates (`tpl:{message_id}:HW-01`, …)
   with `← 뒤로` (`back:{message_id}`) and `📄 원문`

## One bot process, one offset sequence

`python -m app.commands.run_telegram_bot` runs a single `getUpdates`
long-poll (`app/telegram_bot.py::TelegramBotRunner`) that handles **both**:

- ordinary messages: `/latest`, `/history`, `/status`, `/pause`, `/resume`, `/help`,
  plain text (same as `/latest`)
- `callback_query` updates: routed to `app.template_flow.dispatch_callback`

### `/latest` and ordinary text use the same renderer as a new message

`/latest` and plain-text messages both call
`app.template_flow.send_latest_alert`, which looks up the most recently
collected record and renders it through `send_initial_alert` — the exact
same function `poll_once` uses for a genuinely new SafeCity message (see
"Sending: `poll_once` -> initial alert" below). There is only one rendering
path for a disaster-message alert: the original text, the 8 selection
buttons, and the secondary "실험적 추천" hint are always byte-for-byte
identical regardless of what triggered the send, and a button press on a
`/latest`-triggered message routes through `dispatch_callback` exactly like
one on a poller-triggered message (same `tpl:{message_id}:{short_code}`
callback data, since `message_id` is the real `messages.internal_id`
either way). This replaced an older, separate `build_latest_reply` summary
renderer (MarkdownV2 text only, no buttons) — that renderer no longer
exists. If the database is empty, `/latest`/plain text instead sends
"아직 저장된 재난문자가 없습니다." with no keyboard.

Delivery target and persistence differ from a poller-triggered alert even
though the rendering is identical — see docs/service_v1.md "Broadcast vs.
interactive delivery": `/latest`/text always reply to the requesting chat
(`send_latest_alert(..., chat_id=<inbound chat_id>)`), never
`TELEGRAM_CHAT_ID`, are never gated by `TELEGRAM_SEND_ENABLED`, and never
add a second `template_suggestions` row for what is just a replay.

There is deliberately no second `getUpdates` consumer — Telegram itself
would reject a concurrent one for the same bot token with HTTP 409, and a
local file lock (`app/process_lock.py`) refuses to start a second instance
of this process regardless. `python -m app.commands.run_local` starts this
plus the poller as the two child processes that make up the whole local
runtime.

The offset itself is persisted in `system_state.telegram_update_offset`
(`Database.get_telegram_update_offset`/`set_telegram_update_offset`):
`TelegramBotRunner.__init__` loads it at startup (logged as `Telegram bot
starting: version=<x> persisted_offset=<n>`) and it's written back — a
short, immediately-committed statement, never held open across a Telegram
network call — right after each update is handled (or safely rejected). A
clean or abnormal restart always resumes from the last persisted offset
instead of replaying already-handled updates.

## Sending: `poll_once` -> initial alert

For every genuinely new record, `app/commands/poll_once.py` calls
`app.template_flow.send_initial_alert` (which itself calls
`build_initial_alert` below to get the text/keyboard, sends it, then
persists the rule engine's suggestion). `send_latest_alert` — used by
`/latest` and ordinary text, see above — calls the exact same
`send_initial_alert` for the most recently collected record, so both
paths share one implementation with no duplicated rendering code.
`build_initial_alert` itself:

1. runs `recommend_template` (informational only — see `docs/service_v1.md`)
   inside its own `try/except`, so a rule-engine crash can never block the
   original alert
2. builds one message: sender/time, the **complete original body
   verbatim**, "사용할 템플릿을 선택해 주세요.", and (only if a rule
   recommendation exists) a secondary "(실험적 추천: ...)" line
3. attaches an 8-button keyboard (`build_selection_keyboard`): the 7
   templates + "📄 원문", laid out 2-per-row; a template with
   `enabled: false` in `config/message_templates.yaml` is omitted from the
   keyboard entirely, not just grayed out
4. sends as one plain-text message (no MarkdownV2 — avoids escaping the
   arbitrary original text) and records the rule engine's own guess in
   `template_suggestions`, in a `try/except` that runs *after* the send so a
   storage failure can never look like (or cause) a failed delivery

```
Row 1: [🌊 홍수주의보] [☔ 호우 해제]
Row 2: [☔ 호우 하향]  [☔ 호우 복합]
Row 3: [🔥 폭염 상향]  [🔥 폭염주의보]
Row 4: [🌙 열대야]    [📄 원문]
```

Callback data: `tpl:{message_id}:{short_code}` (e.g. `tpl:482:rain_clr`),
always well under Telegram's 64-byte limit. Short codes are in
`TEMPLATE_SHORT_CODES` in `app/telegram_sender.py`.

## Receiving: template selection -> preview

`app.template_flow.dispatch_callback` is the single entry point for every
inline-button press. For a `tpl:` callback:

1. **answer the callback immediately** (stops the loading spinner)
2. reject silently (log only) if `from.id` isn't in
   `TELEGRAM_ALLOWED_USER_IDS`
3. reject (no-op) if this exact `callback_query_id` was already processed —
   see "Duplicate-callback guard" below
4. load the **original** message from `messages` by `message_id`
5. re-run `extract_slots` + `render_template` against the **original body**
   (never a previous suggestion), log one `template_actions` row, create
   one `template_previews` row
6. send the preview as a **new** message (the original alert is never
   edited or deleted) with the right button set:
   - complete: `[✅ 최종 OK] [↩️ 취소] [🤖 AI로 작성]`
   - incomplete: `[↩️ 취소] [🤖 AI로 작성]` (no OK at all)
   - the AI button is omitted whenever `AI_ENABLED=false`
7. if the send itself fails, the `template_actions`/`template_previews`
   rows are updated to `status="failed"` — the DB stays consistent and
   queryable either way

Preview callback data: `preview:{preview_id}:confirm|cancel|ai` — never the
rendered text itself.

Every callback response (selection preview, confirm, cancel, AI) is sent to
`interaction_chat_id` — `callback_query.message.chat.id`, the chat the
pressed button's message actually lives in — never `TELEGRAM_CHAT_ID`. A
callback missing that field is acknowledged (so the spinner stops) and
dropped, with no default-chat fallback. `template_previews` records this
chat id (`interaction_chat_id`), and confirm/cancel/AI each require the
current callback's chat_id *and* user_id to match it/`selected_by` before
acting — see docs/service_v1.md "Preview ownership" for the exact rejection
behavior on a mismatch.

### Incomplete preview format

```
[템플릿 작성 미완료]

선택 포맷:
{display name}

확인된 값:
{slots that did extract}

누락 필드:
{missing slots}

원문:
{original message}
```

Never invents a missing region/time/river/warning-level value — a mismatch
renders cleanly as "incomplete," not a guess.

## Confirm / cancel / AI

Every one of the three actions below first checks the preview's `status`;
only `rule_preview`/`ai_preview` ("active") are ever acted on — see "Latest
preview only" below.

- **✅ 최종 OK** (`_handle_preview_confirm`): validates the preview has no
  missing slots, then writes the decision *before* touching the preview's
  own status:
  1. `db.upsert_decision(...)` (includes a source snapshot — see below). If
     this raises, nothing is marked confirmed and the operator gets
     `[최종 확정 실패]` — the preview stays `rule_preview`/`ai_preview` so the
     same button can be retried.
  2. Only after that succeeds: `db.update_preview_status(..., status=
     "confirmed")`. If *this* step fails, the decision itself is already
     safely written — the operator still gets `[최종 확정 완료]` (a retried
     click just re-upserts the same decision and retries this step).
  3. Reply with `[최종 확정 완료]`.
  Re-clicking OK on an already-`confirmed` preview just resends the same
  confirmation text — idempotent, no second decision row.
- **↩️ 취소** (`_handle_preview_cancel`): marks the preview `cancelled`,
  resends the original body with the same 8 selection buttons so the
  operator can pick again. Never creates a decision. Previous messages are
  never deleted. Rejected (no state change) if the preview isn't active —
  see below.
- **🤖 AI로 작성** (`_handle_preview_ai`): see `docs/service_v1.md`. On
  success, the source Rule preview is marked `superseded` and a *new*
  `template_previews` row (`extraction_method="ai"`) is created with only
  `[✅ 최종 OK] [↩️ 취소]`; on failure, the *original* preview is left active
  (not superseded) and re-shown as incomplete (no new preview row, no AI
  retry button) so the operator can still confirm-later/cancel/pick again.

## Latest preview only (active-preview supersede policy)

A preview's `status` moves through: `rule_preview`/`ai_preview` (active,
i.e. confirmable/cancellable) → one of `confirmed` / `cancelled` / `failed`
/ `superseded` (all terminal). Two things create a new active preview and
supersede whatever was active before, for that `(message_id, selected_by)`:

- **Selecting a template again** (`db.supersede_active_previews`): any
  existing `rule_preview`/`ai_preview` row for that message+operator becomes
  `superseded` before the new one is inserted.
- **A successful AI generation**: the source Rule preview it was generated
  from becomes `superseded` (a *failed* AI attempt does not touch it).

`confirmed` rows are never touched by supersede — an already-confirmed
preview stays `confirmed` forever, and a later re-selection/re-confirmation
for the same message updates `template_decisions` in place without
disturbing the earlier confirmed preview's own row (full history stays
reconstructable from `template_previews`).

Any confirm/cancel/AI callback aimed at a non-active preview (`superseded`,
`cancelled`, `failed`) gets:

```
[사용할 수 없는 초안]

더 최신 초안이 있거나 이미 취소된 초안입니다.
최신 Telegram 메시지의 버튼을 사용해 주세요.
```

— never silently ignored, and never allowed to resurrect an old preview
into a decision or a cancellation.

## Decision source snapshots

`template_decisions` carries `source_id_snapshot`/`sender_or_region_
snapshot`/`sent_at_snapshot`/`original_body_snapshot`, populated from the
`messages` row at confirmation time. This is what lets a confirmed decision
outlive the source message's own retention window with the *full* original
text still attached, not just its `message_id` — see
`docs/database_retention.md`. A pre-existing database (created before these
columns existed) migrates them in automatically via `ALTER TABLE` on
startup; older rows just have `NULL` snapshots.

## Duplicate-callback guard

A single small table, `processed_callback_queries(callback_query_id TEXT
PRIMARY KEY, ...)`, is checked (and marked) before *any* routing decision —
template selection, confirm, cancel, or AI alike. This replaced the
narrower PR #3 approach (a `UNIQUE` column only on `template_actions`,
which couldn't guard preview callbacks that don't write to that table at
all). `template_actions.callback_query_id` still has its own `UNIQUE`
constraint as defense-in-depth.

## Local controlled validation

Recorded output (mocked Telegram, no live network) is in
`docs/template_engine.md` and `docs/part1_completion_report.md`. Live
button-click behavior against a real chat was not exercised this session —
see the final report for what's still pending.

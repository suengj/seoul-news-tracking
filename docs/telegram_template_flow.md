# Telegram Template Flow (wiring detail)

How Service v1's select -> preview -> confirm/cancel/AI flow is actually
wired over Telegram. See `docs/service_v1.md` for the product-level
picture and `docs/template_engine.md` for the rule/extraction/rendering
logic itself.

## One bot process, one offset sequence

`python -m app.commands.run_telegram_bot` runs a single `getUpdates`
long-poll (`app/telegram_bot.py::TelegramBotRunner`) that handles **both**:

- ordinary messages: `/latest`, `/status`, `/pause`, `/resume`, `/help`,
  plain text (same as `/latest`)
- `callback_query` updates: routed to `app.template_flow.dispatch_callback`

There is deliberately no second `getUpdates` consumer — Telegram itself
would reject a concurrent one for the same bot token with HTTP 409, and a
local file lock (`app/process_lock.py`) refuses to start a second instance
of this process regardless. `python -m app.commands.run_local` starts this
plus the poller as the two child processes that make up the whole local
runtime.

## Sending: `poll_once` -> initial alert

For every genuinely new record, `app/commands/poll_once.py` calls
`app.template_flow.build_initial_alert`, which:

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

- **✅ 최종 OK** (`_handle_preview_confirm`): validates the preview has no
  missing slots, `UPSERT`s one row into `template_decisions`
  (`message_id` is `UNIQUE` — a later confirmation on a different preview
  for the same message replaces the current decision in place), marks the
  preview `confirmed`, replies with `[최종 확정 완료]`. Re-clicking OK on an
  already-`confirmed` preview just resends the same confirmation text —
  idempotent, no second decision row.
- **↩️ 취소** (`_handle_preview_cancel`): marks the preview `cancelled`,
  resends the original body with the same 8 selection buttons so the
  operator can pick again. Never creates a decision. Previous messages are
  never deleted.
- **🤖 AI로 작성** (`_handle_preview_ai`): see `docs/service_v1.md`. On
  success, a *new* `template_previews` row (`extraction_method="ai"`) is
  created with only `[✅ 최종 OK] [↩️ 취소]`; on failure, the *original*
  preview is re-shown as incomplete (no new preview row, no AI retry
  button) so the operator can still cancel and pick again.

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

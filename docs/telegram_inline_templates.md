# Telegram Inline Template Buttons

How new Seoul SafeCity messages get an inline-keyboard alert, and how button
presses are handled. See `docs/template_engine.md` for the rule/extraction/
rendering logic itself — this doc covers the Telegram-specific wiring.

## Sending: `poll_once`

For every genuinely new record being sent (new records, plus previously
collected-but-unsent ones — never historical/backfill records),
`app/commands/poll_once.py` calls `_send_via_template_pipeline`, which:

1. runs `recommend_template` + (if a recommendation exists) `extract_slots`
   + `render_template`
2. builds one alert message (`build_template_alert_message`): sender/time,
   the **complete original body verbatim**, the recommended template's
   display name, its rule score, matched signals, and either the rendered
   draft or the reason it couldn't be rendered
3. attaches an inline keyboard (`build_keyboard`) and sends both as one
   plain-text Telegram message (no MarkdownV2 — avoids any need to escape
   the original message text)
4. records the attempt in the `template_suggestions` table (candidates,
   extraction, rendered text), keyed by the message's own `internal_id`

**Any exception in step 1 is caught** — the alert still sends, with
"권장 템플릿 없음" and no draft, so template-processing bugs can never block
delivery of the original message.

### Keyboard layout

```
Row 1: [✅ 권장 포맷]* [📄 원문]
Row 2: [🌊 홍수주의보] [☔ 호우 해제]
Row 3: [☔ 호우 하향]  [☔ 호우 복합]
Row 4: [🔥 폭염 상향]  [🔥 폭염주의보]
Row 5: [🌙 열대야]
```

\* omitted when there is no safe recommendation (UNKNOWN) — only "📄 원문"
remains in row 1 in that case.

### Callback data

`tpl:{message_id}:{short_code}` (e.g. `tpl:482:rain_clr`), always well
under Telegram's 64-byte `callback_data` limit — never the message text
itself. Short codes are in `TEMPLATE_SHORT_CODES` in `app/telegram_sender.py`.

## Receiving: `run_telegram_bot`

```
python -m app.commands.run_telegram_bot
```

A separate, long-running process (`getUpdates` long-polling — no webhook
server, no Docker) that only handles `callback_query` updates. SIGINT/
SIGTERM stop it after the in-flight update finishes.

For each button press (`handle_callback_query` in
`app/commands/run_telegram_bot.py`):

1. **answer the callback immediately** (stops Telegram's loading spinner)
   regardless of what happens next
2. reject silently (log only, no message sent, nothing exposed) if the
   `from.id` is not in `TELEGRAM_ALLOWED_USER_IDS`, if `callback_data` is
   malformed, or if the referenced `message_id` doesn't exist
3. skip re-processing (but stay a no-op success) if this exact
   `callback_query_id` was already recorded — a duplicate Telegram delivery
   of the same press, not a new user action
4. load the **original** message from the real-time `messages` table by
   `message_id`
5. `ORIGINAL_ONLY` → resend the original body verbatim, no extractor run.
   Any other template → **re-run `extract_slots` against the original
   body** (never against the previous suggestion) and render
6. send the result as a **new** Telegram message — the original alert is
   never edited or deleted, and every button press produces its own reply
7. record the outcome in `template_actions` (selected template, who chose
   it, extraction, rendered text, status, error)

### Manual mismatch

If a user picks a template the message doesn't actually support, rendering
fails cleanly (missing required slot) rather than inventing a value. The
reply is:

```
[템플릿 생성 불가]

선택 포맷:
{display name}

누락 필드:
{missing slots}

확인된 값:
{slots that did extract successfully}

원문:
{original message}
```

### Telegram failures never corrupt state

If sending a reply fails (`send_plain_text` returns a failed outcome), the
`template_actions` row is still written with `status="failed"` and the
error message — the database is left in a consistent, queryable state
either way.

## Local controlled validation

Recorded results (test command output, `test_template` runs, and — if a
real bot/chat was available in this session — a live inline-button test)
are in `docs/template_engine.md` and `docs/part1_completion_report.md` §9.

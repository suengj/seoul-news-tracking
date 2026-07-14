# Service v1: human-first template selection

Current version: see `pyproject.toml` / `app/version.py` (also shown in the
Telegram bot's startup log and `/status` reply) — see `docs/versioning.md`
for the release process. As of this writing: **0.1.1**.

Service v1 integrates the recurring local poller and Telegram command bot
(previously a separate branch) with the deterministic template engine
(previously another separate branch) into one small, compact runtime. The
core product decision: **a human always picks the template.** The system
never auto-selects, auto-renders-and-sends, or auto-confirms anything.

## The flow

```
1. Poller collects a genuinely new Seoul SafeCity message (every
   POLL_INTERVAL_SECONDS, default 300s).
2. The message is stored, then sent to Telegram as-is, with 8 equal-weight
   buttons: the 7 templates + "📄 원문". A small secondary line may show
   "실험적 추천: ..." (the deterministic rule engine's own guess) but it is
   never a button, never bold, never the primary action.
3. An authorized operator reads the original message and taps one button.
4. The system re-runs deterministic extraction against the *original* body
   for the selected template, renders it if possible, and sends a preview:
     - complete  -> [✅ 최종 OK] [↩️ 취소] [🤖 AI로 작성]
     - incomplete -> [↩️ 취소] [🤖 AI로 작성]     (no OK button at all)
   (the AI button is omitted entirely when AI_ENABLED=false)
5. ✅ 최종 OK -> writes one row to `template_decisions` (see below) and
   replies with a confirmation. This is the only action that ever produces
   a decision.
6. ↩️ 취소 -> marks the preview cancelled and resends the original body with
   the same 8 selection buttons, so the operator can try a different
   template. Previous messages are never edited or deleted.
7. 🤖 AI로 작성 -> calls OpenAI (only now, only for the template the human
   already picked) to re-extract the same slots; on success this produces a
   *new* preview with only [✅ 최종 OK] [↩️ 취소] (AI never re-offers itself).
```

## Broadcast vs. interactive delivery

Two distinct Telegram delivery concepts, both implemented on top of
`TelegramSender.send_plain_text(chat_id=..., enforce_send_enabled=...)`
(`app/telegram_sender.py`) — never conflated, never a silent fallback from
one to the other:

| | Automatic broadcast | Interactive reply |
|---|---|---|
| When | Poller detects a genuinely new SafeCity message (`app.commands.poll_once`) | `/latest`, ordinary text, `/status`, `/pause`, `/resume`, `/help`, any callback (template selection, preview confirm/cancel/AI) |
| Target chat | `TELEGRAM_CHAT_ID` (the configured broadcast chat), always | The chat_id the inbound message/callback actually came from — `message.chat.id` or `callback_query.message.chat.id` |
| `TELEGRAM_SEND_ENABLED` | Honored — the flag gates this | Never honored — a direct reply to something an operator just did must never be silently dropped |
| Persists `template_suggestions`? | Yes, once | No (a `/latest` replay never adds a second row) |

`app.template_flow.send_initial_alert(..., target_chat_id, persist_suggestion,
enforce_send_enabled)` is the single function both paths call — the
rendered text/keyboard are always byte-for-byte identical, only the target
chat and persistence differ. `send_latest_alert` (used by `/latest`/text)
always passes the inbound chat_id and `persist_suggestion=False`.
`app.commands.poll_once` always passes `settings.telegram_chat_id` and
`persist_suggestion=True`.

Every callback handler in `app.template_flow.dispatch_callback` extracts
`interaction_chat_id` from `callback_query.message.chat.id` — the chat the
inline keyboard actually lives in — and routes its response there. If that
field is missing (a malformed update), the callback is acknowledged (so
Telegram's spinner stops) but nothing else happens: there is no default-chat
fallback for a callback with unknown routing.

### Preview ownership: bound to the originating chat and user

`template_previews.interaction_chat_id` records the chat a preview's
selection button was pressed in. Every confirm/cancel/AI callback requires
**both** the callback's current chat_id and user_id to match the preview's
`interaction_chat_id`/`selected_by` before acting — a mismatch (a different
authorized operator, or the same operator from a different chat) gets:

```
[사용할 수 없는 요청]

이 초안이 생성된 Telegram 대화에서 다시 시도해 주세요.
```

and the preview is left completely untouched — never resurrected, never
revealed to the mismatched caller. A database created before this column
existed migrates it in automatically (`NULL` for old rows, which then never
match any caller and simply can't be acted on again — expected for rows
predating this fix).

### Diagnosing repeated/misrouted responses

If `TELEGRAM_CHAT_ID` appears to receive a reply that wasn't a genuine new
SafeCity message, or an operator reports getting no reply to `/latest`,
check the bot's INFO-level attribution logs (`app/telegram_bot.py::dispatch`,
`app/template_flow.py::dispatch_callback`): each inbound update logs
`update_id`, `user_id`, `chat_id`, `command`/`action`, `routed_chat_id`, and
`outcome` (never the message text or disaster-message body). If
`routed_chat_id` ever equals `TELEGRAM_CHAT_ID` for something other than an
automatic broadcast, that is the bug to chase — it should be structurally
impossible after this fix, since every interactive call site passes its own
inbound `chat_id` explicitly and never relies on `send_plain_text`'s default.

## Action vs. preview vs. decision

Three different database tables exist because they answer three different
questions, and conflating them was the main thing this integration fixed:

| Table | Answers | Written by |
|---|---|---|
| `template_actions` | "which button did the operator press, and did the reply actually send?" | every template-selection tap |
| `template_previews` | "what did we show, and is it currently confirmable?" | every selection tap (rule) and every AI attempt that succeeds |
| `template_decisions` | "what is the current authoritative, confirmed final text for this message?" | only an explicit ✅ 최종 OK |

A template button press or a cancellation is **never** treated as a final
label — only `template_decisions` is future automation ground truth.
Re-confirming a different preview for the same message updates the existing
`template_decisions` row in place (`message_id` is `UNIQUE`); the full
history of what was tried is still reconstructable from `template_previews`
alone, so no separate decision-history table was added (see
`docs/database_retention.md` for why these tables are also exempt from
message retention cleanup).

`template_decisions` also carries an immutable **source snapshot**
(`source_id_snapshot`, `sender_or_region_snapshot`, `sent_at_snapshot`,
`original_body_snapshot`), written at confirmation time from the `messages`
row that existed then. This is what makes the message → confirmed-template
training pair survive `messages` retention cleanup — without it, a decision
whose source message aged out after `MESSAGE_RETENTION_DAYS` would still
reference the original text only by an FK that no longer resolves. A
database created before this column existed migrates automatically (see
`app/database._migrate_schema`); its older rows simply have `NULL`
snapshots, and every newly confirmed decision always populates all four.

### Confirming never lies about success

✅ 최종 OK only ever replies `[최종 확정 완료]` after the `template_decisions`
row is actually written. If that write fails (e.g. a transient SQLite lock),
the operator gets `[최종 확정 실패]` and the preview is left exactly as it
was — still `rule_preview`/`ai_preview`, so the same OK button can simply be
pressed again. (If the decision write itself succeeds but the follow-up
"mark this preview `confirmed`" bookkeeping update fails, the ground truth
already exists — the operator still gets a genuine success reply, since a
retried click just re-upserts the same decision.)

### Latest-preview-only

Selecting a new template for the same message (by the same operator)
immediately marks any of that operator's other still-active previews for
that message (`rule_preview`/`ai_preview`) as `superseded`; a successful AI
generation likewise supersedes the Rule preview it was generated from (a
*failed* AI attempt does not — the original Rule preview stays confirmable).
Confirm/cancel/AI on a `superseded`, `cancelled`, or `failed` preview is
rejected with `[사용할 수 없는 초안]`, pointing the operator at the latest
Telegram message instead. An already-`confirmed` preview can still be
re-confirmed (idempotent resend) but never cancelled.

## Rule extraction vs. on-demand AI

- **Rule** (`app/template_rules.py`, `app/template_extractors.py`,
  `app/template_renderer.py`): deterministic regex/dictionary logic, no ML,
  runs automatically the moment a template is selected. See
  `docs/template_engine.md`.
- **AI** (`app/ai_client.py`): only called when the operator explicitly taps
  "🤖 AI로 작성". The template is already fixed by the human at that point —
  AI extracts that template's declared slots only, never picks a different
  template, never writes the fixed YAML wording itself, and its output is
  always pushed back through the same `render_template()` the Rule path
  uses (see "On-demand AI" below).

## On-demand AI

- `AI_ENABLED=false` (default) hides the AI button entirely.
- When enabled, `app/ai_client.py` calls OpenAI's structured-output API
  (`client.chat.completions.parse(response_format=<pydantic model>)`),
  verified against the current official docs
  (`developers.openai.com/api/docs/guides/structured-outputs`). Every
  returned slot must declare `evidence` that is actually a substring of the
  original message text (whitespace-normalized); an undeclared slot name,
  a changed `template_id`, a missing/unverifiable evidence string, or a
  still-missing required slot all fail validation (`status=validation_failed`)
  rather than producing a draft.
- Every attempt — success or failure — is logged in `ai_generations`
  (model, prompt version, token counts, status, error; never the API key).
- Validation is stricter than "the evidence text exists somewhere in the
  message": the returned `value` must itself be supported by its `evidence`
  — a normalized substring for a scalar value, every conservatively-split
  item present in the evidence or the full message for a list-like value
  (e.g. "중랑천, 안양천"), or a deterministic time-normalization match
  (whitespace/leading-zero/`:` vs `시`, no semantic inference) for a
  pure time value. `value="한강"` next to `evidence="중랑천에 홍수주의보가
  발령되었습니다"` fails even though that evidence string is real and
  present in the message — the evidence must actually support *that* value,
  not just exist. See `app/ai_client.py::_value_supported_by_evidence`.
- **API-key note**: the real `OPENAI_API_KEY` is provided separately by the
  operator. Until it is supplied, `AI_ENABLED` stays `false` and only
  mocked-client tests exercise this path (see `tests/test_ai_client.py`) —
  live AI extraction is explicitly reported as pending in the completion
  report, not silently assumed to work.

## Known limitations

- The rule engine's "실험적 추천" hint uses the same signal-group scoring as
  before; it is cosmetic-only now and never gates or auto-fills anything.
- The active-preview supersede policy is scoped to `(message_id,
  selected_by)` — if two different authorized operators both select
  templates for the same message, each operator's own previews supersede
  each other independently, not across operators. Each operator's preview is
  also bound to the chat they selected it from (see "Preview ownership"
  above), so this is a scoping choice, not a routing gap.

## Next phase (not in this session)

Server/VPS deployment, HTTPS webhook instead of long-polling, and live AI
validation once `OPENAI_API_KEY` is supplied.

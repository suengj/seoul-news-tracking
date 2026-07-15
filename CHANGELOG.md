# Changelog

## [Unreleased]

## [0.4.1] - 2026-07-15

Compact production hotfix for two narrow v0.4.0 defects, plus an explicit
local command to inspect and control the shared SafeCity collector after
migrating from an older version. The v0.4.0 independent-operator architecture
is unchanged: Telegram `/pause` and `/resume` remain personal mute/unmute
aliases, and the new command is the only explicit shared-collector control.

### Fixed
- Failed or pending deliveries are no longer retried for users removed from
  `TELEGRAM_ALLOWED_USER_IDS`. `list_retryable_deliveries` now takes the
  current allowed-user IDs and filters the retry set with a parameterized
  `IN (...)` clause (empty list is safe — no rows, no invalid SQL, no
  `TELEGRAM_CHAT_ID` fallback), so a retry authorizes identically to a new
  delivery. Historical delivery rows are kept for audit, just not resent.
- `/status` no longer exposes the operator identity that paused the shared
  collector: the `중지 요청자` (`paused_by`) line was removed from the
  user-facing reply. The pause timestamp may still appear, without any actor
  identity. The underlying `system_state.paused_by` / `resumed_by` fields and
  internal logs are unchanged and still retain administrative attribution.

### Added
- `app/commands/poller_control.py`: a local-only administrative command to
  inspect, resume, and pause the shared SafeCity poller after migration from
  older versions:
  - `python -m app.commands.poller_control status`
  - `python -m app.commands.poller_control resume`
  - `python -m app.commands.poller_control pause`
  It is idempotent, records control events under the non-user administrative
  actor id `0`, prints only non-sensitive fields (no tokens or Telegram
  identifiers), and is deliberately NOT exposed through the Telegram bot or
  `/help`.
- Offline validator checks: `Retry authorization filtering`, `Status privacy`,
  `Personal/global pause separation`, and `Local poller control`.

## [0.4.0] - 2026-07-15

Independent Telegram operators: every authorized operator is now an equal,
independent entity. The SafeCity collector and `messages` DB stay shared;
automatic delivery, commands, previews, decisions, AI, and mute/subscribe
state are fully personal. There is no primary/default chat — `TELEGRAM_CHAT_ID`
degrades to a legacy migration/bootstrap value only. See
[docs/independent_operator_model.md](docs/independent_operator_model.md).

### Added
- Personal subscriptions (`telegram_subscriptions`): one equal, independent
  subscription per authorized operator (`active` / `muted` / `unsubscribed`),
  auto-registered on first authorized private interaction.
- Per-recipient automatic delivery (`telegram_deliveries`): each new SafeCity
  record fans out independently to every active private subscription, one
  delivery row per recipient, so one recipient's failure never blocks another
  and retries target only the failed recipient.
- Personal notification commands `/subscribe`, `/unsubscribe`, `/mute`,
  `/unmute`; `/pause` and `/resume` are now personal aliases of `/mute` and
  `/unmute` (they no longer touch shared polling).
- Non-blocking on-demand AI: a bounded `ThreadPoolExecutor`
  (`TELEGRAM_AI_WORKERS`, 1–4, default 2) runs AI generation so one operator's
  AI request never blocks another operator's non-AI commands.
- `TELEGRAM_AI_WORKERS` setting (validated 1–4).
- Extended offline validator (`validate_telegram_behavior`) covering the full
  independent-operator model (dual auto-register, fan-out, independent retry,
  personal mute/subscribe, group rejection, preview/decision isolation, AI
  independence, no legacy fallback, no backfill, legacy decision migration).

### Changed
- Automatic delivery no longer targets a single `TELEGRAM_CHAT_ID`; it uses
  active personal subscriptions loaded from SQLite. `TELEGRAM_ALLOWED_USER_IDS`
  are the operators; `TELEGRAM_SEND_ENABLED` remains the global master switch.
- Operational commands and inline buttons are strictly private-chat only; a
  group/supergroup/channel action is acknowledged and rejected in place, never
  processed and never rerouted to a private chat.
- `/status` shows two independent sections: the caller's own personal alert
  status and the shared common-collection status (no other operator's ids).
- Previews and Final-OK decisions are scoped per operator+chat:
  `template_decisions` is rebuilt to `UNIQUE(message_id, confirmed_by,
  interaction_chat_id)`; `template_actions` and `ai_generations` gain
  `interaction_chat_id`. `messages.telegram_status` / `telegram_message_id`
  are retained as backward-compatible aggregates derived from
  `telegram_deliveries`.

### Fixed
- `create_delivery_if_missing` now uses the cursor `rowcount` (not the stale
  `lastrowid`) to detect an ignored duplicate insert, so fan-out never
  re-sends an already-recorded per-recipient delivery.

### Migration
- Idempotent, atomic rebuild of `template_decisions` preserving every existing
  decision and its immutable source snapshots; `interaction_chat_id` is
  recovered from the confirming preview or defaults to the literal `'legacy'`.
- One-time subscription bootstrap seeds active peers from historical private
  Preview owners for authorized users, then the legacy `TELEGRAM_CHAT_ID` only
  when it maps unambiguously to exactly one allowed user (never a negative
  group id, never a guessed owner).

## [0.3.0] - 2026-07-14

### Added
- `/history` command with dynamic recent-message buttons (up to 10, KST
  `MM/DD HH:MM · {region}` labels, `hist:{internal_id}` callbacks).
- Offline Telegram routing validation command:
  `python -m app.commands.validate_telegram_behavior`.
- Interaction latency and delivery-mode logging (`delivery_mode`,
  `chat_type`, `elapsed_ms`, `TELEGRAM_SLOW_INTERACTION_MS` warning threshold).

### Fixed
- `answer_callback_query` is no longer gated by `TELEGRAM_SEND_ENABLED`
  (interactive acknowledgement still requires a bot token; 3s timeout).
- Interactive sends without an explicit `chat_id` now fail closed instead of
  silently falling back to `TELEGRAM_CHAT_ID`.
- Legacy pre-v0.2.0 short-code callback payloads (`rain_clr`, `flood_adv`, …)
  remain readable so buttons already in Telegram chats keep working.

### Documented
- Private-chat isolation versus shared-group visibility.
- Broadcast versus interactive response behavior.
- Latency interpretation and AI blocking notes.

## [0.2.0] - 2026-07-14

### Added
- Excel workbook (`templates/서울시_재난특보_X템플릿.xlsx`) as the human-managed
  template catalog source, synchronized into `config/message_templates.yaml`
  via `python -m app.commands.sync_templates_from_excel --check|--write`.
- Version-2 YAML catalog: 21 business templates (19 automation + 2 reference),
  plus isolated `system_templates` / `legacy_templates` sections.
- Two-stage Telegram selection menus (category → template) with `cat:` /
  `tpl:` / `back:` callbacks.
- Canonical ID resolver for historical Service v1 aliases
  (`HEAVY_RAIN_CLEARED` → `HW-05`, etc.).
- Preview badge `⚠️ 부서 검수 필요` for `review_required` templates.

### Changed
- Runtime wording now comes from the generated Excel snapshot (exact workbook
  text, URLs, and spacing preserved).
- Selection keyboards derive labels and category membership from YAML rather
  than a hardcoded 8-button layout.

## [0.1.1] - 2026-07-14

### Fixed
- Multi-user Telegram replies now return to the originating chat instead of
  going nowhere (`/latest`, ordinary text) or leaking to the configured
  broadcast chat.
- Callback previews and confirmations (template selection, preview
  confirm/cancel/AI) no longer leak to `TELEGRAM_CHAT_ID`; they always
  target the chat the button was actually clicked in.
- `/latest` and ordinary text no longer create a duplicate
  `template_suggestions` row on every replay — only a genuinely new
  poller-detected message persists one.
- Telegram `getUpdates` offset now survives a process restart instead of
  resetting to 0 in memory, preventing already-handled updates from being
  replayed.

### Security
- Fixed cross-chat response routing that could expose a requested disaster
  message, or another operator's template draft, to the wrong Telegram chat.
- Preview confirm/cancel/AI callbacks now require the callback's chat and
  user to match the chat/user the preview was created for, rejecting
  mismatches with a generic "이 초안이 생성된 Telegram 대화에서 다시 시도해
  주세요." reply instead of acting on the draft.

## [0.1.0] - 2026-07-13

Initial Service v1 release (prior work, documented here in compact form):

- Seoul SafeCity disaster-message collection, dedup, and retention (Part 1).
- Recurring local poller + unified Telegram bot (`/latest`, `/status`,
  `/pause`, `/resume`, `/help`) with SQLite WAL/busy_timeout hardening.
- Human-in-the-loop template selection: initial alert with 8 selection
  buttons, rule-based extraction, preview, and confirm/cancel/on-demand AI
  assist, with `template_decisions` as the durable ground truth surviving
  message retention.
- Generalized duplicate-callback guard across all callback types.

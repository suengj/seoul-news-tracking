# Changelog

## [Unreleased]

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

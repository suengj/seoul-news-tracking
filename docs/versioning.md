# Versioning

Semantic Versioning (`MAJOR.MINOR.PATCH`). `pyproject.toml`'s
`[project].version` is the single source of truth — `app/version.py` reads
it at runtime via `tomllib` (never hardcode the version string elsewhere).
It's surfaced in the Telegram bot's startup log and in `/status`.

- **PATCH**: backward-compatible bug and security fixes (e.g. 0.1.1: the
  cross-chat routing fix — no config/DB/API shape changed, an existing
  deployment just upgrades and restarts).
- **MINOR**: backward-compatible features (new command, new template, new
  optional column/setting with a safe default).
- **MAJOR**: breaking configuration, DB schema, or API changes (removing/
  renaming an env var, a migration that isn't purely additive, a changed
  Telegram command contract).

`0.4.0` (independent Telegram operators) is a **MINOR** release: the schema
migration is additive plus an idempotent, decision-preserving rebuild of
`template_decisions`; `TELEGRAM_CHAT_ID` is retained (now legacy-only) rather
than removed; and existing commands keep working (`/pause`→`/mute`,
`/resume`→`/unmute` aliases). See `docs/independent_operator_model.md`.

`0.4.1` is a **PATCH** hotfix: no schema, config, or Telegram command contract
change. It (1) filters the retry set by the current `TELEGRAM_ALLOWED_USER_IDS`
(`list_retryable_deliveries` gains an `allowed_user_ids` argument — a Database
method signature change only, internal to the app), (2) removes the pauser's
identity (`paused_by`) from the user-facing `/status`, and (3) adds a
local-only `poller_control` command to inspect/resume/pause the shared
collector. The v0.4.0 independent-operator architecture is unchanged.

`0.5.0` (live source migration) is a **MINOR** release: the retired Seoul
SafeCity `JSESSIONID`/XHR collector is replaced by the MOIS SafetyData API
(primary) with a conditional SafeKorea HTML fallback. New, purely additive
`system_state` columns (`last_collection_source`, `last_collection_source_at`,
`last_primary_error_category`, `source_cutover_at`,
`source_bootstrap_completed`); new settings with safe defaults
(`SAFETYDATA_*`, `SAFEKOREA_*`); existing commands, Telegram contract,
templates, and the historical database are unchanged. Requires running
`python -m app.commands.source_cutover_mois --bootstrap` once on any existing
(non-empty) database before the Poller will deliver — see
`docs/source_cutover_runbook.md`.

## Release steps

Same as before. For 0.4.0 / 0.4.1 specifically: run
`python -m app.commands.validate_telegram_behavior` (the extended
independent-operator PASS/FAIL block, now including `Retry authorization
filtering`, `Status privacy`, `Personal/global pause separation`, and `Local
poller control`), validate on a **copy** of the production DB (never the real
file), check the shared collector with `python -m app.commands.poller_control
status` (resume only if a legacy pause left it disabled), restart on merged
main, perform the two-operator private-chat check in
`docs/telegram_routing_validation.md`, then tag (`v0.4.0` / `v0.4.1`) only
after live validation succeeds.

For `0.5.0`: additionally run `python -m app.commands.inspect_mois_api`,
`python -m app.commands.inspect_safekorea_fallback`, and
`python -m app.commands.validate_source_pipeline`; validate cutover on a
**copy** of the production DB with `source_cutover_mois --inspect` /
`--bootstrap` before ever running bootstrap against the real file; confirm
the first post-cutover poll causes no historical send burst; then tag
`v0.5.0` only after live validation succeeds — see
`docs/source_cutover_runbook.md`.

1. Update/add tests for the change.
2. Update `CHANGELOG.md` (`[Unreleased]` -> a new dated version section).
3. Bump the version in `pyproject.toml`.
4. Open a PR; get it reviewed.
5. Merge to `main`.
6. Restart the local service on the merged `main` and perform live
   validation (see docs/service_v1.md "diagnosing repeated responses" for
   what to check after a routing-related change).
7. Only after live verification succeeds: create an annotated Git tag
   (`git tag -a vX.Y.Z`) and push it.
8. Record the release commit/tag in `CHANGELOG.md` or a completion report.

**Never tag a release before live verification.** A green test suite proves
the code is internally consistent; it does not prove the real bot token,
real chat IDs, and real operators behave as expected end-to-end.

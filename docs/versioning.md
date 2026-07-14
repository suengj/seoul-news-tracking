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

## Release steps

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

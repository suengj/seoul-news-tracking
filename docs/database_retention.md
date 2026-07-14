# Database Retention

## Settings

| Variable | Default | Accepted range | Meaning |
|---|---|---|---|
| `MESSAGE_RETENTION_DAYS` | 90 | 1–3650 | how long a collected message row is kept, measured from its source `sent_at` |
| `RUN_HISTORY_RETENTION_DAYS` | 14 | 1–365 | how long a `run_history` row is kept, measured from `started_at` |
| `TOMBSTONE_RETENTION_DAYS` | 365 | 1–3650 | how long a deleted message's dedup fingerprint is kept (see below) |
| `STORE_SUCCESSFUL_NOOP_RUNS` | false | boolean | whether a successful poll that found nothing new gets its own permanent history row |
| `CLEANUP_INTERVAL_HOURS` | 24 | 1–168 | minimum time between automatic cleanup runs |

All values are validated at startup (`app/config.py`); an out-of-range or
non-numeric value raises `ConfigError` with a clear message rather than
silently clamping or being ignored.

## Message cleanup

`MESSAGE_RETENTION_DAYS` is measured against **`sent_at`** (the source's own
timestamp for when the alert was sent), not `detected_at` (when this system
collected it) — `sent_at` is the meaningful "how old is this disaster
alert" business timestamp.

**Implication of deleting old message records:** once a message row is
deleted, `/latest`, `database_status`, and any future historical lookup can
no longer see it. This is intentional — Part 1/2 is a live-alerting tool,
not a permanent archive. If you need longer retention for analysis, raise
`MESSAGE_RETENTION_DAYS` (up to 3650) or export before cleanup runs.

### Why deletion doesn't cause re-notification

The collector only ever fetches Seoul SafeCity's *recent* records (see
`docs/source_discovery.md`), so a message old enough to be deleted by
retention is already far outside the window the source would ever return
again — in practice, re-notification risk is effectively zero.

As a defense-in-depth measure anyway, every deleted message first gets a
**tombstone**: a row in the `tombstones` table containing only
`source_id`, `raw_hash`, and `expired_at` — deliberately **not** the message
body, sender, or timestamp. `Database.is_known()` checks tombstones the
same way it checks the live `messages` table, so even if a since-deleted
record's ID or content hash ever reappeared in a poll response, it would
still be correctly recognized as already-seen and not re-sent to Telegram.
Tombstones are retained for `TOMBSTONE_RETENTION_DAYS` (default 365, longer
than message retention) and cleaned up separately.

## Run-history retention and noop suppression

Every `poll_once`/poller cycle used to write one permanent `run_history`
row, including cycles that found nothing new — at one cycle per minute
that grows without bound. With `STORE_SUCCESSFUL_NOOP_RUNS=false` (the
default), a run_history row for a cycle that was **successful, found zero
new records, and had zero Telegram failures** is deleted immediately after
being finalized instead of being kept.

Always kept, regardless of `STORE_SUCCESSFUL_NOOP_RUNS`:

- failed runs (collector errors, schema problems, etc.)
- successful runs that found at least one new message
- runs with at least one Telegram delivery failure
- `/pause` and `/resume` control events (logged via a dedicated
  `record_control_event` call, never suppressed)

All `run_history` rows — including these "always kept" ones — are still
subject to the age-based `RUN_HISTORY_RETENTION_DAYS` cleanup. "Always
kept" means "always created," not "kept forever."

## Expected DB growth

With defaults (90-day message retention, one poll every
`POLL_INTERVAL_SECONDS` = 300s, noop suppression on): `run_history` stays
small (only rows for actual new-message cycles/failures/control events —
realistically a handful per day, not one per poll cycle). `messages` grows
with actual disaster-message volume, bounded by 90 days of Seoul-wide
alerts (observed: dozens per day at most in initial live testing — see
`docs/part1_completion_report.md`). `tombstones` grows with deleted
messages but stores only three short text fields per row, so it stays
cheap even at a year's retention.

## Service v1 template tables are never pruned

`template_suggestions`, `template_actions`, `template_previews`,
`template_decisions`, and `ai_generations` are **not** touched by
`cleanup_preview()`/`cleanup_execute()` at all — only `messages`,
`run_history`, and `tombstones` are. This is deliberate, not an oversight:

- A confirmed `template_decisions` row is the future automation ground
  truth (see `docs/service_v1.md`) and must outlive the source message's
  own retention window, not just the same 90 days. Since v1.1, the row also
  carries an immutable snapshot of the source message at confirmation time
  (`source_id_snapshot`, `sender_or_region_snapshot`, `sent_at_snapshot`,
  `original_body_snapshot`) — populated by every `upsert_decision()` call —
  so even the **full original text** survives message deletion, not merely
  a `message_id` that would otherwise point at nothing. A database created
  before these columns existed gets them added automatically via
  `ALTER TABLE` on startup (`Database._migrate_schema`); pre-existing rows
  simply have `NULL` snapshots.
- Their `message_id` columns are declared as **plain indexed integers, not
  `REFERENCES messages(internal_id)` foreign keys** (see the module
  docstring in `app/database.py`). `PRAGMA foreign_keys=ON` is on for
  everything else's integrity, but an enforced FK here would make
  `cleanup_execute()` raise `FOREIGN KEY constraint failed` the first time
  it tried to delete a message that already had a decision — exactly the
  scenario this table exists to survive. `tests/test_database.py::
  test_confirmed_decision_survives_message_retention_cleanup` and
  `tests/test_template_flow.py::test_confirmed_decision_contains_source_snapshots`
  are the regression guards for this (the latter also asserts the full
  original body is still readable off the decision row after cleanup).
  `tests/test_database.py::test_snapshot_columns_migrate_onto_pre_change_schema`
  guards the `ALTER TABLE` migration itself against a pre-change database.
- If you need to prune these tables too (e.g. for storage on a long-running
  deployment), do it explicitly and separately — there is no
  `TEMPLATE_*_RETENTION_DAYS` setting in Service v1 by design; add one only
  when it's actually needed, not preemptively.

## How cleanup runs

- **Automatically**, inside the poller, at most once every
  `CLEANUP_INTERVAL_HOURS` (tracked via `system_state.last_cleanup_at`,
  which survives restarts — so restarting the poller frequently does not
  cause cleanup to run more often than configured).
- **Manually**, via `python -m app.commands.cleanup_database`:

```bash
python -m app.commands.cleanup_database --dry-run   # default; reports only, no changes
python -m app.commands.cleanup_database --confirm    # actually deletes
```

Dry-run reports message rows eligible, run-history rows eligible,
tombstone rows eligible, and the estimated post-cleanup row counts — and
is guaranteed not to write anything (verified in
`tests/test_cleanup_and_status_commands.py`).

Each cleanup step (tombstone inserts, message deletes, run-history
deletes, tombstone deletes) is its own short statement immediately
committed — consistent with the "no long-held write transactions" rule in
`docs/local_runtime.md`.

## Inspecting DB status

```bash
python -m app.commands.database_status
```

Reports file size, total/baseline message counts, counts by
`telegram_status`, oldest/newest `sent_at`, run-history count and span,
Service v1 template preview/decision/AI-generation row counts (see above —
these are never pruned), current retention settings, polling-enabled
state, last successful poll, last error, and cleanup eligibility counts at
current settings. This is a local CLI tool and does print the local
database path (useful for debugging); the Telegram `/status` command
deliberately does not.

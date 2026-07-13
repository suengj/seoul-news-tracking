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

With defaults (90-day message retention, one poll/minute, noop suppression
on): `run_history` stays small (only rows for actual new-message
cycles/failures/control events — realistically a handful per day, not
1440/day). `messages` grows with actual disaster-message volume, bounded
by 90 days of Seoul-wide alerts (observed: dozens per day at most in
initial live testing — see `docs/part1_completion_report.md`). `tombstones`
grows with deleted messages but stores only three short text fields per
row, so it stays cheap even at a year's retention.

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
current retention settings, polling-enabled state, last successful poll,
last error, and cleanup eligibility counts at current settings. This is a
local CLI tool and does print the local database path (useful for
debugging); the Telegram `/status` command deliberately does not.

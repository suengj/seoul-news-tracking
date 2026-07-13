# Historical Backfill Collector

Collects raw historical disaster-message records from the public archive at
`https://www.safetydata.go.kr/disaster-data/disasterNotification` into a
separate SQLite database, until the database confirms a target number of
unique records. See `docs/history_source_discovery.md` for how the source was
inspected and `docs/history_database.md` for the schema.

This collector performs **no filtering, classification, or transformation**.
It stores raw field values as extracted from the list and detail pages.

## Commands

```bash
# Read-only: confirm the source is still reachable and the parser still matches
python -m app.commands.inspect_history_source

# Start a new backfill run (fails if an active run already exists)
python -m app.commands.backfill_history --target-count 10000 --delay-seconds 1.5

# Resume the last paused/failed run (keeps its original target-count)
python -m app.commands.backfill_history --resume

# Print current run status without collecting
python -m app.commands.backfill_history --status

# Validate the database after a run
python -m app.commands.validate_history_db

# Export a small random sample for manual inspection (not the full dataset)
python -m app.commands.export_history_sample --count 100 \
    --output data/exports/history_sample.jsonl
```

`--target-count` and `--delay-seconds` default to `HISTORY_TARGET_COUNT` and
`HISTORY_REQUEST_DELAY_SECONDS` from the environment (see `.env.example`).

## Collection strategy

- The list endpoint (`GET /disaster-data/disasterNotification`) is paginated
  with `currentPage` (1 = most recent), `cntPerPage` (rows per page — 100 by
  default), and `pageSize` (UI page-link block size only).
- For every list row, the collector extracts the stable `sn` id from the
  detail link. IDs already present in `historical_raw_messages` are skipped
  without a request (`db.is_known_source_id`) — detail pages are **never**
  re-fetched for already-stored records.
- Each unseen id triggers exactly one `GET
  /disaster-data/disasterNotificationDetail?sn=<id>` request, which supplies
  the complete body, the extended region list, and the author field.
- `currentPage` increases by one after each fully-processed list page,
  moving from the newest records toward older ones — matching the task's
  "continue collecting older pages" requirement when duplicates or malformed
  records reduce the net insert rate on a given page.
- **The only stop condition is `SELECT COUNT(*) FROM historical_raw_messages`
  reaching the target** (`HistoryDatabase.unique_count()`), checked at the
  top of every iteration — never an in-memory counter, fetched-item count, or
  page count.

## Resume / interruption model

Progress is tracked in `historical_crawl_runs` (one row per run):

- A run starts in status `running`.
- Progress (`current_page`, `pages_processed`, `fetched_count`,
  `inserted_count`, `duplicate_count`, `malformed_count`, `error_count`,
  `retry_count`) is persisted after every fully-processed list page.
- `SIGINT`/`SIGTERM` set a flag checked before each list page and before each
  detail fetch; the in-flight record finishes, progress is persisted with
  status `paused`, and the process exits — no partial or truncated record is
  ever written.
- If a pause happens mid-page, `current_page` is persisted **unchanged** (not
  advanced), so `--resume` re-fetches the same list page. This is safe and
  idempotent: every row on that page whose `sn` is already stored is skipped
  again without a new detail request.
- `--resume` requires an existing run in `running`, `paused`, or `failed`
  status; starting a fresh run while one is still active is refused (use
  `--resume` or `--status` instead) to avoid two runs racing on the same
  target.
- A run only reaches status `completed` once the DB's unique row count meets
  the target, or status `failed` if list/detail requests are exhausted after
  bounded retries.

## Request behavior

- Sequential only — no concurrency.
- One reusable `httpx.Client` per run, plain GET requests, normal
  browser-like `User-Agent`, explicit connect/read timeout
  (`HISTORY_REQUEST_TIMEOUT_SECONDS`).
- A fixed delay (`HISTORY_REQUEST_DELAY_SECONDS`, accepted range 0.5–10s)
  follows every request (list or detail) before the next one is issued.
- Transport failures/timeouts retry up to `HISTORY_MAX_RETRIES` times with
  exponential backoff (`delay_seconds * 2^attempt`).
- HTTP 403/429 responses get a longer, escalating cooldown
  (`30s * (attempt + 1)`) rather than being retried immediately; if 403/429
  persists past the retry budget, the run stops with status `failed` and
  progress is preserved for a later `--resume` once the block clears.
- Only the two documented HTML endpoints are ever requested — no images,
  CSS, fonts, or other page assets.

## Progress reporting

Printed once per fully-processed list page (not per record):

```
pages=250 fetched=3,140 inserted=2,886 duplicates=241 malformed=13 target=10,000 progress=28.86% status=running
```

`DEBUG`-level per-request logging is available via `LOG_LEVEL=DEBUG` but is
not enabled by default.

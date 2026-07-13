# Historical Raw Database

A dedicated SQLite database, entirely separate from the real-time message
store (`app/database.py` / `data/seoul_news.db`). This keeps the 10,000-record
historical backfill from mixing with live-polling data and lets either be
reprocessed, rebuilt, or discarded independently.

- Default path: `data/history_raw.db` (git-ignored; never committed).
- Configurable via `HISTORY_DATABASE_PATH`.
- Managed by `app/history_database.py` (`HistoryDatabase`), schema in the
  module's `SCHEMA` constant, applied with `CREATE TABLE IF NOT EXISTS` /
  `CREATE INDEX IF NOT EXISTS` — safe to re-run against an existing file
  (see `tests/test_history_database.py::test_migration_is_idempotent`).
- Opened in SQLite WAL mode for safer concurrent reads (`--status`,
  `validate_history_db`) while a backfill is writing.

## `historical_raw_messages`

Raw field values only — no classification, no normalized "sender"/"disaster
type", no rewriting. `body_raw` is the complete original message text,
including embedded line breaks and symbols.

| Column | Notes |
|---|---|
| `internal_id` | autoincrement PK |
| `source` | fixed label identifying the collector/source, e.g. `safetydata.go.kr/disaster-data/disasterNotification` |
| `source_id` | the site's stable `sn` id, extracted from the detail link. `UNIQUE` |
| `sent_at_raw` | the site's own timestamp string (`yyyy/MM/dd HH:mm:ss`), verbatim |
| `sent_at` | best-effort Asia/Seoul parse of `sent_at_raw`, for indexing/sorting only — never authoritative over `sent_at_raw` |
| `sender_raw` | the detail page's "작성자" field (observed as `관리자`/admin for every sampled record — the platform's own attribution, not a disaster-issuing agency) |
| `region_raw` | the bracketed region list from the detail page's title line — more complete than what's embedded at the end of the message body |
| `title_raw` | the detail page's full title line (`<timestamp>[<region list>]`) |
| `body_raw` | the complete message body, `<br>` converted to `\n`, HTML entities decoded, otherwise untouched |
| `list_url` | the list endpoint URL |
| `detail_url` | the specific detail page URL for this record |
| `raw_payload` | small JSON of the extracted list+detail fields (not the full HTML page) |
| `raw_hash` | see below. `UNIQUE` |
| `collected_at` | when this collector inserted the row (Asia/Seoul, ISO 8601) |
| `source_page` | the `currentPage` value the record's id was discovered on |
| `source_position` | 1-based row position within that list page |

### Dedup / uniqueness

Two independent `UNIQUE` constraints, enforced by SQLite itself (not just
application logic):

1. `source_id` — always available for this source (the `sn` parameter).
2. `raw_hash` — a SHA-256 fallback, computed as:
   - `sha256("source_id:" + source_id)` when `source_id` is available (the
     common case here — content edits upstream between crawls don't create
     a false "new" record), or
   - `sha256(sender_raw + "\x1f" + sent_at_raw + "\x1f" + region_raw + "\x1f" + body_raw)`
     when no stable id exists (for schema generality/future sources).

A second attempt to insert a duplicate `source_id` or `raw_hash` raises
`DuplicateRecordError` (wrapping SQLite's `IntegrityError`); the collector
counts it as a duplicate rather than treating it as fatal.

### Indexes

`source_id` and `raw_hash` already get an implicit unique index from their
`UNIQUE` constraints. Explicit additional indexes:

- `idx_hist_sent_at` on `sent_at`
- `idx_hist_source_page` on `source_page`

## `historical_crawl_runs`

One row per backfill run (a fresh run when starting without `--resume`; the
same row is updated in place across pauses/resumes). See
`docs/history_backfill.md` for the full resume/interruption model. Columns:
`run_id`, `target_count`, `started_at`, `finished_at`, `current_page`,
`current_cursor`, `current_date_from`, `current_date_to`, `pages_processed`,
`fetched_count`, `inserted_count`, `duplicate_count`, `malformed_count`,
`error_count`, `retry_count`, `status`
(`running`/`paused`/`completed`/`failed`), `last_error`, `updated_at`.

`current_cursor`, `current_date_from`, and `current_date_to` are reserved for
a future date-range-driven crawl strategy; the plain-pagination strategy used
here only needs `current_page`.

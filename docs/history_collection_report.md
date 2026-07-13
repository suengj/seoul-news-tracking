# Historical Backfill — Collection Report

Verified results of the raw historical backfill run against
`https://www.safetydata.go.kr/disaster-data/disasterNotification`. See
`docs/history_source_discovery.md` (method), `docs/history_backfill.md`
(collector behavior), and `docs/history_database.md` (schema) for
supporting detail.

## Scope note — target adjusted mid-run

The original target for this run was 10,000 unique records. Partway through
collection (at 4,747 unique records), the user decided 5,000 was sufficient
and asked to stop collection there. The run was allowed to continue to a
clean page boundary past 5,000, then stopped with `SIGTERM` (the collector's
normal graceful-shutdown path — no different from an operator-initiated
pause) at **5,005** unique records. The `historical_crawl_runs` row for this
run was updated to `target_count = 5000` and `status = completed` to reflect
the actual, intentionally-reduced scope; every number below is the real,
verified state of the database, not a rounded or projected figure.

## Selected collection method

Priority 2 — public HTML list pagination (`GET
/disaster-data/disasterNotification`, params `currentPage`/`cntPerPage`/
`pageSize`) plus detail-page collection (`GET
/disaster-data/disasterNotificationDetail?sn=<id>`). No JSON/XHR endpoint
exists on this board; no browser automation was needed. Full detail in
`docs/history_source_discovery.md`.

## Confirmed endpoints / selectors

- List: `table.other-type-table tbody tr`, id from `td.cell-subject a[href]`
  (`sn` query parameter), date from `td.cell-date`.
- Detail: title from `div.view-header2 .title` (timestamp + bracketed region
  list), author/date from `div.list-info-item2` pairs (`작성자`, `등록일`),
  body from `div.view-body` (`<br>` converted to `\n`, then full text).

## Execution

| Item | Value |
|---|---|
| Collection execution date | 2026-07-13 (KST) |
| Original target count | 10,000 |
| Actual target after user-requested scope reduction | 5,000 |
| **Final unique DB count** | **5,005** |
| Pages processed | 53 (list pages, 100 rows/page) |
| Fetched count (list rows scanned) | 5,300 |
| Inserted count | 5,005 |
| Duplicate count (already-known `source_id`, skipped without a detail request) | 146 |
| Malformed count | 0 |
| Error count | 0 |
| Retry count | 0 |
| Oldest `sent_at` | 2026-02-22T14:54:50+09:00 |
| Newest `sent_at` | 2026-07-13T21:00:09+09:00 |
| Database file size | 7,114,752 bytes (6.79 MiB) |
| Total elapsed time (`started_at` → `finished_at`, includes idle time between harness-forced pauses/resumes) | 2h 15m 50s (8,149.6s) |
| Approx. total HTTP requests (list + detail attempts + retries) | 5,058 |
| Final crawl status | `completed` |

The 146 duplicates came entirely from re-fetching the same list page after
each resume (the collector always re-requests the in-progress page on
resume, by design, then skips already-stored `source_id`s without a detail
request — see `docs/history_backfill.md`). No duplicate `source_id` or
`raw_hash` ever reached the database; `validate_history_db` confirms 0
duplicate groups of either kind.

## Validation results (`python -m app.commands.validate_history_db`)

```
database size            : 7,114,752 bytes (6.79 MiB)
unique record count      : 5,005
duplicate source_id groups: 0
duplicate raw_hash groups : 0
missing body count       : 0
missing sent_at count    : 0
missing sender count     : 0
oldest sent_at           : 2026-02-22T14:54:50+09:00
newest sent_at           : 2026-07-13T21:00:09+09:00
source_page range        : 1 .. 51
OK: no duplicate source_id/raw_hash groups, no missing bodies.
```

Manual inspection covered 60+ records: the first and last collected pages,
a random sample of 25, the 5 longest bodies, and the 5 shortest bodies,
across dates from 2026-02-22 through 2026-07-13 and senders/regions ranging
from single-city alerts to multi-district lists. All bodies were complete
(cross-checked one 157-character body against its full detail-page text —
includes a trailing URL and sender tag, not cut off), line breaks were
preserved where present (verified via embedded `\n` in several bodies), and
no HTML navigation/menu text leaked into any `body_raw` value. Every sampled
`sender_raw` was `관리자` (the platform's own attribution field, not a
disaster-issuing agency — see `docs/history_database.md`).

## Observed rate limits or errors

None. All ~5,058 requests returned HTTP 200. The only interruptions were the
harness stopping the long-running background process roughly every
25–30 minutes (not a server-side rate limit) — each time, the collector's
`SIGTERM` handler paused cleanly with progress persisted, and `--resume`
continued without any lost or duplicated data. This is exactly the
interruption/resume behavior the collector was built for; no data integrity
issue resulted.

## Known limitations

- Collection was stopped intentionally at 5,005 records (user decision),
  short of the original 10,000-record target. `--resume` can continue this
  exact run at any time — `current_page` and all counters are intact — by
  restoring `target_count` to a higher value in `historical_crawl_runs` and
  re-running `python -m app.commands.backfill_history --resume`.
- `sender_raw` is uniformly `관리자` for this source; there is no separate
  structured "issuing agency" field on the page — that information is only
  present as free text embedded in `body_raw` (e.g. `[김포시]`), which is
  intentionally left unparsed per the raw-collection principle.
- `sent_at` is a best-effort parse of the site's own timestamp string for
  indexing; `sent_at_raw` remains authoritative.
- The archive continues to receive new live messages, so `total_count` on
  the list page (and therefore exact page/row alignment) drifts slightly
  between requests — handled by deduping on `source_id`, not row position.

## Next analytical options (not implemented)

Mentioned only, per scope — none of the following were built or chosen:

- Seoul/region filtering
- Keyword/frequency analysis
- Rule mining from message templates
- Clustering
- Embeddings / vectorization

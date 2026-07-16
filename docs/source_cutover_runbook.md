# Source Cutover Runbook (v0.5.0)

How to move a running v0.4.x deployment (Seoul SafeCity collector) onto the
v0.5.0 live source (행정안전부/MOIS SafetyData API primary, 국민안전24 HTML
conditional fallback) without a historical send burst, and how to diagnose
issues afterward. See `docs/live_source_migration_mois_api_plan.md`,
`docs/mois_api_contract_confirmed.md`, and
`docs/safekorea_html_fallback_plan.md` for the confirmed contract this
runbook assumes.

## 1. Why a cutover step is required at all

The old Seoul SafeCity collector's stable IDs and the new MOIS `SN` /
SafeKorea `bbsSn` are different identifier spaces. On a database that
already has historical messages (`is_empty == False`), every currently
visible MOIS/SafeKorea record would look "new" to a naive dedup check and
be sent to every subscriber as a fresh alert — a historical burst.

`source_cutover_mois --bootstrap` prevents this by registering the
currently-visible MOIS/SafeKorea window as a known baseline (same mechanism
the existing `is_baseline` flag already uses for a fresh v0.4.x install) —
without ever creating a `telegram_deliveries` row or a
`template_suggestions` row. The Poller (`app/commands/poll_once.py`) refuses
live automatic delivery (`--send`) on a non-empty database until
`system_state.source_bootstrap_completed = true`. A genuinely empty
(fresh-install) database does not need this step — its own first poll cycle
already establishes a safe baseline the same way v0.4.x always did.

## 2. Preconditions

1. `SAFETYDATA_SERVICE_KEY` is set in the local `.env` (never commit it).
2. The shared collector is paused: `python -m app.commands.poller_control
   status` / `pause`. `source_cutover_mois --bootstrap` refuses to run while
   `polling_enabled` is true — this is what keeps a rerun after go-live from
   silently absorbing a genuine new alert as an unsent "baseline" record
   instead of delivering it.
3. A consistent backup of the real SQLite DB + WAL/SHM exists (see §4).
4. `python -m app.commands.inspect_mois_api` and
   `python -m app.commands.inspect_safekorea_fallback` both pass and their
   results match the confirmed contract docs.
5. `python -m app.commands.validate_source_pipeline` passes offline.

## 3. Commands

```bash
# Read-only. Reports current source state, the current MOIS/fallback window,
# how many of those records already exist in the DB (exact source-ID,
# cross-source-ID, or raw-hash match — see §5), and whether bootstrap can
# proceed safely. Never writes anything.
python -m app.commands.source_cutover_mois --inspect

# Registers every currently-visible MOIS/SafeKorea Seoul record as a known
# baseline (is_baseline=1). Creates zero telegram_deliveries, zero
# template_suggestions. Idempotent: safe to rerun before the collector is
# resumed (it will just find everything already known); refuses outright
# once the collector is live again.
python -m app.commands.source_cutover_mois --bootstrap
```

A fresh (empty) database just gets `source_bootstrap_completed = true`
immediately, with nothing inserted — the normal first-poll-cycle baseline
path handles the rest.

## 4. DB-copy validation (always do this before touching the real DB)

```bash
cp data/seoul_news.db data/seoul_news.db-wal data/seoul_news.db-shm /path/to/copy/
DATABASE_PATH=/path/to/copy/seoul_news.db python -m app.commands.source_cutover_mois --inspect
DATABASE_PATH=/path/to/copy/seoul_news.db python -m app.commands.source_cutover_mois --bootstrap
DATABASE_PATH=/path/to/copy/seoul_news.db python -m app.commands.source_cutover_mois --bootstrap  # rerun: must be a no-op
```

Verify on the copy:

- original message count preserved (plus the newly registered baseline rows)
- `telegram_subscriptions`, `template_previews`, `template_decisions`,
  `ai_generations` row counts unchanged
- zero `telegram_deliveries` rows created by either bootstrap run
- zero `template_suggestions` rows created by either bootstrap run
- `system_state.source_bootstrap_completed = 1` and `source_cutover_at` set
- old (SafeCity-era) `source_id` values are still readable in `messages`

Never run `--bootstrap` against the real database until this passes.

## 5. Cross-source duplicate prevention (what "already known" means)

MOIS's `SN` and SafeKorea's `bbsSn` were empirically confirmed to be the
**same numeric id** for the same message (both systems read the same
underlying 긴급재난문자 record — see
`docs/safekorea_html_fallback_plan.md` §0). `Database.is_known()` therefore
checks every namespaced variant of a record's id
(`app.models.cross_source_equivalent_ids`) — `MOIS:12345` and
`SAFEKOREA:12345` are treated as the same message — in addition to the
existing `raw_hash` fallback. No canonical text fingerprinting is used: the
exact-ID match is simpler and strictly more reliable, since SafeKorea's list
HTML prepends the sending org name to the body (e.g. `[노원구] ...`) in a way
that makes `raw_hash` alone NOT match across sources.

This is why `source_cutover_mois --inspect` reports "canonical-fingerprint
matches: not used" rather than a fuzzy-match count.

## 6. Production cutover sequence

1. `python -m app.commands.poller_control pause` (if not already paused).
2. Stop the `seoulnews` tmux service cleanly; confirm no stale
   `run_local`/`run_poller`/`run_telegram_bot` processes remain.
3. Back up the real DB + WAL/SHM.
4. `python -m app.commands.inspect_mois_api`
5. `python -m app.commands.inspect_safekorea_fallback`
6. `python -m app.commands.source_cutover_mois --inspect`
7. `python -m app.commands.source_cutover_mois --bootstrap`
8. Verify: no Telegram messages were sent, no `telegram_deliveries` created,
   `source_bootstrap_completed = 1`.
9. Start the v0.5.0 service in tmux; `python -m app.commands.poller_control
   resume`.
10. Verify: version 0.5.0, all three processes alive, poll interval 300s, no
    HTTP 409 on the Telegram bot, last successful poll updates, recent
    source shown is `mois_safetydata_api` or `safekorea_html_fallback`.

## 7. Live validation after cutover

- **First post-cutover poll**: the current overlap window must not be
  resent — both operators should see no historical burst.
- **Interactive regression**: `/status`, `/latest`, `/history`, category and
  template selection, Preview cancel — independently for each operator.
- **New genuine message**: when the next real Seoul-targeted message
  appears, confirm each active operator gets exactly one independent
  delivery, and `/status`'s shared section shows the correct source.
- **Controlled fallback test**: temporarily point `SAFETYDATA_SERVICE_KEY`
  to an invalid value in a non-production run (never corrupt the real key)
  and confirm the SafeKorea fallback delivers correctly, then confirm API
  recovery does not resend that same message.

## 8. Diagnosing repeated fallback use

`/status`'s shared section shows `최근 수집 원천` (source) and `Primary 최근
오류` (category: `timeout` / `rate_limited` / `auth_failed` / `schema_error`
/ `http_error` / `unknown`) — never a request URL, never the service key,
never a message body. If the fallback is used repeatedly, check that
category first: `auth_failed` almost always means the service key needs
re-verification with `inspect_mois_api`; `rate_limited`/`timeout` may just
need `SAFETYDATA_NUM_OF_ROWS` lowered.

## 9. Resuming the shared collector after cutover

`python -m app.commands.poller_control resume`. This is the only supported
way to re-enable shared collection — Telegram `/resume` is a personal
unmute alias and never touches `system_state.polling_enabled`.

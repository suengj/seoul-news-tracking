"""Explicit source cutover between the retired Seoul SafeCity collector and
the v0.5.0 MOIS API / SafeKorea HTML fallback sources.

Usage:
    python -m app.commands.source_cutover_mois --inspect
    python -m app.commands.source_cutover_mois --bootstrap

Cross-source duplicate prevention (see docs/safekorea_html_fallback_plan.md
§0 and app/models.py `cross_source_equivalent_ids`) is exact-ID-based: MOIS's
`SN` and SafeKorea's `bbsSn` were empirically confirmed to be the same
numeric id for the same message, so `Database.is_known()` already treats
"MOIS:{id}" and "SAFEKOREA:{id}" as the same record. No fuzzy/canonical text
fingerprinting is needed or used.

`--inspect` is fully read-only. `--bootstrap` registers every
currently-visible MOIS/SafeKorea record as a known baseline (never sending
anything, never creating template_suggestions) and marks
`source_bootstrap_completed = true`, so the first real Poller cycle after
cutover does not resend the current window as a historical burst. Both are
idempotent; `--bootstrap` refuses to run while the shared collector is
enabled (`poller_control status/pause`), so it can only re-sweep the window
before the service actually goes live — never silently swallow a genuine
new alert once live delivery has resumed.
"""

from __future__ import annotations

import argparse
import sys

from app.collector import CollectionResult, CollectorError, fetch_records
from app.config import Settings, load_settings
from app.database import open_database
from app.logging_config import configure_logging


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--inspect", action="store_true", help="read-only report, no DB changes")
    group.add_argument(
        "--bootstrap",
        action="store_true",
        help="register the current MOIS/SafeKorea window as a known baseline (idempotent, never sends)",
    )
    return parser.parse_args(argv)


def _collect_current_window(settings: Settings) -> CollectionResult:
    return fetch_records(settings)


def run_inspect(settings: Settings) -> int:
    print("Source cutover inspection (read-only)\n")

    with open_database(settings.database_path) as db:
        state = db.get_system_state()
        print("=== current source state ===")
        print(f"  last_collection_source      : {state.last_collection_source or '(none yet)'}")
        print(f"  last_collection_source_at   : {state.last_collection_source_at or '(none yet)'}")
        print(f"  last_primary_error_category : {state.last_primary_error_category or '(none)'}")
        print(f"  source_bootstrap_completed  : {state.source_bootstrap_completed}")
        print(f"  source_cutover_at           : {state.source_cutover_at or '(not yet)'}")
        print(f"  shared collector polling_enabled: {state.polling_enabled}")
        db_is_empty = db.is_empty
        print(f"  database is_empty           : {db_is_empty}")

        print("\n=== current MOIS/fallback window ===")
        try:
            result = _collect_current_window(settings)
        except CollectorError as exc:
            print(f"  FAILED to collect: {exc}", file=sys.stderr)
            return 1

        print(f"  method                 : {result.method}")
        print(f"  fallback used          : {result.fallback_used}")
        print(f"  fetched (Seoul-filtered): {result.fetched_count}")

        exact_matches = 0
        unmatched: list = []
        for record in result.records:
            if db.is_known(record):
                exact_matches += 1
            else:
                unmatched.append(record)

        print("\n=== duplicate-prevention check against existing DB ===")
        print(f"  exact matches (source-ID, cross-source-ID, or raw-hash): {exact_matches}")
        print(f"  unmatched (would become new/baseline) candidates       : {len(unmatched)}")
        print(
            "  canonical-fingerprint layer: not used — cross-source identity is exact-ID-based "
            "(MOIS SN == SafeKorea bbsSn, confirmed live; see docs/safekorea_html_fallback_plan.md)."
        )
        print("  duplicate prevention is deterministic: True")

        if state.source_bootstrap_completed:
            print(
                "\nbootstrap already completed — rerunning --bootstrap is a safe idempotent no-op"
            )
            print("as long as the shared collector remains paused.")
        elif db_is_empty:
            print(
                "\ndatabase is empty (fresh install) — normal poll-cycle baseline behavior applies;"
            )
            print("--bootstrap will just mark the cutover complete without inserting anything.")
        elif state.polling_enabled:
            print("\nbootstrap can proceed safely: False — shared collector is still enabled.")
            print("  Run: python -m app.commands.poller_control pause")
        else:
            print(
                f"\nbootstrap can proceed safely: True "
                f"({len(unmatched)} record(s) would be registered as baseline, 0 sent)"
            )

    return 0


def run_bootstrap(settings: Settings) -> int:
    with open_database(settings.database_path) as db:
        state = db.get_system_state()

        # Refuse unconditionally while the shared collector is live — this is
        # what keeps a rerun after go-live from silently absorbing a genuine
        # new alert as an unsent "baseline" record instead of delivering it.
        if state.polling_enabled:
            print(
                "REFUSED: shared collector is still enabled. Run "
                "`python -m app.commands.poller_control pause` first.",
                file=sys.stderr,
            )
            return 1

        if db.is_empty:
            print("database is empty (fresh install) — no baseline registration needed.")
            db.mark_source_bootstrap_completed()
            print(
                "source_bootstrap_completed = true (fresh DB: normal poll-cycle baseline applies)."
            )
            return 0

        print("Collecting current MOIS/fallback window for baseline registration...")
        try:
            result = _collect_current_window(settings)
        except CollectorError as exc:
            print(f"FAILED to collect: {exc}", file=sys.stderr)
            return 1

        print(f"  method                  : {result.method}")
        print(f"  fetched (Seoul-filtered): {result.fetched_count}")

        inserted = 0
        already_known = 0
        for record in result.records:
            if db.is_known(record):
                already_known += 1
                continue
            db.insert(record, is_baseline=True)
            inserted += 1

        db.mark_source_bootstrap_completed()

        print(f"\nbaseline records inserted this run : {inserted}")
        print(f"already known (skipped)            : {already_known}")
        print("telegram_deliveries created         : 0 (bootstrap never sends)")
        print("template_suggestions created        : 0 (bootstrap never classifies)")
        print("source_bootstrap_completed          = true")
        print(
            "\nRun `python -m app.commands.poller_control resume` when ready to start live delivery."
        )

    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)

    if not settings.safetydata_service_key.strip():
        print(
            "SAFETYDATA_SERVICE_KEY is missing from the local .env. Add the "
            "approved key and rerun the API inspection.",
            file=sys.stderr,
        )
        return 1

    if args.inspect:
        return run_inspect(settings)
    return run_bootstrap(settings)


if __name__ == "__main__":
    raise SystemExit(main())

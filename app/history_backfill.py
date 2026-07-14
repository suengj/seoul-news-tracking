"""Resumable backfill orchestration: paginate the list, fetch unseen detail
pages, insert raw records, and persist crawl-run progress after every list
page (docs/history_backfill.md).

Stop condition is strictly the confirmed unique row count in SQLite
(`HistoryDatabase.unique_count()`), never in-memory counters, per the task's
raw-collection principle.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from app.history_collector import (
    HistoryCollectorError,
    RateLimitedError,
    build_client,
    fetch_detail_page,
    fetch_list_page,
)
from app.history_database import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_PAUSED,
    RUN_STATUS_RUNNING,
    DuplicateRecordError,
    HistoryDatabase,
)
from app.history_models import HistoricalRawRecord
from app.history_parser import HistoryParseError, parse_detail_page, parse_list_page, parse_sent_at

logger = logging.getLogger(__name__)

HISTORY_SOURCE_NAME = "safetydata.go.kr/disaster-data/disasterNotification"
LIST_URL = "https://www.safetydata.go.kr/disaster-data/disasterNotification"
DETAIL_URL = "https://www.safetydata.go.kr/disaster-data/disasterNotificationDetail"

DEFAULT_CNT_PER_PAGE = 100
DEFAULT_PAGE_SIZE = 10


class HistoryBackfillError(RuntimeError):
    pass


@dataclass
class BackfillProgress:
    run_id: int
    current_page: int
    pages_processed: int
    fetched_count: int
    inserted_count: int
    duplicate_count: int
    malformed_count: int
    error_count: int
    retry_count: int
    target_count: int
    unique_count: int
    status: str


ProgressCallback = Callable[[BackfillProgress], None]
StopCheck = Callable[[], bool]


def _build_record(
    *,
    row,
    detail,
    current_page: int,
) -> HistoricalRawRecord:
    sent_at_raw = detail.reg_date_raw or row.cell_date
    return HistoricalRawRecord(
        source=HISTORY_SOURCE_NAME,
        source_id=row.source_id,
        sent_at_raw=sent_at_raw,
        sent_at=parse_sent_at(sent_at_raw),
        sender_raw=detail.sender_raw,
        region_raw=detail.region_raw,
        title_raw=detail.title_raw,
        body_raw=detail.body_raw,
        list_url=LIST_URL,
        detail_url=f"{DETAIL_URL}?sn={row.source_id}",
        raw_payload={
            "list": {"cell_date": row.cell_date, "list_body_text": row.list_body_text},
            "detail": {"title": detail.title_raw, "reg_date": detail.reg_date_raw},
        },
        source_page=current_page,
        source_position=row.position,
    )


def run_backfill(
    db: HistoryDatabase,
    *,
    target_count: int,
    delay_seconds: float,
    timeout_seconds: float,
    max_retries: int,
    resume: bool,
    cnt_per_page: int = DEFAULT_CNT_PER_PAGE,
    page_size: int = DEFAULT_PAGE_SIZE,
    sleep_fn: Callable[[float], None] = time.sleep,
    should_stop: StopCheck = lambda: False,
    progress_cb: ProgressCallback | None = None,
    transport=None,
) -> BackfillProgress:
    active = db.find_active_run()

    if resume:
        if active is None:
            raise HistoryBackfillError("no active run found to resume; start a new run first")
        run_id = active["run_id"]
        target_count = active["target_count"]
        current_page = active["current_page"]
        pages_processed = active["pages_processed"]
        fetched_count = active["fetched_count"]
        inserted_count = active["inserted_count"]
        duplicate_count = active["duplicate_count"]
        malformed_count = active["malformed_count"]
        error_count = active["error_count"]
        retry_count = active["retry_count"]
    else:
        if active is not None:
            raise HistoryBackfillError(
                f"an active run already exists (run_id={active['run_id']}, "
                f"status={active['status']}); use --resume or --status"
            )
        current_page = 1
        pages_processed = fetched_count = inserted_count = 0
        duplicate_count = malformed_count = error_count = retry_count = 0
        run_id = db.start_run(target_count=target_count, current_page=current_page)

    status = RUN_STATUS_RUNNING
    last_error: str | None = None
    client = build_client(timeout_seconds=timeout_seconds, transport=transport)

    def _persist(*, page_for_resume: int, run_status: str) -> None:
        db.update_run_progress(
            run_id,
            current_page=page_for_resume,
            pages_processed=pages_processed,
            fetched_count=fetched_count,
            inserted_count=inserted_count,
            duplicate_count=duplicate_count,
            malformed_count=malformed_count,
            error_count=error_count,
            retry_count=retry_count,
            status=run_status,
            last_error=last_error,
        )

    def _snapshot(run_status: str) -> BackfillProgress:
        return BackfillProgress(
            run_id=run_id,
            current_page=current_page,
            pages_processed=pages_processed,
            fetched_count=fetched_count,
            inserted_count=inserted_count,
            duplicate_count=duplicate_count,
            malformed_count=malformed_count,
            error_count=error_count,
            retry_count=retry_count,
            target_count=target_count,
            unique_count=db.unique_count(),
            status=run_status,
        )

    try:
        while True:
            if db.unique_count() >= target_count:
                status = RUN_STATUS_COMPLETED
                _persist(page_for_resume=current_page, run_status=status)
                break

            if should_stop():
                status = RUN_STATUS_PAUSED
                _persist(page_for_resume=current_page, run_status=status)
                break

            try:
                list_result = fetch_list_page(
                    client,
                    current_page=current_page,
                    cnt_per_page=cnt_per_page,
                    page_size=page_size,
                    delay_seconds=delay_seconds,
                    max_retries=max_retries,
                    sleep_fn=sleep_fn,
                )
            except RateLimitedError as exc:
                status, last_error, error_count = RUN_STATUS_FAILED, str(exc), error_count + 1
                _persist(page_for_resume=current_page, run_status=status)
                break
            except HistoryCollectorError as exc:
                status, last_error, error_count = RUN_STATUS_FAILED, str(exc), error_count + 1
                _persist(page_for_resume=current_page, run_status=status)
                break

            retry_count += list_result.retry_count
            sleep_fn(delay_seconds)

            parsed_list = parse_list_page(list_result.html)
            fetched_count += len(parsed_list.rows)

            if not parsed_list.rows:
                status = RUN_STATUS_COMPLETED if db.unique_count() > 0 else RUN_STATUS_FAILED
                last_error = (
                    None if status == RUN_STATUS_COMPLETED else "list page returned zero rows"
                )
                _persist(page_for_resume=current_page, run_status=status)
                break

            stopped_mid_page = False
            for row in parsed_list.rows:
                if should_stop():
                    stopped_mid_page = True
                    break

                if db.is_known_source_id(row.source_id):
                    duplicate_count += 1
                    continue

                try:
                    detail_result = fetch_detail_page(
                        client,
                        source_id=row.source_id,
                        delay_seconds=delay_seconds,
                        max_retries=max_retries,
                        sleep_fn=sleep_fn,
                    )
                except RateLimitedError as exc:
                    status, last_error, error_count = RUN_STATUS_FAILED, str(exc), error_count + 1
                    stopped_mid_page = True
                    _persist(page_for_resume=current_page, run_status=status)
                    break
                except HistoryCollectorError as exc:
                    malformed_count += 1
                    error_count += 1
                    last_error = str(exc)
                    logger.warning("detail fetch failed for sn=%s: %s", row.source_id, exc)
                    sleep_fn(delay_seconds)
                    continue

                retry_count += detail_result.retry_count
                sleep_fn(delay_seconds)

                try:
                    detail = parse_detail_page(detail_result.html)
                except HistoryParseError as exc:
                    malformed_count += 1
                    logger.warning("malformed detail page for sn=%s: %s", row.source_id, exc)
                    continue

                record = _build_record(row=row, detail=detail, current_page=current_page)
                try:
                    db.insert_record(record)
                    inserted_count += 1
                except DuplicateRecordError:
                    duplicate_count += 1

                if db.unique_count() >= target_count:
                    break

            pages_processed += 1

            if stopped_mid_page and status == RUN_STATUS_RUNNING:
                status = RUN_STATUS_PAUSED
                _persist(page_for_resume=current_page, run_status=status)
                break

            if status != RUN_STATUS_RUNNING:
                break

            if db.unique_count() >= target_count:
                status = RUN_STATUS_COMPLETED
                _persist(page_for_resume=current_page, run_status=status)
                break

            next_page = current_page + 1
            _persist(page_for_resume=next_page, run_status=status)

            if progress_cb is not None:
                progress_cb(_snapshot(status))

            current_page = next_page

    finally:
        client.close()

    if status in (RUN_STATUS_COMPLETED, RUN_STATUS_FAILED):
        db.finish_run(run_id, status=status, last_error=last_error)

    final = _snapshot(status)
    if progress_cb is not None:
        progress_cb(final)
    return final

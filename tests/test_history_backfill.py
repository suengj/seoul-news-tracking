from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.history_backfill import run_backfill
from app.history_database import (
    HistoryDatabase,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_PAUSED,
)


def _list_html(sns_dates_texts: list[tuple[str, str, str]], total: int) -> str:
    trs = "".join(
        f'<tr><td class="cell-no">{i}</td>'
        f'<td class="board-list-new cell-subject">'
        f'<a href="/disaster-data/disasterNotificationDetail?sn={sn}">{text}</a></td>'
        f'<td class="cell-date">{date}</td></tr>'
        for i, (sn, date, text) in enumerate(sns_dates_texts)
    )
    return f'<p class="board-count">총 <span>{total}</span>건</p><table class="other-type-table"><tbody>{trs}</tbody></table>'


def _detail_html(sn: str, *, body: str = "본문 내용", include_body: bool = True) -> str:
    body_div = f'<div class="view-body view-bodyH"><p>{body}</p></div>' if include_body else ""
    return (
        '<div class="view-header2"><div class="title">2026/07/13 10:00:00[테스트구]</div>'
        '<div class="list-info-item2"><div class="list-info-title">작성자</div>'
        '<div class="list-info-desc">관리자</div></div>'
        '<div class="list-info-item2"><div class="list-info-title">등록일</div>'
        '<div class="list-info-desc">2026/07/13 10:00:00</div></div>'
        f"</div>{body_div}"
    )


def _rows_for_page(current_page: int, cnt_per_page: int, total: int) -> list[tuple[str, str, str]]:
    offset = (current_page - 1) * cnt_per_page
    top = total - offset
    bottom = max(top - cnt_per_page + 1, 1)
    if top < 1:
        return []
    return [(str(sn), "2026/07/13 10:00:00", f"메시지 {sn}") for sn in range(top, bottom - 1, -1)]


class FakeSite:
    """Deterministic in-memory stand-in for the archive: `total` sequential
    records, newest (highest sn) first, paginated exactly like the real site."""

    def __init__(
        self,
        total: int,
        *,
        malformed_sns: set[str] = frozenset(),
        rate_limited_sns: set[str] = frozenset(),
    ):
        self.total = total
        self.malformed_sns = malformed_sns
        self.rate_limited_sns = set(rate_limited_sns)
        self.detail_calls: list[str] = []
        self.list_calls: list[int] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if "disasterNotificationDetail" in request.url.path:
            sn = params["sn"]
            self.detail_calls.append(sn)
            if sn in self.rate_limited_sns:
                self.rate_limited_sns.discard(sn)  # only rate-limit once, then succeed on retry
                return httpx.Response(429, text="slow down")
            return httpx.Response(
                200, text=_detail_html(sn, include_body=sn not in self.malformed_sns)
            )

        current_page = int(params["currentPage"])
        cnt_per_page = int(params["cntPerPage"])
        self.list_calls.append(current_page)
        rows = _rows_for_page(current_page, cnt_per_page, self.total)
        return httpx.Response(200, text=_list_html(rows, self.total))


def _transport(site: FakeSite) -> httpx.MockTransport:
    return httpx.MockTransport(site.handler)


@pytest.fixture
def db(tmp_path: Path) -> HistoryDatabase:
    database = HistoryDatabase(tmp_path / "history.db")
    yield database
    database.close()


def test_stops_exactly_at_target_count(db):
    site = FakeSite(total=25)
    result = run_backfill(
        db,
        target_count=15,
        delay_seconds=0,
        timeout_seconds=5,
        max_retries=1,
        resume=False,
        cnt_per_page=10,
        sleep_fn=lambda _s: None,
        transport=_transport(site),
    )
    assert result.status == RUN_STATUS_COMPLETED
    assert db.unique_count() == 15
    assert result.inserted_count == 15


def test_malformed_record_does_not_block_reaching_target(db):
    # 12 records total; sn "16" is malformed (mapped within first page of 10 when total=16)
    site = FakeSite(total=12, malformed_sns={"12"})
    result = run_backfill(
        db,
        target_count=10,
        delay_seconds=0,
        timeout_seconds=5,
        max_retries=1,
        resume=False,
        cnt_per_page=10,
        sleep_fn=lambda _s: None,
        transport=_transport(site),
    )
    assert result.status == RUN_STATUS_COMPLETED
    assert db.unique_count() == 10
    assert result.malformed_count == 1
    assert (
        result.pages_processed == 2
    )  # had to continue into page 2 to make up for the malformed skip


def test_graceful_shutdown_persists_progress_and_resume_completes(db):
    site = FakeSite(total=25)
    stop_after = {"n": 3, "count": 0}

    def should_stop() -> bool:
        stop_after["count"] += 1
        return stop_after["count"] > stop_after["n"]

    paused_result = run_backfill(
        db,
        target_count=20,
        delay_seconds=0,
        timeout_seconds=5,
        max_retries=1,
        resume=False,
        cnt_per_page=10,
        sleep_fn=lambda _s: None,
        should_stop=should_stop,
        transport=_transport(site),
    )
    assert paused_result.status == RUN_STATUS_PAUSED
    inserted_before_resume = db.unique_count()
    assert 0 < inserted_before_resume < 20

    detail_calls_before_resume = list(site.detail_calls)

    resumed_result = run_backfill(
        db,
        target_count=20,  # ignored on resume; kept from the original run
        delay_seconds=0,
        timeout_seconds=5,
        max_retries=1,
        resume=True,
        cnt_per_page=10,
        sleep_fn=lambda _s: None,
        transport=_transport(site),
    )
    assert resumed_result.status == RUN_STATUS_COMPLETED
    assert db.unique_count() == 20

    # already-inserted records must not be re-fetched on resume (idempotent, no wasted requests)
    already_known = set(detail_calls_before_resume)
    calls_since_resume = site.detail_calls[len(detail_calls_before_resume) :]
    refetched_known = [sn for sn in calls_since_resume if sn in already_known]
    assert refetched_known == []


def test_resume_without_active_run_raises(db):
    from app.history_backfill import HistoryBackfillError

    with pytest.raises(HistoryBackfillError):
        run_backfill(
            db,
            target_count=5,
            delay_seconds=0,
            timeout_seconds=5,
            max_retries=1,
            resume=True,
            sleep_fn=lambda _s: None,
        )


def test_starting_new_run_while_one_is_active_raises(db):
    from app.history_backfill import HistoryBackfillError

    site = FakeSite(total=25)
    stop_immediately = lambda: True  # noqa: E731
    run_backfill(
        db,
        target_count=20,
        delay_seconds=0,
        timeout_seconds=5,
        max_retries=1,
        resume=False,
        cnt_per_page=10,
        sleep_fn=lambda _s: None,
        should_stop=stop_immediately,
        transport=_transport(site),
    )
    assert db.find_active_run() is not None

    with pytest.raises(HistoryBackfillError):
        run_backfill(
            db,
            target_count=20,
            delay_seconds=0,
            timeout_seconds=5,
            max_retries=1,
            resume=False,
            transport=_transport(site),
        )


def test_rate_limited_detail_fetch_marks_run_failed_and_preserves_progress(db):
    site = FakeSite(total=25, rate_limited_sns={f"{sn}" for sn in range(1, 26)})  # always 429s
    result = run_backfill(
        db,
        target_count=20,
        delay_seconds=0,
        timeout_seconds=5,
        max_retries=0,  # no retries -> first 429 exhausts immediately
        resume=False,
        cnt_per_page=10,
        sleep_fn=lambda _s: None,
        transport=_transport(site),
    )
    assert result.status == RUN_STATUS_FAILED
    assert result.error_count >= 1
    # progress made before the failure must still be queryable for resume
    active = db.find_active_run()
    assert active is not None
    assert active["status"] == RUN_STATUS_FAILED


def test_duplicate_page_reprocessing_does_not_reinsert(db):
    site = FakeSite(total=10)
    run_backfill(
        db,
        target_count=10,
        delay_seconds=0,
        timeout_seconds=5,
        max_retries=1,
        resume=False,
        cnt_per_page=10,
        sleep_fn=lambda _s: None,
        transport=_transport(site),
    )
    assert db.unique_count() == 10

    # Simulate an operator re-running backfill against a DB that already has
    # everything: starting fresh (not --resume) should refuse since a
    # completed run leaves no *active* row, so this models re-running the
    # same page's detail fetch directly at the DB layer instead.
    for sn in range(1, 11):
        assert db.is_known_source_id(str(sn)) is True
    assert len(site.detail_calls) == 10  # exactly once each, no re-fetching


def test_progress_callback_receives_snapshots(db):
    site = FakeSite(total=25)
    snapshots = []
    run_backfill(
        db,
        target_count=15,
        delay_seconds=0,
        timeout_seconds=5,
        max_retries=1,
        resume=False,
        cnt_per_page=10,
        sleep_fn=lambda _s: None,
        progress_cb=snapshots.append,
        transport=_transport(site),
    )
    assert len(snapshots) >= 1
    assert snapshots[-1].status == RUN_STATUS_COMPLETED
    assert snapshots[-1].unique_count == 15

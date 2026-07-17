"""행정안전부(MOIS) SafetyData 긴급재난문자 API (DSSP-IF-00247) collector.

Confirmed live contract — see docs/mois_api_contract_confirmed.md:

- Success envelope: {header: {resultCode, resultMsg, errorMsg}, numOfRows,
  pageNo, totalCount, body}. `body` is a flat list of item dicts, or `null`
  when `totalCount == 0` (valid empty, not an error).
- Failure envelope: {header: {resultCode != "00", resultMsg, errorMsg},
  body: null}. HTTP status is always 200 even on failure — the API never
  uses 401/403 for an unregistered key; failures are result-code driven.
  (A generic HTTP client error, e.g. connection-level 401/403 from an
  intermediary, is still handled defensively below.)
- `crtDt` is an inclusive lower bound ("from this date to now"), not a
  single-day filter, and MUST always be supplied — omitting it returns the
  entire historical dataset.
- Page ordering is unspecified; callers must sort by `sent_at` themselves.
- `rgnNm=서울특별시` was empirically confirmed to include every multi-region
  Seoul record in the inspected window, so it is used as a server-side
  optimization — RCPTN_RGN_NM is still the authoritative client-side filter.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import SAFETYDATA_API_URL, SAFETYDATA_DATASET_PAGE_URL, Settings
from app.models import DisasterMessageRecord, is_seoul_recipient

logger = logging.getLogger(__name__)

SEOUL_TZ = ZoneInfo("Asia/Seoul")

REQUIRED_FIELDS = ("SN", "CRT_DT", "MSG_CN", "RCPTN_RGN_NM")
CRT_DT_FORMAT = "%Y/%m/%d %H:%M:%S"

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0
MAX_RETRIES = 2
BACKOFF_BASE_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 10.0
MAX_RETRY_AFTER_SECONDS = 30.0
ABSOLUTE_MAX_PAGES = 100

SUCCESS_RESULT_CODE = "00"
# Empirically confirmed: "30" = SERVICE KEY IS NOT REGISTERED ERROR (see
# docs/mois_api_contract_confirmed.md). Other data.go.kr-style portals commonly
# use 20/22 for access-denied/quota; classified here defensively even though
# only "30" has been observed live, so /status reports "auth_failed" instead
# of a generic "schema_error" if one of these appears.
AUTH_FAILURE_RESULT_CODES = frozenset({"20", "22", "30", "31", "32"})


class MoisApiError(RuntimeError):
    """Base class for all MOIS API collection errors."""


class MoisConfigError(MoisApiError):
    """Missing/invalid local configuration. Never eligible for fallback — fail fast."""


class MoisRuntimeError(MoisApiError):
    """Hard runtime failure (network, HTTP, envelope, schema). Eligible for
    the same-cycle SafeKorea fallback."""


class MoisAuthError(MoisRuntimeError):
    """401/403 or an auth-failure result code. Classified separately so
    callers never hammer retries on a rejected key."""


class MoisSchemaError(MoisRuntimeError):
    """Envelope or record shape does not match the confirmed contract."""


def _kst_today() -> datetime:
    return datetime.now(tz=SEOUL_TZ)


def _crt_dt_for(lookback_days: int) -> str:
    return (_kst_today() - timedelta(days=lookback_days)).strftime("%Y%m%d")


def _build_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=READ_TIMEOUT, pool=READ_TIMEOUT
    )
    return httpx.Client(timeout=timeout, transport=transport)


def _request_page(
    client: httpx.Client,
    *,
    service_key: str,
    page_no: int,
    num_of_rows: int,
    crt_dt: str,
    use_rgn_filter: bool,
) -> dict[str, Any]:
    """Issue one page request with bounded retry/backoff, and return the
    validated success envelope's top-level dict. Never logs the key or the
    full query string. Raises MoisRuntimeError/MoisAuthError on hard failure."""
    params: dict[str, str] = {
        "serviceKey": service_key,
        "numOfRows": str(num_of_rows),
        "pageNo": str(page_no),
        "returnType": "json",
        "crtDt": crt_dt,
    }
    if use_rgn_filter:
        params["rgnNm"] = "서울특별시"

    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.get(SAFETYDATA_API_URL, params=params)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_exc = exc
            backoff = min(BACKOFF_BASE_SECONDS * (2**attempt), MAX_BACKOFF_SECONDS)
            logger.warning(
                "MOIS API request failed (attempt %d/%d): %s; backing off %.1fs",
                attempt + 1,
                MAX_RETRIES + 1,
                type(exc).__name__,
                backoff,
            )
            if attempt < MAX_RETRIES:
                time.sleep(backoff)
            continue

        if response.status_code in (401, 403):
            raise MoisAuthError(f"MOIS API rejected the request: HTTP {response.status_code}")

        if response.status_code == 429:
            retry_after_raw = response.headers.get("retry-after")
            try:
                retry_after = min(float(retry_after_raw), MAX_RETRY_AFTER_SECONDS)
            except (TypeError, ValueError):
                retry_after = min(BACKOFF_BASE_SECONDS * (2**attempt) * 4, MAX_RETRY_AFTER_SECONDS)
            logger.warning("MOIS API rate limited (429); backing off %.1fs", retry_after)
            if attempt < MAX_RETRIES:
                time.sleep(retry_after)
                continue
            raise MoisRuntimeError("MOIS API rate limited (429) after bounded retries")

        if response.status_code >= 500:
            backoff = min(BACKOFF_BASE_SECONDS * (2**attempt), MAX_BACKOFF_SECONDS)
            logger.warning(
                "MOIS API server error HTTP %d (attempt %d/%d); backing off %.1fs",
                response.status_code,
                attempt + 1,
                MAX_RETRIES + 1,
                backoff,
            )
            if attempt < MAX_RETRIES:
                time.sleep(backoff)
                continue
            raise MoisRuntimeError(
                f"MOIS API server error after retries: HTTP {response.status_code}"
            )

        if response.status_code == 400:
            raise MoisRuntimeError("MOIS API rejected the request: HTTP 400")

        if response.status_code != 200:
            raise MoisRuntimeError(f"MOIS API unexpected HTTP status: {response.status_code}")

        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            raise MoisRuntimeError(f"MOIS API returned non-JSON content-type: {content_type!r}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise MoisRuntimeError(f"MOIS API returned invalid JSON: {exc}") from exc

        if not isinstance(payload, dict) or "header" not in payload:
            raise MoisSchemaError("MOIS API response missing top-level 'header'")

        header = payload["header"]
        if not isinstance(header, dict) or "resultCode" not in header:
            raise MoisSchemaError("MOIS API response 'header' missing 'resultCode'")

        result_code = str(header["resultCode"])
        if result_code != SUCCESS_RESULT_CODE:
            message = (
                f"MOIS API result-code failure: resultCode={result_code!r} "
                f"resultMsg={header.get('resultMsg')!r}"
            )
            if result_code in AUTH_FAILURE_RESULT_CODES:
                raise MoisAuthError(message)
            raise MoisRuntimeError(message)

        return payload

    raise MoisRuntimeError(f"MOIS API request failed after retries: {last_exc}") from last_exc


def _parse_crt_dt(raw: str) -> datetime:
    try:
        naive = datetime.strptime(raw.strip(), CRT_DT_FORMAT)
    except (ValueError, AttributeError) as exc:
        raise MoisSchemaError(f"unparseable CRT_DT: {raw!r}") from exc
    return naive.replace(tzinfo=SEOUL_TZ)


def _validate_and_convert(raw: dict[str, Any], *, detected_at: datetime) -> DisasterMessageRecord:
    missing = [key for key in REQUIRED_FIELDS if key not in raw or raw[key] in (None, "")]
    if missing:
        raise MoisSchemaError(f"MOIS record missing required field(s): {missing}")

    sn = raw["SN"]
    sent_at = _parse_crt_dt(str(raw["CRT_DT"]))
    region = raw["RCPTN_RGN_NM"]

    return DisasterMessageRecord(
        source_id=f"MOIS:{sn}",
        sender_or_region=region,
        sent_at=sent_at,
        original_body=raw["MSG_CN"],
        source_url=SAFETYDATA_DATASET_PAGE_URL,
        detected_at=detected_at,
        raw_payload=dict(raw),
    )


def fetch_records(
    settings: Settings, *, transport: httpx.BaseTransport | None = None
) -> list[DisasterMessageRecord]:
    """Fetch, paginate, validate, and Seoul-filter current MOIS records.

    Raises MoisConfigError for a missing key (never fallback-eligible — the
    caller must fail fast). Raises MoisRuntimeError/subclasses for any hard
    runtime failure (the caller may fall back to SafeKorea). Returns an
    empty list for a valid zero-result response or a response with zero
    Seoul-targeted records after RCPTN_RGN_NM filtering — both are
    successful no-ops, not errors.

    `transport` is exposed only so tests can inject an httpx.MockTransport;
    production code never passes it.
    """
    service_key = settings.safetydata_service_key.strip()
    if not service_key:
        raise MoisConfigError(
            "SAFETYDATA_SERVICE_KEY is not configured; refusing to collect "
            "(missing key is a configuration failure, not a fallback trigger)"
        )

    crt_dt = _crt_dt_for(settings.safetydata_lookback_days)
    num_of_rows = settings.safetydata_num_of_rows
    detected_at = _kst_today()

    all_raw_items: list[dict[str, Any]] = []
    previous_page_sns: set[Any] | None = None

    with _build_client(transport) as client:
        for page_no in range(1, ABSOLUTE_MAX_PAGES + 1):
            envelope = _request_page(
                client,
                service_key=service_key,
                page_no=page_no,
                num_of_rows=num_of_rows,
                crt_dt=crt_dt,
                use_rgn_filter=True,
            )

            body = envelope.get("body")
            if body is None:
                # Valid empty page — either genuinely no more records, or
                # (page 1 only) a valid zero-result window.
                break
            if not isinstance(body, list):
                raise MoisSchemaError(
                    f"MOIS API 'body' must be a list or null, got {type(body).__name__}"
                )

            page_sns = {item.get("SN") for item in body if isinstance(item, dict)}
            if previous_page_sns is not None and page_sns and page_sns == previous_page_sns:
                raise MoisRuntimeError(f"MOIS API pagination loop detected at page {page_no}")
            previous_page_sns = page_sns

            all_raw_items.extend(body)

            total_count = envelope.get("totalCount")
            if isinstance(total_count, int) and len(all_raw_items) >= total_count:
                break
            if len(body) < num_of_rows:
                break
        else:
            raise MoisRuntimeError(
                f"MOIS API pagination exceeded max page guard ({ABSOLUTE_MAX_PAGES})"
            )

    records = [_validate_and_convert(raw, detected_at=detected_at) for raw in all_raw_items]

    # crtDt is documented (and empirically confirmed) as an inclusive lower
    # bound, so a record older than it is a live API contract violation, not
    # a genuinely new message. Observed live: a cycle that correctly sent
    # crtDt=20260716 got back records dated back to 2023-09 — the API
    # silently ignored its own filter. Without this guard those get treated
    # as brand-new and auto-delivered to every operator as a fresh alert.
    crt_dt_lower_bound = datetime.strptime(crt_dt, "%Y%m%d").replace(tzinfo=SEOUL_TZ)
    stale_count = sum(1 for r in records if r.sent_at < crt_dt_lower_bound)
    if stale_count:
        logger.warning(
            "MOIS API returned %d record(s) older than the requested crtDt=%s lower "
            "bound; dropping them as a live API contract violation, not new messages",
            stale_count,
            crt_dt,
        )
    records = [r for r in records if r.sent_at >= crt_dt_lower_bound]

    seoul_records = [r for r in records if is_seoul_recipient(r.sender_or_region)]
    seoul_records.sort(key=lambda r: r.sent_at)

    logger.info(
        "MOIS API collection: fetched=%d seoul=%d crtDt=%s",
        len(records),
        len(seoul_records),
        crt_dt,
    )

    return seoul_records

"""Collection orchestration: MOIS SafetyData API (primary) with a
conditional 국민안전24 HTML fallback within the same poll cycle.

v0.5.0 replaces the retired Seoul SafeCity session/XHR collector — see
docs/live_source_migration_mois_api_plan.md. The old
safecity.seoul.go.kr collector, `_bootstrap_session`, `JSESSIONID`, and
`/disstr/selectDisstrSms.do` are never called from here or anywhere else in
runtime code.

Fallback trigger policy (see docs/live_source_migration_mois_api_plan.md §16):

- primary success (with or without records, including zero Seoul records
  after RCPTN_RGN_NM filtering) -> use the API result, never fall back
- primary configuration failure (e.g. missing service key) -> fail fast,
  never fall back
- primary hard runtime failure -> one SafeKorea fallback attempt this cycle
- primary failure + fallback success (including a valid empty fallback
  result) -> use the fallback result
- primary failure + fallback failure (or fallback disabled) -> fail closed;
  no records, so no automatic Telegram send

The downstream Poller never needs to know which source produced a given
`DisasterMessageRecord` — both sources converge on the same normalized
model and `CollectionResult` contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app import mois_api, safekorea_fallback
from app.config import Settings, load_settings
from app.models import DisasterMessageRecord

logger = logging.getLogger(__name__)

METHOD_MOIS_API = "mois_safetydata_api"
METHOD_SAFEKOREA_FALLBACK = "safekorea_html_fallback"


class CollectorError(RuntimeError):
    """Raised for any condition that must abort collection rather than send
    incomplete/duplicate data: a primary configuration failure (fail fast,
    never fallback-eligible), or a primary hard failure with no working
    fallback (fail closed)."""


@dataclass
class CollectionResult:
    records: list[DisasterMessageRecord]
    method: str
    fetched_count: int
    full_text_confirmed: bool
    # Sanitized failure category for the primary attempt, e.g. "timeout",
    # "rate_limited", "auth_failed", "schema_error", "http_error" — never a
    # raw exception message that could contain a URL query string or key.
    primary_error_category: str | None = None
    fallback_used: bool = False


def _classify_primary_error(exc: Exception) -> str:
    """A short, secret-safe failure category for observability
    (/status, run_history) — never the raw exception text.

    Checks the exception type (and, for a retry-exhausted network failure,
    `__cause__` — the underlying `httpx.TimeoutException`/`ConnectError`,
    since `mois_api._request_page` re-raises `from last_exc`) before falling
    back to a string heuristic on the message, because the underlying
    exception's own message (e.g. `httpx.TimeoutException("boom")`) does not
    necessarily contain a recognizable word itself.
    """
    if isinstance(exc, mois_api.MoisAuthError):
        return "auth_failed"
    if isinstance(exc, mois_api.MoisSchemaError):
        return "schema_error"

    cause = exc.__cause__
    if isinstance(cause, (httpx.TimeoutException, httpx.ConnectError)):
        return "timeout"

    message = str(exc).lower()
    if "429" in message or "rate limit" in message:
        return "rate_limited"
    if "timeout" in message or "timed out" in message:
        return "timeout"
    if "http" in message:
        return "http_error"
    return "unknown"


def fetch_records(
    settings: Settings | None = None,
    *,
    mois_transport: httpx.BaseTransport | None = None,
    safekorea_transport: httpx.BaseTransport | None = None,
) -> CollectionResult:
    """Collect current Seoul-targeted disaster messages: MOIS API first,
    with a same-cycle SafeKorea HTML fallback only on a primary hard
    runtime failure. Never returns partial/best-effort data — either a
    successful CollectionResult (possibly with zero records, which is a
    valid no-op) or a raised CollectorError.

    `settings` defaults to `load_settings()` so existing call sites
    (`fetch_records()`) keep working unchanged. `mois_transport`/
    `safekorea_transport` are exposed only so tests can inject an
    `httpx.MockTransport`; production code never passes them.
    """
    settings = settings or load_settings()

    try:
        records = mois_api.fetch_records(settings, transport=mois_transport)
    except mois_api.MoisConfigError as exc:
        raise CollectorError(f"MOIS API configuration error: {exc}") from exc
    except mois_api.MoisApiError as exc:
        error_category = _classify_primary_error(exc)
        logger.warning("MOIS API hard failure (%s): %s", error_category, exc)

        if not settings.safekorea_fallback_enabled:
            raise CollectorError(
                f"MOIS API failed ({error_category}) and SafeKorea fallback is disabled"
            ) from exc

        try:
            fallback_records = safekorea_fallback.fetch_records(
                settings, transport=safekorea_transport
            )
        except safekorea_fallback.SafeKoreaError as fallback_exc:
            raise CollectorError(
                f"MOIS API failed ({error_category}) and SafeKorea fallback also failed: {fallback_exc}"
            ) from fallback_exc

        logger.warning(
            "collection degraded: using SafeKorea fallback after MOIS API failure (%s)",
            error_category,
        )
        return CollectionResult(
            records=fallback_records,
            method=METHOD_SAFEKOREA_FALLBACK,
            fetched_count=len(fallback_records),
            full_text_confirmed=True,
            primary_error_category=error_category,
            fallback_used=True,
        )

    return CollectionResult(
        records=records,
        method=METHOD_MOIS_API,
        fetched_count=len(records),
        full_text_confirmed=True,
        primary_error_category=None,
        fallback_used=False,
    )

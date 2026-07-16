"""Read-only, secret-safe contract inspection for the MOIS SafetyData
긴급재난문자 API (DSSP-IF-00247).

Usage:
    python -m app.commands.inspect_mois_api

This command makes real HTTP requests using the local SAFETYDATA_SERVICE_KEY
and prints only sanitized structural information: HTTP status, content-type,
JSON shape (keys/types), item-list path, field presence, and counts. It never
prints:

- the service key
- a request URL with its query string
- complete MSG_CN (message body) values
- complete RCPTN_RGN_NM (region) values

If SAFETYDATA_SERVICE_KEY is missing, this command stops immediately and
does not make any request — a missing key must never be inferred from
guessed envelope behavior.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import SAFETYDATA_API_URL, load_settings
from app.logging_config import configure_logging

SEOUL_TZ = ZoneInfo("Asia/Seoul")

EXPECTED_ITEM_FIELDS = {
    "SN",
    "CRT_DT",
    "MSG_CN",
    "RCPTN_RGN_NM",
    "EMRG_STEP_NM",
    "DST_SE_NM",
    "REG_YMD",
    "MDFCN_YMD",
}

# Fields whose *value* must never be printed in full.
FULLY_MASKED_FIELDS = {"MSG_CN"}
# Fields whose value is printed only as type/shape, never content.
TYPE_ONLY_FIELDS = {"RCPTN_RGN_NM"}
# Fields printed with a partial mask (format confirmation without exact value).
PARTIALLY_MASKED_FIELDS = {"CRT_DT", "REG_YMD", "MDFCN_YMD"}

# Small, fixed strategy set — never grows without redoing this inspection.
KEY_STRATEGIES = ("params_auto_encode", "raw_query_insert")

MAX_PAGES_FOR_COMPLETENESS_CHECK = 10
STRUCTURAL_NUM_OF_ROWS = 20
COMPLETENESS_NUM_OF_ROWS = 100


def _partial_mask(value: str) -> str:
    value = str(value)
    if len(value) <= 4:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 4)


def _classify_date_format(value: str) -> str:
    value = str(value)
    if re.fullmatch(r"\d{8}", value):
        return "YYYYMMDD (8 digits)"
    if re.fullmatch(r"\d{14}", value):
        return "YYYYMMDDHHMMSS (14 digits)"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return "YYYY-MM-DD"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", value):
        return "YYYY-MM-DD HH:MM:SS"
    if re.fullmatch(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", value):
        return "YYYY/MM/DD HH:MM:SS"
    if re.fullmatch(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d+", value):
        return "YYYY/MM/DD HH:MM:SS.fraction"
    return f"unrecognized format, length={len(value)}"


def _describe(obj: Any, depth: int, max_depth: int, path: str, out: list[str]) -> None:
    indent = "  " * depth
    if depth > max_depth:
        out.append(f"{indent}{path}: <max depth reached>")
        return

    if isinstance(obj, dict):
        out.append(f"{indent}{path} (object, {len(obj)} keys): {sorted(obj.keys())}")
        for key, value in obj.items():
            _describe(value, depth + 1, max_depth, f"{path}.{key}", out)
        return

    if isinstance(obj, list):
        out.append(f"{indent}{path} (array, len={len(obj)})")
        if obj:
            _describe(obj[0], depth + 1, max_depth, f"{path}[0]", out)
        return

    field_name = path.rsplit(".", 1)[-1].rsplit("[", 1)[0]
    if field_name in FULLY_MASKED_FIELDS:
        text = str(obj)
        out.append(f"{indent}{path}: {type(obj).__name__}, length={len(text)} (value masked)")
    elif field_name in TYPE_ONLY_FIELDS:
        out.append(f"{indent}{path}: {type(obj).__name__} (value not printed)")
    elif field_name in PARTIALLY_MASKED_FIELDS:
        out.append(
            f"{indent}{path}: {type(obj).__name__} = {_partial_mask(obj)} "
            f"(format: {_classify_date_format(obj)})"
        )
    else:
        text = str(obj)
        shown = text if len(text) <= 60 else text[:60] + "...(truncated)"
        out.append(f"{indent}{path}: {type(obj).__name__} = {shown!r}")


def describe_json_shape(payload: Any, max_depth: int = 5) -> str:
    out: list[str] = []
    _describe(payload, 0, max_depth, "$", out)
    return "\n".join(out)


def find_item_list_candidates(obj: Any, path: str = "$") -> list[tuple[str, list[dict]]]:
    """Recursively find lists-of-dicts whose keys overlap the documented
    item field set. Returns [(path, list_of_dicts), ...]."""
    candidates: list[tuple[str, list[dict]]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            candidates.extend(find_item_list_candidates(value, f"{path}.{key}"))
    elif isinstance(obj, list):
        if obj and isinstance(obj[0], dict):
            overlap = EXPECTED_ITEM_FIELDS & set(obj[0].keys())
            if len(overlap) >= 2:
                candidates.append((path, obj))
        for index, value in enumerate(obj[:1]):
            candidates.extend(find_item_list_candidates(value, f"{path}[{index}]"))
    return candidates


def _build_client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=15.0))


def _request(
    client: httpx.Client,
    *,
    service_key: str,
    strategy: str,
    page_no: int,
    num_of_rows: int,
    crt_dt: str,
    rgn_nm: str | None,
) -> httpx.Response:
    """Issue one request using the given key-encoding strategy.

    `params_auto_encode`: pass the key through httpx's normal params dict
    (httpx URL-encodes it). `raw_query_insert`: insert the key into the query
    string exactly as provided, unencoded, for keys that are already
    pre-encoded by the issuing portal. Never logs the key value under either
    strategy.
    """
    base_params: dict[str, str] = {
        "numOfRows": str(num_of_rows),
        "pageNo": str(page_no),
        "returnType": "json",
        "crtDt": crt_dt,
    }
    if rgn_nm is not None:
        base_params["rgnNm"] = rgn_nm

    if strategy == "params_auto_encode":
        return client.get(SAFETYDATA_API_URL, params={"serviceKey": service_key, **base_params})

    if strategy == "raw_query_insert":
        query = "&".join(f"{k}={v}" for k, v in base_params.items())
        url = f"{SAFETYDATA_API_URL}?serviceKey={service_key}&{query}"
        return client.get(url)

    raise ValueError(f"unknown strategy: {strategy}")


def _looks_like_auth_failure(response: httpx.Response) -> bool:
    if response.status_code in (401, 403):
        return True
    if response.status_code == 200:
        text = response.text[:2000]
        return any(
            marker in text
            for marker in ("SERVICE_KEY", "서비스키", "인증", "등록되지 않은", "UNREGISTERED")
        )
    return False


def _resolve_service_key_strategy(
    client: httpx.Client, *, service_key: str, crt_dt: str
) -> tuple[str, httpx.Response] | None:
    """Try each strategy once with a minimal request; return the first that
    succeeds (HTTP 200, JSON content-type, no auth-failure marker)."""
    for strategy in KEY_STRATEGIES:
        try:
            response = _request(
                client,
                service_key=service_key,
                strategy=strategy,
                page_no=1,
                num_of_rows=1,
                crt_dt=crt_dt,
                rgn_nm="서울특별시",
            )
        except httpx.HTTPError as exc:
            print(f"strategy {strategy}: request error ({type(exc).__name__})")
            continue

        content_type = response.headers.get("content-type", "")
        if _looks_like_auth_failure(response):
            print(f"strategy {strategy}: HTTP {response.status_code}, auth-failure marker detected")
            continue
        if response.status_code != 200:
            print(f"strategy {strategy}: HTTP {response.status_code}, rejected")
            continue
        if "json" not in content_type and "text" not in content_type:
            print(f"strategy {strategy}: unexpected content-type {content_type!r}")
            continue

        print(
            f"strategy {strategy}: HTTP {response.status_code}, content-type={content_type!r} -> OK"
        )
        return strategy, response

    return None


def _extract_sn_set(items: list[dict]) -> set[str]:
    return {str(item["SN"]) for item in items if "SN" in item}


def _region_contains_seoul(region: Any) -> bool:
    """Minimal local check for the inspection command only (not the
    production filter): does this RCPTN_RGN_NM value contain a 서울특별시
    token, handling both string and list representations."""
    if isinstance(region, list):
        tokens: list[str] = []
        for entry in region:
            tokens.extend(str(entry).split(","))
    else:
        tokens = str(region).split(",")
    tokens = [t.strip() for t in tokens]
    return any(t == "서울특별시" or t.startswith("서울특별시 ") for t in tokens)


def _collect_all_pages(
    client: httpx.Client,
    *,
    service_key: str,
    strategy: str,
    crt_dt: str,
    rgn_nm: str | None,
    num_of_rows: int,
    item_list_path: str,
    max_pages: int,
) -> tuple[list[dict], int]:
    """Paginate up to max_pages, returning (all_items, pages_fetched).
    Stops when a page returns fewer than num_of_rows items."""
    all_items: list[dict] = []
    for page_no in range(1, max_pages + 1):
        response = _request(
            client,
            service_key=service_key,
            strategy=strategy,
            page_no=page_no,
            num_of_rows=num_of_rows,
            crt_dt=crt_dt,
            rgn_nm=rgn_nm,
        )
        if response.status_code != 200:
            print(f"  page {page_no}: HTTP {response.status_code}, stopping pagination")
            break
        try:
            payload = response.json()
        except ValueError:
            print(f"  page {page_no}: non-JSON response, stopping pagination")
            break

        candidates = find_item_list_candidates(payload)
        matching = [c for c in candidates if c[0] == item_list_path]
        items = matching[0][1] if matching else (candidates[0][1] if candidates else [])
        all_items.extend(items)
        if len(items) < num_of_rows:
            return all_items, page_no
    return all_items, max_pages


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--crt-dt",
        default=None,
        help="override crtDt (YYYYMMDD) for testing against a window known to have records; "
        "defaults to KST yesterday",
    )
    return parser.parse_args(argv)


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

    yesterday_kst = args.crt_dt or (datetime.now(tz=SEOUL_TZ) - timedelta(days=1)).strftime(
        "%Y%m%d"
    )

    print("=== A. Basic structural request ===")
    print(f"endpoint      : GET {SAFETYDATA_API_URL}")
    print(f"crtDt         : {yesterday_kst}")
    print("rgnNm         : 서울특별시")
    print(f"numOfRows     : {STRUCTURAL_NUM_OF_ROWS}")
    print()

    with _build_client() as client:
        resolved = _resolve_service_key_strategy(
            client, service_key=settings.safetydata_service_key, crt_dt=yesterday_kst
        )
        if resolved is None:
            print(
                "FAILED: no key-encoding strategy succeeded. The key may be "
                "invalid, unapproved, or require a strategy not covered here.",
                file=sys.stderr,
            )
            return 1
        strategy, first_response = resolved
        print(f"\nconfirmed service-key strategy: {strategy}\n")

        response = _request(
            client,
            service_key=settings.safetydata_service_key,
            strategy=strategy,
            page_no=1,
            num_of_rows=STRUCTURAL_NUM_OF_ROWS,
            crt_dt=yesterday_kst,
            rgn_nm="서울특별시",
        )

        print(f"HTTP status   : {response.status_code}")
        print(f"content-type  : {response.headers.get('content-type', '')}")

        try:
            payload = response.json()
        except ValueError:
            print("FAILED: response is not valid JSON.", file=sys.stderr)
            print("first 500 chars (sanitized to structure only if HTML):", file=sys.stderr)
            return 1

        print("\n--- JSON shape (values sanitized) ---")
        print(describe_json_shape(payload))

        candidates = find_item_list_candidates(payload)
        print("\n--- item-list path candidates ---")
        if not candidates:
            print("NONE FOUND — no list-of-dicts overlapping documented item fields.")
        for path, items in candidates:
            first_keys = sorted(items[0].keys()) if items else []
            print(f"path={path}  count={len(items)}  item_keys={first_keys}")

        print("\n--- documented field presence (first item, if any) ---")
        if candidates:
            item_list_path, items = candidates[0]
            if items:
                first = items[0]
                for field in sorted(EXPECTED_ITEM_FIELDS):
                    present = field in first
                    value_type = type(first.get(field)).__name__ if present else "N/A"
                    print(f"  {field}: present={present} type={value_type}")
            else:
                item_list_path = candidates[0][0]
        else:
            item_list_path = None

        # --- C/D: envelope field detection (result code, total count, page metadata) ---
        print("\n--- envelope key scan (top 2 levels, non-item keys) ---")
        if isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(value, (dict,)):
                    for sub_key, sub_value in value.items():
                        if not isinstance(sub_value, (list, dict)):
                            print(f"  {key}.{sub_key} = {sub_value!r}")
                elif not isinstance(value, list):
                    print(f"  {key} = {value!r}")

        # --- E: ordering check across the fetched page ---
        print("\n--- ordering check (page 1, by CRT_DT) ---")
        if candidates and candidates[0][1]:
            items = candidates[0][1]
            crt_dts = [item.get("CRT_DT") for item in items if "CRT_DT" in item]
            if len(crt_dts) >= 2:
                ascending = all(a <= b for a, b in zip(crt_dts, crt_dts[1:]))
                descending = all(a >= b for a, b in zip(crt_dts, crt_dts[1:]))
                if descending and not ascending:
                    print("  page 1 appears newest-first (descending CRT_DT)")
                elif ascending and not descending:
                    print("  page 1 appears oldest-first (ascending CRT_DT)")
                else:
                    print("  ordering UNSPECIFIED/mixed — production must sort by sent_at itself")
            else:
                print("  fewer than 2 items on page 1 — cannot determine ordering from this sample")

        # --- section 8: server-side rgnNm completeness check ---
        print("\n=== B. Seoul filter completeness check (rgnNm vs no rgnNm) ===")
        if item_list_path is None:
            print("SKIPPED: no item-list path resolved from step A.")
        else:
            filtered_items, filtered_pages = _collect_all_pages(
                client,
                service_key=settings.safetydata_service_key,
                strategy=strategy,
                crt_dt=yesterday_kst,
                rgn_nm="서울특별시",
                num_of_rows=COMPLETENESS_NUM_OF_ROWS,
                item_list_path=item_list_path,
                max_pages=MAX_PAGES_FOR_COMPLETENESS_CHECK,
            )
            unfiltered_items, unfiltered_pages = _collect_all_pages(
                client,
                service_key=settings.safetydata_service_key,
                strategy=strategy,
                crt_dt=yesterday_kst,
                rgn_nm=None,
                num_of_rows=COMPLETENESS_NUM_OF_ROWS,
                item_list_path=item_list_path,
                max_pages=MAX_PAGES_FOR_COMPLETENESS_CHECK,
            )

            filtered_sns = _extract_sn_set(filtered_items)
            unfiltered_seoul_items = [
                item
                for item in unfiltered_items
                if _region_contains_seoul(item.get("RCPTN_RGN_NM"))
            ]
            unfiltered_seoul_sns = _extract_sn_set(unfiltered_seoul_items)
            multi_region_seoul = [
                item
                for item in unfiltered_seoul_items
                if isinstance(item.get("RCPTN_RGN_NM"), str)
                and "," in item["RCPTN_RGN_NM"]
                or isinstance(item.get("RCPTN_RGN_NM"), list)
                and len(item["RCPTN_RGN_NM"]) > 1
            ]

            missing_from_filter = unfiltered_seoul_sns - filtered_sns

            print(
                f"filtered (rgnNm=서울특별시) pages fetched : {filtered_pages}, total items: {len(filtered_items)}"
            )
            print(
                f"unfiltered pages fetched                 : {unfiltered_pages}, total items: {len(unfiltered_items)}"
            )
            print(f"unfiltered items with Seoul in RCPTN_RGN_NM: {len(unfiltered_seoul_items)}")
            print(f"  of which multi-region                   : {len(multi_region_seoul)}")
            print(f"Seoul SNs missing from rgnNm-filtered set  : {len(missing_from_filter)}")

            if len(unfiltered_seoul_items) == 0:
                print(
                    "\nINCONCLUSIVE: zero Seoul-region records in this date window "
                    "(crtDt is a single day and may legitimately have no messages). "
                    "Decision cannot be made from this sample alone."
                )
            elif missing_from_filter:
                print(
                    "\nDECISION: rgnNm=서울특별시 OMITS multi-region Seoul records. "
                    "Production must omit rgnNm and use client-side RCPTN_RGN_NM filtering only."
                )
            else:
                print(
                    "\nDECISION: rgnNm=서울특별시 includes every Seoul-containing "
                    "record found in this window. Production may use rgnNm=서울특별시 "
                    "as a server-side optimization, with client-side RCPTN_RGN_NM "
                    "validation still applied."
                )

        # --- F: page-size maximum probe (single bounded probe) ---
        print("\n=== C. Page-size probe ===")
        probe_response = _request(
            client,
            service_key=settings.safetydata_service_key,
            strategy=strategy,
            page_no=1,
            num_of_rows=min(settings.safetydata_num_of_rows, 1000),
            crt_dt=yesterday_kst,
            rgn_nm="서울특별시",
        )
        print(
            f"requested numOfRows={min(settings.safetydata_num_of_rows, 1000)} -> HTTP {probe_response.status_code}"
        )
        if probe_response.status_code == 200:
            try:
                probe_payload = probe_response.json()
                probe_candidates = find_item_list_candidates(probe_payload)
                if probe_candidates:
                    print(f"returned item count: {len(probe_candidates[0][1])}")
            except ValueError:
                print("probe response was not JSON")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

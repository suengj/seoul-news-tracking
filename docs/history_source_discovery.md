# History Source Discovery — 재난안전데이터공유플랫폼 (safetydata.go.kr)

Investigation date: 2026-07-13 (KST). All findings below were verified against
the live site with real HTTP requests (`curl`), not assumed or guessed.

## 1. Target page

- URL: `https://www.safetydata.go.kr/disaster-data/disasterNotification`
- This is the public "재난 문자" (disaster message) historical archive board
  under 재난안전데이터공유플랫폼. As of the investigation date it reports
  **54,375** total records (`총 <span>54375</span>건의 게시물이 검색되었습니다.`),
  a figure that grows continuously since the board also receives new live
  messages.

## 2. Method selection

A plain `curl -A "<UA>" <url>` GET returns a full server-rendered HTML page
(HTTP 200, `text/html;charset=UTF-8`) with the message list **already present
in the initial HTML** — there is no XHR/JSON call involved. The list, the
search form, and pagination are all classic server-rendered HTML with a
`<form name="frm" method="GET">` that resubmits with query parameters.

There is no JSON/XHR endpoint to reverse-engineer. Per the collection-method
priority in the task, this selects **priority 2: public HTML list pagination
plus detail-page collection**. Browser automation (priority 3) is not needed;
everything works with plain sequential HTTP GET requests.

## 3. List endpoint

- URL: `GET https://www.safetydata.go.kr/disaster-data/disasterNotification`
- Method: `GET` (confirmed via the page's own `<form name="frm" method="GET">`
  and the `movePage(currentPage, cntPerPage, pageSize)` JS function, which sets
  those three hidden form fields and calls `frm.submit()`).
- Pagination parameters (all query-string, all confirmed working via curl):
  - `currentPage` — 1-based page index. Page 1 = most recent records.
  - `cntPerPage` — rows returned per request. Confirmed working values: 10
    (page default), 100, 500, 1000. No error at 1000; used for the backfill
    to minimize total list requests (~55 requests to cover 54k rows instead
    of ~5,400).
  - `pageSize` — controls only the pagination *link block* size shown in the
    UI (how many page-number links appear); does not affect the number of
    rows returned. Kept at `10` (the page's own default) for parity with
    normal browsing.
- Date-range parameters (present on the search form, not required for a plain
  chronological backfill, documented for completeness):
  - `searchStartDttm`, `searchEndDttm` — free-text fields validated client-side
    against `yyyy-MM-dd`.
  - `keyword` — free-text search box.
  - `orderBy` — only one option exists in the `<select>` (등록일순 / by
    registration date), so it is not a real lever.
- No cookie or session is required. Each `curl` call above was made from a
  cookie-less, independent process and returned full data every time; the
  `JSESSIONID`/`clientid` cookies the server sets are not read back or
  validated.
- No authentication or login is required for this board.
- Response: HTTP 200, `Content-Type: text/html;charset=UTF-8`.
- **Important caveat on row numbering:** the visible `NO` column
  (`td.cell-no`) is a display-only row rank computed against the live total
  count at request time — it is **not** a stable record ID and drifts
  slightly between requests as new messages land on the board. It must not be
  used as `source_id`.
- Each list row provides: the row's display rank (`cell-no`, not stable), the
  message text as the link's visible text (`td.cell-subject a`), and the
  send timestamp (`td.cell-date`, format `yyyy/MM/dd HH:mm:ss`). The
  **detail-page URL and its `sn` query parameter are embedded in the link
  `href`** (`/disaster-data/disasterNotificationDetail?sn=<id>`) and are the
  stable, monotonically-increasing primary key for each message.

## 4. Detail endpoint

- URL: `GET https://www.safetydata.go.kr/disaster-data/disasterNotificationDetail?sn=<id>`
- Method: `GET`. Same no-cookie, no-auth behavior as the list page.
- Response: HTTP 200, `text/html;charset=UTF-8`.
- Confirmed fields on the detail page:
  - `div.view-header2 .title` — `"<yyyy/MM/dd HH:mm:ss>[<region 1>,<region 2>,...]"`.
    The bracketed region list here is **more complete** than what appears
    embedded at the end of the message body (which is often a short
    abbreviated sender/region tag) — this is the only place the fuller
    region list is available, so the detail page is fetched for every record.
  - `div.list-info-item2` pairs — "작성자" (author, observed as `관리자` for
    every sampled record — the platform's own attribution, not the sending
    agency) and "등록일" (registration timestamp, redundant with the list's
    `cell-date` and the title's leading timestamp).
  - `div.view-body.view-bodyH p` — the complete original message body,
    including embedded line breaks and symbols (`vo.la/...`, `☎`, HTML
    entities like `&gt;`, etc.). Verified against several sampled records
    (including a 91-character missing-person alert with an internal newline)
    that list-page text and detail-page body text match exactly for the
    records checked — the list never truncates, but the detail page is still
    the canonical, structurally-parsed source for `body_raw`, since it is the
    only place the extended region list (`region_raw`) is available.
- **Stable source ID**: yes — the `sn` query parameter, extracted from the
  detail link `href` on the list page. Used as `source_id`.
- **Full-message source**: the detail page (`div.view-body.view-bodyH`).

## 5. Required headers / cookies / session

- Required headers: only a normal browser-like `User-Agent`. No
  `X-Requested-With`, no auth headers, no CSRF token.
- Cookies: none required. The server issues `JSESSIONID`/`clientid` cookies
  on every response but they are never validated back; sequential
  cookie-less requests all returned HTTP 200 with full data.
- No CAPTCHA, WAF challenge, or login wall was encountered for either the
  list page or detail pages at normal request rates.

## 6. Summary table

| Aspect | Value |
|---|---|
| List endpoint | `GET /disaster-data/disasterNotification` |
| Detail endpoint | `GET /disaster-data/disasterNotificationDetail?sn=<id>` |
| Request method | GET (both) |
| Pagination params | `currentPage`, `cntPerPage`, `pageSize` |
| Date params (unused for plain backfill) | `searchStartDttm`, `searchEndDttm`, `keyword` |
| Required headers | User-Agent only |
| Cookie/session required | No |
| Full-message source | Detail page `div.view-body.view-bodyH` |
| Stable source ID | Yes — `sn` query parameter |
| HTTP status observed | 200 for all requests during investigation |
| Response format | `text/html;charset=UTF-8` (server-rendered) |
| robots.txt | `Allow: /` for all user agents |

## 7. Selected collection method

**Priority 2 — public HTML list pagination plus detail-page collection.**
No JSON/XHR endpoint exists on this board, so priority 1 does not apply, and
browser automation (priority 3) is unnecessary since everything is reachable
with plain sequential `httpx` GET requests.

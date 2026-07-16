# Source Discovery — Seoul SafeCity 재난문자 widget

> **RETIRED — 역사적 source-discovery 기록만 남긴다. v0.5.0 runtime에서 사용하지
> 않는다.**
>
> Seoul SafeCity(`safecity.seoul.go.kr`)의 재난문자 widget은 개편으로 더 이상
> 신뢰할 수 없는 source가 되어, v0.5.0부터 행정안전부 SafetyData Open API
> (`DSSP-IF-00247`, `app/mois_api.py`)를 primary로, 국민안전24 HTML
> (`app/safekorea_fallback.py`)을 조건부 fallback으로 완전히 대체했다.
> `app/collector.py`는 더 이상 이 문서의 `JSESSIONID`/XHR 방식을 호출하지
> 않는다. 최신 계약은 다음을 참고한다.
>
> - `docs/live_source_migration_mois_api_plan.md`
> - `docs/mois_api_contract_confirmed.md`
> - `docs/safekorea_html_fallback_plan.md`

Investigation date: 2026-07-13 (KST). All findings below were verified against
the live site with real HTTP requests (`curl`), not assumed.

## 1. Target page

- URL: `https://safecity.seoul.go.kr/news/dist/dust/newsDistDustList.page`
- This URL renders the full "재난안전뉴스" dashboard (`disaster_news_wrap`),
  not only fine-dust content. The right-hand column of that dashboard
  contains two boxes: **재난문자** (`#distMsg`) and **재난·안전뉴스**
  (`#distNews`). Only the 재난문자 box is in scope for this project.

## 2. Initial raw HTML (unauthenticated `curl` GET)

- A plain `curl -A "<UA>" <url>` returns a full server-rendered JSP page
  (~107 KB), HTTP 200, `Content-Type: text/html;charset=UTF-8`.
- The 재난문자 box exists in the initial HTML but is **empty** — it ships
  with a single placeholder row ("오늘 재난문자가 없습니다.") and is
  populated entirely by an inline `<script>` that runs on
  `$(document).ready(...)`. There is no server-side pre-render of the actual
  messages.
- The GET response sets a `JSESSIONID` cookie (`Set-Cookie`). This cookie
  turned out to be required for the data call (see §4).

## 3. Rendered DOM behavior (read from the page's own inline JS, since no
   browser automation was available in this environment — see "Method note"
   below)

The widget's rendering code (`LAYER_POPUP.RIGHT_NEWS_TEMPLATE.fn_getDistMsg`,
saved verbatim in `artifacts/source_discovery/widget_js_excerpt.txt`) builds
two `<div>`s per record and inserts the **same full message string**
(`sDistMsg`, taken directly from `res.sms[i].smsMsg` with only the leading
`[구청명]` tag split off) into both:

- `div.txt.msgDefault` — the single-line preview shown by default.
- `div.msgDetail` — the expanded view, hidden by default
  (`$(".msgDetail").hide()`) and shown when the row is clicked
  (`onclick="COMMON.fn_setDistMsg(this)"`).

Both nodes receive the complete, untruncated string in JS — the code never
calls `.slice()`, `.substring()`, or appends `...`. This means:

- **The visual "..." truncation is CSS-only** (line clamping /
  `overflow:hidden` on `.msgDefault`), applied to a DOM node that already
  contains the full text. `textContent`/`innerText` on `.msgDefault` would
  read the full string; clicking the row just swaps which sibling div is
  visible, it does not fetch anything new.
- This conclusion is drawn from static analysis of the page's own
  unminified source, not by executing it in a browser (no Playwright/Chromium
  was available in this environment; see Method note). It is treated as
  supporting evidence, not the primary proof — the primary proof is §4.

**Method note:** Playwright is not installed in this environment and could
not be installed without a large one-time browser download during this
session. Full-DOM confirmation (Step 1 of the required investigation) was
therefore done by reading the page's own inline rendering code rather than by
inspecting a live `document`. This is documented as a known limitation. It
does not weaken the conclusion below because §4 independently proves the full
text is already present, untruncated, in the JSON the widget itself
fetches — the DOM is provably downstream of that JSON and cannot contain
*less* text than it.

## 4. Network / XHR — the actual data source

Reading the same inline script identified the exact AJAX call the widget
makes on load and every 60 seconds thereafter:

```js
$.ajax({
    type: "post",
    url: G_S_CONTEXT_PATH + "/disstr/selectDisstrSms.do",
    async: true,
    dataType: "json",
    success: function(res) { /* res.sms[i].smsMsg, .disstrDate, .lctnNm, ... */ }
});
```

`G_S_CONTEXT_PATH` resolves to the empty string (app deployed at domain
root), so the live endpoint is:

```
POST https://safecity.seoul.go.kr/disstr/selectDisstrSms.do
```

This was reproduced outside the browser with plain `curl`/`httpx` — no
Playwright required for normal polling.

### Reproduction steps and required headers

| Attempt | Headers | Result |
|---|---|---|
| POST, no cookie, no special headers | UA only | **403 Forbidden** (WAF) |
| POST, cookie only | `Cookie: JSESSIONID=...` | **403 Forbidden** |
| POST, cookie + Referer | `Cookie`, `Referer` | **403 Forbidden** |
| POST, cookie + `X-Requested-With` | `Cookie`, `X-Requested-With: XMLHttpRequest` | **200 OK**, JSON body |

Conclusion: the WAF/JSP session filter requires **(a)** a valid `JSESSIONID`
obtained from a prior `GET` of any page on the domain, and **(b)**
`X-Requested-With: XMLHttpRequest`. `Referer` and `Content-Type` are not
required (tested with an empty POST body). This was re-verified with a
**fresh** session (new GET → new cookie → POST) to rule out a fluke.

- **HTTP method:** `POST`
- **Body:** none required (empty POST body works)
- **Required non-secret headers:**
  - `X-Requested-With: XMLHttpRequest`
  - a normal browser-like `User-Agent`
  - `Cookie: JSESSIONID=<value from a prior GET>` — this is a session
    artifact issued per-request by the server, not a secret credential; it
    is never committed and is re-acquired on every collector run.
- **Response Content-Type:** `application/json;charset=UTF-8`
- **Response shape:**

```json
{
  "sms": [
    {
      "disstrSmsSn": "DS00050621",
      "orgnlSn": "260812",
      "disstrDate": "2026/07/13 00:45:39",
      "lctnNm": "서울특별시 은평구 갈현동",
      "smsMsg": "현재 갈현로33가길 일대 한전 변압기 고장으로 정전 발생. ... [은평구]"
    }
  ]
}
```

A sanitized 2-record sample is stored at
`artifacts/source_discovery/sample_response.json`. This is public
disaster-alert information (the same text broadcast to citizens), not
personal or secret data.

### Field mapping

| Source field | Meaning | Used as |
|---|---|---|
| `disstrSmsSn` | Stable disaster-SMS serial number, e.g. `DS00050621` | `source_id` (stable — see §Dedup) |
| `orgnlSn` | Internal original sequence number | stored in `raw_payload` only |
| `disstrDate` | Send timestamp, `"YYYY/MM/DD HH:MM:SS"`, local Seoul time, **no timezone marker in the payload itself** | parsed and stored as tz-aware `Asia/Seoul` `sent_at` |
| `lctnNm` | Sending district/region, sometimes multiple comma-separated districts for city-wide alerts | `sender_or_region` |
| `smsMsg` | **Complete original message**, including trailing `[구청명]` tag, `▲` bullet symbols, embedded URLs, and literal `\r\n` line breaks | `original_body` (stored byte-for-byte, no rewriting) |

### Stable source ID

`disstrSmsSn` (e.g. `DS00050621`) is present on every observed record and is
monotonically increasing with `orgnlSn`. It is used directly as the stable
source ID. The SHA-256 fallback (sender + sent_at + full body) exists only
for the case a future response is ever missing this field.

### Pagination / "더보기" / refresh behavior

- A single call to `selectDisstrSms.do` returned **23 records** in the test
  run (recent days, not just 5) — the endpoint is not paginated per se; it
  returns "however many recent records the server currently holds" (server
  filters by date window server-side).
- The widget's own JS renders **all** returned records into `#distMsg` at
  once. "더보기" (`fn_moreDistMsg`) does **not** issue a second request — it
  only toggles a CSS class (`.more`) that expands the box's height so the
  already-rendered rows (previously reachable only by scrolling within a
  fixed-height box) become fully visible, and simultaneously hides the
  sibling 재난·안전뉴스 box. So "5개 더보기" is a pure CSS/layout affordance,
  not a separate data-fetching flow.
- There is no dedicated manual "refresh" button element in the DOM;
  refresh is automatic — the page re-calls the same endpoint every 60
  seconds via `setInterval`. The collector reproduces this behavior via
  scheduled polling (Part 2), and `poll_once` reproduces a single refresh
  cycle.
- Because a single response already contains the full recent window and any
  overlap with previous polls, the dedup layer (source_id / hash) is what
  actually prevents duplicate delivery — not request-level pagination logic.

### Collection-method priority selected

1. **Selected: direct dedicated data endpoint** — `POST
   /disstr/selectDisstrSms.do`. Stable, reproducible outside the browser,
   returns full untruncated text with a stable ID, requires only a cheap
   session bootstrap (one GET) plus one header. This is used for all normal
   polling (`httpx`, no browser).
2. **Fallback (not currently needed):** if the endpoint's header/session
   requirements change and start rejecting the collector, the next fallback
   is scraping the rendered `#distMsg` HTML fragment after executing the
   page's own inline JS (confirmed in §3 to already contain full text) via
   headless-browser automation. This is intentionally not implemented in
   Part 1 per the "browser automation only when lighter methods are
   impossible" rule.
3. Browser automation (Playwright) was not required and was not used for
   normal polling. It remains available for future re-discovery if the site
   changes.

### Known fragility

- The WAF appears to key off `X-Requested-With` plus a live `JSESSIONID`;
  if Seoul adds stricter bot detection (e.g. requiring a Referer chain, a
  CSRF token minted per page load, or JS-challenge cookies), this endpoint
  could start returning 403 and the collector must fail loudly (it already
  treats HTTP 403/429 as hard errors — see `app/collector.py`).
  the fallback in §Collection-method priority.
- `lctnNm` formatting (trailing spaces, comma-joined multi-district strings)
  is not normalized by the source; it is stored as-is per the
  "preserve original" requirement.
- `disstrDate` has no explicit timezone marker; it is assumed to be Seoul
  local time (consistent with the server's own `Date` header and the site's
  Korean-only audience) and is localized to `Asia/Seoul` on parse.
- Response record count varies (observed 23 in one poll); nothing in the
  payload guarantees a maximum or minimum count, so the collector must not
  assume a fixed page size.

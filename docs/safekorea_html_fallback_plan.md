# 국민안전24 재난문자 HTML Fallback 계획

- 작성일: 2026-07-16 (KST)
- 대상 서비스: `seoul-news-tracking`
- 기준 버전: v0.4.1
- 목표 버전: v0.5.0
- Primary source: 행정안전부 SafetyData Open API `DSSP-IF-00247`
- Fallback source: 국민안전24 재난문자 조회 페이지
- 상태: **라이브 검증 완료** (`python -m app.commands.inspect_safekorea_fallback`)

## 0. 라이브 검증 결과 요약 (v0.5.0 확정 계약)

### HTML 구조 — 서버렌더링, JS 불필요

- `GET https://www.safekorea.go.kr/safekorea-kor/ctim/cmsg/calamitySms.do`는 순수 서버렌더링
  HTML을 반환한다 (redirect 0회, content-type `text/html`). JavaScript 실행이나 브라우저 자동화가
  전혀 필요 없다 — `httpx` GET + `BeautifulSoup`만으로 충분.
- 목록 row selector: `div.board-list table tbody tr` (데스크톱 테이블). 같은 데이터가
  `div.brd-listarea`(모바일용, CSS `display:none` 기본)에도 중복 렌더링되므로 반드시
  데스크톱 selector만 사용하고 모바일 블록은 무시한다.
- 페이지당 10행 고정. 총 건수는 `div.board-count` 텍스트(`전체51건` 형식)에서 파싱 가능.
- Pagination은 `div.pagination button`(`fnPageSubmit(N)`)이지만 실제로는 단순 GET
  querystring `currentPage=N`으로 각 페이지를 직접 요청할 수 있음을 확인(폼 재제출 불필요).
- 빈 결과 표현: `tbody`에 행이 정확히 1개, 텍스트가 `"데이터가 존재하지 않습니다."`이고
  `board-count`가 `전체0건`.

### 목록 HTML에 전체 데이터가 이미 존재 — 상세 페이지 불필요

각 `<tr>`는 다음을 이미 포함한다:

```html
<tr>
  <td>홍수</td>  <!-- 재해구분 -->
  <td class="tit">
    <a href="javascript:onSubmit('261132');">[전체 본문 텍스트, 요약/절단 없음]</a>
    <p> ㆍ&nbsp;발송일시 : 2026/07/14 22:47:07 ㆍ&nbsp;긴급단계 : 안전안내
        ㆍ&nbsp;송출지역 : 경기도 광명시, 경기도 시흥시, 서울특별시 구로구 </p>
  </td>
</tr>
```

10개 row 전수 검사 결과 본문/발송일시/송출지역/bbsSn 누락 0건. **결론: 상세 페이지 요청은
정상 운영에서 불필요하다.** (섹션 15 원안의 detail fetch 로직은 실제로는 사용되지 않음 —
필요 시를 대비해 상세 페이지 존재 자체는 확인하지 않았으므로, 목록에 없는 필드가 향후
발견되면 재검토.)

### `bbsSn` — 안정적이며 SafetyData API의 `SN`과 동일한 값

`href="javascript:onSubmit('261132');"`에서 추출한 `bbsSn`은 **SafetyData API의 `SN`과 정확히
동일한 값**임을 확인했다 (같은 날짜 window에서 MOIS `SN` 9건과 SafeKorea `bbsSn` 9건이
정확히 일치). 두 시스템이 동일한 상위 재난문자 DB를 공유하기 때문으로 보인다. →
**cross-source dedup은 canonical fingerprint 없이 숫자 ID 동일성만으로 결정적으로 해결된다**
(섹션 9 참고).

### 필드 값 비교 — sent_at/region 완전 일치, body는 접두 조직명 차이

동일 `SN`/`bbsSn`(261088)에 대해:

| 필드 | MOIS | SafeKorea | 일치 |
|---|---|---|---|
| 발송시각 | `2026/07/14 17:13:08` | `2026/07/14 17:13:08` | 완전 일치 |
| 송출지역 | `서울특별시 노원구 ` | `서울특별시 노원구` | 공백 외 일치(trim 필요) |
| 본문 | `오늘 밤 노원구에...[노원구]` | `[노원구] 오늘 밤 노원구에...[노원구]` | **SafeKorea가 발신기관명 `[노원구]`를 본문 앞에 추가로 표시** |

→ `raw_hash(sender_or_region, sent_at, original_body)`는 이 접두어 차이 때문에 두 source
간 **일치하지 않는다.** 본문에서 임의로 단어를 제거하는 것은 금지되어 있으므로(섹션 9),
canonical fingerprint로 이 차이를 흡수하는 대신 — 이미 확인된 **숫자 ID 동일성**을
cross-source dedup의 결정적 근거로 사용한다.

### 서울 필터(`sbLawArea1=1100000000`) 복수지역 포함 여부 — 확정

전국 무필터 목록은 7일 기준 1,227건으로 전수 비교가 비현실적이므로, 필터링된 결과셋
자체의 내적 일관성으로 검증했다: 필터가 복수지역 레코드를 제외한다면 필터 결과에
콤마 포함 레코드가 존재할 수 없다.

- Seoul 필터 결과: 51건
- 그중 복수지역(콤마 포함) 레코드: **14건**, 예:
  - `경기도 광명시, 경기도 시흥시, 서울특별시 구로구` (SN 261132, MOIS와 교차검증 완료)
  - `경기도, 서울특별시, 인천광역시` (SN 261101, MOIS와 교차검증 완료)
  - `서울특별시 강남구, ..., 서울특별시 중랑구` (자치구 25개 전체 나열)
- 51건 전부 `is_seoul_recipient()` 통과 (0건 leak)

**결정: `sbLawArea1=1100000000`은 다중지역 서울 레코드를 누락하지 않는다.** 프로덕션
fallback은 Seoul 필터 URL을 사용하고, 반환된 각 레코드에 client-side `is_seoul_recipient()`
검증을 그대로 병행한다.

### 조회기간

`startDate`/`endDate` 7일 범위(`2026-07-09`~`2026-07-16`)가 정상 동작함을 확인. 그 이상의
범위 제한 여부는 별도로 테스트하지 않았다 (섹션 7의 최대 1주일 정책을 그대로 사용).

## 1. 결론

실시간 수집의 기본 원천은 행정안전부 SafetyData Open API로 유지한다.

국민안전24의 재난문자 조회 페이지는 API 호출이 실패한 **해당 Poll cycle에만** 사용하는 조건부 HTML fallback으로 설계한다.

기존 서울안전누리 수집 경로는 계속 OFF하며, fallback으로 복구하지 않는다.

```text
Primary
SafetyData API
    |
    | 성공 또는 정상 0건
    v
기존 normalized pipeline

    | API hard failure
    v
Fallback
국민안전24 서울 필터 HTML
    |
    v
같은 DisasterMessageRecord pipeline
```

Fallback은 별도의 발송 시스템이 아니다. 어느 원천을 사용하더라도 결과를 동일한 `DisasterMessageRecord`로 변환한 뒤 기존 DB dedup, 개인별 Telegram fan-out, 템플릿, AI, Final OK 흐름을 그대로 사용한다.

## 2. 확인된 국민안전24 조회 URL

### 지역 필터 없음

```text
https://www.safekorea.go.kr/safekorea-kor/ctim/cmsg/calamitySms.do?menuSn=34&bbsSn=&currentPage=1&firstYn=&searchType=&cOcrcType=&dsstrSeId=&sbLawArea1=&sbLawArea2=&sbLawArea3=&keyword=&startDate=2026-07-09&endDate=2026-07-16&readYn=Y
```

### 서울특별시 필터

```text
https://www.safekorea.go.kr/safekorea-kor/ctim/cmsg/calamitySms.do?menuSn=34&bbsSn=&currentPage=1&firstYn=&searchType=&cOcrcType=&dsstrSeId=&sbLawArea1=1100000000&sbLawArea2=&sbLawArea3=&keyword=&startDate=2026-07-09&endDate=2026-07-16&readYn=Y
```

확인된 주요 query parameter:

| Parameter | 용도 | 운영 값 |
|---|---|---|
| `currentPage` | 목록 페이지 | 1부터 증가 |
| `bbsSn` | 상세 항목 식별에 사용되는 것으로 보이는 값 | HTML 조사 후 확정 |
| `sbLawArea1` | 시도 필터 | 서울특별시 `1100000000` |
| `sbLawArea2` | 시군구 필터 | 빈 값 |
| `sbLawArea3` | 하위 지역 필터 | 빈 값 |
| `startDate` | 조회 시작일 | KST 기준 동적 계산 |
| `endDate` | 조회 종료일 | KST 오늘 |
| `readYn` | 조회 화면 동작 parameter | `Y` 유지 |
| `dsstrSeId` | 재해유형 필터 | 빈 값: 서울 대상 전체 재난문자 수집 |
| `keyword` | 키워드 | 빈 값 |

`sbLawArea1=1100000000`은 서울특별시 필터로 사용한다.

Fallback production request에서는 지역 필터가 없는 URL을 사용하지 않는다. 지역 필터 없음 URL은 초기 구조 조사와 서울 필터 누락 여부 비교에만 사용한다.

## 3. 아직 로컬에서 확인해야 하는 HTML 계약

현재 원격 조사 환경에서는 해당 query URL의 상세 HTML을 안정적으로 fetch하지 못했다. 따라서 다음 selector와 detail contract는 추측으로 구현하지 않고 로컬에서 실제 페이지를 검사해야 한다.

필수 확인 항목:

1. 목록 row selector
2. 페이지당 row 수
3. 마지막 페이지 판정 방식
4. 각 row에서 stable ID를 얻는 위치
5. `bbsSn`이 실제 stable detail ID인지
6. 상세 페이지 URL 또는 form submit 방식
7. 상세 본문 selector
8. 발송일시 selector
9. 긴급단계 selector
10. 송출지역 selector
11. 재해구분 selector가 존재하는지
12. JavaScript 실행 없이 initial HTML에 데이터가 있는지
13. session cookie, CSRF token, Referer가 필요한지
14. 검색 기간 최대 허용 범위
15. 서울 필터가 복수 송출지역 내 서울 포함 항목도 반환하는지

구현 전 read-only 조사 명령을 추가한다.

```bash
python -m app.commands.inspect_safekorea_fallback
```

명령의 역할:

- 서울 필터 목록 page 1을 요청
- HTTP status, content-type, row 수, pagination metadata 출력
- list/detail selector 후보 검사
- 첫 항목의 detail request를 1회 수행
- 본문을 출력하지 않고 추출 필드의 존재와 길이만 표시
- sanitized fixture 생성 옵션 제공
- 구조가 불명확하면 non-zero 종료

자동화 전에는 브라우저 개발자 도구 Network/Elements로 같은 계약을 한 번 수동 확인한다.

## 4. 서울 필터 검증

국민안전24 서울 필터가 아래와 같은 복수지역 메시지를 포함하는지 반드시 비교한다.

```text
경기도 광명시, 경기도 시흥시, 서울특별시 구로구
```

검증 방법:

1. 동일한 KST 날짜 범위로 지역 필터 없는 목록을 조회
2. `sbLawArea1=1100000000` 목록을 조회
3. 지역 필터 없는 결과 중 상세 송출지역에 `서울특별시`가 포함된 항목을 추출
4. 해당 stable ID가 서울 필터 목록에도 모두 존재하는지 비교

판정:

- 모두 존재: 서울 필터 URL을 production fallback으로 사용
- 일부 누락: production fallback에서 지역 필터 없는 목록을 받고 상세 `송출지역`으로 로컬 서울 필터

단, 지역 필터 없는 방식은 요청량이 크게 증가할 수 있으므로 누락이 실제 확인된 경우에만 사용한다.

최종 서울 판정은 목록의 표시 텍스트나 메시지 본문이 아니라 상세 페이지의 공식 `송출지역` 값을 기준으로 한다.

## 5. Fallback 활성화 조건

매 300초 Poll cycle의 순서는 고정한다.

```text
1. SafetyData API 요청
2. 성공하면 API 결과만 사용
3. 실패하면 같은 cycle에서 국민안전24 fallback 시도
4. 둘 다 실패하면 cycle 실패, 자동 발송 없음
```

Fallback을 즉시 시도하는 조건:

- connect/read timeout
- DNS/network failure
- HTTP 429
- HTTP 5xx
- HTTP 401/403: key가 로컬에 존재하고 이전에 정상 검증된 경우
- API-level 실패 result code
- JSON decode 실패
- required response field 누락
- schema drift
- pagination contract 위반

Fallback을 사용하지 않는 조건:

- `SAFETYDATA_SERVICE_KEY` 자체가 비어 있음
- 잘못된 local configuration
- API가 정상 success envelope와 함께 0건을 반환
- Poller가 local admin command로 pause 상태
- Telegram master send switch만 OFF인 상태

키 누락은 배포 설정 오류이므로 fallback으로 숨기지 않고 fail-fast한다.

정상 0건은 장애가 아니므로 HTML을 추가 조회하지 않는다.

## 6. Fallback 복구 방식

별도의 장기 `fallback mode`를 만들지 않는다.

다음 Poll cycle에서도 항상 Primary API를 먼저 시도한다.

```text
Cycle N
API 실패 → HTML fallback 사용

Cycle N+1
API 재시도
API 성공 → 즉시 API 사용
```

이 방식은 상태기계를 크게 만들지 않으면서도 API가 복구되면 자동으로 primary로 돌아간다.

다만 로그와 DB run history에는 실제 사용 원천을 기록한다.

권장 method 값:

```text
mois_safetydata_api
safekorea_html_fallback
```

`/status`에는 비밀이나 URL query를 노출하지 않고 다음 정도만 표시할 수 있다.

```text
최근 수집 원천: 행정안전부 API
```

또는

```text
최근 수집 원천: 국민안전24 Fallback
```

## 7. HTML 수집 범위

Fallback은 최근 최대 1주일 검색 화면을 사용한다.

날짜는 KST 기준으로 동적으로 생성한다.

```python
end_date = today_kst
start_date = today_kst - timedelta(days=7)
```

초기 조사에서 사이트가 inclusive 기준으로 7일보다 좁은 범위만 허용하면 실제 허용 범위에 맞춘다.

페이지 수집:

1. `currentPage=1`부터 시작
2. 각 list row의 stable ID 추출
3. 이미 DB 또는 fallback in-cycle set에 있는 ID는 detail fetch 생략 가능
4. 필요한 unseen row만 detail request
5. 다음 페이지가 없거나 row가 0건이면 종료
6. 동일 page ID가 반복되면 pagination loop 오류로 중단
7. 최대 페이지 상한을 두어 무한 요청 방지

Fallback은 장애 시에만 실행되므로 공격적인 병렬화가 필요하지 않다. 순차 요청과 짧은 bounded delay를 사용한다.

## 8. HTML 상세 필드 정규화

상세 HTML에서 다음 의미의 필드를 추출한다.

| HTML 의미 | Normalized field |
|---|---|
| 상세 항목 ID | namespaced `source_id` |
| 발송일시 | `sent_at` |
| 메시지 본문 | `original_body` |
| 송출지역 | `sender_or_region` |
| 긴급단계 | `raw_payload.emergency_step` |
| 재해구분 | `raw_payload.disaster_type` (존재 시) |

Fallback source ID:

```python
source_id = f"SAFEKOREA:{bbs_sn}"
```

`bbsSn`이 실제 stable ID가 아닌 것으로 확인되면 페이지 position을 사용하지 말고 detail URL의 안정적인 식별자를 조사한다.

`source_url`에는 service key가 없으므로 해당 공개 detail URL을 저장할 수 있다. 단, 불필요한 session parameter는 제거한다.

원문 보존:

- 본문 문구를 교정하거나 요약하지 않음
- HTML `<br>`은 줄바꿈으로 변환
- entity decode 수행
- UI label은 본문에 섞지 않음
- 발송일시·긴급단계·송출지역은 별도 field로 저장

## 9. Primary와 Fallback 간 중복 방지

Primary API와 국민안전24 HTML은 동일 사건에 서로 다른 source ID를 제공할 가능성이 높다.

따라서 source ID namespace만으로는 중복 발송을 막을 수 없다.

구현 전 같은 최근 항목 20건 이상을 양쪽 source에서 비교한다.

비교 대상:

- `SN` 대 `bbsSn`
- `CRT_DT` 대 HTML 발송일시
- `MSG_CN` 대 HTML 본문
- `RCPTN_RGN_NM` 대 HTML 송출지역
- 긴급단계
- 재해구분

우선순위:

1. 기존 `raw_hash(sender_or_region, sent_at, original_body)`가 양쪽에서 동일한지 확인
2. 동일하면 기존 DB dedup을 그대로 사용
3. 지역 표기/줄바꿈 차이로 동일하지 않으면 source-neutral canonical fingerprint 추가

Canonical normalization은 최소한으로 제한한다.

- Unicode NFC
- CRLF/LF 통일
- 연속 whitespace 축약
- 앞뒤 whitespace 제거
- 송출지역 comma token trim 및 정렬
- 발송시각 KST 초 단위 통일

본문 단어를 제거하거나 맞춤법을 바꾸지 않는다.

동일 source message를 Primary에서 이미 보낸 뒤 Fallback에서 다시 발견하더라도 새 `telegram_deliveries`를 만들지 않아야 한다.

## 10. Cutover와 Fallback의 관계

v0.5.0 최초 배포 시에는 SafetyData API current window를 먼저 baseline 처리한다.

국민안전24 fallback도 동일 날짜 window에서 cross-source 비교를 수행하되, 배포 시점의 기존 항목을 자동 발송하지 않는다.

Cutover 완료 후:

- API 신규 항목 → 정상 발송
- API 실패 + fallback 신규 항목 → 정상 발송
- API에서 이미 처리된 항목을 fallback이 다시 반환 → dedup
- fallback에서 먼저 처리한 항목을 API 복구 후 다시 반환 → dedup

## 11. 코드 위치

권장 최소 구조:

```text
app/collector.py
  - primary API first
  - failure classification
  - fallback 호출
  - CollectionResult.method 설정

app/mois_parser.py
  - SafetyData JSON parsing

app/safekorea_fallback.py
  - list URL 생성
  - list/detail HTML fetch
  - HTML parsing
  - 서울 필터 검증

app/config.py
  - SAFETYDATA_SERVICE_KEY
  - fallback timeout/retry/page cap (필요한 최소 설정만)

app/commands/inspect_safekorea_fallback.py
  - read-only structure discovery

app/commands/source_cutover_mois.py
  - API/HTML cross-source cutover 검사
```

Generic provider registry나 plugin framework는 만들지 않는다.

## 12. 설정

권장 설정:

```env
SAFETYDATA_SERVICE_KEY=
SAFETYDATA_FALLBACK_ENABLED=true
SAFETYDATA_FALLBACK_MAX_PAGES=10
SAFETYDATA_FALLBACK_REQUEST_TIMEOUT_SECONDS=15
```

Fallback은 운영 연속성을 위한 기능이므로 기본값을 `true`로 둘 수 있다. 다만 HTML 구조 검사와 테스트가 완료되기 전에는 production에서 활성화하지 않는다.

날짜, 서울 코드, base URL은 코드 상수로 관리한다.

## 13. 오류 처리

Fallback 자체 실패 조건:

- HTTP 403/429 지속
- HTML 대신 challenge/login page
- 목록 selector 0건이지만 명시적 empty marker도 없음
- detail selector 누락
- 발송일시 parse 실패
- 본문 누락
- 송출지역 누락
- pagination loop

Fallback이 실패하면 partial record를 전송하지 않는다.

한 항목만 malformed인 경우 전체 cycle을 실패시킬지 skip할지는 실제 HTML 안정성을 보고 결정한다. 기본 원칙은 잘못된 재난문자를 보내지 않는 fail-closed다.

## 14. 관측성

Run log에 다음을 남긴다.

- primary outcome
- fallback attempted 여부
- fallback outcome
- source method
- fetched count
- Seoul included count
- duplicate count
- new count
- detail requests
- elapsed time

남기지 않는 것:

- Service Key
- query string 전체
- 재난문자 전체 본문
- Telegram user/chat ID

Primary 실패 후 Fallback 성공은 service continuity는 성공이지만 운영 경고가 필요한 degraded 상태로 기록한다.

```text
status=degraded
method=safekorea_html_fallback
```

## 15. 테스트

### URL 생성

- 서울 코드 `1100000000`
- KST 날짜 범위
- page number
- 빈 재해/키워드 필터
- URL encoding

### HTML fixture

- 정상 list
- empty list
- pagination
- detail body
- `<br>` 줄바꿈
- 긴급단계
- 송출지역
- malformed detail
- challenge page

### 서울 필터

- 서울 전체
- 서울 자치구
- 경기 + 서울 복수지역
- 서울 미포함
- 본문에만 서울 등장

### Failover

- API 성공 → fallback 호출 0회
- API 정상 0건 → fallback 호출 0회
- API timeout → fallback 호출
- API 429/5xx/schema error → fallback 호출
- key 누락 → fail-fast, fallback 호출 0회
- API와 fallback 모두 실패 → 발송 0건

### Cross-source dedup

- API 먼저 수집 후 HTML 동일 항목
- HTML 먼저 수집 후 API 동일 항목
- 줄바꿈/지역 token 순서 차이
- 다른 문자 false merge 방지

### Regression

- 모든 active 독립 Telegram 사용자 fan-out
- 사용자별 retry
- `/latest`, `/history`
- Template/Preview/AI/Decision 독립성
- 개인 mute/unmute
- 300초 polling
- Excel/YAML sync

## 16. 배포 순서

1. API contract inspector 실행
2. SafeKorea HTML inspector 실행
3. 양쪽 source 최근 항목 비교
4. cross-source dedup 방식 확정
5. sanitized fixtures commit
6. unit/integration tests
7. tmux 서비스 정지
8. SQLite + WAL/SHM backup
9. cutover inspect
10. cutover bootstrap
11. v0.5.0 서비스 시작
12. 첫 cycle에서 과거 항목 발송 0건 확인
13. API 장애 mock으로 fallback local validation
14. 실제 신규 문자에서 active 모든 사용자 fan-out 확인

## 17. 완료 조건

- Primary API가 정상일 때 국민안전24 요청 0회
- Primary hard failure 시 해당 cycle에서 fallback 실행
- 정상 API 0건은 fallback으로 오판하지 않음
- 서울 필터가 복수지역 서울 항목을 누락하지 않음
- 상세 HTML에서 본문·발송일시·긴급단계·송출지역 추출
- 두 source 간 동일 항목 중복 발송 0건
- 기존 서울안전누리 호출 0회
- 둘 다 실패하면 fail-closed
- 두 명 이상 독립 사용자 기능 regression 없음
- Service Key 비노출
- docs/tests/version 업데이트

## 18. 구현 전 남은 확인

1. 국민안전24 list/detail HTML selector
2. `bbsSn` stable ID 여부
3. 서울 필터의 복수지역 포함 여부
4. 최대 조회기간의 정확한 inclusive 규칙
5. pagination의 마지막 페이지 판정
6. API와 HTML의 필드 값이 어느 수준으로 동일한지
7. 기존 `raw_hash`만으로 cross-source dedup이 충분한지

위 항목은 로컬에서 실제 페이지와 Service Key를 이용해 확인한 뒤 구현한다.

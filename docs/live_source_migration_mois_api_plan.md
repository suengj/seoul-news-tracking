# 실시간 재난문자 Source 전환 계획 — 행정안전부 API + 국민안전24 Fallback

- 작성일: 2026-07-16 (KST)
- 현재 서비스 기준: v0.4.1
- 목표 버전: v0.5.0
- 대상 저장소: `suengj/seoul-news-tracking`
- Primary 안내: `https://www.safetydata.go.kr/disaster-data/view?dataSn=228`
- Primary API: `https://www.safetydata.go.kr/V2/api/DSSP-IF-00247`
- Fallback: 국민안전24 재난문자 조회 HTML
- 상태: 구현 전 계약 확인 및 Cutover 설계 완료

## 1. 배경

기존 실시간 Collector는 서울안전누리 페이지에서 `JSESSIONID`를 얻은 뒤 비공개성 XHR endpoint를 호출한다.

현재 관련 코드:

- `app/collector.py`
  - 서울안전누리 page GET
  - `/disstr/selectDisstrSms.do` POST
- `app/parser.py`
  - 기존 `sms[]` payload parsing
- `app/config.py`
  - `SOURCE_PAGE_URL`, `SOURCE_API_URL`
- `docs/source_discovery.md`
  - 기존 source 조사 기록

서울안전누리 개편으로 기존 계약을 더 이상 신뢰할 수 없으므로 공식 행정안전부 SafetyData API로 교체한다.

API가 일시적으로 실패할 때에는 국민안전24의 서울 필터 재난문자 조회 HTML을 조건부 fallback으로 사용한다.

## 2. 최종 Source 우선순위

```text
1순위: 행정안전부 SafetyData Open API
2순위: 국민안전24 서울 필터 HTML
사용 금지: 기존 서울안전누리 collector
```

원칙:

1. API가 성공하면 API만 사용한다.
2. API가 정상적으로 0건을 반환하면 fallback을 실행하지 않는다.
3. API가 hard failure이면 같은 Poll cycle에서 국민안전24 fallback을 시도한다.
4. API와 fallback 모두 실패하면 fail-closed한다.
5. 기존 서울안전누리로는 어떤 경우에도 fallback하지 않는다.
6. 어느 원천을 사용하더라도 `DisasterMessageRecord` 이후 처리 흐름은 동일하다.

국민안전24 fallback 상세 설계는 다음 문서를 따른다.

```text
docs/safekorea_html_fallback_plan.md
```

## 3. Primary API 계약

### Request

```text
GET /V2/api/DSSP-IF-00247
```

요청 변수:

| 국문 | 영문 | 필수 | 용도 |
|---|---|---:|---|
| 서비스키 | `serviceKey` | Y | 로컬 `.env` secret |
| 페이지당개수 | `numOfRows` | N | page size |
| 페이지번호 | `pageNo` | N | pagination |
| 응답타입 | `returnType` | N | `json` |
| 조회시작일자 | `crtDt` | N | `YYYYMMDD` |
| 지역명 | `rgnNm` | N | `서울특별시` |

### Response item

| 국문 | 영문 | Normalized mapping |
|---|---|---|
| 일련번호 | `SN` | `source_id = MOIS:{SN}` |
| 생성일시 | `CRT_DT` | `sent_at` |
| 메시지내용 | `MSG_CN` | `original_body` |
| 수신지역명 | `RCPTN_RGN_NM` | `sender_or_region` |
| 긴급단계명 | `EMRG_STEP_NM` | `raw_payload` |
| 재해구분명 | `DST_SE_NM` | `raw_payload` |
| 등록일자 | `REG_YMD` | `raw_payload` |
| 수정일자 | `MDFCN_YMD` | `raw_payload` |

`REG_YMD`, `MDFCN_YMD`는 발송시각으로 사용하지 않는다.

## 4. 서울특별시 대상 판정

최종 판정 기준은 메시지 본문이 아니라 공식 수신지역 field인 `RCPTN_RGN_NM`이다.

포함:

- `서울특별시`
- `서울특별시 구로구`
- `서울특별시 강남구`
- 복수지역 중 하나라도 `서울특별시` 포함

예:

```text
경기도 광명시, 경기도 시흥시, 서울특별시 구로구
```

위 항목은 서울 대상이다.

제외:

- `MSG_CN`에만 서울이라는 단어가 등장
- 발신기관명에만 서울이 등장
- `RCPTN_RGN_NM`에 서울특별시가 없음

서버 요청에는 `rgnNm=서울특별시`를 우선 사용하되, 실제 API 검사에서 복수지역 서울 항목이 누락되는지 비교한다.

```text
rgnNm
= 요청량 절감용 서버 필터

RCPTN_RGN_NM
= 최종 client-side 판정 기준
```

## 5. 국민안전24 Fallback URL

서울 필터 URL의 핵심 값:

```text
sbLawArea1=1100000000
```

조회 URL 형태:

```text
https://www.safekorea.go.kr/safekorea-kor/ctim/cmsg/calamitySms.do
?menuSn=34
&bbsSn=
&currentPage=1
&firstYn=
&searchType=
&cOcrcType=
&dsstrSeId=
&sbLawArea1=1100000000
&sbLawArea2=
&sbLawArea3=
&keyword=
&startDate=<KST dynamic date>
&endDate=<KST today>
&readYn=Y
```

지역 필터 없는 URL은 production fallback이 아니라 서울 필터 completeness 검사에만 사용한다.

Fallback은 목록만으로 끝나지 않을 수 있다. 각 항목의 stable ID와 상세 URL을 확인하고, unseen 항목만 상세 HTML을 요청해 다음을 추출한다.

- 본문
- 발송일시
- 긴급단계
- 송출지역
- 재해구분(존재 시)

HTML selector와 detail contract는 로컬 inspector로 먼저 확정한다.

## 6. 환경변수

로컬 `.env`:

```env
SAFETYDATA_SERVICE_KEY=
SAFETYDATA_FALLBACK_ENABLED=true
SAFETYDATA_FALLBACK_MAX_PAGES=10
SAFETYDATA_FALLBACK_REQUEST_TIMEOUT_SECONDS=15
```

`.env.example`에는 빈 값과 설명만 추가한다.

보안 원칙:

- Service Key commit 금지
- query string 전체 log 금지
- `httpx` params로 전달
- `source_url`에 key 포함 금지
- 오류 body 전체 log 금지
- key 누락은 fallback으로 숨기지 않고 fail-fast

## 7. 코드 변경 위치

### `app/collector.py`

`fetch_records()` 외부 계약을 유지한다.

```python
fetch_records(...) -> CollectionResult
```

내부 흐름:

```text
SafetyData API 요청
→ 성공: API normalized records 반환
→ 정상 0건: 빈 CollectionResult 반환
→ hard failure: SafeKorea fallback
→ fallback 성공: HTML normalized records 반환
→ fallback 실패: CollectorError
```

삭제/비활성화:

- `_bootstrap_session()`
- 기존 challenge-page 로직
- `JSESSIONID`
- `X-Requested-With`
- 서울안전누리 URL

### `app/mois_parser.py`

- API success/error envelope
- pagination metadata
- field validation
- KST datetime parsing
- `RCPTN_RGN_NM` 서울 판정
- `DisasterMessageRecord` 변환

### `app/safekorea_fallback.py`

- 서울 필터 URL 생성
- list/detail HTML fetch
- pagination
- detail parsing
- 서울 송출지역 재검증
- normalized record 변환

### `app/config.py`

추가:

```python
safetydata_service_key: str
safetydata_fallback_enabled: bool
safetydata_fallback_max_pages: int
safetydata_fallback_request_timeout_seconds: float
```

삭제:

```python
SOURCE_PAGE_URL
SOURCE_API_URL
```

### Inspection commands

```bash
python -m app.commands.inspect_mois_api
python -m app.commands.inspect_safekorea_fallback
```

두 command는 read-only이며 secret과 원문을 기본 출력하지 않는다.

## 8. Fallback 활성화 조건

Fallback 실행:

- timeout/connect/DNS failure
- HTTP 429
- HTTP 5xx
- 이전에 유효했던 key의 401/403
- API-level error result code
- invalid JSON
- schema drift
- required field 누락
- pagination contract 오류

Fallback 미실행:

- Service Key 미설정
- invalid local config
- 정상 API 0건
- Poller가 local admin pause
- Telegram send master switch만 OFF

다음 cycle에는 다시 API부터 시도한다. 별도 복잡한 장기 failover state machine은 만들지 않는다.

## 9. Pagination과 조회기간

Polling interval은 300초를 유지한다.

Primary API:

- `crtDt`는 KST 전일 또는 검증된 overlap 시작일
- 최신순 조회 여부 확인
- `numOfRows` 최대값 확인
- 전체 page를 무한 반복하지 않도록 상한 설정

Fallback:

- KST 오늘과 최대 최근 1주일 범위
- `currentPage=1`부터 증가
- 동일 page ID 반복 시 중단
- unseen item만 detail fetch
- 최대 page 상한 적용

## 10. 정상 0건 처리

공식 API가 success envelope와 함께 0건을 반환하면 정상 no-op이다.

기존 `EmptyWidgetError`를 그대로 사용해 Poll 실패로 기록하지 않는다.

필요하면 다음처럼 의미를 분리한다.

```text
ValidEmptyResult
SourceContractError
```

정상 0건 때문에 fallback을 호출하거나 Telegram 경고를 보내지 않는다.

## 11. Cross-source 중복 방지

Primary와 fallback은 동일 문자에 서로 다른 source ID를 사용할 수 있다.

```text
MOIS:{SN}
SAFEKOREA:{stable_detail_id}
```

따라서 구현 전에 양쪽 최근 항목을 비교한다.

- 발송시각
- 본문
- 수신/송출지역
- 긴급단계
- 재해구분

우선 기존 `raw_hash(sender_or_region, sent_at, original_body)`가 양쪽에서 동일한지 확인한다.

동일하지 않으면 최소한의 source-neutral canonical fingerprint를 추가한다.

- Unicode NFC
- CRLF/LF 통일
- whitespace 축약
- 송출지역 token trim/sort
- KST 발송시각 통일

본문 내용을 임의로 제거하거나 교정하지 않는다.

API에서 먼저 수집된 항목을 fallback이 반환하거나, fallback에서 먼저 수집된 항목을 API가 반환해도 `telegram_deliveries`를 새로 만들지 않아야 한다.

## 12. Source Cutover

Collector 교체만 하면 기존 DB와 다른 source ID 때문에 최근 문자가 재발송될 수 있다.

명시적 cutover command를 추가한다.

```bash
python -m app.commands.source_cutover_mois --inspect
python -m app.commands.source_cutover_mois --bootstrap
```

`--inspect`:

- DB 변경 없음
- API/HTML 현재 window 조회
- source ID match
- raw hash match
- canonical match 후보
- 신규로 보이는 항목 수

`--bootstrap`:

1. Poller 정지 확인
2. DB + WAL/SHM backup 확인
3. API 및 fallback current window 수집
4. 현재 항목을 baseline/cross-source known 처리
5. Telegram delivery 생성 금지
6. cutover marker 기록
7. idempotent 재실행 보장

첫 v0.5.0 cycle에서 과거 문자 발송은 0건이어야 한다.

## 13. 기존 후속 기능 유지

다음은 변경하지 않는다.

- SQLite messages/dedup/retention
- 독립 `telegram_subscriptions`
- 사용자별 `telegram_deliveries`
- 사용자별 retry
- `/latest`, `/history`, `/status`, `/help`
- `/subscribe`, `/unsubscribe`, `/mute`, `/unmute`
- `/pause`, `/resume` 개인 alias
- 카테고리/템플릿 선택
- Rule Preview
- AI Preview
- Final OK
- 사용자별 Preview/Decision 독립성
- 19개 자동화 템플릿
- Excel/YAML sync
- 별도 5,005건 historical raw DB

후속 코드는 API/HTML field name을 직접 참조하지 않고 normalized model만 사용한다.

## 14. 관측성

각 Poll cycle에 기록:

- primary outcome
- fallback attempted
- fallback outcome
- actual source method
- fetched count
- Seoul included count
- duplicate/new count
- elapsed time

권장 method:

```text
mois_safetydata_api
safekorea_html_fallback
```

Primary 실패 후 fallback 성공은 수집 성공이지만 degraded 상태로 기록한다.

`/status`에는 다음을 추가할 수 있다.

```text
최근 수집 원천: 행정안전부 API
```

또는

```text
최근 수집 원천: 국민안전24 Fallback
```

## 15. 테스트

### API

- key 누락 fail-fast
- success JSON
- valid empty
- API-level error
- 401/403/429/500
- timeout/backoff
- pagination
- schema drift
- key redaction

### 서울 필터

- 서울 전체
- 25개 자치구
- 복수지역 중 서울 포함
- 서울 미포함
- 본문에만 서울 포함

### Fallback

- 서울 URL 생성
- dynamic KST date
- list/detail fixture
- pagination
- empty marker
- malformed/challenge page
- 본문/발송일시/긴급단계/송출지역

### Failover

- API 성공 → fallback 0회
- API 정상 0건 → fallback 0회
- API hard failure → fallback
- 둘 다 실패 → 발송 0건
- key 누락 → fallback 0회

### Cross-source dedup

- API → HTML 동일 항목
- HTML → API 동일 항목
- whitespace/지역 순서 차이
- false merge 방지

### Regression

- 두 명 이상 독립 Telegram fan-out
- 사용자별 retry
- Template/AI/Decision 독립성
- 300초 polling
- Excel/YAML 21/19/2

## 16. 구현 순서

1. 사용자가 `.env`에 Service Key 입력
2. `inspect_mois_api` 실행
3. 실제 JSON envelope와 pagination 확정
4. `inspect_safekorea_fallback` 실행
5. list/detail selector와 `bbsSn` 안정성 확정
6. 서울 필터 completeness 비교
7. 양 source 최근 항목 비교
8. cross-source dedup 방식 확정
9. Collector/parser 구현
10. cutover command 구현
11. DB copy 검증
12. tests/lint/format/validator
13. tmux 정지 및 backup
14. cutover bootstrap
15. v0.5.0 시작
16. 과거 발송 0건 확인
17. 신규 문자 모든 active 사용자 fan-out 확인

## 17. 완료 조건

- 기존 서울안전누리 runtime 호출 0회
- API 정상 시 fallback 호출 0회
- API hard failure 시 SafeKorea fallback
- 정상 0건 오판 없음
- 서울 수신/송출지역 기준 정확한 필터
- Service Key 비노출
- API/HTML 동일 문자 중복 발송 0건
- 첫 cutover 과거 문자 발송 0건
- 두 명 이상 독립 사용자 regression 없음
- API와 fallback 모두 실패 시 fail-closed
- 문서, `.env.example`, tests, CHANGELOG, version 업데이트
- 목표 버전 v0.5.0

## 18. 구현 전에 반드시 확인할 항목

1. SafetyData JSON 최상위 envelope
2. data list 경로 및 pagination metadata
3. `CRT_DT` 실제 형식
4. `rgnNm=서울특별시`의 복수지역 포함 여부
5. 국민안전24 list/detail selector
6. `bbsSn` stable ID 여부
7. 국민안전24 조회기간 inclusive 규칙
8. API와 HTML field 값 동일성
9. 기존 raw hash로 cross-source dedup 가능한지

확인되지 않은 계약을 추측으로 구현하지 않는다. 로컬 inspection 결과가 문서와 다르면 작업을 중단하고 차이를 보고한 뒤 결정한다.

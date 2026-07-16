# 실시간 재난문자 Source 전환 계획 — 행정안전부 SafetyData API

- 작성일: 2026-07-16 (KST)
- 현재 서비스 기준: v0.4.1
- 대상 저장소: `suengj/seoul-news-tracking`
- 신규 공식 데이터 안내: `https://www.safetydata.go.kr/disaster-data/view?dataSn=228`
- 신규 API base: `https://www.safetydata.go.kr/V2/api/DSSP-IF-00247`
- 상태: **구현 전 기획 확정 단계**

## 1. 변경 배경

기존 실시간 수집은 서울안전누리 페이지에서 세션을 생성한 뒤 비공개성 XHR 엔드포인트를 호출하는 방식이다.

현재 코드 경로:

- `app/collector.py`
  - `GET https://safecity.seoul.go.kr/news/dist/dust/newsDistDustList.page`
  - `POST https://safecity.seoul.go.kr/disstr/selectDisstrSms.do`
- `app/parser.py`
  - 기존 응답의 `sms[]`와 `disstrSmsSn`, `disstrDate`, `lctnNm`, `smsMsg`를 해석
- `app/config.py`
  - `SOURCE_PAGE_URL`, `SOURCE_API_URL` 상수 보유
- `docs/source_discovery.md`
  - 기존 서울안전누리 XHR 조사 결과

서울안전누리 개편으로 이 계약을 더 이상 신뢰할 수 없으므로, 실시간 원천을 행정안전부 재난안전데이터공유플랫폼의 공식 Open API로 전환한다.

공식 데이터 페이지는 해당 데이터를 `행정안전부_긴급재난문자`로 설명하며, Open API가 REST 기반 JSON/XML 방식이고 회원가입 및 활용 신청이 필요하다고 안내한다.

## 2. 이번 전환의 핵심 원칙

1. **기존 서울안전누리 수집 경로는 런타임에서 완전히 OFF한다.**
2. **자동 fallback으로 기존 홈페이지를 다시 호출하지 않는다.**
3. 신규 API 실패 시 잘못된 데이터를 보내지 말고 Poll cycle을 실패 처리한다.
4. API 응답은 현재의 `DisasterMessageRecord`로 정규화한다.
5. 정규화 이후의 DB, 중복 제거, Telegram fan-out, 템플릿, AI, 사용자별 독립 처리 구조는 변경하지 않는다.
6. 서비스 키는 로컬 `.env`에만 저장하며 Git, 로그, DB, `source_url`, 오류 메시지에 노출하지 않는다.
7. 첫 전환 시 기존 API와 신규 API의 ID 체계가 다르므로 **명시적 cutover baseline**을 수행해 과거 문자가 재발송되지 않게 한다.
8. 현재의 5,005건 역사 DB와 HTML backfill 기능은 별도 분석 자산이므로 이번 실시간 원천 전환에서 수정하지 않는다.

## 3. 범위

### 포함

- 실시간 collector를 행정안전부 SafetyData API로 교체
- 신규 API 계약 검사 명령 추가
- 서울특별시 및 서울 25개 자치구 수신 대상 필터
- 신규 응답을 `DisasterMessageRecord`로 변환
- 페이지네이션/조회 기간/정렬 검증 후 최근 데이터 polling
- source cutover baseline 및 재발송 방지
- API 오류/쿼터/인증 오류 관측성
- `.env.example`, 테스트, 문서, 버전 업데이트

### 제외

- Telegram 독립 사용자 구조 변경
- 템플릿 YAML 및 Excel 문안 변경
- AI 모델 또는 Prompt 변경
- 기존 `data/history_raw.db` 재수집
- X 자동 게시
- 기존 서울안전누리 경로를 fallback으로 유지하는 기능

## 4. 확정된 신규 설정

로컬 `.env`에 다음 키를 둔다.

```env
# Required for the live MOIS SafetyData collector.
# Never commit the real value.
SAFETYDATA_SERVICE_KEY=
```

`.env.example`에도 동일한 빈 항목과 설명을 추가한다.

권장 코드 상수:

```python
MOIS_API_BASE_URL = "https://www.safetydata.go.kr/V2/api/DSSP-IF-00247"
MOIS_DATASET_PAGE_URL = "https://www.safetydata.go.kr/disaster-data/view?dataSn=228"
```

주의:

- 요청 URL 문자열을 직접 조합해 로그로 출력하지 않는다.
- `serviceKey`는 `httpx`의 `params`로 전달한다.
- 오류 로그에는 query string을 제거한 base URL만 남긴다.
- `DisasterMessageRecord.source_url`에는 서비스 키가 포함된 호출 URL이 아니라 `MOIS_DATASET_PAGE_URL` 또는 key가 없는 base URL만 저장한다.
- 실제 키가 URL-encoded key인지 decoded key인지는 최초 live contract 검사에서 확인하고 문서화한다. 추측으로 이중 인코딩하지 않는다.

## 5. 구현 구조

프로젝트를 크게 재구성하지 않는다. 기존 인터페이스를 유지하는 것이 핵심이다.

### 5.1 `app/collector.py`

현재 외부 호출부의 `fetch_records()` 계약을 유지한다.

```python
fetch_records(...) -> CollectionResult
```

내부만 다음으로 교체한다.

```text
기존
GET 서울안전누리 page
→ JSESSIONID
→ POST selectDisstrSms.do

신규
GET /V2/api/DSSP-IF-00247
→ serviceKey + 문서에서 확인된 조회 파라미터
→ JSON 응답 검증
→ 서울 수신 대상 필터
→ DisasterMessageRecord 목록
```

삭제 또는 비활성화 대상:

- `_bootstrap_session()`
- `_looks_like_challenge_page()`
- `JSESSIONID` 처리
- `X-Requested-With` 헤더
- `SOURCE_PAGE_URL`
- `SOURCE_API_URL`
- 서울안전누리 도메인 호출

런타임 코드에는 legacy source toggle을 두지 않는다. 필요한 과거 코드는 Git history로 복구할 수 있다.

### 5.2 Parser 분리

`app/parser.py`를 신규 API 전용으로 바꾸거나, 가독성을 위해 다음처럼 명확하게 이름을 분리한다.

```text
app/mois_parser.py
```

단, 모듈 하나를 추가하는 수준으로 유지하고 generic provider framework는 만들지 않는다.

Parser 책임:

- 최상위 성공/오류 envelope 검증
- record list 존재 및 type 검증
- stable source ID 추출
- 발송시각을 `Asia/Seoul` timezone-aware datetime으로 변환
- 수신지역 원문 보존
- 메시지 본문 원문 보존
- 서울 대상 필터 판정
- `DisasterMessageRecord` 생성

API 문서와 실제 응답을 확인하기 전에는 field name을 확정하지 않는다. 기존 필드명을 신규 API에 억지로 대입하지 않는다.

### 5.3 `app/config.py`

추가:

```python
safetydata_service_key: str
```

삭제:

```python
SOURCE_PAGE_URL
SOURCE_API_URL
```

Startup 또는 collector 호출 시 `SAFETYDATA_SERVICE_KEY`가 없으면 명확하게 실패한다.

예상 오류:

```text
SAFETYDATA_SERVICE_KEY is required for the MOIS SafetyData collector
```

Telegram bot의 `/latest`, `/history`, 템플릿 작업은 DB 조회만 사용하므로 키가 없더라도 원칙적으로 동작할 수 있다. 다만 `run_local`의 Poller child는 키 누락으로 반복 crash하지 않게 시작 전 설정 검증 또는 명확한 backoff가 필요하다.

## 6. API 계약 확인 단계 — 구현 전 필수

공식 안내 페이지는 Open API의 존재와 JSON/XML 제공 방식은 확인되지만, 로그인/활용승인 없이 현재 환경에서 다음 세부 계약을 검증할 수 없다.

- 요청 파라미터 이름
- JSON 선택 파라미터
- 페이지 번호/페이지 크기 파라미터
- 날짜 범위 파라미터 및 형식
- 지역 필터 파라미터 지원 여부
- 응답 최상위 envelope
- record list 경로
- stable ID field
- 발송시각 field와 timezone
- 메시지 본문 field
- 수신지역 field
- 결과 코드/오류 코드
- 호출 한도와 갱신 주기

따라서 구현 시 먼저 다음 read-only 명령을 추가한다.

```bash
python -m app.commands.inspect_mois_api
```

동작:

1. `.env`의 `SAFETYDATA_SERVICE_KEY` 로드
2. 최소 1회 요청
3. service key를 절대 출력하지 않음
4. HTTP status, content-type, 최상위 key, record count, 각 record의 field name/type만 출력
5. 실제 본문과 실제 수신지역은 기본 출력하지 않음
6. `--save-sanitized-fixture` 옵션일 때 식별 가능한 원문을 마스킹한 fixture만 저장
7. 계약이 문서와 다르면 구현을 중단하고 질문/보고

이 검사가 통과하기 전에는 parser field mapping을 확정하지 않는다.

## 7. 서울 대상 필터 정책

판정은 메시지 본문 keyword가 아니라 **API의 공식 수신지역 field**를 기준으로 한다.

기본 포함 정책:

1. 수신지역에 `서울특별시` 또는 공식 서울 전체 코드가 포함
2. 수신지역에 서울 25개 자치구가 포함
3. 다중 수신지역 중 하나라도 서울특별시/서울 자치구에 해당

서울 25개 자치구:

```text
강남구, 강동구, 강북구, 강서구, 관악구, 광진구, 구로구, 금천구,
노원구, 도봉구, 동대문구, 동작구, 마포구, 서대문구, 서초구,
성동구, 성북구, 송파구, 양천구, 영등포구, 용산구, 은평구,
종로구, 중구, 중랑구
```

제외 원칙:

- 본문에 서울이라는 단어만 등장하지만 수신지역이 서울이 아닌 메시지
- 발신기관이 서울 소재라는 이유만으로 서울 수신으로 추정한 메시지
- 지역 field가 불명확한데 임의로 서울로 추정한 메시지

보존 원칙:

- `sender_or_region`에는 API 수신지역 원문을 최대한 보존
- 정규화된 서울 판정 결과는 raw payload 또는 별도 내부 함수 결과로만 사용
- multi-region 문자열을 임의로 잘라 원문을 손실하지 않음

서버 측 지역 필터가 제공되면 요청량 감소 목적으로 사용하되, client-side 서울 검증을 반드시 한 번 더 수행한다.

## 8. 정규화 field mapping

실제 field name은 `inspect_mois_api` 결과로 확정한다.

| 정규화 필드 | 신규 API에서 필요한 의미 | 규칙 |
|---|---|---|
| `source_id` | 긴급재난문자 stable serial/sequence | 존재 시 그대로 사용 |
| `sent_at` | 실제 문자 발송시각 | `Asia/Seoul`로 저장 |
| `sender_or_region` | 공식 수신지역 원문 | 문자열/배열 형식을 원문 의미 보존 형태로 변환 |
| `original_body` | 전체 재난문자 본문 | 요약·교정·trim 최소화, 전체 본문 보존 |
| `source_url` | 공개 dataset URL | service key 없는 URL만 저장 |
| `detected_at` | 우리 시스템의 수집시각 | 현재 KST |
| `raw_payload` | 원본 record JSON | service key 없이 record만 저장 |

stable ID가 없거나 신뢰할 수 없는 경우:

- API의 복합 key 조합을 공식 계약에 따라 사용
- 최후 fallback은 기존 `raw_hash(sender_or_region, sent_at, original_body)`
- 임의 index나 page position을 ID로 사용하지 않음

## 9. Polling 및 Pagination

기존 `POLL_INTERVAL_SECONDS=300`은 유지한다.

신규 API의 pagination/date filter를 계약 검사 후 다음 원칙으로 구현한다.

1. 최신 순으로 조회
2. 충분한 overlap window를 포함
3. `source_id` 및 raw hash로 DB dedup
4. 최신 페이지가 장애로 일부 누락돼도 다음 poll에서 재수집 가능
5. 한 poll에서 지나치게 많은 과거 페이지를 반복 조회하지 않음
6. API 호출량 제한을 넘지 않음
7. valid empty result는 정상 no-op으로 처리
8. 인증 오류, API resultCode 오류, schema 오류는 hard failure로 처리

현재의 `EmptyWidgetError` 의미는 재검토한다. 공식 API에서 정상적으로 record 0건을 반환하면 Poll 실패가 아니라 성공적인 no-op이어야 한다.

## 10. Source cutover와 중복 재발송 방지

이 부분은 필수이다.

기존 서울안전누리와 신규 행안부 API가 같은 문자에 대해 서로 다른 source ID 또는 지역 문자열을 제공할 수 있다. 단순히 collector만 교체하면 이미 DB에 있는 최근 문자가 신규 record로 인식되어 두 운영자 모두에게 재발송될 수 있다.

명시적인 cutover 명령을 추가한다.

```bash
python -m app.commands.source_cutover_mois --inspect
python -m app.commands.source_cutover_mois --bootstrap
```

`--inspect`:

- 기존 DB 변경 없음
- 신규 API에서 조회될 record 수
- 기존 DB와 exact source ID match 수
- raw hash match 수
- timestamp/body 유사 match 후보 수
- 신규로 보이는 record 수
- service key 비노출

`--bootstrap`:

1. 서비스/Poller 정지 상태 확인
2. DB backup 존재 확인
3. 신규 API의 현재 조회 window를 수집
4. 현재 조회된 모든 record를 cutover baseline으로 저장하거나 tombstone/equivalent 처리
5. Telegram delivery row를 생성하지 않음
6. source cutover marker 기록
7. 완료 후 이후 발송시각의 신규 record만 정상 fan-out

권장 최소 system state:

```text
active_source = mois_safetydata_api
source_cutover_at = <KST timestamp>
source_bootstrap_completed = true
```

Schema 변경을 더 줄일 수 있으면 기존 control/audit table에 명시적 source cutover event를 기록해도 되지만, 재시작 후에도 cutover 완료 여부를 확실하게 판정할 수 있어야 한다.

자동으로 기존 서울안전누리 API와 신규 API를 동시에 호출해 비교하는 dual-run은 하지 않는다. 기존 source가 이미 신뢰 불가하기 때문이다.

## 11. 기존 후속 기능 보존

신규 collector가 `DisasterMessageRecord`를 동일하게 반환하면 다음은 그대로 유지한다.

- `app/commands/poll_once.py`
  - active personal subscriptions fan-out
  - 사용자별 `telegram_deliveries`
  - 사용자별 retry
- `/latest`
- `/history`
- 카테고리/템플릿 버튼
- Rule Preview
- AI Preview
- Final OK
- 사용자별 Preview/Decision 독립성
- `/subscribe`, `/unsubscribe`, `/mute`, `/unmute`
- `/pause`, `/resume` 개인 alias
- 19개 자동화 템플릿 및 Excel/YAML sync
- 90일 운영 DB retention
- 5,005건 historical raw DB 분리

후속 로직에서 source-specific field를 직접 참조하면 안 된다. 모든 후속 코드는 normalized model만 사용해야 한다.

## 12. 오류 처리

다음은 Poll 실패로 기록하고 Telegram 자동 발송을 수행하지 않는다.

- service key 없음
- HTTP 401/403
- HTTP 429
- HTTP 5xx 재시도 소진
- API envelope의 실패 result code
- JSON이 아닌 응답
- required field 누락
- 발송시각 parse 실패
- 본문 누락
- pagination 반복/무한 loop 감지

재시도:

- timeout/connect error 및 5xx: bounded exponential backoff
- 429: `Retry-After`가 있으면 우선 적용, 없으면 longer backoff
- 401/403: 반복 재시도하지 않고 인증 오류로 명확히 기록

절대 하지 않을 것:

- 오류 시 기존 서울안전누리 scraper 호출
- partial record를 Telegram으로 전송
- API 오류 HTML/JSON 전체를 로그에 출력
- service key가 포함된 URL 출력

## 13. 테스트 계획

### Collector/API contract

- service key 누락 시 fail-fast
- JSON success fixture
- JSON empty fixture
- API-level error fixture
- 401/403/429/500
- timeout/retry/backoff
- pagination 종료
- schema drift
- service key log redaction
- 서울안전누리 도메인 호출이 0회임을 검증

### 서울 필터

- 서울특별시 전체
- 25개 각 자치구
- `서울특별시 강남구`
- multi-region 중 서울 포함
- 서울 미포함
- 본문에만 서울 포함
- 유사 문자열 false positive
- 배열/문자열 수신지역 형식

### Normalization

- stable ID
- KST datetime
- full body 보존
- raw payload 보존
- source URL에 key 없음
- raw hash deterministic

### Cutover

- 기존 DB가 있어도 첫 신규 API window를 재발송하지 않음
- bootstrap idempotent
- bootstrap 중 Telegram delivery 미생성
- bootstrap 후 신규 record만 A/B 양쪽에 전달
- 기존 두 사용자 subscription/decision 유지

### Regression

- two independent Telegram users fan-out
- one recipient failure/retry isolation
- `/latest`, `/history`
- Preview/Decision per operator
- AI worker independence
- personal mute/unmute
- 300초 polling
- Excel/YAML sync 21/19/2

## 14. 구현 순서

### Phase 0 — API 계약 확인

1. 사용자가 로컬 `.env`에 `SAFETYDATA_SERVICE_KEY` 입력
2. `inspect_mois_api` 구현/실행
3. 실제 요청 파라미터와 response field 확정
4. sanitized fixture 생성
5. 이 문서의 field mapping section 업데이트

### Phase 1 — Collector 교체

1. config/env 추가
2. MOIS collector/parser 구현
3. 서울 필터 구현
4. 기존 서울안전누리 runtime code 제거
5. unit tests

### Phase 2 — Cutover 안전장치

1. source cutover inspect/bootstrap command
2. DB-copy 검증
3. 재발송 방지 테스트
4. 운영 runbook 업데이트

### Phase 3 — 배포

1. `seoulnews` tmux 서비스 정지
2. SQLite + WAL/SHM 일관 backup
3. `.env` key 설정 확인
4. API inspect 통과
5. cutover inspect
6. cutover bootstrap
7. 최신 main으로 서비스 시작
8. 첫 poll에서 과거 알림 미발송 확인
9. 다음 신규 문자에서 모든 active operator에게 독립 fan-out 확인

## 15. 버전

현재 v0.4.1에서 공식 실시간 source를 교체하는 기능 변경이므로 목표 버전은 다음을 권장한다.

```text
v0.5.0
```

단순 patch가 아니라 collector 계약, 환경변수, cutover 절차가 추가되기 때문이다.

## 16. 구현 완료 조건

- 기존 서울안전누리 URL 요청이 runtime에서 0회
- 공식 SafetyData API만 실시간 source로 사용
- service key가 Git/log/DB에 없음
- 서울특별시/25개 자치구 대상만 수집
- 응답 전체 본문과 발송시각 보존
- valid empty result 정상 처리
- schema/error 실패 시 자동 발송 없음
- 첫 cutover에서 과거 문자 재발송 없음
- 이후 신규 문자 A/B/N active 사용자에게 독립 발송
- Telegram/템플릿/AI/Decision 기존 기능 regression 없음
- tests, lint, format, offline validator 통과
- 문서와 `.env.example` 업데이트
- live two-user validation 완료 후 tag

## 17. 구현 전 확인이 필요한 사항

아래 항목은 service key를 이용한 실제 1회 contract inspection 전에는 확정할 수 없다.

1. API 활용 신청 및 key가 현재 활성 상태인지
2. key가 encoded/decoded 중 어느 형태인지
3. JSON 응답을 요청하는 정확한 파라미터
4. pagination/date/region 파라미터 이름과 허용 범위
5. stable ID, 본문, 발송시각, 수신지역의 실제 field name
6. 서울 수신지역이 `서울특별시`, 자치구명, 행정코드 중 어떤 형태로 반환되는지
7. 호출 한도 및 데이터 갱신 지연

이 중 field 계약이 확인되지 않으면 추측으로 구현하지 말고 작업을 중단한 뒤 사용자에게 sanitized schema를 보고하고 확인을 받아야 한다.

# 행정안전부 긴급재난문자 API 계약 확인 메모

- 확인일: 2026-07-16 (KST)
- 공식 데이터: `행정안전부_긴급재난문자`
- 안내 페이지: `https://www.safetydata.go.kr/disaster-data/view?dataSn=228`
- API base: `https://www.safetydata.go.kr/V2/api/DSSP-IF-00247`
- 관련 전환 계획: `docs/live_source_migration_mois_api_plan.md`

## 1. 확정된 요청 파라미터

| 한글명 | 영문명 | 필수 | 구현 용도 |
|---|---|---:|---|
| 서비스키 | `serviceKey` | Y | 로컬 `.env`의 `SAFETYDATA_SERVICE_KEY` |
| 페이지당개수 | `numOfRows` | N | 페이지당 조회 건수 |
| 페이지번호 | `pageNo` | N | 페이지네이션 |
| 응답타입 | `returnType` | N | `json` 고정 |
| 조회시작일자 | `crtDt` | N | `YYYYMMDD`; polling overlap window 시작일 |
| 지역명 | `rgnNm` | N | 시도명 또는 시군구명; `서울특별시` 서버측 필터 시도 |

권장 요청:

```text
GET /V2/api/DSSP-IF-00247
  ?serviceKey=<secret>
  &returnType=json
  &numOfRows=<validated size>
  &pageNo=<1..N>
  &crtDt=<YYYYMMDD>
  &rgnNm=서울특별시
```

`serviceKey`는 URL 문자열에 직접 결합하거나 로그로 출력하지 않고 `httpx`의 `params`로 전달한다.

## 2. 확정된 레코드 필드

| 한글명 | 영문명 | 필수 | 정규화/보존 규칙 |
|---|---|---:|---|
| 일련번호 | `SN` | Y | stable source identifier. DB에는 `MOIS:{SN}` 형태로 namespace를 붙이는 것을 권장 |
| 생성일시 | `CRT_DT` | Y | 실제 문자 발송일시로 사용하고 `Asia/Seoul` timezone-aware datetime으로 변환 |
| 메시지내용 | `MSG_CN` | Y | `original_body`; 문구 교정·요약 없이 전체 원문 보존 |
| 수신지역명 | `RCPTN_RGN_NM` | Y | 서울 판정의 최종 기준이며 `sender_or_region`에 원문 보존 |
| 긴급단계명 | `EMRG_STEP_NM` | Y | raw payload 및 향후 분류 보조 metadata. 값: 긴급재난/안전안내/위급재난 |
| 재해구분명 | `DST_SE_NM` | Y | raw payload 및 템플릿 추천 보조 metadata. 예: 폭염/호우/홍수 등 |
| 등록일자 | `REG_YMD` | Y | audit metadata; `sent_at`으로 사용하지 않음 |
| 수정일자 | `MDFCN_YMD` | Y | audit/update metadata; `sent_at`으로 사용하지 않음 |

정규화 mapping:

```text
source_id        = "MOIS:" + str(SN)
sent_at          = parse(CRT_DT, Asia/Seoul)
sender_or_region = RCPTN_RGN_NM 원문
original_body    = MSG_CN 원문
source_url       = key가 없는 공식 dataset 안내 URL
detected_at      = 수집 시각(KST)
raw_payload      = 전체 record JSON (serviceKey 없음)
```

`REG_YMD`와 `MDFCN_YMD`는 문자 발송시각이 아니므로 dedup 또는 `sent_at`의 주 기준으로 사용하지 않는다.

## 3. 서울 대상 판정

사용자가 제시한 실제 예시처럼 하나의 문자가 여러 지역에 동시에 송출될 수 있다.

```text
RCPTN_RGN_NM = "경기도 광명시, 경기도 시흥시, 서울특별시 구로구"
```

이 레코드는 서울 대상에 포함한다.

### authoritative filter

최종 판정은 `MSG_CN` 본문이 아니라 `RCPTN_RGN_NM`으로 수행한다.

```python
is_seoul = any(
    normalized_region == "서울특별시"
    or normalized_region.startswith("서울특별시 ")
    for normalized_region in split_recipient_regions(RCPTN_RGN_NM)
)
```

최소 안전 기준은 `RCPTN_RGN_NM` 안에 행정구역 token으로 `서울특별시`가 포함되는지 확인하는 것이다.

- 포함: `서울특별시`, `서울특별시 구로구`, 다중 지역 중 `서울특별시 ...` 하나 이상
- 제외: 본문에만 서울이 언급되고 수신지역에는 서울이 없음
- 제외: 발신기관이 서울 소재이지만 수신지역에는 서울이 없음
- 제외: `서울`이라는 일반 문자열만 있고 공식 행정구역명 `서울특별시`가 없음

서울 25개 자치구 이름만 단독으로 반환되는 실제 API 변형이 발견되면, 25개 구 allow-list를 보조 판정에 추가한다. 다만 문서상 `rgnNm`은 시도명/시군구명을 지원하고 실제 표시 예시는 `서울특별시 구로구`이므로 기본 계약은 `서울특별시` token을 기준으로 한다.

### server-side + client-side 이중 필터

1. 요청에 `rgnNm=서울특별시`를 사용해 호출량을 줄인다.
2. 반환된 각 레코드에 대해 `RCPTN_RGN_NM`을 다시 검사한다.
3. 서버측 `rgnNm`이 다중 송출지역 레코드를 누락하는지 실제 contract test로 확인한다.
4. 누락이 확인되면 서버측 지역 필터를 제거하고 날짜 기준 전체 데이터를 받은 뒤 client-side 서울 필터만 사용한다.

즉, `rgnNm`은 최적화 수단이고 `RCPTN_RGN_NM` 검증이 정확성의 source of truth다.

## 4. Polling 조회창

`crtDt`는 조회시작일자이므로 단순히 오늘 날짜만 넣으면 자정 직전 발송분이나 API 반영 지연분을 놓칠 수 있다.

권장 정책:

- 매 poll마다 KST 기준 전일 날짜를 `crtDt`로 사용해 최소 1일 overlap 확보
- `pageNo=1`부터 페이지 순회
- API가 제공하는 total count/page metadata가 있으면 이를 사용
- metadata가 없으면 반환 건수가 `numOfRows`보다 작을 때 종료
- 동일 페이지 반복, SN 반복만 계속되는 상황에는 loop guard 적용
- DB의 `source_id`와 `raw_hash`로 최종 dedup

5분 polling interval은 유지한다.

## 5. 아직 live key로 확인해야 하는 계약

필드와 request parameter는 문서상 확인됐지만, 실제 JSON 1회 호출로 아래는 반드시 확인해야 한다.

1. JSON 최상위 envelope와 record list 경로
2. 성공/실패 result code의 field name과 값
3. total count/page metadata field
4. `CRT_DT`, `REG_YMD`, `MDFCN_YMD`의 실제 문자열 형식
5. `RCPTN_RGN_NM`이 문자열인지 배열인지
6. `rgnNm=서울특별시`가 다중지역 레코드도 반환하는지
7. `numOfRows` 허용 최대치
8. 기본 정렬이 최신순인지
9. service key가 decoded 또는 encoded 형태 중 무엇을 요구하는지
10. 429/쿼터 응답 형식과 호출 제한

이 확인은 다음 명령으로 수행한다.

```bash
python -m app.commands.inspect_mois_api
```

검사 시 원문 메시지, 수신지역, service key는 기본 출력하지 않고 schema/type/count만 출력한다.

## 6. Cutover 주의사항

기존 서울안전누리 ID와 신규 `SN`은 다른 namespace다. 같은 재난문자라도 신규 source에서 새 ID로 보일 수 있으므로 collector 교체 직후 현재 API window를 자동 발송하면 안 된다.

권장 절차:

1. tmux 서비스/Poller 정지
2. SQLite 및 WAL/SHM 일관 backup
3. API contract inspection
4. `source_cutover_mois --inspect`
5. 현재 API window를 `is_baseline=True`로 저장
6. baseline record에는 `telegram_deliveries`를 생성하지 않음
7. 이후 새 `MOIS:{SN}`만 모든 active 개인 구독자에게 fan-out

이 절차는 v0.4.1의 독립 사용자 구조를 그대로 유지한다.

## 7. 코드 변경 지점

- `app/config.py`
  - `SAFETYDATA_SERVICE_KEY`
  - MOIS base/dataset URL
  - 기존 서울안전누리 runtime 상수 제거
- `app/collector.py`
  - 세션/XHR collector 제거
  - MOIS GET + pagination + retry
- `app/mois_parser.py`
  - API envelope/record validation
  - field mapping
  - 서울 필터
- `app/commands/inspect_mois_api.py`
  - secret-safe contract 검사
- `app/commands/source_cutover_mois.py`
  - inspect/bootstrap
- `tests/`
  - API fixture, 서울 필터, pagination, cutover, A/B fan-out regression
- `.env.example`, README, CHANGELOG, source/runbook docs

## 8. 결론

현재 문서 정보만으로 다음은 확정 가능하다.

- 서울 대상 판정의 핵심 필드는 `RCPTN_RGN_NM`
- 서버 요청에는 `rgnNm=서울특별시`를 사용할 수 있음
- 정확성을 위해 응답의 `RCPTN_RGN_NM` client-side 검증을 반드시 병행
- `SN`은 신규 stable ID, `CRT_DT`는 발송시각, `MSG_CN`은 원문
- `EMRG_STEP_NM`과 `DST_SE_NM`은 분류 보조 metadata로 보존
- 기존 Telegram 독립 사용자/템플릿/AI 기능은 normalized record 이후 그대로 재사용

실제 service key를 넣은 1회 inspection 후에는 JSON envelope와 pagination만 확정하면 구현을 시작할 수 있다.

# 세션 정리 · 이어하기용 (2026-05-05)

다음 대화에서 바로 맥락 잡을 수 있도록 **이전 대화와 구현 내용**을 압축 정리했습니다.

## 합의·원칙

1. **1번(기존 스모크)**: `coupang_crawler._run_smoke_worker` **본문 미수정**. 키워드당 창 1회, 구글→쿠팡→검색→probe 경로 유지.
2. **2번(MODE2)**: 별도 모듈 `coupang_mode2_session.py` — 동일 부트스트랩 후 **단일 창**에서 검색창만 바꿔 연속 수집.
3. **타이밍**: 키워드 간 `random.uniform(7, 10)` 초, 마지막 키워드 처리 후 **5초** 뒤 브라우저 종료.
4. **스코어 공식**: `final_score = 0.5*keyword_score + 0.5*sales_power` 등 **추천 엔진 핵심 수식 변경 없음**.

## 이번에 추가·변경된 구현

| 항목 | 설명 |
|------|------|
| **MODE2** | `coupang_mode2_session.run_mode2_sequential_blocking` — Playwright를 직접 열어 부트스트랩 후 루프. |
| **Probe JS** | `coupang_mode2_probe_eval.js` — 스모크 worker 내부 `_probe_js`와 동일 내용 유지 필요(추출 파일). |
| **DB 007** | `sql/007_coupang_autocollect_mode2_usage.sql` — 2번으로 처리한 `(batch_token, keyword_text)` 기록, 동일 배치 재선택 방지. |
| **DB 함수** | `query_mode2_autocollect_used_keywords`, `insert_mode2_autocollect_keyword_usage`; **1146**(테이블 없음) 시 `ensure_schema()` 후 1회 재시도. |
| **MODE2 시작** | `run_mode2_sequential_blocking` 초반에 `ensure_schema()` 호출(Railway 등 미적용 DB 자가 복구). |
| **4번 UI** | 자동 쿠팡 수집: **1번 / 2번** 라디오, 2번 시 「이미 처리한 키워드 제외」체크, 2번은 `ThreadPoolExecutor`에서 블로킹 실행. |
| **대시보드 문구** | `04_revenue_keywords` 상단 expander, `web_sidebar`, `coupang_tab` — 운영 흐름·테이블 안내. |
| **캐시** | `web_common._TOOL_RESOURCE_VERSION` (최근 **5**) — `get_tool()` 캐시 무효화용. |

## 쿠팡 스냅샷 `source_type`

- 엔진/1번 자동수집 경로: `recommend_engine` 또는 스모크 `smoke` (기존).
- **2번 단일창**: `recommend_engine_mode2`, payload에 `batch_token` 포함 → `raw_json`에서 추적 가능.

## Railway에서 겪은 오류

- **`ProgrammingError(1146)`**: `coupang_autocollect_mode2_usage` 없음 → `sql/007` 미적용 또는 배포에 SQL 미포함.  
  대응: `ensure_schema()` + DB 함수 1146 재시도 + MODE2 시작 시 `ensure_schema()`.

## 성공 로그 예시 (MODE2)

```
[MODE2] ensure_schema() applied (includes sql/007 if present)
[MODE2] goto https://www.google.com/
[MODE2] google search → coupang link (query='쿠팡')
[MODE2] keyword 1/3: '...'
[MODE2] sleep 8.xx s before next keyword
...
[MODE2] final hold 5s before browser close
```

## 기존 핸드오프 문서

- 전체 추천 엔진·설정·스키마 개요: `docs/HANDOFF_RECOMMEND_KEYWORD_ENGINE.md` (갱신 권장).

## 다음에 할 일(제안)

- `coupang_mode2_probe_eval.js`와 `coupang_crawler` 스모크 probe **동기화** 절차 문서화(한쪽 수정 시 다른 쪽 반영).
- 3번 탭에서 `recommend_engine_mode2` 필터 조회 필요 여부.
- MODE2 실패 시 폴백(남은 키워드만 1번) 정책 합의.

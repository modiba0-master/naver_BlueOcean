# Handoff — 2026-05-03

## 1) 이번 세션 핵심 목표/결과

- 쿠팡 탭 구조(`coupang_tab.py`)는 유지하면서, `BlueOceanTool.start_analysis` 기반 분석 로직을 소싱 중심으로 고도화.
- 기능 플래그 기반 3단계 롤아웃(stage 1→2→3) 반영.
- 실행로그 탭에 stage 검증용 진단 패널 추가.
- `AttributeError: get_last_analysis_detail_df` 크래시를 안전 가드로 즉시 복구.
- 커밋/푸시/배포까지 완료.

## 2) 반영된 기술 변경

### 분석 엔진 (`blue_ocean_tool.py`)

- 롤아웃 단계 플래그 추가:
  - `BLUEOCEAN_SOURCING_STAGE` (0~3)
  - 미설정 시 `settings.sourcing_rollout_stage` 참고, 기본 `0`
- 신규 함수:
  - `classify_keyword_intent()` (탐색형/구매형/정보형)
  - `detect_seasonality()` (`seasonal`/`steady`/`trend`)
  - `_compute_sales_power()`
  - `_compute_competition_score_advanced()`
  - `_parse_number_from_text()`, `_safe_float()` 등 유틸
- 확장 함수:
  - `get_product_info(..., include_competition=True)`  
    광고비율/브랜드 점유/신상품비율 추출
  - `get_coupang_top10_stats(..., include_profile=True)`  
    리뷰/가격 기반 판매력 프로파일 + `sales_power`
- 분석 row 확장 필드:
  - `intent`, `sales_power`, `competition_score`, `season_type`
- stage 3에서 재가중치 최종 점수 계산 로직 반영(구매형 보정 포함).
- 실행 직후 상세 DF 접근용:
  - `get_last_analysis_detail_df()`

### 대시보드 (`app_web.py`)

- 실행로그 탭에 expander 추가:
  - `소싱 고도화 진단 (Stage 1~3)`
  - `intent`, `season_type`, `sales_power`, `competition_score`, 점수/밴드 확인
- 캐시된 구버전 인스턴스 대응:
  - `getattr(tool, "get_last_analysis_detail_df", None)` 가드
  - 없으면 빈 DataFrame 사용 (크래시 방지)

## 3) 커밋/푸시/배포 이력 (이번 세션)

- `2255b76`  
  `feat(analysis): staged sourcing rollout with dashboard diagnostics`
- `234438a`  
  `fix(web): guard detail dataframe accessor for cached tool instances`

원격 반영:
- `origin/master` 푸시 완료

배포:
- `railway up --detach` 실행 완료

## 4) 검증 체크 포인트 (다음 세션 시작용)

1. 실행로그 탭:
   - stage 로그(`stage=1/2/3`) 출력 확인
   - 진단 패널 컬럼 노출 확인
2. 시장성 점수 조회 탭:
   - `최종 점수`, `판단 밴드` 분포 변화 확인
3. 단계별 롤아웃:
   - `stage=1` 안정화 → `stage=2` → `stage=3` 순차
4. 쿠팡 탭:
   - 기존 동작 유지 확인 (본 세션에서 구조 변경 없음)

## 5) 유의사항

- DB 스키마는 변경하지 않음 (기존 저장 경로 유지).
- `.chroma/`는 로컬 산출물로 보이며 커밋 제외 상태 유지.


# BlueOcean Handoff (2026-04-27)

다음 호출에서 바로 이어서 작업할 수 있도록, 오늘 반영한 변경과 현재 상태를 정리합니다.

## 1) 오늘 반영 완료 사항

- 웹 실행 기본화 유지, `--desktop`/`--cli` 옵션 분기 유지
- 블루오션 점수 100점 스케일 정규화 적용
- 모바일 지표 중심 집계로 전환
  - 검색량: `monthlyMobileQcCnt`
  - 클릭량: `monthlyAveMobileClkCnt`
- 결과 표시 정책
  - 점수 기준 상위 50개 선별
  - 화면 표시는 모바일 검색량 내림차순
- DB 조회 정렬 정책
  - 월평균 검색수 내림차순 우선
  - 동률 시 블루오션 점수 내림차순

## 2) 성능 최적화 반영 사항

- 분석 후보 1차 선별식 변경
  - 점수 = 모바일 클릭수 70% + 모바일 검색수 30%
- 모드별 정밀 분석 개수
  - 빠른 모드: 상위 20개
  - 정밀 모드: 상위 60개
- 트렌드 API 호출 제한
  - 상위 20개만 트렌드 API 호출
  - 나머지는 기본 지표로 계산
- 캐시
  - 메모리 캐시 유지
  - DB 캐시 24시간 TTL 추가 (`query_recent_keyword_cache`)
  - 캐시 키 조건: `keyword_text + start_date + end_date` 기반 최근 성공 실행

## 3) 블루오션 점수식(최신)

단순 검색량 우선이 아니라, 저경쟁 + 수요 + 상승 추세를 반영:

- 경쟁도: `log1p(product_count)` (작을수록 유리)
- 수요강도: `log1p(avg_qc) * (1 + avg_ctr/100)`
- 성장가중: 최근 3개월 평균 / 이전 3개월 평균 (`0.7 ~ 1.8` 클램프)
- raw score
  - 트렌드 존재: `(수요강도 * 성장가중 / max(1.0, 경쟁도)) * 100`
  - 트렌드 없음: `(수요강도 / max(1.0, 경쟁도)) * 100`
- 이후 전체 결과에서 0~100으로 정규화하여 최종 `블루오션 점수` 표시

## 4) DB 저장 범위(현재)

- `analysis_runs`
  - 시드 키워드 원문, 시작/종료일, 성공/실패, 결과 건수, 에러
- `keyword_metrics`
  - 키워드별 월평균 검색/클릭/CTR, 상품수, 블루오션 점수, 전략 문구
- `keyword_trends_monthly`
  - 월별 ratio, 추정 검색량, 추정 클릭량

현재 자동 누적 저장이며, 별도 보관 주기/삭제 정책은 코드상 없음.

## 5) 최근 배포 커밋

- `72d9204`: fast/precise + 기본 60일
- `7957c6f`: seed 카테고리 하드 필터 제거
- `58c2ec8`: 트렌드 비어도 결과 노출
- `cca53e1`: 모바일 지표/100점 스케일
- `40a7c62`: 70/30 선별 + fast20/precise60 + trend20 + DB 24h cache
- `538db3d`: 저경쟁/상승추세 중심 점수식

## 6) 다음 호출 권장 시작점

- 실제 운영 데이터 기준 점수 결과 샘플 2~3개를 확인
  - 목표: "상품수 적고 검색량 상승" 키워드가 상위에 노출되는지 검증
- 필요시 가중치 미세 조정
  - 성장가중 클램프 범위(0.7~1.8)
  - 수요강도 내 CTR 가중 비율
  - 빠른/정밀 모드 deep_limit 조정

---

## 7) 2026-04-27 심야 추가 반영 사항 (최신)

- 대시보드 `최근 DB 결과 조회`를 `시장성 점수 조회 (DB)`로 전환
  - 점수식: `수요 × 트렌드 × 전환 ÷ 경쟁`
  - 구성 점수 컬럼(수요/트렌드/전환/경쟁/최종) 표시
- `keyword_metrics`에 쿠팡 지표 저장 컬럼 확장
  - `top10_avg_reviews`, `top10_avg_price`
- 쿠팡 크롤러 모듈 분리/도입
  - 신규 파일: `coupang_crawler.py`
  - `blue_ocean_tool.py`는 해당 모듈 호출 방식으로 치환
- 성능/복잡도 간소화 적용
  - 호출 순서: `cache -> requests(retry1) -> selenium fallback`
  - Selenium 랜덤 대기 축소 (`1.0~2.0s`)
  - 운영 기본 `HEADLESS=True`
  - `blue_ocean_tool.py` 내부 중복 쿠팡 캐시 제거
- 관리자 인증 게이트 추가
  - `app_web.py`에서 로그인 전 대시보드 접근 차단
  - 필요 환경변수: `MODIBA_ADMIN_ID`, `MODIBA_ADMIN_PASSWORD`
  - 로그인/로그아웃 흐름 추가
- 쿠팡 로그인 세션 2단계 운영 지원
  - 1단계(1회): 전용 프로필로 수동 로그인 세션 저장
  - 2단계(운영): headless 재사용 실행
  - 가이드 문서: `docs/coupang_login_session_guide.md`

## 8) 현재 확인된 이슈/원인

- 쿠팡 값 미표시는 대시보드 문제가 아니라 **크롤러 수집 실패**가 원인
  - requests 경로: `403`
  - selenium 경로: `Access Denied` + `TimeoutException`
- 로그인 세션 재사용 테스트 중 확인된 추가 이슈
  - 기존 사용자 프로필 재사용 시 `DevToolsActivePort` 크래시 가능
  - 전용 프로필 경로 사용으로 충돌 위험 완화

## 9) 다음 호출 즉시 실행 체크리스트

1. Railway 변수 점검
   - `MODIBA_ADMIN_ID`, `MODIBA_ADMIN_PASSWORD`
   - (선택) `COUPANG_CHROME_USER_DATA_DIR`, `COUPANG_CHROME_PROFILE`, `COUPANG_HEADLESS`
2. 로컬에서 1회 로그인 세션 저장
   - `python .\coupang_crawler.py --bootstrap-login --wait-seconds 180`
3. 운영 모드 테스트
   - `python .\coupang_crawler.py --keyword "돼지족발"`
4. 분석 실행 후 로그 확인
   - `cache_hit`, `requests_ok`, `selenium_ok`, `failed`, `success_rate`
5. 쿠팡 값 미수집 지속 시
   - 점수 테이블에서 쿠팡 항목 `N/A` 표기 전략 검토
   - 접근 정책(프록시/공식 API 대체) 의사결정 필요

## 10) 최신 커밋 체인 (마감 시점)

- `148c5af`: 관리자 로그인 게이트 적용
- `e407809`: 쿠팡 로그인 세션 부트스트랩 + 운영 가이드
- `ab1fca7`: 쿠팡 크롤러 모듈화 및 흐름 간소화
- `0d66deb`: 시장성 점수 대시보드 + 쿠팡 전환 지표 연동


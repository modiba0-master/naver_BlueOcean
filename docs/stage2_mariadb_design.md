# BlueOcean Stage 2 - Railway MariaDB 기본 설계

## 목표
- 기존 Excel 저장 중심 흐름을 DB 중심 누적/검색형으로 전환한다.
- Railway 환경에서는 MariaDB(`MYSQL_URL`)를 기본으로 사용한다.

## 산출물
- 스키마: `sql/001_blue_ocean_mariadb.sql`
- DB 레이어: `db.py`
- 검증 Harness: `harness_db.py`

## 테이블 설계 요약

### 1) analysis_runs
- 실행 단위 메타데이터 저장
- 시드 키워드 원본, 시작/종료일, 상태, 결과 건수, 에러 메시지 관리

### 2) keyword_metrics
- 실행별 키워드 결과 저장
- 블루오션 점수, 검색량 추정, 클릭 추정, 상품수, 전략 문구 저장
- `run_id + seed_keyword + keyword_text` 유니크로 중복 방지

### 3) keyword_trends_monthly
- 키워드 월별 비율/추정 볼륨 저장
- `metric_id + trend_month` 유니크

## Harness First 검증 순서
- H1: 스키마 생성 가능
- H2: 실행 메타 insert 가능
- H3: 샘플 키워드 결과 insert/upsert 가능
- H4: 월별 트렌드 insert/upsert 가능
- H5: 상위 점수 조회 가능

## 실행 방법
```bash
cd "g:\내 드라이브\Antigravity_Work\01_naver_BlueOcean"
python harness_db.py
```

## 다음 단계(Stage 3) 제안
- `blue_ocean_tool.py`의 `all_results` 생성부를 DB insert 흐름으로 연결
- Excel 저장은 옵션화(기본 OFF)
- UI에서 "DB 저장/검색" 버튼 추가

## 운영 점검 기록 (2026-04-10)
- Railway CLI 로그인 확인: `railway whoami` -> `modiba0@gmail.com`
- 링크 상태 확인: `railway status` -> `modiba-blueocean / production / blueocean-app`
- 변수 확인: `railway variables` 에 `MARIADB_HOST/PORT/USER/PASSWORD/DATABASE` 확인
- 송수신 검증: `railway ssh -s blueocean-app python harness_db.py`
  - H1 schema ensure: OK
  - H2 run insert: OK
  - H3/H4 metrics, trends insert/upsert: OK
  - H5/H6 read query: OK

## 운영 메모 (로그인 세션 보존)
- Railway 로그인 세션은 터미널 프로파일 기준으로 유지된다.
- 세션 확인은 아래 명령으로 빠르게 점검한다.
  - `railway whoami`
  - `railway status`
- 세션이 만료된 경우에만 `railway login`을 재실행한다.

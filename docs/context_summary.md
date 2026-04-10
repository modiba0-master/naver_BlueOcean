# 모디바 계열 프로젝트 — 맥락 요약 (에이전트 핸드오프용)

이 문서는 **새 채팅/다음 에이전트**가 저장소와 운영 결정을 빠르게 이어 받도록 정리했습니다. 코드 세부사항은 각 모듈 docstring·`sql/`·`.env.example`을 보조로 참고하세요.

---

## 이 문서를 읽는 방법

1. **현재 저장소(`Modiba_GoodPrice`)**는 아래 **「프로젝트 A」**가 전부입니다.
2. **같은 PC의 다른 폴더**에서 진행된 작업은 **「프로젝트 B·C」**로 이름만 붙여 두었습니다. 해당 경로가 없으면 과거 세션 기준 설명일 수 있습니다.
3. 운영 URL·DB 우선순위는 `.cursor/rules/modiba-goodprice-railway.mdc`와 동일하게 **Railway 공개 URL + `MYSQL_URL`**을 기준으로 합니다.

---

## 프로젝트 A — 모디바 착한 가격찾기 (Modiba_GoodPrice)

**역할:** 네이버 쇼핑·쿠팡에서 키워드 검색 상위 상품 가격을 수집하고, MariaDB에 적재하며, Streamlit 대시보드와 선택적 가격 알림(ntfy)을 제공합니다.

### 핵심 로직

| 영역 | 설명 | 주요 파일 |
|------|------|-----------|
| 네이버 수집 | 쇼핑 검색 Open API `shop.json`, 상위 N개(기본 10), 제목 HTML 제거·옵션 키워드(`OPTION_KEYWORDS`) 추출 | `collector.py` |
| 쿠팡 수집 | `COUPANG_MODE`: **API** / **CRAWL** / **AUTO**. API일 때 `COUPANG_PRODUCT_API_KIND`: **affiliate_search**(기본, 파트너스 쇼핑몰 키워드 검색) 또는 **seller_catalog**(판매자 본인 등록상품 목록). `affiliate_search`는 `COUPANG_PARTNERS_*` 키 우선 사용 | `coupang_collector.py`, `config_credentials.py` |
| 일괄 작업 | `.env`의 `SEARCH_KEYWORD`·네이버 키로 네이버+쿠팡 수집 → `product_monitoring` INSERT → 가격 변동 시 ntfy | `scheduler_runner.py` → `run_collection_job()`; 진입점 `run_once.py` |
| 웹 UI | Streamlit: 실시간 검색, DB 조회, 알림 토픽 설정, 서버 IP·DB 모드 표시 등 | `main.py` |
| DB 연결 | **`MYSQL_URL` / `MARIADB_URL` / `DATABASE_URL` 우선**(있으면 `MARIADB_*` 플러그인 변수는 무시). 스키마 `mysql`/`mariadb`만 허용 | `database.py` (`_dsn_from_env`) |
| SKIP_DB | `SKIP_DB=True`면 DB 생략·콘솔 표 위주(로컬 점검). 운영 기본은 `False` | `console_report.py`, `run_once.py` docstring |
| 가격 알림 | 동일 플랫폼·몰·상품명 기준 **직전 `total_price`와 불일치** 시 ntfy (토픽은 DB에서 로드) | `price_alerts.py`, `notifications.py` |
| 입력 검증 | 빈 키워드 등 폼 검증·하네스 | `input_validation.py`, `tests/test_input_harness.py` |

### DB 구조 (MariaDB)

- **`product_monitoring`** (현재 적재 테이블)  
  수집 시각, `platform`(`NAVER`/`COUPANG`), `keyword`, `rank`, `mall_name`, `product_name`, `option_label`, `base_price`, `delivery_fee`, `total_price`, `delivery_note`, `created_at`.  
  네이버는 최저가 중심, 쿠팡은 상품가+배송비 반영.

- **`search_results`** (레거시)  
  초기 설계 스냅샷용. `004_migrate_search_results_to_product_monitoring.sql`로 `product_monitoring`으로 이전 가능.

- **`alert_recipients`**  
  단일 행 `id=1`. **컬럼명은 `phone1`~`phone5`이지만 실제 값은 ntfy 토픽(또는 URL) 최대 5개**로 재사용됨 (`database.py` 주석 참고).

### 주요 결정 사항

- **Harness First:** 수집기·입력·쿠팡 가드 등 `tests/test_*_harness.py`로 방어 로직 검증.
- **Railway:** 배포 시 **`MYSQL_URL` 단일 소스** 권장(내부 DB와 플러그인 변수 병행 시 혼선 방지). 공개 URL 예시: `https://modibagoodprice-production.up.railway.app` (실제 값은 Railway 대시보드 기준).
- **네이버 API:** 기존 블루오션 프로젝트와 동일 앱 자격(`NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET`) 사용.
- **쿠팡 API 분리:** 판매자(Wing) 키와 파트너스 키를 분리.  
  - 쇼핑몰 키워드 검색(`affiliate_search`)은 `COUPANG_PARTNERS_ACCESS_KEY` / `COUPANG_PARTNERS_SECRET_KEY` 권장  
  - 판매자 목록(`seller_catalog`)은 `COUPANG_ACCESS_KEY` / `COUPANG_SECRET_KEY`
- **쿠팡 출구 IP:** 쿠팡 403 방지를 위해 대시보드(실제 실행 프로세스) 출구 IP를 UI에서 표시·비교. `COUPANG_REGISTERED_EGRESS_IP`(또는 `COUPANG_WHITELIST_IP`)로 Wing 등록값과 대조 가능.
- **쿠팡 경로 전환:** 공식 API 게이트웨이 경로는 환경변수로 교체 가능 (`COUPANG_AFFILIATE_SEARCH_PATH`, `COUPANG_MARKETPLACE_PATH_TEMPLATE`).
- **알림:** ntfy(`NTFY_SERVER` 등); 토픽은 대시보드에서 저장해 DB에 반영.

### 진입점·검증

- 대시보드: `streamlit run main.py` (루트에서).
- 크론/일회: `python run_once.py` — `SEARCH_KEYWORD` 필수.
- 스케줄러: `scheduler_runner.py` — 매일 **09:00 KST** 등(파일 내 APScheduler 설정 확인).

---

## 프로젝트 B — 모디바 마진 시뮬레이션 하네스 (Modiba_System)

**저장 위치(예상):** `Antigravity_Work/Modiba_System/` (현재 워크스페이스에 없을 수 있음)

**역할:** 네이버 API·DB 연동 **이전**에, 경쟁사 가격 변동 시 마진·마진율을 숫자로 검증하는 **`test_margin_harness.py`** (Harness First).

**후속 설계(대화 기준):** `collector.py` / `simulator.py` / `database.py` 분리 예정이었음.

---

## 프로젝트 C — 친절한 모디바 명세서 OCR (02_Invoice_OCR)

**저장 위치(예상):** `G:\내 드라이브\Antigravity_Work\02_Invoice_OCR\` 등 (본 저장소와 별도)

**역할:** Google Cloud Vision `document_text_detection` + Gemini로 명세서 이미지 → 항목 정제 → Pandas 엑셀. GUI 파일 선택·결과 폴더·키 환경변수화 등은 `invoice_master.py` / `invoice_app_v2.py` 계열로 진행된 바 있음.

**본 저장소와 관계:** 브랜딩만 공유하고 **코드베이스는 분리**로 이해하면 됨.

---

## 보안·운영 주의

- **비밀번호·API 키는 저장소에 커밋하지 말 것.** `.env`는 gitignore 대상 유지.
- 과거 채팅에 노출된 자격 증명은 **재발급·교체** 권장.

---

## 빠른 체크리스트 (다음 에이전트)

- [ ] Railway Variables에 `MYSQL_URL` 및 네이버 키, `SEARCH_KEYWORD` 확인.
- [ ] `SKIP_DB` 운영 값이 의도대로인지(`False` 권장).
- [ ] 스키마: 신규 환경이면 `database.py`의 `CREATE_*` 또는 `sql/*.sql` 적용 순서 확인.
- [ ] 쿠팡 검색 API 종류 확인: `COUPANG_PRODUCT_API_KIND=affiliate_search`(권장) 또는 `seller_catalog`.
- [ ] `affiliate_search` 사용 시 파트너스 키(`COUPANG_PARTNERS_ACCESS_KEY`, `COUPANG_PARTNERS_SECRET_KEY`)가 실제 배포 환경에 설정됐는지 확인.
- [ ] 쿠팡 401이면 키 유형(Wing vs 파트너스)부터 확인, 403이면 출구 IP 화이트리스트부터 확인.
- [ ] 쿠팡 사용 시 `COUPANG_MODE`·API 경로·가드 관련 env 확인 (`docs/coupang_api_checklist.md`, `RAILWAY_NO_DOCKER_GUIDE.md` 참고).

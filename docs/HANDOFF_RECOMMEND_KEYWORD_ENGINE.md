# Handoff: 매출 키워드 추천 엔진 & 대시보드 (2026-05)

다음 대화에서 이어가기 위한 요약입니다.

## 목표(완료 방향)

- **추천 엔진**: 연관 확장 → 네이버 모바일 필터 → `keyword_score` → (옵션) 쿠팡 → `final_score = 0.5*keyword_score + 0.5*sales_power` 유지.
- **운영**: 설정 분리(YAML/ENV/UI), Streamlit에서 `asyncio.run` 충돌 회피(워커 스레드 + 엔진 내부 ThreadPool), 스키마 자동 적용.
- **발굴 UX**: 1차 카테고리(버티컬) + 2차 매출 규모 프리셋(`config/revenue_keyword_discovery.yaml`).
- **중간 단계**: 쿠팡 전 스코어 후보 DB(`recommended_keyword_candidates`, `sql/005`) + 선택 CSV(`reports/`).
- **쿠팡 DB**: 추천 엔진 크롤 결과를 3번 탭과 동일 테이블 `coupang_search_runs` / `coupang_search_ranked_items`에 `source_type=recommend_engine`으로 저장(`insert_coupang_search_snapshot`).

## 주요 파일

| 영역 | 경로 |
|------|------|
| 엔진 | `recommended_keyword_engine.py` |
| 설정 로드 | `revenue_keyword_settings.py`, `revenue_keyword_discovery.py` |
| YAML | `config/revenue_keyword_guide.yaml`, `config/revenue_keyword_discovery.yaml` |
| DB | `db.py` (`ensure_schema`, `insert_recommended_keywords`, `insert_recommended_keyword_candidates`, `build_recommend_engine_coupang_snapshot_payload`) |
| SQL | `sql/003_*`, `004_*`, `005_recommended_keyword_candidates_mariadb.sql` |
| Streamlit | `app_web.py`, `view_pages/04_revenue_keywords.py`, `web_common.py` (`get_tool` 캐시 버전 `_TOOL_RESOURCE_VERSION`) |
| CLI | `run_recommend.py` |

## 알려진 이슈 / 확인 사항

1. **`meta`에 `precoup_candidate_count` 없음** → Python 프로세스가 예전 `recommended_keyword_engine`을 들고 있는 경우. **Streamlit 완전 재시작**으로 해결.
2. **precoup DB 0행** → `005` 테이블 생성 여부, DSN, UI에서 후보 DB 체크. 로그 `[Recommend] precoup built=...` 확인.
3. **`run_recommended_keyword_engine` AttributeError** → `@st.cache_resource` 구버전 인스턴스; `web_common._TOOL_RESOURCE_VERSION` 올려 캐시 무효화.
4. **쿠팡 전부 탈락** → `after_coupang: 0`이면 `recommended_keywords`는 0행이 정상; 스냅샷/검증 기준·크롤 환경 점검.

## Git / 배포

- `.gitignore`: `.chroma/`, `reports/recommended_precoup_*.csv` 제외.
- `config.json`: 로컬에서 `skip_coupang_top10_in_analysis` 등 변경됐을 수 있음(커밋 시 diff 확인).

## 다음에 할 일(제안)

- Railway/운영 DB에 `005` 적용 및 `ensure_schema` 로그 확인.
- 쿠팡 통과율·`small_10m` 프리셋 `min_review`/`min_avg_price` 조정 여부 검토.

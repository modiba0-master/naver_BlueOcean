"""4. 매출 키워드 추천 — 설정 연동 + 워커 스레드에서 엔진 실행 (asyncio.run 충돌 방지)."""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from revenue_keyword_discovery import TIER_UI_KEYS, VERTICAL_UI_OPTIONS
from web_common import get_tool

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_GUIDE_PATH = _ROOT / "config" / "revenue_keyword_guide.sample.yaml"


def main_page() -> None:
    st.subheader("매출 발생 키워드 추천")
    st.caption(
        "1차: **카테고리(버티컬)** → 2차: **목표 매출 규모**로 발굴 범위를 잡고, "
        "`config/revenue_keyword_discovery.yaml`에서 네이버 쇼핑 카테고리·검색량·쿠팡 검증을 조정합니다. "
        "가중 스코어·final_score 공식은 기존과 동일합니다. "
        "추가로 `revenue_keyword_guide.yaml` / ENV / 아래 슬라이더가 **필터·쿠팡 상위 N**을 덮어씁니다."
    )

    with st.expander("현재까지 운영 방식 (대시보드 기준)", expanded=False):
        st.markdown(
            """
#### 설정 반영 순서
- **UI(이 탭 슬라이더·체크박스)** → **환경변수** → **`config/revenue_keyword_guide.yaml`** / **`config/revenue_keyword_discovery.yaml`** → 코드 기본값  
- Discovery(카테고리·매출 티어)는 `revenue_keyword_discovery.yaml`과 아래 1·2차 선택으로 조정됩니다.

#### 4번 탭에서 하는 일 (한 줄씩)
1. **추천 엔진 실행**: 워커 스레드에서 `BlueOceanTool.run_recommended_keyword_engine` 호출 (Streamlit 메인 스레드에서 `asyncio.run`을 쓰지 않음).  
2. **연관 확장·모바일 필터·키워드 스코어**까지 계산 후, 옵션에 따라 **쿠팡 검증·Top10 스냅샷**을 진행하거나 **쿠팡 단계 생략**으로 끝낼 수 있습니다.  
3. 실행마다 **`batch_token`**(UUID)이 부여되며, 로그/완료 메시지에 표시됩니다.

#### DB·파일 저장 위치
| 구분 | 저장 위치 | 비고 |
|------|------------|------|
| 쿠팡 **전** 스코어 후보 | `recommended_keyword_candidates` | 배치·`rank_position`(엔진 내 순위)·카테고리 경로 등 |
| 쿠팡 검증까지 반영된 최종 추천 | `recommended_keywords` | `after_coupang` 등 조건 충족 시 적재 |
| 쿠팡 검색 **Top10** 스냅샷 | `coupang_search_runs`, `coupang_search_ranked_items` | **3번 탭과 동일 테이블**. 추천 엔진/자동수집 경로는 `source_type=recommend_engine` |
| 쿠팡 전 CSV (옵션) | `reports/recommended_precoup_{batch_token}.csv` | 체크 시 생성 |

#### 이 탭 하단 「추천 키워드 자동 쿠팡 수집」
- **1번**: 배치에서 고른 키워드를 **순서대로**, 키워드마다 **브라우저(스모크) 1회** 실행 — **3번 탭과 동일**한 스모크(구글→쿠팡→검색→probe).  
- **2번(실험)**: 창 **1회**만 열고 동일 부트스트랩 후, 쿠팡 검색창만 바꿔 연속 수집(키워드 간 **7~10초 랜덤** 대기, 마지막 후 **5초** 뒤 종료). DB `source_type=recommend_engine_mode2`.  
- 2번으로 처리한 키워드는 **`coupang_autocollect_mode2_usage`** 에 기록되어 **동일 배치에서 기본 제외**됩니다.  
- 옵션: **BLOCKED** 계열 시 **3분 대기 후 1회 재시도** (1번·2번 공통 옵션).  
- 결과는 **`coupang_search_runs` / `coupang_search_ranked_items`** 에 쌓이며, 3번 탭 하단 **「추천엔진 추출 키워드 조회」**에서 확인할 수 있습니다.

#### 3번 탭과의 역할 나눔
- **3번**: 단일 키워드 수동 스모크·저장 Top10 조회·추천 배치에서 고른 키워드의 DB 뷰.  
- **4번**: 추천 엔진 실행·후보 배치 관리·**자동 순차 쿠팡 수집** 트리거.

#### 운영 시 참고
- 쿠팡 측 **WAF·Access Denied**는 네트워크·세션 패턴에 따라 발생할 수 있어, **한 번에 많은 키워드**보다 소량·여유 간격을 권장합니다.  
- `precoup DB=0` 등은 `sql/005` 적용·DB DSN·체크박스 상태를 로그/경고 문구대로 확인하세요.
            """
        )

    tool = get_tool()

    with st.expander("추천 엔진 실행", expanded=True):
        cat_pick = st.selectbox(
            "1차 · 키워드 발굴 카테고리",
            options=["(카테고리 제한 없음)"] + VERTICAL_UI_OPTIONS,
            index=0,
            key="rev_discovery_vertical",
            help="네이버 쇼핑 첫 카테고리·경로와 매칭합니다. 시드 힌트가 앞에 붙습니다.",
        )
        tier_labels = [x[1] for x in TIER_UI_KEYS]
        tier_choice = st.radio(
            "2차 · 목표 매출 규모 (키워드 큐모)",
            options=tier_labels,
            index=1,
            horizontal=True,
            key="rev_discovery_tier",
            help="소규모=니치·상한 검색량, 대규모=고검색·쿠팡 리뷰 기준 강화 등 프리셋입니다.",
        )
        tier_key = next(k for k, lab in TIER_UI_KEYS if lab == tier_choice)

        seed_in = st.text_input(
            "추가 시드 키워드 (쉼표 구분, 선택)",
            value=st.session_state.get("seed_input", "") or "",
            key="rev_engine_seeds",
            help="카테고리 힌트 시드 뒤에 이어서 연관 확장에 사용됩니다.",
        )
        c1, c2 = st.columns(2)
        with c1:
            top_out = st.slider("TOP 출력 개수", min_value=5, max_value=50, value=20, key="rev_top_out")
            coup_cap = st.slider("쿠팡 분석 상위 개수", min_value=10, max_value=200, value=100, key="rev_coup_cap")
        with c2:
            sem_ui = st.slider("쿠팡 동시 실행 수", min_value=1, max_value=10, value=4, key="rev_sem")
            min_vol = st.number_input("최소 검색량(모바일)", min_value=0, value=500, key="rev_min_vol")
            min_ctr = st.number_input("최소 CTR (%)", min_value=0.0, max_value=30.0, value=1.0, step=0.1, key="rev_min_ctr")

        with st.expander("중간 저장·쿠팡 DB (3번 탭과 동일 `coupang_search_*`)", expanded=False):
            st.caption(
                "연관 확장·네이버 지표까지는 가볍고, **쿠팡 크롤은 키워드당 부담**이 큽니다. "
                "먼저 DB/CSV에 **쿠팡 전 후보**를 남겨 품질을 보고, 필요할 때만 쿠팡 단계를 켜세요."
            )
            st.checkbox("쿠팡 전 스코어 후보 → MariaDB (`recommended_keyword_candidates`)", value=True, key="rev_precoup_db")
            st.checkbox("쿠팡 전 스코어 후보 → CSV (`reports/recommended_precoup_*.csv`)", value=False, key="rev_precoup_csv")
            st.checkbox(
                "쿠팡 크롤 시 순위 스냅샷 → DB (`coupang_search_runs` / `coupang_search_ranked_items`, 3번 탭과 동일)",
                value=True,
                key="rev_coup_snap",
            )
            st.checkbox(
                "쿠팡 검증·판매력 단계 생략 (키워드점수만으로 최종 순위, 크롤 0회)",
                value=False,
                key="rev_skip_coup",
            )

        run_eng = st.button("추천 엔진 실행", type="primary", key="rev_run_engine")
        if run_eng:
            vert = None if cat_pick.startswith("(") else str(cat_pick).strip()
            ui_settings: Dict[str, Any] = {
                "filter": {"min_search": int(min_vol), "min_ctr": float(min_ctr)},
                "step4": {"top_n": int(coup_cap), "semaphore": int(sem_ui)},
                "discovery": {"vertical": vert, "tier": tier_key},
                "precoupang": {
                    "save_db": bool(st.session_state.get("rev_precoup_db", True)),
                    "save_csv": bool(st.session_state.get("rev_precoup_csv", False)),
                    "persist_coupang_snapshot": bool(st.session_state.get("rev_coup_snap", True)),
                    "max_rows": 2000,
                },
                "skip_coupang": bool(st.session_state.get("rev_skip_coup", False)),
            }

            log_buf: list[str] = []

            def _collect_log(msg: str) -> None:
                log_buf.append(msg)

            def _worker() -> Dict[str, Any]:
                return tool.run_recommended_keyword_engine(
                    seed_in,
                    top_output=int(top_out),
                    persist_db=True,
                    progress=_collect_log,
                    ui_settings=ui_settings,
                )

            with st.spinner("추천 엔진 실행 중… (워커 스레드)"):
                try:
                    with ThreadPoolExecutor(max_workers=1) as ex:
                        result = ex.submit(_worker).result(timeout=7200)
                except Exception as e:
                    st.error(f"{type(e).__name__}: {e}")
                    result = None

            if log_buf:
                st.code("\n".join(log_buf[-200:]))

            if isinstance(result, dict) and result.get("ok"):
                meta = result.get("meta") or {}
                p_saved = int(meta.get("precoup_candidates_saved") or 0)
                scored_n = int(meta.get("scored") or 0)
                # 구버전 엔진 meta에 precoup_candidate_count 없을 때 scored로 표시 보정
                p_cnt = int(meta.get("precoup_candidate_count") or scored_n or 0)
                st.success(
                    f"완료 · batch `{result.get('batch_token', '')}` · "
                    f"precoup DB={p_saved}행 (스코어 후보 약 {p_cnt}개) · "
                    f"CSV={meta.get('precoup_csv_path') or '—'}"
                )
                if meta.get("precoup_candidate_count") is None and scored_n > 0:
                    st.info(
                        "진단 필드(`precoup_candidate_count` 등)가 meta에 없습니다. "
                        "**Streamlit/배포 프로세스를 재시작**해 최신 `recommended_keyword_engine.py`가 로드됐는지 확인하세요. "
                        "(그 전까지는 위 후보 개수가 `scored` 기준 추정치입니다.)"
                    )
                if p_saved <= 0 and p_cnt > 0:
                    if meta.get("precoup_skip_reason") or meta.get("precoup_error"):
                        st.warning(
                            "쿠팡 전 후보가 DB에 안 들어갔습니다. "
                            f"원인: `{meta.get('precoup_skip_reason') or 'unknown'}` · "
                            f"{meta.get('precoup_error') or ''} "
                            "`sql/005_recommended_keyword_candidates_mariadb.sql` 적용·앱 재시작, "
                            "또는 **CSV 저장** 후 재실행을 시도하세요."
                        )
                    else:
                        st.warning(
                            "스코어 후보는 있는데 precoup DB가 0행입니다. "
                            "① 옵션에서 **쿠팡 전 스코어 후보 → MariaDB** 체크 ② `sql/005` 적용 후 **앱 재시작** "
                            "③ 로그에 `[Recommend] precoup built=` 줄이 있는지 확인하세요."
                        )
                st.caption(f"meta={meta}")
                top = result.get("top") or []
                if top:
                    st.dataframe(pd.DataFrame(top), width="stretch", hide_index=True)
            elif isinstance(result, dict):
                st.warning(str(result.get("error") or result))

    st.markdown("---")
    with st.expander("추천 키워드 자동 쿠팡 수집 (3번 탭 스모크 방식)", expanded=False):
        st.caption(
            "**1번**: 키워드마다 창을 새로 열어 구글→쿠팡→검색→probe(기존과 동일). "
            "**2번**: 창 1회만 열고 동일 부트스트랩 후, 검색창만 바꿔 키워드 간 **7~10초 랜덤 대기**로 연속 수집(실험). "
            "2번으로 처리된 키워드는 DB에 기록되어 **동일 배치에서 기본 제외**됩니다."
        )
        try:
            from db import (
                is_dsn_configured,
                query_latest_coupang_run_meta,
                query_mode2_autocollect_used_keywords,
                query_recommended_candidate_batches,
                query_recommended_candidates_by_batch,
            )

            if not is_dsn_configured():
                st.warning("DB 설정이 없어 자동 수집 컨트롤러를 실행할 수 없습니다.")
            else:
                batches = query_recommended_candidate_batches(limit=30)
                if not batches:
                    st.info("추천 후보 배치가 없습니다. 먼저 추천 엔진을 실행하세요.")
                else:
                    bopts = [b["batch_token"] for b in batches if b.get("batch_token")]
                    blabel = {
                        b["batch_token"]: f"{b['batch_token']} · rows={b['row_count']} · created={b['created_at']}"
                        for b in batches
                        if b.get("batch_token")
                    }
                    batch_token = st.selectbox(
                        "자동 수집 대상 배치",
                        options=bopts,
                        index=0,
                        key="rev_auto_batch_token",
                        format_func=lambda x: blabel.get(x, x),
                    )
                    all_rows = query_recommended_candidates_by_batch(batch_token, limit=800)
                    l1_values = sorted({str(r.get("category_l1") or "").strip() for r in all_rows if str(r.get("category_l1") or "").strip()})
                    cat_filter = st.selectbox(
                        "카테고리(L1) 필터",
                        options=["(전체)"] + l1_values,
                        index=0,
                        key="rev_auto_cat_l1",
                    )
                    selected_rows = all_rows
                    if cat_filter != "(전체)":
                        selected_rows = [r for r in all_rows if str(r.get("category_l1") or "").strip() == cat_filter]

                    crawl_mode = st.radio(
                        "수집 모드",
                        ("1번 · 키워드당 창 1회 (안정)", "2번 · 단일 창 연속 (실험)"),
                        index=0,
                        horizontal=True,
                        key="rev_auto_crawl_mode",
                    )
                    use_mode2 = crawl_mode.startswith("2번")
                    exclude_m2 = False
                    if use_mode2:
                        exclude_m2 = st.checkbox(
                            "이 배치에서 2번으로 이미 처리한 키워드 제외 (권장)",
                            value=True,
                            key="rev_auto_exclude_mode2_used",
                        )
                    used_mode2: set[str] = set()
                    if use_mode2 and exclude_m2:
                        try:
                            used_mode2 = set(query_mode2_autocollect_used_keywords(batch_token))
                        except Exception:
                            used_mode2 = set()
                    eligible_rows = (
                        [r for r in selected_rows if str(r.get("keyword_text") or "").strip() not in used_mode2]
                        if use_mode2
                        else list(selected_rows)
                    )
                    if use_mode2 and used_mode2:
                        st.caption(
                            f"2번 기준 제외 키워드 **{len([r for r in selected_rows if str(r.get('keyword_text') or '').strip() in used_mode2])}**개 "
                            f"(테이블 `coupang_autocollect_mode2_usage`)"
                        )

                    n_elig = max(1, len(eligible_rows) or 1)
                    kmax = st.slider(
                        "실행 키워드 개수",
                        min_value=1,
                        max_value=max(1, min(50, n_elig)),
                        value=min(5, n_elig),
                        key="rev_auto_limit",
                    )
                    retry_blocked = st.checkbox("BLOCKED 시 3분 후 1회 재시도", value=True, key="rev_auto_retry_blocked")
                    run_auto = st.button("자동 수집 실행", type="primary", key="rev_auto_run_btn")

                    st.caption(
                        f"실행 대기 후보 **{len(eligible_rows)}**개 · 카테고리 필터 후 **{len(selected_rows)}**개 · 배치 전체 **{len(all_rows)}**개"
                    )
                    preview = eligible_rows[: min(20, len(eligible_rows))]
                    if preview:
                        st.dataframe(
                            pd.DataFrame(
                                [
                                    {
                                        "rank": r.get("rank_position"),
                                        "keyword": r.get("keyword_text"),
                                        "category_l1": r.get("category_l1"),
                                        "category_path": r.get("category_path"),
                                        "keyword_score": r.get("keyword_score"),
                                    }
                                    for r in preview
                                ]
                            ),
                            width="stretch",
                            hide_index=True,
                        )

                    if run_auto:
                        target_rows = eligible_rows[: int(kmax)]
                        if not target_rows:
                            st.warning("실행할 키워드가 없습니다. (2번 제외로 모두 걸러졌을 수 있습니다)")
                        else:
                            cc = tool.coupang_crawler
                            result_rows: List[Dict[str, Any]] = []
                            with st.spinner("자동 수집 실행 중..."):
                                if use_mode2:
                                    from coupang_mode2_session import run_mode2_sequential_blocking

                                    m2_log: list[str] = []

                                    def _m2_log(msg: str) -> None:
                                        m2_log.append(msg)

                                    def _mode2_worker() -> list[dict[str, Any]]:
                                        kws = [str(r.get("keyword_text") or "").strip() for r in target_rows]
                                        return run_mode2_sequential_blocking(
                                            cc,
                                            kws,
                                            batch_token=str(batch_token),
                                            google_query="쿠팡",
                                            retry_blocked=bool(retry_blocked),
                                            log=_m2_log,
                                        )

                                    try:
                                        with ThreadPoolExecutor(max_workers=1) as ex:
                                            result_rows = ex.submit(_mode2_worker).result(timeout=7200)
                                    except Exception as e:
                                        st.error(f"{type(e).__name__}: {e}")
                                        result_rows = []
                                    if m2_log:
                                        st.code("\n".join(m2_log[-120:]), language="text")
                                    for r in result_rows:
                                        if isinstance(r, dict) and r.get("keyword"):
                                            r.setdefault(
                                                "items",
                                                int(r.get("item_count_db") or r.get("items_saved") or 0),
                                            )
                                else:
                                    for idx, row in enumerate(target_rows, start=1):
                                        kw = str(row.get("keyword_text") or "").strip()
                                        if not kw:
                                            continue
                                        os.environ["COUPANG_SMOKE_COUPANG_QUERY"] = kw
                                        ok_start = cc.smoke_open_playwright_chromium_window(
                                            url="https://www.google.com/",
                                            wait_seconds=5.0,
                                        )
                                        ok_ready, st0 = cc.poll_smoke_startup_outcome(timeout_seconds=12.0)
                                        time.sleep(10)
                                        meta = query_latest_coupang_run_meta(kw)
                                        reason = ""
                                        last_err = cc.get_last_error() or {}
                                        if isinstance(last_err, dict):
                                            reason = str(last_err.get("code") or "")
                                        retried = False
                                        if retry_blocked and ("BLOCKED" in reason.upper()):
                                            time.sleep(180)
                                            retried = True
                                            ok_start2 = cc.smoke_open_playwright_chromium_window(
                                                url="https://www.google.com/",
                                                wait_seconds=5.0,
                                            )
                                            ok_ready2, st1 = cc.poll_smoke_startup_outcome(timeout_seconds=12.0)
                                            _ = (ok_start2, ok_ready2, st1)
                                            time.sleep(10)
                                            meta = query_latest_coupang_run_meta(kw)
                                            last_err = cc.get_last_error() or {}
                                            reason = str(
                                                (last_err.get("code") if isinstance(last_err, dict) else "")
                                                or reason
                                            )
                                        try:
                                            cc.stop_smoke_playwright_chromium_window()
                                        except Exception:
                                            pass
                                        result_rows.append(
                                            {
                                                "idx": idx,
                                                "keyword": kw,
                                                "start_ok": bool(ok_start),
                                                "ready_ok": bool(ok_ready),
                                                "phase": (st0 or {}).get("phase") if isinstance(st0, dict) else "",
                                                "reason_code": reason,
                                                "retried_once": retried,
                                                "run_id": (meta or {}).get("run_id") if isinstance(meta, dict) else None,
                                                "items": int((meta or {}).get("item_count") or 0)
                                                if isinstance(meta, dict)
                                                else 0,
                                            }
                                        )
                            st.success("자동 수집 실행 완료")
                            if result_rows:
                                st.dataframe(pd.DataFrame(result_rows), width="stretch", hide_index=True)
                            ok_cnt = sum(
                                1
                                for r in result_rows
                                if int(r.get("items") or r.get("item_count_db") or 0) > 0
                            )
                            st.caption(f"저장 성공(아이템 1개 이상) {ok_cnt}/{max(1, len(result_rows))}")
        except Exception as e:
            st.warning(f"자동 수집 컨트롤러 로드 실패: {e}")

    with st.expander("가이드 YAML (참고)", expanded=False):
        guide_path_str = st.text_input(
            "샘플 YAML 경로",
            value=str(_DEFAULT_GUIDE_PATH),
            key="revenue_guide_path",
        )
        path = Path(guide_path_str.strip()).expanduser()
        raw_text = ""
        err: Optional[str] = None
        try:
            raw_text = path.read_text(encoding="utf-8")
        except Exception as e:
            err = str(e)
        if err:
            st.warning(err)
        st.code(raw_text or "(비어 있음)", language="yaml")
        st.caption("운영 설정 파일: `config/revenue_keyword_guide.yaml` (별도 편집)")


main_page()

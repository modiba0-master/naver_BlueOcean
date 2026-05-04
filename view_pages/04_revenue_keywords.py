"""4. 매출 키워드 추천 — 설정 연동 + 워커 스레드에서 엔진 실행 (asyncio.run 충돌 방지)."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional

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

"""공통 사이드바: 실행 설정·벤치마크·분석 실행 트리거."""
import os
from datetime import date, timedelta

import streamlit as st

from category_benchmark import run_category_benchmark
from shopping_insight_benchmark import run_shopping_insight_benchmark

from web_common import detect_naver_categories, get_tool, should_skip_coupang_top10_caption


def render_sidebar() -> bool:
    """사이드바를 그립니다. 반환값: 이번 실행에서 `분석 실행`이 눌렸는지."""
    run_clicked = False
    with st.sidebar:
        st.subheader("실행 설정")
        if st.button("로그아웃", width="stretch"):
            st.session_state["is_admin_authed"] = False
            if "login_form_admin_id" in st.session_state:
                del st.session_state["login_form_admin_id"]
            if "login_form_admin_pw" in st.session_state:
                del st.session_state["login_form_admin_pw"]
            st.rerun()
        seeds_text = st.text_input("주제어(쉼표로 구분)", key="seed_input")
        detect_clicked = st.button("주제어 기반 카테고리 찾기", width="stretch")
        if detect_clicked:
            first_seed = str(seeds_text).split(",")[0].strip() if seeds_text else ""
            client_id = str(os.getenv("NAVER_CLIENT_ID", "")).strip() or str(
                get_tool().config.get("naver_open_api", {}).get("client_id", "")
            ).strip()
            client_secret = str(os.getenv("NAVER_CLIENT_SECRET", "")).strip() or str(
                get_tool().config.get("naver_open_api", {}).get("client_secret", "")
            ).strip()
            detected, msg = detect_naver_categories(first_seed, client_id, client_secret)
            st.session_state["detected_categories"] = detected
            st.session_state["detected_category_msg"] = msg

        if st.session_state.get("detected_category_msg"):
            st.caption(st.session_state["detected_category_msg"])
        if st.session_state.get("detected_categories"):
            st.markdown("**네이버 쇼핑 노출 카테고리(추정)**")
            for row in st.session_state["detected_categories"]:
                st.write(f"- {row}")

        default_end = date.today()
        default_start = default_end - timedelta(days=60)
        start_date = st.date_input("분석 시작일", value=default_start)
        end_date = st.date_input("분석 종료일", value=default_end)

        benchmark_clicked = st.button("카테고리 벤치마크 (주제어 vs 연관 상위 20)", width="stretch")
        if benchmark_clicked:
            first_seed = str(seeds_text).split(",")[0].strip() if seeds_text else ""
            if not first_seed:
                st.warning("주제어를 입력한 뒤 다시 눌러주세요.")
            else:
                _tool_bm = get_tool()
                with st.spinner("쇼핑 카테고리·키워드도구·상품수 조회 중…"):
                    bm_df, bm_note = run_category_benchmark(_tool_bm, first_seed, top_n=20)
                st.session_state["category_benchmark_df"] = bm_df
                st.session_state["category_benchmark_note"] = bm_note or ""

        insight_persist_db = st.checkbox(
            "인사이트 결과를 MariaDB에 저장",
            value=True,
            key="insight_persist_db",
            help="주제어·카테고리·데이터랩 순위 키워드별 모바일 검색·클릭·CTR·상품수·시장접목점수를 insight_discovery_* 테이블에 적재합니다.",
        )
        insight_clicked = st.button("데이터랩 인사이트 Top20 (분야 인기검색어 + 지표)", width="stretch")
        if insight_clicked:
            first_seed = str(seeds_text).split(",")[0].strip() if seeds_text else ""
            if not first_seed:
                st.warning("주제어를 입력한 뒤 다시 눌러주세요.")
            elif start_date > end_date:
                st.warning("분석 기간(시작일·종료일)을 올바르게 설정해주세요.")
            else:
                _tool_ins = get_tool()
                with st.spinner("데이터랩 인사이트 순위·키워드도구·상품수 조회 중…"):
                    ins_df, ins_note = run_shopping_insight_benchmark(
                        _tool_ins,
                        first_seed,
                        start_date=start_date.strftime("%Y-%m-%d"),
                        end_date=end_date.strftime("%Y-%m-%d"),
                        top_n=20,
                        persist_to_db=bool(insight_persist_db),
                    )
                st.session_state["insight_benchmark_df"] = ins_df
                st.session_state["insight_benchmark_note"] = ins_note or ""

        mode_label = st.selectbox("분석 모드", ["빠른 모드", "정밀 모드"], index=0)
        if should_skip_coupang_top10_caption():
            st.caption(
                "실행로그 분석에서 쿠팡 Top10 수집은 생략됩니다. 쿠팡 지표는 「3. 쿠팡 상품 키워드 분석」에서 확인하세요."
            )
        run_clicked = st.button("분석 실행", type="primary", width="stretch")

        st.divider()
        st.markdown("**환경 확인**")
        _db_url = (
            (os.getenv("MYSQL_URL") or "").strip()
            or (os.getenv("MYSQL_PUBLIC_URL") or "").strip()
            or (os.getenv("MARIADB_PUBLIC_URL") or "").strip()
            or (os.getenv("MARIADB_URL") or "").strip()
            or (os.getenv("DATABASE_URL") or "").strip()
            or (os.getenv("DATABASE_PUBLIC_URL") or "").strip()
        )
        has_db_url = bool(_db_url)
        st.write(f"- MariaDB 접속 URL 환경변수: {'예' if has_db_url else '아니오'}")
        if not has_db_url:
            st.caption(
                "로컬에서는 Railway TCP 프록시 주소가 필요합니다. 예: MARIADB_PUBLIC_URL, MYSQL_PUBLIC_URL 또는 MYSQL_URL "
                "(railway.internal 호스트는 PC에서 해석되지 않습니다.)"
            )

        # 분석 모드는 세션에 저장해 상위에서 분석 실행 시 사용
        st.session_state["_sidebar_mode_value"] = "fast" if mode_label == "빠른 모드" else "precise"
        st.session_state["_sidebar_start_date"] = start_date
        st.session_state["_sidebar_end_date"] = end_date

    return bool(run_clicked)

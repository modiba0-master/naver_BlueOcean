"""Modiba BlueOcean Streamlit 엔트리 — 사이드바 공통 + `st.navigation` 멀티페이지."""
import os
from datetime import date, timedelta
from typing import List

import pandas as pd
import streamlit as st

from blue_ocean_tool import apply_admin_env_from_config, apply_database_env_from_config

from web_common import get_tool, inject_dashboard_style
from web_sidebar import render_sidebar

_ROOT = os.path.dirname(os.path.abspath(__file__))


def _view_page(filename: str) -> str:
    return os.path.join(_ROOT, "view_pages", filename)


def _init_session_state() -> None:
    if "seed_input" not in st.session_state:
        st.session_state["seed_input"] = ""
    if "detected_categories" not in st.session_state:
        st.session_state["detected_categories"] = []
    if "detected_category_msg" not in st.session_state:
        st.session_state["detected_category_msg"] = ""
    if "last_run_logs" not in st.session_state:
        st.session_state["last_run_logs"] = []
    if "last_run_summary" not in st.session_state:
        st.session_state["last_run_summary"] = ""
    if "last_run_report_df" not in st.session_state:
        st.session_state["last_run_report_df"] = pd.DataFrame()
    if "category_benchmark_df" not in st.session_state:
        st.session_state["category_benchmark_df"] = pd.DataFrame()
    if "category_benchmark_note" not in st.session_state:
        st.session_state["category_benchmark_note"] = ""
    if "insight_benchmark_df" not in st.session_state:
        st.session_state["insight_benchmark_df"] = pd.DataFrame()
    if "insight_benchmark_note" not in st.session_state:
        st.session_state["insight_benchmark_note"] = ""


def run() -> None:
    st.set_page_config(page_title="Modiba BlueOcean", layout="wide")
    apply_database_env_from_config("config.json")
    apply_admin_env_from_config("config.json")
    inject_dashboard_style()
    st.title("Modiba BlueOcean Web")
    st.caption("Railway 웹서비스용 - 관리자 인증 후 대시보드 접근")

    if "is_admin_authed" not in st.session_state:
        st.session_state["is_admin_authed"] = False

    admin_id = str(os.getenv("MODIBA_ADMIN_ID", "")).strip()
    admin_pw = str(os.getenv("MODIBA_ADMIN_PASSWORD", "")).strip()

    if not admin_id or not admin_pw:
        st.error(
            "관리자 계정이 설정되지 않았습니다. "
            "환경변수 `MODIBA_ADMIN_ID`, `MODIBA_ADMIN_PASSWORD`를 설정하거나, "
            "`config.local.json`의 `admin.modiba_admin_id` / `admin.modiba_admin_password`를 설정한 뒤 "
            "앱을 다시 시작해주세요."
        )
        return

    if not st.session_state["is_admin_authed"]:
        if "login_form_admin_id" not in st.session_state:
            st.session_state["login_form_admin_id"] = admin_id
        _prefill_pw = str(os.getenv("MODIBA_ADMIN_LOGIN_PREFILL_PW", "1")).strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        if "login_form_admin_pw" not in st.session_state:
            st.session_state["login_form_admin_pw"] = admin_pw if _prefill_pw else ""

        with st.form("admin_login_form", clear_on_submit=False):
            st.text_input("관리자 아이디", key="login_form_admin_id")
            st.text_input("관리자 비밀번호", key="login_form_admin_pw", type="password")
            submitted = st.form_submit_button("관리자 로그인", type="primary")
            if submitted:
                tid = str(st.session_state.get("login_form_admin_id", "")).strip()
                tpw = str(st.session_state.get("login_form_admin_pw", "")).strip()
                if tid == admin_id and tpw == admin_pw:
                    st.session_state["is_admin_authed"] = True
                    st.success("인증 성공. 대시보드로 이동합니다.")
                    st.rerun()
                else:
                    st.error("아이디 또는 비밀번호가 올바르지 않습니다.")
        st.info(
            "관리자 계정으로 로그인해야 대시보드 접근이 가능합니다. "
            "아이디·비밀번호는 `config.local.json`의 `admin` 또는 환경변수에서 불러오며, "
            "앱 재시작 후에도 동일 설정이 적용됩니다. 비밀번호 자동 채움을 끄려면 "
            "`MODIBA_ADMIN_LOGIN_PREFILL_PW=0` 을 설정하세요."
        )
        return

    _init_session_state()

    pages = [
        st.Page(_view_page("01_exec_log.py"), title="1. 실행로그", icon="📋"),
        st.Page(_view_page("02_market_score.py"), title="2. 시장성 점수조회", icon="📈"),
        st.Page(_view_page("03_coupang.py"), title="3. 쿠팡 상품 키워드 분석", icon="🛒"),
        st.Page(_view_page("04_revenue_keywords.py"), title="4. 매출 키워드 추천", icon="💰"),
    ]
    # 사이드바에서 페이지 메뉴가 실행 설정보다 위에 오도록 네비게이션을 먼저 등록
    nav = st.navigation(pages)

    run_clicked = render_sidebar()
    tool = get_tool()

    if run_clicked:
        sd = st.session_state.get("_sidebar_start_date")
        ed = st.session_state.get("_sidebar_end_date")
        if sd is None:
            sd = date.today() - timedelta(days=60)
        if ed is None:
            ed = date.today()
        if sd > ed:
            st.error("시작일이 종료일보다 클 수 없습니다.")
        else:
            logs: List[str] = []

            def _log(msg: str) -> None:
                logs.append(msg)

            seeds_text = str(st.session_state.get("seed_input", "") or "")
            mode_value = str(st.session_state.get("_sidebar_mode_value", "fast") or "fast")
            with st.spinner("분석 중입니다. API 호출량에 따라 시간이 걸릴 수 있습니다."):
                summary, report_df = tool.start_analysis(
                    seeds=seeds_text,
                    start_date=sd.strftime("%Y-%m-%d"),
                    end_date=ed.strftime("%Y-%m-%d"),
                    analysis_mode=mode_value,
                    log_callback=_log,
                )
            st.session_state["last_run_logs"] = logs
            st.session_state["last_run_summary"] = summary or ""
            st.session_state["last_run_report_df"] = report_df if report_df is not None else pd.DataFrame()

    nav.run()


if __name__ == "__main__":
    run()

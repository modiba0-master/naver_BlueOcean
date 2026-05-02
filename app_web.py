import asyncio
import os
import subprocess
import sys
import webbrowser
from collections import Counter
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st


if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

from blue_ocean_tool import (
    BlueOceanTool,
    apply_admin_env_from_config,
    apply_database_env_from_config,
)
from coupang_tab import render_coupang_keyword_analysis_tab
from db import (
    get_connection,
    is_dsn_configured,
    query_market_score_rows,
)
from report_format import report_to_excel_bytes

_GOOGLE_HOME_URL = "https://www.google.com/"


def _chrome_exe_candidates_windows() -> List[str]:
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pfx86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    return [
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pfx86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
    ]


def open_google_home_in_desktop_browser() -> bool:
    """로컬에서 Chrome(있으면) 또는 기본 브라우저로 구글 홈만 연다. 서버 헤드리스에서는 실패할 수 있다."""
    url = _GOOGLE_HOME_URL
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        for exe in _chrome_exe_candidates_windows():
            if exe and os.path.isfile(exe):
                try:
                    subprocess.Popen(
                        [exe, url],
                        close_fds=True,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=flags,
                    )
                    return True
                except OSError:
                    continue
        try:
            subprocess.Popen(
                ["chrome", url],
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            return True
        except OSError:
            pass
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _inject_tab_style() -> None:
    st.markdown(
        """
        <style>
        div[data-baseweb="tab-list"] {
            gap: 0.5rem;
            background: #f7fafc;
            padding: 0.35rem;
            border-radius: 0.8rem;
            border: 1px solid #e2e8f0;
        }
        button[data-baseweb="tab"] {
            border-radius: 0.65rem;
            border: 1px solid #e2e8f0;
            background: #ffffff;
            padding: 0.5rem 0.8rem;
            font-weight: 600;
        }
        button[data-baseweb="tab"][aria-selected="true"] {
            background: #e6f4ff;
            border-color: #8ec5ff;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _band_style(value: object) -> str:
    band = str(value or "").upper().strip()
    if band == "GO":
        return "background-color: #e8f7ee; color: #166534; font-weight: 700;"
    if band == "WATCH":
        return "background-color: #fff7e6; color: #92400e; font-weight: 700;"
    if band == "DROP":
        return "background-color: #fdecec; color: #991b1b; font-weight: 700;"
    return ""


def _apply_band_filter(df: pd.DataFrame, selected_bands: List[str], go_only: bool) -> pd.DataFrame:
    if "decision_band" not in df.columns:
        return df
    result = df.copy()
    if go_only:
        result = result[result["decision_band"].astype(str).str.upper() == "GO"]
    elif selected_bands:
        bands_upper = {b.upper() for b in selected_bands}
        result = result[result["decision_band"].astype(str).str.upper().isin(bands_upper)]
    return result


def _period_to_range(label: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    now = datetime.now()
    if label == "오늘":
        start = datetime.combine(date.today(), datetime.min.time())
        end = datetime.combine(date.today(), datetime.max.time())
        return start, end
    if label == "최근 7일":
        return now - timedelta(days=7), now
    if label == "최근 30일":
        return now - timedelta(days=30), now
    if label == "최근 60일":
        return now - timedelta(days=60), now
    if label == "최근 120일":
        return now - timedelta(days=120), now
    return None, None


@st.cache_resource
def get_tool() -> BlueOceanTool:
    # config.json and env are loaded inside the tool.
    return BlueOceanTool(config_path="config.json")


@st.cache_data
def detect_naver_categories(seed_keyword: str, client_id: str, client_secret: str) -> Tuple[List[str], str]:
    seed = str(seed_keyword).strip()
    if not seed:
        return [], "주제어를 먼저 입력해주세요."

    if not client_id or not client_secret:
        return [], "네이버 Open API 인증 정보가 없습니다."

    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    url = "https://openapi.naver.com/v1/search/shop.json"
    params = {"query": seed, "display": 50, "sort": "sim"}

    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        if res.status_code != 200:
            return [], f"카테고리 탐색 실패: HTTP {res.status_code}"
        items = res.json().get("items", [])
    except Exception as e:
        return [], f"카테고리 탐색 오류: {e}"

    if not items:
        return [], "검색 결과가 없어 카테고리를 찾지 못했습니다."

    category_counter: Counter[str] = Counter()
    for it in items:
        c1 = str(it.get("category1", "")).strip()
        c2 = str(it.get("category2", "")).strip()
        c3 = str(it.get("category3", "")).strip()
        c4 = str(it.get("category4", "")).strip()
        cats = [c for c in [c1, c2, c3, c4] if c]
        if cats:
            category_counter[" > ".join(cats)] += 1

    if not category_counter:
        return [], "카테고리 정보가 포함된 상품이 없어 표시할 수 없습니다."

    top_categories = [f"{cat} ({cnt}건)" for cat, cnt in category_counter.most_common(8)]
    return top_categories, "주제어 기반 카테고리 탐색 완료"


def run() -> None:
    st.set_page_config(page_title="Modiba BlueOcean", layout="wide")
    apply_database_env_from_config("config.json")
    apply_admin_env_from_config("config.json")
    _inject_tab_style()
    st.title("Modiba BlueOcean Web")
    st.caption("Railway 웹서비스용 - 관리자 인증 후 대시보드 접근")

    # 관리자 인증 게이트
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
        # 재시작 후에도 config.local.json → env 로 올라온 계정으로 폼 초기값 유지
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

    with st.sidebar:
        st.subheader("실행 설정")
        if st.button("로그아웃", width='stretch'):
            st.session_state["is_admin_authed"] = False
            if "login_form_admin_id" in st.session_state:
                del st.session_state["login_form_admin_id"]
            if "login_form_admin_pw" in st.session_state:
                del st.session_state["login_form_admin_pw"]
            st.rerun()
        seeds_text = st.text_input("주제어(쉼표로 구분)", key="seed_input")
        detect_clicked = st.button("주제어 기반 카테고리 찾기", width='stretch')
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
        mode_label = st.selectbox("분석 모드", ["빠른 모드", "정밀 모드"], index=0)
        run_clicked = st.button("분석 실행", type="primary", width='stretch')

        st.divider()
        st.markdown("**환경 확인**")
        # db._dsn_from_env 우선순위와 동일하게 URL 존재 여부만 표시 (값은 노출하지 않음)
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

    tool = get_tool()

    if run_clicked:
        if start_date > end_date:
            st.error("시작일이 종료일보다 클 수 없습니다.")
        else:
            logs: List[str] = []

            def _log(msg: str) -> None:
                logs.append(msg)

            with st.spinner("분석 중입니다. API 호출량에 따라 시간이 걸릴 수 있습니다."):
                mode_value = "fast" if mode_label == "빠른 모드" else "precise"
                summary, report_df = tool.start_analysis(
                    seeds=seeds_text,
                    start_date=start_date.strftime("%Y-%m-%d"),
                    end_date=end_date.strftime("%Y-%m-%d"),
                    analysis_mode=mode_value,
                    log_callback=_log,
                )
            st.session_state["last_run_logs"] = logs
            st.session_state["last_run_summary"] = summary or ""
            st.session_state["last_run_report_df"] = report_df if report_df is not None else pd.DataFrame()

    tab_log, tab_market, tab_coupang = st.tabs(
        ["📋 1. 실행로그", "📈 2. 시장성 점수조회", "🛒 3. 쿠팡 상품 키워드 분석"]
    )

    with tab_log:
        st.subheader("실행 로그")
        last_logs = st.session_state.get("last_run_logs", [])
        last_summary = str(st.session_state.get("last_run_summary", "")).strip()
        last_report_df = st.session_state.get("last_run_report_df", pd.DataFrame())

        if last_logs or last_summary:
            st.code("\n".join(last_logs) if last_logs else "로그 없음")
            if last_summary:
                st.success(f"분석 완료: {last_summary}")
            else:
                st.warning("결과가 없어 저장되지 않았습니다.")
            if isinstance(last_report_df, pd.DataFrame) and not last_report_df.empty:
                st.subheader("리포트 미리보기 (템플릿 8열)")
                st.dataframe(last_report_df, width='stretch', hide_index=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M")
                xlsx = report_to_excel_bytes(last_report_df)
                st.download_button(
                    label="엑셀 다운로드 (모디바 카테고리별 TOP10 형식)",
                    data=xlsx,
                    file_name=f"모디바_카테고리별_TOP10_추천분석서_{ts}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    width='stretch',
                    key="dl_analysis_report",
                )
        else:
            st.info("왼쪽 사이드바에서 조건을 설정하고 `분석 실행`을 눌러주세요.")

    with tab_market:
        st.subheader("시장성 점수 조회 (DB)")
        st.caption("최종 점수 = 수요 점수 × 트렌드 점수 × 전환 점수 ÷ 경쟁 점수")

        if not is_dsn_configured():
            st.warning(
                "MariaDB 접속 정보가 환경에 없습니다. PowerShell 예: "
                "`$env:MARIADB_PUBLIC_URL='mariadb://...'` 후 앱 재실행, 또는 "
                "`railway run streamlit run app_web.py`, 또는 `config.json`에 "
                "`database.mariadb_public_url` 을 추가하세요."
            )
        else:
            with st.expander("MariaDB 연결 확인", expanded=False):
                if st.button("연결 테스트 (SELECT 1)", key="db_ping_market_tab"):
                    try:
                        with get_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute("SELECT 1")
                                cur.fetchone()
                                cur.execute("SELECT DATABASE(), VERSION()")
                                row = cur.fetchone()
                        dbname = row[0] if row else ""
                        ver = (row[1] or "")[:100] if row else ""
                        st.success(f"연결 성공 — 현재 DB: `{dbname}`")
                        if ver:
                            st.caption(ver)
                    except Exception as e:
                        st.error(f"{type(e).__name__}: {e}")

        filter_col1, filter_col2, filter_col3, filter_col4 = st.columns([2, 1, 1, 1])
        with filter_col1:
            keyword_like = st.text_input("키워드 필터", value="")
        with filter_col2:
            period = st.selectbox("기간", ["오늘", "최근 7일", "최근 30일", "최근 60일", "최근 120일", "전체"], index=1)
        with filter_col3:
            limit = st.selectbox("건수", [20, 50, 100, 200], index=1)
        with filter_col4:
            sort_by = st.selectbox("정렬", ["2차 최종 점수", "최종 점수", "월검색량(수요)", "분석시각"], index=0)

        band_col1, band_col2 = st.columns([3, 1])
        with band_col1:
            selected_bands = st.multiselect("판단 밴드", ["GO", "WATCH", "DROP"], default=["GO", "WATCH", "DROP"])
        with band_col2:
            go_only = st.checkbox("GO만 보기", value=False)

        started_from, started_to = _period_to_range(period)

        score_rows: List[dict] = []
        if is_dsn_configured():
            try:
                score_rows = query_market_score_rows(
                    limit=int(limit),
                    keyword_like=(keyword_like or "").strip() or None,
                    started_from=started_from,
                    started_to=started_to,
                )
            except Exception as e:
                st.error(f"DB 조회 실패: {e}")
                score_rows = []

        if score_rows:
            score_df = pd.DataFrame(score_rows)
            score_df = _apply_band_filter(score_df, selected_bands, go_only)
            sort_map = {
                "2차 최종 점수": ("final_score", False),
                "최종 점수": ("market_score", False),
                "월검색량(수요)": ("monthly_search_volume_est", False),
                "분석시각": ("started_at", False),
            }
            sort_col, ascending = sort_map.get(sort_by, ("final_score", False))
            if sort_col in score_df.columns:
                score_df = score_df.sort_values(by=sort_col, ascending=ascending, na_position="last")

            show_cols = [
                "started_at",
                "seed_keyword",
                "keyword_text",
                "monthly_search_volume_est",
                "product_count",
                "demand_score",
                "trend_score",
                "conversion_score",
                "competition_score",
                "market_score",
                "opportunity_score",
                "commercial_score",
                "final_score",
                "decision_band",
                "top10_avg_reviews",
                "top10_avg_price",
            ]
            show_cols = [c for c in show_cols if c in score_df.columns]
            view_df = score_df[show_cols].rename(
                columns={
                    "started_at": "분석시각",
                    "seed_keyword": "주제어",
                    "keyword_text": "키워드",
                    "monthly_search_volume_est": "월검색량(수요)",
                    "product_count": "상품수(경쟁)",
                    "demand_score": "수요 점수",
                    "trend_score": "트렌드 점수",
                    "conversion_score": "전환 점수",
                    "competition_score": "경쟁 점수",
                    "market_score": "최종 점수",
                    "opportunity_score": "기회 점수",
                    "commercial_score": "판매가치 점수",
                    "final_score": "2차 최종 점수",
                    "decision_band": "판단 밴드",
                    "top10_avg_reviews": "쿠팡 Top10 평균리뷰수",
                    "top10_avg_price": "쿠팡 Top10 평균가격",
                }
            )
            st.caption("밴드 색상: GO(초록) · WATCH(주황) · DROP(빨강)")
            if "판단 밴드" in view_df.columns:
                st.dataframe(
                    view_df.style.map(_band_style, subset=["판단 밴드"]),
                    width='stretch',
                    hide_index=True,
                )
            else:
                st.dataframe(view_df, width='stretch', hide_index=True)

            insight_cols = [
                "keyword_text",
                "decision_band",
                "ai_summary",
                "ai_action",
                "ai_risk",
                "ai_confidence",
                "ai_model_version",
            ]
            if all(c in score_df.columns for c in insight_cols):
                insight_rows = score_df[insight_cols].fillna("")
                with st.expander("AI 인사이트 (근거 기반 요약)", expanded=False):
                    for _, row in insight_rows.head(10).iterrows():
                        keyword = str(row.get("keyword_text", "")).strip()
                        if not keyword:
                            continue
                        st.markdown(f"**{keyword}** · {row.get('decision_band', '')}")
                        summary = str(row.get("ai_summary", "")).strip()
                        action = str(row.get("ai_action", "")).strip()
                        risk = str(row.get("ai_risk", "")).strip()
                        confidence = row.get("ai_confidence", "")
                        model_version = str(row.get("ai_model_version", "")).strip()
                        if summary:
                            st.write(f"- 요약: {summary}")
                        if action:
                            st.write(f"- 액션: {action}")
                        if risk:
                            st.write(f"- 리스크: {risk}")
                        if confidence != "":
                            st.write(f"- 신뢰도: {confidence}")
                        if model_version:
                            st.caption(f"model: {model_version}")
                        st.divider()

            st.caption(
                "전환 점수는 쿠팡 Top10 평균리뷰수/평균가격이 있으면 해당 값을 우선 사용하고, "
                "수집 실패 시 클릭/CTR 대체값으로 계산됩니다."
            )

            ts = datetime.now().strftime("%Y%m%d_%H%M")
            xlsx_db = report_to_excel_bytes(view_df)
            st.download_button(
                label="엑셀 다운로드 (시장성 점수 조회 결과)",
                data=xlsx_db,
                file_name=f"모디바_시장성점수_조회결과_{ts}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_market_score_report",
                width='stretch',
            )
        else:
            if is_dsn_configured():
                st.info("조회 결과가 없습니다.")

    with tab_coupang:
        render_coupang_keyword_analysis_tab()


if __name__ == "__main__":
    run()

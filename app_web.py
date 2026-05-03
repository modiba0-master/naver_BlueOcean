import asyncio
import json
import os
import subprocess
import sys
import webbrowser
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

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
    query_analysis_runs_history,
    query_insight_discovery_rows,
    query_market_score_rows,
)
from report_format import coerce_next_month_volume_column, report_to_excel_bytes
from category_benchmark import run_category_benchmark
from shopping_insight_benchmark import run_shopping_insight_benchmark

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


def _accounting_float_format_for_column(col_name: str) -> str:
    """천 단위 콤마 + 소수 자릿수 (비율·점수류는 소수 네 자리)."""
    c = str(col_name)
    if any(x in c for x in ("CTR", "비(", "비율", "점수", "접목", "ratio")):
        return "{:,.4f}"
    return "{:,.2f}"


def _accounting_format_styler(df: pd.DataFrame, *, band_column: Optional[str] = None):
    """숫자 열에 천 단위 구분 콤마 적용. 밴드 열은 기존 색 스타일 유지."""
    if df.empty:
        return df
    fmt: dict[str, str] = {}
    for col in df.columns:
        if band_column and col == band_column:
            continue
        ser = df[col]
        if pd.api.types.is_bool_dtype(ser):
            continue
        if pd.api.types.is_datetime64_any_dtype(ser):
            continue
        if not pd.api.types.is_numeric_dtype(ser):
            continue
        if pd.api.types.is_integer_dtype(ser):
            fmt[col] = "{:,.0f}"
        else:
            fmt[col] = _accounting_float_format_for_column(col)
    sty = df.style
    if fmt:
        sty = sty.format(fmt, na_rep="")
    if band_column and band_column in df.columns:
        sty = sty.map(_band_style, subset=[band_column])
    return sty


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


def _format_analysis_run_history_label(row: Dict[str, Any]) -> str:
    """시장성 탭 — analysis_runs 한 줄 라벨 (실행 시각 · 시드 요약 · 행 수 · 상태)."""
    ts = row.get("started_at")
    try:
        if hasattr(ts, "strftime"):
            ts_s = ts.strftime("%Y-%m-%d %H:%M")
        else:
            ts_s = str(ts or "")[:19]
    except Exception:
        ts_s = str(ts or "")
    raw = str(row.get("seed_keywords_raw") or "").strip()
    seeds = [s.strip() for s in raw.split(",") if s.strip()]
    if not seeds:
        seed_s = "(시드 없음)"
    elif len(seeds) <= 2:
        seed_s = ", ".join(seeds)
    else:
        seed_s = f"{seeds[0]} 외 {len(seeds) - 1}건"
    n = int(row.get("metric_rows") or 0)
    st = str(row.get("status") or "")
    if st == "SUCCESS":
        icon, badge = "✅", "완료"
    elif st == "FAILED":
        icon, badge = "❌", "실패"
    elif st == "RUNNING":
        icon, badge = "🔄", "진행중"
    else:
        icon, badge = "⚪", st or "-"
    return f"{icon} {ts_s} · {seed_s} · 연관 {n}행 [{badge}]"


@st.cache_resource
def get_tool() -> BlueOceanTool:
    # config.json and env are loaded inside the tool.
    return BlueOceanTool(config_path="config.json")


def _should_skip_coupang_top10_caption() -> bool:
    """
    실행로그 쪽 쿠팡 생략 안내 표시 여부.
    Streamlit 캐시된 구 Tool 인스턴스에는 필드가 없을 수 있어 메서드 → 속성 → config 순으로 판단한다.
    """
    t = get_tool()
    fn = getattr(t, "should_skip_coupang_top10_in_analysis", None)
    if callable(fn):
        return bool(fn())
    if hasattr(t, "skip_coupang_top10_in_analysis"):
        return bool(getattr(t, "skip_coupang_top10_in_analysis"))
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return bool((cfg.get("settings") or {}).get("skip_coupang_top10_in_analysis", False))
    except Exception:
        return False


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
    if "category_benchmark_df" not in st.session_state:
        st.session_state["category_benchmark_df"] = pd.DataFrame()
    if "category_benchmark_note" not in st.session_state:
        st.session_state["category_benchmark_note"] = ""
    if "insight_benchmark_df" not in st.session_state:
        st.session_state["insight_benchmark_df"] = pd.DataFrame()
    if "insight_benchmark_note" not in st.session_state:
        st.session_state["insight_benchmark_note"] = ""

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

        benchmark_clicked = st.button("카테고리 벤치마크 (주제어 vs 연관 상위 20)", width='stretch')
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
        insight_clicked = st.button("데이터랩 인사이트 Top20 (분야 인기검색어 + 지표)", width='stretch')
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
        if _should_skip_coupang_top10_caption():
            st.caption(
                "실행로그 분석에서 쿠팡 Top10 수집은 생략됩니다. 쿠팡 지표는 「3. 쿠팡 상품 키워드 분석」에서 확인하세요."
            )
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
        _ins_df = st.session_state.get("insight_benchmark_df", pd.DataFrame())
        _ins_note = str(st.session_state.get("insight_benchmark_note", "") or "")
        if isinstance(_ins_df, pd.DataFrame) and not _ins_df.empty:
            st.subheader("데이터랩 쇼핑인사이트 · 분야 인기 검색어 vs 주제어")
            st.markdown(_ins_note)
            st.dataframe(_accounting_format_styler(_ins_df), width='stretch', hide_index=True)
            st.caption(
                "인기 검색어 순위는 datalab.naver.com 이 사용하는 비공개 순위 API 결과입니다. "
                "주제어 지표는 광고 키워드도구·쇼핑 검색으로 보강했습니다."
            )
            st.divider()

        _bm_df = st.session_state.get("category_benchmark_df", pd.DataFrame())
        _bm_note = str(st.session_state.get("category_benchmark_note", "") or "")
        if isinstance(_bm_df, pd.DataFrame) and not _bm_df.empty:
            st.subheader("카테고리 벤치마크 (주제어 대비)")
            st.markdown(_bm_note)
            st.dataframe(_accounting_format_styler(_bm_df), width='stretch', hide_index=True)
            st.caption(
                "모바일 월 검색·클릭·CTR은 네이버 광고 키워드도구, 상품수는 네이버 쇼핑 검색 total 입니다. "
                "‘카테고리 내 1~20위’는 공개 API에 없어 연관 키워드 중 검색량 상위 20으로 근사합니다."
            )
            st.divider()

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
                _preview_df = coerce_next_month_volume_column(last_report_df)
                st.dataframe(_accounting_format_styler(_preview_df), width='stretch', hide_index=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M")
                xlsx = report_to_excel_bytes(_preview_df)
                st.download_button(
                    label="엑셀 다운로드 (모디바 카테고리별 TOP10 형식)",
                    data=xlsx,
                    file_name=f"모디바_카테고리별_TOP10_추천분석서_{ts}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    width='stretch',
                    key="dl_analysis_report",
                )
                _detail_fn = getattr(tool, "get_last_analysis_detail_df", None)
                detail_df = _detail_fn() if callable(_detail_fn) else pd.DataFrame()
                if isinstance(detail_df, pd.DataFrame) and not detail_df.empty:
                    with st.expander("소싱 고도화 진단 (Stage 1~3)", expanded=False):
                        stage_cols = [
                            "주제어",
                            "키워드",
                            "intent",
                            "season_type",
                            "sales_power",
                            "competition_score",
                            "기회 점수",
                            "판매가치 점수",
                            "최종 점수",
                            "판단 밴드",
                        ]
                        visible_cols = [c for c in stage_cols if c in detail_df.columns]
                        stage_header_ko = {
                            "intent": "검색 의도",
                            "season_type": "시즌 유형",
                            "sales_power": "판매력 점수",
                            "competition_score": "경쟁 강도 점수",
                        }
                        if visible_cols:
                            detail_view = detail_df[visible_cols].rename(
                                columns={k: v for k, v in stage_header_ko.items() if k in visible_cols}
                            )
                            st.dataframe(
                                _accounting_format_styler(detail_view, band_column="판단 밴드"),
                                width="stretch",
                                hide_index=True,
                            )
                        else:
                            st.caption("현재 롤아웃 단계에서 추가 진단 컬럼이 아직 생성되지 않았습니다.")
                        if "intent" in detail_df.columns:
                            intent_counts = detail_df["intent"].astype(str).value_counts().to_dict()
                            st.caption(f"검색 의도 분포: {intent_counts}")
                        if "season_type" in detail_df.columns:
                            season_counts = detail_df["season_type"].astype(str).value_counts().to_dict()
                            st.caption(f"시즌 유형 분포: {season_counts}")
        else:
            st.info("왼쪽 사이드바에서 조건을 설정하고 `분석 실행`을 눌러주세요.")

    with tab_market:
        st.subheader("시장성 점수 조회 (DB)")
        st.caption("최종 점수 = 수요 점수 × 트렌드 점수 × 전환 점수 ÷ 경쟁 점수")

        market_query_mode = "기간·키워드 필터 — DB 전체 검색"
        selected_run_id: Optional[int] = None
        skip_market_query = False
        picked_metric_rows: Optional[int] = None
        fetch_limit_run_for_query: Optional[int] = None

        if is_dsn_configured():
            market_query_mode = st.radio(
                "조회 방식",
                [
                    "실행(run) 단위 — 저장된 분석 선택",
                    "기간·키워드 필터 — DB 전체 검색",
                ],
                horizontal=True,
                key="market_score_query_mode",
                help=(
                    "실행(run) 단위: 「1. 실행로그」에서 `분석 실행`으로 저장된 묶음입니다. "
                    "한 번에 여러 주제어를 실행해도 같은 시각에 기록된 결과는 한 줄(run)로 묶입니다."
                ),
            )
            if market_query_mode.startswith("실행(run)"):
                try:
                    run_hist = query_analysis_runs_history(limit=100)
                except Exception as e:
                    st.warning(f"실행 이력 조회 실패: {e}")
                    run_hist = []
                if not run_hist:
                    skip_market_query = True
                    st.info(
                        "저장된 분석 실행이 없습니다. DB가 켜진 상태에서 「1. 실행로그」에서 "
                        "`분석 실행`을 완료하면 이 목록에 표시됩니다."
                    )
                else:
                    labels = [_format_analysis_run_history_label(r) for r in run_hist]
                    pick_idx = st.selectbox(
                        "분석 실행 선택",
                        options=list(range(len(run_hist))),
                        format_func=lambda i: labels[int(i)],
                        index=0,
                        key="market_score_selected_run_idx",
                        help="목록은 최신 실행 순입니다. 첫 항목이 가장 최근 분석입니다.",
                    )
                    picked = run_hist[int(pick_idx)]
                    selected_run_id = int(picked["run_id"])
                    picked_metric_rows = int(picked.get("metric_rows") or 0)
                    if picked_metric_rows == 0:
                        st.warning(
                            "선택한 실행에 저장된 연관 키워드 행이 **0건**입니다. "
                            "실패한 실행이거나 DB 적재 전 중단된 경우일 수 있습니다."
                        )
                    run_fetch_full = st.checkbox(
                        "전체 보기 (최대 15,000행)",
                        value=False,
                        key="market_run_full_fetch",
                        help="끄면 처음 3,000행만 불러와 대용량 run에서도 UI가 가볍게 유지됩니다.",
                    )
                    fetch_limit_run_for_query = 15000 if run_fetch_full else 3000
                    st.caption(
                        f"선택한 실행 ID: `{picked.get('run_id')}` · 상태 `{picked.get('status')}` · "
                        f"DB 저장 행: **{picked_metric_rows}**건 · 이번 조회 상한: **{fetch_limit_run_for_query}**건"
                    )

        if is_dsn_configured():
            with st.expander("데이터랩 인사이트 파이프라인 (MariaDB 저장분)", expanded=False):
                st.caption(
                    "주제어 → 쇼핑 카테고리 → 데이터랩 분야 인기검색어 Top N 경로로 적재된 행입니다. "
                    "기존 상품 분석(`분석 실행`)과 별도 테이블입니다."
                )
                try:
                    id_rows = query_insight_discovery_rows(limit=120)
                except Exception as e:
                    st.warning(f"인사이트 DB 조회 실패: {e}")
                    id_rows = []
                if id_rows:
                    id_df = pd.DataFrame(id_rows)
                    rename_map = {
                        "created_at": "저장시각",
                        "seed_keyword": "주제어",
                        "shopping_category_path": "쇼핑 카테고리 경로",
                        "datalab_category_id": "데이터랩 cid",
                        "row_kind": "행 종류",
                        "insight_rank": "인사이트 순위",
                        "keyword_text": "키워드",
                        "mobile_monthly_qc": "모바일 월 검색수",
                        "mobile_monthly_clk": "모바일 월 클릭수",
                        "ctr_pct": "CTR(%)",
                        "product_count": "쇼핑 상품수(추정)",
                        "market_fit_score": "시장 접목 점수",
                        "vs_seed_volume_ratio": "검색수 비(주제어 대비)",
                        "vs_seed_click_ratio": "클릭수 비(주제어 대비)",
                    }
                    id_view = id_df.rename(columns={k: v for k, v in rename_map.items() if k in id_df.columns})
                    st.dataframe(_accounting_format_styler(id_view), width="stretch", hide_index=True)
                else:
                    st.info("저장된 인사이트 행이 없습니다. 사이드바에서 인사이트 Top20을 실행하고 DB 저장을 켠 상태로 다시 시도하세요.")

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

        if is_dsn_configured() and market_query_mode.startswith("실행(run)"):
            st.caption(
                "실행(run) 선택 모드에서는 **기간** 필터가 적용되지 않습니다. "
                "아래 키워드 필터만 선택한 실행 안에서 부분 검색됩니다."
            )

        filter_col1, filter_col2, filter_col3, filter_col4 = st.columns([2, 1, 1, 1])
        with filter_col1:
            keyword_like = st.text_input("키워드 필터", value="", key="ms_kw_filter")
        with filter_col2:
            period = st.selectbox(
                "기간",
                ["오늘", "최근 7일", "최근 30일", "최근 60일", "최근 120일", "전체"],
                index=1,
                key="ms_period_filter",
            )
        with filter_col3:
            if selected_run_id is None:
                limit_sel = st.selectbox("건수", [20, 50, 100, 200], index=1, key="ms_limit_sel")
            else:
                limit_sel = int(fetch_limit_run_for_query or 3000)
                st.caption(f"실행(run) 조회 상한 **{limit_sel:,}** 행")
        with filter_col4:
            sort_by = st.selectbox(
                "정렬",
                [
                    "2차 최종 점수",
                    "대시보드 최종 점수",
                    "블루오션 점수",
                    "판매력 점수",
                    "월검색량(수요)",
                    "분석시각",
                ],
                index=0,
                key="ms_sort_by",
            )

        band_col1, band_col2 = st.columns([3, 1])
        with band_col1:
            selected_bands = st.multiselect(
                "판단 밴드",
                ["GO", "WATCH", "DROP"],
                default=["GO", "WATCH", "DROP"],
                key="ms_bands",
            )
        with band_col2:
            go_only = st.checkbox("GO만 보기", value=False, key="ms_go_only")

        started_from, started_to = _period_to_range(period)

        score_rows: List[dict] = []
        if is_dsn_configured():
            try:
                if skip_market_query:
                    score_rows = []
                else:
                    score_rows = query_market_score_rows(
                        limit=int(limit_sel),
                        keyword_like=(keyword_like or "").strip() or None,
                        started_from=started_from if selected_run_id is None else None,
                        started_to=started_to if selected_run_id is None else None,
                        run_id=selected_run_id,
                    )
            except Exception as e:
                st.error(f"DB 조회 실패: {e}")
                score_rows = []

        if score_rows:
            if selected_run_id is not None and picked_metric_rows is not None:
                if picked_metric_rows > len(score_rows):
                    st.warning(
                        f"이 실행에는 DB에 **{picked_metric_rows:,}**행이 있으나, "
                        f"현재 설정으로 **{len(score_rows):,}**행만 불러왔습니다. "
                        "「전체 보기」를 켜거나 키워드 필터를 조정해 보세요."
                    )
            if len(score_rows) >= 2800:
                st.info(
                    f"불러온 행이 **{len(score_rows):,}**개입니다. 아래 필터·정렬은 브라우저에서 처리됩니다."
                )

            score_df = pd.DataFrame(score_rows)

            mf1, mf2 = st.columns(2)
            with mf1:
                sel_intent = st.multiselect(
                    "검색 의도",
                    ["구매형", "탐색형", "정보형"],
                    default=["구매형", "탐색형", "정보형"],
                    key="ms_intent_filter",
                    help="키워드 텍스트 규칙으로 재분류합니다(DB 저장값 아님).",
                )
            with mf2:
                sel_season = st.multiselect(
                    "시즌 유형",
                    ["seasonal", "trend", "steady"],
                    default=["seasonal", "trend", "steady"],
                    key="ms_season_filter",
                    help="저장된 월별 검색 추정 시계열로 재추정합니다.",
                )

            if sel_intent:
                score_df = score_df[score_df["intent"].isin(sel_intent)]
            if sel_season:
                score_df = score_df[score_df["season_type"].isin(sel_season)]

            comp_col = "competition_score"
            sale_col = "sales_power"
            if not score_df.empty:
                clo = float(score_df[comp_col].min())
                chi = float(score_df[comp_col].max())
                slo = float(score_df[sale_col].min())
                shi = float(score_df[sale_col].max())
                sf1, sf2 = st.columns(2)
                with sf1:
                    if chi > clo:
                        crng = st.slider(
                            "경쟁 점수 범위 (상품수 log 기반)",
                            min_value=clo,
                            max_value=chi,
                            value=(clo, chi),
                            key="ms_comp_slider",
                        )
                    else:
                        crng = (clo, chi)
                        st.caption(f"경쟁 점수가 단일값(**{clo:.4f}**)이라 범위 조정 없음.")
                with sf2:
                    if shi > slo:
                        srng = st.slider(
                            "판매력 점수 범위",
                            min_value=slo,
                            max_value=shi,
                            value=(slo, shi),
                            key="ms_sales_slider",
                        )
                    else:
                        srng = (slo, shi)
                        st.caption(f"판매력 점수가 단일값(**{slo:.2f}**)이라 범위 조정 없음.")
                score_df = score_df[
                    (score_df[comp_col] >= crng[0])
                    & (score_df[comp_col] <= crng[1])
                    & (score_df[sale_col] >= srng[0])
                    & (score_df[sale_col] <= srng[1])
                ]

            score_df = _apply_band_filter(score_df, selected_bands, go_only)

            if score_df.empty:
                st.warning("표시할 행이 없습니다. 검색 의도·시즌·점수 슬라이더·판단 밴드 조건을 완화해 보세요.")
            else:
                st.markdown("##### 선택 결과 요약")
                m1, m2, m3, m4, m5 = st.columns(5)
                with m1:
                    st.metric("표시 행 수", f"{len(score_df):,}")
                with m2:
                    st.metric("평균 블루오션", f"{float(score_df['blue_ocean_score'].mean()):,.2f}")
                with m3:
                    st.metric("평균 경쟁도", f"{float(score_df['competition_score'].mean()):,.4f}")
                pur = float((score_df["intent"].astype(str) == "구매형").mean() * 100.0)
                with m4:
                    st.metric("구매형 비율", f"{pur:.1f}%")
                st_mix = float(
                    (
                        score_df["season_type"].isin(["seasonal", "trend"]).astype(float).mean()
                        * 100.0
                    )
                )
                with m5:
                    st.metric("seasonal+trend 비율", f"{st_mix:.1f}%")

                sort_map = {
                    "2차 최종 점수": ("final_score", False),
                    "대시보드 최종 점수": ("market_score", False),
                    "블루오션 점수": ("blue_ocean_score", False),
                    "판매력 점수": ("sales_power", False),
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
                    "intent",
                    "season_type",
                    "monthly_search_volume_est",
                    "product_count",
                    "demand_score",
                    "trend_score",
                    "conversion_score",
                    "competition_score",
                    "sales_power",
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
                        "intent": "검색 의도",
                        "season_type": "시즌 유형",
                        "monthly_search_volume_est": "월검색량(수요)",
                        "product_count": "상품수(경쟁)",
                        "demand_score": "수요 점수",
                        "trend_score": "트렌드 점수",
                        "conversion_score": "전환 점수",
                        "competition_score": "경쟁 점수",
                        "sales_power": "판매력 점수",
                        "market_score": "대시보드 최종 점수",
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
                        _accounting_format_styler(view_df, band_column="판단 밴드"),
                        width="stretch",
                        hide_index=True,
                    )
                else:
                    st.dataframe(_accounting_format_styler(view_df), width="stretch", hide_index=True)

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
                    "전환 점수는 쿠팡 Top10 평균리뷰수/평균가격이 있으면 우선 사용합니다. "
                    "판매력 점수는 DB `판매가치`가 있으면 그 값을 쓰고, 없으면 Top10·클릭 기반 추정입니다. "
                    "검색 의도·시즌 유형은 조회 시점에 재계산된 값입니다."
                )

                ts = datetime.now().strftime("%Y%m%d_%H%M")
                xlsx_db = report_to_excel_bytes(view_df)
                run_fn = f"run{selected_run_id}_" if selected_run_id is not None else ""
                st.download_button(
                    label="엑셀 다운로드 (현재 필터·정렬 적용 결과)",
                    data=xlsx_db,
                    file_name=f"모디바_시장성_{run_fn}{ts}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="dl_market_score_report",
                    width="stretch",
                )
        else:
            if is_dsn_configured():
                st.info("조회 결과가 없습니다.")

    with tab_coupang:
        render_coupang_keyword_analysis_tab()


if __name__ == "__main__":
    run()

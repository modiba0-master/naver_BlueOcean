"""Streamlit 대시보드 공통: 스타일, 포맷, Tool 캐시, 네이버 카테고리 탐지."""
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

from blue_ocean_tool import BlueOceanTool

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
    """로컬에서 Chrome(있으면) 또는 기본 브라우저로 구글 홈만 연다."""
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


def inject_dashboard_style() -> None:
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
    c = str(col_name)
    if any(x in c for x in ("CTR", "비(", "비율", "점수", "접목", "ratio")):
        return "{:,.4f}"
    return "{:,.2f}"


def accounting_format_styler(df: pd.DataFrame, *, band_column: Optional[str] = None):
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


def apply_band_filter(df: pd.DataFrame, selected_bands: List[str], go_only: bool) -> pd.DataFrame:
    if "decision_band" not in df.columns:
        return df
    result = df.copy()
    if go_only:
        result = result[result["decision_band"].astype(str).str.upper() == "GO"]
    elif selected_bands:
        bands_upper = {b.upper() for b in selected_bands}
        result = result[result["decision_band"].astype(str).str.upper().isin(bands_upper)]
    return result


def period_to_range(label: str) -> Tuple[Optional[datetime], Optional[datetime]]:
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


def format_analysis_run_history_label(row: Dict[str, Any]) -> str:
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


# Streamlit은 프로세스 동안 @st.cache_resource 인스턴스를 재사용한다.
# BlueOceanTool에 메서드/속성이 추가되면 구버전 인스턴스에는 반영되지 않으므로
# API 표면이 바뀔 때마다 이 값을 올려 캐시를 무효화한다.
_TOOL_RESOURCE_VERSION = 3


@st.cache_resource
def get_tool(_resource_version: int = _TOOL_RESOURCE_VERSION) -> BlueOceanTool:
    _ = _resource_version
    return BlueOceanTool(config_path="config.json")


def should_skip_coupang_top10_caption() -> bool:
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

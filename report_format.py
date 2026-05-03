"""
모디바 카테고리별 TOP10 / 통합분석 리포트 엑셀과 동일한 열 구성.

참고 열(순서 고정):
  주제어, 키워드, 월평균 검색수(추정), 익월 검색량(추정)(시즌·상승 시만),
  월평균 클릭수(추정), 평균 클릭율(CTR), 상품수, 블루오션 점수, 전략 제언
"""

from __future__ import annotations

import io
from typing import Any, Dict, Iterable, List

import pandas as pd

EXCEL_REPORT_COLUMNS: List[str] = [
    "주제어",
    "키워드",
    "월평균 검색수(추정)",
    "익월 검색량(추정)",
    "월평균 클릭수(추정)",
    "평균 클릭율(CTR)",
    "상품수",
    "블루오션 점수",
    "전략 제언",
]

NEXT_MONTH_VOLUME_COL = "익월 검색량(추정)"


def coerce_next_month_volume_column(df: pd.DataFrame) -> pd.DataFrame:
    """
    Streamlit(PyArrow) 직렬화: '익월 검색량(추정)'에 문자열(예: '—')·int 혼합 object 열이 있으면 ArrowTypeError.
    숫자만 nullable Int64로 통일한다.
    """
    if df.empty or NEXT_MONTH_VOLUME_COL not in df.columns:
        return df
    out = df.copy()
    out[NEXT_MONTH_VOLUME_COL] = pd.to_numeric(
        out[NEXT_MONTH_VOLUME_COL], errors="coerce"
    ).astype("Int64")
    return out


def _format_ctr(value: Any) -> str:
    if value is None:
        return "0.00%"
    s = str(value).strip()
    if s.endswith("%"):
        return s
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "0.00%"


def _format_blue_ocean_score_pct(value: Any) -> str:
    if value is None:
        return "0.00%"
    s = str(value).strip()
    if s.endswith("%"):
        return s
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "0.00%"


def finalize_analysis_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """분석 단계에서 만든 DataFrame을 템플릿 열 순서로 맞춤."""
    missing = [c for c in EXCEL_REPORT_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"리포트용 DataFrame에 누락된 열: {missing}")
    out = df[EXCEL_REPORT_COLUMNS].copy()
    return coerce_next_month_volume_column(out)


def dataframe_from_db_metric_rows(rows: Iterable[Dict[str, Any]]) -> pd.DataFrame:
    """DB/쿼리 결과(dict)를 템플릿 열 이름으로 변환."""
    records: List[Dict[str, Any]] = []
    for r in rows:
        records.append(
            {
                "주제어": r.get("seed_keyword", ""),
                "키워드": r.get("keyword_text", ""),
                "월평균 검색수(추정)": int(r.get("monthly_search_volume_est", 0) or 0),
                "익월 검색량(추정)": pd.NA,
                "월평균 클릭수(추정)": float(r.get("monthly_click_est", 0.0) or 0.0),
                "평균 클릭율(CTR)": _format_ctr(r.get("avg_ctr_pct")),
                "상품수": int(r.get("product_count", 0) or 0),
                "블루오션 점수": _format_blue_ocean_score_pct(r.get("blue_ocean_score", 0.0)),
                "전략 제언": (r.get("strategy_text") or "").strip(),
            }
        )
    out = pd.DataFrame.from_records(records, columns=EXCEL_REPORT_COLUMNS)
    return coerce_next_month_volume_column(out)


def report_to_excel_bytes(df: pd.DataFrame) -> bytes:
    """Streamlit download_button 등에 넘길 xlsx 바이너리."""
    buf = io.BytesIO()
    # 템플릿과 동일 열만 쓰기
    ordered = df[EXCEL_REPORT_COLUMNS] if all(c in df.columns for c in EXCEL_REPORT_COLUMNS) else df
    ordered.to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()

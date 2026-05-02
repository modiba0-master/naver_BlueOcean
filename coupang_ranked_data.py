"""
쿠팡 검색 스모크 순위표 데이터: 메모리 → MariaDB → JSON 파일 폴백 해석 (Streamlit 비의존).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd


def default_smoke_extract_json_path() -> Path:
    return Path(__file__).resolve().parent / ".smoke" / "last_smoke_extract.json"


class RankedSource(str, Enum):
    EMPTY = "empty"
    MEMORY = "memory"
    DATABASE = "database"
    JSON_FILE = "json_file"


@dataclass(frozen=True)
class CoupangRankedSnapshot:
    """순위 행 목록과 출처. DB 조회 예외 시에도 JSON 폴백을 채울 수 있도록 db_error를 유지한다."""

    items: List[Dict[str, Any]]
    source: RankedSource
    db_error: Optional[str] = None


def smoke_file_fallback_ranked(smoke_json_path: Path, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
    """메모리/DB가 비었을 때 last_smoke_extract.json 과 동일 키워드면 순위표 폴백."""
    if not smoke_json_path.is_file():
        return []
    try:
        with open(smoke_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return []
        if str(data.get("keyword", "")).strip() != str(keyword).strip():
            return []
        raw_items = data.get("top10") or data.get("top3") or []
        if not isinstance(raw_items, list):
            return []
        out: List[Dict[str, Any]] = []
        for it in raw_items[:limit]:
            if not isinstance(it, dict):
                continue
            try:
                rk = int(it.get("rank") or 0)
            except Exception:
                rk = 0
            if rk < 1:
                continue
            out.append(
                {
                    "rank": rk,
                    "title": str(it.get("title", "")),
                    "price": str(it.get("price", "")),
                    "shipping": str(it.get("shipping", "")),
                    "review_count": str(it.get("review_count", "")),
                    "review_score": str(it.get("review_score", "")),
                    "url": str(it.get("url", "")),
                }
            )
        return out
    except Exception:
        return []


def resolve_coupang_ranked_snapshot(
    keyword: str,
    *,
    get_smoke_ranked_ui_cache: Optional[Callable[[str], List[Dict[str, Any]]]],
    query_db_latest: Callable[[str, int], List[Dict[str, Any]]],
    dsn_configured: bool,
    smoke_json_path: Path,
    limit: int = 10,
) -> CoupangRankedSnapshot:
    kw = str(keyword).strip()
    if not kw:
        return CoupangRankedSnapshot([], RankedSource.EMPTY)

    db_error: Optional[str] = None

    if callable(get_smoke_ranked_ui_cache):
        mem_items = list(get_smoke_ranked_ui_cache(kw))
        if mem_items:
            return CoupangRankedSnapshot(mem_items, RankedSource.MEMORY)

    coupang_items: List[Dict[str, Any]] = []
    if dsn_configured:
        try:
            coupang_items = list(query_db_latest(kw, limit))
        except Exception as ex:
            db_error = str(ex)
        if coupang_items:
            return CoupangRankedSnapshot(coupang_items, RankedSource.DATABASE, db_error=db_error)

    json_items = smoke_file_fallback_ranked(smoke_json_path, kw, limit=limit)
    if json_items:
        return CoupangRankedSnapshot(json_items, RankedSource.JSON_FILE, db_error=db_error)

    return CoupangRankedSnapshot([], RankedSource.EMPTY, db_error=db_error)


def build_top10_rank_dataframe(coupang_items: List[Dict[str, Any]]) -> pd.DataFrame:
    """순위 1~10 행 템플릿을 채운 표시용 DataFrame."""
    top10_template = pd.DataFrame(
        [
            {
                "순위": rank,
                "상품명": "",
                "가격(원)": "",
                "리뷰수": "",
                "평점": "",
                "배송비": "",
                "상품 URL": "",
            }
            for rank in range(1, 11)
        ]
    )
    by_rank = {int(i["rank"]): i for i in coupang_items if isinstance(i, dict) and i.get("rank")}
    for rk in range(1, 11):
        item = by_rank.get(rk)
        if not item:
            continue
        top10_template.at[rk - 1, "상품명"] = str(item.get("title", "")).strip()
        top10_template.at[rk - 1, "가격(원)"] = str(item.get("price", "")).strip()
        top10_template.at[rk - 1, "리뷰수"] = str(item.get("review_count", "")).strip()
        top10_template.at[rk - 1, "평점"] = str(item.get("review_score", "")).strip()
        top10_template.at[rk - 1, "배송비"] = str(item.get("shipping", "")).strip()
        top10_template.at[rk - 1, "상품 URL"] = str(item.get("url", "")).strip()
    return top10_template

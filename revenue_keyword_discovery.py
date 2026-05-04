"""
키워드 발굴: 1차 카테고리(버티컬) + 2차 매출 규모(small/medium/large) 프리셋.
네이버 쇼핑 검색 API의 category 경로와 매칭하며, 스코어 수식은 변경하지 않는다.
"""
from __future__ import annotations

import copy
import os
from typing import Any, Dict, List, Optional, Tuple

import yaml

_ROOT = os.path.dirname(os.path.abspath(__file__))
_DISCOVERY_PATH = os.path.join(_ROOT, "config", "revenue_keyword_discovery.yaml")

# 파일 없을 때 최소 동작용 (medium + 카테고리 없음)
_EMBEDDED: Dict[str, Any] = {
    "tier_presets": {
        "small_10m": {
            "filter": {"min_search": 350, "min_ctr": 1.05, "max_search": 15000},
            "pool": {"max_products": 32000},
            "coupang": {"topk_validate": 3, "min_review_topk": 55, "min_avg_price_topk": 11000.0},
        },
        "medium_30m": {
            "filter": {"min_search": 600, "min_ctr": 1.0},
            "pool": {"max_products": 50000},
            "coupang": {"topk_validate": 3, "min_review_topk": 100, "min_avg_price_topk": 8000.0},
        },
        "large_50m": {
            "filter": {"min_search": 1500, "min_ctr": 0.88},
            "pool": {"max_products": 80000},
            "coupang": {"topk_validate": 3, "min_review_topk": 160, "min_avg_price_topk": 7000.0},
        },
    },
    "verticals": {},
}

VERTICAL_UI_OPTIONS: List[str] = [
    "패션잡화",
    "미용",
    "인테리어",
    "생활용품",
    "반려동물 용품",
    "주방용품",
    "욕실용품",
]

TIER_UI_KEYS: List[Tuple[str, str]] = [
    ("small_10m", "소규모 · 월 약 1,000만 원 매출 키워드"),
    ("medium_30m", "중규모 · 월 약 3,000만 원 매출 키워드"),
    ("large_50m", "대규모 · 월 약 5,000만 원 매출 키워드"),
]


def load_discovery_doc() -> Dict[str, Any]:
    doc = copy.deepcopy(_EMBEDDED)
    if os.path.isfile(_DISCOVERY_PATH):
        try:
            with open(_DISCOVERY_PATH, "r", encoding="utf-8") as f:
                y = yaml.safe_load(f) or {}
            if isinstance(y, dict):
                if isinstance(y.get("tier_presets"), dict):
                    doc["tier_presets"].update(y["tier_presets"])
                if isinstance(y.get("verticals"), dict):
                    doc["verticals"].update(y["verticals"])
        except Exception:
            pass
    return doc


def _l1_from_path(cat_path: str) -> str:
    s = str(cat_path or "").strip()
    if not s:
        return ""
    return s.split(">")[0].strip()


def category_matches_vertical(cat_path: str, match: Optional[Dict[str, Any]]) -> bool:
    """match: { l1_any: [...], path_contains_any: [...] }"""
    if not match or not isinstance(match, dict):
        return True
    path = str(cat_path or "").strip()
    if not path:
        return False
    l1 = _l1_from_path(path)
    l1_any = match.get("l1_any") or []
    if isinstance(l1_any, list) and l1_any:
        if l1 not in [str(x).strip() for x in l1_any if str(x).strip()]:
            return False
    subs = match.get("path_contains_any") or []
    if isinstance(subs, list) and subs:
        if not any(str(s).strip() and str(s).strip() in path for s in subs):
            return False
    return True


def build_discovery_context(
    vertical: Optional[str],
    tier: Optional[str],
) -> Tuple[Dict[str, Any], List[str], Optional[Dict[str, Any]], List[str]]:
    """
    반환:
      preset_for_settings: load_recommend_settings에 deep_merge할 dict (filter/pool/coupang)
      seeds_extra: 시드에 병합할 힌트
      match_spec: 카테고리 경로 필터(없으면 필터 생략)
      log_lines
    """
    logs: List[str] = []
    doc = load_discovery_doc()
    tier_key = (tier or "").strip() or "medium_30m"
    if tier_key not in doc.get("tier_presets", {}):
        tier_key = "medium_30m"
        logs.append(f"[Discovery] unknown tier → fallback medium_30m")

    tp = doc["tier_presets"].get(tier_key) or {}
    preset: Dict[str, Any] = {}
    for block in ("filter", "pool", "coupang"):
        if isinstance(tp.get(block), dict):
            preset[block] = copy.deepcopy(tp[block])

    vkey = (vertical or "").strip()
    seeds_extra: List[str] = []
    match_spec: Optional[Dict[str, Any]] = None
    if vkey:
        vdoc = (doc.get("verticals") or {}).get(vkey)
        if isinstance(vdoc, dict):
            seeds_extra = [str(x).strip() for x in (vdoc.get("seed_hints") or []) if str(x).strip()]
            m = vdoc.get("match")
            if isinstance(m, dict) and m:
                match_spec = m
        else:
            logs.append(f"[Discovery] vertical `{vkey}` not in YAML; category filter skipped")
            vkey = ""

    logs.append(f"[Discovery] tier={tier_key}" + (f" vertical={vkey}" if vkey else ""))
    return preset, seeds_extra, match_spec, logs


def parse_discovery_from_ui_settings(ui: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    if not ui or not isinstance(ui, dict):
        return None, None
    d = ui.get("discovery")
    if not isinstance(d, dict):
        return None, None
    v = d.get("vertical")
    t = d.get("tier")
    vs = str(v).strip() if v is not None and str(v).strip() else None
    ts = str(t).strip() if t is not None and str(t).strip() else None
    return vs, ts

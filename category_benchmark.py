"""
주제어 → 네이버 쇼핑 노출 카테고리 추정 → 카테고리 축 연관 키워드 중 모바일 검색량 상위 N개 벤치마크.

주의: 네이버 공개 API에는 ‘쇼핑 카테고리 내 공식 검색순위 1~20’을 주는 엔드포인트가 없습니다.
여기서는 쇼핑 검색으로 다수결 카테고리를 고른 뒤, 키워드도구(광고 API)에 카테고리 leaf 등을 힌트로 넣어
연관 키워드를 받고 모바일 월간 검색수 기준 상위 N개를 ‘카테고리 인근 상위 키워드’로 사용합니다.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

DATALAB_SHOPPING_KEYWORD_TREND_URL = "https://openapi.naver.com/v1/datalab/shopping/category/keywords"


def _norm_kw(text: str) -> str:
    return "".join(str(text or "").split()).lower()


# 카테고리 벤치마크 연관어 중 ‘매장·맛집·레시피·조리 정보’ 등 쇼핑 상품 소싱과 거리가 큰 표현 (부분 문자열 매칭).
# 완전 분류는 불가하며, 오탐·미탐이 있을 수 있음 → 필요 시 튜닝.
_STORE_INTENT_BLOCKLIST_SUBSTRINGS: Tuple[str, ...] = (
    "맛집",
    "레시피",
    "만드는법",
    "만드는방법",
    "하는법",
    "하는방법",
    "조리법",
    "손질법",
    "보관법",
    "먹는법",
)


def is_non_store_product_keyword(keyword: str) -> bool:
    """상품 판매·스토어 소싱보다 지역·정보 검색에 가까운 키워드면 True."""
    compact = "".join(str(keyword or "").split())
    if not compact:
        return True
    return any(tok in compact for tok in _STORE_INTENT_BLOCKLIST_SUBSTRINGS)


def clean_val(v: Any) -> float:
    if isinstance(v, str) and "<" in v:
        return 5.0
    try:
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0


def dominant_category_from_shop(
    seed: str,
    *,
    client_id: str,
    client_secret: str,
    display: int = 50,
) -> Tuple[str, str]:
    """쇼핑 검색 상위 노출 상품의 카테고리 경로 중 최빈값 1개."""
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    url = "https://openapi.naver.com/v1/search/shop.json"
    params = {"query": seed, "display": display, "sort": "sim"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=15)
        if res.status_code != 200:
            return "", f"쇼핑 검색 실패 HTTP {res.status_code}"
        items = res.json().get("items", []) or []
    except Exception as e:
        return "", f"쇼핑 검색 오류: {e}"

    if not items:
        return "", "쇼핑 검색 결과가 없어 카테고리를 정하지 못했습니다."

    from collections import Counter

    cnt: Counter[str] = Counter()
    for it in items:
        c1 = str(it.get("category1", "")).strip()
        c2 = str(it.get("category2", "")).strip()
        c3 = str(it.get("category3", "")).strip()
        c4 = str(it.get("category4", "")).strip()
        cats = [c for c in [c1, c2, c3, c4] if c]
        if cats:
            cnt[" > ".join(cats)] += 1

    if not cnt:
        return "", "상품에 카테고리 정보가 없습니다."

    top_path = cnt.most_common(1)[0][0]
    return top_path, "ok"


def extract_keyword_metrics_row(item: Dict[str, Any]) -> Dict[str, Any]:
    mo_qc = clean_val(item.get("monthlyMobileQcCnt"))
    mo_clk = clean_val(item.get("monthlyAveMobileClkCnt"))
    ctr = (mo_clk / mo_qc * 100.0) if mo_qc > 0 else 0.0
    return {
        "rel_keyword": str(item.get("relKeyword", "") or ""),
        "monthly_mobile_qc": mo_qc,
        "monthly_mobile_clk": mo_clk,
        "ctr_pct": round(ctr, 4),
    }


def find_keyword_row(rows: List[Dict[str, Any]], target: str) -> Optional[Dict[str, Any]]:
    t = _norm_kw(target)
    for item in rows:
        if _norm_kw(str(item.get("relKeyword", "") or "")) == t:
            return item
    return None


def merge_keyword_lists(*lists: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for lst in lists:
        for item in lst or []:
            key = _norm_kw(str(item.get("relKeyword", "") or ""))
            if not key:
                continue
            prev = merged.get(key)
            if prev is None:
                merged[key] = item
                continue
            if clean_val(item.get("monthlyMobileQcCnt")) >= clean_val(prev.get("monthlyMobileQcCnt")):
                merged[key] = item
    out = list(merged.values())
    out.sort(key=lambda x: clean_val(x.get("monthlyMobileQcCnt")), reverse=True)
    return out


def _datalab_keyword_ratio_series_batches(
    headers: Dict[str, str],
    category_code: str,
    keywords: List[str],
    *,
    start_date: str,
    end_date: str,
    time_unit: str,
    sleep_sec: float = 0.12,
) -> Dict[str, List[float]]:
    """
    쇼핑인사이트 키워드별 트렌드 API — 요청당 keyword 그룹 최대 5개, 그룹당 검색어 1개.
    반환: 정규화 키(_norm_kw) → 시계열 순서의 ratio 리스트.
    """
    out: Dict[str, List[float]] = {}
    uniq: List[str] = []
    seen_nk = set()
    for kw in keywords:
        raw = str(kw or "").strip()
        if not raw:
            continue
        nk = _norm_kw(raw)
        if nk in seen_nk:
            continue
        seen_nk.add(nk)
        uniq.append(raw)

    for i in range(0, len(uniq), 5):
        chunk = uniq[i : i + 5]
        groups = [{"name": k[:80], "param": [k]} for k in chunk]
        body = {
            "startDate": start_date,
            "endDate": end_date,
            "timeUnit": time_unit,
            "category": category_code,
            "keyword": groups,
            "device": "mo",
            "gender": "",
            "ages": [],
        }
        try:
            res = requests.post(DATALAB_SHOPPING_KEYWORD_TREND_URL, headers=headers, json=body, timeout=30)
            if res.status_code != 200:
                time.sleep(sleep_sec)
                continue
            payload = res.json()
            for block in payload.get("results") or []:
                klist = block.get("keyword") or []
                kw_key = _norm_kw(str(klist[0])) if klist else ""
                rows = sorted(block.get("data") or [], key=lambda d: str(d.get("period", "")))
                ratios = [float(d.get("ratio") or 0.0) for d in rows]
                if kw_key:
                    out[kw_key] = ratios
        except Exception:
            pass
        time.sleep(sleep_sec)
    return out


def _uniq_preserve(tags: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _future_growth_expected(month_ratios: List[float], season: str) -> bool:
    """단기 평균이 중기 평균보다 높거나, 계절형에서 최근 구간 상승 시 향후 증가 여력으로 표시."""
    if len(month_ratios) < 4:
        return False
    short = sum(month_ratios[-2:]) / 2.0
    if len(month_ratios) >= 5:
        mid = sum(month_ratios[-5:-2]) / 3.0
    else:
        mid = sum(month_ratios[:-2]) / max(1, len(month_ratios) - 2)
    if mid > 0 and short >= mid * 1.06:
        return True
    if season == "seasonal" and len(month_ratios) >= 3 and month_ratios[-1] > month_ratios[-2]:
        return True
    return False


def _compose_importance_tags(tool: Any, month_ratios: List[float], week_ratios: List[float]) -> str:
    tags: List[str] = []
    season = "steady"
    if len(month_ratios) >= 3:
        season = tool.detect_seasonality(month_ratios)

    if len(month_ratios) >= 4:
        baseline = sum(month_ratios[-4:-1]) / 3.0
        last_m = month_ratios[-1]
        if baseline > 0 and last_m >= baseline * 1.22:
            tags.append("월 급상승")

    if len(week_ratios) >= 6:
        base_w = sum(week_ratios[-6:-1]) / 5.0
        last_w = week_ratios[-1]
        if base_w > 0 and last_w >= base_w * 1.18:
            tags.append("주 급상승")

    if season == "seasonal":
        tags.append("계절·시즌성")
    elif season == "trend":
        tags.append("상승 추세")

    if len(month_ratios) >= 4 and _future_growth_expected(month_ratios, season):
        tags.append("향후 검색 증가 예상")

    tags = _uniq_preserve(tags)
    return " · ".join(tags) if tags else "—"


def _attach_benchmark_importance_tags(
    tool: Any,
    cat_path: str,
    rows: List[Dict[str, Any]],
) -> Tuple[int, int]:
    """
    rows 내 각 행에 '중요도 태그' 채움. 반환: (성공 조회 키워드 수, 전체 키워드 수)
    """
    open_headers = getattr(tool, "open_headers", None) or {}
    if not isinstance(open_headers, dict) or not open_headers.get("X-Naver-Client-Id"):
        for r in rows:
            r["중요도 태그"] = "—"
        return 0, len(rows)

    try:
        from shopping_insight_benchmark import resolve_root_cid
    except Exception:
        for r in rows:
            r["중요도 태그"] = "—"
        return 0, len(rows)

    root_cid, _cid_label = resolve_root_cid(cat_path)
    cid_str = str(int(root_cid))

    kws = [str(r.get("키워드", "")).strip() for r in rows if str(r.get("키워드", "")).strip()]
    if not kws:
        return 0, 0

    today = date.today()
    end_m = today.strftime("%Y-%m-%d")
    start_m = (today - timedelta(days=420)).strftime("%Y-%m-%d")
    end_w = end_m
    start_w = (today - timedelta(days=120)).strftime("%Y-%m-%d")

    month_map = _datalab_keyword_ratio_series_batches(
        open_headers,
        cid_str,
        kws,
        start_date=start_m,
        end_date=end_m,
        time_unit="month",
    )
    week_map = _datalab_keyword_ratio_series_batches(
        open_headers,
        cid_str,
        kws,
        start_date=start_w,
        end_date=end_w,
        time_unit="week",
    )

    ok = 0
    for r in rows:
        kw = str(r.get("키워드", "")).strip()
        nk = _norm_kw(kw)
        mv = month_map.get(nk, [])
        wv = week_map.get(nk, [])
        if not mv and not wv:
            r["중요도 태그"] = "트렌드 미조회"
        else:
            ok += 1
            r["중요도 태그"] = _compose_importance_tags(tool, mv, wv)
    return ok, len(rows)


def run_category_benchmark(
    tool: Any,
    seed_keyword: str,
    *,
    top_n: int = 20,
    product_sleep_sec: float = 0.035,
) -> Tuple[pd.DataFrame, str]:
    """
    반환: (비교 표 DataFrame, 안내 메모 문자열)
    tool: BlueOceanTool (ads_api, get_product_info, open_headers/config 사용)
    """
    seed = str(seed_keyword or "").strip()
    if not seed:
        return pd.DataFrame(), "주제어가 비어 있습니다."

    cfg = getattr(tool, "config", {}) or {}
    naver_open = cfg.get("naver_open_api") or {}
    cid = str(naver_open.get("client_id", "") or "").strip()
    csec = str(naver_open.get("client_secret", "") or "").strip()
    if not cid or not csec:
        return pd.DataFrame(), "네이버 Open API(client_id/secret) 설정이 필요합니다."

    cat_path, shop_msg = dominant_category_from_shop(seed, client_id=cid, client_secret=csec)
    if not cat_path:
        return pd.DataFrame(), shop_msg

    parts = [p.strip() for p in cat_path.split(">") if str(p).strip()]
    leaf = parts[-1] if parts else seed
    leaf_hint = leaf.replace(" ", "")
    parent_leaf_hint = ""
    if len(parts) >= 2:
        parent_leaf_hint = (parts[-2] + parts[-1]).replace(" ", "")

    ads_api = getattr(tool, "ads_api", None)
    if ads_api is None:
        return pd.DataFrame(), "광고 API(키워드도구)를 사용할 수 없습니다."

    raw_leaf = ads_api.get_related_keywords(leaf_hint) or []
    raw_parent_leaf = ads_api.get_related_keywords(parent_leaf_hint) if parent_leaf_hint else []
    seed_compact = seed.replace(" ", "")
    raw_seed = ads_api.get_related_keywords(seed_compact) or []

    merged_cat = merge_keyword_lists(raw_leaf, raw_parent_leaf)
    used_seed_pool_fallback = len(merged_cat) == 0
    cat_pool = merge_keyword_lists(raw_seed) if used_seed_pool_fallback else merged_cat
    cat_pool.sort(key=lambda x: clean_val(x.get("monthlyMobileQcCnt")), reverse=True)

    seed_row = find_keyword_row(raw_seed, seed) or find_keyword_row(cat_pool, seed)
    if seed_row is None:
        sm = extract_keyword_metrics_row(
            {
                "relKeyword": seed,
                "monthlyMobileQcCnt": 0,
                "monthlyAveMobileClkCnt": 0,
            }
        )
        seed_kw = seed
        seed_qc = sm["monthly_mobile_qc"]
        seed_clk = sm["monthly_mobile_clk"]
        seed_ctr = sm["ctr_pct"]
    else:
        sm = extract_keyword_metrics_row(seed_row)
        seed_kw = sm["rel_keyword"] or seed
        seed_qc = sm["monthly_mobile_qc"]
        seed_clk = sm["monthly_mobile_clk"]
        seed_ctr = sm["ctr_pct"]

    def product_total(kw: str) -> int:
        try:
            total, _ = tool.get_product_info(kw)
            return int(total or 0)
        except Exception:
            return 0

    seed_pc = product_total(seed_kw)
    time.sleep(product_sleep_sec)

    seeds_norm = _norm_kw(seed_kw)
    bench_rows: List[Dict[str, Any]] = []
    seen = {seeds_norm}
    rank = 0
    skipped_non_product = 0
    for item in cat_pool:
        kw = str(item.get("relKeyword", "") or "").strip()
        if not kw or _norm_kw(kw) in seen:
            continue
        if is_non_store_product_keyword(kw):
            skipped_non_product += 1
            continue
        seen.add(_norm_kw(kw))
        rank += 1
        if rank > top_n:
            break
        m = extract_keyword_metrics_row(item)
        pc = product_total(kw)
        time.sleep(product_sleep_sec)
        vs_q = (m["monthly_mobile_qc"] / seed_qc) if seed_qc > 0 else None
        vs_c = None
        if seed_clk > 0:
            vs_c = m["monthly_mobile_clk"] / seed_clk
        elif m["monthly_mobile_clk"] > 0:
            vs_c = None
        bench_rows.append(
            {
                "구분": "카테고리 벤치마크",
                "순위": rank,
                "키워드": kw,
                "모바일 월 검색수": int(round(m["monthly_mobile_qc"])),
                "모바일 월 클릭수": int(round(m["monthly_mobile_clk"])),
                "CTR(%)": round(m["ctr_pct"], 4),
                "쇼핑 상품수(추정)": pc,
                "검색수 비(대비 주제어)": (round(vs_q, 4) if vs_q is not None else None),
                "클릭수 비(대비 주제어)": (round(vs_c, 4) if vs_c is not None else None),
            }
        )

    vs_q_seed = 1.0
    vs_c_seed = 1.0 if seed_clk > 0 else None

    seed_out = {
        "구분": "주제어",
        "순위": "—",
        "키워드": seed_kw,
        "모바일 월 검색수": int(round(seed_qc)),
        "모바일 월 클릭수": int(round(seed_clk)),
        "CTR(%)": round(seed_ctr, 4),
        "쇼핑 상품수(추정)": seed_pc,
        "검색수 비(대비 주제어)": vs_q_seed,
        "클릭수 비(대비 주제어)": vs_c_seed,
    }

    table_rows = [seed_out] + bench_rows
    trend_ok, trend_total = _attach_benchmark_importance_tags(tool, cat_path, table_rows)
    df = pd.DataFrame(table_rows)
    _bm_cols = [
        "구분",
        "순위",
        "키워드",
        "중요도 태그",
        "모바일 월 검색수",
        "모바일 월 클릭수",
        "CTR(%)",
        "쇼핑 상품수(추정)",
        "검색수 비(대비 주제어)",
        "클릭수 비(대비 주제어)",
    ]
    df = df[[c for c in _bm_cols if c in df.columns]]

    note = (
        f"대표 카테고리(쇼핑 노출 다수결): `{cat_path}`"
        + (
            f" → 키워드도구 힌트 `{leaf_hint}`"
            + (f", `{parent_leaf_hint}`" if parent_leaf_hint else "")
            + f" 기준 연관 키워드 중 **모바일 월간 검색수 상위 {top_n}개**"
            if not used_seed_pool_fallback
            else f" — 카테고리 힌트 연관어가 비어 **주제어 키워드도구** 결과로 상위 {top_n}개를 구성"
        )
        + "와 주제어를 비교했습니다. "
        "공식 ‘카테고리 내 검색순위’ API는 제공되지 않아, 동일 의미의 근사 벤치마크입니다."
        " **중요도 태그** 열은 DataLab 쇼핑 키워드별 클릭 **비율(구간 내 최대=100 상대값)** 과 "
        "월·주 단위 증가 패턴으로 추정한 참고용 라벨입니다(실제 검색건수와 다를 수 있음)."
    )
    if trend_total > 0:
        note += f" 트렌드 API 반영 행: **{trend_ok}/{trend_total}**."
    if skipped_non_product:
        note += (
            f" 지역·정보 검색형으로 보이는 연관어(예: 맛집·레시피·○○법 등) "
            f"**{skipped_non_product}건**은 상품 소싱 벤치마크에서 제외했습니다."
        )
    if rank < top_n and len(cat_pool) > 0:
        note += (
            f" (연관어 풀에서 제외 후 표시 가능한 키워드가 **{rank}개**뿐이라 상위 {top_n}개를 채우지 못했습니다.)"
        )
    if shop_msg != "ok":
        note = shop_msg + " | " + note

    return df, note

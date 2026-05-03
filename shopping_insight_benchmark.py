"""
쇼핑인사이트(데이터랩 웹)의 분야별 인기검색어 순위 + 광고 키워드도구·쇼핑 검색으로 지표 보강.

- 인기검색어 Top N: 데이터랩 프론트가 사용하는 비공개 JSON 엔드포인트를 호출합니다.
  (네이버 공식 Open API 문서의 datalab/shopping/* 과는 별개입니다.)
  변경·차단 가능성이 있으므로 실패 시 안내 메시지를 표시합니다.

공식 문서(트렌드·비율 지표): https://developers.naver.com/docs/serviceapi/datalab/shopping/shopping.md
웹 UI: https://datalab.naver.com/shoppingInsight/sCategory.naver
"""

from __future__ import annotations

import math
import time
from datetime import date as date_cls
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

from category_benchmark import (
    _norm_kw,
    dominant_category_from_shop,
    extract_keyword_metrics_row,
    find_keyword_row,
)

# 네이버 쇼핑 대분야(cat_id) — 데이터랩 분야 선택과 동일 체계 (키=대표 카테고리명)
ROOT_CATEGORY_CID: Dict[str, int] = {
    "패션의류": 50000000,
    "패션잡화": 50000001,
    "화장품/미용": 50000002,
    "디지털/가전": 50000003,
    "가구/인테리어": 50000004,
    "출산/육아": 50000005,
    "식품": 50000006,
    "스포츠/레저": 50000007,
    "생활/건강": 50000008,
    "여가/생활편의": 50000009,
    "면세점": 50000010,
    "도서": 50005542,
}

DATALAB_RANK_URL = "https://datalab.naver.com/shoppingInsight/getCategoryKeywordRank.naver"

DATALAB_POST_HEADERS = {
    "Referer": "https://datalab.naver.com/shoppingInsight/sCategory.naver",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}


def _category1_from_path(path: str) -> str:
    return str(path or "").split(">")[0].strip()


def compute_market_fit_score(
    qc: float,
    clk: float,
    ctr_pct: float,
    product_count: int,
) -> float:
    """수요·클릭·CTR 대비 상품수(경쟁) 휴리스틱 점수 0~100."""
    demand = math.log1p(max(0.0, float(qc)))
    conv = math.log1p(max(0.0, float(clk)))
    comp = max(1.0, math.log1p(max(0, int(product_count))))
    ctr_factor = 1.0 + max(0.0, float(ctr_pct)) / 100.0
    raw = (demand * ctr_factor * conv / comp) * 12.0
    return round(min(100.0, max(0.0, raw)), 4)


def _display_rows_to_db_payload(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows:
        kind = "SEED" if r.get("구분") == "주제어" else "INSIGHT"
        ir = r.get("인사이트 순위")
        insight_rank = None if ir == "—" or ir is None else int(ir)
        vs_q = r.get("검색수 비(대비 주제어)")
        vs_c = r.get("클릭수 비(대비 주제어)")
        out.append(
            {
                "row_kind": kind,
                "insight_rank": insight_rank,
                "keyword_text": str(r.get("키워드", "")),
                "mobile_monthly_qc": int(r.get("모바일 월 검색수", 0) or 0),
                "mobile_monthly_clk": float(r.get("모바일 월 클릭수", 0) or 0),
                "ctr_pct": float(r.get("CTR(%)", 0) or 0),
                "product_count": int(r.get("쇼핑 상품수(추정)", 0) or 0),
                "market_fit_score": float(r.get("시장 접목 점수", 0) or 0),
                "vs_seed_volume_ratio": float(vs_q) if vs_q is not None else None,
                "vs_seed_click_ratio": float(vs_c) if vs_c is not None else None,
            }
        )
    return out


def resolve_root_cid(category_path: str) -> Tuple[int, str]:
    """
    쇼핑 검색으로 얻은 카테고리 경로의 첫 번째(대분야) 명칭을 데이터랩 분야 코드로 매핑.
    세부(leaf) cat_id 가 없으면 대분야 단계만 사용 (웹에서 선택하는 세부 분야와 완전 동일하지 않을 수 있음).
    """
    c1 = _category1_from_path(category_path)
    if not c1:
        return 50000008, "생활/건강(기본)"

    if c1 in ROOT_CATEGORY_CID:
        return ROOT_CATEGORY_CID[c1], c1

    compact = c1.replace(" ", "")
    for name, cid in ROOT_CATEGORY_CID.items():
        if compact == name.replace(" ", "") or name in c1 or c1 in name:
            return cid, name

    return 50000008, "생활/건강(대분야 불명—기본값)"


def fetch_datalab_category_keyword_ranks(
    cid: int,
    start_date: str,
    end_date: str,
    *,
    limit: int = 20,
    time_unit: str = "date",
    device: str = "",
    gender: str = "",
    age: str = "",
) -> Tuple[List[Tuple[int, str]], str]:
    """
    데이터랩 쇼핑인사이트 분야별 인기 검색어 순위.
    한 페이지 최대 20건 → Top20은 page=1 한 번이면 충분.
    """
    limit = max(1, min(500, int(limit)))
    page_size = min(20, limit)
    page = 1
    out: List[Tuple[int, str]] = []
    base_rank = 0

    while len(out) < limit:
        body = (
            f"cid={cid}&timeUnit={time_unit}&startDate={start_date}&endDate={end_date}"
            f"&age={age}&gender={gender}&device={device}"
            f"&page={page}&count={page_size}"
        )
        try:
            res = requests.post(
                DATALAB_RANK_URL,
                headers=DATALAB_POST_HEADERS,
                data=body.encode("utf-8"),
                timeout=25,
            )
        except Exception as e:
            return [], f"데이터랩 순위 요청 오류: {e}"

        if res.status_code != 200:
            return [], f"데이터랩 순위 API HTTP {res.status_code} (비공식 엔드포인트 변경 가능)"

        ct = (res.headers.get("Content-Type") or "").lower()
        if "html" in ct or res.text.lstrip().startswith("<!"):
            return [], (
                "데이터랩 순위 API가 HTML 페이지를 반환했습니다. "
                "(네트워크/IP 제한·로그인 필요 등) 로컬 PC 브라우저 환경에서 다시 시도해 주세요."
            )

        try:
            payload = res.json()
        except Exception:
            return [], "데이터랩 응답 JSON 파싱 실패"

        ranks = payload.get("ranks")
        if not isinstance(ranks, list) or not ranks:
            break

        for block in ranks:
            if len(out) >= limit:
                break
            base_rank += 1
            kw = ""
            if isinstance(block, dict):
                kw = str(block.get("keyword") or block.get("query") or "").strip()
            elif isinstance(block, str):
                kw = block.strip()
            if kw:
                out.append((base_rank, kw))

        if len(ranks) < page_size:
            break
        page += 1
        if page > 25:
            break
        time.sleep(0.35)

    if not out:
        return [], "인기 검색어 목록이 비었습니다. 기간·분야 코드를 확인해주세요."

    return out, "ok"


def _keyword_row_from_ads(ads_api: Any, keyword: str) -> Optional[Dict[str, Any]]:
    hint = str(keyword or "").replace(" ", "")
    if not hint:
        return None
    raw = ads_api.get_related_keywords(hint) or []
    row = find_keyword_row(raw, keyword)
    if row is None:
        row = find_keyword_row(raw, hint)
    if row is None and raw:
        # 힌트와 동일 한글 표기가 리스트에 없을 때 첫 행 사용 (보조)
        row = raw[0]
    return row


def run_shopping_insight_benchmark(
    tool: Any,
    seed_keyword: str,
    *,
    start_date: str,
    end_date: str,
    top_n: int = 20,
    ads_sleep_sec: float = 0.04,
    product_sleep_sec: float = 0.03,
    persist_to_db: bool = False,
) -> Tuple[pd.DataFrame, str]:
    seed = str(seed_keyword or "").strip().split(",")[0].strip()
    if not seed:
        return pd.DataFrame(), "주제어가 비어 있습니다."

    cfg = getattr(tool, "config", {}) or {}
    naver_open = cfg.get("naver_open_api") or {}
    cid_o = str(naver_open.get("client_id", "") or "").strip()
    csec = str(naver_open.get("client_secret", "") or "").strip()
    if not cid_o or not csec:
        return pd.DataFrame(), "네이버 Open API(client_id/secret) 설정이 필요합니다."

    cat_path, shop_msg = dominant_category_from_shop(seed, client_id=cid_o, client_secret=csec)
    if not cat_path:
        return pd.DataFrame(), shop_msg

    root_cid, cid_label = resolve_root_cid(cat_path)

    ranks, rank_msg = fetch_datalab_category_keyword_ranks(
        root_cid,
        start_date,
        end_date,
        limit=top_n,
        time_unit="date",
    )
    if not ranks:
        return pd.DataFrame(), rank_msg

    ads_api = getattr(tool, "ads_api", None)
    if ads_api is None:
        return pd.DataFrame(), "광고 API(키워드도구)를 사용할 수 없습니다."

    def product_total(kw: str) -> int:
        try:
            total, _ = tool.get_product_info(kw)
            return int(total or 0)
        except Exception:
            return 0

    def metrics_for(kw: str) -> Dict[str, Any]:
        row = _keyword_row_from_ads(ads_api, kw)
        if row is None:
            return {
                "모바일 월 검색수": 0,
                "모바일 월 클릭수": 0,
                "CTR(%)": 0.0,
                "source": "ads_miss",
            }
        m = extract_keyword_metrics_row(row)
        return {
            "모바일 월 검색수": int(round(m["monthly_mobile_qc"])),
            "모바일 월 클릭수": int(round(m["monthly_mobile_clk"])),
            "CTR(%)": round(m["ctr_pct"], 4),
            "source": "ads",
        }

    seed_m = metrics_for(seed)
    time.sleep(ads_sleep_sec)
    seed_pc = product_total(seed)
    time.sleep(product_sleep_sec)

    seed_qc = float(seed_m["모바일 월 검색수"])
    seed_clk = float(seed_m["모바일 월 클릭수"])

    rows: List[Dict[str, Any]] = [
        {
            "구분": "주제어",
            "인사이트 순위": "—",
            "키워드": seed,
            "모바일 월 검색수": seed_m["모바일 월 검색수"],
            "모바일 월 클릭수": seed_m["모바일 월 클릭수"],
            "CTR(%)": seed_m["CTR(%)"],
            "쇼핑 상품수(추정)": seed_pc,
            "검색수 비(대비 주제어)": 1.0,
            "클릭수 비(대비 주제어)": (1.0 if seed_clk > 0 else None),
            "시장 접목 점수": compute_market_fit_score(
                seed_qc, seed_clk, float(seed_m["CTR(%)"]), seed_pc
            ),
        }
    ]

    for insight_rank, kw in ranks:
        if _norm_kw(kw) == _norm_kw(seed):
            continue
        m = metrics_for(kw)
        time.sleep(ads_sleep_sec)
        pc = product_total(kw)
        time.sleep(product_sleep_sec)
        qc = float(m["모바일 월 검색수"])
        clk = float(m["모바일 월 클릭수"])
        rq = (qc / seed_qc) if seed_qc > 0 else None
        rc = (clk / seed_clk) if seed_clk > 0 else None
        rows.append(
            {
                "구분": "인사이트 Top",
                "인사이트 순위": insight_rank,
                "키워드": kw,
                "모바일 월 검색수": m["모바일 월 검색수"],
                "모바일 월 클릭수": m["모바일 월 클릭수"],
                "CTR(%)": m["CTR(%)"],
                "쇼핑 상품수(추정)": pc,
                "검색수 비(대비 주제어)": (round(rq, 4) if rq is not None else None),
                "클릭수 비(대비 주제어)": (round(rc, 4) if rc is not None else None),
                "시장 접목 점수": compute_market_fit_score(
                    qc, clk, float(m["CTR(%)"]), pc
                ),
            }
        )

    note = (
        f"**데이터랩 인사이트(비공식 JSON)** 분야 코드 `{root_cid}` ({cid_label}), "
        f"기간 `{start_date}` ~ `{end_date}` 기준 인기 검색어 상위 **{len(ranks)}건** 중 주제어와 중복 제외 후 표시. "
        f"쇼핑 노출 다수결 카테고리 경로: `{cat_path}`. "
        "대분야 `cid`만 사용하므로 웹에서 선택한 **세부 분야**와 순위가 다를 수 있습니다. "
        "모바일 검색·클릭·CTR은 **광고 키워드도구**, 상품수는 **쇼핑 검색 total** 입니다. "
        "[쇼핑인사이트 웹](https://datalab.naver.com/shoppingInsight/sCategory.naver), "
        "[공식 DataLab API 안내](https://developers.naver.com/docs/serviceapi/datalab/shopping/shopping.md)."
    )
    if shop_msg != "ok":
        note = f"{shop_msg}\n\n{note}"

    if persist_to_db:
        try:
            from db import create_insight_discovery_run, insert_insight_discovery_keywords, is_dsn_configured

            if not is_dsn_configured():
                note += "\n\n⚠️ MariaDB 접속 환경변수가 없어 인사이트 결과를 저장하지 않았습니다."
            else:
                ds = date_cls.fromisoformat(str(start_date)[:10])
                de = date_cls.fromisoformat(str(end_date)[:10])
                run = create_insight_discovery_run(
                    seed_keyword=seed,
                    shopping_category_path=cat_path,
                    datalab_category_id=int(root_cid),
                    period_start=ds,
                    period_end=de,
                    status="SUCCESS",
                    note="",
                )
                db_rows = _display_rows_to_db_payload(rows)
                n_ins = insert_insight_discovery_keywords(run["id"], db_rows)
                note += (
                    f"\n\n✅ MariaDB 저장 완료: run_id **{run['id']}**, 키워드 행 **{n_ins}**건 "
                    f"(token `{run['run_token']}`)."
                )
        except Exception as e:
            note += f"\n\n⚠️ MariaDB 저장 실패: {e}"

    return pd.DataFrame(rows), note

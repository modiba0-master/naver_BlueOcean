"""
추천 키워드 엔진 — BlueOceanTool 상단 단계(연관 확장·모바일 단일 지표·스코어·쿠팡 검증).
STEP3 이후 상위 TOP_N만 쿠팡, asyncio+Semaphore는 별도 스레드에서 실행(Streamlit 메인 루프 충돌 방지).
"""
from __future__ import annotations

import asyncio
import csv
import math
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from keyword_normalize import dedupe_items_by_keyword, is_brand_or_noise, normalize_keyword
from revenue_keyword_discovery import (
    build_discovery_context,
    category_matches_vertical,
    parse_discovery_from_ui_settings,
)
from revenue_keyword_settings import load_recommend_settings, settings_to_engine_kwargs


def _clean_metric(v: Any) -> float:
    """광고 키워드도구 누락·`<` 표기 등 BlueOceanTool.start_analysis와 유사 처리."""
    if isinstance(v, str) and "<" in v:
        return 5.0
    try:
        return float(v) if v is not None and str(v).strip() != "" else 0.0
    except Exception:
        return 0.0


def _ctr_mobile_pct(item: Dict[str, Any]) -> Tuple[float, float, float]:
    """모바일만 사용: 검색수·클릭수·CTR(%). PC 필드는 참조하지 않음."""
    mo_qc = _clean_metric(item.get("monthlyMobileQcCnt"))
    mo_clk = _clean_metric(item.get("monthlyAveMobileClkCnt"))
    ctr = (mo_clk / mo_qc * 100.0) if mo_qc > 0 else 0.0
    return mo_qc, mo_clk, ctr


def _demand_norm(vol: float) -> float:
    return max(0.0, min(100.0, math.log1p(max(0.0, vol)) / math.log1p(100_000) * 100.0))


def _competition_norm(product_count: int) -> float:
    pc = max(0, int(product_count))
    inv = max(0.0, 100.0 - min(100.0, math.log1p(pc) / math.log1p(500_000) * 100.0))
    return max(0.0, min(100.0, inv))


def _ctr_norm(ctr_pct: float) -> float:
    if ctr_pct <= 1.0:
        return 0.0
    if ctr_pct >= 15.0:
        return 100.0
    return max(0.0, min(100.0, (ctr_pct - 1.0) / (15.0 - 1.0) * 100.0))


def _trend_norm_from_growth_factor(gf: float) -> float:
    gf = max(0.7, min(1.8, float(gf)))
    return max(0.0, min(100.0, (gf - 0.7) / 1.1 * 100.0))


def _expand_related_pool(tool: Any, seeds: List[str], target: int = 10_000) -> List[Dict[str, Any]]:
    pool: Dict[str, Dict[str, Any]] = {}
    q: deque[str] = deque()
    for s in seeds:
        t = str(s).strip()
        if t:
            q.append(t)
    seen_seed: set[str] = set()
    iters = 0
    max_iters = 50
    while len(pool) < target and q and iters < max_iters:
        iters += 1
        seed = q.popleft()
        sk = normalize_keyword(seed)
        if sk in seen_seed:
            continue
        seen_seed.add(sk)
        rows = tool.ads_api.get_related_keywords(seed.replace(" ", "")) or []
        for it in rows:
            kw = str(it.get("relKeyword") or "").strip()
            if not kw or is_brand_or_noise(kw):
                continue
            nk = normalize_keyword(kw)
            if nk in pool:
                continue
            pool[nk] = it
            if len(pool) >= target:
                break
        if len(pool) < target and rows:
            ranked = sorted(rows, key=lambda x: _clean_metric(x.get("monthlyMobileQcCnt")), reverse=True)
            for it in ranked[:20]:
                kw = str(it.get("relKeyword") or "").strip()
                nk = normalize_keyword(kw)
                if nk and nk not in seen_seed:
                    q.append(kw)
    out = list(pool.values())[:target]
    return dedupe_items_by_keyword(out, "relKeyword")


def _validate_topk(
    tool: Any,
    items: List[Dict[str, Any]],
    k: int,
    min_rev: int,
    min_avg_price: float,
) -> bool:
    if not items:
        return False
    top = items[: max(1, k)]
    rev_ok = False
    for it in top:
        rv = tool._parse_number_from_text(
            it.get("review_count") or it.get("reviewCount") or it.get("review")
        )
        if rv is not None and float(rv) >= float(min_rev):
            rev_ok = True
            break
    if not rev_ok:
        return False
    prices = tool._extract_price_values(top)
    if not prices:
        return False
    return (sum(prices) / len(prices)) >= float(min_avg_price)


def _sales_power_from_crawl(tool: Any, data: Dict[str, Any]) -> float:
    items = data.get("top10_items") or []
    review_values = tool._extract_review_values(items)
    price_values = tool._extract_price_values(items)
    avg_reviews = float(data.get("avg_reviews") or 0.0)
    avg_price = float(data.get("avg_price") or 0.0)
    profile: Dict[str, Any] = {
        "avg_reviews": avg_reviews,
        "avg_price": avg_price,
        "review_growth_proxy": 0.0,
        "review_distribution": 0.0,
        "price_stability": 0.0,
    }
    if review_values:
        nrv = len(review_values)
        head_third = max(1, nrv // 3)
        avg_top2 = sum(review_values[:2]) / min(2, nrv)
        avg_third = sum(review_values[:head_third]) / head_third
        recent_avg = max(avg_top2, avg_third)
        total_avg = sum(review_values) / nrv
        denom_l = math.log1p(max(1e-12, total_avg))
        numer_l = math.log1p(max(0.0, recent_avg))
        review_growth_proxy = (numer_l / denom_l) if denom_l > 1e-15 else 0.0
        total_sum = sum(review_values)
        if total_sum > 1e-12:
            rv_sorted = sorted(review_values, reverse=True)
            top2_sum = sum(rv_sorted[: min(2, len(rv_sorted))])
            top_share = top2_sum / total_sum
            review_distribution = 1.0 - min(1.0, top_share)
        else:
            review_distribution = 0.0
        profile["review_growth_proxy"] = max(0.0, min(2.0, review_growth_proxy))
        profile["review_distribution"] = max(0.0, min(1.0, review_distribution))
    if len(price_values) >= 2:
        p_avg = sum(price_values) / len(price_values)
        variance = sum((p - p_avg) ** 2 for p in price_values) / len(price_values)
        stdev = math.sqrt(max(0.0, variance))
        cv = stdev / p_avg if p_avg > 0 else 1.0
        profile["price_stability"] = max(0.0, min(1.0, 1.0 - cv))
    return float(tool._compute_sales_power(profile))


def _coupang_eval_one_safe(
    tool: Any,
    row: Dict[str, Any],
    *,
    topk: int,
    min_rev: int,
    min_avg_price: float,
    log: Callable[[str], None],
    persist_coupang_snapshot: bool = True,
) -> Optional[Dict[str, Any]]:
    kw = str(row["keyword_text"])
    try:
        data = tool.coupang_crawler.crawl_coupang(kw)
        if persist_coupang_snapshot:
            try:
                from db import (
                    build_recommend_engine_coupang_snapshot_payload,
                    insert_coupang_search_snapshot,
                    is_dsn_configured,
                )

                if is_dsn_configured():
                    pl = build_recommend_engine_coupang_snapshot_payload(kw, data)
                    if pl is not None:
                        insert_coupang_search_snapshot(pl)
            except Exception as ex:
                try:
                    log(f"[WARN] coupang_search snapshot failed: {kw[:80]} ({type(ex).__name__}: {ex})")
                    log("[INFO] continue")
                except Exception:
                    pass
        items = data.get("top10_items") or []
        if not _validate_topk(tool, items, topk, min_rev, min_avg_price):
            return None
        sp = _sales_power_from_crawl(tool, data)
        out = dict(row)
        out["sales_power"] = max(0.0, min(100.0, float(sp)))
        return out
    except Exception as e:
        try:
            log(f"[WARN] keyword failed: {kw} ({type(e).__name__}: {e})")
            log("[INFO] continue")
        except Exception:
            pass
        return None


class RecommendedKeywordEngine:
    def __init__(self, tool: Any):
        self.tool = tool
        self._product_pair_cache: Dict[str, Tuple[int, str]] = {}
        self._trend_cache: Dict[str, Tuple[float, str]] = {}

    def _product_count_and_path(self, keyword: str) -> Tuple[int, str]:
        nk = normalize_keyword(keyword)
        if nk in self._product_pair_cache:
            return self._product_pair_cache[nk]
        total, path = self.tool.get_product_info(keyword)
        self._product_pair_cache[nk] = (int(total or 0), str(path or ""))
        return self._product_pair_cache[nk]

    def _trend_and_season(self, keyword: str, start: str, end: str) -> Tuple[float, str]:
        nk = normalize_keyword(keyword)
        key = f"{nk}|{start}|{end}"
        if key in self._trend_cache:
            return self._trend_cache[key]
        m = self.tool.get_monthly_trends(keyword, start, end)
        if not m:
            self._trend_cache[key] = (50.0, "steady")
            return self._trend_cache[key]
        periods = sorted(m.keys())
        vals = [float(m[p]) for p in periods if m.get(p) is not None]
        gf = self.tool._compute_growth_factor(vals)
        tn = _trend_norm_from_growth_factor(gf)
        season = self.tool.detect_seasonality(vals)
        self._trend_cache[key] = (float(tn), str(season or "steady"))
        return self._trend_cache[key]

    def _coupang_async_block(
        self,
        rows: List[Dict[str, Any]],
        *,
        semaphore_n: int,
        topk: int,
        min_rev: int,
        min_avg_price: float,
        log: Callable[[str], None],
        persist_coupang_snapshot: bool = True,
    ) -> List[Optional[Dict[str, Any]]]:
        """메인 스트림릿 스레드가 아닌 워커 스레드에서만 호출 (asyncio.run 안전)."""

        async def _async_coupang() -> List[Optional[Dict[str, Any]]]:
            sem = asyncio.Semaphore(max(1, int(semaphore_n)))

            async def one(r: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                async with sem:
                    return await asyncio.to_thread(
                        _coupang_eval_one_safe,
                        self.tool,
                        r,
                        topk=int(topk),
                        min_rev=int(min_rev),
                        min_avg_price=float(min_avg_price),
                        log=log,
                        persist_coupang_snapshot=bool(persist_coupang_snapshot),
                    )

            return await asyncio.gather(*[one(r) for r in rows])

        return asyncio.run(_async_coupang())

    def run(
        self,
        seeds_csv: str,
        *,
        target_expand: int = 10_000,
        max_products: int = 50_000,
        top_output: int = 20,
        topk_validate: int = 3,
        min_review_topk: int = 100,
        min_avg_price_topk: float = 8000.0,
        trend_days: int = 120,
        persist_db: bool = True,
        progress: Optional[Any] = None,
        ui_settings: Optional[Dict[str, Any]] = None,
        # 아래는 settings_to_engine_kwargs로 덮어쓸 수 있음(직접 호출 시 하위 호환)
        min_volume: Optional[int] = None,
        min_ctr_pct: Optional[float] = None,
        top_after_score: Optional[int] = None,
        coupang_semaphore: Optional[int] = None,
        score_weights: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        def log(msg: str) -> None:
            if progress:
                try:
                    progress(msg)
                except Exception:
                    pass

        d_vert, d_tier = parse_discovery_from_ui_settings(ui_settings)
        disc_preset, seeds_extra, match_spec, disc_logs = build_discovery_context(d_vert, d_tier)
        for line in disc_logs:
            log(line)

        cfg, cfg_logs = load_recommend_settings(ui_settings, discovery_preset=disc_preset)
        for line in cfg_logs:
            log(line)
        ek = settings_to_engine_kwargs(cfg)
        if min_volume is not None:
            ek["min_volume"] = int(min_volume)
        if min_ctr_pct is not None:
            ek["min_ctr_pct"] = float(min_ctr_pct)
        if top_after_score is not None:
            ek["top_after_score"] = int(top_after_score)
        if coupang_semaphore is not None:
            ek["coupang_semaphore"] = int(coupang_semaphore)
        if score_weights is not None:
            ek["score_weights"] = dict(score_weights)

        w = ek.get("score_weights") or {"demand": 0.25, "competition": 0.25, "ctr": 0.25, "trend": 0.25}
        wd = float(w.get("demand", 0.25))
        wc = float(w.get("competition", 0.25))
        wctr = float(w.get("ctr", 0.25))
        wt = float(w.get("trend", 0.25))
        ws = wd + wc + wctr + wt
        if ws <= 0:
            wd = wc = wctr = wt = 0.25
        else:
            wd, wc, wctr, wt = wd / ws, wc / ws, wctr / ws, wt / ws

        min_vol = int(ek["min_volume"])
        min_ctr = float(ek["min_ctr_pct"])
        top_n_coupang = int(ek["top_after_score"])
        sem_n = int(ek["coupang_semaphore"])
        max_products = int(ek.get("max_products") or max_products)
        max_vol_opt = ek.get("max_volume")
        topk_val = int(ek.get("topk_validate") or topk_validate)
        min_rev_k = int(ek.get("min_review_topk") or min_review_topk)
        min_price_k = float(ek.get("min_avg_price_topk") or min_avg_price_topk)

        seeds = [s.strip() for s in str(seeds_csv or "").split(",") if s.strip()]
        combined_seeds: List[str] = []
        seen_m: set[str] = set()
        for s in (seeds_extra or []) + seeds:
            t = str(s).strip()
            if not t:
                continue
            nk = normalize_keyword(t)
            if nk in seen_m:
                continue
            seen_m.add(nk)
            combined_seeds.append(t)
        seeds = combined_seeds
        if not seeds:
            return {"ok": False, "error": "시드 키워드가 비어 있습니다.", "top": []}

        batch_token = uuid.uuid4().hex
        pcfg: Dict[str, Any] = {}
        if ui_settings and isinstance(ui_settings.get("precoupang"), dict):
            pcfg = dict(ui_settings["precoupang"])
        save_precoup_db = bool(pcfg.get("save_db", True))
        save_precoup_csv = bool(pcfg.get("save_csv", False))
        try:
            precoup_max = max(1, min(50_000, int(pcfg.get("max_rows", 2000))))
        except (TypeError, ValueError):
            precoup_max = 2000
        persist_snap = bool(pcfg.get("persist_coupang_snapshot", True))
        skip_coupang = bool((ui_settings or {}).get("skip_coupang")) or bool(pcfg.get("skip_coupang"))

        log(f"[Recommend] STEP1 expand target={target_expand}")
        raw_items = _expand_related_pool(self.tool, seeds, target_expand)
        log(f"[Recommend] {len(raw_items)} generated")

        end_d = date.today()
        start_d = end_d - timedelta(days=int(trend_days))
        start_s = start_d.strftime("%Y-%m-%d")
        end_s = end_d.strftime("%Y-%m-%d")

        step2: List[Dict[str, Any]] = []
        for it in raw_items:
            mo_qc, _mo_clk, ctr = _ctr_mobile_pct(it)
            if mo_qc < float(min_vol):
                continue
            if ctr < float(min_ctr):
                continue
            if max_vol_opt is not None:
                try:
                    if mo_qc > float(max_vol_opt):
                        continue
                except (TypeError, ValueError):
                    pass
            kw = str(it.get("relKeyword") or "").strip()
            if not kw:
                continue
            step2.append({"keyword_text": kw, "item": it, "mo_qc": mo_qc, "ctr_pct": ctr})

        filtered: List[Dict[str, Any]] = []
        max_workers = min(24, max(4, len(step2) // 50 + 4))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(self._product_count_and_path, r["keyword_text"]): r for r in step2}
            for fu in as_completed(futs):
                r = futs[fu]
                try:
                    pc, cat_path = fu.result()
                except Exception:
                    pc, cat_path = 0, ""
                if pc <= 0 or pc > int(max_products):
                    continue
                if match_spec is not None and not category_matches_vertical(cat_path, match_spec):
                    continue
                r["product_count"] = int(pc)
                filtered.append(r)

        log(f"[Recommend] {len(filtered)} filtered")

        scored: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            tfuts = {
                ex.submit(self._trend_and_season, r["keyword_text"], start_s, end_s): r
                for r in filtered
            }
            for fu in as_completed(tfuts):
                r = tfuts[fu]
                try:
                    tnorm, season_type = fu.result()
                except Exception:
                    tnorm, season_type = 50.0, "steady"
                mo_qc = float(r["mo_qc"])
                ctr = float(r["ctr_pct"])
                pc = int(r["product_count"])
                kw = str(r["keyword_text"])
                d_norm = _demand_norm(mo_qc)
                c_norm = _competition_norm(pc)
                ctr_n = _ctr_norm(ctr)
                kw_score = wd * d_norm + wc * c_norm + wctr * ctr_n + wt * float(tnorm)
                kw_score = max(0.0, min(100.0, float(kw_score)))
                intent = self.tool.classify_keyword_intent(kw)
                scored.append(
                    {
                        "keyword_text": kw,
                        "keyword": kw,
                        "metric_basis": "mobile",
                        "monthly_search_volume": int(mo_qc),
                        "product_count": pc,
                        "ctr_pct": round(ctr, 4),
                        "demand_score": round(d_norm, 2),
                        "competition_component": round(c_norm, 2),
                        "ctr_component": round(ctr_n, 2),
                        "trend_score": round(float(tnorm), 2),
                        "keyword_score": round(kw_score, 2),
                        "intent": intent,
                        "season_type": str(season_type or "steady"),
                    }
                )

        scored.sort(key=lambda x: float(x["keyword_score"]), reverse=True)

        precoup_saved = 0
        precoup_csv_path = ""
        precoup_skip_reason: Optional[str] = None
        precoup_error: Optional[str] = None
        cand_cap = min(len(scored), precoup_max)
        cand_rows: List[Dict[str, Any]] = []
        for i, row in enumerate(scored[:cand_cap], start=1):
            ks = float(row.get("keyword_score") or 0.0)
            cand_rows.append(
                {
                    "rank_position": i,
                    "keyword_text": row["keyword_text"],
                    "keyword": row.get("keyword") or row["keyword_text"],
                    "metric_basis": row.get("metric_basis") or "mobile",
                    "monthly_search_volume": int(row.get("monthly_search_volume") or 0),
                    "product_count": int(row.get("product_count") or 0),
                    "ctr_pct": float(row.get("ctr_pct") or 0.0),
                    "demand_score": float(row.get("demand_score") or 0.0),
                    "competition_component": float(row.get("competition_component") or 0.0),
                    "ctr_component": float(row.get("ctr_component") or 0.0),
                    "trend_score": float(row.get("trend_score") or 0.0),
                    "keyword_score": float(row.get("keyword_score") or 0.0),
                    "intent": str(row.get("intent") or ""),
                    "season_type": str(row.get("season_type") or "steady"),
                    "reason_text": (
                        f"모바일 검색 {int(row.get('monthly_search_volume', 0)):,} · "
                        f"상품수 {int(row.get('product_count', 0)):,} · CTR {float(row.get('ctr_pct', 0)):.2f}% · "
                        f"키워드점수 {ks:.1f} (쿠팡 전)"
                    ),
                    "extra_json": {
                        "pipeline": "recommended_keyword_precoup",
                        "discovery_vertical": d_vert,
                        "discovery_tier": d_tier,
                        "batch_token": batch_token,
                    },
                }
            )
        from db import is_dsn_configured

        _dsn = bool(is_dsn_configured())
        _dbe = bool(getattr(self.tool, "db_enabled", False))
        log(
            f"[Recommend] precoup built={len(cand_rows)} cap={cand_cap} "
            f"save_db={save_precoup_db} persist_db={persist_db} "
            f"tool.db_enabled={_dbe} dsn_configured={_dsn}"
        )
        # 후보 테이블은 분석 실행 DB 플래그와 분리: DSN만 있으면 적재 시도(스키마 005 필요)
        if not save_precoup_db:
            precoup_skip_reason = "precoupang.save_db=false"
        elif not persist_db:
            precoup_skip_reason = "persist_db=false"
        elif not cand_rows:
            precoup_skip_reason = "no_scored_rows"
        elif not _dsn:
            precoup_skip_reason = "dsn_not_configured"
        else:
            try:
                from db import insert_recommended_keyword_candidates

                precoup_saved = insert_recommended_keyword_candidates(
                    batch_token, str(seeds_csv or ""), cand_rows
                )
                log(
                    f"[DB] recommended_keyword_candidates 저장 {precoup_saved}행 "
                    f"batch={batch_token}"
                )
                if precoup_saved <= 0 and cand_rows:
                    precoup_skip_reason = "insert_returned_zero"
            except Exception as e:
                precoup_error = f"{type(e).__name__}: {e}"
                precoup_skip_reason = "insert_failed"
                log(f"[DB] recommended_keyword_candidates 저장 실패: {precoup_error}")
                log("[INFO] 테이블 없음이면 sql/005 적용 후 ensure_schema(또는 앱 재시작)를 확인하세요.")
        if save_precoup_csv and cand_rows:
            try:
                rep = Path(__file__).resolve().parent / "reports"
                rep.mkdir(parents=True, exist_ok=True)
                precoup_csv_path = str(rep / f"recommended_precoup_{batch_token}.csv")
                fieldnames = list(cand_rows[0].keys())
                fieldnames = [f for f in fieldnames if f != "extra_json"]
                with open(precoup_csv_path, "w", encoding="utf-8-sig", newline="") as fp:
                    w = csv.DictWriter(fp, fieldnames=fieldnames, extrasaction="ignore")
                    w.writeheader()
                    for cr in cand_rows:
                        w.writerow({k: cr.get(k, "") for k in fieldnames})
                log(f"[CSV] precoup 저장 {precoup_csv_path}")
            except Exception as e:
                log(f"[CSV] precoup 저장 실패(무시): {e}")
                precoup_csv_path = ""

        top_for_coupang = scored[: max(1, int(top_n_coupang))]
        coupang_rows: List[Optional[Dict[str, Any]]]
        merged: List[Dict[str, Any]] = []
        if skip_coupang:
            log("[Recommend] skip_coupang=True — 쿠팡 검증·판매력 생략")
            log(f"[Recommend] {len(top_for_coupang)} Coupang 후보 (스킵됨)")
            coupang_rows = []
            take_n = max(1, int(top_output))
            for r in scored[:take_n]:
                ks = float(r["keyword_score"])
                fin = 0.5 * ks + 0.5 * 0.0
                fin = max(0.0, min(100.0, fin))
                reason = (
                    f"(쿠팡 생략) 모바일 검색 {int(r.get('monthly_search_volume', 0)):,} · "
                    f"상품수 {int(r.get('product_count', 0)):,} · CTR {float(r.get('ctr_pct', 0)):.2f}% · "
                    f"키워드점수 {ks:.1f} · 판매력 0.0 → 최종 {fin:.1f}"
                )
                merged.append(
                    {
                        **r,
                        "sales_power": 0.0,
                        "final_score": round(fin, 2),
                        "reason_text": reason,
                    }
                )
        else:
            log(f"[Recommend] {len(top_for_coupang)} selected for Coupang")
            with ThreadPoolExecutor(max_workers=1) as pool:
                coupang_rows = pool.submit(
                    self._coupang_async_block,
                    top_for_coupang,
                    semaphore_n=sem_n,
                    topk=int(topk_val),
                    min_rev=int(min_rev_k),
                    min_avg_price=float(min_price_k),
                    log=log,
                    persist_coupang_snapshot=persist_snap,
                ).result()

            for r in coupang_rows:
                if r is None:
                    continue
                ks = float(r["keyword_score"])
                sp = float(r["sales_power"])
                fin = 0.5 * ks + 0.5 * sp
                fin = max(0.0, min(100.0, fin))
                reason = (
                    f"모바일 검색 {int(r.get('monthly_search_volume', 0)):,} · "
                    f"상품수 {int(r.get('product_count', 0)):,} · CTR {float(r.get('ctr_pct', 0)):.2f}% · "
                    f"키워드점수 {ks:.1f} · 판매력 {sp:.1f}"
                )
                merged.append(
                    {
                        **r,
                        "sales_power": round(sp, 2),
                        "final_score": round(fin, 2),
                        "reason_text": reason,
                    }
                )

        merged.sort(key=lambda x: float(x["final_score"]), reverse=True)
        top_final = merged[: max(1, int(top_output))]

        out_rows: List[Dict[str, Any]] = []
        for i, row in enumerate(top_final, start=1):
            out_rows.append(
                {
                    "rank_position": i,
                    "keyword_text": row["keyword_text"],
                    "keyword": row.get("keyword") or row["keyword_text"],
                    "metric_basis": row.get("metric_basis") or "mobile",
                    "monthly_search_volume": int(row.get("monthly_search_volume") or 0),
                    "product_count": int(row.get("product_count") or 0),
                    "ctr_pct": float(row.get("ctr_pct") or 0.0),
                    "demand_score": float(row.get("demand_score") or 0.0),
                    "competition_component": float(row.get("competition_component") or 0.0),
                    "ctr_component": float(row.get("ctr_component") or 0.0),
                    "trend_score": float(row.get("trend_score") or 0.0),
                    "keyword_score": float(row.get("keyword_score") or 0.0),
                    "sales_power": float(row.get("sales_power") or 0.0),
                    "final_score": float(row.get("final_score") or 0.0),
                    "intent": str(row.get("intent") or ""),
                    "season_type": str(row.get("season_type") or "steady"),
                    "reason_text": str(row.get("reason_text") or ""),
                    "extra_json": {
                        "pipeline": "recommended_keyword_engine_v1",
                        "discovery_vertical": d_vert,
                        "discovery_tier": d_tier,
                        "skip_coupang": bool(skip_coupang),
                        "precoup_candidates_saved": int(precoup_saved),
                    },
                }
            )

        saved = 0
        if persist_db and bool(getattr(self.tool, "db_enabled", False)):
            try:
                from db import insert_recommended_keywords, is_dsn_configured

                if is_dsn_configured():
                    saved = insert_recommended_keywords(batch_token, seeds_csv, out_rows)
                    log(f"[DB] recommended_keywords 저장 {saved}행 batch={batch_token}")
            except Exception as e:
                log(f"[DB] 저장 실패(무시): {e}")

        log("[Recommend] completed")
        return {
            "ok": True,
            "batch_token": batch_token,
            "meta": {
                "expanded": len(raw_items),
                "after_hard_filter": len(filtered),
                "scored": len(scored),
                "coupang_candidates": len(top_for_coupang),
                "after_coupang": len(merged),
                "top_n": len(out_rows),
                "discovery_vertical": d_vert,
                "discovery_tier": d_tier,
                "precoup_candidates_saved": int(precoup_saved),
                "precoup_candidate_count": int(len(cand_rows)),
                "precoup_skip_reason": precoup_skip_reason,
                "precoup_error": precoup_error,
                "precoup_csv_path": precoup_csv_path or None,
                "skip_coupang": bool(skip_coupang),
                "persist_coupang_snapshot": bool(persist_snap),
            },
            "top": [
                {
                    "keyword": r["keyword_text"],
                    "score": r["final_score"],
                    "keyword_score": r["keyword_score"],
                    "sales_power": r["sales_power"],
                    "competition_norm": r["competition_component"],
                    "product_count": r["product_count"],
                    "intent": r.get("intent"),
                    "season_type": r.get("season_type"),
                    "reason": r["reason_text"],
                }
                for r in out_rows
            ],
            "rows": out_rows,
        }


def run_recommended_engine(tool: Any, seeds_csv: str, **kwargs: Any) -> Dict[str, Any]:
    return RecommendedKeywordEngine(tool).run(seeds_csv, **kwargs)

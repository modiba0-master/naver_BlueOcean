#!/usr/bin/env python3
"""
CLI: 추천 키워드 엔진 실행 (Streamlit 이벤트 루프와 분리).
예: python run_recommend.py --seeds "차량용,수납" --limit 20
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict

from blue_ocean_tool import BlueOceanTool, apply_admin_env_from_config, apply_database_env_from_config


def main() -> int:
    p = argparse.ArgumentParser(description="추천 키워드 엔진 (BlueOceanTool)")
    p.add_argument("--seeds", type=str, required=True, help="쉼표 구분 시드 키워드")
    p.add_argument("--limit", type=int, default=20, help="TOP 출력 개수 (기본 20)")
    p.add_argument("--top-n", dest="top_n", type=int, default=None, help="쿠팡 분석 상위 개수 (ENV TOP_N보다 후순위)")
    p.add_argument("--sem", type=int, default=None, help="쿠팡 동시성 (ENV SEM보다 후순위)")
    p.add_argument("--min-search", dest="min_search", type=int, default=None)
    p.add_argument("--min-ctr", dest="min_ctr", type=float, default=None)
    p.add_argument(
        "--vertical",
        type=str,
        default=None,
        help="발굴 카테고리(예: 주방용품). config/revenue_keyword_discovery.yaml verticals 키와 일치",
    )
    p.add_argument(
        "--tier",
        type=str,
        default="medium_30m",
        choices=("small_10m", "medium_30m", "large_50m"),
        help="매출 규모 프리셋",
    )
    p.add_argument("--no-db", action="store_true", help="DB 저장 생략")
    p.add_argument("--skip-coupang", action="store_true", help="쿠팡 크롤·판매력 단계 생략 (키워드점수만)")
    p.add_argument("--no-precoup-db", action="store_true", help="쿠팡 전 후보 테이블 저장 끔")
    p.add_argument("--precoup-csv", action="store_true", help="쿠팡 전 후보 CSV 저장")
    p.add_argument(
        "--no-coupang-snapshot",
        action="store_true",
        help="쿠팡 크롤 시 coupang_search_* 스냅샷 DB 저장 끔",
    )
    args = p.parse_args()

    apply_database_env_from_config("config.json")
    apply_admin_env_from_config("config.json")

    from db import log_recommended_keywords_schema_status

    tool = BlueOceanTool()
    try:
        log_recommended_keywords_schema_status()
    except Exception as e:
        print(f"[WARN] schema log: {e}", file=sys.stderr)

    ui: Dict[str, Any] = {}
    v = (args.vertical or "").strip()
    tier_s = str(args.tier or "medium_30m").strip() or "medium_30m"
    if v:
        ui["discovery"] = {"vertical": v, "tier": tier_s}
    elif tier_s != "medium_30m":
        ui["discovery"] = {"vertical": None, "tier": tier_s}

    if args.top_n is not None:
        ui.setdefault("step4", {})["top_n"] = int(args.top_n)
    if args.sem is not None:
        ui.setdefault("step4", {})["semaphore"] = int(args.sem)
    if args.min_search is not None:
        ui.setdefault("filter", {})["min_search"] = int(args.min_search)
    if args.min_ctr is not None:
        ui.setdefault("filter", {})["min_ctr"] = float(args.min_ctr)

    if args.skip_coupang or args.no_precoup_db or args.precoup_csv or args.no_coupang_snapshot:
        ui.setdefault("precoupang", {})
        ui["precoupang"]["save_db"] = not bool(args.no_precoup_db)
        ui["precoupang"]["save_csv"] = bool(args.precoup_csv)
        ui["precoupang"]["persist_coupang_snapshot"] = not bool(args.no_coupang_snapshot)
    if args.skip_coupang:
        ui["skip_coupang"] = True

    def _log(msg: str) -> None:
        print(msg, flush=True)

    result = tool.run_recommended_keyword_engine(
        args.seeds,
        top_output=int(args.limit),
        persist_db=not bool(args.no_db),
        progress=_log,
        ui_settings=ui if ui else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

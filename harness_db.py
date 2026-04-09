from __future__ import annotations

from datetime import date

from db import (
    create_run,
    ensure_schema,
    finish_run,
    insert_keyword_metrics,
    insert_monthly_trends,
    query_report_metrics_full,
    query_report_top_per_seed,
    query_top_keywords,
)


def main() -> None:
    print("[H1] schema ensure ...")
    ensure_schema()
    print("ok")

    print("[H2] create run ...")
    run = create_run("축산물,닭가슴살", date(2026, 1, 1), date(2026, 4, 7))
    print("ok run_id=", run["id"], "token=", run["run_token"])

    print("[H3] insert sample metrics ...")
    metrics = insert_keyword_metrics(
        run["id"],
        [
            {
                "seed_keyword": "축산물",
                "keyword_text": "닭가슴살",
                "monthly_search_volume_est": 12000,
                "monthly_click_est": 820.5,
                "avg_ctr_pct": 6.84,
                "product_count": 340000,
                "blue_ocean_score": 2.4132,
                "strategy_text": "유망 키워드",
            },
            {
                "seed_keyword": "축산물",
                "keyword_text": "닭가슴살10kg",
                "monthly_search_volume_est": 4500,
                "monthly_click_est": 390.2,
                "avg_ctr_pct": 8.67,
                "product_count": 120000,
                "blue_ocean_score": 3.2516,
                "strategy_text": "강력 추천",
            },
        ],
    )
    print("ok metrics_count=", len(metrics))

    print("[H4] insert monthly trends ...")
    if metrics:
        c = insert_monthly_trends(
            metrics[0]["metric_id"],
            [
                {"trend_month": "2025-12", "ratio_value": 80.2, "est_search_volume": 9800, "est_click_volume": 730.0},
                {"trend_month": "2026-01", "ratio_value": 85.9, "est_search_volume": 10400, "est_click_volume": 760.2},
                {"trend_month": "2026-02", "ratio_value": 92.1, "est_search_volume": 11500, "est_click_volume": 801.9},
            ],
        )
        print("ok trend_rows=", c)

    finish_run(run["id"], success=True, result_count=len(metrics))
    print("[H5] query top keywords ...")
    top = query_top_keywords(limit=5)
    print("ok top_count=", len(top))
    for row in top[:3]:
        print(row)

    print("[H6] report query (full + top per seed) ...")
    full = query_report_metrics_full(limit=10)
    tps = query_report_top_per_seed(top_n=5)
    print("ok full_rows=", len(full), "top_per_seed_rows=", len(tps))

    print("\nHarness completed.")


if __name__ == "__main__":
    main()

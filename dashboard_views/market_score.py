"""2. 시장성 점수 조회 페이지 본문."""
from datetime import datetime
from typing import List, Optional

import pandas as pd
import streamlit as st

from db import (
    get_connection,
    is_dsn_configured,
    query_analysis_runs_history,
    query_insight_discovery_rows,
    query_market_score_rows,
)
from report_format import report_to_excel_bytes

from web_common import accounting_format_styler, apply_band_filter, format_analysis_run_history_label, period_to_range


def render_market_score_page() -> None:
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
                labels = [format_analysis_run_history_label(r) for r in run_hist]
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
                st.dataframe(accounting_format_styler(id_view), width="stretch", hide_index=True)
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

    started_from, started_to = period_to_range(period)

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

        score_df = apply_band_filter(score_df, selected_bands, go_only)

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
                    accounting_format_styler(view_df, band_column="판단 밴드"),
                    width="stretch",
                    hide_index=True,
                )
            else:
                st.dataframe(accounting_format_styler(view_df), width="stretch", hide_index=True)

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

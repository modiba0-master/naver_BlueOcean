import os
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

import pandas as pd
import streamlit as st

from blue_ocean_tool import BlueOceanTool
from db import query_report_metrics_full, query_report_top_per_seed
from report_format import dataframe_from_db_metric_rows, report_to_excel_bytes


def _period_to_range(label: str) -> Tuple[Optional[datetime], Optional[datetime]]:
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


@st.cache_resource
def get_tool() -> BlueOceanTool:
    # config.json and env are loaded inside the tool.
    return BlueOceanTool(config_path="config.json")


def run() -> None:
    st.set_page_config(page_title="Modiba BlueOcean", layout="wide")
    st.title("Modiba BlueOcean Web")
    st.caption("Railway 웹서비스용 - 분석 실행 및 DB 결과 조회 (엑셀 템플릿 8열 형식)")

    with st.sidebar:
        st.subheader("실행 설정")
        seeds_text = st.text_input("주제어(쉼표로 구분)", value="축산물, 정육")
        default_end = date.today()
        default_start = default_end - timedelta(days=120)
        start_date = st.date_input("분석 시작일", value=default_start)
        end_date = st.date_input("분석 종료일", value=default_end)
        run_clicked = st.button("분석 실행", type="primary", use_container_width=True)

        st.divider()
        st.markdown("**환경 확인**")
        has_mysql_url = bool((os.getenv("MYSQL_URL") or "").strip())
        st.write(f"- MYSQL_URL 설정: {'예' if has_mysql_url else '아니오'}")

    tool = get_tool()

    col1, col2 = st.columns([2, 3])
    with col1:
        st.subheader("실행 로그")
        if run_clicked:
            if start_date > end_date:
                st.error("시작일이 종료일보다 클 수 없습니다.")
            else:
                logs: List[str] = []

                def _log(msg: str) -> None:
                    logs.append(msg)

                with st.spinner("분석 중입니다. API 호출량에 따라 시간이 걸릴 수 있습니다."):
                    summary, report_df = tool.start_analysis(
                        seeds=seeds_text,
                        start_date=start_date.strftime("%Y-%m-%d"),
                        end_date=end_date.strftime("%Y-%m-%d"),
                        log_callback=_log,
                    )
                st.code("\n".join(logs) if logs else "로그 없음")
                if summary:
                    st.success(f"분석 완료: {summary}")
                else:
                    st.warning("결과가 없어 저장되지 않았습니다.")
                if report_df is not None and not report_df.empty:
                    st.subheader("리포트 미리보기 (템플릿 8열)")
                    st.dataframe(report_df, use_container_width=True, hide_index=True)
                    ts = datetime.now().strftime("%Y%m%d_%H%M")
                    xlsx = report_to_excel_bytes(report_df)
                    st.download_button(
                        label="엑셀 다운로드 (모디바 카테고리별 TOP10 형식)",
                        data=xlsx,
                        file_name=f"모디바_카테고리별_TOP10_추천분석서_{ts}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                    )
        else:
            st.info("왼쪽 사이드바에서 조건을 설정하고 `분석 실행`을 눌러주세요.")

    with col2:
        st.subheader("최근 DB 결과 조회")
        filter_col1, filter_col2, filter_col3 = st.columns([2, 1, 1])
        with filter_col1:
            keyword_like = st.text_input("키워드 필터", value="")
        with filter_col2:
            period = st.selectbox("기간", ["오늘", "최근 7일", "최근 30일", "최근 60일", "최근 120일", "전체"], index=1)
        with filter_col3:
            limit = st.selectbox("건수", [20, 50, 100, 200], index=1)

        top10_mode = st.checkbox("주제어별 상위 N건 (카테고리별 TOP10 스타일)", value=False)
        top_n = 10
        if top10_mode:
            top_n = int(st.number_input("주제어당 건수", min_value=1, max_value=100, value=10))

        started_from, started_to = _period_to_range(period)

        try:
            if top10_mode:
                raw_rows = query_report_top_per_seed(
                    top_n=top_n,
                    keyword_like=(keyword_like or "").strip() or None,
                    started_from=started_from,
                    started_to=started_to,
                )
                report_df_db = dataframe_from_db_metric_rows(raw_rows)
            else:
                raw_rows = query_report_metrics_full(
                    limit=int(limit),
                    keyword_like=(keyword_like or "").strip() or None,
                    started_from=started_from,
                    started_to=started_to,
                )
                report_df_db = dataframe_from_db_metric_rows(raw_rows)
        except Exception as e:
            st.error(f"DB 조회 실패: {e}")
            report_df_db = pd.DataFrame()
            raw_rows = []

        if not report_df_db.empty:
            st.subheader("템플릿 8열 형식")
            st.dataframe(report_df_db, use_container_width=True, hide_index=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M")
            suffix = f"TOP{top_n}_" if top10_mode else ""
            xlsx_db = report_to_excel_bytes(report_df_db)
            st.download_button(
                label="엑셀 다운로드 (DB 조회 결과)",
                data=xlsx_db,
                file_name=f"모디바_카테고리별_{suffix}추천분석서_{ts}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_db_report",
                use_container_width=True,
            )
        elif not raw_rows:
            st.info("조회 결과가 없습니다.")


if __name__ == "__main__":
    run()

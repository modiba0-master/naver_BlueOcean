import os
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

import pandas as pd
import streamlit as st

from blue_ocean_tool import BlueOceanTool
from db import query_top_keywords


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
    st.caption("Railway 웹서비스용 - 분석 실행 및 DB 결과 조회")

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
                    result = tool.start_analysis(
                        seeds=seeds_text,
                        start_date=start_date.strftime("%Y-%m-%d"),
                        end_date=end_date.strftime("%Y-%m-%d"),
                        log_callback=_log,
                    )
                st.code("\n".join(logs) if logs else "로그 없음")
                if result:
                    st.success(f"분석 완료: {result}")
                else:
                    st.warning("결과가 없어 저장되지 않았습니다.")
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

        started_from, started_to = _period_to_range(period)
        rows = query_top_keywords(
            limit=int(limit),
            keyword_like=(keyword_like or "").strip() or None,
            started_from=started_from,
            started_to=started_to,
        )

        if rows:
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("조회 결과가 없습니다.")


if __name__ == "__main__":
    run()

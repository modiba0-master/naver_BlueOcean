"""1. 실행로그 페이지 본문."""
from datetime import datetime
from typing import List

import pandas as pd
import streamlit as st

from blue_ocean_tool import BlueOceanTool
from report_format import coerce_next_month_volume_column, report_to_excel_bytes

from web_common import accounting_format_styler


def render_exec_log_page(tool: BlueOceanTool) -> None:
    st.subheader("실행 로그")
    _ins_df = st.session_state.get("insight_benchmark_df", pd.DataFrame())
    _ins_note = str(st.session_state.get("insight_benchmark_note", "") or "")
    if isinstance(_ins_df, pd.DataFrame) and not _ins_df.empty:
        st.subheader("데이터랩 쇼핑인사이트 · 분야 인기 검색어 vs 주제어")
        st.markdown(_ins_note)
        st.dataframe(accounting_format_styler(_ins_df), width="stretch", hide_index=True)
        st.caption(
            "인기 검색어 순위는 datalab.naver.com 이 사용하는 비공개 순위 API 결과입니다. "
            "주제어 지표는 광고 키워드도구·쇼핑 검색으로 보강했습니다."
        )
        st.divider()

    _bm_df = st.session_state.get("category_benchmark_df", pd.DataFrame())
    _bm_note = str(st.session_state.get("category_benchmark_note", "") or "")
    if isinstance(_bm_df, pd.DataFrame) and not _bm_df.empty:
        st.subheader("카테고리 벤치마크 (주제어 대비)")
        st.markdown(_bm_note)
        st.dataframe(accounting_format_styler(_bm_df), width="stretch", hide_index=True)
        st.caption(
            "모바일 월 검색·클릭·CTR은 네이버 광고 키워드도구, 상품수는 네이버 쇼핑 검색 total 입니다. "
            "‘카테고리 내 1~20위’는 공개 API에 없어 연관 키워드 중 검색량 상위 20으로 근사합니다."
        )
        st.divider()

    last_logs: List[str] = st.session_state.get("last_run_logs", [])
    last_summary = str(st.session_state.get("last_run_summary", "")).strip()
    last_report_df = st.session_state.get("last_run_report_df", pd.DataFrame())

    if last_logs or last_summary:
        st.code("\n".join(last_logs) if last_logs else "로그 없음")
        if last_summary:
            st.success(f"분석 완료: {last_summary}")
        else:
            st.warning("결과가 없어 저장되지 않았습니다.")
        if isinstance(last_report_df, pd.DataFrame) and not last_report_df.empty:
            st.subheader("리포트 미리보기 (템플릿 8열)")
            _preview_df = coerce_next_month_volume_column(last_report_df)
            st.dataframe(accounting_format_styler(_preview_df), width="stretch", hide_index=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M")
            xlsx = report_to_excel_bytes(_preview_df)
            st.download_button(
                label="엑셀 다운로드 (모디바 카테고리별 TOP10 형식)",
                data=xlsx,
                file_name=f"모디바_카테고리별_TOP10_추천분석서_{ts}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
                key="dl_analysis_report",
            )
            _detail_fn = getattr(tool, "get_last_analysis_detail_df", None)
            detail_df = _detail_fn() if callable(_detail_fn) else pd.DataFrame()
            if isinstance(detail_df, pd.DataFrame) and not detail_df.empty:
                with st.expander("소싱 고도화 진단 (Stage 1~3)", expanded=False):
                    stage_cols = [
                        "주제어",
                        "키워드",
                        "intent",
                        "season_type",
                        "sales_power",
                        "competition_score",
                        "기회 점수",
                        "판매가치 점수",
                        "최종 점수",
                        "판단 밴드",
                    ]
                    visible_cols = [c for c in stage_cols if c in detail_df.columns]
                    stage_header_ko = {
                        "intent": "검색 의도",
                        "season_type": "시즌 유형",
                        "sales_power": "판매력 점수",
                        "competition_score": "경쟁 강도 점수",
                    }
                    if visible_cols:
                        detail_view = detail_df[visible_cols].rename(
                            columns={k: v for k, v in stage_header_ko.items() if k in visible_cols}
                        )
                        st.dataframe(
                            accounting_format_styler(detail_view, band_column="판단 밴드"),
                            width="stretch",
                            hide_index=True,
                        )
                    else:
                        st.caption("현재 롤아웃 단계에서 추가 진단 컬럼이 아직 생성되지 않았습니다.")
                    if "intent" in detail_df.columns:
                        intent_counts = detail_df["intent"].astype(str).value_counts().to_dict()
                        st.caption(f"검색 의도 분포: {intent_counts}")
                    if "season_type" in detail_df.columns:
                        season_counts = detail_df["season_type"].astype(str).value_counts().to_dict()
                        st.caption(f"시즌 유형 분포: {season_counts}")
    else:
        st.info("왼쪽 사이드바에서 조건을 설정하고 `분석 실행`을 눌러주세요.")

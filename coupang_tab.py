"""
Streamlit — 쿠팡 상품 키워드 분석 탭 UI (크롤러는 get_shared_crawler 단일 진입).
"""

from __future__ import annotations

import os
from datetime import timedelta

import streamlit as st

from coupang_crawler import get_shared_crawler
from coupang_ranked_data import (
    CoupangRankedSnapshot,
    RankedSource,
    build_top10_rank_dataframe,
    default_smoke_extract_json_path,
    resolve_coupang_ranked_snapshot,
)
from db import get_connection, is_dsn_configured, query_coupang_latest_ranked_items

_COUPANG_TABLE_REFRESH_SEC = 2.0
_PLAYWRIGHT_SMOKE_MAX_SECONDS = 5.0
_GOOGLE_HOME_URL = "https://www.google.com/"

_LAST_SMOKE_JSON_PATH = default_smoke_extract_json_path()

_SESSION_COUPANG_PREP_STATUS = "coupang_prep_status"
_SESSION_LAST_SMOKE_EXTRACT_MTIME = "_last_smoke_extract_mtime"


def _load_recent_recommended_keywords(limit: int = 200) -> list[str]:
    """
    최근 추천엔진 후보(recommended_keyword_candidates)에서 키워드를 가져온다.
    - 배치 내 rank_position 순서 우선
    - 중복 제거 후 최신 배치부터 노출
    """
    if not is_dsn_configured():
        return []
    try:
        lim = max(20, min(int(limit), 1000))
    except Exception:
        lim = 200
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.keyword_text
                    FROM recommended_keyword_candidates c
                    INNER JOIN (
                        SELECT batch_token, MAX(created_at) AS max_created
                        FROM recommended_keyword_candidates
                        GROUP BY batch_token
                        ORDER BY max_created DESC
                        LIMIT 20
                    ) b ON c.batch_token = b.batch_token
                    ORDER BY b.max_created DESC, c.rank_position ASC
                    LIMIT %s
                    """,
                    (lim,),
                )
                rows = cur.fetchall()
    except Exception:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        kw = str((r[0] if isinstance(r, (list, tuple)) else r) or "").strip()
        if not kw:
            continue
        if kw in seen:
            continue
        seen.add(kw)
        out.append(kw)
    return out


def _load_recent_candidate_batches(limit: int = 20) -> list[dict]:
    """recommended_keyword_candidates 최근 배치 목록(batch_token, created_at, count)."""
    if not is_dsn_configured():
        return []
    try:
        lim = max(5, min(int(limit), 100))
    except Exception:
        lim = 20
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT batch_token, MAX(created_at) AS max_created, COUNT(*) AS row_count
                    FROM recommended_keyword_candidates
                    GROUP BY batch_token
                    ORDER BY max_created DESC
                    LIMIT %s
                    """,
                    (lim,),
                )
                rows = cur.fetchall()
    except Exception:
        return []
    out: list[dict] = []
    for row in rows:
        out.append(
            {
                "batch_token": str(row[0] or ""),
                "created_at": row[1],
                "row_count": int(row[2] or 0),
            }
        )
    return out


def _load_keywords_by_candidate_batch(batch_token: str, limit: int = 500) -> list[str]:
    """특정 recommended_keyword_candidates 배치에서 rank 순 키워드 목록."""
    bt = str(batch_token or "").strip()
    if not bt or not is_dsn_configured():
        return []
    try:
        lim = max(20, min(int(limit), 3000))
    except Exception:
        lim = 500
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT keyword_text
                    FROM recommended_keyword_candidates
                    WHERE batch_token = %s
                    ORDER BY rank_position ASC
                    LIMIT %s
                    """,
                    (bt, lim),
                )
                rows = cur.fetchall()
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        kw = str((r[0] if isinstance(r, (list, tuple)) else r) or "").strip()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        out.append(kw)
    return out


def _load_recent_coupang_saved_keywords(limit: int = 300) -> list[str]:
    """coupang_search_runs에 저장된 최근 키워드 목록(source_type 무관)."""
    if not is_dsn_configured():
        return []
    try:
        lim = max(20, min(int(limit), 2000))
    except Exception:
        lim = 300
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT keyword_text
                    FROM coupang_search_runs
                    GROUP BY keyword_text
                    ORDER BY MAX(collected_at) DESC
                    LIMIT %s
                    """,
                    (lim,),
                )
                rows = cur.fetchall()
    except Exception:
        return []
    out: list[str] = []
    for r in rows:
        kw = str((r[0] if isinstance(r, (list, tuple)) else r) or "").strip()
        if kw:
            out.append(kw)
    return out


def _render_coupang_rank_table_db_once(keyword_text: str) -> None:
    """
    선택 키워드 Top10을 DB에서 1회 조회해 표시한다.
    (fragment auto-refresh 비활성: 선택 시 계속 재조회되는 현상 방지)
    """
    kw = str(keyword_text or "").strip()
    if not kw:
        st.info("키워드를 선택해주세요.")
        return
    if not is_dsn_configured():
        st.warning("MariaDB가 설정되지 않아 저장된 Top10을 조회할 수 없습니다.")
        return
    try:
        items = query_coupang_latest_ranked_items(kw, limit=10)
    except Exception as e:
        st.warning(f"쿠팡 DB 조회 실패: {e}")
        return
    if not items:
        st.info("선택 키워드의 저장된 Top10이 없습니다.")
        return
    st.dataframe(build_top10_rank_dataframe(items), width="stretch", hide_index=True)
    st.caption("표시: MariaDB에 저장된 해당 키워드 최신 Top10")


def _rerun_if_smoke_json_updated() -> None:
    if not _LAST_SMOKE_JSON_PATH.is_file():
        return
    try:
        mt = _LAST_SMOKE_JSON_PATH.stat().st_mtime
        prev = st.session_state.get(_SESSION_LAST_SMOKE_EXTRACT_MTIME)
        if prev is None:
            st.session_state[_SESSION_LAST_SMOKE_EXTRACT_MTIME] = mt
        elif mt > float(prev):
            st.session_state[_SESSION_LAST_SMOKE_EXTRACT_MTIME] = mt
            st.rerun()
    except OSError:
        pass


@st.fragment(run_every=timedelta(seconds=_COUPANG_TABLE_REFRESH_SEC))
def _render_coupang_rank_table_live(keyword_text: str) -> None:
    """스모크 probe 직후 메모리 캐시를 우선 표시하고, DB·JSON 폴백 및 JSON 갱신 시 전체 rerun."""
    _rerun_if_smoke_json_updated()

    cc = get_shared_crawler()
    kw = str(keyword_text).strip()

    cache_fn = getattr(cc, "get_smoke_ranked_ui_cache", None)
    snapshot = resolve_coupang_ranked_snapshot(
        kw,
        get_smoke_ranked_ui_cache=cache_fn if callable(cache_fn) else None,
        query_db_latest=lambda k, lim: query_coupang_latest_ranked_items(k, limit=lim),
        dsn_configured=is_dsn_configured(),
        smoke_json_path=_LAST_SMOKE_JSON_PATH,
        limit=10,
    )

    if snapshot.db_error:
        st.caption(f"쿠팡 DB 조회 실패: {snapshot.db_error}")

    st.dataframe(build_top10_rank_dataframe(snapshot.items), width="stretch", hide_index=True)

    _render_rank_source_captions(cc, kw, snapshot)


def _render_rank_source_captions(cc: object, kw: str, snapshot: CoupangRankedSnapshot) -> None:
    if not kw:
        return
    items = snapshot.items
    src = snapshot.source
    if items:
        if src == RankedSource.MEMORY:
            st.caption(
                "표시: 방금 스모크 **메모리 결과**(최대 10위). 같은 내용이 MariaDB 및 `.smoke/last_smoke_extract.json` 에 저장되며, "
                "JSON이 갱신되면 전체 화면을 새로고침합니다."
            )
        elif src == RankedSource.JSON_FILE:
            st.caption(
                "표시: `.smoke/last_smoke_extract.json` 과 입력 키워드가 일치하는 **파일 폴백** 결과(최대 10위)."
            )
        elif src == RankedSource.DATABASE:
            st.caption("표시: MariaDB에 저장된 해당 키워드 **가장 최근** 결과(최대 10위).")
        return

    is_running_fn = getattr(cc, "is_smoke_playwright_running", None)
    if callable(is_running_fn) and is_running_fn():
        st.caption(
            "스모크 실행 중입니다. 순위표는 probe 완료 후 수 초 안에 자동으로 채워집니다 "
            f"(약 {_COUPANG_TABLE_REFRESH_SEC:.0f}초 간격 갱신)."
        )
    elif not is_dsn_configured():
        st.caption(
            "MariaDB가 설정되지 않았습니다. 스모크 결과는 **메모리에만** 남으며, 페이지를 새로고침하면 사라질 수 있습니다."
        )
    else:
        st.caption(
            "해당 키워드로 저장된 결과가 없습니다. 검색 실행 후 잠시만 기다리거나 다른 탭으로 갔다 오세요."
        )


def render_coupang_keyword_analysis_tab() -> None:
    """대시보드 탭 «쿠팡 상품 키워드 분석» 본문."""
    st.subheader("쿠팡 상품 키워드 분석")
    st.caption("단일 키워드 검색 결과 Top10 상품 정보를 표시합니다.")

    with st.expander("이 탭과 4번 탭·DB 연동 (현재 방식)", expanded=False):
        st.markdown(
            "- **스모크 검색**: 아래 키워드 입력 후 실행 → Top10은 MariaDB "
            "`coupang_search_runs` / `coupang_search_ranked_items` 에 저장됩니다.  \n"
            "- **4번 매출 키워드 추천**의 **자동 쿠팡 수집**도 **동일 스모크 경로**를 키워드마다 순차 호출합니다.  \n"
            "- 추천 엔진이 쿠팡 스냅샷을 켠 채 돌 때도 **같은 테이블**에 적재되며, 그때 `source_type`은 **`recommend_engine`** 입니다.  \n"
            "- **단계별·저장 위치 전체**는 **4번 탭** 상단 **「현재까지 운영 방식」** 을 펼쳐 보세요.  \n"
            "- 이 페이지 하단 **「추천엔진 추출 키워드 조회」**에서 배치 토큰별로 저장된 Top10을 골라 볼 수 있습니다."
        )

    if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_SERVICE_NAME"):
        st.info(
            "Railway 등 **원격 서버**에서 앱이 실행 중입니다. Playwright Chromium 창은 **서버 쪽**에만 열리고, "
            "지금 쓰는 브라우저가 있는 **이 PC 화면에는 창이 보이지 않습니다.** "
            "확인은 아래 **스모크 상태**(phase·URL·JSON)로 하시면 됩니다. "
            "이 PC에서 창까지 보려면 저장소를 받아 로컬에서 `streamlit run app_web.py` 를 실행하세요."
        )

    st.caption(
        "**키워드 검색** 은 입력한 쿠팡 키워드로 Playwright Chromium 스모크를 실행합니다. "
        f"최대 {int(_PLAYWRIGHT_SMOKE_MAX_SECONDS)}초 유지하며, **[강제 종료]** 로 중단할 수 있습니다."
    )
    st.caption(
        "이 화면은 **PNG 스크린샷을 저장하지 않습니다**(.smoke/smoke_step*.png 등은 Cursor·외부 스모크 도구 산물일 수 있음). "
        "브라우저 **세션 스냅샷** 저장만 `.smoke/coupang_state.json` 에 하며, 끄려면 `COUPANG_SMOKE_STORAGE_STATE=false` "
        "환경변수를 설정하세요."
    )

    cc = get_shared_crawler()

    c_input_col1, c_input_col2 = st.columns([3, 1])
    with c_input_col1:
        coupang_keyword = st.text_input(
            "쿠팡 검색 키워드",
            value="",
            placeholder="예: 그램 노트북",
            key="coupang_single_keyword",
        )
    with c_input_col2:
        prep_pw_smoke_clicked = st.button(
            "키워드 검색",
            key="coupang_prep_pw_smoke_btn",
            width="stretch",
        )

    prep_pw_stop_clicked = st.button(
        "Playwright Chromium 강제 종료",
        key="coupang_prep_pw_smoke_stop_btn",
        width="stretch",
        disabled=not cc.is_smoke_playwright_running(),
    )
    if cc.is_smoke_playwright_running():
        st.caption(
            f"스모크 Chromium 실행 중 — 최대 {int(_PLAYWRIGHT_SMOKE_MAX_SECONDS)}초 유지 또는 위 버튼으로 즉시 종료."
        )

    if prep_pw_smoke_clicked:
        if not str(coupang_keyword).strip():
            st.warning("쿠팡 검색 키워드를 입력해주세요.")
        else:
            os.environ["COUPANG_SMOKE_COUPANG_QUERY"] = str(coupang_keyword).strip()
            with st.spinner("Playwright Chromium 백그라운드 시작 중..."):
                ok_start = cc.smoke_open_playwright_chromium_window(
                    url=_GOOGLE_HOME_URL,
                    wait_seconds=_PLAYWRIGHT_SMOKE_MAX_SECONDS,
                )
                ok = False
                if ok_start:
                    ok, _last_status = cc.poll_smoke_startup_outcome(timeout_seconds=10.0)
            st.session_state[_SESSION_COUPANG_PREP_STATUS] = {
                "mode": "playwright_chromium_smoke",
                "ok": bool(ok),
                "stats": cc.get_stats(),
                "last_error": cc.get_last_error(),
                "note": (
                    f"키워드={str(coupang_keyword).strip()!r}, "
                    f"별도 창 유지 최대 {int(_PLAYWRIGHT_SMOKE_MAX_SECONDS)}초. "
                    "아래 **스모크 상태** 패널에서 phase·URL로 진행 여부를 확인하세요."
                ),
            }

    if prep_pw_stop_clicked:
        cc.stop_smoke_playwright_chromium_window()
        st.session_state[_SESSION_COUPANG_PREP_STATUS] = {
            "mode": "playwright_chromium_smoke_stop",
            "ok": True,
            "stats": cc.get_stats(),
            "last_error": {},
            "note": "스모크 Chromium 종료 요청을 보냈습니다.",
        }

    prep_status = st.session_state.get(_SESSION_COUPANG_PREP_STATUS)
    if isinstance(prep_status, dict):
        mode = prep_status.get("mode", "unknown")
        if prep_status.get("ok"):
            st.success(f"접속 준비 확인 성공(mode={mode})")
        else:
            st.warning(f"접속 준비 확인 실패(mode={mode})")
        st.caption(f"prep_stats={prep_status.get('stats', {})}")
        if prep_status.get("last_error"):
            st.error(f"prep_last_error={prep_status.get('last_error')}")
        if prep_status.get("note"):
            st.caption(str(prep_status.get("note")))

    smpv = cc.get_smoke_playwright_status()
    if str(smpv.get("phase") or "idle") != "idle":
        with st.expander(
            "Playwright Chromium 스모크 — 창·로드 확인 (phase / 상태)",
            expanded=bool(smpv.get("thread_alive")),
        ):
            st.json(smpv)
            if smpv.get("thread_alive") and smpv.get("phase") not in ("failed", "closed", "opened"):
                st.caption("로드 중이면 잠시 후 **Rerun / 새로고침**으로 다시 확인하세요.")

    _render_coupang_rank_table_live(coupang_keyword)
    st.caption("표시 컬럼: 순위, 상품명, 가격, 리뷰수, 평점, 배송비, 상품 URL")

    st.markdown("---")
    st.subheader("추천엔진 추출 키워드 조회")
    st.caption(
        "추천엔진에서 저장된 `recommended_keyword_candidates` 키워드를 필터링해서 선택하고, "
        "선택 키워드의 쿠팡 Top10(최신 저장)을 확인합니다."
    )

    batches = _load_recent_candidate_batches(limit=30)
    if not batches:
        st.info("표시할 추천엔진 배치가 없습니다. (DB/스키마/저장 상태 확인)")
        return

    batch_options = [b["batch_token"] for b in batches if b.get("batch_token")]
    batch_labels = {
        b["batch_token"]: (
            f"{b['batch_token']} · rows={b['row_count']} · "
            f"created={b['created_at']}"
        )
        for b in batches
        if b.get("batch_token")
    }
    selected_batch = st.selectbox(
        "최근 배치 토큰 선택",
        options=batch_options,
        index=0,
        key="coupang_reco_batch_token",
        format_func=lambda x: batch_labels.get(x, x),
    )
    st.caption(
        f"선택 배치: `{selected_batch}` · "
        f"candidate rows={next((b['row_count'] for b in batches if b['batch_token']==selected_batch), 0)}"
    )

    source_mode = st.radio(
        "키워드 목록 소스",
        options=("선택 배치 후보", "쿠팡 저장 키워드", "후보+저장 합집합"),
        index=2,
        horizontal=True,
        key="coupang_reco_kw_source_mode",
    )

    batch_kws = _load_keywords_by_candidate_batch(selected_batch, limit=800)
    saved_kws = _load_recent_coupang_saved_keywords(limit=800)
    if source_mode == "선택 배치 후보":
        recent_kws = batch_kws
    elif source_mode == "쿠팡 저장 키워드":
        recent_kws = saved_kws
    else:
        recent_kws = []
        seen_union: set[str] = set()
        for kw in (batch_kws + saved_kws):
            if kw in seen_union:
                continue
            seen_union.add(kw)
            recent_kws.append(kw)

    if not recent_kws:
        # 폴백
        recent_kws = _load_recent_recommended_keywords(limit=300)
    if not recent_kws:
        st.info("표시할 추천엔진 키워드가 없습니다. (DB/스키마/저장 상태 확인)")
        return

    f_col1, f_col2 = st.columns([2, 3])
    with f_col1:
        q = st.text_input(
            "키워드 필터",
            value="",
            key="coupang_reco_kw_filter",
            placeholder="예: 이케아, 타일, 텀블러",
        )
    qn = str(q).strip().lower()
    filtered_kws = [kw for kw in recent_kws if (not qn or qn in kw.lower())]
    if not filtered_kws:
        st.warning("필터 결과가 없습니다.")
        return

    with f_col2:
        selected_kw = st.selectbox(
            "추출 완료 키워드 선택",
            options=filtered_kws,
            key="coupang_reco_kw_selected",
        )

    st.caption(f"필터 결과 {len(filtered_kws)}개 / 전체 {len(recent_kws)}개")
    _render_coupang_rank_table_db_once(selected_kw)

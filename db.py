from __future__ import annotations

import os
import uuid
import math
import json
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional
from urllib.parse import unquote, urlparse

import pymysql


def is_dsn_configured() -> bool:
    """환경에 DB URL 또는 host+user가 있으면 True (연결 성공 여부는 검사하지 않음)."""
    url = (
        os.environ.get("MYSQL_URL")
        or os.environ.get("MYSQL_PUBLIC_URL")
        or os.environ.get("MARIADB_PUBLIC_URL")
        or os.environ.get("MARIADB_URL")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("DATABASE_PUBLIC_URL")
        or ""
    ).strip()
    if url:
        return True
    host = (
        os.environ.get("MARIADB_HOST")
        or os.environ.get("MYSQLHOST")
        or os.environ.get("DB_HOST")
        or ""
    ).strip()
    user = (
        os.environ.get("MARIADB_USER")
        or os.environ.get("MYSQLUSER")
        or os.environ.get("DB_USER")
        or ""
    ).strip()
    return bool(host and user)


def _dsn_from_env() -> Dict[str, Any]:
    url = (
        os.environ.get("MYSQL_URL")
        or os.environ.get("MYSQL_PUBLIC_URL")
        or os.environ.get("MARIADB_PUBLIC_URL")
        or os.environ.get("MARIADB_URL")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("DATABASE_PUBLIC_URL")
        or ""
    ).strip()
    if url:
        p = urlparse(url)
        if p.scheme not in ("mysql", "mariadb"):
            raise RuntimeError("MYSQL_URL must start with mysql:// or mariadb://")
        return {
            "host": p.hostname or "",
            "port": int(p.port or 3306),
            "user": unquote(p.username or ""),
            "password": unquote(p.password or ""),
            "database": unquote((p.path or "/railway").lstrip("/")),
            "charset": "utf8mb4",
        }

    host = (
        os.environ.get("MARIADB_HOST")
        or os.environ.get("MYSQLHOST")
        or os.environ.get("DB_HOST")
        or ""
    ).strip()
    user = (
        os.environ.get("MARIADB_USER")
        or os.environ.get("MYSQLUSER")
        or os.environ.get("DB_USER")
        or ""
    ).strip()
    if not host or not user:
        raise RuntimeError(
            "DB 연결 정보가 없습니다. 다음 중 하나를 설정하세요 — "
            "전체 URL: MARIADB_PUBLIC_URL 또는 MYSQL_PUBLIC_URL 또는 MYSQL_URL 또는 MARIADB_URL 또는 DATABASE_URL / DATABASE_PUBLIC_URL; "
            "또는 호스트+계정: MARIADB_HOST+MARIADB_USER(+비밀번호 등). "
            "로컬 PC에서는 Railway MariaDB의 TCP 프록시 URL(MARIADB_PUBLIC_URL)을 쓰거나, "
            "같은 프로젝트에서 `railway run streamlit run app_web.py` 처럼 railway run으로 실행하세요."
        )

    # *.railway.internal 은 Railway 프라이빗 네트워크 안에서만 해석됨 (로컬 PC에서는 연결 불가).
    if host.endswith(".railway.internal"):
        railway_like = bool(
            os.environ.get("RAILWAY_ENVIRONMENT")
            or os.environ.get("RAILWAY_PROJECT_ID")
            or os.environ.get("RAILWAY_SERVICE_NAME")
        )
        if not railway_like:
            raise RuntimeError(
                "MARIADB_HOST ends with .railway.internal — this hostname only works inside Railway. "
                "For local runs add MARIADB_PUBLIC_URL / MYSQL_PUBLIC_URL (TCP Proxy URL from Railway MariaDB) or MYSQL_URL "
                "with a public host, or remove MARIADB_HOST when using a full DB URL."
            )

    return {
        "host": host,
        "port": int(
            (
                os.environ.get("MARIADB_PORT")
                or os.environ.get("MYSQLPORT")
                or os.environ.get("DB_PORT")
                or "3306"
            ).strip()
        ),
        "user": user,
        "password": (
            os.environ.get("MARIADB_PASSWORD")
            or os.environ.get("MYSQLPASSWORD")
            or os.environ.get("DB_PASSWORD")
            or ""
        ).strip(),
        "database": (
            os.environ.get("MARIADB_DATABASE")
            or os.environ.get("MYSQLDATABASE")
            or os.environ.get("DB_NAME")
            or "modiba"
        ).strip(),
        "charset": "utf8mb4",
    }


@contextmanager
def get_connection() -> Generator[pymysql.connections.Connection, None, None]:
    cfg = _dsn_from_env()
    conn = pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset=cfg["charset"],
        autocommit=False,
        cursorclass=pymysql.cursors.Cursor,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema() -> None:
    sql_dir = Path(__file__).resolve().parent / "sql"
    sql_files = sorted(sql_dir.glob("*.sql"))
    statements: List[str] = []
    for schema_file in sql_files:
        sql_text = schema_file.read_text(encoding="utf-8")
        statements.extend([s.strip() for s in sql_text.split(";") if s.strip()])
    with get_connection() as conn:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)


def create_run(seed_keywords_raw: str, start_date: date, end_date: date) -> Dict[str, Any]:
    run_token = uuid.uuid4().hex[:24]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO analysis_runs (run_token, seed_keywords_raw, start_date, end_date, status)
                VALUES (%s, %s, %s, %s, 'RUNNING')
                """,
                (run_token, seed_keywords_raw, start_date, end_date),
            )
            run_id = int(cur.lastrowid)
    return {"id": run_id, "run_token": run_token}


def finish_run(run_id: int, *, success: bool, result_count: int = 0, error_message: str = "") -> None:
    status = "SUCCESS" if success else "FAILED"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE analysis_runs
                SET status=%s, result_count=%s, error_message=%s, finished_at=%s
                WHERE id=%s
                """,
                (status, int(result_count), error_message or None, datetime.now(), int(run_id)),
            )


def _dash_safe_float(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        if math.isnan(x):
            return default
        return x
    except Exception:
        return default


def dashboard_classify_keyword_intent(keyword: str) -> str:
    """키워드 텍스트만으로 검색 의도 분류(DB 미저장 분석과 동일 규칙)."""
    try:
        text = str(keyword or "").strip().lower()
        if not text:
            return "정보형"
        if any(k in text for k in ("구매", "가격", "할인")):
            return "구매형"
        if any(k in text for k in ("추천", "비교", "순위")):
            return "탐색형"
        return "정보형"
    except Exception:
        return "정보형"


def dashboard_detect_seasonality(trend_series: List[float]) -> str:
    """월별 추정 검색량 시계열로 시즌 유형 추정(BlueOceanTool과 동일 기준)."""
    try:
        vals = [_dash_safe_float(v, 0.0) for v in trend_series if _dash_safe_float(v, 0.0) > 0]
        if len(vals) < 6:
            return "steady"
        peak = max(vals)
        avg = sum(vals) / len(vals)
        trough = min(vals)
        variance = sum((x - avg) ** 2 for x in vals) / len(vals)
        std_v = math.sqrt(max(0.0, variance))
        vol_ratio = (std_v / avg) if avg > 1e-12 else 0.0
        recent = sum(vals[-3:]) / max(1, len(vals[-3:]))
        prev = sum(vals[:-3]) / max(1, len(vals[:-3])) if len(vals) > 3 else avg
        seasonal_pattern = avg > 0 and peak / avg >= 1.6 and trough / avg <= 0.7
        volatile_enough = vol_ratio > 0.25
        if seasonal_pattern and volatile_enough:
            return "seasonal"
        if prev > 0 and recent / prev >= 1.25:
            return "trend"
        return "steady"
    except Exception:
        return "steady"


def dashboard_sales_power_estimate(
    top10_rev: Optional[float],
    top10_price: Optional[float],
    monthly_click: float,
    avg_ctr: float,
) -> float:
    """DB에 판매력이 없을 때 Top10·클릭 기반 0~100 추정."""
    try:
        mc = max(0.0, float(monthly_click))
        ctr = max(0.0, float(avg_ctr))
        review_price = 0.0
        if top10_price is not None and float(top10_price) > 0 and top10_rev is not None:
            lp_price = math.log1p(max(1.0, float(top10_price)))
            denom_price = max(math.pow(lp_price, 0.7), 1e-12)
            review_price = math.log1p(max(0.0, float(top10_rev))) / denom_price
        conv_fb = max(0.1, math.log1p(mc) * (1.0 + ctr / 100.0))
        rp_part = min(100.0, review_price * 42.0) if review_price > 0 else 0.0
        fb_part = min(100.0, conv_fb * 35.0)
        sp = rp_part + (fb_part * 0.35 if rp_part <= 0 else fb_part * 0.2)
        return round(max(0.0, min(100.0, sp)), 2)
    except Exception:
        return 0.0


def query_analysis_runs_history(limit: int = 80) -> List[Dict[str, Any]]:
    """
    실행로그 탭 「분석 실행」으로 적재된 analysis_runs 목록 (run 단위).
    시장성 점수 탭에서 특정 실행 결과만 불러올 때 사용.
    """
    lim = max(1, min(int(limit), 200))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ar.id, ar.started_at, ar.finished_at, ar.status,
                       ar.seed_keywords_raw, ar.result_count,
                       (
                           SELECT COUNT(*) FROM keyword_metrics km WHERE km.run_id = ar.id
                       ) AS metric_rows
                FROM analysis_runs ar
                ORDER BY ar.started_at DESC
                LIMIT %s
                """,
                (lim,),
            )
            rows = cur.fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "run_id": int(r[0]),
                "started_at": r[1],
                "finished_at": r[2],
                "status": str(r[3] or ""),
                "seed_keywords_raw": str(r[4] or ""),
                "result_count": int(r[5] or 0),
                "metric_rows": int(r[6] or 0),
            }
        )
    return out


def insert_keyword_metrics(
    run_id: int, rows: Iterable[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO keyword_metrics
                    (run_id, seed_keyword, keyword_text, monthly_search_volume_est, monthly_click_est,
                     avg_ctr_pct, product_count, top10_avg_reviews, top10_avg_price, blue_ocean_score, strategy_text)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      monthly_search_volume_est=VALUES(monthly_search_volume_est),
                      monthly_click_est=VALUES(monthly_click_est),
                      avg_ctr_pct=VALUES(avg_ctr_pct),
                      product_count=VALUES(product_count),
                      top10_avg_reviews=VALUES(top10_avg_reviews),
                      top10_avg_price=VALUES(top10_avg_price),
                      blue_ocean_score=VALUES(blue_ocean_score),
                      strategy_text=VALUES(strategy_text)
                    """,
                    (
                        int(run_id),
                        str(r.get("seed_keyword", ""))[:255],
                        str(r.get("keyword_text", ""))[:255],
                        int(r.get("monthly_search_volume_est", 0) or 0),
                        float(r.get("monthly_click_est", 0.0) or 0.0),
                        float(r.get("avg_ctr_pct", 0.0) or 0.0),
                        int(r.get("product_count", 0) or 0),
                        (float(r.get("top10_avg_reviews")) if r.get("top10_avg_reviews") is not None else None),
                        (float(r.get("top10_avg_price")) if r.get("top10_avg_price") is not None else None),
                        float(r.get("blue_ocean_score", 0.0) or 0.0),
                        (str(r.get("strategy_text", ""))[:512] if r.get("strategy_text") else None),
                    ),
                )
                metric_id = int(cur.lastrowid) if cur.lastrowid else None
                if metric_id is None:
                    cur.execute(
                        """
                        SELECT id FROM keyword_metrics
                        WHERE run_id=%s AND seed_keyword=%s AND keyword_text=%s
                        LIMIT 1
                        """,
                        (int(run_id), str(r.get("seed_keyword", ""))[:255], str(r.get("keyword_text", ""))[:255]),
                    )
                    one = cur.fetchone()
                    metric_id = int(one[0]) if one else 0
                out.append({"metric_id": metric_id, "keyword_text": str(r.get("keyword_text", ""))})
    return out


def insert_monthly_trends(metric_id: int, trend_rows: Iterable[Dict[str, Any]]) -> int:
    batch: List[tuple] = []
    for tr in trend_rows:
        batch.append(
            (
                int(metric_id),
                str(tr.get("trend_month", ""))[:7],
                float(tr.get("ratio_value", 0.0) or 0.0),
                int(tr.get("est_search_volume", 0) or 0),
                float(tr.get("est_click_volume", 0.0) or 0.0),
            )
        )
    if not batch:
        return 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO keyword_trends_monthly
                (metric_id, trend_month, ratio_value, est_search_volume, est_click_volume)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  ratio_value=VALUES(ratio_value),
                  est_search_volume=VALUES(est_search_volume),
                  est_click_volume=VALUES(est_click_volume)
                """,
                batch,
            )
    return len(batch)


def insert_keyword_evaluations(run_id: int, rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO keyword_evaluations
                    (run_id, metric_id, opportunity_score, commercial_score, final_score, decision_band)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      opportunity_score=VALUES(opportunity_score),
                      commercial_score=VALUES(commercial_score),
                      final_score=VALUES(final_score),
                      decision_band=VALUES(decision_band)
                    """,
                    (
                        int(run_id),
                        int(r.get("metric_id", 0) or 0),
                        float(r.get("opportunity_score", 0.0) or 0.0),
                        float(r.get("commercial_score", 0.0) or 0.0),
                        float(r.get("final_score", 0.0) or 0.0),
                        str(r.get("decision_band", "WATCH"))[:16],
                    ),
                )
                eval_id = int(cur.lastrowid) if cur.lastrowid else 0
                out.append({"evaluation_id": eval_id, "metric_id": int(r.get("metric_id", 0) or 0)})
    return out


def insert_ai_insights(rows: Iterable[Dict[str, Any]]) -> int:
    batch = []
    for r in rows:
        batch.append(
            (
                int(r.get("run_id", 0) or 0),
                int(r.get("metric_id", 0) or 0),
                str(r.get("keyword_text", ""))[:255],
                str(r.get("summary_text", ""))[:2000],
                str(r.get("action_text", ""))[:1000],
                str(r.get("risk_text", ""))[:2000],
                json.dumps(r.get("evidence_json", []), ensure_ascii=False),
                float(r.get("confidence_score", 0.0) or 0.0),
                str(r.get("model_version", "rule-based-v1"))[:64],
                int(r.get("token_usage_est", 0) or 0),
                1 if bool(r.get("cache_hit", False)) else 0,
            )
        )
    if not batch:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO ai_insights
                (run_id, metric_id, keyword_text, summary_text, action_text, risk_text,
                 evidence_json, confidence_score, model_version, token_usage_est, cache_hit)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                batch,
            )
    return len(batch)


def insert_ai_pipeline_logs(run_id: int, metric_id: int, rows: Iterable[Dict[str, Any]]) -> int:
    batch = []
    for r in rows:
        batch.append(
            (
                int(run_id),
                int(metric_id),
                str(r.get("node_name", ""))[:64],
                str(r.get("status", "SUCCESS"))[:16],
                int(r.get("latency_ms", 0) or 0),
                int(r.get("token_usage_est", 0) or 0),
                str(r.get("meta_json", ""))[:2000] if r.get("meta_json") else None,
            )
        )
    if not batch:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO ai_pipeline_logs
                (run_id, metric_id, node_name, status, latency_ms, token_usage_est, meta_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                batch,
            )
    return len(batch)


def query_top_keywords(
    limit: int = 20,
    keyword_like: Optional[str] = None,
    started_from: Optional[datetime] = None,
    started_to: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    where_parts: List[str] = []
    params: List[Any] = []
    if keyword_like:
        where_parts.append("km.keyword_text LIKE %s")
        params.append(f"%{keyword_like.strip()}%")
    if started_from:
        where_parts.append("ar.started_at >= %s")
        params.append(started_from)
    if started_to:
        where_parts.append("ar.started_at <= %s")
        params.append(started_to)
    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    params.append(max(1, min(int(limit), 200)))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT ar.id, ar.started_at, km.seed_keyword, km.keyword_text, km.blue_ocean_score,
                       km.monthly_search_volume_est, km.product_count
                FROM keyword_metrics km
                JOIN analysis_runs ar ON ar.id = km.run_id
                {where_sql}
                ORDER BY km.monthly_search_volume_est DESC, km.blue_ocean_score DESC, ar.started_at DESC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = cur.fetchall()

    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "run_id": r[0],
                "started_at": r[1],
                "seed_keyword": r[2],
                "keyword_text": r[3],
                "blue_ocean_score": float(r[4]),
                "monthly_search_volume_est": int(r[5]),
                "product_count": int(r[6]),
            }
        )
    return out


def query_report_metrics_full(
    limit: int = 500,
    keyword_like: Optional[str] = None,
    started_from: Optional[datetime] = None,
    started_to: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    엑셀 템플릿(8열)에 맞추기 위한 전체 지표 행.
    """
    where_parts: List[str] = []
    params: List[Any] = []
    if keyword_like:
        where_parts.append("km.keyword_text LIKE %s")
        params.append(f"%{keyword_like.strip()}%")
    if started_from:
        where_parts.append("ar.started_at >= %s")
        params.append(started_from)
    if started_to:
        where_parts.append("ar.started_at <= %s")
        params.append(started_to)
    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    lim = max(1, min(int(limit), 2000))
    params.append(lim)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT ar.started_at,
                       km.seed_keyword, km.keyword_text,
                       km.monthly_search_volume_est, km.monthly_click_est, km.avg_ctr_pct,
                       km.product_count, km.top10_avg_reviews, km.top10_avg_price,
                       km.blue_ocean_score, km.strategy_text
                FROM keyword_metrics km
                JOIN analysis_runs ar ON ar.id = km.run_id
                {where_sql}
                ORDER BY km.monthly_search_volume_est DESC, km.blue_ocean_score DESC, ar.started_at DESC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = cur.fetchall()

    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "started_at": r[0],
                "seed_keyword": r[1],
                "keyword_text": r[2],
                "monthly_search_volume_est": int(r[3]),
                "monthly_click_est": float(r[4]),
                "avg_ctr_pct": float(r[5]),
                "product_count": int(r[6]),
                "top10_avg_reviews": (float(r[7]) if r[7] is not None else None),
                "top10_avg_price": (float(r[8]) if r[8] is not None else None),
                "blue_ocean_score": float(r[9]),
                "strategy_text": r[10] or "",
            }
        )
    return out


def query_report_top_per_seed(
    top_n: int = 10,
    keyword_like: Optional[str] = None,
    started_from: Optional[datetime] = None,
    started_to: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    주제어(seed_keyword)별 블루오션 점수 상위 N건 (카테고리별 TOP10 리포트용).
    MariaDB 10.2+ ROW_NUMBER() 필요.
    """
    where_parts: List[str] = []
    params: List[Any] = []
    if keyword_like:
        where_parts.append("km.keyword_text LIKE %s")
        params.append(f"%{keyword_like.strip()}%")
    if started_from:
        where_parts.append("ar.started_at >= %s")
        params.append(started_from)
    if started_to:
        where_parts.append("ar.started_at <= %s")
        params.append(started_to)
    inner_where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    n = max(1, min(int(top_n), 100))
    params.append(n)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT started_at, seed_keyword, keyword_text,
                       monthly_search_volume_est, monthly_click_est, avg_ctr_pct,
                       product_count, top10_avg_reviews, top10_avg_price, blue_ocean_score, strategy_text
                FROM (
                    SELECT ar.started_at,
                           km.seed_keyword, km.keyword_text,
                           km.monthly_search_volume_est, km.monthly_click_est, km.avg_ctr_pct,
                           km.product_count, km.top10_avg_reviews, km.top10_avg_price,
                           km.blue_ocean_score, km.strategy_text,
                           ROW_NUMBER() OVER (
                             PARTITION BY km.seed_keyword
                             ORDER BY km.blue_ocean_score DESC, ar.started_at DESC
                           ) AS rn
                    FROM keyword_metrics km
                    JOIN analysis_runs ar ON ar.id = km.run_id
                    {inner_where}
                ) ranked
                WHERE ranked.rn <= %s
                ORDER BY ranked.monthly_search_volume_est DESC, ranked.blue_ocean_score DESC, ranked.seed_keyword
                """,
                tuple(params),
            )
            rows = cur.fetchall()

    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "started_at": r[0],
                "seed_keyword": r[1],
                "keyword_text": r[2],
                "monthly_search_volume_est": int(r[3]),
                "monthly_click_est": float(r[4]),
                "avg_ctr_pct": float(r[5]),
                "product_count": int(r[6]),
                "top10_avg_reviews": (float(r[7]) if r[7] is not None else None),
                "top10_avg_price": (float(r[8]) if r[8] is not None else None),
                "blue_ocean_score": float(r[9]),
                "strategy_text": r[10] or "",
            }
        )
    return out


def query_recent_keyword_cache(
    keyword_text: str,
    *,
    start_date: date,
    end_date: date,
    ttl_hours: int = 24,
) -> Optional[Dict[str, Any]]:
    """
    최근 TTL 내 동일 키워드 지표/월별 트렌드 캐시 조회.
    """
    keyword = str(keyword_text or "").strip()
    if not keyword:
        return None

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT km.id, km.monthly_search_volume_est, km.monthly_click_est, km.avg_ctr_pct,
                       km.product_count, km.top10_avg_reviews, km.top10_avg_price,
                       km.blue_ocean_score, km.strategy_text
                FROM keyword_metrics km
                JOIN analysis_runs ar ON ar.id = km.run_id
                WHERE km.keyword_text = %s
                  AND ar.status = 'SUCCESS'
                  AND ar.start_date = %s
                  AND ar.end_date = %s
                  AND ar.started_at >= DATE_SUB(NOW(), INTERVAL %s HOUR)
                ORDER BY ar.started_at DESC
                LIMIT 1
                """,
                (keyword, start_date, end_date, int(max(1, ttl_hours))),
            )
            row = cur.fetchone()
            if not row:
                return None

            metric_id = int(row[0])
            cur.execute(
                """
                SELECT trend_month, ratio_value, est_search_volume, est_click_volume
                FROM keyword_trends_monthly
                WHERE metric_id = %s
                ORDER BY trend_month
                """,
                (metric_id,),
            )
            trend_rows = cur.fetchall()

    trends: Dict[str, Dict[str, Any]] = {}
    for tr in trend_rows:
        trends[str(tr[0])] = {
            "ratio": float(tr[1]),
            "est_search_volume": int(tr[2]),
            "est_click_volume": float(tr[3]),
        }

    return {
        "monthly_search_volume_est": int(row[1]),
        "monthly_click_est": float(row[2]),
        "avg_ctr_pct": float(row[3]),
        "product_count": int(row[4]),
        "top10_avg_reviews": (float(row[5]) if row[5] is not None else None),
        "top10_avg_price": (float(row[6]) if row[6] is not None else None),
        "blue_ocean_score": float(row[7]),
        "strategy_text": row[8] or "",
        "trends": trends,
    }


def query_market_score_rows(
    limit: int = 50,
    keyword_like: Optional[str] = None,
    started_from: Optional[datetime] = None,
    started_to: Optional[datetime] = None,
    run_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    대시보드용 시장성 점수 조회 행.
    점수식: 수요 * 트렌드 * 전환 / 경쟁

    run_id 가 주어지면 해당 실행(run)에 속한 행만 반환하고, 기간 필터는 적용하지 않는다.
    """
    where_parts: List[str] = []
    params: List[Any] = []
    if keyword_like:
        where_parts.append("km.keyword_text LIKE %s")
        params.append(f"%{keyword_like.strip()}%")
    if run_id is not None:
        where_parts.append("ar.id = %s")
        params.append(int(run_id))
    else:
        if started_from:
            where_parts.append("ar.started_at >= %s")
            params.append(started_from)
        if started_to:
            where_parts.append("ar.started_at <= %s")
            params.append(started_to)
    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    if run_id is not None:
        lim = max(1, min(int(limit), 15000))
        order_sql = "ORDER BY km.seed_keyword ASC, km.blue_ocean_score DESC"
    else:
        lim = max(1, min(int(limit), 500))
        order_sql = "ORDER BY km.monthly_search_volume_est DESC, ar.started_at DESC"
    params.append(lim)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT km.id, ar.started_at, km.seed_keyword, km.keyword_text,
                       km.monthly_search_volume_est, km.monthly_click_est, km.avg_ctr_pct,
                       km.product_count, km.top10_avg_reviews, km.top10_avg_price, km.blue_ocean_score,
                       ke.opportunity_score, ke.commercial_score, ke.final_score, ke.decision_band,
                       ai.summary_text, ai.action_text, ai.risk_text, ai.confidence_score, ai.model_version,
                       ar.id
                FROM keyword_metrics km
                JOIN analysis_runs ar ON ar.id = km.run_id
                LEFT JOIN keyword_evaluations ke ON ke.metric_id = km.id
                LEFT JOIN (
                    SELECT t1.*
                    FROM ai_insights t1
                    JOIN (
                        SELECT metric_id, MAX(id) AS max_id
                        FROM ai_insights
                        GROUP BY metric_id
                    ) t2 ON t1.metric_id = t2.metric_id AND t1.id = t2.max_id
                ) ai ON ai.metric_id = km.id
                {where_sql}
                {order_sql}
                LIMIT %s
                """,
                tuple(params),
            )
            metric_rows = cur.fetchall()

            metric_ids = [int(r[0]) for r in metric_rows]
            trend_map: Dict[int, List[float]] = {mid: [] for mid in metric_ids}
            if metric_ids:
                placeholders = ", ".join(["%s"] * len(metric_ids))
                cur.execute(
                    f"""
                    SELECT metric_id, trend_month, est_search_volume
                    FROM keyword_trends_monthly
                    WHERE metric_id IN ({placeholders})
                    ORDER BY metric_id, trend_month
                    """,
                    tuple(metric_ids),
                )
                for tr in cur.fetchall():
                    trend_map[int(tr[0])].append(float(tr[2] or 0.0))

    def _trend_score(vols: List[float]) -> float:
        if len(vols) < 4:
            return 1.0
        recent = vols[-3:]
        prev = vols[:-3] if len(vols) > 3 else vols[:1]
        recent_avg = sum(recent) / max(1, len(recent))
        prev_avg = sum(prev) / max(1, len(prev))
        if prev_avg <= 0:
            ratio = 1.2 if recent_avg > 0 else 1.0
        else:
            ratio = recent_avg / prev_avg
        return max(0.7, min(1.8, float(ratio)))

    out: List[Dict[str, Any]] = []
    for r in metric_rows:
        metric_id = int(r[0])
        monthly_search = int(r[4] or 0)
        monthly_click = float(r[5] or 0.0)
        avg_ctr = float(r[6] or 0.0)
        product_count = int(r[7] or 0)
        top10_avg_reviews = float(r[8]) if r[8] is not None else None
        top10_avg_price = float(r[9]) if r[9] is not None else None
        trend_vols = trend_map.get(metric_id, [])

        demand_score = math.log1p(max(0, monthly_search))
        trend_score = _trend_score(trend_vols)
        if top10_avg_reviews is not None and top10_avg_price is not None and top10_avg_price > 0:
            conversion_score = max(0.1, math.log1p(max(0.0, top10_avg_reviews)) / math.log1p(top10_avg_price))
        else:
            conversion_score = max(0.1, math.log1p(max(0.0, monthly_click)) * (1.0 + max(0.0, avg_ctr) / 100.0))
        competition_score = max(1.0, math.log1p(max(0, product_count)))
        market_score = (demand_score * trend_score * conversion_score) / competition_score

        opportunity_score = float(r[11]) if r[11] is not None else None
        commercial_score = float(r[12]) if r[12] is not None else None
        final_score = float(r[13]) if r[13] is not None else None
        decision_band = str(r[14]) if r[14] is not None else None
        ai_summary = str(r[15]) if r[15] is not None else None
        ai_action = str(r[16]) if r[16] is not None else None
        ai_risk = str(r[17]) if r[17] is not None else None
        ai_confidence = float(r[18]) if r[18] is not None else None
        ai_model_version = str(r[19]) if r[19] is not None else None
        analysis_run_id = int(r[20]) if len(r) > 20 and r[20] is not None else None

        kw_txt = str(r[3] or "")
        intent_d = dashboard_classify_keyword_intent(kw_txt)
        season_d = dashboard_detect_seasonality(trend_vols)
        if commercial_score is not None:
            sales_pw = round(max(0.0, min(100.0, float(commercial_score))), 2)
        else:
            sales_pw = dashboard_sales_power_estimate(
                top10_avg_reviews, top10_avg_price, monthly_click, avg_ctr
            )

        out.append(
            {
                "started_at": r[1],
                "seed_keyword": r[2],
                "keyword_text": r[3],
                "monthly_search_volume_est": monthly_search,
                "monthly_click_est": monthly_click,
                "avg_ctr_pct": avg_ctr,
                "product_count": product_count,
                "blue_ocean_score": float(r[10] or 0.0),
                "demand_score": round(demand_score, 4),
                "trend_score": round(trend_score, 4),
                "conversion_score": round(conversion_score, 4),
                "competition_score": round(competition_score, 4),
                "market_score": round(float(market_score), 4),
                "opportunity_score": opportunity_score,
                "commercial_score": commercial_score,
                "final_score": final_score,
                "decision_band": decision_band,
                "top10_avg_reviews": top10_avg_reviews,
                "top10_avg_price": top10_avg_price,
                "ai_summary": ai_summary,
                "ai_action": ai_action,
                "ai_risk": ai_risk,
                "ai_confidence": ai_confidence,
                "ai_model_version": ai_model_version,
                "analysis_run_id": analysis_run_id,
                "intent": intent_d,
                "season_type": season_d,
                "sales_power": sales_pw,
            }
        )

    if run_id is None:
        out.sort(key=lambda x: x["market_score"], reverse=True)
    return out


def insert_coupang_search_snapshot(payload: Dict[str, Any]) -> int:
    """
    쿠팡 키워드 검색 스냅샷(요약+랭킹 아이템)을 별도 테이블에 저장한다.
    기존 주제어 분석 테이블과 완전 분리된 저장 경로다.
    """
    if not isinstance(payload, dict):
        return 0

    keyword_text = str(payload.get("keyword", "")).strip()[:255]
    if not keyword_text:
        return 0

    raw_saved_at = str(payload.get("saved_at", "")).strip()
    try:
        collected_at = datetime.fromisoformat(raw_saved_at) if raw_saved_at else datetime.now()
    except Exception:
        collected_at = datetime.now()

    source_type = str(payload.get("source_type", "smoke")).strip()[:32] or "smoke"
    page_url = str(payload.get("url", "")).strip()[:1000] or None
    page_title = str(payload.get("title", "")).strip()[:500] or None
    html_len = payload.get("html_len", None)
    card_count = payload.get("card_count", None)
    organic_count = payload.get("organic_count", None)

    def _to_int_or_none(v: Any) -> Optional[int]:
        if v is None or v == "":
            return None
        try:
            return int(v)
        except Exception:
            return None

    rank_items = payload.get("top10") or payload.get("top3") or []
    if not isinstance(rank_items, list):
        rank_items = []

    inserted_items = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO coupang_search_runs
                (collected_at, source_type, keyword_text, page_url, page_title, html_len, card_count, organic_count, raw_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    collected_at,
                    source_type,
                    keyword_text,
                    page_url,
                    page_title,
                    _to_int_or_none(html_len),
                    _to_int_or_none(card_count),
                    _to_int_or_none(organic_count),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            run_id = int(cur.lastrowid)

            batch: List[tuple] = []
            for item in rank_items:
                if not isinstance(item, dict):
                    continue
                try:
                    rank_no = int(item.get("rank", 0) or 0)
                except Exception:
                    rank_no = 0
                if rank_no <= 0:
                    continue
                batch.append(
                    (
                        run_id,
                        rank_no,
                        str(item.get("title", "")).strip()[:1000] or None,
                        str(item.get("price", "")).strip()[:128] or None,
                        str(item.get("shipping", "")).strip()[:255] or None,
                        str(item.get("review_count", "")).strip()[:64] or None,
                        str(item.get("review_score", "")).strip()[:64] or None,
                        str(item.get("url", "")).strip()[:1200] or None,
                    )
                )

            if batch:
                cur.executemany(
                    """
                    INSERT INTO coupang_search_ranked_items
                    (run_id, rank_no, product_title, price_text, shipping_text, review_count_text, review_score_text, product_url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      product_title=VALUES(product_title),
                      price_text=VALUES(price_text),
                      shipping_text=VALUES(shipping_text),
                      review_count_text=VALUES(review_count_text),
                      review_score_text=VALUES(review_score_text),
                      product_url=VALUES(product_url)
                    """,
                    batch,
                )
                inserted_items = len(batch)
    return inserted_items


def query_coupang_latest_ranked_items(keyword_text: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    해당 키워드로 가장 최근 저장된 스모크/크롤 run의 순위별 상품 행을 반환한다 (기본 최대 10개).
    """
    kw = str(keyword_text or "").strip()[:255]
    if not kw:
        return []
    lim = max(1, min(int(limit), 50))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.rank_no, i.product_title, i.price_text, i.shipping_text,
                       i.review_count_text, i.review_score_text, i.product_url
                FROM coupang_search_ranked_items i
                INNER JOIN (
                    SELECT id FROM coupang_search_runs
                    WHERE keyword_text = %s
                    ORDER BY collected_at DESC
                    LIMIT 1
                ) latest ON i.run_id = latest.id
                WHERE i.rank_no <= %s
                ORDER BY i.rank_no ASC
                """,
                (kw, lim),
            )
            rows = cur.fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "rank": int(row[0] or 0),
                "title": row[1] or "",
                "price": row[2] or "",
                "shipping": row[3] or "",
                "review_count": row[4] or "",
                "review_score": row[5] or "",
                "url": row[6] or "",
            }
        )
    return out


def create_insight_discovery_run(
    seed_keyword: str,
    shopping_category_path: Optional[str],
    datalab_category_id: Optional[int],
    period_start: date,
    period_end: date,
    *,
    status: str = "SUCCESS",
    note: str = "",
) -> Dict[str, Any]:
    """인사이트 파이프라인 실행 1건 메타 저장."""
    run_token = uuid.uuid4().hex[:24]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO insight_discovery_runs
                (run_token, seed_keyword, shopping_category_path, datalab_category_id,
                 datalab_period_start, datalab_period_end, status, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_token,
                    str(seed_keyword)[:255],
                    shopping_category_path,
                    int(datalab_category_id) if datalab_category_id is not None else None,
                    period_start,
                    period_end,
                    str(status)[:20],
                    (note[:65000] if note else None),
                ),
            )
            run_id = int(cur.lastrowid)
    return {"id": run_id, "run_token": run_token}


def insert_insight_discovery_keywords(run_id: int, rows: Iterable[Dict[str, Any]]) -> int:
    batch: List[tuple] = []
    for r in rows:
        batch.append(
            (
                int(run_id),
                str(r.get("row_kind", "INSIGHT"))[:16],
                int(r["insight_rank"]) if r.get("insight_rank") is not None else None,
                str(r.get("keyword_text", ""))[:255],
                int(r.get("mobile_monthly_qc", 0) or 0),
                float(r.get("mobile_monthly_clk", 0.0) or 0.0),
                float(r.get("ctr_pct", 0.0) or 0.0),
                int(r.get("product_count", 0) or 0),
                float(r.get("market_fit_score", 0.0) or 0.0),
                float(r["vs_seed_volume_ratio"]) if r.get("vs_seed_volume_ratio") is not None else None,
                float(r["vs_seed_click_ratio"]) if r.get("vs_seed_click_ratio") is not None else None,
            )
        )
    if not batch:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO insight_discovery_keywords
                (run_id, row_kind, insight_rank, keyword_text, mobile_monthly_qc, mobile_monthly_clk,
                 ctr_pct, product_count, market_fit_score, vs_seed_volume_ratio, vs_seed_click_ratio)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                batch,
            )
    return len(batch)


def query_insight_discovery_rows(limit: int = 100) -> List[Dict[str, Any]]:
    """대시보드용: 최근 인사이트 파이프라인 키워드 행."""
    lim = max(1, min(int(limit), 500))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.created_at, r.seed_keyword, r.shopping_category_path, r.datalab_category_id,
                       k.row_kind, k.insight_rank, k.keyword_text,
                       k.mobile_monthly_qc, k.mobile_monthly_clk, k.ctr_pct, k.product_count,
                       k.market_fit_score, k.vs_seed_volume_ratio, k.vs_seed_click_ratio
                FROM insight_discovery_runs r
                INNER JOIN insight_discovery_keywords k ON k.run_id = r.id
                ORDER BY r.created_at DESC,
                         CASE k.row_kind WHEN 'SEED' THEN 0 ELSE 1 END,
                         COALESCE(k.insight_rank, 9999) ASC
                LIMIT %s
                """,
                (lim,),
            )
            raw = cur.fetchall()
    out: List[Dict[str, Any]] = []
    for row in raw:
        out.append(
            {
                "created_at": row[0],
                "seed_keyword": row[1],
                "shopping_category_path": row[2],
                "datalab_category_id": row[3],
                "row_kind": row[4],
                "insight_rank": row[5],
                "keyword_text": row[6],
                "mobile_monthly_qc": int(row[7] or 0),
                "mobile_monthly_clk": float(row[8] or 0),
                "ctr_pct": float(row[9] or 0),
                "product_count": int(row[10] or 0),
                "market_fit_score": float(row[11] or 0),
                "vs_seed_volume_ratio": float(row[12]) if row[12] is not None else None,
                "vs_seed_click_ratio": float(row[13]) if row[13] is not None else None,
            }
        )
    return out

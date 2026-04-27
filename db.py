from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional
from urllib.parse import unquote, urlparse

import pymysql


def _dsn_from_env() -> Dict[str, Any]:
    url = (
        os.environ.get("MYSQL_URL")
        or os.environ.get("MARIADB_URL")
        or os.environ.get("DATABASE_URL")
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
            "Set MYSQL_URL/MARIADB_URL or host/user vars "
            "(MARIADB_HOST+MARIADB_USER, MYSQLHOST+MYSQLUSER)."
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
    schema_file = Path(__file__).resolve().parent / "sql" / "001_blue_ocean_mariadb.sql"
    sql_text = schema_file.read_text(encoding="utf-8")
    statements = [s.strip() for s in sql_text.split(";") if s.strip()]
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
                     avg_ctr_pct, product_count, blue_ocean_score, strategy_text)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      monthly_search_volume_est=VALUES(monthly_search_volume_est),
                      monthly_click_est=VALUES(monthly_click_est),
                      avg_ctr_pct=VALUES(avg_ctr_pct),
                      product_count=VALUES(product_count),
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
                       km.product_count, km.blue_ocean_score, km.strategy_text
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
                "blue_ocean_score": float(r[7]),
                "strategy_text": r[8] or "",
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
                       product_count, blue_ocean_score, strategy_text
                FROM (
                    SELECT ar.started_at,
                           km.seed_keyword, km.keyword_text,
                           km.monthly_search_volume_est, km.monthly_click_est, km.avg_ctr_pct,
                           km.product_count, km.blue_ocean_score, km.strategy_text,
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
                "blue_ocean_score": float(r[7]),
                "strategy_text": r[8] or "",
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
                       km.product_count, km.blue_ocean_score, km.strategy_text
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
        "blue_ocean_score": float(row[5]),
        "strategy_text": row[6] or "",
        "trends": trends,
    }

import os
import requests
import pandas as pd
from datetime import datetime, timedelta
import time
import sys
import subprocess
import threading
import queue
import socket
from collections import Counter
try:
    from tkinter import messagebox
except Exception:
    messagebox = None
import hmac
import hashlib
import base64
import json
import math
from typing import Any, Dict, List, Optional, Tuple

from report_format import finalize_analysis_dataframe
from coupang_crawler import get_shared_crawler
from ai_pipeline import AIPipeline

try:
    import customtkinter as ctk
except Exception:
    ctk = None

try:
    from db import (
        create_run,
        ensure_schema,
        finish_run,
        insert_keyword_metrics,
        insert_keyword_evaluations,
        insert_ai_insights,
        insert_ai_pipeline_logs,
        insert_monthly_trends,
        query_recent_keyword_cache,
        query_top_keywords,
    )
except Exception:
    create_run = None
    ensure_schema = None
    finish_run = None
    insert_keyword_metrics = None
    insert_keyword_evaluations = None
    insert_ai_insights = None
    insert_ai_pipeline_logs = None
    insert_monthly_trends = None
    query_recent_keyword_cache = None
    query_top_keywords = None

def resource_path(relative_path: str) -> str:
    """
    PyInstaller(onefile) 실행 시 번들 내부에서 리소스를 찾기 위한 경로 계산.
    """
    if os.path.isabs(relative_path):
        return relative_path
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def apply_db_cfg_dict_to_env(db_cfg: Any) -> None:
    """
    database 항목(dict)을 os.environ에 반영한다. 이미 설정된 환경변수는 덮어쓰지 않는다.
    """
    if not isinstance(db_cfg, dict):
        return

    mysql_url = str(db_cfg.get("mysql_url", "") or db_cfg.get("mariadb_url", "")).strip()
    mysql_public_url = str(db_cfg.get("mysql_public_url", "") or "").strip()
    mariadb_public_url = str(db_cfg.get("mariadb_public_url", "") or "").strip()
    if mysql_public_url and not (os.environ.get("MYSQL_PUBLIC_URL") or "").strip():
        os.environ["MYSQL_PUBLIC_URL"] = mysql_public_url
    if mariadb_public_url and not (os.environ.get("MARIADB_PUBLIC_URL") or "").strip():
        os.environ["MARIADB_PUBLIC_URL"] = mariadb_public_url
    if mysql_url and not (
        (os.environ.get("MYSQL_URL") or "").strip()
        or (os.environ.get("MYSQL_PUBLIC_URL") or "").strip()
        or (os.environ.get("MARIADB_PUBLIC_URL") or "").strip()
        or (os.environ.get("MARIADB_URL") or "").strip()
        or (os.environ.get("DATABASE_URL") or "").strip()
        or (os.environ.get("DATABASE_PUBLIC_URL") or "").strip()
    ):
        os.environ["MYSQL_URL"] = mysql_url

    mapping = {
        "host": "MARIADB_HOST",
        "port": "MARIADB_PORT",
        "user": "MARIADB_USER",
        "password": "MARIADB_PASSWORD",
        "database": "MARIADB_DATABASE",
    }
    for key, env_name in mapping.items():
        value = str(db_cfg.get(key, "")).strip()
        if value and not (os.environ.get(env_name) or "").strip():
            os.environ[env_name] = value


def apply_database_env_from_config(config_path: str = "config.json") -> None:
    """
    config.json + config.local.json의 database 를 합쳐 환경변수로 올린다.
    같은 키는 config.local.json 이 우선. 이미 설정된 env 는 덮어쓰지 않는다.
    로컬 DB URL은 git 에 안 올리는 config.local.json 에만 두면 된다.
    """
    merged_db: Dict[str, Any] = {}
    for filename in (config_path, "config.local.json"):
        resolved = resource_path(filename)
        if not os.path.isfile(resolved):
            continue
        try:
            with open(resolved, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            continue
        db = cfg.get("database") or {}
        if isinstance(db, dict):
            merged_db.update(db)
    apply_db_cfg_dict_to_env(merged_db)


class NaverSearchAdsAPI:
    def __init__(self, config):
        self.access_license = config['access_license']
        self.secret_key = config['secret_key']
        self.customer_id = config['customer_id']
        self.base_url = "https://api.naver.com"

    def generate_signature(self, timestamp, method, uri):
        message = f"{timestamp}.{method}.{uri}"
        hash = hmac.new(bytes(self.secret_key, "utf-8"), bytes(message, "utf-8"), hashlib.sha256)
        return base64.b64encode(hash.digest()).decode("utf-8")

    def get_header(self, method, uri):
        timestamp = str(int(time.time() * 1000))
        signature = self.generate_signature(timestamp, method, uri)
        return {
            "Content-Type": "application/json; charset=UTF-8",
            "X-Timestamp": timestamp,
            "X-API-KEY": self.access_license,
            "X-Customer": str(self.customer_id),
            "X-Signature": signature
        }

    def get_related_keywords(self, seed_keyword):
        """주제어 하나로 최대 1,000개의 연관 키워드를 자동 발굴"""
        uri = "/keywordstool"
        method = "GET"
        params = {"hintKeywords": seed_keyword.replace(" ", ""), "showDetail": "1"}
        try:
            res = requests.get(f"{self.base_url}{uri}", params=params, headers=self.get_header(method, uri))
            if res.status_code == 200:
                return res.json().get("keywordList", [])
            return []
        except: return []

class BlueOceanTool:
    def __init__(self, config_path="config.json"):
        resolved_config_path = resource_path(config_path)
        with open(resolved_config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)
        
        self.open_headers = {
            "X-Naver-Client-Id": self.config['naver_open_api']['client_id'],
            "X-Naver-Client-Secret": self.config['naver_open_api']['client_secret'],
            "Content-Type": "application/json"
        }
        self.ads_api = NaverSearchAdsAPI(self.config['naver_ads_api'])
        self.coupang_crawler = get_shared_crawler()
        self.output_dir = self.config['settings']['output_dir']
        self.save_excel = bool(self.config.get("settings", {}).get("save_excel", False))
        self.db_enabled = bool(self.config.get("settings", {}).get("db_enabled", True))
        self.ai_insight_enabled = str(os.getenv("AI_INSIGHT_ENABLED", "true")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._apply_db_env_from_config()
        self.ai_pipeline = AIPipeline(base_dir=os.path.dirname(os.path.abspath(__file__)))
        
        # 출력 폴더 생성
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir, exist_ok=True)
        self.report_dir = os.path.join(self.output_dir, "reports")
        if not os.path.exists(self.report_dir):
            os.makedirs(self.report_dir, exist_ok=True)

        if self.db_enabled:
            if ensure_schema is None:
                raise RuntimeError("DB 모듈(db.py) 로드 실패: MariaDB 저장을 사용할 수 없습니다.")
            try:
                ensure_schema()
            except Exception as e:
                # DB 설정이 없어도 GUI 사용은 가능해야 하므로 저장 기능만 비활성화
                self.db_enabled = False
                print(f"[WARN] DB 연결 설정 누락/실패로 DB 저장을 비활성화합니다: {e}")

    def _apply_db_env_from_config(self):
        """
        config.json의 database 항목을 환경변수로 보강한다.
        기존 환경변수가 이미 있으면 덮어쓰지 않는다.
        """
        apply_db_cfg_dict_to_env(self.config.get("database") or {})

    def get_monthly_trends(self, keyword, start_date, end_date):
        """요청된 기간의 월별 트렌드 지수 수집"""
        url = "https://openapi.naver.com/v1/datalab/shopping/category/keywords"
        # 키워드별로 카테고리 매핑이 복잡하므로, 가장 범용적인 '생활/건강' 카테고리(50000008) 기준
        body = {
            "startDate": start_date, "endDate": end_date, "timeUnit": "month",
            "category": "50000008", "device": "mo", "ages": [], "gender": "",
            "keyword": [{"name": keyword, "param": [keyword]}]
        }
        try:
            res = requests.post(url, headers=self.open_headers, json=body)
            if res.status_code == 200:
                data = res.json().get('results', [])
                if data and data[0].get('data'):
                    return { d['period']: d['ratio'] for d in data[0]['data'] }
            return {}
        except: return {}

    def get_product_info(self, keyword) -> tuple[int, str]:
        url = f"https://openapi.naver.com/v1/search/shop.json?query={keyword}&display=1"
        try:
            res = requests.get(url, headers=self.open_headers)
            if res.status_code == 200:
                payload = res.json()
                total = int(payload.get("total", 0) or 0)
                items = payload.get("items", []) or []
                if items:
                    it = items[0]
                    cats = [
                        str(it.get("category1", "")).strip(),
                        str(it.get("category2", "")).strip(),
                        str(it.get("category3", "")).strip(),
                        str(it.get("category4", "")).strip(),
                    ]
                    cat_path = " > ".join([c for c in cats if c])
                else:
                    cat_path = ""
                return total, cat_path
            return 0, ""
        except Exception:
            return 0, ""

    def get_product_count(self, keyword):
        total, _ = self.get_product_info(keyword)
        return total

    def get_coupang_top10_stats(self, keyword: str) -> tuple[Optional[float], Optional[float]]:
        try:
            data = self.coupang_crawler.crawl_coupang(keyword)
            avg_reviews = float(data.get("avg_reviews", 0.0) or 0.0)
            avg_price = float(data.get("avg_price", 0.0) or 0.0)
            if avg_reviews <= 0 or avg_price <= 0:
                return None, None
            return avg_reviews, avg_price
        except Exception:
            return None, None

    def _strategy_text(self, blue_ocean_score: float) -> str:
        if blue_ocean_score >= 70:
            return "강력 추천 황금 키워드! 수요 대비 경쟁자가 매우 적습니다."
        if blue_ocean_score >= 40:
            return "유망 키워드. 썸네일과 상세페이지 차별화 추천."
        return "경쟁이 있으나 틈새 시장 공략이 가능합니다."

    def _compute_growth_factor(self, est_vols_qc: List[float]) -> float:
        """
        검색량 추세 상승 가중치.
        - 최근 3개월 평균 / 이전 3개월 평균 비율을 사용
        - 과도한 튐을 막기 위해 0.7~1.8 범위로 클램프
        """
        if not est_vols_qc:
            return 1.0
        if len(est_vols_qc) < 4:
            return 1.0

        recent = est_vols_qc[-3:]
        prev = est_vols_qc[:-3] if len(est_vols_qc) > 3 else est_vols_qc[:1]
        recent_avg = sum(recent) / max(1, len(recent))
        prev_avg = sum(prev) / max(1, len(prev))

        if prev_avg <= 0:
            growth = 1.2 if recent_avg > 0 else 1.0
        else:
            growth = recent_avg / prev_avg

        return max(0.7, min(1.8, float(growth)))

    def _compute_commercial_score(
        self,
        *,
        monthly_click_est: float,
        avg_ctr_pct: float,
        top10_avg_reviews: Optional[float],
        top10_avg_price: Optional[float],
        product_count: int,
    ) -> float:
        click_part = min(100.0, math.log1p(max(0.0, monthly_click_est)) * 14.5)
        ctr_part = min(100.0, max(0.0, avg_ctr_pct) * 6.0)
        if top10_avg_reviews is not None and top10_avg_price is not None and top10_avg_price > 0:
            review_price_ratio = math.log1p(max(0.0, top10_avg_reviews)) / math.log1p(max(1.0, top10_avg_price))
            conversion_part = min(100.0, max(0.0, review_price_ratio) * 65.0)
        else:
            conversion_part = min(100.0, click_part * 0.55 + ctr_part * 0.45)
        competition_penalty = min(35.0, math.log1p(max(0, product_count)) * 2.6)
        commercial = (conversion_part * 0.65 + click_part * 0.2 + ctr_part * 0.15) - competition_penalty
        return round(max(0.0, min(100.0, commercial)), 2)

    def _decision_band(self, final_score: float) -> str:
        if final_score >= 70:
            return "GO"
        if final_score >= 45:
            return "WATCH"
        return "DROP"

    def _build_evaluation_rows(
        self,
        all_results: List[Dict],
        *,
        start_date: str,
        end_date: str,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for r in all_results:
            opportunity_score = float(r.get("블루오션 점수", 0.0) or 0.0)
            commercial_score = self._compute_commercial_score(
                monthly_click_est=float(r.get("월평균 클릭수(추정)", 0.0) or 0.0),
                avg_ctr_pct=float(str(r.get("평균 클릭율(CTR)", "0")).replace("%", "") or 0.0),
                top10_avg_reviews=(
                    float(r.get("쿠팡 Top10 평균리뷰수"))
                    if r.get("쿠팡 Top10 평균리뷰수") is not None
                    else None
                ),
                top10_avg_price=(
                    float(r.get("쿠팡 Top10 평균가격"))
                    if r.get("쿠팡 Top10 평균가격") is not None
                    else None
                ),
                product_count=int(r.get("상품수", 0) or 0),
            )
            final_score = round(opportunity_score * 0.45 + commercial_score * 0.55, 2)
            decision_band = self._decision_band(final_score)
            rows.append(
                {
                    "seed_keyword": r.get("주제어", ""),
                    "keyword_text": r.get("키워드", ""),
                    "opportunity_score": round(opportunity_score, 2),
                    "commercial_score": commercial_score,
                    "final_score": final_score,
                    "decision_band": decision_band,
                    "date_range": f"{start_date}:{end_date}",
                    "monthly_search_volume_est": int(r.get("월평균 검색수(추정)", 0) or 0),
                    "monthly_click_est": float(r.get("월평균 클릭수(추정)", 0.0) or 0.0),
                    "avg_ctr_pct": float(str(r.get("평균 클릭율(CTR)", "0")).replace("%", "") or 0.0),
                    "product_count": int(r.get("상품수", 0) or 0),
                    "trend_score": float(r.get("트렌드 점수", 1.0) or 1.0),
                    "top10_avg_reviews": r.get("쿠팡 Top10 평균리뷰수"),
                    "top10_avg_price": r.get("쿠팡 Top10 평균가격"),
                }
            )
        return rows

    def _persist_to_db(
        self,
        all_results: List[Dict],
        trends_by_keyword: Dict[str, Dict],
        evaluation_rows: List[Dict[str, Any]],
        seeds,
        start_date,
        end_date,
        log,
    ) -> int:
        if not self.db_enabled:
            return 0
        if create_run is None:
            raise RuntimeError("DB 함수 로드 실패로 저장할 수 없습니다.")

        run = create_run(
            seed_keywords_raw=",".join([str(s).strip() for s in seeds if str(s).strip()]),
            start_date=datetime.strptime(start_date, "%Y-%m-%d").date(),
            end_date=datetime.strptime(end_date, "%Y-%m-%d").date(),
        )
        run_id = int(run["id"])
        try:
            metric_rows = []
            for r in all_results:
                ctr_str = str(r.get("평균 클릭율(CTR)", "0")).replace("%", "").strip()
                metric_rows.append(
                    {
                        "seed_keyword": r.get("주제어", ""),
                        "keyword_text": r.get("키워드", ""),
                        "monthly_search_volume_est": int(r.get("월평균 검색수(추정)", 0) or 0),
                        "monthly_click_est": float(r.get("월평균 클릭수(추정)", 0.0) or 0.0),
                        "avg_ctr_pct": float(ctr_str or 0.0),
                        "product_count": int(r.get("상품수", 0) or 0),
                        "top10_avg_reviews": (
                            float(r.get("쿠팡 Top10 평균리뷰수"))
                            if r.get("쿠팡 Top10 평균리뷰수") is not None
                            else None
                        ),
                        "top10_avg_price": (
                            float(r.get("쿠팡 Top10 평균가격"))
                            if r.get("쿠팡 Top10 평균가격") is not None
                            else None
                        ),
                        "blue_ocean_score": float(r.get("블루오션 점수", 0.0) or 0.0),
                        "strategy_text": self._strategy_text(float(r.get("블루오션 점수", 0.0) or 0.0)),
                    }
                )

            metric_refs = insert_keyword_metrics(run_id, metric_rows)
            kw_to_metric = {m["keyword_text"]: int(m["metric_id"]) for m in metric_refs}

            trend_inserted = 0
            for kw, trends in trends_by_keyword.items():
                metric_id = kw_to_metric.get(kw)
                if not metric_id or not trends:
                    continue
                trend_rows = []
                for month, payload in trends.items():
                    trend_rows.append(
                        {
                            "trend_month": str(month)[:7],
                            "ratio_value": float(payload.get("ratio", 0.0) or 0.0),
                            "est_search_volume": int(payload.get("est_search_volume", 0) or 0),
                            "est_click_volume": float(payload.get("est_click_volume", 0.0) or 0.0),
                        }
                    )
                trend_inserted += insert_monthly_trends(metric_id, trend_rows)

            eval_inserted = 0
            insight_inserted = 0
            pipeline_log_inserted = 0
            if insert_keyword_evaluations is not None and evaluation_rows:
                eval_payload = []
                for e in evaluation_rows:
                    metric_id = kw_to_metric.get(str(e.get("keyword_text", "")), 0)
                    if not metric_id:
                        continue
                    eval_payload.append(
                        {
                            "metric_id": int(metric_id),
                            "opportunity_score": float(e.get("opportunity_score", 0.0) or 0.0),
                            "commercial_score": float(e.get("commercial_score", 0.0) or 0.0),
                            "final_score": float(e.get("final_score", 0.0) or 0.0),
                            "decision_band": str(e.get("decision_band", "WATCH")),
                        }
                    )
                eval_refs = insert_keyword_evaluations(run_id, eval_payload)
                eval_inserted = len(eval_refs)

                if self.ai_insight_enabled and insert_ai_insights is not None and insert_ai_pipeline_logs is not None:
                    memory_rows = []
                    for e in evaluation_rows:
                        payload = dict(e)
                        payload["run_id"] = run_id
                        insight = self.ai_pipeline.generate_insight(payload)
                        metric_id = kw_to_metric.get(str(e.get("keyword_text", "")), 0)
                        if not metric_id:
                            continue
                        insert_ai_insights(
                            [
                                {
                                    "run_id": run_id,
                                    "metric_id": int(metric_id),
                                    "keyword_text": str(e.get("keyword_text", "")),
                                    "summary_text": str(insight.get("summary", "")),
                                    "action_text": str(insight.get("action", "")),
                                    "risk_text": "\n".join(insight.get("risks", []) or []),
                                    "evidence_json": insight.get("evidence", []),
                                    "confidence_score": float(insight.get("confidence", 0.0) or 0.0),
                                    "model_version": str(insight.get("model_version", "rule-based-v1")),
                                    "token_usage_est": int(insight.get("token_usage_est", 0) or 0),
                                    "cache_hit": bool(insight.get("cache_hit", False)),
                                }
                            ]
                        )
                        insight_inserted += 1
                        pipeline_logs = insight.get("pipeline_logs", []) or []
                        if pipeline_logs:
                            insert_ai_pipeline_logs(
                                run_id,
                                int(metric_id),
                                [
                                    {
                                        "node_name": str(l.get("node_name", "node")),
                                        "status": str(l.get("status", "SUCCESS")),
                                        "latency_ms": int(l.get("latency_ms", 0) or 0),
                                        "token_usage_est": int(l.get("token_usage_est", 0) or 0),
                                        "meta_json": l.get("meta_json"),
                                    }
                                    for l in pipeline_logs
                                ],
                            )
                            pipeline_log_inserted += len(pipeline_logs)
                        memory_rows.append(
                            {
                                "run_id": run_id,
                                "seed_keyword": e.get("seed_keyword", ""),
                                "keyword_text": e.get("keyword_text", ""),
                                "opportunity_score": e.get("opportunity_score", 0.0),
                                "commercial_score": e.get("commercial_score", 0.0),
                                "final_score": e.get("final_score", 0.0),
                                "decision_band": e.get("decision_band", "WATCH"),
                            }
                        )
                    self.ai_pipeline.upsert_case_memory(memory_rows)

            finish_run(run_id, success=True, result_count=len(metric_rows))
            log(
                f"🗄️ DB 저장 완료: run_id={run_id}, metrics={len(metric_rows)}건, "
                f"trends={trend_inserted}건, evals={eval_inserted}건, insights={insight_inserted}건, "
                f"pipeline_logs={pipeline_log_inserted}건"
            )
            return run_id
        except Exception as e:
            finish_run(run_id, success=False, result_count=0, error_message=str(e))
            raise

    def start_analysis(
        self,
        seeds=None,
        start_date=None,
        end_date=None,
        analysis_mode: str = "precise",
        log_callback=None,
    ) -> Tuple[Optional[str], Optional[pd.DataFrame]]:
        """
        GUI에서 실행하기 위한 분석 함수.
        - seeds/start_date/end_date가 없으면 기존처럼 console input을 사용합니다.
        - log_callback(msg) 를 주면 print 대신 콜백으로 메시지를 전달합니다.
        - 반환: (요약 문자열 | None, 모디바 엑셀 템플릿 형식 DataFrame | None)
        """
        def log(msg=""):
            if log_callback is not None:
                log_callback(str(msg))
            else:
                print(msg)

        # 헤더
        log("\n" + "=" * 50)
        log(" 🚀 [친절한 모디바] 블루오션 자동 탐색 툴 가동")
        log("=" * 50)

        # 입력 받기(없으면 콘솔 input 유지)
        if seeds is None:
            seeds = input("\n[1] 분석하실 주제어(시드 키워드)를 입력하세요 (예: 캠핑용품, 주방용품): ").split(",")
        elif isinstance(seeds, str):
            seeds = seeds.split(",")

        if start_date is None:
            start_date = input("[2] 분석 시작일 (YYYY-MM-DD): ")
        if end_date is None:
            end_date = input("[3] 분석 종료일 (YYYY-MM-DD): ")

        # 날짜 검증(문자열로 Naver API에 전달하므로 형식만 확인)
        try:
            sd = datetime.strptime(start_date, "%Y-%m-%d")
            ed = datetime.strptime(end_date, "%Y-%m-%d")
            if sd > ed:
                raise ValueError("시작일이 종료일보다 클 수 없습니다.")
        except Exception as e:
            raise ValueError(f"날짜 형식이 올바르지 않습니다. ({e})")

        log("\n✅ 설정을 확인했습니다. 엔진을 구동합니다...")
        all_results = []
        trends_by_keyword: Dict[str, Dict[str, Dict[str, float]]] = {}
        mode = str(analysis_mode or "precise").strip().lower()
        is_fast_mode = mode in {"fast", "quick", "빠른", "빠른모드"}
        deep_limit = 20 if is_fast_mode else 60
        trend_limit = 20
        cache_ttl_hours = 24
        parsed_start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
        parsed_end_date = datetime.strptime(end_date, "%Y-%m-%d").date()
        product_info_cache: Dict[str, tuple[int, str]] = {}
        trends_cache: Dict[str, Dict[str, float]] = {}

        def clean_val(v):
            if isinstance(v, str) and "<" in v:
                return 5.0
            try:
                return float(v) if v is not None else 0.0
            except Exception:
                return 0.0

        def normalize_keyword(text: str) -> str:
            return "".join(str(text or "").split()).lower()

        def cached_product_info(keyword: str) -> tuple[int, str]:
            key = normalize_keyword(keyword)
            if key in product_info_cache:
                return product_info_cache[key]
            info = self.get_product_info(keyword)
            product_info_cache[key] = info
            return info

        def cached_monthly_trends(keyword: str) -> Dict[str, float]:
            cache_key = f"{normalize_keyword(keyword)}::{start_date}::{end_date}"
            if cache_key in trends_cache:
                return trends_cache[cache_key]
            tr = self.get_monthly_trends(keyword, start_date, end_date)
            trends_cache[cache_key] = tr
            return tr

        for seed in seeds:
            seed = str(seed).strip()
            if not seed:
                continue

            log(f"\n▶ 주제어 [{seed}] 관련 키워드 발굴 중...")
            raw_keywords = self.ads_api.get_related_keywords(seed)
            if not raw_keywords:
                log(f"❌ [{seed}] 관련 키워드를 찾지 못했습니다.")
                continue

            candidate_keywords = raw_keywords
            discovered_category_counter: Counter[str] = Counter()
            # 1차 선별: 모바일 클릭수 70% + 모바일 검색수 30%
            ranked_candidates = sorted(
                raw_keywords,
                key=lambda x: (
                    clean_val(x.get("monthlyAveMobileClkCnt")) * 0.7
                    + clean_val(x.get("monthlyMobileQcCnt")) * 0.3
                ),
                reverse=True,
            )
            candidate_keywords = ranked_candidates[:deep_limit]
            mode_label = "빠른 모드" if is_fast_mode else "정밀 모드"
            log(
                f"ㄴ {mode_label}: {len(raw_keywords)}개 중 상위 {len(candidate_keywords)}개 정밀 분석 (저경쟁/상승추세 우선)"
            )

            # 동일 의미 키워드 중복 제거(공백/대소문자 차이 제거)
            deduped_candidates = []
            seen_kw = set()
            for item in candidate_keywords:
                norm_kw = normalize_keyword(item.get("relKeyword", ""))
                if not norm_kw or norm_kw in seen_kw:
                    continue
                seen_kw.add(norm_kw)
                deduped_candidates.append(item)

            if is_fast_mode:
                log(f"ㄴ 중복 제거 후 {len(deduped_candidates)}개 후보 분석")

            for idx, item in enumerate(deduped_candidates):
                kw = item['relKeyword']
                use_trend_api = idx < trend_limit

                mo_qc = clean_val(item.get("monthlyMobileQcCnt"))
                total_qc = mo_qc

                # 최소 검색량 필터링 (가독성을 위해 500회 이상만)
                if total_qc < 500:
                    continue

                total_clk = clean_val(item.get("monthlyAveMobileClkCnt"))

                # 빠른 모드 추가 조기탈락: 클릭 추정이 너무 낮은 키워드 제외
                if is_fast_mode and total_clk < 10:
                    continue

                cache_hit = None
                trend_score = 1.0
                demand_score = 0.0
                conversion_score = 0.0
                competition_score = 1.0
                if self.db_enabled and query_recent_keyword_cache is not None:
                    try:
                        cache_hit = query_recent_keyword_cache(
                            kw,
                            start_date=parsed_start_date,
                            end_date=parsed_end_date,
                            ttl_hours=cache_ttl_hours,
                        )
                    except Exception:
                        cache_hit = None

                if cache_hit is not None:
                    avg_qc = float(cache_hit.get("monthly_search_volume_est", 0))
                    avg_clk = float(cache_hit.get("monthly_click_est", 0.0))
                    avg_ctr = float(cache_hit.get("avg_ctr_pct", 0.0))
                    prod_count = int(cache_hit.get("product_count", 0))
                    competition_score = max(1.0, math.log1p(max(0, prod_count)))
                    demand_score = math.log1p(max(0.0, avg_qc))
                    conversion_score = max(0.1, math.log1p(max(0.0, avg_clk)) * (1.0 + max(0.0, avg_ctr) / 100.0))
                    raw_score = (demand_score * conversion_score / competition_score) * 100.0
                    kw_trends = cache_hit.get("trends", {}) or {}
                    top10_avg_reviews = cache_hit.get("top10_avg_reviews")
                    top10_avg_price = cache_hit.get("top10_avg_price")
                    if kw_trends:
                        trend_vols = [
                            float(v.get("est_search_volume", 0.0) or 0.0)
                            for _, v in sorted(kw_trends.items(), key=lambda x: x[0])
                        ]
                        trend_score = self._compute_growth_factor(trend_vols) if trend_vols else 1.0
                        trends_by_keyword[kw] = kw_trends
                else:
                    # 쇼핑 상품 수/카테고리 조회
                    prod_count, kw_category = cached_product_info(kw)
                    time.sleep(0.02)

                    # 주제어 기반이 아닌, 실행 중 새롭게 탐색된 카테고리를 우선 기준으로 축적
                    if kw_category:
                        kw_top = " > ".join(kw_category.split(" > ")[:2])
                        if kw_top:
                            discovered_category_counter[kw_top] += 1

                    # 블루오션 점수 (최신 한 달 기준 1차 필터링)
                    safe_total = prod_count if prod_count > 0 else 1
                    blue_ocean_score = (total_clk / safe_total) * 10000

                    trends = cached_monthly_trends(kw) if use_trend_api else {}
                    if trends:
                        # 5개월 역산 로직 적용
                        recent_ratio = list(trends.values())[-1] if list(trends.values())[-1] > 0 else 1.0
                        scale_vector = total_qc / recent_ratio
                        scale_clk_vector = total_clk / recent_ratio

                        est_vols_qc = [r * scale_vector for r in trends.values()]
                        est_vols_clk = [r * scale_clk_vector for r in trends.values()]

                        avg_qc = sum(est_vols_qc) / len(trends)
                        avg_clk = sum(est_vols_clk) / len(trends)
                        avg_ctr = (sum(est_vols_clk) / sum(est_vols_qc) * 100) if sum(est_vols_qc) > 0 else 0

                        # 블루오션 핵심: 저경쟁(상품수) + 수요수준(검색/CTR) + 상승추세
                        trend_score = self._compute_growth_factor(est_vols_qc)
                        competition_score = max(1.0, math.log1p(max(0, prod_count)))
                        demand_score = math.log1p(max(0.0, avg_qc))
                        conversion_score = max(
                            0.1, math.log1p(max(0.0, avg_clk)) * (1.0 + max(0.0, avg_ctr) / 100.0)
                        )
                        raw_score = (demand_score * trend_score * conversion_score / competition_score) * 100.0

                        trends_by_keyword[kw] = {
                            m: {
                                "ratio": float(ratio),
                                "est_search_volume": int(round(ratio * scale_vector)),
                                "est_click_volume": float(round(ratio * scale_clk_vector, 1)),
                            }
                            for m, ratio in trends.items()
                        }
                    else:
                        # 트렌드 호출 생략/빈값 시 현재 월 지표로 계산
                        avg_qc = total_qc
                        avg_clk = total_clk
                        avg_ctr = (total_clk / total_qc * 100) if total_qc > 0 else 0
                        competition_score = max(1.0, math.log1p(max(0, prod_count)))
                        demand_score = math.log1p(max(0.0, avg_qc))
                        conversion_score = max(
                            0.1, math.log1p(max(0.0, avg_clk)) * (1.0 + max(0.0, avg_ctr) / 100.0)
                        )
                        raw_score = (demand_score * conversion_score / competition_score) * 100.0

                    top10_avg_reviews, top10_avg_price = self.get_coupang_top10_stats(kw)
                    if top10_avg_reviews is not None and top10_avg_price is not None and top10_avg_price > 0:
                        conversion_score = max(
                            0.1, math.log1p(max(0.0, top10_avg_reviews)) / math.log1p(max(1.0, top10_avg_price))
                        )

                all_results.append({
                    "주제어": seed,
                    "키워드": kw,
                    "월평균 검색수(추정)": round(avg_qc),
                    "월평균 클릭수(추정)": round(avg_clk, 1),
                    "평균 클릭율(CTR)": f"{round(avg_ctr, 2)}%",
                    "상품수": prod_count,
                    "쿠팡 Top10 평균리뷰수": top10_avg_reviews,
                    "쿠팡 Top10 평균가격": top10_avg_price,
                    "수요 점수": round(demand_score, 4),
                    "트렌드 점수": round(trend_score, 4),
                    "전환 점수": round(conversion_score, 4),
                    "경쟁 점수": round(competition_score, 4),
                    "_raw_score": float(raw_score),
                    "블루오션 점수": 0.0,
                })

                if idx % 50 == 0:
                    log(f"   - {idx}/{len(deduped_candidates)} 진행 중...")


        if all_results:
            raw_scores = [float(r.get("_raw_score", 0.0) or 0.0) for r in all_results]
            min_score = min(raw_scores) if raw_scores else 0.0
            max_score = max(raw_scores) if raw_scores else 0.0
            for r in all_results:
                rs = float(r.get("_raw_score", 0.0) or 0.0)
                if max_score > min_score:
                    norm_score = ((rs - min_score) / (max_score - min_score)) * 100.0
                else:
                    norm_score = 100.0 if rs > 0 else 0.0
                r["블루오션 점수"] = round(norm_score, 2)
                r.pop("_raw_score", None)

            evaluation_rows = self._build_evaluation_rows(
                all_results,
                start_date=start_date,
                end_date=end_date,
            )
            eval_map = {str(e["keyword_text"]): e for e in evaluation_rows}
            for r in all_results:
                er = eval_map.get(str(r.get("키워드", "")))
                if not er:
                    continue
                r["기회 점수"] = er["opportunity_score"]
                r["판매가치 점수"] = er["commercial_score"]
                r["최종 점수"] = er["final_score"]
                r["판단 밴드"] = er["decision_band"]

            df = (
                pd.DataFrame(all_results)
                .sort_values(by="최종 점수", ascending=False)
                .head(50)
                .sort_values(by="월평균 검색수(추정)", ascending=False)
            )  # 점수 상위 50개 선별 후 모바일 검색수 내림차순 표시

            df["전략 제언"] = df["블루오션 점수"].apply(self._strategy_text)
            report_df = finalize_analysis_dataframe(df)

            # DB 저장(기본)
            run_id = self._persist_to_db(
                all_results,
                trends_by_keyword,
                evaluation_rows,
                seeds,
                start_date,
                end_date,
                log,
            )

            # Excel 저장(옵션)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M")

            def sanitize_filename(text: str) -> str:
                # Windows 파일명에 금지된 문자 제거
                bad_chars = ['\\', '/', ':', '*', '?', '"', '<', '>', '|', '\n', '\r', '\t']
                cleaned = str(text).strip()
                for ch in bad_chars:
                    cleaned = cleaned.replace(ch, '_')
                cleaned = cleaned.replace(' ', '')
                return cleaned[:40] if cleaned else '주제어'

            seeds_for_name = [str(s).strip() for s in seeds if str(s).strip()]
            if len(seeds_for_name) == 1:
                seed_part = sanitize_filename(seeds_for_name[0])
            else:
                seed_part = f"{sanitize_filename(seeds_for_name[0])}_외{len(seeds_for_name)}개"

            filepath = None
            if self.save_excel:
                filename = f"모디바_통합분석리포트_주제어_{seed_part}_{timestamp}.xlsx"
                filepath = os.path.join(self.report_dir, filename)
                report_df.to_excel(filepath, index=False, engine="openpyxl")

            log("\n✨ 분석이 완료되었습니다!")
            try:
                cs = self.coupang_crawler.get_stats()
                success_rate = float(cs.get("selenium_ok", 0)) / max(
                    1, float(cs.get("selenium_ok", 0) + cs.get("failed", 0))
                )
                log(
                    "🧪 쿠팡 크롤링 통계: "
                    f"cache_hit={cs.get('cache_hit', 0)}, "
                    f"requests_ok={cs.get('requests_ok', 0)}, "
                    f"selenium_ok={cs.get('selenium_ok', 0)}, "
                    f"failed={cs.get('failed', 0)}, "
                    f"success_rate={success_rate:.2f}"
                )
            except Exception:
                pass
            if filepath:
                log(f"📁 리포트 저장 위치: {filepath}")
            else:
                log("📁 엑셀 저장은 비활성화되어 DB만 저장했습니다. (settings.save_excel=false)")
            summary = filepath or f"DB run_id={run_id}"
            return summary, report_df

        log("\n❌ 분석 결과가 없습니다. 주제어와 날짜 설정을 확인해주세요.")
        return None, None


class BlueOceanToolGUI:
    def __init__(self, tool: BlueOceanTool):
        if ctk is None:
            raise RuntimeError("customtkinter가 설치되어 있지 않아 GUI를 실행할 수 없습니다.")

        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.tool = tool
        self.app = ctk.CTk()
        self.app.title("블루오션 자동 탐색 툴")
        self.app.geometry("760x860")
        self.app.minsize(700, 760)
        self.level1_options: List[str] = []
        self.level2_map: Dict[str, List[str]] = {}
        self.level3_map: Dict[tuple, List[str]] = {}
        self.level4_map: Dict[tuple, List[str]] = {}
        self.auto_category_seed: Optional[str] = None
        self._load_category_hierarchy()

        # 상태
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.done_queue: "queue.Queue[bool]" = queue.Queue()
        self.worker_thread = None

        # 레이아웃
        root = ctk.CTkFrame(self.app, corner_radius=14)
        root.pack(fill="both", expand=True, padx=16, pady=16)

        title = ctk.CTkLabel(
            root,
            text="블루오션 자동 탐색 툴",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        title.pack(pady=(6, 14))

        ctk.CTkLabel(
            root,
            text="분석 실행",
            anchor="w",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(fill="x", padx=6, pady=(0, 6))
        ctk.CTkLabel(root, text="카테고리 필터 (category_naver.xls 기반)", anchor="w").pack(fill="x", padx=6)
        category_row = ctk.CTkFrame(root, corner_radius=10, fg_color="transparent")
        category_row.pack(fill="x", padx=6, pady=(6, 8))
        category_row.grid_columnconfigure(0, weight=1)
        category_row.grid_columnconfigure(1, weight=1)
        category_row.grid_columnconfigure(2, weight=1)
        category_row.grid_columnconfigure(3, weight=1)

        fallback = ["데이터 없음"]
        l1_values = self.level1_options or fallback
        self.cat_l1_menu = ctk.CTkOptionMenu(category_row, values=l1_values, command=self.on_cat_l1_changed)
        self.cat_l1_menu.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.cat_l2_menu = ctk.CTkOptionMenu(category_row, values=fallback, command=self.on_cat_l2_changed)
        self.cat_l2_menu.grid(row=0, column=1, sticky="ew", padx=4)
        self.cat_l3_menu = ctk.CTkOptionMenu(category_row, values=fallback, command=self.on_cat_l3_changed)
        self.cat_l3_menu.grid(row=0, column=2, sticky="ew", padx=4)
        self.cat_l4_menu = ctk.CTkOptionMenu(category_row, values=fallback, command=self.on_cat_l4_changed)
        self.cat_l4_menu.grid(row=0, column=3, sticky="ew", padx=(4, 0))

        ctk.CTkLabel(root, text="주제어(시드 키워드) - 콤마로 구분", anchor="w").pack(fill="x", padx=6)
        self.seed_entry = ctk.CTkEntry(root, height=38, placeholder_text="예: 캠핑용품, 주방용품")
        self.seed_entry.pack(fill="x", padx=6, pady=(6, 12))
        if self.level1_options:
            self.cat_l1_menu.set(self.level1_options[0])
            self.on_cat_l1_changed(self.level1_options[0])
        else:
            self.cat_l1_menu.set("데이터 없음")

        row = ctk.CTkFrame(root, corner_radius=10)
        row.pack(fill="x", padx=6, pady=(0, 12))

        col1 = ctk.CTkFrame(row, corner_radius=10, fg_color="transparent")
        col1.grid(row=0, column=0, sticky="nsew", padx=(6, 3))
        col2 = ctk.CTkFrame(row, corner_radius=10, fg_color="transparent")
        col2.grid(row=0, column=1, sticky="nsew", padx=(3, 6))
        row.grid_columnconfigure(0, weight=1)
        row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(col1, text="시작일 (YYYY-MM-DD)", anchor="w").pack(fill="x", padx=4, pady=(2, 6))
        self.start_date_entry = ctk.CTkEntry(col1, height=38, placeholder_text="예: 2025-01-01")
        self.start_date_entry.pack(fill="x", padx=4)

        ctk.CTkLabel(col2, text="종료일 (YYYY-MM-DD)", anchor="w").pack(fill="x", padx=4, pady=(2, 6))
        self.end_date_entry = ctk.CTkEntry(col2, height=38, placeholder_text="예: 2025-12-31")
        self.end_date_entry.pack(fill="x", padx=4)

        self.status_label = ctk.CTkLabel(root, text="대기 중...", anchor="w", text_color="gray70")
        self.status_label.pack(fill="x", padx=6, pady=(6, 8))

        btn_row = ctk.CTkFrame(root, corner_radius=10, fg_color="transparent")
        btn_row.pack(fill="x", padx=6, pady=(4, 12))

        self.run_button = ctk.CTkButton(
            btn_row,
            text="실행",
            height=42,
            fg_color=("#1f6feb", "#1f6feb"),
            hover_color="#1557c0",
            command=self.on_run,
        )
        self.run_button.pack(side="left", padx=4, pady=2)

        self.stop_button = ctk.CTkButton(
            btn_row,
            text="닫기",
            height=42,
            fg_color=("gray60", "gray30"),
            hover_color=("gray50", "gray20"),
            command=self.app.destroy,
        )
        self.stop_button.pack(side="right", padx=4, pady=2)

        # DB 조회 패널 (수동 날짜 입력 대신 자동 기간 옵션)
        query_panel = ctk.CTkFrame(root, corner_radius=10)
        query_panel.pack(fill="x", padx=6, pady=(0, 12))

        ctk.CTkLabel(
            query_panel,
            text="DB 최근 실행 결과 조회",
            anchor="w",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(fill="x", padx=8, pady=(8, 4))
        self.query_keyword_entry = ctk.CTkEntry(
            query_panel, height=34, placeholder_text="키워드 필터(선택, 부분일치)"
        )
        self.query_keyword_entry.pack(fill="x", padx=8, pady=(0, 8))

        query_row = ctk.CTkFrame(query_panel, corner_radius=8, fg_color="transparent")
        query_row.pack(fill="x", padx=8, pady=(0, 8))
        query_row.grid_columnconfigure(0, weight=1)
        query_row.grid_columnconfigure(1, weight=1)
        query_row.grid_columnconfigure(2, weight=1)

        self.query_period_menu = ctk.CTkOptionMenu(
            query_row,
            values=["오늘", "최근 7일", "최근 30일", "최근 60일", "최근 120일", "전체"],
        )
        self.query_period_menu.set("최근 7일")
        self.query_period_menu.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.query_limit_menu = ctk.CTkOptionMenu(
            query_row,
            values=["20", "50", "100", "200"],
        )
        self.query_limit_menu.set("50")
        self.query_limit_menu.grid(row=0, column=1, sticky="ew", padx=6)

        self.query_button = ctk.CTkButton(
            query_row,
            text="최근 결과 조회",
            height=34,
            command=self.on_query_recent,
        )
        self.query_button.grid(row=0, column=2, sticky="ew", padx=(6, 0))

        ctk.CTkLabel(root, text="진행 로그", anchor="w").pack(fill="x", padx=6, pady=(2, 6))
        self.log_box = ctk.CTkTextbox(root, corner_radius=10, height=430)
        self.log_box.pack(fill="both", expand=True, padx=6, pady=(0, 10))
        self.log_box.configure(state="disabled")

        self.app.after(100, self._poll_queues)

    def _load_category_hierarchy(self):
        path = resource_path("category_naver.xls")
        if not os.path.exists(path):
            return
        try:
            df = pd.read_excel(path)
            if df.shape[1] < 5:
                return
            category_df = df.iloc[:, 1:5].fillna("")
            for _, row in category_df.iterrows():
                l1, l2, l3, l4 = [str(v).strip() for v in row.tolist()]
                if not l1:
                    continue
                if l1 not in self.level1_options:
                    self.level1_options.append(l1)
                if l2:
                    self.level2_map.setdefault(l1, [])
                    if l2 not in self.level2_map[l1]:
                        self.level2_map[l1].append(l2)
                if l2 and l3:
                    key3 = (l1, l2)
                    self.level3_map.setdefault(key3, [])
                    if l3 not in self.level3_map[key3]:
                        self.level3_map[key3].append(l3)
                if l2 and l3 and l4:
                    key4 = (l1, l2, l3)
                    self.level4_map.setdefault(key4, [])
                    if l4 not in self.level4_map[key4]:
                        self.level4_map[key4].append(l4)
        except Exception:
            return

    def _set_menu_values(self, menu, values: List[str]):
        items = values if values else ["-"]
        menu.configure(values=items)
        menu.set(items[0])

    def on_cat_l1_changed(self, selected: str):
        l2_values = self.level2_map.get(selected, [])
        self._set_menu_values(self.cat_l2_menu, l2_values)
        self.on_cat_l2_changed(self.cat_l2_menu.get())

    def on_cat_l2_changed(self, selected: str):
        l1 = self.cat_l1_menu.get()
        l3_values = self.level3_map.get((l1, selected), [])
        self._set_menu_values(self.cat_l3_menu, l3_values)
        self.on_cat_l3_changed(self.cat_l3_menu.get())

    def on_cat_l3_changed(self, selected: str):
        l1 = self.cat_l1_menu.get()
        l2 = self.cat_l2_menu.get()
        l4_values = self.level4_map.get((l1, l2, selected), [])
        self._set_menu_values(self.cat_l4_menu, l4_values)
        self.on_cat_l4_changed(self.cat_l4_menu.get())

    def on_cat_l4_changed(self, selected: str):
        self._sync_seed_entry_with_category()

    def _current_selected_category_keyword(self) -> str:
        picks = [self.cat_l4_menu.get(), self.cat_l3_menu.get(), self.cat_l2_menu.get(), self.cat_l1_menu.get()]
        return next((v for v in picks if v and v not in {"-", "데이터 없음"}), "")

    def _sync_seed_entry_with_category(self):
        selected = self._current_selected_category_keyword()
        current = self.seed_entry.get().strip()
        parts = [p.strip() for p in current.split(",") if p.strip()]

        # 카테고리 탐색 중 누적 방지를 위해, 이전 자동 삽입값만 교체
        if self.auto_category_seed and self.auto_category_seed in parts:
            parts = [p for p in parts if p != self.auto_category_seed]

        if selected and selected not in parts:
            parts.append(selected)

        self.seed_entry.delete(0, "end")
        if parts:
            self.seed_entry.insert(0, ", ".join(parts))
        self.auto_category_seed = selected or None

    def append_log(self, msg: str):
        # GUI 업데이트는 main thread에서만
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def set_running(self, running: bool):
        self.run_button.configure(state="disabled" if running else "normal")
        self.status_label.configure(
            text="분석 중..." if running else "대기 중...",
            text_color="gray70" if not running else "DodgerBlue2",
        )

    def _query_period_to_range(self) -> tuple[datetime | None, datetime | None]:
        choice = self.query_period_menu.get()
        now = datetime.now()
        if choice == "오늘":
            start = datetime(now.year, now.month, now.day, 0, 0, 0)
            end = datetime(now.year, now.month, now.day, 23, 59, 59)
            return start, end
        if choice == "최근 7일":
            return now - timedelta(days=7), now
        if choice == "최근 30일":
            return now - timedelta(days=30), now
        if choice == "최근 60일":
            return now - timedelta(days=60), now
        if choice == "최근 120일":
            return now - timedelta(days=120), now
        return None, None

    def on_query_recent(self):
        if query_top_keywords is None:
            self.append_log("❌ DB 조회 모듈을 로드하지 못했습니다.")
            return
        try:
            limit = int(self.query_limit_menu.get())
            keyword_like = self.query_keyword_entry.get().strip() or None
            started_from, started_to = self._query_period_to_range()
            rows = query_top_keywords(
                limit=limit,
                keyword_like=keyword_like,
                started_from=started_from,
                started_to=started_to,
            )
            self.append_log("")
            self.append_log(f"📊 DB 조회 결과: {len(rows)}건")
            if not rows:
                self.append_log("   - 조건에 맞는 데이터가 없습니다.")
                return
            self.append_log("No | 실행일시            | 키워드                    | 점수(%)   | 검색량 | 상품수")
            self.append_log("-" * 88)
            for i, r in enumerate(rows[: min(len(rows), 20)], start=1):
                dt = str(r["started_at"])[:19]
                kw = str(r["keyword_text"])[:22].ljust(22)
                score_pct = f"{float(r.get('blue_ocean_score', 0.0) or 0.0):.2f}%"
                self.append_log(
                    f"{i:>2} | {dt} | {kw} | "
                    f"{score_pct:>8} | {r['monthly_search_volume_est']:>5} | {r['product_count']:>6}"
                )
            if len(rows) > 20:
                self.append_log(f"... (총 {len(rows)}건 중 상위 20건만 표시)")
        except Exception as e:
            self.append_log(f"❌ DB 조회 실패: {e}")

    def on_run(self):
        seeds_text = self.seed_entry.get().strip()
        start_date = self.start_date_entry.get().strip()
        end_date = self.end_date_entry.get().strip()

        if not seeds_text:
            if messagebox is not None:
                messagebox.showerror("입력 오류", "주제어(시드 키워드)를 입력해주세요.")
            return
        if not start_date or not end_date:
            if messagebox is not None:
                messagebox.showerror("입력 오류", "시작일/종료일을 입력해주세요.")
            return

        seeds = [s.strip() for s in seeds_text.split(",") if s.strip()]
        if not seeds:
            if messagebox is not None:
                messagebox.showerror("입력 오류", "주제어(시드 키워드)가 비어 있습니다.")
            return

        # 날짜 형식은 worker에서 검증(에러는 로그로 보이게 처리)
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.status_label.configure(text="분석 시작...", text_color="gray70")

        self.set_running(True)

        def worker():
            try:
                summary, _report_df = self.tool.start_analysis(
                    seeds=seeds,
                    start_date=start_date,
                    end_date=end_date,
                    log_callback=lambda m: self.log_queue.put(m),
                )
                if summary:
                    self.log_queue.put(f"\n✅ 결과: {summary}")
            except Exception as e:
                self.log_queue.put(f"\n❌ 오류 발생: {e}")
            finally:
                self.done_queue.put(True)

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def _poll_queues(self):
        # 로그 처리
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.append_log(msg)
        except queue.Empty:
            pass

        # 종료 처리
        try:
            _ = self.done_queue.get_nowait()
            self.set_running(False)
        except queue.Empty:
            pass

        self.app.after(100, self._poll_queues)

    def mainloop(self):
        self.app.mainloop()


def launch_web_mode():
    app_path = resource_path("app_web.py")
    requested_port = str(os.environ.get("PORT", "8501")).strip() or "8501"
    try:
        base_port = int(requested_port)
    except Exception:
        base_port = 8501

    port = base_port
    for p in range(base_port, base_port + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if s.connect_ex(("127.0.0.1", p)) != 0:
                port = p
                break

    env = os.environ.copy()
    env["STREAMLIT_SERVER_HEADLESS"] = "true"
    env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        app_path,
        "--server.address",
        "0.0.0.0",
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    subprocess.call(cmd, env=env)


if __name__ == "__main__":
    # 기본 실행은 웹(Streamlit)으로 고정하고, 필요 시 데스크톱/CLI 옵션을 사용한다.
    if "--desktop" not in sys.argv and "--cli" not in sys.argv:
        launch_web_mode()
    else:
        try:
            tool = BlueOceanTool()
        except Exception as e:
            print(f"\n오류 발생: {e}")
            raise

        if "--cli" in sys.argv or ctk is None:
            # 한글 인코딩(콘솔용)
            if sys.stdout.encoding != 'utf-8':
                try:
                    import io
                    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
                except Exception:
                    pass
            try:
                tool.start_analysis()
            except Exception as e:
                print(f"\n오류 발생: {e}")
        else:
            gui = BlueOceanToolGUI(tool)
            gui.mainloop()

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
from tkinter import messagebox
import hmac
import hashlib
import base64
import json
from typing import Dict, List, Optional, Tuple

from report_format import finalize_analysis_dataframe

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
        insert_monthly_trends,
        query_top_keywords,
    )
except Exception:
    create_run = None
    ensure_schema = None
    finish_run = None
    insert_keyword_metrics = None
    insert_monthly_trends = None
    query_top_keywords = None

def resource_path(relative_path: str) -> str:
    """
    PyInstaller(onefile) 실행 시 번들 내부에서 리소스를 찾기 위한 경로 계산.
    """
    if os.path.isabs(relative_path):
        return relative_path
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)

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
        self.output_dir = self.config['settings']['output_dir']
        self.save_excel = bool(self.config.get("settings", {}).get("save_excel", False))
        self.db_enabled = bool(self.config.get("settings", {}).get("db_enabled", True))
        self._apply_db_env_from_config()
        
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
        db_cfg = self.config.get("database", {}) or {}
        if not isinstance(db_cfg, dict):
            return

        mysql_url = str(db_cfg.get("mysql_url", "") or db_cfg.get("mariadb_url", "")).strip()
        if mysql_url and not (
            (os.environ.get("MYSQL_URL") or "").strip()
            or (os.environ.get("MARIADB_URL") or "").strip()
            or (os.environ.get("DATABASE_URL") or "").strip()
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

    def get_product_count(self, keyword):
        url = f"https://openapi.naver.com/v1/search/shop.json?query={keyword}&display=1"
        try:
            res = requests.get(url, headers=self.open_headers)
            if res.status_code == 200: return res.json().get('total', 0)
            return 0
        except: return 0

    def _strategy_text(self, blue_ocean_score: float) -> str:
        if blue_ocean_score > 5:
            return "강력 추천 황금 키워드! 수요 대비 경쟁자가 매우 적습니다."
        if blue_ocean_score > 1:
            return "유망 키워드. 썸네일과 상세페이지 차별화 추천."
        return "경쟁이 있으나 틈새 시장 공략이 가능합니다."

    def _persist_to_db(self, all_results: List[Dict], trends_by_keyword: Dict[str, Dict], seeds, start_date, end_date, log) -> int:
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

            finish_run(run_id, success=True, result_count=len(metric_rows))
            log(f"🗄️ DB 저장 완료: run_id={run_id}, metrics={len(metric_rows)}건, trends={trend_inserted}건")
            return run_id
        except Exception as e:
            finish_run(run_id, success=False, result_count=0, error_message=str(e))
            raise

    def start_analysis(
        self,
        seeds=None,
        start_date=None,
        end_date=None,
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

        for seed in seeds:
            seed = str(seed).strip()
            if not seed:
                continue

            log(f"\n▶ 주제어 [{seed}] 관련 키워드 발굴 중...")
            raw_keywords = self.ads_api.get_related_keywords(seed)
            if not raw_keywords:
                log(f"❌ [{seed}] 관련 키워드를 찾지 못했습니다.")
                continue

            log(f"ㄴ {len(raw_keywords)}개 연관 키워드 전수 분석 시작... (약 1~2분 소요)")

            for idx, item in enumerate(raw_keywords):
                kw = item['relKeyword']

                # 기본 필터링: 검색량이 너무 적은 것은 제외
                def clean_val(v):
                    if isinstance(v, str) and "<" in v:
                        return 5.0
                    try:
                        return float(v) if v is not None else 0.0
                    except Exception:
                        return 0.0

                pc_qc = clean_val(item.get("monthlyPcQcCnt"))
                mo_qc = clean_val(item.get("monthlyMobileQcCnt"))
                total_qc = pc_qc + mo_qc

                # 최소 검색량 필터링 (가독성을 위해 500회 이상만)
                if total_qc < 500:
                    continue

                total_clk = clean_val(item.get("monthlyAvePcClkCnt")) + clean_val(item.get("monthlyAveMobileClkCnt"))

                # 쇼핑 상품 수 조회
                prod_count = self.get_product_count(kw)
                time.sleep(0.02)

                # 블루오션 점수 (최신 한 달 기준 1차 필터링)
                safe_total = prod_count if prod_count > 0 else 1
                blue_ocean_score = (total_clk / safe_total) * 10000

                # 상위 유망 후보군만 상세 기간 분석 진행 (API 할당량 절약)
                if blue_ocean_score > 0.5:
                    trends = self.get_monthly_trends(kw, start_date, end_date)
                    if not trends:
                        continue

                    # 5개월 역산 로직 적용
                    recent_ratio = list(trends.values())[-1] if list(trends.values())[-1] > 0 else 1.0
                    scale_vector = total_qc / recent_ratio
                    scale_clk_vector = total_clk / recent_ratio

                    est_vols_qc = [r * scale_vector for r in trends.values()]
                    est_vols_clk = [r * scale_clk_vector for r in trends.values()]

                    avg_qc = sum(est_vols_qc) / len(trends)
                    avg_clk = sum(est_vols_clk) / len(trends)
                    avg_ctr = (sum(est_vols_clk) / sum(est_vols_qc) * 100) if sum(est_vols_qc) > 0 else 0

                    # 기간 평균 기준 최종 점수 재산출
                    final_score = (avg_clk / safe_total) * 10000

                    all_results.append({
                        "주제어": seed,
                        "키워드": kw,
                        "월평균 검색수(추정)": round(avg_qc),
                        "월평균 클릭수(추정)": round(avg_clk, 1),
                        "평균 클릭율(CTR)": f"{round(avg_ctr, 2)}%",
                        "상품수": prod_count,
                        "블루오션 점수": round(final_score, 4)
                    })
                    trends_by_keyword[kw] = {
                        m: {
                            "ratio": float(ratio),
                            "est_search_volume": int(round(ratio * scale_vector)),
                            "est_click_volume": float(round(ratio * scale_clk_vector, 1)),
                        }
                        for m, ratio in trends.items()
                    }

                if idx % 50 == 0:
                    log(f"   - {idx}/{len(raw_keywords)} 진행 중...")

        if all_results:
            df = pd.DataFrame(all_results).sort_values(by="블루오션 점수", ascending=False).head(100)  # 상위 100개만 리포트

            df["전략 제언"] = df["블루오션 점수"].apply(self._strategy_text)
            report_df = finalize_analysis_dataframe(df)

            # DB 저장(기본)
            run_id = self._persist_to_db(all_results, trends_by_keyword, seeds, start_date, end_date, log)

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
            self.append_log("No | 실행일시            | 키워드                    | 점수    | 검색량 | 상품수")
            self.append_log("-" * 88)
            for i, r in enumerate(rows[: min(len(rows), 20)], start=1):
                dt = str(r["started_at"])[:19]
                kw = str(r["keyword_text"])[:22].ljust(22)
                self.append_log(
                    f"{i:>2} | {dt} | {kw} | "
                    f"{r['blue_ocean_score']:>6.4f} | {r['monthly_search_volume_est']:>5} | {r['product_count']:>6}"
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
            messagebox.showerror("입력 오류", "주제어(시드 키워드)를 입력해주세요.")
            return
        if not start_date or not end_date:
            messagebox.showerror("입력 오류", "시작일/종료일을 입력해주세요.")
            return

        seeds = [s.strip() for s in seeds_text.split(",") if s.strip()]
        if not seeds:
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

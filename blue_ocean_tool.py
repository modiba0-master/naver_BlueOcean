import os
import requests
import pandas as pd
from datetime import datetime, timedelta
import time
import sys
import threading
import queue
from tkinter import messagebox
import hmac
import hashlib
import base64
import json
from typing import Dict, List

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
        
        # 출력 폴더 생성
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir, exist_ok=True)
        self.report_dir = os.path.join(self.output_dir, "reports")
        if not os.path.exists(self.report_dir):
            os.makedirs(self.report_dir, exist_ok=True)

        if self.db_enabled:
            if ensure_schema is None:
                raise RuntimeError("DB 모듈(db.py) 로드 실패: MariaDB 저장을 사용할 수 없습니다.")
            ensure_schema()

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

    def start_analysis(self, seeds=None, start_date=None, end_date=None, log_callback=None):
        """
        GUI에서 실행하기 위한 분석 함수.
        - seeds/start_date/end_date가 없으면 기존처럼 console input을 사용합니다.
        - log_callback(msg) 를 주면 print 대신 콜백으로 메시지를 전달합니다.
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

            df['전략 제언'] = df["블루오션 점수"].apply(self._strategy_text)

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
                df.to_excel(filepath, index=False, engine='openpyxl')

            log("\n✨ 분석이 완료되었습니다!")
            if filepath:
                log(f"📁 리포트 저장 위치: {filepath}")
            else:
                log("📁 엑셀 저장은 비활성화되어 DB만 저장했습니다. (settings.save_excel=false)")
            return filepath or f"DB run_id={run_id}"

        log("\n❌ 분석 결과가 없습니다. 주제어와 날짜 설정을 확인해주세요.")
        return None


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
        ctk.CTkLabel(root, text="주제어(시드 키워드) - 콤마로 구분", anchor="w").pack(fill="x", padx=6)
        self.seed_entry = ctk.CTkEntry(root, height=38, placeholder_text="예: 캠핑용품, 주방용품")
        self.seed_entry.pack(fill="x", padx=6, pady=(6, 12))

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
                filepath = self.tool.start_analysis(
                    seeds=seeds,
                    start_date=start_date,
                    end_date=end_date,
                    log_callback=lambda m: self.log_queue.put(m),
                )
                if filepath:
                    self.log_queue.put(f"\n✅ 결과: {filepath}")
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

if __name__ == "__main__":
    try:
        tool = BlueOceanTool()
    except Exception as e:
        print(f"\n❌ 오류 발생: {e}")
        raise

    # GUI 모드: 기본 실행은 GUI로 전환(콘솔은 --cli 옵션일 때)
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
            print(f"\n❌ 오류 발생: {e}")
    else:
        gui = BlueOceanToolGUI(tool)
        gui.mainloop()

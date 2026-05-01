import argparse
import asyncio
import atexit
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse, parse_qsl, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, Error, Page, Playwright, TimeoutError, sync_playwright

try:
    from db import insert_coupang_search_snapshot
except Exception:
    insert_coupang_search_snapshot = None

# CP949 환경에서도 깨지지 않게 출력하기 위한 유틸 함수
def safe_print(*args, **kwargs):
    text = " ".join(map(str, args))
    try:
        print(text, **kwargs)
    except UnicodeEncodeError:
        print(text.encode("cp949", errors="ignore").decode("cp949", errors="ignore"), **kwargs)


def _dump_smoke_extract_report(payload: Dict[str, Any]) -> None:
    """스모크 HTML 추출 결과를 JSON 파일로 남겨 대시보드 밖에서도 확인 가능하게 한다."""
    raw = os.environ.get("COUPANG_SMOKE_EXTRACT_JSON")
    if raw is not None and str(raw).strip().lower() in {"0", "off", "false", "none"}:
        return
    if raw and str(raw).strip():
        out_p = Path(str(raw).strip()).expanduser()
        if not out_p.is_absolute():
            out_p = (Path(__file__).resolve().parent / out_p).resolve()
    else:
        out_p = Path(__file__).resolve().parent / ".smoke" / "last_smoke_extract.json"
    try:
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        safe_print(f"[SMOKE] 추출 결과 저장: {out_p}")
    except Exception as ex:
        safe_print(f"[SMOKE] 추출 결과 파일 저장 실패(무시): {ex!r}")


def _persist_smoke_extract_report_to_db(payload: Dict[str, Any]) -> None:
    """
    스모크 추출 결과를 쿠팡 전용 DB 테이블에 저장한다.
    기존 주제어 분석 테이블(keyword_metrics 등)과 분리된 경로다.
    """
    if insert_coupang_search_snapshot is None:
        return
    if not isinstance(payload, dict):
        return
    if str(os.environ.get("COUPANG_SMOKE_EXTRACT_DB", "true")).strip().lower() in {"0", "false", "off", "no"}:
        return
    try:
        stored = int(insert_coupang_search_snapshot(payload) or 0)
        safe_print(f"[SMOKE][DB] 쿠팡 스냅샷 저장 완료 items={stored}")
    except Exception as db_ex:
        safe_print(f"[SMOKE][DB] 쿠팡 스냅샷 저장 실패(무시): {db_ex!r}")


# --- playwright-stealth (탐지 완화 스크립트 주입) ---
# PyPI 패키지 "playwright-stealth" 2.x(Mattwmaster58): 동기 경로는 stealth_sync가 아니라
#   Stealth 인스턴스의 apply_stealth_sync(page) 가 공식 API다.
# 구버전 1.x 일부: stealth_sync(page) 단일 함수만 제공하는 배포가 있다.
# 따라서 v2 Stealth를 먼저 시도하고, 없을 때만 stealth_sync 로 폴백한다.
# STEALTH_AVAILABLE 은 둘 중 실제 호출 가능한 경로가 있으면 True (차단 reason 분기 등에 사용).
import importlib.util

_STEALTH_V2_INSTANCE: Optional[Any] = None
_STEALTH_LEGACY_FN: Optional[Any] = None

try:
    from playwright_stealth import Stealth as _StealthCls  # type: ignore

    if hasattr(_StealthCls, "apply_stealth_sync"):
        _STEALTH_V2_CLS = _StealthCls
    else:
        _STEALTH_V2_CLS = None
except Exception:
    _STEALTH_V2_CLS = None

if _STEALTH_V2_CLS is None:
    try:
        from playwright_stealth import stealth_sync as _legacy_sync  # type: ignore

        _STEALTH_LEGACY_FN = _legacy_sync if callable(_legacy_sync) else None
    except Exception:
        _STEALTH_LEGACY_FN = None

STEALTH_AVAILABLE = _STEALTH_V2_CLS is not None or _STEALTH_LEGACY_FN is not None
_STEALTH_PKG_PRESENT = importlib.util.find_spec("playwright_stealth") is not None


def apply_stealth(page: Page) -> None:
    global _STEALTH_V2_INSTANCE
    if not STEALTH_AVAILABLE:
        return
    try:
        if _STEALTH_V2_CLS is not None:
            if _STEALTH_V2_INSTANCE is None:
                _STEALTH_V2_INSTANCE = _STEALTH_V2_CLS()
            _STEALTH_V2_INSTANCE.apply_stealth_sync(page)
        elif _STEALTH_LEGACY_FN is not None:
            _STEALTH_LEGACY_FN(page)
        safe_print("[INFO] Stealth 적용 완료")
    except Exception as e:
        safe_print(f"[ERROR] Stealth 적용 실패: {str(e)}")


# 상품 리스트 대기·파싱 공통 셀렉터 (DOM 변경 시 한곳만 수정)
_PRODUCT_LIST_SELECTOR = (
    "li.search-product, "
    "li.ProductUnit_productUnit__Qd6sv, "
    "li[data-product-id], "
    "ul#product-list > li"
)

HEADLESS = True


def _ensure_windows_proactor_policy() -> None:
    """Playwright subprocess requires Proactor loop on Windows."""
    if sys.platform != "win32":
        return
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        return

class CoupangCrawler:
    """Playwright 기반 쿠팡 검색 Top10 수집기."""

    def __init__(self) -> None:
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._last_success_cache: Dict[str, Dict[str, Any]] = {}
        self._stats = {"cache_hit": 0, "requests_ok": 0, "playwright_ok": 0, "failed": 0, "blocked": 0}
        self._last_error: Dict[str, str] = {}
        self._last_fetch_source = "unknown"
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._io_lock = threading.RLock()
        self._smoke_thread: Optional[threading.Thread] = None
        self._smoke_stop_event: Optional[threading.Event] = None
        self._smoke_subproc: Optional[subprocess.Popen] = None
        self._smoke_stop_file: Optional[str] = None
        self._smoke_tmpdir: Optional[str] = None
        self._smoke_status: Dict[str, Any] = {
            "phase": "idle",
            "headless": None,
            "target_url": "",
            "page_url": "",
            "page_title": "",
            "opened_at": None,
            "closed_at": None,
            "hint": "",
            "error": "",
            "top3_items": [],
        }
        self._smoke_preview_png: Optional[bytes] = None
        env_raw = str(os.environ.get("COUPANG_HEADLESS", "")).strip().lower()
        if env_raw in {"0", "false", "n", "no"}:
            self._headless = False
        elif env_raw in {"1", "true", "y", "yes"}:
            self._headless = True
        else:
            self._headless = HEADLESS

        self._chrome_user_data_dir = str(os.environ.get("COUPANG_CHROME_USER_DATA_DIR", "")).strip()
        self._chrome_profile = str(os.environ.get("COUPANG_CHROME_PROFILE", "")).strip()
        if not self._chrome_user_data_dir:
            self._chrome_user_data_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                ".coupang_chrome_profile_crawl",
            )
        os.makedirs(self._chrome_user_data_dir, exist_ok=True)
        # 수동 준비(홈/로그인/검색 대기)와 자동 크롤이 프로필 락을 나누지 않도록 분리
        self._prep_user_data_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            ".coupang_chrome_profile_prep",
        )
        os.makedirs(self._prep_user_data_dir, exist_ok=True)
        if not self._chrome_profile:
            self._chrome_profile = "Default"

    def _smoke_status_update(self, **kwargs: Any) -> None:
        with self._io_lock:
            self._smoke_status = {**self._smoke_status, **kwargs}

    def get_smoke_playwright_status(self) -> Dict[str, Any]:
        """대시보드에서 스모크 Chromium 진행 여부 확인용(phase·URL·캡처 등)."""
        self._maybe_reap_smoke_subprocess()
        with self._io_lock:
            out = dict(self._smoke_status)
            png = self._smoke_preview_png
            sub = self._smoke_subproc
        if sub is not None and sub.poll() is None:
            out = {
                **out,
                "phase": "windows_subprocess_running",
                "thread_alive": True,
                "headless": False,
                "subprocess_pid": sub.pid,
                "hint": (
                    "별도 Python 프로세스에서 headed Chromium이 실행 중입니다. "
                    "작업 표시줄·Alt+Tab에서 창을 확인하세요. "
                    "Railway 등 **원격 대시보드**만 쓰는 경우 Chromium은 **서버**에서만 떠서 이 PC에는 보이지 않습니다."
                ),
            }
            return out
        out["thread_alive"] = self.is_smoke_playwright_running()
        if png:
            out["preview_png"] = png
        return out

    @staticmethod
    def _smoke_use_subprocess_launch() -> bool:
        """기본은 in-process(상태 공유). 필요 시 COUPANG_SMOKE_SUBPROCESS=1 로만 별도 프로세스 실행."""
        if str(os.environ.get("COUPANG_SMOKE_SUBPROCESS", "")).strip() == "1":
            return sys.platform == "win32"
        if (
            os.environ.get("RAILWAY_ENVIRONMENT")
            or os.environ.get("RAILWAY_SERVICE_NAME")
            or os.environ.get("RAILWAY_PROJECT_ID")
        ):
            return False
        return False

    def _maybe_reap_smoke_subprocess(self) -> None:
        with self._io_lock:
            sub = self._smoke_subproc
            tmp = self._smoke_tmpdir
        if sub is None:
            return
        if sub.poll() is None:
            return
        with self._io_lock:
            self._smoke_subproc = None
            self._smoke_stop_file = None
            self._smoke_tmpdir = None
            self._smoke_status = {
                **self._smoke_status,
                "phase": "closed",
                "closed_at": time.time(),
                "thread_alive": False,
                "hint": "스모크 자식 프로세스가 종료되었습니다.",
            }
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    def _terminate_smoke_subprocess_if_any(self) -> None:
        self._maybe_reap_smoke_subprocess()
        with self._io_lock:
            sub = self._smoke_subproc
            sf = self._smoke_stop_file
            tmp = self._smoke_tmpdir
            self._smoke_subproc = None
            self._smoke_stop_file = None
            self._smoke_tmpdir = None
        if sf:
            try:
                os.makedirs(os.path.dirname(sf), exist_ok=True)
            except OSError:
                pass
            try:
                with open(sf, "w", encoding="utf-8") as fp:
                    fp.write("stop")
            except OSError:
                pass
        if sub is not None and sub.poll() is None:
            try:
                sub.terminate()
                sub.wait(timeout=12.0)
            except Exception:
                try:
                    sub.kill()
                except Exception:
                    pass
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    def _sanitize_playwright_browser_env(self) -> None:
        """Railway 리눅스 경로를 Windows 로컬에 복사하면 Chromium을 못 찾으므로 무효 경로는 제거."""
        raw = str(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")).strip()
        if not raw:
            return
        norm = raw.replace("\\", "/")
        if sys.platform == "win32":
            if norm.startswith("/") and not norm.startswith("//"):
                safe_print(
                    "[PLAYWRIGHT_CHECK] Unix 스타일 PLAYWRIGHT_BROWSERS_PATH는 Windows에서 무시합니다. "
                    "(로컬은 기본 캐시 또는 Windows 경로를 사용합니다.)"
                )
                os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
                return
        try:
            base = Path(raw)
        except Exception:
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
            return
        if not base.exists():
            safe_print(
                f"[PLAYWRIGHT_CHECK] PLAYWRIGHT_BROWSERS_PATH={raw!r} 경로 없음 — "
                "Playwright 기본 브라우저 캐시로 대체합니다."
            )
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)

    def _prep_force_headless(self) -> bool:
        """Linux 서버에 DISPLAY 없으면 headed 실행이 곧바로 죽으므로 headless로 폴백."""
        if sys.platform == "win32":
            return False
        if os.environ.get("DISPLAY"):
            return False
        safe_print("[WARN] DISPLAY 없음 — 준비용 창은 headless로 진행합니다 (서버 환경).")
        return True

    def _cache_key(self, keyword: str) -> str:
        return f"{keyword.strip()}_{datetime.now().strftime('%Y%m%d')}"

    def _save_smoke_screenshot_file(self, png_bytes: bytes, tag: str) -> Optional[str]:
        """
        스모크 캡처 PNG를 .smoke 폴더에 저장한다.
        파일명 예: smoke_step0_open_20260501_220900.png
        """
        try:
            out_dir = Path(__file__).resolve().parent / ".smoke"
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_tag = re.sub(r"[^a-zA-Z0-9_-]", "_", str(tag or "shot"))
            out_path = out_dir / f"smoke_{safe_tag}_{ts}.png"
            with open(out_path, "wb") as f:
                f.write(png_bytes)
            return str(out_path)
        except Exception as e:
            safe_print(f"[SMOKE] 스크린샷 파일 저장 실패(무시): {e!r}")
            return None

    def _fallback_profile_dir(self, prep_profile: bool) -> str:
        suffix = "prep" if prep_profile else "crawl"
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            f".coupang_chrome_profile_{suffix}_fallback_{os.getpid()}_{int(time.time() * 1000)}",
        )

    def _log_playwright_preflight(self) -> None:
        path = str(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")).strip()
        if not path:
            safe_print("[PLAYWRIGHT_CHECK] PLAYWRIGHT_BROWSERS_PATH is not set.")
            return
        base = Path(path)
        bins: List[Path] = []
        if base.exists():
            bins = list(base.glob("chromium-*/chrome-linux64/chrome")) + list(
                base.glob("chromium-*/chrome-win64/chrome.exe")
            )
        safe_print(
            f"[PLAYWRIGHT_CHECK] path={path}, exists={base.exists()}, chromium_bin_count={len(bins)}"
        )

    def _parse_int(self, text: str) -> Optional[int]:
        raw = re.sub(r"[^0-9]", "", str(text or ""))
        if not raw:
            return None
        try:
            return int(raw)
        except Exception:
            return None

    def _parse_float(self, text: str) -> Optional[float]:
        raw = str(text or "").strip()
        if not raw:
            return None
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)", raw)
        if not m:
            return None
        try:
            return float(m.group(1))
        except Exception:
            return None

    def _pick_price_won_from_li(self, li: BeautifulSoup) -> str:
        """
        가격 텍스트에서 실제 판매가를 우선 추출한다.
        - custom-oos 블록의 대표 가격(span)을 최우선 사용
        - '1개당' 같은 단가 안내는 제외
        - fallback에서는 첫 번째 원화 값을 판매가로 사용
        """
        # 1) 최근 DOM: custom-oos (예: 할인율 + 판매가 + 1개당 단가)
        for n in li.select(".custom-oos span, .custom-oos div, [class*='custom-oos'] span"):
            t = n.get_text(" ", strip=True)
            if not t or "개당" in t:
                continue
            m = re.search(r"[\d,]+\s*원", t)
            if m:
                return re.sub(r"\s+", "", m.group(0))

        # 2) 기존 DOM fallback
        area = (
            li.select_one(".PriceArea_priceArea__NntJz")
            or li.select_one(".sale-price")
            or li.select_one("[class*='price']")
        )
        if not area:
            return ""
        blob = area.get_text(" ", strip=True)
        ms = list(re.finditer(r"[\d,]+\s*원", blob))
        if not ms:
            return ""
        # 일반적으로 첫 번째 값이 대표 판매가(뒤쪽은 단가/보조 문구일 수 있음)
        return re.sub(r"\s+", "", ms[0].group(0))

    def _pick_shipping_from_li(self, li: BeautifulSoup) -> str:
        """배송비/로켓 등 배송 관련 배지만 사용(할인율 % 배지 오탐 방지). 없으면 배송 키워드로 보조 추출."""
        n = li.select_one(".TextBadge_feePrice__n_gta, [data-badge-type='feePrice']")
        if n:
            return n.get_text(" ", strip=True)
        for sel in (
            "[class*='DeliveryInfo']",
            "[class*='deliveryInfo']",
            "[class*='DeliveryBadge']",
            "[class*='RocketBadge']",
            "[class*='RocketDelivery']",
            "[class*='rocketDelivery']",
            "[class*='ProductUnit_badge']",
            "[class*='ImageBadge']",
            "[class*='BadgeList']",
        ):
            n2 = li.select_one(sel)
            if n2:
                t = n2.get_text(" ", strip=True)
                if t and not re.fullmatch(r"\d+%", t.strip()):
                    return t
        badge_blob = " ".join(
            x.get_text(" ", strip=True)
            for x in li.select(
                "[class*='Badge'], [class*='badge'], [class*='Delivery'], "
                "[class*='delivery'], [class*='Label'], [class*='label'], "
                "[class*='Rocket'], [class*='rocket'], [data-badge-type]"
            )
        )
        kw_hit = self._pick_shipping_keywords_from_text(badge_blob)
        if kw_hit:
            return kw_hit
        return self._pick_shipping_keywords_from_text(li.get_text(" ", strip=True))

    @staticmethod
    def _pick_shipping_keywords_from_text(blob: str) -> str:
        """로켓/무료배송/출발·도착 등 검색 결과 카드에 자주 노출되는 배송 문구만 모은다."""
        if not blob:
            return ""
        seen: List[str] = []
        for kw in (
            "로켓배송",
            "판매자로켓",
            "로켓직구",
            "로켓그로스",
            "새벽배송",
            "오늘 출발",
            "오늘출발",
            "도착보장",
            "내일도착",
            "내일 도착",
            "무료배송",
            "판매자 배송",
            "판매자배송",
        ):
            if kw in blob and kw not in seen:
                seen.append(kw)
        return " / ".join(seen)

    def _normalize_review_count_display(self, raw: str) -> str:
        m = re.search(r"\(\s*([\d,]+)\s*\)", str(raw or ""))
        if m:
            return m.group(1).replace(",", "")
        n = self._parse_int(raw)
        return str(n) if n is not None else str(raw or "").strip()

    def _build_result(self, product_count: int, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        reviews = [float(it["review_count"]) for it in items if it.get("review_count") is not None]
        prices = [float(it["price"]) for it in items if it.get("price") is not None]
        if len(reviews) < 1 or len(prices) < 1:
            return {"product_count": int(product_count), "avg_reviews": 0.0, "avg_price": 0.0, "top10_items": items}
        return {
            "product_count": int(product_count),
            "avg_reviews": round(sum(reviews) / len(reviews), 2),
            "avg_price": round(sum(prices) / len(prices), 2),
            "top10_items": items,
        }

    def _default_result(self) -> Dict[str, Any]:
        return {
            "product_count": 0,
            "avg_reviews": 0.0,
            "avg_price": 0.0,
            "top10_items": [],
            "reason_code": "NO_RESULT",
        }

    def _result_with_reason(self, reason_code: str) -> Dict[str, Any]:
        one = self._default_result()
        one["reason_code"] = reason_code
        return one

    def _build_search_url(self, keyword: str) -> str:
        trace = f"bo{int(time.time())}{random.randint(100, 999)}"
        return (
            f"https://www.coupang.com/np/search?component=&q={quote(keyword)}"
            f"&traceId={trace}&channel=user"
        )

    def _is_blocked(self, html: str, title: str = "") -> bool:
        text = f"{title}\n{html}".lower()
        blocked_signals = [
            "access denied",
            "robot",
            "automated queries",
            "captcha",
            "서비스 이용에 불편",
            "차단",
        ]
        return any(sig in text for sig in blocked_signals)

    def _parse_top10_from_html(self, html: str) -> tuple[int, List[Dict[str, Any]]]:
        parser = "lxml"
        try:
            soup = BeautifulSoup(html, parser)
        except Exception:
            soup = BeautifulSoup(html, "html.parser")

        products = soup.select("li.ProductUnit_productUnit__Qd6sv")
        if not products:
            products = soup.select("li.search-product")
        if not products:
            products = soup.select("ul#product-list > li")
        items: List[Dict[str, Any]] = []
        rank_no = 0
        for li in products:
            is_ad = bool(
                li.select_one(
                    ".search-product__ad-badge, .search-product__ad, .ad-badge-text"
                )
            )
            if not is_ad:
                li_text = li.get_text(" ", strip=True)
                has_rank_mark = li.select_one("[class*='RankMark_rank']") is not None
                if "광고" in li_text and not has_rank_mark:
                    is_ad = True
            if is_ad:
                continue
            title_node = li.select_one(".ProductUnit_productNameV2__cV9cw, .name")
            price_raw = self._pick_price_won_from_li(li)
            review_count_node = li.select_one(
                ".ProductRating_productRating__jjf7W [class*='fw-text-'], "
                ".rating-total-count, .rating-count, .count"
            )
            review_score_node = li.select_one(
                ".ProductRating_productRating__jjf7W [aria-label], "
                ".ProductRating_productRating__jjf7W em, "
                ".ProductRating_productRating__jjf7W strong, "
                ".ProductRating_productRating__jjf7W [class*='rating'], "
                ".star .rating"
            )
            link_node = (
                li.select_one("a[href*='vp/products']")
                or li.select_one("a[href*='/products/']")
                or li.select_one("a[href*='www.coupang.com/vp/']")
                or li.select_one("a[href^='/vp/products']")
                or li.select_one("a[href]")
            )

            title = title_node.get_text(strip=True) if title_node else ""
            review_count_raw = review_count_node.get_text(strip=True) if review_count_node else ""
            review_score_raw = ""
            if review_score_node is not None:
                review_score_raw = str(
                    review_score_node.get("aria-label", "") or review_score_node.get_text(strip=True) or ""
                )
            shipping_fee_raw = self._pick_shipping_from_li(li)
            price_num = self._parse_int(price_raw)
            review_num = self._parse_int(self._normalize_review_count_display(review_count_raw))
            review_score = self._parse_float(review_score_raw)
            url = ""
            if link_node is not None:
                href = str(link_node.get("href", "")).strip()
                if href.startswith("http"):
                    url = href
                elif href.startswith("/"):
                    url = f"https://www.coupang.com{href}"

            if not title or price_num is None:
                continue
            rank_no += 1
            items.append(
                {
                    "rank": rank_no,
                    "title": title,
                    "price": float(price_num),
                    "review_count": float(review_num) if review_num is not None else None,
                    "review_score": float(review_score) if review_score is not None else None,
                    "shipping_fee": shipping_fee_raw or None,
                    "url": url,
                }
            )
            if len(items) >= 10:
                break
        return len(products), items

    def get_cached_result(self, keyword: str) -> Optional[Dict[str, Any]]:
        key = str(keyword or "").strip()
        if not key:
            return None
        one = self._last_success_cache.get(key)
        return dict(one) if one is not None else None

    def _get_page(
        self, force_headless: Optional[bool] = None, *, prep_profile: bool = False
    ) -> Optional[Page]:
        if self._page is not None:
            return self._page
        try:
            self._sanitize_playwright_browser_env()
            self._log_playwright_preflight()
            _ensure_windows_proactor_policy()
            use_headless = self._headless if force_headless is None else bool(force_headless)
            primary_user_data_dir = self._prep_user_data_dir if prep_profile else self._chrome_user_data_dir
            user_data_dirs = [primary_user_data_dir, self._fallback_profile_dir(prep_profile)]
            self._playwright = sync_playwright().start()
            # --- launch_persistent_context 의 channel (브라우저 바이너리 선택) ---
            # 미설정(None): playwright install 로 받은 번들 Chromium — 서버·Docker·CI에 시스템 Chrome이 없어도 동일 동작.
            # 설정 시: OS에 깔린 브라우저를 씀. 예) chrome, msedge (Playwright 문서의 channel 값과 동일).
            # 기본을 번들로 둔 이유: 환경마다 설치 유무·버전이 달라져 오차가 나기 쉽기 때문. 필요할 때만 env로 전환.
            _channel = str(os.environ.get("COUPANG_PLAYWRIGHT_CHANNEL", "")).strip() or None
            last_error: Optional[Exception] = None
            for idx, user_data_dir in enumerate(user_data_dirs):
                try:
                    self._context = self._playwright.chromium.launch_persistent_context(
                        user_data_dir=user_data_dir,
                        headless=use_headless,
                        channel=_channel,
                        viewport={"width": 1440, "height": 2000},
                        locale="ko-KR",
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                        ),
                        extra_http_headers={
                            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                            "sec-ch-ua-mobile": "?0",
                            "sec-ch-ua-platform": '"Windows"',
                            "Referer": "https://www.google.com/"
                        },
                        args=[
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                            "--disable-blink-features=AutomationControlled",
                            "--disable-infobars",
                            "--window-size=1440,2000",
                            "--start-maximized",
                            f"--profile-directory={self._chrome_profile}",
                        ],
                    )
                    break
                except Error as e:
                    last_error = e
                    if idx == 0 and ("ProcessSingleton" in str(e) or "profile is already in use" in str(e)):
                        safe_print("[WARN] profile in use 감지 — fallback 프로필로 재시도합니다.")
                        continue
                    raise
            if self._context is None and last_error is not None:
                raise last_error
            page = self._context.new_page()

            # [USER_CUSTOM_STUFF]
            if STEALTH_AVAILABLE:
                safe_print("[INFO] Stealth 모드 활성화: 탐지 우회 적용 중...")
                apply_stealth(page)
            elif _STEALTH_PKG_PRESENT:
                safe_print(
                    "[WARN] playwright_stealth는 설치되어 있으나 stealth_sync API를 사용할 수 없습니다. "
                    "기본 모드로 실행합니다."
                )
            else:
                safe_print("[WARN] playwright_stealth 미설치: 기본 모드로 실행합니다. (차단 위험 높음)")

            # 공통 스크립트 주입 (라이브러리 없이도 가능한 우회)
            page.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                """
            )
            page.set_default_timeout(15000)
            self._page = page
            self._last_error = {}
            return self._page
        except Error as e:
            safe_print(f"[Crawler Error] keyword=PLAYWRIGHT_INIT, error={e}")
            self._last_error = {
                "code": "PLAYWRIGHT_INIT_FAILED",
                "message": str(e),
            }
            return None
        except Exception as e:
            safe_print(f"[Crawler Error] keyword=PLAYWRIGHT_INIT_UNEXPECTED, error={e!r}")
            self._last_error = {
                "code": "PLAYWRIGHT_INIT_UNEXPECTED",
                "message": repr(e),
            }
            return None

    def open_home_ready_session(self, wait_seconds: int = 120) -> bool:
        with self._io_lock:
            if self._page is not None:
                self.close()
            page = self._get_page(force_headless=self._prep_force_headless(), prep_profile=True)
            if page is None:
                return False
            try:
                page.goto("https://www.coupang.com", wait_until="domcontentloaded")
                if self._is_blocked(page.content(), page.title()):
                    safe_print("[WAF_BLOCK] 쿠팡 접속이 차단되었습니다. (Access Denied/CAPTCHA)")
                    self._stats["blocked"] += 1
                    return False
                page.wait_for_selector("input[name='q'], input[placeholder*='검색']", timeout=15000)
                safe_print("[Ready] 쿠팡 홈 접속 완료. 검색창 입력 가능 상태입니다.")
                safe_print("[Ready] 이 창에서 직접 키워드를 입력해 주세요.")
                safe_print(f"[Ready] 대기시간: {wait_seconds}초")
                time.sleep(max(10, int(wait_seconds)))
                return True
            except Exception as e:
                safe_print(f"[Crawler Error] keyword=OPEN_HOME_READY, error={e!r}")
                return False
            finally:
                self.close()

    def _simulate_human_actions(self, page: Page) -> None:
        try:
            page.wait_for_timeout(random.randint(500, 1200))
            page.mouse.move(random.randint(200, 700), random.randint(200, 550), steps=random.randint(10, 30))
            page.wait_for_timeout(random.randint(300, 800))
            page.mouse.wheel(0, random.randint(500, 1200))
            page.wait_for_timeout(random.randint(400, 900))
            page.mouse.wheel(0, random.randint(-250, 100))
            page.wait_for_timeout(random.randint(300, 700))
        except Exception:
            return

    def _accept_google_consent_if_present(self, page: Page) -> None:
        """
        Google 첫 진입 시 뜨는 동의 팝업(모두 수락/Accept all)을 1회 수락 시도한다.
        팝업이 없거나 셀렉터가 바뀐 경우에도 흐름은 계속 진행한다.
        """
        try:
            # 동의 화면에서는 종종 consent.google.com 으로 리다이렉트되므로 짧게 대기
            page.wait_for_timeout(600)
            url_lower = str(page.url or "").lower()

            # 1) 메인 문서에서 직접 버튼 탐색
            candidates = [
                page.get_by_role("button", name=re.compile(r"모두\s*수락|동의하고\s*계속", re.I)).first,
                page.get_by_role("button", name=re.compile(r"accept\s*all|i\s*agree", re.I)).first,
                page.locator("button[aria-label*='모두 수락'], button[aria-label*='Accept all']").first,
                page.locator("form[action*='consent'] button, form[action*='consent'] input[type='submit']").first,
            ]
            for btn in candidates:
                try:
                    btn.wait_for(state="visible", timeout=1800)
                    btn.click(timeout=2500)
                    safe_print("[SMOKE] Google 동의 팝업 수락 완료(메인 문서)")
                    page.wait_for_timeout(500)
                    return
                except Exception:
                    continue

            # 2) iframe 내부 동의 버튼 탐색
            for fr in page.frames:
                f_url = str(fr.url or "").lower()
                if "consent" not in f_url and "google" not in f_url and "intro" not in f_url:
                    continue
                for sel in (
                    "button:has-text('모두 수락')",
                    "button:has-text('동의하고 계속')",
                    "button:has-text('Accept all')",
                    "button:has-text('I agree')",
                    "form[action*='consent'] button",
                    "form[action*='consent'] input[type='submit']",
                ):
                    try:
                        b = fr.locator(sel).first
                        if b.count() > 0:
                            b.click(timeout=2500)
                            safe_print("[SMOKE] Google 동의 팝업 수락 완료(iframe)")
                            page.wait_for_timeout(500)
                            return
                    except Exception:
                        continue

            # 동의 도메인인데 버튼을 못 찾았으면 흔적만 남김
            if "consent.google.com" in url_lower:
                safe_print("[SMOKE] Google 동의 화면 감지했으나 수락 버튼을 찾지 못했습니다.")
        except Exception as e:
            safe_print(f"[SMOKE] Google 동의 팝업 처리 중 예외(무시): {e!r}")

    def _session_headers_from_page(self, page: Page) -> Dict[str, str]:
        try:
            ua = str(page.evaluate("() => navigator.userAgent"))
        except Exception:
            ua = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }
        return headers

    def _requests_with_browser_session(self, page: Page, search_url: str) -> Optional[Dict[str, Any]]:
        if self._context is None:
            return None
        try:
            storage = self._context.storage_state()
            cookies = storage.get("cookies", []) if isinstance(storage, dict) else []
            jar = requests.cookies.RequestsCookieJar()
            for c in cookies:
                jar.set(
                    str(c.get("name", "")),
                    str(c.get("value", "")),
                    domain=str(c.get("domain", "")).lstrip("."),
                    path=str(c.get("path", "/")),
                )
            headers = self._session_headers_from_page(page)
            parsed = urlparse(search_url)
            q = dict(parse_qsl(parsed.query, keep_blank_values=True))
            q["component"] = q.get("component", "")
            q["channel"] = q.get("channel", "user")
            req_url = urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    parsed.params,
                    urlencode(q, doseq=True),
                    parsed.fragment,
                )
            )
            res = requests.get(req_url, headers=headers, cookies=jar, timeout=12, allow_redirects=True)
            if res.status_code != 200:
                safe_print(f"[Crawler][requests] status={res.status_code}")
                self._last_error = {
                    "code": f"REQUESTS_HTTP_{int(res.status_code)}",
                    "message": f"requests status={int(res.status_code)}",
                }
                return None
            if self._is_blocked(res.text, ""):
                safe_print("[WAF_BLOCK][requests] blocked signal detected in requests parsing")
                self._stats["blocked"] += 1
                self._last_error = {
                    "code": "REQUESTS_BLOCKED_BY_WAF",
                    "message": "blocked signal detected in requests response",
                }
                return None
            product_count, items = self._parse_top10_from_html(res.text)
            safe_print(f"[Crawler][requests] parsed product_count={product_count} top10_items={len(items)}")
            if product_count <= 0:
                self._last_error = {
                    "code": "REQUESTS_NO_PRODUCTS",
                    "message": "requests parse returned zero products",
                }
                return None
            self._last_fetch_source = "requests"
            return self._build_result(product_count, items)
        except Exception as e:
            safe_print(f"[Crawler Error] keyword=REQUESTS_SESSION, error={e!r}")
            self._last_error = {
                "code": "REQUESTS_EXCEPTION",
                "message": repr(e),
            }
            return None

    def bootstrap_login_session(self, wait_seconds: int = 120) -> bool:
        with self._io_lock:
            if self._page is not None:
                self.close()
            page = self._get_page(force_headless=self._prep_force_headless(), prep_profile=True)
            if page is None:
                return False
            try:
                page.goto("https://www.coupang.com/np/coupanglogin/login", wait_until="domcontentloaded")
                safe_print("[Bootstrap] 쿠팡 로그인 페이지를 열었습니다.")
                safe_print(f"[Bootstrap] 아래 경로에 세션이 저장됩니다: {self._prep_user_data_dir}")
                safe_print(f"[Bootstrap] {wait_seconds}초 내 수동 로그인 후 창을 그대로 두세요.")
                time.sleep(max(10, int(wait_seconds)))
                safe_print("[Bootstrap] 세션 저장 절차를 종료합니다.")
                return True
            except Exception as e:
                safe_print(f"[Crawler Error] keyword=BOOTSTRAP_LOGIN, error={e!r}")
                return False
            finally:
                self.close()

    def open_search_ready_session(self, wait_seconds: int = 120) -> bool:
        with self._io_lock:
            if self._page is not None:
                self.close()
            page = self._get_page(force_headless=self._prep_force_headless(), prep_profile=True)
            if page is None:
                return False
            try:
                page.goto("https://www.coupang.com", wait_until="domcontentloaded")
                if self._is_blocked(page.content(), page.title()):
                    safe_print("[WAF_BLOCK] 쿠팡 접속이 차단되었습니다. (Access Denied/CAPTCHA)")
                    self._stats["blocked"] += 1
                    return False
                page.wait_for_selector("input[name='q'], input[placeholder*='검색']", timeout=15000)
                self._simulate_human_actions(page)
                safe_print("[Ready] 쿠팡 메인 페이지 접속 완료.")
                safe_print("[Ready] 검색창에 키워드를 직접 입력해 주세요.")
                safe_print(f"[Ready] {wait_seconds}초 동안 브라우저를 유지합니다.")
                time.sleep(max(10, int(wait_seconds)))
                safe_print("[Ready] 수동 입력 대기 모드를 종료합니다.")
                return True
            except Exception as e:
                safe_print(f"[Crawler Error] keyword=OPEN_SEARCH_READY, error={e!r}")
                return False
            finally:
                self.close()

    def open_google_ready_session(self, wait_seconds: int = 180) -> bool:
        with self._io_lock:
            if self._page is not None:
                self.close()
            page = self._get_page(force_headless=self._prep_force_headless(), prep_profile=True)
            if page is None:
                return False
            try:
                page.goto("https://www.google.com/ncr", wait_until="domcontentloaded")
                page.wait_for_selector("textarea[name='q'], input[name='q']:not([type='hidden'])", timeout=15000)
                safe_print("[Ready] Google 홈 화면이 열렸습니다.")
                safe_print("[Ready] 직접 검색 후 쿠팡 결과 페이지 URL을 복사해 전달해 주세요.")
                safe_print(f"[Ready] {wait_seconds}초 동안 브라우저를 유지합니다.")
                time.sleep(max(10, int(wait_seconds)))
                return True
            except Exception as e:
                safe_print(f"[Crawler Error] keyword=OPEN_GOOGLE_READY, error={e!r}")
                return False
            finally:
                self.close()

    def parse_coupang_search_url(self, search_url: str) -> Dict[str, Any]:
        url = str(search_url or "").strip()
        if not url:
            return self._result_with_reason("EMPTY_URL")
        if "coupang.com/np/search" not in url:
            return self._result_with_reason("INVALID_URL")

        with self._io_lock:
            page = self._get_page(force_headless=True)
            if page is None:
                return self._result_with_reason("PLAYWRIGHT_INIT_FAILED")
            try:
                page.goto(url, wait_until="domcontentloaded")
                html = page.content()
                if self._is_blocked(html, page.title()):
                    safe_print("[WAF_BLOCK] 지정된 URL 파싱 중 WAF 차단 발생.")
                    self._stats["blocked"] += 1
                    reason = "BLOCKED_BY_WAF" if STEALTH_AVAILABLE else "BLOCKED_BY_WAF_NO_STEALTH"
                    return self._result_with_reason(reason)

                page.wait_for_selector(_PRODUCT_LIST_SELECTOR, timeout=10000)
                page.wait_for_timeout(800)
                html = page.content()
                product_count, items = self._parse_top10_from_html(html)
                if product_count <= 0:
                    return self._result_with_reason("NO_PRODUCTS")
                out = self._build_result(product_count, items)
                out["reason_code"] = "OK"
                return out
            except TimeoutError:
                return self._result_with_reason("TIMEOUT")
            except Exception as e:
                safe_print(f"[Crawler Error] keyword=PARSE_URL, error={e!r}")
                return self._result_with_reason("PARSE_FAILED")
            finally:
                self.close()

    def parse_local_html(self, file_path: str) -> Dict[str, Any]:
        """직접 저장한 로컬 HTML 파일 파싱 기능"""
        if not os.path.exists(file_path):
            return self._result_with_reason("FILE_NOT_FOUND")
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                html = f.read()
            
            if self._is_blocked(html, ""):
                safe_print(f"[WAF_BLOCK] 로컬 파일({file_path}) 내에 WAF/CAPTCHA 차단 신호가 있습니다.")
                self._stats["blocked"] += 1
                return self._result_with_reason("BLOCKED_BY_WAF")

            product_count, items = self._parse_top10_from_html(html)
            if product_count <= 0:
                return self._result_with_reason("NO_PRODUCTS")
            
            out = self._build_result(product_count, items)
            out["reason_code"] = "OK"
            return out
        except Exception as e:
            safe_print(f"[Crawler Error] keyword=PARSE_LOCAL, error={e!r}")
            return self._result_with_reason("PARSE_FAILED")

    def _crawl_with_playwright(self, keyword: str) -> Optional[Dict[str, Any]]:
        with self._io_lock:
            page = self._get_page()
            if page is None:
                return None
            try:
                url = self._build_search_url(keyword)
                page.goto(url, wait_until="domcontentloaded")
                if self._is_blocked(page.content(), page.title()):
                    safe_print("[WAF_BLOCK][playwright] initial load blocked by WAF/CAPTCHA")
                    self._stats["blocked"] += 1
                    self._last_error = {
                        "code": "PLAYWRIGHT_BLOCKED_BY_WAF_INITIAL",
                        "message": "initial load blocked by WAF/CAPTCHA",
                    }
                    return None
                self._simulate_human_actions(page)

                req_result = self._requests_with_browser_session(page, url)
                if req_result is not None:
                    return req_result

                page.wait_for_selector(_PRODUCT_LIST_SELECTOR, timeout=12000)
                page.wait_for_timeout(random.randint(900, 1600))
                safe_print(
                    f"[Crawler][playwright] ready keyword={keyword} "
                    f"title={page.title()[:80]} url={page.url}"
                )
                html = page.content()
                if self._is_blocked(html, page.title()):
                    safe_print("[WAF_BLOCK][playwright] blocked signal detected before parsing")
                    self._stats["blocked"] += 1
                    return None
                product_count, items = self._parse_top10_from_html(html)
                safe_print(
                    f"[Crawler][playwright] parsed keyword={keyword} "
                    f"product_count={product_count} top10_items={len(items)}"
                )
                if product_count <= 0:
                    self._last_error = {
                        "code": "PLAYWRIGHT_NO_PRODUCTS",
                        "message": "playwright parse returned zero products",
                    }
                    return None
                self._last_fetch_source = "playwright"
                return self._build_result(product_count, items)
            except TimeoutError as e:
                safe_print(f"[Crawler Error] keyword={keyword}, error=timeout, detail={e}")
                self._last_error = {
                    "code": "PLAYWRIGHT_SELECTOR_TIMEOUT",
                    "message": str(e),
                }
                return None
            except Exception as e:
                err_name = type(e).__name__
                if err_name == "TargetClosedError" or "TargetClosed" in err_name:
                    safe_print(f"[Crawler Error] keyword={keyword}, browser/context closed: {e!r}")
                    self._last_error = {"code": "BROWSER_CLOSED", "message": str(e)}
                    try:
                        self.close()
                    except Exception:
                        pass
                    return None
                try:
                    cur = page.url if page else "N/A"
                    title = (page.title() if page else "N/A") or "N/A"
                except Exception:
                    cur = "N/A"
                    title = "N/A"
                safe_print(f"[Crawler Error] keyword={keyword}, current_url={cur}, title={title}, error={e!r}")
                self._last_error = {
                    "code": "PLAYWRIGHT_EXCEPTION",
                    "message": repr(e),
                }
                return None

    def crawl_coupang(self, keyword: str) -> Dict[str, Any]:
        kw = str(keyword or "").strip()
        if not kw:
            return self._result_with_reason("EMPTY_KEYWORD")

        ck = self._cache_key(kw)
        if ck in self._cache:
            self._stats["cache_hit"] += 1
            return dict(self._cache[ck])

        result = self._crawl_with_playwright(kw)
        if result is not None:
            self._cache[ck] = result
            self._last_success_cache[kw] = result
            if self._last_fetch_source == "requests":
                self._stats["requests_ok"] += 1
            else:
                self._stats["playwright_ok"] += 1
            result["reason_code"] = "OK"
            return dict(result)

        self._stats["failed"] += 1
        cached = self.get_cached_result(kw)
        if cached is not None:
            cached["reason_code"] = "CACHE_FALLBACK"
            return cached
        if self._stats.get("blocked", 0) > 0:
            reason = "BLOCKED_BY_WAF" if STEALTH_AVAILABLE else "BLOCKED_BY_WAF_NO_STEALTH"
            return self._result_with_reason(reason)
        code = str((self._last_error or {}).get("code", "")).strip().upper()
        if code == "PLAYWRIGHT_SELECTOR_TIMEOUT":
            return self._result_with_reason("PLAYWRIGHT_TIMEOUT")
        if code == "PLAYWRIGHT_NO_PRODUCTS":
            return self._result_with_reason("NO_PRODUCTS_PARSED")
        if code.startswith("REQUESTS_HTTP_"):
            return self._result_with_reason(code)
        if code == "REQUESTS_NO_PRODUCTS":
            return self._result_with_reason("REQUESTS_NO_PRODUCTS")
        if code == "REQUESTS_EXCEPTION":
            return self._result_with_reason("REQUESTS_EXCEPTION")
        if code == "PLAYWRIGHT_EXCEPTION":
            return self._result_with_reason("PLAYWRIGHT_EXCEPTION")
        if code == "PLAYWRIGHT_BLOCKED_BY_WAF_INITIAL":
            return self._result_with_reason("BLOCKED_BY_WAF_INITIAL")
        return self._result_with_reason("CRAWL_FAILED")

    def is_smoke_playwright_running(self) -> bool:
        self._maybe_reap_smoke_subprocess()
        with self._io_lock:
            sub = self._smoke_subproc
            t = self._smoke_thread
        if sub is not None and sub.poll() is None:
            return True
        return t is not None and t.is_alive()

    def stop_smoke_playwright_chromium_window(self, join_timeout: float = 20.0) -> None:
        """백그라운드 스모크 Chromium을 즉시 닫도록 신호를 보낸 뒤 스레드 종료를 기다린다."""
        self._terminate_smoke_subprocess_if_any()
        thr: Optional[threading.Thread] = None
        with self._io_lock:
            ev = self._smoke_stop_event
            thr = self._smoke_thread
        if ev is not None:
            ev.set()
        if thr is not None:
            thr.join(timeout=max(1.0, float(join_timeout)))
        with self._io_lock:
            self._smoke_thread = None
            self._smoke_stop_event = None

    def _run_smoke_worker(self, url: str, max_wait_seconds: float, stop_event: threading.Event) -> None:
        """별도 스레드에서 실행. persistent 크롤 세션과 무관한 ephemeral Chromium."""
        target = str(url).strip() or "https://www.google.com/"
        with self._io_lock:
            self._smoke_preview_png = None
        hint_h_env = "headless=True (COUPANG_SMOKE_HEADLESS) — OS 창 없음. 캡처·phase로 확인."
        hint_h_auto = (
            "headless=True — Linux에 DISPLAY가 없어 자동 headless입니다. "
            "Railway/Docker에서는 스크린샷·phase로만 확인됩니다."
        )
        hint_v = (
            "headless=False — Playwright Chromium이 별도 창으로 보여야 합니다 (로컬 Windows 등)."
        )
        self._smoke_status_update(
            phase="launching",
            target_url=target,
            page_url="",
            page_title="",
            opened_at=None,
            closed_at=None,
            error="",
            top3_items=[],
            hint="브라우저 기동 중…",
        )
        self._sanitize_playwright_browser_env()
        self._log_playwright_preflight()
        _ensure_windows_proactor_policy()
        # env 명시 시 우선. 미설정 시 DISPLAY 없는 Linux는 headed 불가 → _prep_force_headless 와 동일하게 headless.
        _sh = str(os.environ.get("COUPANG_SMOKE_HEADLESS", "")).strip().lower()
        if _sh in {"1", "true", "y", "yes"}:
            use_headless = True
            _smoke_hint = hint_h_env
        elif _sh in {"0", "false", "n", "no"}:
            use_headless = False
            if sys.platform != "win32" and not (os.environ.get("DISPLAY") or "").strip():
                safe_print(
                    "[SMOKE] COUPANG_SMOKE_HEADLESS=false 이지만 DISPLAY가 없어 headless로 강제합니다."
                )
                use_headless = True
                _smoke_hint = hint_h_auto
            else:
                _smoke_hint = hint_v
        elif self._prep_force_headless():
            use_headless = True
            _smoke_hint = hint_h_auto
            safe_print("[SMOKE] DISPLAY 없음 — 스모크는 headless로 실행합니다.")
        else:
            use_headless = False
            _smoke_hint = hint_v
        self._smoke_status_update(headless=use_headless, hint=_smoke_hint)
        _raw_ss = os.environ.get("COUPANG_SMOKE_STORAGE_STATE")
        smoke_storage_state_path = str(_raw_ss).strip() if _raw_ss is not None else ""
        if not smoke_storage_state_path:
            smoke_storage_state_path = ".smoke/coupang_state.json"
        elif smoke_storage_state_path.lower() in {"0", "off", "false", "none"}:
            smoke_storage_state_path = ""
        context_kwargs: Dict[str, Any] = {
            "viewport": {"width": 1280, "height": 900},
            "locale": "ko-KR",
        }
        if smoke_storage_state_path:
            try:
                _ssp = Path(smoke_storage_state_path).expanduser()
                if not _ssp.is_absolute():
                    _ssp = (Path(__file__).resolve().parent / _ssp).resolve()
                smoke_storage_state_path = str(_ssp)
                if _ssp.is_file():
                    context_kwargs["storage_state"] = smoke_storage_state_path
                    safe_print(f"[SMOKE] storage_state 로드: {smoke_storage_state_path}")
                else:
                    safe_print(f"[SMOKE] storage_state 파일 없음(신규 세션 시작): {smoke_storage_state_path}")
            except Exception as ss_e:
                safe_print(f"[SMOKE] storage_state 경로 처리 실패(무시): {ss_e!r}")
                smoke_storage_state_path = ""
        _channel = str(os.environ.get("COUPANG_PLAYWRIGHT_CHANNEL", "")).strip() or None
        pw: Optional[Playwright] = None
        browser = None
        failed = False
        try:
            pw = sync_playwright().start()
            self._smoke_status_update(phase="playwright_started")
            browser = pw.chromium.launch(
                headless=use_headless,
                channel=_channel,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1280,900",
                ],
            )
            self._smoke_status_update(phase="chromium_launched")
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            page.set_default_timeout(30000)
            self._smoke_status_update(phase="navigating")
            page.goto(target, wait_until="domcontentloaded")
            safe_print(f"[SMOKE] Playwright Chromium 준비 완료 url={target} headless={use_headless}")
            try:
                page.evaluate(
                    """async () => {
                        const link = document.createElement('link');
                        link.rel = 'stylesheet';
                        link.href = 'https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;700&display=swap';
                        document.head.appendChild(link);
                        await new Promise((r) => setTimeout(r, 500));
                        if (document.fonts && document.fonts.ready) {
                            await document.fonts.ready;
                        }
                    }"""
                )
                page.add_style_tag(
                    content="html, body, input, textarea, button { font-family: 'Noto Sans KR', sans-serif !important; }"
                )
                page.wait_for_timeout(500)
            except Exception as fe:
                safe_print(f"[SMOKE] 웹폰트 주입 생략/실패: {fe!r}")

            # 구글 검색창에 입력 후 Enter (로컬 headed에서 타이핑이 보임). 비활성: COUPANG_SMOKE_GOOGLE_QUERY=""
            _raw_sq = os.environ.get("COUPANG_SMOKE_GOOGLE_QUERY")
            google_query = "쿠팡" if _raw_sq is None else str(_raw_sq).strip()
            if google_query:
                try:
                    self._accept_google_consent_if_present(page)
                    self._smoke_status_update(
                        phase="google_search_input",
                        hint=f"구글 검색창에 입력 중: {google_query!r} (한 글자씩 표시)",
                    )
                    box = page.locator("textarea[name='q'], input[name='q']").first
                    box.wait_for(state="visible", timeout=15000)
                    box.click(timeout=5000)
                    page.wait_for_timeout(250)
                    box.fill("")
                    page.keyboard.type(google_query, delay=100)
                    page.wait_for_timeout(400)
                    page.keyboard.press("Enter")
                    try:
                        page.wait_for_url(re.compile(r"/search\?"), timeout=25000)
                    except Exception:
                        safe_print("[SMOKE] 검색 결과 URL 대기 타임아웃 — 계속 진행")
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(1200)
                    self._smoke_status_update(
                        phase="google_search_done",
                        hint=f"구글 검색 완료: {google_query!r}",
                    )
                    safe_print(f"[SMOKE] 구글 검색 실행 완료 query={google_query!r}")

                    # 검색 결과 화면에서 마우스를 한 바퀴 움직인 뒤 쿠팡 공식 도메인 링크 클릭 (headed 시각 확인용)
                    try:
                        self._smoke_status_update(
                            phase="smoke_mouse_circle",
                            hint="검색 결과 창 안에서 마우스 포인터를 원형으로 한 바퀴 이동합니다.",
                        )
                        vp = page.viewport_size or {"width": 1280, "height": 900}
                        vw = float(vp.get("width", 1280))
                        vh = float(vp.get("height", 900))
                        cx = vw / 2.0
                        cy = vh / 2.0
                        radius = min(vw, vh) * 0.22
                        steps = 52
                        page.mouse.move(cx + radius, cy)
                        page.wait_for_timeout(40)
                        for _i in range(1, steps + 1):
                            ang = (2.0 * math.pi * _i) / steps
                            page.mouse.move(
                                cx + radius * math.cos(ang),
                                cy + radius * math.sin(ang),
                            )
                            page.wait_for_timeout(12)
                        page.wait_for_timeout(200)

                        self._smoke_status_update(
                            phase="smoke_find_coupang_link",
                            hint="검색 결과에서 https://www.coupang.com 또는 coupang.com 링크를 찾아 클릭합니다.",
                        )
                        coupang_locators = [
                            page.locator("a").filter(
                                has_text=re.compile(r"https://www\.coupang\.com", re.I)
                            ).first,
                            page.locator('a[href^="https://www.coupang.com"]').first,
                            page.locator('a[href*="www.coupang.com"]').first,
                            page.locator('a[href*="coupang.com"]').first,
                        ]
                        clicked = False
                        last_pick_err: Optional[Exception] = None
                        for loc in coupang_locators:
                            try:
                                loc.wait_for(state="visible", timeout=6000)
                                loc.scroll_into_view_if_needed(timeout=5000)
                                ctx = page.context
                                n_before = len(ctx.pages)
                                loc.click(timeout=15000)
                                page.wait_for_timeout(350)
                                if len(ctx.pages) > n_before:
                                    page = ctx.pages[-1]
                                    page.wait_for_load_state("domcontentloaded", timeout=25000)
                                else:
                                    page.wait_for_url(
                                        re.compile(r"coupang\.com"),
                                        timeout=25000,
                                    )
                                    page.wait_for_load_state("domcontentloaded")
                                page.wait_for_timeout(600)
                                clicked = True
                                break
                            except Exception as pe:
                                last_pick_err = pe
                                continue
                        if clicked:
                            self._smoke_status_update(
                                phase="smoke_coupang_opened",
                                hint="쿠팡 페이지로 이동했습니다.",
                            )
                            safe_print("[SMOKE] 검색 결과에서 쿠팡 링크 클릭 후 로드까지 완료")

                            # 쿠팡 진입 후 마우스 원형 2바퀴 + 검색창 2회 클릭 + 검색어 입력 + Enter
                            try:
                                self._smoke_status_update(
                                    phase="smoke_coupang_mouse_circle",
                                    hint="쿠팡 화면 안에서 마우스 포인터를 원형으로 2바퀴 이동합니다.",
                                )
                                page.wait_for_timeout(700)
                                vp2 = page.viewport_size or {"width": 1280, "height": 900}
                                vw2 = float(vp2.get("width", 1280))
                                vh2 = float(vp2.get("height", 900))
                                cx2 = vw2 / 2.0
                                cy2 = vh2 / 2.0
                                radius2 = min(vw2, vh2) * 0.20
                                turns = 2
                                steps2 = 52 * turns
                                page.mouse.move(cx2 + radius2, cy2)
                                page.wait_for_timeout(40)
                                for _j in range(1, steps2 + 1):
                                    ang2 = (2.0 * math.pi * _j) / 52.0
                                    page.mouse.move(
                                        cx2 + radius2 * math.cos(ang2),
                                        cy2 + radius2 * math.sin(ang2),
                                    )
                                    page.wait_for_timeout(10)
                                page.wait_for_timeout(220)

                                _raw_cq = os.environ.get("COUPANG_SMOKE_COUPANG_QUERY")
                                search_kw = "그램 노트북" if _raw_cq is None else str(_raw_cq).strip()
                                if not search_kw:
                                    raise RuntimeError("COUPANG_SMOKE_COUPANG_QUERY 가 비어 있습니다.")
                                self._smoke_status_update(
                                    phase="smoke_coupang_search_input",
                                    hint=f"쿠팡 검색창에 입력 중: {search_kw!r}",
                                )
                                input_locators = [
                                    page.locator("input[name='q']").first,
                                    page.get_by_placeholder("찾고 싶은 상품을 검색해보세요!").first,
                                    page.locator("input[placeholder*='상품']").first,
                                    page.locator("input[type='search']").first,
                                    page.locator("header input").first,
                                ]
                                search_box = None
                                for in_loc in input_locators:
                                    try:
                                        in_loc.wait_for(state="visible", timeout=5000)
                                        search_box = in_loc
                                        break
                                    except Exception:
                                        continue
                                if search_box is None:
                                    raise RuntimeError("쿠팡 검색 입력창을 찾지 못했습니다.")

                                search_box.click(timeout=5000)
                                page.wait_for_timeout(200)
                                search_box.click(timeout=5000)
                                page.wait_for_timeout(220)
                                search_box.fill("")
                                page.keyboard.type(search_kw, delay=95)
                                page.wait_for_timeout(250)

                                self._smoke_status_update(
                                    phase="smoke_coupang_search_enter",
                                    hint=f"쿠팡 검색 Enter 실행: {search_kw!r}",
                                )
                                page.keyboard.press("Enter")
                                try:
                                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                                except Exception:
                                    pass
                                try:
                                    page.wait_for_load_state("networkidle", timeout=18000)
                                except Exception:
                                    pass
                                page.wait_for_timeout(600)
                                try:
                                    page.wait_for_selector(
                                        "li.ProductUnit_productUnit__Qd6sv, li.search-product, "
                                        "ul#product-list > li, ul#productList li, li[data-product-id]",
                                        timeout=22000,
                                        state="attached",
                                    )
                                except Exception:
                                    safe_print("[SMOKE] 상품 리스트 DOM 대기 타임아웃 — probe는 계속 시도")
                                page.wait_for_timeout(500)
                                self._smoke_status_update(
                                    phase="smoke_coupang_search_done",
                                    hint=f"쿠팡 검색 실행 완료: {search_kw!r}",
                                )
                                safe_print(f"[SMOKE] 쿠팡 검색 실행 완료 query={search_kw!r}")
                                try:
                                    self._smoke_status_update(
                                        phase="smoke_coupang_html_probe",
                                        hint="결과 페이지 HTML에서 상품명/가격 추출 가능 여부를 확인합니다.",
                                    )
                                    _probe_js = r"""() => {
                                        const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
                                        const text = (el) => (el ? norm(el.textContent) : "");
                                        const pickPrice = (card) => {
                                            // 1) custom-oos 대표 판매가 우선
                                            const cands = card.querySelectorAll(
                                                ".custom-oos span, .custom-oos div, [class*='custom-oos'] span"
                                            );
                                            for (const n of cands) {
                                                const t = norm(n.innerText || n.textContent || "");
                                                if (!t || t.includes("개당")) continue;
                                                const mm = t.match(/[\d,]+\s*원/);
                                                if (mm) return norm(mm[0].replace(/\s+/g, ""));
                                            }
                                            // 2) fallback
                                            const area = card.querySelector(".PriceArea_priceArea__NntJz")
                                                || card.querySelector(".sale-price")
                                                || card.querySelector("[class*='price']");
                                            if (!area) return "";
                                            const blob = norm(area.innerText || area.textContent || "");
                                            const re = /[\d,]+\s*원/g;
                                            let first = "";
                                            let m;
                                            while ((m = re.exec(blob)) !== null) {
                                                if (!first) first = m[0];
                                            }
                                            return first ? norm(first.replace(/\s+/g, "")) : "";
                                        };
                                        const pickShippingKeywords = (blob) => {
                                            if (!blob) return "";
                                            const kws = [
                                                "로켓배송", "판매자로켓", "로켓직구", "로켓그로스", "새벽배송",
                                                "오늘 출발", "오늘출발", "도착보장", "내일도착", "내일 도착",
                                                "무료배송", "판매자 배송", "판매자배송",
                                            ];
                                            const seen = [];
                                            for (let i = 0; i < kws.length; i++) {
                                                const kw = kws[i];
                                                if (blob.includes(kw) && seen.indexOf(kw) === -1) seen.push(kw);
                                            }
                                            return seen.join(" / ");
                                        };
                                        const pickShipping = (card) => {
                                            const fee = card.querySelector(
                                                ".TextBadge_feePrice__n_gta, [data-badge-type='feePrice']"
                                            );
                                            if (fee) return norm(fee.textContent);
                                            const trySels = [
                                                "[class*='DeliveryInfo']",
                                                "[class*='deliveryInfo']",
                                                "[class*='DeliveryBadge']",
                                                "[class*='RocketBadge']",
                                                "[class*='RocketDelivery']",
                                                "[class*='rocketDelivery']",
                                                "[class*='ProductUnit_badge']",
                                                "[class*='ImageBadge']",
                                                "[class*='BadgeList']",
                                            ];
                                            for (let i = 0; i < trySels.length; i++) {
                                                const n = card.querySelector(trySels[i]);
                                                if (n) {
                                                    const t = norm(n.textContent);
                                                    if (t && !/^\d+%$/.test(t)) return t;
                                                }
                                            }
                                            const badgeBlob = Array.from(card.querySelectorAll(
                                                "[class*='Badge'], [class*='badge'], [class*='Delivery'], "
                                                + "[class*='delivery'], [class*='Label'], [class*='label'], "
                                                + "[class*='Rocket'], [class*='rocket'], [data-badge-type]"
                                            )).map((n) => norm(n.textContent)).join(" ");
                                            let kw = pickShippingKeywords(badgeBlob);
                                            if (kw) return kw;
                                            kw = pickShippingKeywords(norm(card.innerText || card.textContent || ""));
                                            return kw;
                                        };
                                        const pickReviewScore = (card) => {
                                            const wrap = card.querySelector(".ProductRating_productRating__jjf7W");
                                            if (!wrap) return "";
                                            const labeled = wrap.querySelector("[aria-label]");
                                            if (labeled) {
                                                const al = norm(labeled.getAttribute("aria-label") || "");
                                                const am = al.match(/(\d+(?:\.\d+)?)/);
                                                if (am) return am[1];
                                            }
                                            const starSels = ["em", "strong", "[class*='rating']"];
                                            for (let si = 0; si < starSels.length; si++) {
                                                const n = wrap.querySelector(starSels[si]);
                                                if (n) {
                                                    const t = norm(n.textContent);
                                                    const tm = t.match(/(\d+(?:\.\d+)?)/);
                                                    if (tm) return tm[1];
                                                }
                                            }
                                            return "";
                                        };
                                        const pickProductUrl = (card) => {
                                            let a = card.querySelector(
                                                "a[href*='vp/products'], a[href*='/products/'], "
                                                + "a[href*='www.coupang.com/vp/'], a[href^='/vp/products']"
                                            );
                                            if (!a) a = card.querySelector("a[href]");
                                            if (!a) return "";
                                            let href = (a.getAttribute("href") || "").trim();
                                            if (!href) return "";
                                            if (href.startsWith("/")) href = "https://www.coupang.com" + href;
                                            return href;
                                        };
                                        const pickReview = (card) => {
                                            const el = card.querySelector(
                                                ".ProductRating_productRating__jjf7W [class*='fw-text-'], "
                                                + ".rating-total-count, .rating-count, .count"
                                            );
                                            const t = el ? norm(el.textContent) : "";
                                            const paren = t.match(/\(\s*([\d,]+)\s*\)/);
                                            if (paren) return paren[1].replace(/,/g, "");
                                            const digits = t.match(/[\d,]+/);
                                            return digits ? digits[0].replace(/,/g, "") : t;
                                        };
                                        const cards = Array.from(document.querySelectorAll(
                                            "li.ProductUnit_productUnit__Qd6sv, li.search-product, "
                                            + "ul#product-list > li, ul#productList li, li[data-product-id]"
                                        ));
                                        const isAd = (card) => {
                                            if (card.querySelector(
                                                ".search-product__ad-badge, .search-product__ad, .ad-badge-text"
                                            )) return true;
                                            if (norm(card.textContent).includes("광고")
                                                && !card.querySelector("[class*='RankMark_rank']")) return true;
                                            return false;
                                        };
                                        const extract = (card) => {
                                            const titleEl = card.querySelector(
                                                ".ProductUnit_productNameV2__cV9cw, .name"
                                            );
                                            return {
                                                title: text(titleEl),
                                                price: pickPrice(card),
                                                shipping: pickShipping(card),
                                                review_count: pickReview(card),
                                                review_score: pickReviewScore(card),
                                                url: pickProductUrl(card),
                                            };
                                        };
                                        const organic = [];
                                        for (const card of cards) {
                                            if (isAd(card)) continue;
                                            const row = extract(card);
                                            if (row.title && /[\d,]+원/.test(row.price)) organic.push(row);
                                        }
                                        const top3 = organic.slice(0, 3).map((row, idx) => ({
                                            rank: idx + 1,
                                            title: row.title,
                                            price: row.price,
                                            shipping: row.shipping,
                                            review_count: row.review_count,
                                            review_score: row.review_score,
                                            url: row.url,
                                        }));
                                        const sample = organic.slice(0, 5).map((row) => ({
                                            name: row.title,
                                            price: row.price,
                                            review_score: row.review_score,
                                            url: row.url,
                                        }));
                                        const html = document.documentElement && document.documentElement.outerHTML;
                                        return {
                                            url: location.href,
                                            title: document.title || "",
                                            html_len: html ? html.length : 0,
                                            card_count: cards.length,
                                            organic_count: organic.length,
                                            top3: top3,
                                            sample: sample,
                                        };
                                    }"""
                                    probe: Optional[Dict[str, Any]] = None
                                    last_ev: Optional[Exception] = None
                                    for _probe_try in range(4):
                                        try:
                                            probe = page.evaluate(_probe_js)
                                            break
                                        except Exception as ev_e:
                                            last_ev = ev_e
                                            msg = str(ev_e).lower()
                                            if (
                                                "execution context was destroyed" in msg
                                                or "navigation" in msg
                                            ):
                                                page.wait_for_timeout(900 + _probe_try * 700)
                                                try:
                                                    page.wait_for_load_state(
                                                        "domcontentloaded", timeout=20000
                                                    )
                                                except Exception:
                                                    pass
                                                continue
                                            raise
                                    if probe is None:
                                        raise last_ev or RuntimeError("HTML probe evaluate failed")
                                    self._smoke_status_update(top3_items=probe.get("top3", []))
                                    safe_print(
                                        "[SMOKE] HTML probe: "
                                        f"url={probe.get('url','')} "
                                        f"title={probe.get('title','')!r} "
                                        f"html_len={probe.get('html_len',0)} "
                                        f"card_count={probe.get('card_count',0)} "
                                        f"organic_count={probe.get('organic_count', 0)}"
                                    )
                                    safe_print(f"[SMOKE] HTML probe top3={probe.get('top3', [])!r}")
                                    safe_print(f"[SMOKE] HTML probe sample={probe.get('sample', [])!r}")
                                    smoke_payload = {
                                        "saved_at": datetime.now().isoformat(timespec="seconds"),
                                        "keyword": search_kw,
                                        "source_type": "smoke",
                                        **probe,
                                    }
                                    _dump_smoke_extract_report(smoke_payload)
                                    _persist_smoke_extract_report_to_db(smoke_payload)
                                except Exception as hp_e:
                                    safe_print(f"[SMOKE] HTML probe 실패(무시): {hp_e!r}")
                                    smoke_payload = {
                                        "saved_at": datetime.now().isoformat(timespec="seconds"),
                                        "keyword": search_kw,
                                        "source_type": "smoke",
                                        "error": repr(hp_e),
                                        "top3": [],
                                        "card_count": None,
                                    }
                                    _dump_smoke_extract_report(smoke_payload)
                                    _persist_smoke_extract_report_to_db(smoke_payload)
                            except Exception as ce2:
                                safe_print(f"[SMOKE] 쿠팡 검색 자동 시연 단계 실패: {ce2!r}")
                        else:
                            safe_print(
                                f"[SMOKE] 쿠팡 링크 클릭 단계 건너뜀/실패 — 스크린샷은 현 SERP 기준: {last_pick_err!r}"
                            )
                    except Exception as ce:
                        safe_print(f"[SMOKE] 마우스 원형/쿠팡 클릭 단계 실패 — 스크린샷만 진행: {ce!r}")
                except Exception as se:
                    safe_print(f"[SMOKE] 구글 검색 단계 실패 — 스크린샷만 진행: {se!r}")

            try:
                png = page.screenshot(type="png", full_page=False)
            except Exception as cap_err:
                safe_print(f"[SMOKE] 스크린샷 생략: {cap_err!r}")
                png = None
            with self._io_lock:
                self._smoke_preview_png = png
            try:
                ptitle = page.title()
                purl = page.url
            except Exception:
                ptitle = ""
                purl = ""
            self._smoke_status_update(
                phase="opened",
                page_url=purl,
                page_title=ptitle,
                opened_at=time.time(),
                hint=(
                    "첫 로드 완료. 초기 1회 + 10초 간격 3회(총 4회) 자동 캡처를 진행합니다."
                ),
            )
            with self._io_lock:
                self._last_error = {}

            # 요청 사양: 초기 화면 1회 + 10초 간격 3회 추가 캡처
            # 총 4장의 캡처를 파일로 남기고, 마지막 캡처를 대시보드 미리보기에 유지한다.
            shot_plan = [
                (0, "step0_open"),
                (10, "step1_10s"),
                (20, "step2_20s"),
                (30, "step3_30s"),
            ]
            start_ts = time.monotonic()
            for sec_mark, shot_tag in shot_plan:
                while (time.monotonic() - start_ts) < float(sec_mark):
                    if stop_event.is_set():
                        break
                    time.sleep(0.2)
                if stop_event.is_set():
                    break
                try:
                    shot_png = page.screenshot(type="png", full_page=False)
                    with self._io_lock:
                        self._smoke_preview_png = shot_png
                    saved = self._save_smoke_screenshot_file(shot_png, shot_tag)
                    if saved:
                        safe_print(f"[SMOKE] 스크린샷 저장: {saved}")
                except Exception as shot_err:
                    safe_print(f"[SMOKE] 주기 캡처 실패(무시): {shot_err!r}")

            deadline = time.monotonic() + float(max_wait_seconds)
            poll = 0.5
            self._smoke_status_update(phase="holding")
            while time.monotonic() < deadline:
                if stop_event.is_set():
                    safe_print("[SMOKE] 사용자 강제 종료 신호 수신.")
                    self._smoke_status_update(hint="강제 종료 신호 수신, 브라우저를 닫는 중…")
                    break
                time.sleep(poll)
            if smoke_storage_state_path:
                try:
                    Path(smoke_storage_state_path).parent.mkdir(parents=True, exist_ok=True)
                    context.storage_state(path=smoke_storage_state_path)
                    safe_print(f"[SMOKE] storage_state 저장: {smoke_storage_state_path}")
                except Exception as ss_w:
                    safe_print(f"[SMOKE] storage_state 저장 실패(무시): {ss_w!r}")
            safe_print("[SMOKE] 유지 시간 종료 또는 중지에 따라 브라우저를 닫습니다.")
        except Exception as e:
            failed = True
            safe_print(f"[SMOKE] Playwright Chromium 실패: {e!r}")
            self._smoke_status_update(phase="failed", error=str(e), closed_at=time.time())
            with self._io_lock:
                self._last_error = {"code": "SMOKE_CHROMIUM", "message": str(e)}
        finally:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                if pw is not None:
                    pw.stop()
            except Exception:
                pass
            if not failed:
                self._smoke_status_update(phase="closed", closed_at=time.time())
            with self._io_lock:
                if self._smoke_thread is threading.current_thread():
                    self._smoke_thread = None
                    self._smoke_stop_event = None

    def smoke_open_playwright_chromium_window(
        self,
        url: str = "https://www.google.com/",
        wait_seconds: float = 300.0,
    ) -> bool:
        """
        크롤러 persistent 세션과 별도로 Playwright 번들 Chromium을 별도 창으로 연다.
        Streamlit 요청을 막지 않도록 백그라운드 스레드에서 최대 wait_seconds 동안 유지한다.
        대시보드의 강제 종료 버튼은 stop_smoke_playwright_chromium_window() 로 즉시 닫을 수 있다.
        """
        return self.start_smoke_playwright_chromium_window(url=url, max_wait_seconds=wait_seconds)

    def start_smoke_playwright_chromium_window(
        self,
        url: str = "https://www.google.com/",
        max_wait_seconds: float = 300.0,
    ) -> bool:
        max_wait_seconds = max(5.0, float(max_wait_seconds))
        target = str(url).strip() or "https://www.google.com/"
        old_thr: Optional[threading.Thread] = None
        old_ev: Optional[threading.Event] = None
        with self._io_lock:
            old_thr = self._smoke_thread
            old_ev = self._smoke_stop_event
            self._smoke_preview_png = None
        if old_ev is not None:
            old_ev.set()
        if old_thr is not None:
            old_thr.join(timeout=20.0)

        self._terminate_smoke_subprocess_if_any()

        self._smoke_status_update(
            phase="queued",
            target_url=target,
            headless=None,
            page_url="",
            page_title="",
            opened_at=None,
            closed_at=None,
            error="",
            hint="스모크 Chromium을 시작합니다.",
        )

        if CoupangCrawler._smoke_use_subprocess_launch():
            tmpd = tempfile.mkdtemp(prefix="modiba_pwsmoke_")
            stopf = os.path.join(tmpd, "stop.txt")
            script = os.path.abspath(__file__)
            cmd = [
                sys.executable,
                script,
                "--smoke-child",
                target,
                str(int(max_wait_seconds)),
                stopf,
            ]
            cwd = os.path.dirname(script)
            cflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=cflags,
                )
            except OSError as exc:
                self._smoke_status_update(
                    phase="failed",
                    error=f"subprocess_spawn:{exc}",
                    closed_at=time.time(),
                    hint=str(exc),
                )
                shutil.rmtree(tmpd, ignore_errors=True)
                return False
            with self._io_lock:
                self._smoke_subproc = proc
                self._smoke_stop_file = stopf
                self._smoke_tmpdir = tmpd
                self._smoke_thread = None
                self._smoke_stop_event = None
            self._smoke_status_update(
                phase="windows_subprocess",
                headless=False,
                subprocess_pid=proc.pid,
                hint=(
                    "자식 프로세스에서 headed Chromium을 실행했습니다. 작업 표시줄에서 창을 확인하세요. "
                    "브라우저로 Railway 주소만 연 경우 창은 서버에만 뜨고 이 PC에는 보이지 않습니다."
                ),
            )
            return True

        with self._io_lock:
            self._smoke_stop_event = threading.Event()
            stop_ev = self._smoke_stop_event

        def runner() -> None:
            self._run_smoke_worker(target, max_wait_seconds, stop_ev)

        t = threading.Thread(target=runner, name="pw-smoke-chromium", daemon=True)
        with self._io_lock:
            self._smoke_thread = t
        t.start()
        return True

    def get_stats(self) -> Dict[str, int]:
        return dict(self._stats)

    def get_last_error(self) -> Dict[str, str]:
        return dict(self._last_error)

    def close(self) -> None:
        try:
            self.stop_smoke_playwright_chromium_window(join_timeout=15.0)
        except Exception:
            pass
        with self._io_lock:
            if self._page is not None:
                try:
                    self._page.close()
                except Exception:
                    pass
                self._page = None
            if self._context is not None:
                try:
                    self._context.close()
                except Exception:
                    pass
                self._context = None
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None


_shared_crawler: Optional[CoupangCrawler] = None


def get_shared_crawler() -> CoupangCrawler:
    global _shared_crawler
    if _shared_crawler is None:
        _shared_crawler = CoupangCrawler()
    return _shared_crawler


def crawl_coupang(keyword: str) -> Dict[str, float]:
    return get_shared_crawler().crawl_coupang(keyword)


def _shutdown() -> None:
    global _shared_crawler
    if _shared_crawler is not None:
        _shared_crawler.close()


atexit.register(_shutdown)

def save_to_excel(result_dict: Dict[str, Any]):
    if result_dict.get("top10_items"):
        try:
            import pandas as pd
            df = pd.DataFrame(result_dict["top10_items"])
            df.to_excel("results.xlsx", index=False)
            safe_print("[System] 결과를 results.xlsx 파일로 성공적으로 저장했습니다.")
        except ImportError:
            safe_print("[System] pandas 또는 openpyxl 모듈이 설치되어 있지 않아 엑셀 저장을 건너뜁니다.")


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--smoke-child":
        _child_url = sys.argv[2]
        _child_sec = float(sys.argv[3])
        _child_stopf = sys.argv[4]
        _ensure_windows_proactor_policy()
        _smoke_crawler = CoupangCrawler()
        _smoke_ev = threading.Event()

        def _watch_smoke_stop_file() -> None:
            while not _smoke_ev.wait(0.35):
                try:
                    if os.path.isfile(_child_stopf):
                        _smoke_ev.set()
                        return
                except OSError:
                    pass

        threading.Thread(target=_watch_smoke_stop_file, daemon=True).start()
        _smoke_crawler._run_smoke_worker(_child_url, _child_sec, _smoke_ev)
        sys.exit(0)

    parser = argparse.ArgumentParser(description="Coupang crawler utility")
    parser.add_argument("--keyword", default="페이스 리프팅 밴드", help="검색 키워드")
    parser.add_argument(
        "--bootstrap-login",
        action="store_true",
        help="전용 프로필로 로그인 세션을 저장하는 1회 실행 모드",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=120,
        help="bootstrap-login 모드에서 로그인 대기 시간(초)",
    )
    parser.add_argument(
        "--open-search-ready",
        action="store_true",
        help="쿠팡 메인 접속 후 검색창 수동 입력 대기 모드",
    )
    parser.add_argument(
        "--open-google-ready",
        action="store_true",
        help="Google 홈 화면만 열어두고 수동 검색 대기 모드",
    )
    parser.add_argument(
        "--parse-url",
        default="",
        help="사용자가 전달한 쿠팡 검색결과 URL 파싱 모드",
    )
    parser.add_argument(
        "--open-home-ready",
        action="store_true",
        help="쿠팡 홈 접속 확인 후 수동 조작 대기 모드",
    )
    parser.add_argument(
        "--parse-local-html",
        default="",
        help="직접 저장한 로컬 HTML 파일을 파싱하는 모드",
    )
    args = parser.parse_args()

    crawler = get_shared_crawler()
    
    result_data = None

    if args.bootstrap_login:
        ok = crawler.bootstrap_login_session(wait_seconds=args.wait_seconds)
        safe_print({"bootstrap_login": bool(ok), "profile_dir": crawler._chrome_user_data_dir, "profile": crawler._chrome_profile})
    elif args.open_home_ready:
        ok = crawler.open_home_ready_session(wait_seconds=args.wait_seconds)
        safe_print({"open_home_ready": bool(ok), "profile_dir": crawler._chrome_user_data_dir, "profile": crawler._chrome_profile})
    elif args.open_google_ready:
        ok = crawler.open_google_ready_session(wait_seconds=args.wait_seconds)
        safe_print({"open_google_ready": bool(ok), "profile_dir": crawler._chrome_user_data_dir, "profile": crawler._chrome_profile})
    elif args.open_search_ready:
        ok = crawler.open_search_ready_session(wait_seconds=args.wait_seconds)
        safe_print({"open_search_ready": bool(ok), "profile_dir": crawler._chrome_user_data_dir, "profile": crawler._chrome_profile})
    elif args.parse_url:
        result_data = crawler.parse_coupang_search_url(args.parse_url)
        safe_print(result_data)
        safe_print(crawler.get_stats())
    elif args.parse_local_html:
        result_data = crawler.parse_local_html(args.parse_local_html)
        safe_print(result_data)
        safe_print(crawler.get_stats())
    else:
        result_data = crawler.crawl_coupang(args.keyword)
        safe_print(result_data)
        safe_print(crawler.get_stats())

    # 결과가 존재하면 엑셀로 저장 (pandas 필요)
    if result_data:
        save_to_excel(result_data)
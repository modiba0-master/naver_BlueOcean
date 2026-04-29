import argparse
import atexit
import os
import random
import re
import time

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/root/.cache/ms-playwright"
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse, parse_qsl, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, Error, Page, Playwright, TimeoutError, sync_playwright
from playwright.sync_api import sync_playwright

# CP949 환경에서도 깨지지 않게 출력하기 위한 유틸 함수
def safe_print(*args, **kwargs):
    text = " ".join(map(str, args))
    try:
        print(text, **kwargs)
    except UnicodeEncodeError:
        print(text.encode("cp949", errors="ignore").decode("cp949", errors="ignore"), **kwargs)

# [수정] 명칭 불일치 및 임포트 에러 완벽 방어 (Soft Import)
try:
    import playwright_stealth
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

def apply_stealth(page: Page):
    """라이브러리 버전에 상관없이 안전하게 스텔스 적용"""
    if not STEALTH_AVAILABLE:
        return
    try:
        # 1. stealth_sync 시도
        if hasattr(playwright_stealth, 'stealth_sync'):
            playwright_stealth.stealth_sync(page)
            safe_print("[INFO] stealth_sync applied.")
        # 2. sync_stealth 시도 (버전에 따라 이름이 다를 수 있음)
        elif hasattr(playwright_stealth, 'sync_stealth'):
            playwright_stealth.sync_stealth(page)
            safe_print("[INFO] sync_stealth applied.")
        # 3. 범용 stealth 시도 (최신 버전에서 주로 사용됨)
        elif hasattr(playwright_stealth, 'stealth'):
            playwright_stealth.stealth(page)
            safe_print("[INFO] stealth applied.")
        else:
            safe_print("[WARN] No known stealth function found in package.")
    except Exception as e:
        safe_print(f"[ERROR] Failed to apply stealth: {e}")

HEADLESS = True


class CoupangCrawler:
    """Playwright 기반 쿠팡 검색 Top10 수집기."""

    def __init__(self) -> None:
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._last_success_cache: Dict[str, Dict[str, Any]] = {}
        self._stats = {"cache_hit": 0, "requests_ok": 0, "playwright_ok": 0, "failed": 0, "blocked": 0}
        self._last_fetch_source = "unknown"
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
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
                ".coupang_chrome_profile",
            )
        os.makedirs(self._chrome_user_data_dir, exist_ok=True)
        if not self._chrome_profile:
            self._chrome_profile = "Default"

    def _cache_key(self, keyword: str) -> str:
        return f"{keyword.strip()}_{datetime.now().strftime('%Y%m%d')}"

    def _log_playwright_preflight(self) -> None:
        path = str(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")).strip()
        if not path:
            safe_print("[PLAYWRIGHT_CHECK] PLAYWRIGHT_BROWSERS_PATH is not set.")
            return
        base = Path(path)
        bins = list(base.glob("chromium-*/chrome-linux64/chrome")) if base.exists() else []
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
            price_node = li.select_one(
                ".PriceArea_priceArea__NntJz [class*='fw-text-'], "
                ".PriceArea_priceArea__NntJz .price-value, "
                ".sale-price strong, .sale-price em"
            )
            review_count_node = li.select_one(
                ".ProductRating_productRating__jjf7W [class*='fw-text-'], "
                ".rating-total-count, .rating-count, .count"
            )
            review_score_node = li.select_one(
                ".ProductRating_productRating__jjf7W [aria-label], "
                ".star .rating"
            )
            shipping_fee_node = li.select_one(
                ".TextBadge_feePrice__n_gta, "
                "[class*='fw-bg-'], .fw-bg-bluegray-100, "
                "[data-badge-type='feePrice']"
            )
            link_node = li.select_one("a[href]")

            title = title_node.get_text(strip=True) if title_node else ""
            price_raw = price_node.get_text(strip=True) if price_node else ""
            review_count_raw = review_count_node.get_text(strip=True) if review_count_node else ""
            review_score_raw = ""
            if review_score_node is not None:
                review_score_raw = str(
                    review_score_node.get("aria-label", "") or review_score_node.get_text(strip=True) or ""
                )
            shipping_fee_raw = shipping_fee_node.get_text(strip=True) if shipping_fee_node else ""
            price_num = self._parse_int(price_raw)
            review_num = self._parse_int(review_count_raw)
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

    def _get_page(self, force_headless: Optional[bool] = None) -> Optional[Page]:
        if self._page is not None:
            return self._page
        try:
            self._log_playwright_preflight()
            use_headless = self._headless if force_headless is None else bool(force_headless)
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=self._chrome_user_data_dir,
                headless=True,
                channel="chromium",
                viewport={"width": 1440, "height": 2000},
                locale="ko-KR",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                    f"--profile-directory={self._chrome_profile}",
                ],
            )
            
            page = self._context.new_page()
            
            # [USER_CUSTOM_STUFF]
            if STEALTH_AVAILABLE:
                safe_print("[INFO] Stealth 모드 활성화: 탐지 우회 적용 중...")
                apply_stealth(page)
            else:
                # 라이브러리가 없을 경우 경고 로그만 남기고 일반 실행
                safe_print("[WARN] playwright_stealth 미설치: 기본 모드로 실행합니다. (차단 위험 높음)")

            # 공통 스크립트 주입 (라이브러리 없이도 가능한 우회)
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            """)
            
            page.set_default_timeout(15000)
            self._page = page
            return self._page
        except Error as e:
            safe_print(f"[Crawler Error] keyword=PLAYWRIGHT_INIT, error={e}")
            return None

    def open_home_ready_session(self, wait_seconds: int = 120) -> bool:
        if self._page is not None:
            self.close()
        page = self._get_page(force_headless=False)
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
                return None
            if self._is_blocked(res.text, ""):
                safe_print("[WAF_BLOCK][requests] blocked signal detected in requests parsing")
                self._stats["blocked"] += 1
                return None
            product_count, items = self._parse_top10_from_html(res.text)
            safe_print(f"[Crawler][requests] parsed product_count={product_count} top10_items={len(items)}")
            if product_count <= 0:
                return None
            self._last_fetch_source = "requests"
            return self._build_result(product_count, items)
        except Exception as e:
            safe_print(f"[Crawler Error] keyword=REQUESTS_SESSION, error={e!r}")
            return None

    def bootstrap_login_session(self, wait_seconds: int = 120) -> bool:
        if self._page is not None:
            self.close()
        page = self._get_page(force_headless=False)
        if page is None:
            return False
        try:
            page.goto("https://www.coupang.com/np/coupanglogin/login", wait_until="domcontentloaded")
            safe_print("[Bootstrap] 쿠팡 로그인 페이지를 열었습니다.")
            safe_print(f"[Bootstrap] 아래 경로에 세션이 저장됩니다: {self._chrome_user_data_dir}")
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
        if self._page is not None:
            self.close()
        page = self._get_page(force_headless=False)
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
        if self._page is not None:
            self.close()
        page = self._get_page(force_headless=False)
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

        page = self._get_page(force_headless=True)
        if page is None:
            return self._result_with_reason("PLAYWRIGHT_INIT_FAILED")
        try:
            page.goto(url, wait_until="domcontentloaded")
            html = page.content()
            if self._is_blocked(html, page.title()):
                safe_print("[WAF_BLOCK] 지정된 URL 파싱 중 WAF 차단 발생.")
                self._stats["blocked"] += 1
                reason = "BLOCKED_BY_WAF" if STEALTH_AVAILABLE else "BLOCKED_BY_WAF_STEALTH_MISSING"
                return self._result_with_reason(reason)

            page.wait_for_selector("li.search-product", timeout=10000)
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
        page = self._get_page()
        if page is None:
            return None
        try:
            url = self._build_search_url(keyword)
            page.goto(url, wait_until="domcontentloaded")
            if self._is_blocked(page.content(), page.title()):
                safe_print("[WAF_BLOCK][playwright] initial load blocked by WAF/CAPTCHA")
                self._stats["blocked"] += 1
                return None
            self._simulate_human_actions(page)

            req_result = self._requests_with_browser_session(page, url)
            if req_result is not None:
                return req_result

            page.wait_for_selector("li.search-product", timeout=12000)
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
                return None
            self._last_fetch_source = "playwright"
            return self._build_result(product_count, items)
        except TimeoutError as e:
            safe_print(f"[Crawler Error] keyword={keyword}, error=timeout, detail={e}")
            return None
        except Exception as e:
            try:
                cur = page.url if page else "N/A"
                title = (page.title() if page else "N/A") or "N/A"
            except Exception:
                cur = "N/A"
                title = "N/A"
            safe_print(f"[Crawler Error] keyword={keyword}, current_url={cur}, title={title}, error={e!r}")
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
            reason = "BLOCKED_BY_WAF" if STEALTH_AVAILABLE else "BLOCKED_BY_WAF_STEALTH_MISSING"
            return self._result_with_reason(reason)
        return self._result_with_reason("CRAWL_FAILED")

    def get_stats(self) -> Dict[str, int]:
        return dict(self._stats)

    def close(self) -> None:
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
import atexit
import argparse
import os
import random
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

HEADLESS = True  # 운영 기본값: True (개발 시 환경변수로 끌 수 있음)


class CoupangCrawler:
    """
    캐시 + requests + selenium fallback 기반 쿠팡 크롤러.
    - cache key: keyword_YYYYMMDD (기본 24h TTL 효과)
    - requests 실패/파싱 실패 시 selenium fallback
    - selenium driver 재사용
    """

    def __init__(self) -> None:
        self._cache: Dict[str, Dict[str, float]] = {}
        self._last_success_cache: Dict[str, Dict[str, float]] = {}
        self._driver: Optional[webdriver.Chrome] = None
        self._stats = {"cache_hit": 0, "requests_ok": 0, "selenium_ok": 0, "failed": 0}
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
            # 전용 세션 저장 기본 경로(프로젝트 내부) - 기존 사용자 기본 프로필 충돌 방지
            self._chrome_user_data_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                ".coupang_chrome_profile",
            )
        os.makedirs(self._chrome_user_data_dir, exist_ok=True)
        if not self._chrome_profile:
            self._chrome_profile = "Default"

    def _cache_key(self, keyword: str) -> str:
        return f"{keyword.strip()}_{datetime.now().strftime('%Y%m%d')}"

    def _parse_int(self, text: str) -> Optional[int]:
        raw = re.sub(r"[^0-9]", "", str(text or ""))
        if not raw:
            return None
        try:
            return int(raw)
        except Exception:
            return None

    def _first_match_text(self, node, selectors: List[str]) -> str:
        for sel in selectors:
            one = node.select_one(sel)
            if one is not None:
                txt = one.get_text(strip=True)
                if txt:
                    return txt
        return ""

    def _extract_from_html(self, html: str) -> Tuple[int, List[float], List[float]]:
        soup = BeautifulSoup(html, "html.parser")
        product_selectors = [
            "li.search-product",
            "li[data-product-id]",
            "ul.search-product-list > li",
        ]
        products = []
        for sel in product_selectors:
            products = soup.select(sel)
            if products:
                break
        product_count = len(products)

        prices: List[float] = []
        reviews: List[float] = []
        ad_selectors = [".search-product__ad", ".badge.ad-badge-text", ".name.ad-badge-text"]
        price_selectors = [".price-value", ".sale-price", ".price > strong", ".price em"]
        review_selectors = [".rating-total-count", ".rating-count", ".rating em", ".count"]
        for li in products:
            if any(li.select_one(x) is not None for x in ad_selectors):
                continue
            price_txt = self._first_match_text(li, price_selectors)
            review_txt = self._first_match_text(li, review_selectors)
            if not price_txt or not review_txt:
                continue
            p = self._parse_int(price_txt)
            r = self._parse_int(review_txt)
            if p is None or r is None:
                continue
            prices.append(float(p))
            reviews.append(float(r))
            if len(prices) >= 10:
                break
        return product_count, reviews, prices

    def _build_result(self, product_count: int, reviews: List[float], prices: List[float]) -> Dict[str, float]:
        if len(reviews) < 3 or len(prices) < 3:
            return {"product_count": int(product_count), "avg_reviews": 0.0, "avg_price": 0.0}
        return {
            "product_count": int(product_count),
            "avg_reviews": round(sum(reviews) / len(reviews), 2),
            "avg_price": round(sum(prices) / len(prices), 2),
        }

    def _default_result(self) -> Dict[str, float]:
        return {"product_count": 0, "avg_reviews": 0.0, "avg_price": 0.0}

    def get_cached_result(self, keyword: str) -> Optional[Dict[str, float]]:
        key = str(keyword or "").strip()
        if not key:
            return None
        one = self._last_success_cache.get(key)
        return dict(one) if one is not None else None

    def _crawl_with_requests(self, keyword: str) -> Optional[Dict[str, float]]:
        url = f"https://www.coupang.com/np/search?q={requests.utils.quote(keyword)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        for _ in range(2):  # retry 1회
            try:
                res = requests.get(url, headers=headers, timeout=10)
                print(f"[Crawler][requests] keyword={keyword} status={res.status_code} body_len={len(res.text)}")
                if res.status_code != 200:
                    continue
                product_count, reviews, prices = self._extract_from_html(res.text)
                print(
                    f"[Crawler][requests] parsed keyword={keyword} "
                    f"product_count={product_count} reviews={len(reviews)} prices={len(prices)}"
                )
                if product_count <= 0:
                    continue
                result = self._build_result(product_count, reviews, prices)
                if result["avg_reviews"] > 0 and result["avg_price"] > 0:
                    return result
            except Exception as e:
                print(f"[Crawler Error] keyword={keyword}, error={e}")
                continue
        return None

    def _get_driver(self, force_headless: Optional[bool] = None) -> Optional[webdriver.Chrome]:
        if self._driver is not None:
            return self._driver
        try:
            options = Options()
            use_headless = self._headless if force_headless is None else bool(force_headless)
            # 개발 시 False / 운영 시 True: COUPANG_HEADLESS 환경변수로 제어
            if use_headless:
                options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)
            options.add_argument(
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            if self._chrome_user_data_dir:
                options.add_argument(f"--user-data-dir={self._chrome_user_data_dir}")
            if self._chrome_profile:
                options.add_argument(f"--profile-directory={self._chrome_profile}")
            self._driver = webdriver.Chrome(options=options)
            self._driver.implicitly_wait(3)
            self._driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            return self._driver
        except WebDriverException as e:
            print(f"[Crawler Error] keyword=DRIVER_INIT, error={e}")
            return None

    def bootstrap_login_session(self, wait_seconds: int = 120) -> bool:
        """
        로그인 세션 저장용 1회 실행:
        - 전용 프로필(non-headless)로 쿠팡 로그인 페이지를 연다.
        - 사용자가 수동 로그인할 시간을 wait_seconds 만큼 제공한다.
        """
        if self._driver is not None:
            self.close()
        driver = self._get_driver(force_headless=False)
        if driver is None:
            return False
        try:
            driver.get("https://www.coupang.com/np/coupanglogin/login")
            print("[Bootstrap] 쿠팡 로그인 페이지를 열었습니다.")
            print(f"[Bootstrap] 아래 경로에 세션이 저장됩니다: {self._chrome_user_data_dir}")
            print(f"[Bootstrap] {wait_seconds}초 내 수동 로그인 후 창을 그대로 두세요.")
            time.sleep(max(10, int(wait_seconds)))
            print("[Bootstrap] 세션 저장 절차를 종료합니다.")
            return True
        except Exception as e:
            print(f"[Crawler Error] keyword=BOOTSTRAP_LOGIN, error={e!r}")
            return False
        finally:
            self.close()

    def _crawl_with_selenium(self, keyword: str) -> Optional[Dict[str, float]]:
        driver = self._get_driver()
        if driver is None:
            return None
        try:
            url = f"https://www.coupang.com/np/search?q={requests.utils.quote(keyword)}"
            driver.get(url)
            print(f"[Crawler][selenium] loaded keyword={keyword} current_url={driver.current_url}")
            wait = WebDriverWait(driver, 8)
            wait.until(
                EC.any_of(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "li.search-product")),
                    EC.presence_of_element_located((By.CSS_SELECTOR, "li[data-product-id]")),
                    EC.presence_of_element_located((By.CSS_SELECTOR, "ul.search-product-list > li")),
                )
            )
            # Selenium fallback 경로에서만 짧은 랜덤 대기
            time.sleep(random.uniform(1.0, 2.0))
            page_title = ""
            try:
                page_title = str(driver.title or "")
            except Exception:
                page_title = ""
            print(
                f"[Crawler][selenium] ready keyword={keyword} "
                f"title={page_title[:80]} page_len={len(driver.page_source)}"
            )
            product_count, reviews, prices = self._extract_from_html(driver.page_source)
            print(
                f"[Crawler][selenium] parsed keyword={keyword} "
                f"product_count={product_count} reviews={len(reviews)} prices={len(prices)}"
            )
            if product_count <= 0:
                return None
            if len(reviews) < 3 or len(prices) < 3:
                return self.get_cached_result(keyword) or self._default_result()
            return self._build_result(product_count, reviews, prices)
        except Exception as e:
            try:
                cur = driver.current_url if driver else "N/A"
                title = (driver.title if driver else "N/A") or "N/A"
            except Exception:
                cur = "N/A"
                title = "N/A"
            print(f"[Crawler Error] keyword={keyword}, current_url={cur}, title={title}, error={e!r}")
            return None

    def crawl_coupang(self, keyword: str) -> Dict[str, float]:
        kw = str(keyword or "").strip()
        if not kw:
            return self._default_result()

        ck = self._cache_key(kw)
        if ck in self._cache:
            self._stats["cache_hit"] += 1
            return dict(self._cache[ck])

        # 1차: requests (retry 1회)
        req_result = self._crawl_with_requests(kw)
        if req_result is not None:
            self._cache[ck] = req_result
            self._last_success_cache[kw] = req_result
            self._stats["requests_ok"] += 1
            return dict(req_result)

        # 2차: selenium fallback
        sel_result = self._crawl_with_selenium(kw)
        if sel_result is not None:
            self._cache[ck] = sel_result
            self._last_success_cache[kw] = sel_result
            self._stats["selenium_ok"] += 1
            return dict(sel_result)

        self._stats["failed"] += 1
        return self.get_cached_result(kw) or self._default_result()

    def get_stats(self) -> Dict[str, int]:
        return dict(self._stats)

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None


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
    args = parser.parse_args()

    crawler = get_shared_crawler()
    if args.bootstrap_login:
        ok = crawler.bootstrap_login_session(wait_seconds=args.wait_seconds)
        print({"bootstrap_login": bool(ok), "profile_dir": crawler._chrome_user_data_dir, "profile": crawler._chrome_profile})
    else:
        print(crawler.crawl_coupang(args.keyword))
        print(crawler.get_stats())

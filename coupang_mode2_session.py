"""
2번 모드: 단일 Playwright 세션에서 구글→쿠팡 부트스트랩(1번 스모크와 동일 순서) 후,
쿠팡 검색창만 바꿔 키워드별 Top10 probe·DB 저장.

- `coupang_crawler._run_smoke_worker` 본문은 수정하지 않는다.
- DOM probe 스크립트는 `coupang_mode2_probe_eval.js`에 두고 스모크 worker와 동일 소스에서 추출 유지.
"""

from __future__ import annotations

import math
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from playwright.sync_api import Page, sync_playwright

from db import insert_mode2_autocollect_keyword_usage, query_latest_coupang_run_meta

from coupang_crawler import (
    CoupangCrawler,
    _PRODUCT_LIST_SELECTOR,
    _persist_smoke_extract_report_to_db,
    apply_stealth,
    safe_print,
)

_ROOT = Path(__file__).resolve().parent
_PROBE_JS_PATH = _ROOT / "coupang_mode2_probe_eval.js"
_PROBE_JS_CACHE: Optional[str] = None


def _load_probe_js() -> str:
    global _PROBE_JS_CACHE
    if _PROBE_JS_CACHE is not None:
        return _PROBE_JS_CACHE
    if not _PROBE_JS_PATH.is_file():
        raise FileNotFoundError(f"Missing probe script: {_PROBE_JS_PATH}")
    _PROBE_JS_CACHE = _PROBE_JS_PATH.read_text(encoding="utf-8").strip()
    return _PROBE_JS_CACHE


def _ensure_windows_proactor_policy() -> None:
    if sys.platform != "win32":
        return
    try:
        import asyncio

        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        return


def _resolve_smoke_headless(cc: CoupangCrawler) -> Tuple[bool, str]:
    """스모크 worker와 동일한 headless 판정."""
    hint_h_env = "headless=True (COUPANG_SMOKE_HEADLESS)"
    hint_h_auto = "headless=True — DISPLAY 없음"
    hint_v = "headless=False — 로컬 창"
    _sh = str(os.environ.get("COUPANG_SMOKE_HEADLESS", "")).strip().lower()
    if _sh in {"1", "true", "y", "yes"}:
        return True, hint_h_env
    if _sh in {"0", "false", "n", "no"}:
        if sys.platform != "win32" and not (os.environ.get("DISPLAY") or "").strip():
            return True, hint_h_auto
        return False, hint_v
    if cc._prep_force_headless():
        return True, hint_h_auto
    return False, hint_v


def _mouse_demo_enabled() -> bool:
    _raw_mouse = (
        os.environ.get("COUPANG_SMOKE_MOUSE_DEMO")
        or os.environ.get("coupang_smoke_mouse_demo")
        or "1"
    )
    return str(_raw_mouse).strip().lower() not in {"0", "false", "no", "off", "n"}


def _google_search_and_open_coupang(cc: CoupangCrawler, page: Page, google_query: str) -> Page:
    """구글 검색 후 SERP에서 쿠팡 링크 클릭(스모크 worker 동일). 활성 Page 반환."""
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
        safe_print("[MODE2] google wait_for_url timeout — continue")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1200)

    if not _mouse_demo_enabled():
        pass
    else:
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
            page.mouse.move(cx + radius * math.cos(ang), cy + radius * math.sin(ang))
            page.wait_for_timeout(12)
        page.wait_for_timeout(200)

    coupang_locators = [
        page.locator("a").filter(has_text=re.compile(r"https://www\.coupang\.com", re.I)).first,
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
                page.wait_for_url(re.compile(r"coupang\.com"), timeout=25000)
                page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(600)
            clicked = True
            return page
        except Exception as pe:
            last_pick_err = pe
            continue
    if not clicked:
        raise RuntimeError(f"쿠팡 링크 클릭 실패: {last_pick_err!r}")
    return page


def _coupang_mouse_circle_if_demo(page: Page) -> None:
    if not _mouse_demo_enabled():
        return
    page.wait_for_timeout(300)
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
        page.mouse.move(cx2 + radius2 * math.cos(ang2), cy2 + radius2 * math.sin(ang2))
        page.wait_for_timeout(10)
    page.wait_for_timeout(220)


def _find_coupang_search_box(page: Page):
    input_locators = [
        page.locator("input[name='q']").first,
        page.get_by_placeholder("찾고 싶은 상품을 검색해보세요!").first,
        page.locator("input[placeholder*='상품']").first,
        page.locator("input[type='search']").first,
        page.locator("header input").first,
    ]
    for in_loc in input_locators:
        try:
            in_loc.wait_for(state="visible", timeout=5000)
            return in_loc
        except Exception:
            continue
    return None


def _coupang_type_keyword_enter(cc: CoupangCrawler, page: Page, search_kw: str) -> None:
    search_box = _find_coupang_search_box(page)
    if search_box is None:
        raise RuntimeError("쿠팡 검색 입력창을 찾지 못했습니다.")
    search_box.click(timeout=5000)
    page.wait_for_timeout(200)
    search_box.click(timeout=5000)
    page.wait_for_timeout(220)
    search_box.fill("")
    page.keyboard.type(search_kw, delay=95)
    page.wait_for_timeout(250)
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
        page.wait_for_selector(_PRODUCT_LIST_SELECTOR, timeout=22000, state="attached")
    except Exception:
        safe_print("[MODE2] 상품 리스트 DOM 대기 타임아웃 — probe 계속")
    page.wait_for_timeout(500)


def _probe_evaluate_with_retries(cc: CoupangCrawler, page: Page, probe_js: str) -> Dict[str, Any]:
    probe: Optional[Dict[str, Any]] = None
    last_ev: Optional[Exception] = None
    for _probe_try in range(4):
        try:
            probe = page.evaluate(probe_js)
            break
        except Exception as ev_e:
            last_ev = ev_e
            msg = str(ev_e).lower()
            if "execution context was destroyed" in msg or "navigation" in msg:
                page.wait_for_timeout(900 + _probe_try * 700)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                except Exception:
                    pass
                continue
            raise
    if probe is None:
        raise last_ev or RuntimeError("HTML probe evaluate failed")
    _t10 = list(probe.get("top10") or [])
    if len(_t10) < 10:
        safe_print(f"[MODE2] 첫 probe 상품 {len(_t10)}개 — 스크롤 후 probe 1회 재시도")
        cc._scroll_coupang_search_results_page(page, max_wheel_batches=10)
        page.wait_for_timeout(450)
        try:
            probe2 = page.evaluate(probe_js)
            if isinstance(probe2, dict):
                t2 = list(probe2.get("top10") or [])
                if len(t2) > len(_t10):
                    probe = probe2
        except Exception as re_e:
            safe_print(f"[MODE2] probe 재시도 생략: {re_e!r}")
    return probe if isinstance(probe, dict) else {}


def _persist_mode2(
    cc: CoupangCrawler,
    keyword: str,
    probe: Dict[str, Any],
    batch_token: str,
) -> int:
    """DB 저장 + UI 캐시 동기화. 반환: Top10 행 수(프로브 기준)."""
    smoke_payload: Dict[str, Any] = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "keyword": str(keyword).strip()[:255],
        "source_type": "recommend_engine_mode2",
        "batch_token": str(batch_token or "").strip()[:64],
        **probe,
    }
    cc._sync_smoke_ranked_ui_cache_from_payload(str(keyword).strip(), smoke_payload)
    _persist_smoke_extract_report_to_db(smoke_payload)
    return len(list(probe.get("top10") or []))


def run_mode2_sequential_blocking(
    cc: CoupangCrawler,
    keywords: List[str],
    *,
    batch_token: str,
    google_query: str = "쿠팡",
    retry_blocked: bool = False,
    log: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, Any]]:
    """
    단일 브라우저에서 키워드 순차 처리. 호출 스레드에서 블로킹(예: Streamlit executor).

    반환: 키워드별 요약 dict 리스트.
    """
    _ensure_windows_proactor_policy()

    def _log(msg: str) -> None:
        safe_print(msg)
        if log:
            try:
                log(msg)
            except Exception:
                pass

    kws = [str(k or "").strip() for k in keywords if str(k or "").strip()]
    if not kws:
        return []

    try:
        from db import ensure_schema

        ensure_schema()
        _log("[MODE2] ensure_schema() applied (includes sql/007 if present)")
    except Exception as es:
        _log(f"[MODE2][WARN] ensure_schema: {es!r}")

    try:
        cc.stop_smoke_playwright_chromium_window()
    except Exception:
        pass

    probe_js = _load_probe_js()
    results: List[Dict[str, Any]] = []

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
                _ssp = (_ROOT / _ssp).resolve()
            smoke_storage_state_path = str(_ssp)
            if _ssp.is_file():
                context_kwargs["storage_state"] = smoke_storage_state_path
        except Exception as ss_e:
            safe_print(f"[MODE2] storage_state 경로 처리 실패: {ss_e!r}")
            smoke_storage_state_path = ""

    cc._sanitize_playwright_browser_env()
    cc._log_playwright_preflight()
    use_headless, _hint = _resolve_smoke_headless(cc)
    _channel = str(os.environ.get("COUPANG_PLAYWRIGHT_CHANNEL", "")).strip() or None

    pw = None
    browser = None
    page: Optional[Page] = None
    try:
        pw = sync_playwright().start()
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
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.set_default_timeout(30000)
        apply_stealth(page)

        target = "https://www.google.com/"
        _log(f"[MODE2] goto {target}")
        page.goto(target, wait_until="domcontentloaded")
        try:
            page.evaluate(
                """async () => {
                    const link = document.createElement('link');
                    link.rel = 'stylesheet';
                    link.href = 'https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;700&display=swap';
                    document.head.appendChild(link);
                    await new Promise((r) => setTimeout(r, 500));
                    if (document.fonts && document.fonts.ready) await document.fonts.ready;
                }"""
            )
            page.add_style_tag(
                content="html, body, input, textarea, button { font-family: 'Noto Sans KR', sans-serif !important; }"
            )
            page.wait_for_timeout(500)
        except Exception:
            pass

        cc._accept_google_consent_if_present(page)
        _log(f"[MODE2] google search → coupang link (query={google_query!r})")
        page = _google_search_and_open_coupang(cc, page, google_query)

        page.wait_for_timeout(400)
        _coupang_mouse_circle_if_demo(page)

        for idx, kw in enumerate(kws):
            if idx > 0:
                delay = random.uniform(7.0, 10.0)
                _log(f"[MODE2] sleep {delay:.2f}s before next keyword")
                time.sleep(delay)

            _log(f"[MODE2] keyword {idx + 1}/{len(kws)}: {kw!r}")
            _coupang_type_keyword_enter(cc, page, kw)

            probe = _probe_evaluate_with_retries(cc, page, probe_js)
            n_items = 0
            err_short = ""
            try:
                n_items = _persist_mode2(cc, kw, probe, batch_token)
            except Exception as pe:
                err_short = str(pe)[:240]
                _log(f"[MODE2][WARN] persist failed: {pe!r}")

            reason = ""
            le = cc.get_last_error() or {}
            if isinstance(le, dict):
                reason = str(le.get("code") or "")

            retried = False
            if retry_blocked and ("BLOCKED" in reason.upper()) and n_items <= 0:
                _log("[MODE2] BLOCKED — sleep 180s retry once")
                time.sleep(180.0)
                retried = True
                try:
                    _coupang_type_keyword_enter(cc, page, kw)
                    probe = _probe_evaluate_with_retries(cc, page, probe_js)
                    n_items = _persist_mode2(cc, kw, probe, batch_token)
                except Exception as e2:
                    err_short = str(e2)[:240]
                le = cc.get_last_error() or {}
                reason = str((le.get("code") if isinstance(le, dict) else "") or reason)

            meta = query_latest_coupang_run_meta(kw)
            ic = int((meta or {}).get("item_count") or 0) if isinstance(meta, dict) else 0
            top_n = len(list(probe.get("top10") or []))
            success = top_n > 0 or ic > 0 or n_items > 0
            insert_mode2_autocollect_keyword_usage(
                batch_token,
                kw,
                success=bool(success),
                item_count=max(ic, top_n, int(n_items)),
                reason_short=(reason or err_short) or None,
            )

            results.append(
                {
                    "idx": idx + 1,
                    "keyword": kw,
                    "items_saved": int(n_items),
                    "item_count_db": ic,
                    "reason_code": reason,
                    "retried_once": retried,
                    "run_id": (meta or {}).get("run_id") if isinstance(meta, dict) else None,
                    "persist_error": err_short or None,
                }
            )

        _log("[MODE2] final hold 5s before browser close")
        time.sleep(5.0)
        if smoke_storage_state_path:
            try:
                Path(smoke_storage_state_path).parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=smoke_storage_state_path)
            except Exception as ss_w:
                safe_print(f"[MODE2] storage_state save failed: {ss_w!r}")
    except Exception as e:
        _log(f"[MODE2][ERROR] session failed: {e!r}")
        results.append(
            {
                "idx": 0,
                "keyword": "",
                "items_saved": 0,
                "item_count_db": 0,
                "reason_code": "MODE2_SESSION",
                "retried_once": False,
                "run_id": None,
                "persist_error": str(e)[:500],
            }
        )
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

    return results

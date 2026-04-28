# 2026-04-28 Session Handoff

## What was completed
- Streamlit dashboard reorganized into three tabs:
  - 실행로그
  - 시장성 점수조회
  - 쿠팡 상품 키워드 분석
- Tab UI improved for operations:
  - cleaner tab styling
  - emoji labels
  - market/coupang table filtering and sorting enhancements (where applicable)
- Coupang tab changed from placeholder-only mode to live crawler mode:
  - keyword input + Top10 조회 button
  - real `crawl_coupang()` execution
  - top10 table render (`순위`, `상품명`, `가격(원)`, `리뷰수`, `평점`, `배송비`, `상품 URL`)
  - fallback template + `reason_code` warning when empty
- Playwright deployment stabilization:
  - `playwright` added to `requirements.txt`
  - Playwright browser install added in `nixpacks.toml` (`python -m playwright install --with-deps chromium`)
- Crawler stability updates:
  - CP949-safe logging (`safe_print`)
  - offline local HTML parse mode (`--parse-local-html`)
  - selector safety fixes for bracket-containing class names
  - optional stealth import flow hardened to avoid startup crash when package API differs
- Removed legacy default local profile dependency from active crawler path
  - no required `.coupang_chrome_profile` path in the runtime logic

## Current known status
- Railway deploys were repeatedly executed and latest deploy logs were confirmed during the session.
- Untracked local directory remains:
  - `.coupang_chrome_profile/`
  - This directory is local-only and not pushed unless explicitly tracked.

## Risks / notes
- Coupang WAF blocking may still occur depending on session/IP/runtime context.
- Crawler parse logic is validated separately (offline/HTML), but live access can still fail externally.

## Next recommended steps
1. Add `.coupang_chrome_profile/` to `.gitignore` to prevent accidental tracking.
2. Add a small “crawler status badge” in the Coupang tab:
   - success count / blocked count / last `reason_code`
3. If needed, save Top10 results from UI download button for operator workflow.

## Quick resume checklist for next chat
- Open `app_web.py` and test the Coupang tab end-to-end with one keyword.
- If empty result appears, inspect `reason_code` first (`BLOCKED_BY_WAF`, `CRAWL_FAILED`, etc.).
- Verify Railway runtime logs for import/runtime mismatch before changing crawler logic.

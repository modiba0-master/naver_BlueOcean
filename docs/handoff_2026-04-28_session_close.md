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

---

## 2026-04-30 update (latest)

### Completed today
- Dependency reproducibility and build fix:
  - `requirements.txt` pinned (notably `pandas==2.3.3`) for Python 3.10 Docker compatibility.
- Runtime/path consistency:
  - `Dockerfile` + Railway variable path aligned to `/app/ms-playwright`.
  - Railway vars updated: `PLAYWRIGHT_BROWSERS_PATH=/app/ms-playwright`, `COUPANG_HEADLESS=true`.
- DB/runtime hardening:
  - Added support for `MYSQL_PUBLIC_URL` / `DATABASE_PUBLIC_URL`.
  - Guardrail for `.railway.internal` host usage outside Railway.
- Crawler stability hardening:
  - Split profiles for crawl vs prep (`.coupang_chrome_profile_crawl`, `.coupang_chrome_profile_prep`).
  - Added Playwright env sanitization for local Windows.
  - Added serialized crawler browser operations (`RLock`) to reduce greenlet thread collisions.
  - Added auto-fallback temp profile retry when ProcessSingleton lock occurs.
- Deploy/verify:
  - Multiple deploys executed and runtime validated.
  - Railway in-container DB harness passed (`harness_db.py` success end-to-end).

### Current status (as of 2026-04-30)
- Web app startup: 정상 (Railway/Local both reachable when process is running).
- DB on Railway: 정상 (schema/create/insert/query checks pass in service container).
- Coupang crawl: still externally blocked in many runs with `BLOCKED_BY_WAF` / Access Denied.
- ProcessSingleton / greenlet issue:
  - Previously reproducible on local with old profile path.
  - After hardening, isolated one-shot run no longer reproduced those exact errors.
  - WAF block remains the dominant failure mode.

### Important operational notes
- Local `Stopping...` with no URL response means Streamlit process ended; restart required.
- `127.0.0.1:8502` is local-only; if no listener, browser will show connection error.
- Credentials were exposed in terminal/chat during troubleshooting. Rotate DB/admin secrets.

### Next chat fast-start checklist
1. Verify Streamlit listener first (`8502` alive check), then open Coupang tab.
2. Run one controlled keyword test and capture:
   - `reason_code`
   - `prep_stats`
   - `last_error`
3. If WAF continues:
   - prioritize operator-facing diagnostics in UI (clear blocked badge/reason),
   - avoid treating it as crawler runtime crash.
4. Re-check local profile lock behavior only if ProcessSingleton reappears in fresh logs.

### Key commits from this session
- `1099224` fix(deps): pin pandas to 2.3.3 for Python 3.10 Docker builds
- `fc2a30a` fix(runtime): separate prep profile and harden local/railway env handling
- `0b238da` fix(crawler): serialize playwright access and auto-fallback profile on lock

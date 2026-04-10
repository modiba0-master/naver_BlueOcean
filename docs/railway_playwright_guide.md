# Railway Playwright 실행 가이드

마지막 업데이트: 2026-04-07  
작성자: Modiba

## 개요

쿠팡 키워드 검색 크롤링은 `playwright`의 Chromium 브라우저를 사용합니다.  
Railway 배포 시 Python 패키지 설치 외에 브라우저 바이너리 설치가 필요합니다.

## 필수 설치

```bash
pip install -r requirements.txt
playwright install --with-deps chromium
```

## Railway 배포 시 체크리스트

- `requirements.txt`에 `playwright`가 포함되어 있는지 확인
- 빌드/초기 실행 단계에서 `playwright install --with-deps chromium`가 실행되는지 확인
- 앱 시작 후 로그에 브라우저 실행 오류(`Executable doesn't exist`)가 없는지 확인

## 권장 환경 변수

- `COUPANG_PAGE_TIMEOUT_SEC`: 페이지 타임아웃(초), 기본 `25`
- `COUPANG_RETRY_COUNT`: 실패 시 재시도 횟수, 기본 `2`
- `COUPANG_RETRY_BACKOFF_SEC`: 재시도 대기(초), 기본 `1.5`
- `COUPANG_WAIT_AFTER_LOAD_SEC`: 페이지 로드 후 안정 대기(초), 기본 `1.2`
- `COUPANG_SEARCH_LIMIT`: 키워드 결과 수집 상한, 기본 `10`

## 동작 확인

1. 운영 URL 접속: `https://modibagoodprice-production.up.railway.app`
2. 통합 검색에서 키워드 입력 후 실행
3. `product_monitoring`에 `platform='COUPANG'` 행이 저장되는지 확인
4. 추출 실패 시 ntfy 토픽(`modiba_price` 또는 저장 토픽)에 `[CRAWL-ERROR]`가 오는지 확인

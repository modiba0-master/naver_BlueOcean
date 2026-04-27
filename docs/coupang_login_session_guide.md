# 쿠팡 로그인 세션 2단계 운영 가이드

목표: 쿠팡 크롤링에서 전용 프로필 세션을 사용해 차단 가능성을 낮추고, 운영 시 headless 모드로 재사용합니다.

## 1) 로그인 세션 저장용 1회 실행

전용 프로필 경로(`.coupang_chrome_profile`)에 로그인 세션을 저장합니다.

```powershell
$env:COUPANG_HEADLESS="false"
python .\coupang_crawler.py --bootstrap-login --wait-seconds 180
```

- 브라우저가 열리면 쿠팡 로그인 완료
- 로그인 후 창을 닫지 말고 대기
- `wait-seconds` 시간이 지나면 세션 저장 종료

## 2) 운영 headless 실행

저장된 세션을 재사용해 headless로 크롤링합니다.

```powershell
$env:COUPANG_HEADLESS="true"
python .\coupang_crawler.py --keyword "돼지족발"
```

## 선택 환경변수

- `COUPANG_CHROME_USER_DATA_DIR`  
  전용 프로필 저장 경로 지정 (기본: 프로젝트 루트의 `.coupang_chrome_profile`)
- `COUPANG_CHROME_PROFILE`  
  크롬 프로필명 (기본: `Default`)
- `COUPANG_HEADLESS`  
  `true`/`false`

## 권장 운영 순서

1. bootstrap-login 1회 수행
2. headless=true로 키워드 테스트
3. `selenium_ok`, `failed`, `success_rate` 로그 확인
4. 실패율이 높으면 bootstrap-login 재실행(세션 갱신)

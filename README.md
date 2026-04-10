# naver_BlueOcean

## 운영 점검 Quick Start
- `railway whoami` 로 로그인 세션이 유지되는지 먼저 확인
- `railway status` 로 `modiba-blueocean / production / blueocean-app` 연결 확인
- `railway variables` 에 `MARIADB_HOST/PORT/USER/PASSWORD/DATABASE` 존재 확인
- DB 송수신 점검은 `railway ssh -s blueocean-app python harness_db.py` 실행
- 앱 로그 점검은 `railway logs -n 80` 로 에러/경고를 확인


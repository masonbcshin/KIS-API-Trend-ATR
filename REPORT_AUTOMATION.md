# KIS Daily Report Automation

## 1) 목적
- 장 종료 후(기본 16:05 KST) 당일 거래 요약 리포트를 자동 생성합니다.
- 생성된 리포트를 텔레그램으로 전송합니다.
- 리포트는 실행 중인 트레이딩 프로세스와 완전히 분리된 별도 실행 파일(`tools/daily_report.py`)로 동작합니다.
- 따라서 트레이딩 프로그램이 꺼져 있어도 MySQL 데이터만으로 리포트를 생성할 수 있습니다.

## 2) 전제 조건
- Python 실행 가능 환경 (`.venv` 권장)
- MySQL 접속 정보 환경변수
  - `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
  - `DB_ENABLED=true`
- 텔레그램 환경변수
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_CHAT_ID`
  - (선택) `TELEGRAM_ENABLED=true`
- (권장) `TRADING_MODE=PAPER` 또는 `TRADING_MODE=REAL`

## 3) 수동 실행
```bash
# 오늘 리포트 생성 + 텔레그램 전송
python tools/daily_report.py

# 어제 리포트 생성 + 텔레그램 전송
python tools/daily_report.py --yesterday

# 특정 날짜 리포트 생성 + 텔레그램 전송
python tools/daily_report.py --date 2026-02-15

# 전송 없이 콘솔 출력
python tools/daily_report.py --date 2026-02-15 --dry-run

# 텔레그램 연결만 테스트
python tools/daily_report.py --test-telegram
```

## 4) cron 설정 (16:05 KST)
- 서버 타임존이 `Asia/Seoul`인지 먼저 확인/설정하세요.
```bash
timedatectl status
sudo timedatectl set-timezone Asia/Seoul
```

- crontab 예시:
```cron
5 16 * * 1-5  cd /path/to/repo && /path/to/venv/bin/python tools/daily_report.py >> logs/report.log 2>&1
```

### 참고
- 위 cron은 월~금 16:05에 실행됩니다.
- 텔레그램 전송 실패 시 프로세스는 정상 종료되며, 다음 cron 주기에 재시도됩니다.

## 5) systemd timer (선택)
- 샘플 파일:
  - `deploy/systemd/kis-daily-report.service`
  - `deploy/systemd/kis-daily-report.timer`

- 설치 예시:
```bash
sudo cp deploy/systemd/kis-daily-report.service /etc/systemd/system/
sudo cp deploy/systemd/kis-daily-report.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kis-daily-report.timer
sudo systemctl list-timers | grep kis-daily-report
```

## 6) 서버 재부팅/.env 주의사항
- cron/systemd는 로그인 셸과 환경이 다릅니다.
- `.env`가 반드시 로드되도록 경로를 고정하세요.
- `WorkingDirectory` 또는 `cd /path/to/repo` 누락 시 상대경로 import 실패가 발생할 수 있습니다.
- 가상환경 Python 경로(`/path/to/venv/bin/python`)를 명시하세요.

## 7) dry-run 검증 절차
1. `python tools/daily_report.py --yesterday --dry-run` 실행
2. 날짜/실현손익/거래횟수/Top3/스냅샷/N/A 표기가 기대값과 일치하는지 확인
3. `python tools/daily_report.py --test-telegram`으로 텔레그램 연결 확인
4. 실제 전송 실행 후 채팅방 메시지 확인
5. 마지막으로 cron/systemd 스케줄에서 로그 파일(`logs/report.log`) 증가를 확인

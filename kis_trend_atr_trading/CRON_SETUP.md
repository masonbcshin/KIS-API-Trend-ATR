# Cron 설정 가이드 - 일일 리포트 자동 전송

## 개요

이 문서는 `report_sender.py`를 cron으로 스케줄링하여 매일 자동으로 텔레그램 리포트를 전송하는 방법을 설명합니다.

---

## 사전 준비

### 1. 텔레그램 봇 설정

```bash
# 1. @BotFather에서 봇 생성
#    텔레그램 → @BotFather 검색 → /newbot 명령

# 2. 봇 토큰 확인 (예: 1234567890:ABCdefGHIjklMNOpqrsTUVwxyz)

# 3. Chat ID 확인
#    봇과 대화 시작 후 아래 URL 접속:
#    https://api.telegram.org/bot<토큰>/getUpdates
#    응답에서 "chat":{"id":XXXXXXXX} 확인
```

### 2. 환경변수 설정

```bash
# .env 파일 생성
cp .env.example .env

# 편집기로 열어서 실제 값 입력
nano .env
```

```env
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_CHAT_ID=123456789
TRADE_DATA_PATH=data/trades.csv
```

### 3. 연결 테스트

```bash
cd /path/to/kis_trend_atr_trading
python3 report_sender.py --test
```

---

## Cron 등록

### 1. crontab 편집

```bash
crontab -e
```

### 2. 스케줄 추가

```cron
# ═══════════════════════════════════════════════════════════════════
# KIS 자동매매 일일 리포트 스케줄
# ═══════════════════════════════════════════════════════════════════

# 매일 18:00 장 마감 후 당일 리포트 전송 (월~금)
0 18 * * 1-5 cd /home/ubuntu/kis_trend_atr_trading && /usr/bin/python3 report_sender.py >> logs/report.log 2>&1

# 또는 매일 09:00 전일 리포트 전송 (월~금)
# 0 9 * * 1-5 cd /home/ubuntu/kis_trend_atr_trading && /usr/bin/python3 report_sender.py --date yesterday >> logs/report.log 2>&1

# 상세 리포트 전송
# 0 18 * * 1-5 cd /home/ubuntu/kis_trend_atr_trading && /usr/bin/python3 report_sender.py --detailed >> logs/report.log 2>&1
```

### 3. cron 시간 형식

```
분 시 일 월 요일 명령
│  │ │  │  │
│  │ │  │  └── 요일 (0-7, 0과 7은 일요일)
│  │ │  └───── 월 (1-12)
│  │ └──────── 일 (1-31)
│  └─────────── 시 (0-23)
└────────────── 분 (0-59)
```

---

## 자주 사용하는 스케줄

| 스케줄 | cron 표현식 | 설명 |
|--------|-------------|------|
| 매일 18:00 (평일) | `0 18 * * 1-5` | 장 마감 후 |
| 매일 09:00 (평일) | `0 9 * * 1-5` | 장 시작 전 |
| 매일 22:00 | `0 22 * * *` | 매일 저녁 |
| 매주 금요일 18:00 | `0 18 * * 5` | 주간 정리용 |
| 매월 1일 09:00 | `0 9 1 * *` | 월간 정리용 |

---

## 환경변수 로드 방법

cron은 사용자 쉘 환경을 로드하지 않으므로, 환경변수를 명시적으로 로드해야 합니다.

### 방법 1: 스크립트 내부에서 .env 로드 (권장)

`report_sender.py`는 `python-dotenv`를 사용하여 자동으로 `.env` 파일을 로드합니다.

### 방법 2: cron에서 환경변수 설정

```cron
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
TRADE_DATA_PATH=/home/ubuntu/kis_trend_atr_trading/data/trades.csv

0 18 * * 1-5 cd /home/ubuntu/kis_trend_atr_trading && /usr/bin/python3 report_sender.py
```

### 방법 3: 래퍼 스크립트 사용

```bash
#!/bin/bash
# run_report.sh

# 프로젝트 디렉토리로 이동
cd /home/ubuntu/kis_trend_atr_trading

# 환경변수 로드
source .env

# Python 경로 (가상환경 사용 시)
# source venv/bin/activate

# 리포트 실행
python3 report_sender.py "$@"
```

```cron
0 18 * * 1-5 /home/ubuntu/kis_trend_atr_trading/run_report.sh >> logs/report.log 2>&1
```

---

## 로그 확인

```bash
# 최근 로그 확인
tail -f logs/report.log

# 오늘 로그 확인
grep "$(date +%Y-%m-%d)" logs/report.log

# cron 실행 로그 확인 (시스템)
grep CRON /var/log/syslog | tail -20
```

---

## 로그 로테이션 설정 (선택)

```bash
# /etc/logrotate.d/kis_report 파일 생성
sudo nano /etc/logrotate.d/kis_report
```

```
/home/ubuntu/kis_trend_atr_trading/logs/report.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 0644 ubuntu ubuntu
}
```

---

## 문제 해결

### cron이 실행되지 않는 경우

1. **경로 확인**
   ```bash
   which python3
   # /usr/bin/python3 형태의 절대 경로 사용
   ```

2. **권한 확인**
   ```bash
   chmod +x report_sender.py
   ```

3. **cron 서비스 확인**
   ```bash
   sudo systemctl status cron
   ```

4. **수동 테스트**
   ```bash
   cd /home/ubuntu/kis_trend_atr_trading
   /usr/bin/python3 report_sender.py
   ```

### 텔레그램 전송 실패

1. 연결 테스트 실행
   ```bash
   python3 report_sender.py --test
   ```

2. 토큰/Chat ID 확인
   ```bash
   cat .env | grep TELEGRAM
   ```

3. 네트워크 확인
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getMe"
   ```

---

## 참고

- [crontab.guru](https://crontab.guru/) - cron 표현식 검증 도구
- [Telegram Bot API](https://core.telegram.org/bots/api) - 공식 문서

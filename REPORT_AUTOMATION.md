# KIS 일일 리포트 자동화 가이드 (아주 쉽게)

이 문서는 처음 하는 사람도 그대로 따라 하면 되게 만들었습니다.

목표는 딱 1개입니다.
- 매일 한국시간(KST) 16:05에 `tools/daily_report.py`를 자동 실행해서 텔레그램으로 리포트 보내기

---

## 1. 먼저 이해하기

`tools/daily_report.py`는 트레이딩 본체와 분리된 별도 프로그램입니다.

그래서 아래가 가능합니다.
- 트레이딩 프로그램이 꺼져 있어도 동작
- MySQL 데이터만 있으면 리포트 생성 가능
- 실패해도 트레이딩 메인 로직에 영향 없음

---

## 2. 준비물 체크 (필수)

### 2-1) Python
```bash
python --version
```

### 2-2) 가상환경 Python 경로 확인
예시:
- `/path/to/repo/.venv/bin/python`

### 2-3) 환경변수(.env)
최소한 아래 값이 있어야 합니다.
- `DB_ENABLED=true`
- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

---

## 3. 수동으로 먼저 테스트 (무조건 먼저)

리포트 자동화를 하기 전에 수동 테스트를 먼저 하세요.

### 3-1) 텔레그램 연결 테스트
```bash
python tools/daily_report.py --test-telegram
```

### 3-2) 어제 리포트를 화면에만 출력(dry-run)
```bash
python tools/daily_report.py --yesterday --dry-run
```

### 3-3) 실제 전송 테스트
```bash
python tools/daily_report.py --yesterday
```

---

## 4. 한국시간 16:05에 맞추는 방법 (중요)

서버가 미국에 있어도 문제 없습니다. 방법은 2가지입니다.

## 방법 A: 서버 시간대를 KST로 변경

```bash
timedatectl status
sudo timedatectl set-timezone Asia/Seoul
timedatectl status
```

`status`에서 `Time zone: Asia/Seoul`이면 성공입니다.

이 경우 cron은 일반 형태로 쓰면 됩니다.
```cron
5 16 * * 1-5 cd /path/to/repo && /path/to/repo/.venv/bin/python tools/daily_report.py --timezone Asia/Seoul >> logs/report.log 2>&1
```

주의:
- 서버 전체 시간대가 바뀌므로, 다른 서비스 로그 시간도 KST로 바뀝니다.

## 방법 B: 서버 시간대는 그대로 두고 cron만 KST로 실행

```cron
CRON_TZ=Asia/Seoul
5 16 * * 1-5 cd /path/to/repo && /path/to/repo/.venv/bin/python tools/daily_report.py --timezone Asia/Seoul >> logs/report.log 2>&1
```

주의:
- 일부 오래된 cron은 `CRON_TZ`를 지원하지 않을 수 있습니다.
- 확인:
```bash
man 5 crontab | grep CRON_TZ
```

---

## 5. cron 실제 등록 방법 (복붙용)

### 5-1) crontab 열기
```bash
crontab -e
```

### 5-2) 아래 한 줄 추가
방법 A를 쓴다면:
```cron
5 16 * * 1-5 cd /path/to/repo && /path/to/repo/.venv/bin/python tools/daily_report.py --timezone Asia/Seoul >> logs/report.log 2>&1
```

방법 B를 쓴다면:
```cron
CRON_TZ=Asia/Seoul
5 16 * * 1-5 cd /path/to/repo && /path/to/repo/.venv/bin/python tools/daily_report.py --timezone Asia/Seoul >> logs/report.log 2>&1
```

### 5-3) 등록 확인
```bash
crontab -l
```

---

## 6. systemd timer로 하는 방법 (선택)

cron 대신 systemd를 쓰고 싶다면 아래 파일을 사용하세요.
- `deploy/systemd/kis-daily-report.service`
- `deploy/systemd/kis-daily-report.timer`

설치:
```bash
sudo cp deploy/systemd/kis-daily-report.service /etc/systemd/system/
sudo cp deploy/systemd/kis-daily-report.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kis-daily-report.timer
sudo systemctl list-timers | grep kis-daily-report
```

---

## 7. 실패했을 때 바로 보는 곳

로그 파일:
- `logs/report.log`

실행 자체 확인:
```bash
tail -n 100 logs/report.log
```

---

## 8. 자주 실패하는 원인 5개와 즉시 조치

1. `.env` 값 누락  
- 조치: `DB_*`, `TELEGRAM_*` 값 다시 입력

2. DB 접속 실패  
- 조치: `DB_HOST/PORT/USER/PASSWORD/NAME` 확인

3. 시간대 설정 실수  
- 조치: 방법 A면 `timedatectl status`, 방법 B면 `CRON_TZ=Asia/Seoul` 확인

4. cron 경로 오타  
- 조치: `cd /path/to/repo`와 `.../.venv/bin/python` 절대경로 사용

5. 텔레그램 토큰/채팅ID 오류  
- 조치: `python tools/daily_report.py --test-telegram`로 먼저 점검

---

## 9. 최종 점검 체크리스트

1. `python tools/daily_report.py --test-telegram` 성공
2. `python tools/daily_report.py --yesterday --dry-run` 출력 정상
3. `crontab -l`에 스케줄 존재
4. 다음 실행 시각 이후 `logs/report.log`에 실행 기록 생성
5. 텔레그램 채팅방에서 리포트 수신 확인

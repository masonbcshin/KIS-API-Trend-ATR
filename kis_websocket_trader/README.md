# KIS WebSocket 자동매매 시스템

> DEPRECATED (adapter-only migration)
>
> 이 폴더의 독립 실행 경로는 단계적으로 중단됩니다.
> 한국 주식 트레이딩 실행은 `kis_trend_atr_trading.apps.kr_trade`를 사용하고,
> WebSocket 기능은 `kis_trend_atr_trading/adapters/kis_ws/`로 흡수되었습니다.

한국투자증권(KIS) WebSocket API를 활용한 실시간 자동매매 시스템입니다.

## 시스템 개요

- **목적**: 장 시작 전 선정된 종목 리스트를 대상으로 실시간 시세를 감시하여 ATR 기준 진입/손절/익절 조건을 체크
- **모드**: CBT(알림만) / LIVE(실거래)
- **운영 시간**: 09:00~15:20 진입 허용, 15:30 자동 종료

## 주요 기능

### 1. 실시간 시세 수신
- KIS WebSocket을 통한 실시간 체결가 수신
- 자동 재연결 로직 (네트워크 단절 대응)

### 2. ATR 기반 전략
- **진입**: 현재가 ≥ entry_price (사전 설정된 돌파가)
- **손절**: 현재가 ≤ stop_loss
- **익절**: 현재가 ≥ take_profit

### 3. 상태 관리
- **WAIT**: 진입 대기
- **ENTERED**: 포지션 보유 중
- **EXITED**: 청산 완료 (재진입 방지)

### 4. 모드 분기
- **CBT 모드**: 주문 없이 텔레그램 알림만 전송
- **LIVE 모드**: 실제 주문 실행 (구조 설계됨)

## 파일 구조

```
kis_websocket_trader/
├── main.py                 # 메인 컨트롤러
├── websocket_client.py     # KIS WebSocket 클라이언트
├── strategy.py             # ATR 전략 로직
├── notifier.py             # 텔레그램 알림 모듈
├── config.py               # 설정 관리
├── requirements.txt        # 의존성 패키지
├── .env.example            # 환경변수 샘플
├── .gitignore              # Git 제외 파일
├── README.md               # 문서
└── data/
    ├── trade_universe.json       # 종목 리스트
    └── trade_universe_sample.json # 샘플 (참고용)
```

## 설치 및 설정

### 1. 의존성 설치

```bash
cd kis_websocket_trader
pip install -r requirements.txt
```

### 2. 환경변수 설정

```bash
cp .env.example .env
```

`.env` 파일을 열어 다음 값들을 설정합니다:

```env
# KIS API 설정
KIS_APP_KEY=your_app_key
KIS_APP_SECRET=your_app_secret
KIS_ACCOUNT_NO=12345678-01

# 거래 모드 (CBT: 알림만, LIVE: 실거래)
TRADE_MODE=CBT

# 텔레그램 설정
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### 3. 종목 리스트 설정

`data/trade_universe.json` 파일에 감시할 종목 정보를 입력합니다:

```json
[
    {
        "stock_code": "005930",
        "stock_name": "삼성전자",
        "entry_price": 71000,
        "stop_loss": 69500,
        "take_profit": 73500,
        "atr": 750,
        "quantity": 10
    }
]
```

## 실행

```bash
python main.py
```

## 운영 흐름

```
[장 시작 전]
1. trade_universe.json에 종목 리스트 준비
   - 각 종목의 entry_price, stop_loss, take_profit, atr 값 설정

[09:00 장 시작]
2. python main.py 실행
3. WebSocket 연결 및 종목 구독
4. 실시간 시세 감시 시작

[09:00 ~ 15:20 거래 시간]
5. WAIT 상태 종목: entry_price 돌파 시 진입 시그널
6. ENTERED 상태 종목: stop_loss/take_profit 도달 시 청산 시그널
7. CBT 모드: 텔레그램 알림 전송
   LIVE 모드: 실제 주문 실행

[15:20 ~ 15:30]
8. 신규 진입 금지 (기존 포지션 청산만 허용)

[15:30]
9. 시스템 자동 종료
```

## 텔레그램 알림 예시

### 진입 시그널 (CBT 모드)
```
📈 [CBT] 진입 시그널 발생
━━━━━━━━━━━━━━━━━━
• 종목코드: 005930
• 종목명: 삼성전자
• 현재가: 71,200원
• 진입가: 71,000원
• 손절가: 69,500원 (-2.11%)
• 익절가: 73,500원 (+3.52%)
━━━━━━━━━━━━━━━━━━
🔔 CBT 모드: 실주문 없음
⏰ 2026-01-28 09:15:30
```

### 손절 시그널
```
🛑 [CBT] 손절 시그널 발생
━━━━━━━━━━━━━━━━━━
• 종목코드: 005930
• 종목명: 삼성전자
• 진입가: 71,200원
• 현재가: 69,400원
• 손절가: 69,500원
• 손실률: -2.53%
━━━━━━━━━━━━━━━━━━
🔔 CBT 모드: 실주문 없음
```

### 익절 시그널
```
🎯 [CBT] 익절 시그널 발생
━━━━━━━━━━━━━━━━━━
• 종목코드: 005930
• 종목명: 삼성전자
• 진입가: 71,200원
• 현재가: 73,600원
• 익절가: 73,500원
• 수익률: +3.37%
━━━━━━━━━━━━━━━━━━
🔔 CBT 모드: 실주문 없음
```

## 주의사항

1. **모의투자 전용**: 현재 WebSocket은 모의투자 서버에 연결됩니다.
2. **실거래 주의**: LIVE 모드 사용 시 실제 금전적 손실이 발생할 수 있습니다.
3. **API 제한**: KIS API Rate Limit을 준수합니다.
4. **시간 동기화**: 시스템 시간이 정확해야 운영 시간 제어가 올바르게 동작합니다.

## 라이선스

이 프로젝트는 개인 학습 및 연구 목적으로 제작되었습니다.
실제 투자에 사용하기 전에 충분한 테스트와 검증이 필요합니다.

## 면책 조항

본 소프트웨어를 사용하여 발생하는 모든 손실에 대해 개발자는 책임지지 않습니다.
투자 결정은 본인의 판단과 책임 하에 이루어져야 합니다.

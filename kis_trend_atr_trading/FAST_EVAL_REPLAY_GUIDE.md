# Fast Eval Replay Guide

이 문서는 `paper-safe` 검증용 replay harness 사용법을 설명합니다.

목적:
- 실장중 재실행 없이 `legacy` 평가 cadence와 `fast-eval` cadence를 같은 입력으로 비교
- 실주문/실계좌/실시간 REST·WS 호출 없이 구조 변경 효과를 검증
- `entry/exit cadence`, `quote age`, `fallback/reconnect` 지표를 반복 재현 가능하게 기록

이 도구는 **검증 전용**입니다.
전략 판단 의미나 주문 로직을 바꾸지 않으며, 운영 엔트리포인트를 대체하지 않습니다.

---

## 1. 실행 위치

프로젝트 루트에서 실행합니다.

```bash
cd /home/user/KIS-API-Trend-ATR
```

권장 실행:

```bash
.venv/bin/python tools/fast_eval_replay.py --help
```

시스템 Python으로도 동작은 가능하지만, 운영 검증은 `.venv/bin/python` 기준으로 맞추는 편이 안전합니다.

---

## 2. 입력 파일 형식

입력은 `JSONL`입니다.
한 줄이 하나의 quote event입니다.

필수 필드:
- `ts` 또는 `event_at`
- `symbol`

권장 필드:
- `received_at`
- `has_position`
- `ws_connected`
- `current_price`
- `open_price`
- `best_bid`
- `best_ask`
- `stock_name`

예시:

```json
{"ts":"2026-03-10T09:00:00+09:00","received_at":"2026-03-10T09:00:00+09:00","symbol":"005930","has_position":false,"ws_connected":true,"current_price":189000}
{"ts":"2026-03-10T09:00:01+09:00","received_at":"2026-03-10T09:00:00.800000+09:00","symbol":"005930","has_position":false,"ws_connected":true,"current_price":189100}
{"ts":"2026-03-10T09:00:01+09:00","received_at":"2026-03-10T09:00:00.850000+09:00","symbol":"396500","has_position":true,"ws_connected":true,"current_price":33800}
```

의미:
- `ts`: 이벤트 시각
- `received_at`: quote를 실제로 받은 시각
- `has_position`: 해당 시점에 보유 중인지 여부
- `ws_connected`: 해당 시점에 WS가 정상 연결 상태인지 여부

---

## 3. 기본 실행 예시

2개 종목은 보유 중이라고 가정하고 replay:

```bash
.venv/bin/python tools/fast_eval_replay.py \
  --input /path/to/quote_replay.jsonl \
  --holding-symbol 000001 \
  --holding-symbol 000002 \
  --pretty
```

파일로 저장:

```bash
.venv/bin/python tools/fast_eval_replay.py \
  --input /path/to/quote_replay.jsonl \
  --holding-symbol 000001 \
  --holding-symbol 000002 \
  --output /tmp/fast_eval_report.json \
  --pretty
```

---

## 4. 주요 옵션

- `--input`
  - replay JSONL 파일 경로
- `--holding-symbol`
  - 보유 종목으로 간주할 코드
  - 여러 번 지정 가능
- `--legacy-interval-sec`
  - legacy 루프 주기 비교값
  - 기본 `30`
- `--fast-entry-cooldown-sec`
  - fast path 미보유 종목 entry cooldown
  - 기본 `12`
- `--fast-entry-debounce-sec`
  - fast path 미보유 종목 debounce
  - 기본 `2`
- `--fast-exit-cooldown-sec`
  - fast path 보유 종목 exit cooldown
  - 기본 `5`
- `--fast-exit-debounce-sec`
  - fast path 보유 종목 debounce
  - 기본 `1`
- `--fast-rest-fallback-cooldown-sec`
  - WS 단절 시 degraded fallback cooldown
  - 기본 `30`
- `--fast-loop-sleep-sec`
  - fast scheduler 내부 tick
  - 기본 `1`
- `--output`
  - JSON 결과 파일 저장 경로
- `--pretty`
  - 보기 좋게 들여쓰기해서 출력

---

## 5. 출력 구조

출력 JSON은 4개 블록으로 나옵니다.

- `input`
  - 입력 파일 정보
  - 시작/종료 시각
  - 이벤트 수
  - 대상 심볼
- `legacy`
  - 기존 completed-bar gate 기준 cadence 결과
- `fast`
  - fast scheduler 기준 cadence 결과
- `comparison`
  - 핵심 비교 수치 요약

중요 필드:

- `legacy_entry_p50_sec`
- `fast_entry_p50_sec`
- `fast_exit_p50_sec`
- `entry_p50_improvement_sec`
- `entry_speedup_ratio`

`fast.global`에는 아래가 포함됩니다.

- `p50_interval_sec`
- `p90_interval_sec`
- `quote_age_p50_sec`
- `quote_age_p90_sec`
- `daily_fetch_calls`
- `rest_quote_calls`
- `account_snapshot_calls`
- `ws_reconnect_count`
- `ws_fallback_count`
- `evaluations`

---

## 6. 해석 기준

예시:

```json
{
  "legacy_entry_p50_sec": 60.0,
  "fast_entry_p50_sec": 12.0,
  "fast_exit_p50_sec": 5.0,
  "entry_p50_improvement_sec": 48.0,
  "entry_speedup_ratio": 5.0
}
```

해석:
- legacy는 미보유 종목 평가가 대략 60초 간격
- fast path는 미보유 종목 평가가 대략 12초 간격
- 보유 종목 exit 감시는 대략 5초 간격
- 같은 입력 기준으로 entry cadence가 약 5배 빨라짐

`quote_age_p50_sec`가 낮을수록 WS quote를 더 신선하게 사용했다는 뜻입니다.

---

## 7. 권장 검증 절차

1. 장중 로그/모의 스트림에서 quote event를 JSONL로 저장
2. replay harness로 `legacy` vs `fast` 비교
3. `entry p50/p90`, `exit p50`, `quote age`, `fallback/reconnect` 확인
4. 목표 범위:
   - 미보유 종목 entry cadence p50: `10~15초`
   - 보유 종목 exit cadence p50: `5초 전후`
5. REST 의존 카운터가 hot path에서 증가하지 않는지 확인

---

## 8. 주의사항

- 이 도구는 실주문 검증 도구가 아닙니다.
- 입력 파일 품질이 결과를 좌우합니다.
- `has_position`, `ws_connected`, `received_at`를 넣지 않으면 exit cadence나 quote age 재현 정확도가 떨어집니다.
- replay 결과가 좋아도 live 환경에서는 WS 품질, API 응답, CPU 상황에 따라 차이가 날 수 있습니다.

---

## 9. 관련 파일

- tool 본체: `kis_trend_atr_trading/tools/fast_eval_replay.py`
- root wrapper: `tools/fast_eval_replay.py`
- cadence scheduler: `kis_trend_atr_trading/engine/evaluation_scheduler.py`
- 테스트:
  - `kis_trend_atr_trading/tests/test_fast_eval_scheduler_unittest.py`
  - `kis_trend_atr_trading/tests/test_fast_eval_replay_unittest.py`

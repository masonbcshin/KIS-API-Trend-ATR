# Legacy DB Compatibility Notes (2026-02-15)

## 배경

운영 DB `positions` 테이블이 현재 코드 기준 표준 스키마와 달라서, 멀티데이 시작 시 실계좌 동기화(upsert)에서 연속적으로 실패가 발생했습니다.

- 증상: `Unknown column ...` 또는 `Field '...' doesn't have a default value`
- 공통 원인: MySQL `STRICT` 모드에서 `NOT NULL` + `DEFAULT` 없음 컬럼 미지정 INSERT

## 반영된 호환성 대응

다음 커밋들에서 레거시 컬럼 호환을 순차 반영했습니다.

- `da6e056`: `symbol`/`stock_code` 호환
- `069dfb2`: 레거시 컬럼 마이그레이션 보강
- `aa8cc1c`, `4bd5c81`: `position_id` 필수 스키마 대응
- `adae499`: `state` 필수 컬럼 대응
- `a491d99`: `entry_date`, `stop_loss`, `take_profit`, `atr_value`, `atr` 호환
- `eb10984`: `created_at`, `updated_at` 호환

현재는 `positions` 메타를 런타임 탐지해, 존재하는 컬럼만 동적으로 INSERT/UPDATE 합니다.

## 텔레그램 에러 알림이 안 온 이유

이번 장애 구간에서 텔레그램 에러가 오지 않은 것은 현재 코드 동작 기준으로 정상입니다.

### 현재 동작

1. `PositionRepository.upsert_from_account_holding()`에서 DB Query 오류가 나면 예외를 상위로 던지지 않고 `None`을 반환합니다.
2. `PositionResynchronizer._sync_db_positions_from_api()`는 `saved is None`일 때 `logger.warning("[RESYNC][DB] 포지션 저장 실패/보류 ...")`만 남기고 `result["warnings"]`에 넣지 않습니다.
3. `MultiDayTradingExecutor.restore_position_on_start()`는 `result["warnings"]`에 들어온 항목만 `telegram.notify_warning(...)`로 전달합니다.

즉, **DB upsert 개별 실패는 "소프트 실패(경고 로그)"로 처리되고, 텔레그램 ERROR 알림 트리거에는 포함되지 않습니다.**

### 텔레그램 ERROR가 발송되는 대표 경로

- 전략 실행 예외 (`notify_error("전략 실행 오류", ...)`)
- 시스템 루프 예외 (`notify_error("시스템 오류", ...)`)
- 주문 실패 (`notify_error("매수/매도 주문 실패", ...)`)
- 동기화 결과가 `UNTRACKED_HOLDING`, `CRITICAL_MISMATCH` 같은 치명 상태일 때

## 운영 권장 확인 항목

- 시작 직후 `repository`에 레거시 컬럼 감지 로그가 출력되는지 확인
- `[RESYNC][DB] 실계좌 기준 반영` 로그가 지속되는지 확인
- `mysql | ERROR`가 재발하지 않는지 확인
- 텔레그램은 치명 경로 중심으로만 확인 (경고성 DB upsert 실패는 기본 미전송)


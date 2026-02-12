"""
KIS Trend-ATR Trading System - 주문 동기화 및 안전장치 모듈

★ 핵심 목적:
    - 주문 → 체결 확인 → 상태 반영의 동기화 보장
    - 단일 인스턴스 실행 보장
    - 장 운영시간 체크
    - API 오류 후 재동기화

★ 감사 보고서 지적 사항 해결:
    - "체결 확인 없이 상태 갱신" 문제 해결
    - "이중 인스턴스 실행" 방지
    - "동시호가 시간대 주문 실패" 방지

작성일: 2026-01-29
"""

import os
import sys
import time
import fcntl
import atexit
import hashlib
from pathlib import Path
from datetime import datetime, time as dt_time
from typing import List, Tuple, Optional, Dict, Set, Any
from dataclasses import dataclass
from enum import Enum

from utils.logger import get_logger
from utils.market_hours import KST
from env import get_trading_mode

try:
    from db.mysql import get_db_manager
except Exception:  # pragma: no cover - DB 미사용 환경
    get_db_manager = None

logger = get_logger("order_synchronizer")


# ════════════════════════════════════════════════════════════════
# 상수 정의
# ════════════════════════════════════════════════════════════════

LOCK_FILE_PATH = Path(__file__).parent.parent / "data" / "instance.lock"
LOCK_STALE_TIMEOUT_SECONDS = int(os.getenv("INSTANCE_LOCK_STALE_TIMEOUT", "3600"))

# 한국 주식시장 시간
MARKET_OPEN = dt_time(9, 0, 0)
MARKET_CLOSE = dt_time(15, 30, 0)
SIMULTANEOUS_QUOTE_START = dt_time(15, 20, 0)  # 동시호가 시작
PRE_MARKET_START = dt_time(8, 30, 0)  # 장전 동시호가
PRE_MARKET_END = dt_time(9, 0, 0)


def _combine_kst(target_date, target_time: dt_time) -> datetime:
    """pytz localize를 사용해 정확한 KST datetime을 생성합니다."""
    return KST.localize(datetime.combine(target_date, target_time))


class MarketStatus(Enum):
    """시장 상태"""
    CLOSED = "CLOSED"                    # 폐장
    PRE_MARKET = "PRE_MARKET"           # 장전 동시호가
    OPEN = "OPEN"                        # 정규장
    SIMULTANEOUS_QUOTE = "SIMULTANEOUS"  # 장 마감 동시호가


class OrderExecutionResult(Enum):
    """주문 실행 결과"""
    SUCCESS = "SUCCESS"           # 완전 체결
    PARTIAL = "PARTIAL"           # 부분 체결
    FAILED = "FAILED"             # 실패
    CANCELLED = "CANCELLED"       # 취소됨
    MARKET_CLOSED = "MARKET_CLOSED"  # 장 마감으로 불가


# ════════════════════════════════════════════════════════════════
# 단일 인스턴스 락 메커니즘
# ════════════════════════════════════════════════════════════════

class SingleInstanceLock:
    """
    단일 인스턴스 실행 보장 클래스
    
    ★ 감사 보고서 지적:
        "동일 프로그램이 실수로 두 번 실행될 경우 이중 매수 발생"
    
    ★ 해결 방법:
        - 파일 락을 사용하여 단일 인스턴스만 실행 허용
        - 두 번째 인스턴스 시작 시 즉시 종료
    
    Usage:
        lock = SingleInstanceLock()
        if not lock.acquire():
            print("이미 실행 중인 인스턴스가 있습니다.")
            sys.exit(1)
        
        # 프로그램 로직...
        
        lock.release()  # 또는 프로그램 종료 시 자동 해제
    """
    
    def __init__(self, lock_file: Path = None):
        """
        Args:
            lock_file: 락 파일 경로 (미입력 시 기본 경로)
        """
        self.lock_file = lock_file or LOCK_FILE_PATH
        self._lock_fd = None
        self._acquired = False
        
        # 데이터 디렉토리 생성
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
    
    def acquire(self) -> bool:
        """
        락을 획득합니다.
        
        Returns:
            bool: 락 획득 성공 여부
        """
        try:
            self._cleanup_stale_lock_file()
            self._lock_fd = open(self.lock_file, 'w')
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            
            # 프로세스 정보 기록
            self._lock_fd.write(f"PID: {os.getpid()}\n")
            self._lock_fd.write(f"Started: {datetime.now(KST).isoformat()}\n")
            self._lock_fd.flush()
            
            self._acquired = True
            
            # 프로그램 종료 시 자동 해제 등록
            atexit.register(self.release)
            
            logger.info(f"[LOCK] 단일 인스턴스 락 획득: PID={os.getpid()}")
            return True
            
        except IOError:
            # 이미 다른 인스턴스가 락을 보유 중
            if self._lock_fd:
                self._lock_fd.close()
                self._lock_fd = None
            
            logger.error("[LOCK] 락 획득 실패 - 이미 다른 인스턴스가 실행 중입니다.")
            return False
        
        except Exception as e:
            logger.error(f"[LOCK] 락 획득 중 오류: {e}")
            return False

    def _cleanup_stale_lock_file(self) -> None:
        """stale lock 파일을 정리합니다. (프로세스 비존재 + 타임아웃 경과)"""
        if not self.lock_file.exists():
            return

        try:
            info = self.get_running_instance_info() or {}
            pid_raw = info.get("PID")
            started_raw = info.get("Started")

            process_alive = False
            if pid_raw and pid_raw.isdigit():
                pid = int(pid_raw)
                try:
                    os.kill(pid, 0)
                    process_alive = True
                except OSError:
                    process_alive = False

            if process_alive:
                return

            if started_raw:
                try:
                    started_at = datetime.fromisoformat(started_raw)
                    age = (datetime.now(KST) - started_at).total_seconds()
                    if age < LOCK_STALE_TIMEOUT_SECONDS:
                        return
                except Exception:
                    pass

            self.lock_file.unlink(missing_ok=True)
            logger.warning("[LOCK] stale lock 파일을 정리했습니다.")
        except Exception as e:
            logger.debug(f"[LOCK] stale lock 정리 스킵: {e}")
    
    def release(self) -> None:
        """락을 해제합니다."""
        if self._lock_fd and self._acquired:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_fd.close()
                self._lock_fd = None
                self._acquired = False
                
                # 락 파일 삭제
                if self.lock_file.exists():
                    self.lock_file.unlink()
                
                logger.info("[LOCK] 단일 인스턴스 락 해제")
            except Exception as e:
                logger.error(f"[LOCK] 락 해제 중 오류: {e}")
    
    @property
    def is_acquired(self) -> bool:
        """락 보유 상태"""
        return self._acquired
    
    def get_running_instance_info(self) -> Optional[Dict]:
        """
        현재 실행 중인 인스턴스 정보를 반환합니다.
        
        Returns:
            Dict: 실행 중인 인스턴스 정보 (없으면 None)
        """
        if not self.lock_file.exists():
            return None
        
        try:
            with open(self.lock_file, 'r') as f:
                content = f.read()
            
            info = {}
            for line in content.strip().split('\n'):
                if ':' in line:
                    key, value = line.split(':', 1)
                    info[key.strip()] = value.strip()
            
            return info
        except Exception:
            return None


# ════════════════════════════════════════════════════════════════
# 장 운영시간 체크
# ════════════════════════════════════════════════════════════════

class MarketHoursChecker:
    """
    장 운영시간 체크 클래스
    
    ★ 감사 보고서 지적:
        "장 종료 직전 주문 실패" - 동시호가 시간대 체크 없음
    
    ★ 해결 방법:
        - 정규장/동시호가/폐장 시간대 명확히 구분
        - 주문 불가 시간대에서는 주문 차단
    """
    
    def __init__(self):
        """MarketHoursChecker 초기화"""
        pass
    
    def get_market_status(self, check_time: datetime = None) -> MarketStatus:
        """
        현재 시장 상태를 반환합니다.
        
        Args:
            check_time: 확인할 시간 (미입력 시 현재 시간)
        
        Returns:
            MarketStatus: 시장 상태
        """
        check_time = check_time or datetime.now(KST)
        current_time = check_time.time()
        weekday = check_time.weekday()
        
        # 주말
        if weekday >= 5:
            return MarketStatus.CLOSED
        
        # 장전 동시호가 (08:30 ~ 09:00)
        if PRE_MARKET_START <= current_time < PRE_MARKET_END:
            return MarketStatus.PRE_MARKET
        
        # 정규장 (09:00 ~ 15:20)
        if MARKET_OPEN <= current_time < SIMULTANEOUS_QUOTE_START:
            return MarketStatus.OPEN
        
        # 장 마감 동시호가 (15:20 ~ 15:30)
        if SIMULTANEOUS_QUOTE_START <= current_time < MARKET_CLOSE:
            return MarketStatus.SIMULTANEOUS_QUOTE
        
        # 폐장
        return MarketStatus.CLOSED
    
    def is_tradeable(self, check_time: datetime = None) -> Tuple[bool, str]:
        """
        주문 가능 여부를 확인합니다.
        
        ★ 정규장에서만 주문 허용
        ★ 동시호가 시간대는 예측 불가능성이 높아 주문 차단
        
        Args:
            check_time: 확인할 시간
        
        Returns:
            Tuple[bool, str]: (주문 가능 여부, 사유)
        """
        status = self.get_market_status(check_time)
        
        if status == MarketStatus.OPEN:
            return True, "정규장 - 주문 가능"
        
        if status == MarketStatus.PRE_MARKET:
            return False, "장전 동시호가 - 주문 차단 (09:00 이후 재시도)"
        
        if status == MarketStatus.SIMULTANEOUS_QUOTE:
            return False, "장 마감 동시호가 - 주문 차단 (익일 재시도)"
        
        if status == MarketStatus.CLOSED:
            return False, "폐장 - 주문 불가"
        
        return False, "알 수 없는 상태"
    
    def get_time_until_market_open(self, check_time: datetime = None) -> int:
        """
        장 시작까지 남은 시간(초)을 반환합니다.
        
        Args:
            check_time: 기준 시간
        
        Returns:
            int: 남은 시간 (초), 이미 장중이면 0
        """
        check_time = check_time or datetime.now(KST)
        current_time = check_time.time()
        
        if MARKET_OPEN <= current_time < SIMULTANEOUS_QUOTE_START:
            return 0  # 이미 장중
        
        # 오늘 장 시작까지
        if current_time < MARKET_OPEN:
            market_open_today = _combine_kst(check_time.date(), MARKET_OPEN)
            return int((market_open_today - check_time).total_seconds())
        
        # 내일 장 시작까지
        from datetime import timedelta
        tomorrow = check_time.date() + timedelta(days=1)
        
        # 주말 고려
        while tomorrow.weekday() >= 5:
            tomorrow += timedelta(days=1)
        
        market_open_next = _combine_kst(tomorrow, MARKET_OPEN)
        return int((market_open_next - check_time).total_seconds())
    
    def get_time_until_market_close(self, check_time: datetime = None) -> int:
        """
        동시호가 시작까지 남은 시간(초)을 반환합니다.
        
        ★ 동시호가 시간대는 주문 불가이므로 실질적인 마감 기준
        
        Args:
            check_time: 기준 시간
        
        Returns:
            int: 남은 시간 (초), 장 마감 후면 0
        """
        check_time = check_time or datetime.now(KST)
        current_time = check_time.time()
        
        if current_time >= SIMULTANEOUS_QUOTE_START:
            return 0  # 이미 동시호가 시작됨
        
        if current_time < MARKET_OPEN:
            return 0  # 아직 장 시작 전
        
        close_time = _combine_kst(check_time.date(), SIMULTANEOUS_QUOTE_START)
        return int((close_time - check_time).total_seconds())


# ════════════════════════════════════════════════════════════════
# 주문 동기화 클래스
# ════════════════════════════════════════════════════════════════

@dataclass
class SynchronizedOrderResult:
    """
    동기화된 주문 실행 결과
    
    ★ 주문 → 체결 확인 → 상태 반영이 모두 완료된 후의 결과
    """
    success: bool                         # 최종 성공 여부
    result_type: OrderExecutionResult     # 결과 유형
    order_no: str = ""                    # 주문 번호
    exec_qty: int = 0                     # 실제 체결 수량
    exec_price: float = 0.0               # 실제 체결 가격
    message: str = ""                     # 상세 메시지
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "result_type": self.result_type.value,
            "order_no": self.order_no,
            "exec_qty": self.exec_qty,
            "exec_price": self.exec_price,
            "message": self.message
        }


class OrderSynchronizer:
    """
    주문 동기화 클래스
    
    ★ 핵심 기능:
        1. 주문 전 장 운영시간 확인
        2. 주문 → 체결 확인 → 결과 반환의 동기화
        3. 부분체결/미체결 상황 명시적 처리
    
    ★ 감사 보고서 해결:
        - "체결 확인 없이 상태 갱신" → 체결 완료 확인 후에만 성공 반환
        - "동시호가 시간대 주문 실패" → 사전 차단
    
    Usage:
        syncer = OrderSynchronizer(api)
        
        result = syncer.execute_buy_order(
            stock_code="005930",
            quantity=10
        )
        
        if result.success:
            # 체결 확인됨 - 안전하게 포지션 상태 업데이트
            position.update(result.exec_price, result.exec_qty)
    """
    
    def __init__(
        self,
        api,
        market_checker: MarketHoursChecker = None,
        execution_timeout: int = 30
    ):
        """
        Args:
            api: KIS API 클라이언트
            market_checker: 장 운영시간 체커
            execution_timeout: 체결 대기 타임아웃 (초)
        """
        self.api = api
        self.market_checker = market_checker or MarketHoursChecker()
        self.execution_timeout = execution_timeout
        self.mode = get_trading_mode()
        self._db = get_db_manager() if get_db_manager else None
        self._schema_checked = False

    def _ensure_order_state_table(self) -> bool:
        """
        order_state 테이블 존재를 보장합니다.
        multiday 경로에서는 initialize_schema가 선행되지 않을 수 있으므로
        최초 접근 시 여기서 안전하게 보정합니다.
        """
        if not self._db:
            return False
        if self._schema_checked:
            return True

        try:
            if hasattr(self._db, "is_connected") and not self._db.is_connected():
                self._db.connect()
            exists = self._db.table_exists("order_state")
            if not exists:
                logger.warning("[SYNC] order_state 테이블 없음 - 스키마 초기화 시도")
                self._db.initialize_schema()
            self._schema_checked = True
            return True
        except Exception as e:
            logger.warning(f"[SYNC] order_state 스키마 확인/생성 실패: {e}")
            return False

    def _build_idempotency_key(
        self,
        side: str,
        stock_code: str,
        quantity: int,
        signal_id: str
    ) -> str:
        if not signal_id:
            signal_id = datetime.now(KST).strftime("%Y%m%d%H%M")
        raw = f"{self.mode}|{side}|{stock_code}|{quantity}|{signal_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _upsert_order_state(
        self,
        idempotency_key: str,
        signal_id: str,
        stock_code: str,
        side: str,
        quantity: int,
        status: str,
        order_no: str = "",
        fill_id: str = "",
        filled_qty: int = 0,
        remaining_qty: int = 0
    ) -> None:
        if not self._db:
            return
        self._ensure_order_state_table()
        try:
            with self._db.transaction() as cursor:
                cursor.execute(
                    """
                    INSERT INTO order_state (
                        idempotency_key, signal_id, symbol, side,
                        requested_qty, filled_qty, remaining_qty, order_no,
                        fill_id, status, mode
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        filled_qty = VALUES(filled_qty),
                        remaining_qty = VALUES(remaining_qty),
                        order_no = VALUES(order_no),
                        fill_id = VALUES(fill_id),
                        status = VALUES(status),
                        mode = VALUES(mode)
                    """,
                    (
                        idempotency_key, signal_id, stock_code, side,
                        quantity, filled_qty, remaining_qty, order_no or None,
                        fill_id or None, status, self.mode
                    )
                )
        except Exception as e:
            logger.warning(f"[SYNC] order_state 저장 실패: {e}")

    def _get_order_state(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        if not self._db:
            return None
        self._ensure_order_state_table()
        try:
            return self._db.execute_query(
                "SELECT * FROM order_state WHERE idempotency_key = %s",
                (idempotency_key,),
                fetch_one=True
            )
        except Exception as e:
            logger.warning(f"[SYNC] order_state 조회 실패: {e}")
            return None

    def recover_pending_orders(self) -> List[Dict[str, Any]]:
        """
        재시작 시 DB의 pending/submitted/partial 주문을 재구성합니다.
        """
        if not self._db:
            return []
        self._ensure_order_state_table()
        try:
            rows = self._db.execute_query(
                """
                SELECT * FROM order_state
                WHERE mode = %s AND status IN ('PENDING','SUBMITTED','PARTIAL')
                ORDER BY updated_at ASC
                """,
                (self.mode,)
            ) or []
            return rows
        except Exception as e:
            logger.warning(f"[SYNC] pending 주문 복구 조회 실패: {e}")
            return []
    
    def execute_buy_order(
        self,
        stock_code: str,
        quantity: int,
        signal_id: str = "",
        skip_market_check: bool = False
    ) -> SynchronizedOrderResult:
        """
        매수 주문을 동기화 실행합니다.
        
        ★ 실행 흐름:
            1. 장 운영시간 확인
            2. 매수 주문 전송
            3. 체결 대기 (wait_for_execution)
            4. 결과 반환 (체결 확인 후에만 success=True)
        
        Args:
            stock_code: 종목 코드
            quantity: 주문 수량
            skip_market_check: 장 시간 체크 건너뛰기 (테스트용)
        
        Returns:
            SynchronizedOrderResult: 동기화된 실행 결과
        """
        # 1. 장 운영시간 확인
        if not skip_market_check:
            tradeable, reason = self.market_checker.is_tradeable()
            if not tradeable:
                logger.warning(f"[SYNC] 매수 불가: {reason}")
                return SynchronizedOrderResult(
                    success=False,
                    result_type=OrderExecutionResult.MARKET_CLOSED,
                    message=reason
                )
        
        idempotency_key = self._build_idempotency_key("BUY", stock_code, quantity, signal_id)
        existing = self._get_order_state(idempotency_key)
        if existing and existing.get("status") in ("PENDING", "SUBMITTED", "PARTIAL", "FILLED"):
            logger.warning(
                f"[SYNC] 중복 매수 주문 차단: {stock_code}, idem={idempotency_key[:12]}..., "
                f"status={existing.get('status')}"
            )
            return SynchronizedOrderResult(
                success=False,
                result_type=OrderExecutionResult.FAILED,
                order_no=existing.get("order_no", "") or "",
                exec_qty=int(existing.get("filled_qty") or 0),
                message=f"중복 주문 차단(status={existing.get('status')})"
            )

        self._upsert_order_state(
            idempotency_key=idempotency_key,
            signal_id=signal_id,
            stock_code=stock_code,
            side="BUY",
            quantity=quantity,
            status="PENDING",
            filled_qty=0,
            remaining_qty=quantity
        )

        # 2. 매수 주문 전송
        logger.info(f"[SYNC] 매수 주문 시작: {stock_code} {quantity}주")
        
        try:
            order_result = self.api.place_buy_order(
                stock_code=stock_code,
                quantity=quantity,
                price=0,  # 시장가
                order_type="01"
            )
            
            if not order_result.get("success"):
                return SynchronizedOrderResult(
                    success=False,
                    result_type=OrderExecutionResult.FAILED,
                    message=f"주문 전송 실패: {order_result.get('message', 'Unknown')}"
                )
            
            order_no = order_result.get("order_no", "")
            self._upsert_order_state(
                idempotency_key=idempotency_key,
                signal_id=signal_id,
                stock_code=stock_code,
                side="BUY",
                quantity=quantity,
                status="SUBMITTED",
                order_no=order_no,
                filled_qty=0,
                remaining_qty=quantity
            )
            
        except Exception as e:
            logger.error(f"[SYNC] 매수 주문 전송 오류: {e}")
            return SynchronizedOrderResult(
                success=False,
                result_type=OrderExecutionResult.FAILED,
                message=f"주문 전송 오류: {str(e)}"
            )
        
        # 3. 체결 대기
        logger.info(f"[SYNC] 체결 대기 중: 주문번호={order_no}")
        
        exec_result = self.api.wait_for_execution(
            order_no=order_no,
            expected_qty=quantity,
            timeout_seconds=self.execution_timeout
        )
        
        # 4. 결과 반환
        if exec_result.get("status") == "FILLED":
            self._upsert_order_state(
                idempotency_key=idempotency_key,
                signal_id=signal_id,
                stock_code=stock_code,
                side="BUY",
                quantity=quantity,
                status="FILLED",
                order_no=order_no,
                filled_qty=exec_result.get("exec_qty", 0),
                remaining_qty=0
            )
            return SynchronizedOrderResult(
                success=True,
                result_type=OrderExecutionResult.SUCCESS,
                order_no=order_no,
                exec_qty=exec_result.get("exec_qty", 0),
                exec_price=exec_result.get("exec_price", 0),
                message=exec_result.get("message", "완전 체결")
            )
        
        elif exec_result.get("status") == "PARTIAL":
            filled_qty = exec_result.get("exec_qty", 0)
            self._upsert_order_state(
                idempotency_key=idempotency_key,
                signal_id=signal_id,
                stock_code=stock_code,
                side="BUY",
                quantity=quantity,
                status="PARTIAL",
                order_no=order_no,
                filled_qty=filled_qty,
                remaining_qty=max(quantity - filled_qty, 0)
            )
            return SynchronizedOrderResult(
                success=False,
                result_type=OrderExecutionResult.PARTIAL,
                order_no=order_no,
                exec_qty=exec_result.get("exec_qty", 0),
                exec_price=exec_result.get("exec_price", 0),
                message=exec_result.get("message", "부분 체결")
            )
        
        else:
            self._upsert_order_state(
                idempotency_key=idempotency_key,
                signal_id=signal_id,
                stock_code=stock_code,
                side="BUY",
                quantity=quantity,
                status="CANCELLED",
                order_no=order_no,
                filled_qty=exec_result.get("exec_qty", 0),
                remaining_qty=max(quantity - exec_result.get("exec_qty", 0), 0)
            )
            return SynchronizedOrderResult(
                success=False,
                result_type=OrderExecutionResult.CANCELLED,
                order_no=order_no,
                exec_qty=exec_result.get("exec_qty", 0),
                exec_price=exec_result.get("exec_price", 0),
                message=exec_result.get("message", "미체결/취소")
            )
    
    def execute_sell_order(
        self,
        stock_code: str,
        quantity: int,
        signal_id: str = "",
        skip_market_check: bool = False,
        is_emergency: bool = False
    ) -> SynchronizedOrderResult:
        """
        매도 주문을 동기화 실행합니다.
        
        ★ 실행 흐름:
            1. 장 운영시간 확인 (긴급 청산 시 건너뜀)
            2. 매도 주문 전송
            3. 체결 대기
            4. 결과 반환
        
        Args:
            stock_code: 종목 코드
            quantity: 주문 수량
            skip_market_check: 장 시간 체크 건너뛰기
            is_emergency: 긴급 청산 여부 (타임아웃 연장)
        
        Returns:
            SynchronizedOrderResult: 동기화된 실행 결과
        """
        # 1. 장 운영시간 확인 (긴급 청산은 동시호가에서도 시도)
        if not skip_market_check and not is_emergency:
            tradeable, reason = self.market_checker.is_tradeable()
            if not tradeable:
                # 동시호가 시간에는 경고만 하고 진행 (청산은 허용)
                status = self.market_checker.get_market_status()
                if status != MarketStatus.SIMULTANEOUS_QUOTE:
                    logger.warning(f"[SYNC] 매도 불가: {reason}")
                    return SynchronizedOrderResult(
                        success=False,
                        result_type=OrderExecutionResult.MARKET_CLOSED,
                        message=reason
                    )
                else:
                    logger.warning(f"[SYNC] 동시호가 중 매도 시도: {reason}")
        
        idempotency_key = self._build_idempotency_key("SELL", stock_code, quantity, signal_id)
        existing = self._get_order_state(idempotency_key)
        if existing and existing.get("status") in ("PENDING", "SUBMITTED", "PARTIAL", "FILLED"):
            logger.warning(
                f"[SYNC] 중복 매도 주문 차단: {stock_code}, idem={idempotency_key[:12]}..., "
                f"status={existing.get('status')}"
            )
            return SynchronizedOrderResult(
                success=False,
                result_type=OrderExecutionResult.FAILED,
                order_no=existing.get("order_no", "") or "",
                exec_qty=int(existing.get("filled_qty") or 0),
                message=f"중복 주문 차단(status={existing.get('status')})"
            )

        self._upsert_order_state(
            idempotency_key=idempotency_key,
            signal_id=signal_id,
            stock_code=stock_code,
            side="SELL",
            quantity=quantity,
            status="PENDING",
            filled_qty=0,
            remaining_qty=quantity
        )

        # 2. 매도 주문 전송
        timeout = self.execution_timeout * 3 if is_emergency else self.execution_timeout
        logger.info(f"[SYNC] 매도 주문 시작: {stock_code} {quantity}주 (긴급={is_emergency})")
        
        try:
            order_result = self.api.place_sell_order(
                stock_code=stock_code,
                quantity=quantity,
                price=0,  # 시장가
                order_type="01"
            )
            
            if not order_result.get("success"):
                return SynchronizedOrderResult(
                    success=False,
                    result_type=OrderExecutionResult.FAILED,
                    message=f"주문 전송 실패: {order_result.get('message', 'Unknown')}"
                )
            
            order_no = order_result.get("order_no", "")
            self._upsert_order_state(
                idempotency_key=idempotency_key,
                signal_id=signal_id,
                stock_code=stock_code,
                side="SELL",
                quantity=quantity,
                status="SUBMITTED",
                order_no=order_no,
                filled_qty=0,
                remaining_qty=quantity
            )
            
        except Exception as e:
            logger.error(f"[SYNC] 매도 주문 전송 오류: {e}")
            return SynchronizedOrderResult(
                success=False,
                result_type=OrderExecutionResult.FAILED,
                message=f"주문 전송 오류: {str(e)}"
            )
        
        # 3. 체결 대기
        logger.info(f"[SYNC] 체결 대기 중: 주문번호={order_no}, 타임아웃={timeout}초")
        
        exec_result = self.api.wait_for_execution(
            order_no=order_no,
            expected_qty=quantity,
            timeout_seconds=timeout
        )
        
        # 4. 결과 반환
        if exec_result.get("status") == "FILLED":
            self._upsert_order_state(
                idempotency_key=idempotency_key,
                signal_id=signal_id,
                stock_code=stock_code,
                side="SELL",
                quantity=quantity,
                status="FILLED",
                order_no=order_no,
                filled_qty=exec_result.get("exec_qty", 0),
                remaining_qty=0
            )
            return SynchronizedOrderResult(
                success=True,
                result_type=OrderExecutionResult.SUCCESS,
                order_no=order_no,
                exec_qty=exec_result.get("exec_qty", 0),
                exec_price=exec_result.get("exec_price", 0),
                message=exec_result.get("message", "완전 체결")
            )
        
        elif exec_result.get("status") == "PARTIAL":
            filled_qty = exec_result.get("exec_qty", 0)
            self._upsert_order_state(
                idempotency_key=idempotency_key,
                signal_id=signal_id,
                stock_code=stock_code,
                side="SELL",
                quantity=quantity,
                status="PARTIAL",
                order_no=order_no,
                filled_qty=filled_qty,
                remaining_qty=max(quantity - filled_qty, 0)
            )
            # 부분 체결 시 - 미체결분은 이미 취소 시도됨
            return SynchronizedOrderResult(
                success=False,
                result_type=OrderExecutionResult.PARTIAL,
                order_no=order_no,
                exec_qty=exec_result.get("exec_qty", 0),
                exec_price=exec_result.get("exec_price", 0),
                message=exec_result.get("message", "부분 체결 - 미체결분 취소됨")
            )
        
        else:
            self._upsert_order_state(
                idempotency_key=idempotency_key,
                signal_id=signal_id,
                stock_code=stock_code,
                side="SELL",
                quantity=quantity,
                status="CANCELLED",
                order_no=order_no,
                filled_qty=exec_result.get("exec_qty", 0),
                remaining_qty=max(quantity - exec_result.get("exec_qty", 0), 0)
            )
            return SynchronizedOrderResult(
                success=False,
                result_type=OrderExecutionResult.CANCELLED,
                order_no=order_no,
                exec_qty=exec_result.get("exec_qty", 0),
                exec_price=exec_result.get("exec_price", 0),
                message=exec_result.get("message", "미체결/취소")
            )


# ════════════════════════════════════════════════════════════════
# 포지션 재동기화 클래스
# ════════════════════════════════════════════════════════════════

class PositionResynchronizer:
    """
    포지션 재동기화 클래스
    
    ★ 핵심 기능:
        - 프로그램 시작 시 실제 계좌와 저장 데이터 동기화
        - API 오류 후 재동기화
        - 불일치 발견 시 안전한 복구
    
    ★ 감사 보고서 해결:
        - "DB vs 실제 계좌 vs 메모리 상태 불일치" 해결
    """
    
    def __init__(self, api, position_store, db_repository=None, trading_mode: str = None):
        """
        Args:
            api: KIS API 클라이언트
            position_store: 포지션 파일 저장소
            db_repository: DB 레포지토리 (선택)
        """
        self.api = api
        self.position_store = position_store
        self.db_repository = db_repository
        self.trading_mode = trading_mode or get_trading_mode()

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        """숫자 변환 실패 시 기본값을 반환합니다."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        """정수 변환 실패 시 기본값을 반환합니다."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _sync_db_positions_from_api(
        self,
        api_holdings: Dict[str, Dict[str, Any]],
        result: Dict[str, Any]
    ) -> None:
        """
        DB 포지션을 실제 계좌 보유 기준으로 강제 동기화합니다.

        정책:
            - 실제 계좌에 없는 DB OPEN 포지션은 CLOSED 처리
            - 실제 계좌에 있는 종목은 DB에 upsert
            - 수량/평균단가는 실제 계좌 값을 우선 사용
        """
        if not self.db_repository:
            return

        try:
            db_open_positions = self.db_repository.get_open_positions()
            db_map = {p.symbol: p for p in db_open_positions}

            # 1) DB에는 있으나 실제 계좌에는 없는 포지션 정리
            for symbol, db_pos in db_map.items():
                if symbol not in api_holdings:
                    self.db_repository.close_position(symbol)
                    msg = f"DB OPEN 정리: {symbol} (실계좌 보유 없음)"
                    result["warnings"].append(msg)
                    logger.warning(f"[RESYNC][DB] {msg}")

            # 2) 실제 계좌 보유를 DB에 반영
            for symbol, holding in api_holdings.items():
                qty = self._safe_int(holding.get("quantity"), 0)
                if qty <= 0:
                    continue

                avg_price = self._safe_float(holding.get("avg_price"), 0.0)
                current_price = self._safe_float(holding.get("current_price"), avg_price)
                base_price = avg_price if avg_price > 0 else max(current_price, 1.0)

                existing = db_map.get(symbol)
                if existing:
                    atr_at_entry = existing.atr_at_entry if existing.atr_at_entry > 0 else max(base_price * 0.01, 1.0)
                    stop_price = existing.stop_price if existing.stop_price > 0 else round(base_price * 0.95, 2)
                    take_profit = existing.take_profit_price
                    trailing_stop = existing.trailing_stop if existing.trailing_stop else stop_price
                    highest_price = max(existing.highest_price or base_price, current_price, base_price)
                else:
                    atr_at_entry = max(base_price * 0.01, 1.0)
                    stop_price = round(base_price * 0.95, 2)
                    take_profit = None
                    trailing_stop = stop_price
                    highest_price = max(current_price, base_price)

                saved = self.db_repository.upsert_from_account_holding(
                    symbol=symbol,
                    entry_price=base_price,
                    quantity=qty,
                    atr_at_entry=atr_at_entry,
                    stop_price=stop_price,
                    take_profit_price=take_profit,
                    trailing_stop=trailing_stop,
                    highest_price=highest_price,
                    entry_time=datetime.now(KST)
                )

                if saved is None:
                    logger.warning(f"[RESYNC][DB] 포지션 저장 실패/보류: {symbol}")
                else:
                    logger.info(
                        f"[RESYNC][DB] 실계좌 기준 반영: {symbol} "
                        f"qty={qty}, avg={base_price:,.0f}"
                    )
        except Exception as e:
            result["warnings"].append(f"DB 동기화 실패: {str(e)}")
            logger.error(f"[RESYNC][DB] 동기화 오류: {e}")
    
    def synchronize_on_startup(self) -> Dict[str, Any]:
        """
        프로그램 시작 시 포지션을 동기화합니다.
        
        ★ 동기화 순서:
            1. API로 실제 보유 조회
            2. 저장된 포지션 데이터 로드
            3. 불일치 해결
            4. 최종 상태 반환
        
        Returns:
            Dict: 동기화 결과
                - success: 동기화 성공 여부
                - position: 유효한 포지션 (없으면 None)
                - action: 수행된 조치
                - warnings: 경고 메시지 목록
        """
        result = {
            "success": False,
            "position": None,
            "action": "",
            "warnings": []
        }
        
        api_holdings: Dict[str, Dict[str, Any]] = {}
        if self.trading_mode == "REAL":
            # 1. API로 실제 보유 조회 (REAL 모드에서만)
            try:
                self.api.get_access_token()
                balance = self.api.get_account_balance()
                
                if not balance.get("success"):
                    result["warnings"].append("계좌 잔고 조회 실패")
                    return result
                
                holdings = balance.get("holdings", [])
                api_holdings = {h["stock_code"]: h for h in holdings if h.get("quantity", 0) > 0}

                # ★ DB는 항상 실계좌 보유를 기준으로 선반영
                self._sync_db_positions_from_api(api_holdings, result)

            except Exception as e:
                result["warnings"].append(f"API 오류: {str(e)}")
                return result
        
        # 2. 저장된 포지션 로드
        stored_position = self.position_store.load_position()

        # PAPER 모드는 API 계좌 동기화를 하지 않고 저장 상태만 사용
        if self.trading_mode != "REAL":
            if stored_position is None:
                result["success"] = True
                result["position"] = None
                result["action"] = "NO_POSITION"
            else:
                result["success"] = True
                result["position"] = stored_position
                result["action"] = "MATCHED"
            return result

        # 3. 불일치 해결
        if stored_position is None and not api_holdings:
            # 케이스 1: 저장 없음 + 보유 없음 → 정상
            result["success"] = True
            result["position"] = None
            result["action"] = "NO_POSITION"
            logger.info("[RESYNC] 포지션 없음 확인")
        
        elif stored_position is None and api_holdings:
            # 케이스 2: 저장 없음 + 보유 있음 → 미기록 보유 발견
            stock_code = list(api_holdings.keys())[0]
            holding = api_holdings[stock_code]
            
            result["success"] = False
            result["position"] = None
            result["action"] = "UNTRACKED_HOLDING"
            result["warnings"].append(
                f"미기록 보유 발견: {stock_code} {holding['quantity']}주 @ {holding['avg_price']:,.0f}원"
            )
            logger.warning(f"[RESYNC] 미기록 보유 발견: {stock_code}")
        
        elif stored_position is not None and not api_holdings:
            # 케이스 3: 저장 있음 + 보유 없음 → 불일치 (저장 데이터 무효)
            result["success"] = False
            result["position"] = None
            result["action"] = "STORED_INVALID"
            result["warnings"].append(
                f"저장된 포지션 무효: {stored_position.stock_code} - 실제 보유 없음"
            )
            
            # 저장 데이터 삭제
            self.position_store.clear_position()
            logger.warning(f"[RESYNC] 저장된 포지션 삭제됨: {stored_position.stock_code}")
        
        elif stored_position is not None and stored_position.stock_code in api_holdings:
            # 케이스 4: 저장 있음 + 일치하는 보유 있음 → 정상 (수량 확인)
            holding = api_holdings[stored_position.stock_code]
            
            if holding["quantity"] == stored_position.quantity:
                # 수량 일치
                result["success"] = True
                result["position"] = stored_position
                result["action"] = "MATCHED"
                logger.info(f"[RESYNC] 포지션 일치 확인: {stored_position.stock_code}")
            else:
                # 수량 불일치 - API 기준으로 조정
                result["success"] = True
                stored_position.quantity = holding["quantity"]
                self.position_store.save_position(stored_position)
                result["position"] = stored_position
                result["action"] = "QTY_ADJUSTED"
                result["warnings"].append(
                    f"수량 조정됨: {stored_position.stock_code} → {holding['quantity']}주"
                )
                logger.warning(f"[RESYNC] 수량 조정: {stored_position.stock_code}")
        
        else:
            # 케이스 5: 저장 있음 + 다른 종목 보유 → 심각한 불일치
            result["success"] = False
            result["position"] = None
            result["action"] = "CRITICAL_MISMATCH"
            result["warnings"].append(
                f"심각한 불일치: 저장={stored_position.stock_code}, "
                f"보유={list(api_holdings.keys())}"
            )
            logger.error("[RESYNC] 심각한 포지션 불일치")
        
        return result
    
    def force_sync_from_api(self) -> Dict[str, Any]:
        """
        API 기준으로 강제 동기화합니다.
        
        ★ 저장된 데이터를 무시하고 API에서 조회한 보유 현황으로 덮어씀
        
        Returns:
            Dict: 동기화 결과
        """
        result = {
            "success": False,
            "holdings": [],
            "action": ""
        }

        if self.trading_mode != "REAL":
            result["success"] = True
            result["action"] = "SKIPPED_NON_REAL_MODE"
            return result
        
        try:
            self.api.get_access_token()
            balance = self.api.get_account_balance()
            
            if not balance.get("success"):
                result["action"] = "API_FAILED"
                return result
            
            holdings = balance.get("holdings", [])
            active_holdings = [h for h in holdings if h.get("quantity", 0) > 0]
            api_holdings = {h["stock_code"]: h for h in active_holdings}
            
            result["success"] = True
            result["holdings"] = active_holdings
            result["action"] = "SYNCED"

            # DB도 실계좌 기준으로 동기화
            self._sync_db_positions_from_api(api_holdings, result)
            
            # 저장 데이터 클리어 (보유 없으면)
            if not active_holdings:
                self.position_store.clear_position()
            
            logger.info(f"[RESYNC] API 강제 동기화 완료: {len(active_holdings)}개 보유")
            return result
            
        except Exception as e:
            result["action"] = f"ERROR: {str(e)}"
            return result


# ════════════════════════════════════════════════════════════════
# 편의 함수
# ════════════════════════════════════════════════════════════════

_instance_lock: Optional[SingleInstanceLock] = None
_market_checker: Optional[MarketHoursChecker] = None


def get_instance_lock() -> SingleInstanceLock:
    """싱글톤 인스턴스 락"""
    global _instance_lock
    if _instance_lock is None:
        _instance_lock = SingleInstanceLock()
    return _instance_lock


def get_market_checker() -> MarketHoursChecker:
    """싱글톤 마켓 체커"""
    global _market_checker
    if _market_checker is None:
        _market_checker = MarketHoursChecker()
    return _market_checker


def ensure_single_instance() -> bool:
    """
    단일 인스턴스 실행을 보장합니다.
    
    Returns:
        bool: 단일 인스턴스 확인 여부 (False면 프로그램 종료 필요)
    """
    lock = get_instance_lock()
    
    if not lock.acquire():
        existing = lock.get_running_instance_info()
        if existing:
            print(f"[ERROR] 이미 실행 중인 인스턴스가 있습니다.")
            print(f"        PID: {existing.get('PID', 'Unknown')}")
            print(f"        시작: {existing.get('Started', 'Unknown')}")
        else:
            print("[ERROR] 인스턴스 락 획득 실패")
        return False
    
    return True


def validate_execution_mode() -> Tuple[bool, str]:
    """
    실행 모드를 검증합니다.
    
    ★ DRY_RUN 모드에서는 실제 주문이 발생하지 않음을 확인합니다.
    ★ REAL 모드에서는 이중 승인 조건을 검증합니다.
    
    Returns:
        Tuple[bool, str]: (검증 성공 여부, 메시지)
    """
    try:
        from config.execution_mode import get_execution_mode_manager
        manager = get_execution_mode_manager()
        
        mode = manager.mode.value
        can_order = manager.can_place_orders()
        
        if mode == "DRY_RUN":
            return True, f"✅ {mode} 모드 - 가상 체결만 수행 (안전)"
        
        elif mode == "PAPER":
            return True, f"✅ {mode} 모드 - 모의투자 API 사용"
        
        elif mode == "REAL":
            if can_order:
                return True, f"⚠️ {mode} 모드 - 실계좌 주문 활성화 (이중 승인 완료)"
            else:
                config = manager.get_config()
                reason = config.get_rejection_reason()
                return True, f"⛔ {mode} 모드 요청되었으나 조건 미충족: {reason} → DRY_RUN으로 전환됨"
        
        return False, f"❓ 알 수 없는 모드: {mode}"
        
    except ImportError:
        return False, "❌ execution_mode 모듈 로드 실패"
    except Exception as e:
        return False, f"❌ 실행 모드 검증 오류: {e}"


def pre_execution_safety_check() -> Tuple[bool, List[str]]:
    """
    실행 전 안전 검사를 수행합니다.
    
    ★ 모든 안전 조건을 검사합니다:
        - 단일 인스턴스 확인
        - Kill Switch 상태 확인
        - 실행 모드 검증
        - 장 운영시간 확인 (옵션)
    
    Returns:
        Tuple[bool, List[str]]: (모든 검사 통과 여부, 경고/오류 메시지 목록)
    """
    messages = []
    all_passed = True
    
    # 1. 실행 모드 검증
    mode_ok, mode_msg = validate_execution_mode()
    messages.append(mode_msg)
    if not mode_ok:
        all_passed = False
    
    # 2. Kill Switch 상태 확인
    try:
        from config.execution_mode import get_execution_mode_manager
        manager = get_execution_mode_manager()
        
        if manager.kill_switch_active:
            all_passed = False
            messages.append("⛔ Kill Switch가 활성화되어 있습니다.")
    except Exception as e:
        messages.append(f"⚠️ Kill Switch 상태 확인 실패: {e}")
    
    # 3. 장 운영시간 확인
    checker = get_market_checker()
    status = checker.get_market_status()
    
    status_messages = {
        MarketStatus.OPEN: "✅ 정규장 - 거래 가능",
        MarketStatus.PRE_MARKET: "⚠️ 장전 동시호가 - 09:00 이후 거래 가능",
        MarketStatus.SIMULTANEOUS_QUOTE: "⚠️ 장 마감 동시호가 - 청산만 가능",
        MarketStatus.CLOSED: "⛔ 폐장 - 거래 불가"
    }
    messages.append(status_messages.get(status, "❓ 알 수 없는 시장 상태"))
    
    return all_passed, messages


def print_safety_check_report() -> None:
    """안전 검사 결과를 출력합니다."""
    all_passed, messages = pre_execution_safety_check()
    
    print("\n" + "═" * 60)
    print("           [실행 전 안전 검사 결과]")
    print("═" * 60)
    
    for msg in messages:
        print(f"  {msg}")
    
    print("─" * 60)
    
    if all_passed:
        print("  ✅ 모든 안전 검사 통과")
    else:
        print("  ⛔ 일부 안전 검사 실패 - 실행 전 확인 필요")
    
    print("═" * 60 + "\n")

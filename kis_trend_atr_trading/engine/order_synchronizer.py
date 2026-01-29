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
from pathlib import Path
from datetime import datetime, time as dt_time
from typing import Dict, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum

from utils.logger import get_logger

logger = get_logger("order_synchronizer")


# ════════════════════════════════════════════════════════════════
# 상수 정의
# ════════════════════════════════════════════════════════════════

LOCK_FILE_PATH = Path(__file__).parent.parent / "data" / "instance.lock"

# 한국 주식시장 시간
MARKET_OPEN = dt_time(9, 0, 0)
MARKET_CLOSE = dt_time(15, 30, 0)
SIMULTANEOUS_QUOTE_START = dt_time(15, 20, 0)  # 동시호가 시작
PRE_MARKET_START = dt_time(8, 30, 0)  # 장전 동시호가
PRE_MARKET_END = dt_time(9, 0, 0)


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
            self._lock_fd = open(self.lock_file, 'w')
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            
            # 프로세스 정보 기록
            self._lock_fd.write(f"PID: {os.getpid()}\n")
            self._lock_fd.write(f"Started: {datetime.now().isoformat()}\n")
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
        check_time = check_time or datetime.now()
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
        check_time = check_time or datetime.now()
        current_time = check_time.time()
        
        if MARKET_OPEN <= current_time < SIMULTANEOUS_QUOTE_START:
            return 0  # 이미 장중
        
        # 오늘 장 시작까지
        if current_time < MARKET_OPEN:
            market_open_today = datetime.combine(check_time.date(), MARKET_OPEN)
            return int((market_open_today - check_time).total_seconds())
        
        # 내일 장 시작까지
        from datetime import timedelta
        tomorrow = check_time.date() + timedelta(days=1)
        
        # 주말 고려
        while tomorrow.weekday() >= 5:
            tomorrow += timedelta(days=1)
        
        market_open_next = datetime.combine(tomorrow, MARKET_OPEN)
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
        check_time = check_time or datetime.now()
        current_time = check_time.time()
        
        if current_time >= SIMULTANEOUS_QUOTE_START:
            return 0  # 이미 동시호가 시작됨
        
        if current_time < MARKET_OPEN:
            return 0  # 아직 장 시작 전
        
        close_time = datetime.combine(check_time.date(), SIMULTANEOUS_QUOTE_START)
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
    
    def execute_buy_order(
        self,
        stock_code: str,
        quantity: int,
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
            return SynchronizedOrderResult(
                success=True,
                result_type=OrderExecutionResult.SUCCESS,
                order_no=order_no,
                exec_qty=exec_result.get("exec_qty", 0),
                exec_price=exec_result.get("exec_price", 0),
                message=exec_result.get("message", "완전 체결")
            )
        
        elif exec_result.get("status") == "PARTIAL":
            return SynchronizedOrderResult(
                success=False,
                result_type=OrderExecutionResult.PARTIAL,
                order_no=order_no,
                exec_qty=exec_result.get("exec_qty", 0),
                exec_price=exec_result.get("exec_price", 0),
                message=exec_result.get("message", "부분 체결")
            )
        
        else:
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
            return SynchronizedOrderResult(
                success=True,
                result_type=OrderExecutionResult.SUCCESS,
                order_no=order_no,
                exec_qty=exec_result.get("exec_qty", 0),
                exec_price=exec_result.get("exec_price", 0),
                message=exec_result.get("message", "완전 체결")
            )
        
        elif exec_result.get("status") == "PARTIAL":
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
    
    def __init__(self, api, position_store, db_repository=None):
        """
        Args:
            api: KIS API 클라이언트
            position_store: 포지션 파일 저장소
            db_repository: DB 레포지토리 (선택)
        """
        self.api = api
        self.position_store = position_store
        self.db_repository = db_repository
    
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
        
        # 1. API로 실제 보유 조회
        try:
            self.api.get_access_token()
            balance = self.api.get_account_balance()
            
            if not balance.get("success"):
                result["warnings"].append("계좌 잔고 조회 실패")
                return result
            
            holdings = balance.get("holdings", [])
            api_holdings = {h["stock_code"]: h for h in holdings if h.get("quantity", 0) > 0}
            
        except Exception as e:
            result["warnings"].append(f"API 오류: {str(e)}")
            return result
        
        # 2. 저장된 포지션 로드
        stored_position = self.position_store.load_position()
        
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
        
        try:
            self.api.get_access_token()
            balance = self.api.get_account_balance()
            
            if not balance.get("success"):
                result["action"] = "API_FAILED"
                return result
            
            holdings = balance.get("holdings", [])
            active_holdings = [h for h in holdings if h.get("quantity", 0) > 0]
            
            result["success"] = True
            result["holdings"] = active_holdings
            result["action"] = "SYNCED"
            
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

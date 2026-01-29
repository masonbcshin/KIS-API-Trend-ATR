"""
═══════════════════════════════════════════════════════════════════════════════
KIS Trend-ATR Trading System - 감사 추적 로거
═══════════════════════════════════════════════════════════════════════════════

모든 트레이딩 이벤트를 감사 추적용으로 기록합니다.

★ 기록 대상:
    - 시그널 판단 (BUY/SELL/HOLD)
    - 주문 요청 (매수/매도)
    - 체결 결과
    - 오류 발생
    - 리스크 이벤트 (Kill Switch, Daily Loss 등)

★ 저장 형식:
    - JSON 파일 (날짜별)
    - 구조화된 이벤트 데이터

★ 목적:
    - "왜 이 매매가 발생했는지" 역추적 가능
    - 규제 준수 및 감사 대응
    - 디버깅 및 전략 개선
"""

import json
import os
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from enum import Enum
import threading
import gzip
import shutil

from utils.logger import get_logger

logger = get_logger("audit_logger")


# ═══════════════════════════════════════════════════════════════════════════════
# 열거형 및 데이터 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class AuditEventType(Enum):
    """감사 이벤트 타입"""
    # 시스템 이벤트
    SYSTEM_START = "SYSTEM_START"
    SYSTEM_STOP = "SYSTEM_STOP"
    CONFIG_LOADED = "CONFIG_LOADED"
    
    # 시그널 이벤트
    SIGNAL_GENERATED = "SIGNAL_GENERATED"
    SIGNAL_FILTERED = "SIGNAL_FILTERED"
    
    # 주문 이벤트
    ORDER_REQUESTED = "ORDER_REQUESTED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    
    # 포지션 이벤트
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_UPDATED = "POSITION_UPDATED"
    POSITION_CLOSED = "POSITION_CLOSED"
    POSITION_RESTORED = "POSITION_RESTORED"
    
    # 리스크 이벤트
    RISK_CHECK_PASSED = "RISK_CHECK_PASSED"
    RISK_CHECK_FAILED = "RISK_CHECK_FAILED"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    KILL_SWITCH_ACTIVATED = "KILL_SWITCH_ACTIVATED"
    
    # 에러 이벤트
    ERROR_OCCURRED = "ERROR_OCCURRED"
    API_ERROR = "API_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    
    # 기타
    CUSTOM = "CUSTOM"


class AuditSeverity(Enum):
    """이벤트 심각도"""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass
class AuditEvent:
    """
    감사 이벤트 데이터 클래스
    
    모든 이벤트는 이 형식으로 기록됩니다.
    """
    # 필수 필드
    event_id: str
    event_type: AuditEventType
    timestamp: str
    severity: AuditSeverity
    
    # 컨텍스트
    stock_code: str = ""
    order_no: str = ""
    session_id: str = ""
    
    # 상세 정보
    message: str = ""
    details: Dict[str, Any] = None
    
    # 메타데이터
    source: str = ""  # 이벤트 발생 모듈
    user: str = ""    # 사용자/시스템 식별
    
    def __post_init__(self):
        if self.details is None:
            self.details = {}
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환"""
        d = asdict(self)
        d["event_type"] = self.event_type.value
        d["severity"] = self.severity.value
        return d
    
    @classmethod
    def from_dict(cls, data: Dict) -> "AuditEvent":
        """딕셔너리에서 생성"""
        data["event_type"] = AuditEventType(data["event_type"])
        data["severity"] = AuditSeverity(data["severity"])
        return cls(**data)


# ═══════════════════════════════════════════════════════════════════════════════
# 감사 로거 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class AuditLogger:
    """
    감사 추적 로거
    
    모든 트레이딩 이벤트를 구조화된 형식으로 기록합니다.
    
    Usage:
        audit = AuditLogger()
        
        # 시그널 기록
        audit.log_signal(
            stock_code="005930",
            signal_type="BUY",
            reason="상승 추세 + 돌파",
            price=70000,
            stop_loss=68000
        )
        
        # 주문 기록
        audit.log_order_submitted(
            stock_code="005930",
            order_type="BUY",
            price=70000,
            quantity=10,
            order_no="123456"
        )
        
        # 에러 기록
        audit.log_error(
            error_type="API_ERROR",
            message="토큰 만료",
            details={"response_code": 401}
        )
    """
    
    def __init__(
        self,
        log_dir: Path = None,
        session_id: str = None,
        max_events_per_file: int = 10000,
        compress_old_logs: bool = True,
        retention_days: int = 90
    ):
        """
        감사 로거 초기화
        
        Args:
            log_dir: 로그 저장 디렉토리
            session_id: 세션 식별자
            max_events_per_file: 파일당 최대 이벤트 수
            compress_old_logs: 과거 로그 압축 여부
            retention_days: 로그 보관 기간 (일)
        """
        self.log_dir = log_dir or Path(__file__).parent.parent / "logs" / "audit"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.session_id = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.max_events_per_file = max_events_per_file
        self.compress_old_logs = compress_old_logs
        self.retention_days = retention_days
        
        self._lock = threading.Lock()
        self._event_counter = 0
        self._current_date = date.today()
        self._events_buffer: List[AuditEvent] = []
        
        # 현재 날짜 파일
        self._current_file = self._get_log_file_path()
        
        # 시작 이벤트 기록
        self.log_system_start()
        
        logger.info(
            f"[AUDIT] 감사 로거 초기화: "
            f"세션={self.session_id}, "
            f"경로={self.log_dir}"
        )
    
    def _get_log_file_path(self, target_date: date = None) -> Path:
        """로그 파일 경로를 반환합니다."""
        target_date = target_date or date.today()
        return self.log_dir / f"audit_{target_date.strftime('%Y%m%d')}.json"
    
    def _generate_event_id(self) -> str:
        """이벤트 ID를 생성합니다."""
        self._event_counter += 1
        return f"{self.session_id}_{self._event_counter:06d}"
    
    def _check_date_change(self) -> None:
        """날짜 변경을 확인하고 필요시 새 파일을 시작합니다."""
        today = date.today()
        if today != self._current_date:
            # 이전 날짜 파일 저장
            self._flush_buffer()
            
            # 압축 및 정리
            if self.compress_old_logs:
                self._compress_old_log(self._current_file)
            
            self._current_date = today
            self._current_file = self._get_log_file_path()
            
            # 보관 기간 초과 로그 삭제
            self._cleanup_old_logs()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 이벤트 기록 메서드
    # ═══════════════════════════════════════════════════════════════════════════
    
    def log_event(
        self,
        event_type: AuditEventType,
        severity: AuditSeverity = AuditSeverity.INFO,
        stock_code: str = "",
        order_no: str = "",
        message: str = "",
        details: Dict[str, Any] = None,
        source: str = ""
    ) -> AuditEvent:
        """
        일반 이벤트를 기록합니다.
        
        Args:
            event_type: 이벤트 타입
            severity: 심각도
            stock_code: 종목 코드
            order_no: 주문번호
            message: 메시지
            details: 상세 정보
            source: 발생 모듈
            
        Returns:
            AuditEvent: 기록된 이벤트
        """
        with self._lock:
            self._check_date_change()
            
            event = AuditEvent(
                event_id=self._generate_event_id(),
                event_type=event_type,
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                severity=severity,
                stock_code=stock_code,
                order_no=order_no,
                session_id=self.session_id,
                message=message,
                details=details or {},
                source=source
            )
            
            self._events_buffer.append(event)
            
            # 버퍼가 가득 차면 플러시
            if len(self._events_buffer) >= 100:
                self._flush_buffer()
            
            return event
    
    def _flush_buffer(self) -> None:
        """버퍼를 파일에 기록합니다."""
        if not self._events_buffer:
            return
        
        try:
            # 기존 이벤트 로드
            existing_events = []
            if self._current_file.exists():
                with open(self._current_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    existing_events = data.get("events", [])
            
            # 새 이벤트 추가
            for event in self._events_buffer:
                existing_events.append(event.to_dict())
            
            # 파일 저장
            data = {
                "session_id": self.session_id,
                "date": str(self._current_date),
                "event_count": len(existing_events),
                "events": existing_events,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
            with open(self._current_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            self._events_buffer.clear()
            
        except Exception as e:
            logger.error(f"[AUDIT] 버퍼 플러시 실패: {e}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 시스템 이벤트
    # ═══════════════════════════════════════════════════════════════════════════
    
    def log_system_start(self, details: Dict = None) -> AuditEvent:
        """시스템 시작을 기록합니다."""
        return self.log_event(
            event_type=AuditEventType.SYSTEM_START,
            severity=AuditSeverity.INFO,
            message="Trading system started",
            details={
                "session_id": self.session_id,
                "start_time": datetime.now().isoformat(),
                **(details or {})
            },
            source="AuditLogger"
        )
    
    def log_system_stop(self, reason: str = "", details: Dict = None) -> AuditEvent:
        """시스템 종료를 기록합니다."""
        event = self.log_event(
            event_type=AuditEventType.SYSTEM_STOP,
            severity=AuditSeverity.INFO,
            message=f"Trading system stopped: {reason}",
            details={
                "reason": reason,
                "stop_time": datetime.now().isoformat(),
                **(details or {})
            },
            source="AuditLogger"
        )
        self._flush_buffer()
        return event
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 시그널 이벤트
    # ═══════════════════════════════════════════════════════════════════════════
    
    def log_signal(
        self,
        stock_code: str,
        signal_type: str,
        reason: str,
        price: float = 0,
        stop_loss: float = 0,
        take_profit: float = 0,
        atr: float = 0,
        trend: str = "",
        details: Dict = None
    ) -> AuditEvent:
        """시그널 생성을 기록합니다."""
        return self.log_event(
            event_type=AuditEventType.SIGNAL_GENERATED,
            severity=AuditSeverity.INFO,
            stock_code=stock_code,
            message=f"Signal generated: {signal_type} - {reason}",
            details={
                "signal_type": signal_type,
                "reason": reason,
                "price": price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "atr": atr,
                "trend": trend,
                **(details or {})
            },
            source="Strategy"
        )
    
    def log_signal_filtered(
        self,
        stock_code: str,
        signal_type: str,
        filter_reason: str,
        details: Dict = None
    ) -> AuditEvent:
        """필터링된 시그널을 기록합니다."""
        return self.log_event(
            event_type=AuditEventType.SIGNAL_FILTERED,
            severity=AuditSeverity.DEBUG,
            stock_code=stock_code,
            message=f"Signal filtered: {signal_type} - {filter_reason}",
            details={
                "signal_type": signal_type,
                "filter_reason": filter_reason,
                **(details or {})
            },
            source="Strategy"
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 주문 이벤트
    # ═══════════════════════════════════════════════════════════════════════════
    
    def log_order_requested(
        self,
        stock_code: str,
        order_type: str,
        price: float,
        quantity: int,
        details: Dict = None
    ) -> AuditEvent:
        """주문 요청을 기록합니다."""
        return self.log_event(
            event_type=AuditEventType.ORDER_REQUESTED,
            severity=AuditSeverity.INFO,
            stock_code=stock_code,
            message=f"Order requested: {order_type} {quantity}주 @ {price:,.0f}",
            details={
                "order_type": order_type,
                "price": price,
                "quantity": quantity,
                **(details or {})
            },
            source="Executor"
        )
    
    def log_order_submitted(
        self,
        stock_code: str,
        order_no: str,
        order_type: str,
        price: float,
        quantity: int,
        details: Dict = None
    ) -> AuditEvent:
        """주문 제출을 기록합니다."""
        return self.log_event(
            event_type=AuditEventType.ORDER_SUBMITTED,
            severity=AuditSeverity.INFO,
            stock_code=stock_code,
            order_no=order_no,
            message=f"Order submitted: {order_no}",
            details={
                "order_type": order_type,
                "price": price,
                "quantity": quantity,
                **(details or {})
            },
            source="API"
        )
    
    def log_order_filled(
        self,
        stock_code: str,
        order_no: str,
        fill_price: float,
        fill_quantity: int,
        pnl: float = 0,
        details: Dict = None
    ) -> AuditEvent:
        """주문 체결을 기록합니다."""
        return self.log_event(
            event_type=AuditEventType.ORDER_FILLED,
            severity=AuditSeverity.INFO,
            stock_code=stock_code,
            order_no=order_no,
            message=f"Order filled: {order_no} @ {fill_price:,.0f}",
            details={
                "fill_price": fill_price,
                "fill_quantity": fill_quantity,
                "pnl": pnl,
                **(details or {})
            },
            source="API"
        )
    
    def log_order_rejected(
        self,
        stock_code: str,
        reason: str,
        details: Dict = None
    ) -> AuditEvent:
        """주문 거부를 기록합니다."""
        return self.log_event(
            event_type=AuditEventType.ORDER_REJECTED,
            severity=AuditSeverity.WARNING,
            stock_code=stock_code,
            message=f"Order rejected: {reason}",
            details={
                "rejection_reason": reason,
                **(details or {})
            },
            source="API"
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 리스크 이벤트
    # ═══════════════════════════════════════════════════════════════════════════
    
    def log_risk_check(
        self,
        passed: bool,
        check_type: str,
        reason: str = "",
        details: Dict = None
    ) -> AuditEvent:
        """리스크 체크 결과를 기록합니다."""
        event_type = (
            AuditEventType.RISK_CHECK_PASSED if passed
            else AuditEventType.RISK_CHECK_FAILED
        )
        severity = AuditSeverity.INFO if passed else AuditSeverity.WARNING
        
        return self.log_event(
            event_type=event_type,
            severity=severity,
            message=f"Risk check {'passed' if passed else 'failed'}: {check_type}",
            details={
                "check_type": check_type,
                "passed": passed,
                "reason": reason,
                **(details or {})
            },
            source="RiskManager"
        )
    
    def log_kill_switch(self, reason: str, details: Dict = None) -> AuditEvent:
        """킬 스위치 발동을 기록합니다."""
        return self.log_event(
            event_type=AuditEventType.KILL_SWITCH_ACTIVATED,
            severity=AuditSeverity.CRITICAL,
            message=f"KILL SWITCH ACTIVATED: {reason}",
            details={
                "reason": reason,
                **(details or {})
            },
            source="RiskManager"
        )
    
    def log_daily_loss_limit(
        self,
        current_loss: float,
        limit: float,
        details: Dict = None
    ) -> AuditEvent:
        """일일 손실 한도 도달을 기록합니다."""
        return self.log_event(
            event_type=AuditEventType.DAILY_LOSS_LIMIT,
            severity=AuditSeverity.WARNING,
            message=f"Daily loss limit reached: {current_loss:,.0f}",
            details={
                "current_loss": current_loss,
                "limit": limit,
                **(details or {})
            },
            source="RiskManager"
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 에러 이벤트
    # ═══════════════════════════════════════════════════════════════════════════
    
    def log_error(
        self,
        error_type: str,
        message: str,
        stock_code: str = "",
        details: Dict = None,
        exception: Exception = None
    ) -> AuditEvent:
        """에러를 기록합니다."""
        error_details = {
            "error_type": error_type,
            **(details or {})
        }
        
        if exception:
            error_details["exception_class"] = exception.__class__.__name__
            error_details["exception_message"] = str(exception)
        
        return self.log_event(
            event_type=AuditEventType.ERROR_OCCURRED,
            severity=AuditSeverity.ERROR,
            stock_code=stock_code,
            message=f"Error: {error_type} - {message}",
            details=error_details,
            source="System"
        )
    
    def log_api_error(
        self,
        endpoint: str,
        error_code: str,
        message: str,
        details: Dict = None
    ) -> AuditEvent:
        """API 에러를 기록합니다."""
        return self.log_event(
            event_type=AuditEventType.API_ERROR,
            severity=AuditSeverity.ERROR,
            message=f"API Error: {endpoint} - {error_code}",
            details={
                "endpoint": endpoint,
                "error_code": error_code,
                "error_message": message,
                **(details or {})
            },
            source="API"
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 유틸리티
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _compress_old_log(self, file_path: Path) -> None:
        """과거 로그 파일을 압축합니다."""
        if not file_path.exists():
            return
        
        try:
            gz_path = file_path.with_suffix('.json.gz')
            with open(file_path, 'rb') as f_in:
                with gzip.open(gz_path, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
            file_path.unlink()
            logger.debug(f"[AUDIT] 로그 압축: {file_path.name}")
        except Exception as e:
            logger.warning(f"[AUDIT] 로그 압축 실패: {e}")
    
    def _cleanup_old_logs(self) -> None:
        """보관 기간 초과 로그를 삭제합니다."""
        cutoff_date = date.today() - timedelta(days=self.retention_days)
        
        for file_path in self.log_dir.glob("audit_*.json*"):
            try:
                # 파일명에서 날짜 추출
                date_str = file_path.stem.replace("audit_", "").replace(".json", "")
                file_date = datetime.strptime(date_str, "%Y%m%d").date()
                
                if file_date < cutoff_date:
                    file_path.unlink()
                    logger.debug(f"[AUDIT] 오래된 로그 삭제: {file_path.name}")
            except:
                pass
    
    def get_events(
        self,
        target_date: date = None,
        event_type: AuditEventType = None,
        stock_code: str = None
    ) -> List[AuditEvent]:
        """
        이벤트를 조회합니다.
        
        Args:
            target_date: 조회 날짜 (None이면 오늘)
            event_type: 이벤트 타입 필터
            stock_code: 종목 코드 필터
            
        Returns:
            List[AuditEvent]: 이벤트 목록
        """
        self._flush_buffer()
        
        file_path = self._get_log_file_path(target_date)
        if not file_path.exists():
            return []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            events = [AuditEvent.from_dict(e) for e in data.get("events", [])]
            
            # 필터 적용
            if event_type:
                events = [e for e in events if e.event_type == event_type]
            if stock_code:
                events = [e for e in events if e.stock_code == stock_code]
            
            return events
            
        except Exception as e:
            logger.error(f"[AUDIT] 이벤트 조회 실패: {e}")
            return []
    
    def close(self) -> None:
        """감사 로거를 종료합니다."""
        self.log_system_stop("Normal shutdown")


# ═══════════════════════════════════════════════════════════════════════════════
# 편의 함수
# ═══════════════════════════════════════════════════════════════════════════════

_audit_logger: Optional[AuditLogger] = None


def get_audit_logger(**kwargs) -> AuditLogger:
    """
    싱글톤 AuditLogger를 반환합니다.
    
    Returns:
        AuditLogger: 감사 로거
    """
    global _audit_logger
    
    if _audit_logger is None:
        _audit_logger = AuditLogger(**kwargs)
    
    return _audit_logger

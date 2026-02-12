"""
KIS Trend-ATR Trading System - 로깅 유틸리티

시스템 전체에서 사용되는 로깅 설정을 관리합니다.
파일과 콘솔에 동시에 로그를 출력합니다.
"""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from .market_hours import KST
from env import get_trading_mode

# 로그 디렉토리 설정 (기본: ~/auto-trade/logs)
LOG_DIR = Path(os.path.expanduser(os.getenv("AUTO_TRADE_LOG_DIR", "~/auto-trade/logs")))


class KSTFormatter(logging.Formatter):
    """로그 타임스탬프를 KST 기준으로 포맷합니다."""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=KST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec="seconds")


def setup_logger(
    name: str = "kis_trading",
    level: str = "INFO",
    log_to_file: bool = True,
    log_dir: Optional[Path] = None
) -> logging.Logger:
    """
    로거를 설정하고 반환합니다.
    
    Args:
        name: 로거 이름
        level: 로그 레벨 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_to_file: 파일에 로그 저장 여부
        log_dir: 로그 파일 저장 디렉토리
    
    Returns:
        logging.Logger: 설정된 로거 인스턴스
    """
    # 로거 생성
    logger = logging.getLogger(name)
    
    # 이미 핸들러가 설정되어 있으면 기존 로거 반환
    if logger.handlers:
        return logger
    
    # PAPER/REAL 정책: 둘 다 INFO 기본 (전략 내부 디버그만 DEBUG 별도 사용)
    mode = get_trading_mode()
    if mode in ("PAPER", "REAL"):
        log_level = logging.INFO
    else:
        log_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(log_level)
    
    # 로그 포맷 설정
    formatter = KSTFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # 콘솔 핸들러 추가
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 파일 핸들러 추가 (선택적)
    if log_to_file:
        if log_dir is None:
            log_dir = LOG_DIR
        
        # 로그 디렉토리 생성
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # 날짜별 로그 파일
        log_filename = f"{name}_{datetime.now(KST).strftime('%Y%m%d')}.log"
        log_filepath = log_dir / log_filename
        
        file_handler = RotatingFileHandler(
            log_filepath,
            encoding="utf-8",
            mode="a",
            maxBytes=10 * 1024 * 1024,
            backupCount=10
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        
        logger.debug(f"로그 파일 경로: {log_filepath}")
    
    return logger


def get_logger(name: str = "kis_trading") -> logging.Logger:
    """
    이미 설정된 로거를 반환하거나 기본 설정으로 새 로거를 생성합니다.
    
    Args:
        name: 로거 이름
    
    Returns:
        logging.Logger: 로거 인스턴스
    """
    logger = logging.getLogger(name)
    
    # 핸들러가 없으면 기본 설정으로 초기화
    if not logger.handlers:
        return setup_logger(name)
    
    return logger


class TradeLogger:
    """
    거래 전용 로거 클래스
    
    매매 기록을 구조화된 형태로 저장합니다.
    """
    
    def __init__(self, logger_name: str = "kis_trading"):
        self.logger = get_logger(logger_name)
    
    def log_signal(
        self,
        signal_type: str,
        stock_code: str,
        price: float,
        reason: str
    ) -> None:
        """
        매매 시그널을 로깅합니다.
        
        Args:
            signal_type: 시그널 타입 (BUY, SELL, HOLD)
            stock_code: 종목 코드
            price: 현재가
            reason: 시그널 발생 사유
        """
        self.logger.info(
            f"[시그널] {signal_type} | 종목: {stock_code} | "
            f"가격: {price:,.0f}원 | 사유: {reason}"
        )
    
    def log_order(
        self,
        order_type: str,
        stock_code: str,
        quantity: int,
        price: float,
        order_no: str = ""
    ) -> None:
        """
        주문 실행을 로깅합니다.
        
        Args:
            order_type: 주문 타입 (BUY, SELL)
            stock_code: 종목 코드
            quantity: 주문 수량
            price: 주문 가격
            order_no: 주문 번호
        """
        self.logger.info(
            f"[주문] {order_type} | 종목: {stock_code} | "
            f"수량: {quantity}주 | 가격: {price:,.0f}원 | 주문번호: {order_no}"
        )
    
    def log_position(
        self,
        action: str,
        stock_code: str,
        entry_price: float,
        current_price: float,
        stop_loss: float,
        take_profit: float,
        pnl_pct: float = 0.0
    ) -> None:
        """
        포지션 상태를 로깅합니다.
        
        Args:
            action: 포지션 액션 (OPEN, UPDATE, CLOSE)
            stock_code: 종목 코드
            entry_price: 진입가
            current_price: 현재가
            stop_loss: 손절가
            take_profit: 익절가
            pnl_pct: 손익률 (%)
        """
        self.logger.info(
            f"[포지션] {action} | 종목: {stock_code} | "
            f"진입: {entry_price:,.0f}원 | 현재: {current_price:,.0f}원 | "
            f"손절: {stop_loss:,.0f}원 | 익절: {take_profit:,.0f}원 | "
            f"손익: {pnl_pct:+.2f}%"
        )
    
    def log_error(self, error_type: str, message: str) -> None:
        """
        에러를 로깅합니다.
        
        Args:
            error_type: 에러 타입
            message: 에러 메시지
        """
        self.logger.error(f"[에러] {error_type} | {message}")
    
    def log_api_call(
        self,
        endpoint: str,
        success: bool,
        response_time: float,
        message: str = ""
    ) -> None:
        """
        API 호출을 로깅합니다.
        
        Args:
            endpoint: API 엔드포인트
            success: 성공 여부
            response_time: 응답 시간 (초)
            message: 추가 메시지
        """
        status = "성공" if success else "실패"
        self.logger.debug(
            f"[API] {endpoint} | {status} | "
            f"응답시간: {response_time:.3f}초 | {message}"
        )

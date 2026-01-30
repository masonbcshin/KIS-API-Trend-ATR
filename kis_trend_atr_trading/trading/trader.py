"""
KIS Trend-ATR Trading System - MySQL 연동 트레이더

이 모듈은 매수/매도 로직을 MySQL과 연동하여 처리합니다.

★ 핵심 원칙:
    1. 모든 거래는 DB에 기록됩니다
    2. 프로그램 재시작 시 DB에서 포지션을 복원합니다
    3. 동일 종목 중복 진입은 DB 기준으로 차단됩니다
    4. 신호 전용 모드에서는 주문 없이 기록만 합니다

★ 트레이딩 플로우:
    [매수 시]
    1. DB에서 해당 종목 OPEN 포지션 존재 여부 확인
    2. 없으면 → positions에 INSERT + trades에 BUY 기록
    3. 있으면 → 중복 진입 차단
    
    [매도 시]
    1. DB에서 OPEN 포지션 조회
    2. 있으면 → positions CLOSED + trades에 SELL 기록 (손익 계산)
    3. 없으면 → 에러 반환

★ 트레이딩 모드:
    - LIVE: 실계좌 주문 → DB 기록
    - PAPER: 모의투자 주문 → DB 기록
    - CBT: 가상 체결 → DB 기록 (주문 API 호출 안 함)
    - SIGNAL_ONLY: 알림만 → DB 기록 (체결 없음)
"""

import os
from datetime import datetime, date
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass
from enum import Enum

from db.mysql import get_db_manager, MySQLManager
from db.repository import (
    PositionRepository,
    TradeRepository,
    AccountSnapshotRepository,
    PositionRecord,
    get_position_repository,
    get_trade_repository,
    get_account_snapshot_repository
)
from utils.logger import get_logger
from utils.telegram_notifier import get_telegram_notifier, TelegramNotifier

logger = get_logger("db_trader")


# ═══════════════════════════════════════════════════════════════════════════════
# 열거형 및 데이터 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class TradingMode(Enum):
    """
    트레이딩 모드
    
    ★ 중학생도 이해할 수 있는 설명:
        - LIVE: 진짜 돈으로 주식 사고 팜
        - PAPER: 가짜 돈으로 연습 (모의투자)
        - CBT: 더 안전한 연습 (API도 안 부름)
        - SIGNAL_ONLY: "지금 사야 해!" 알림만 (실제로 안 삼)
    """
    LIVE = "LIVE"           # 실계좌 주문
    PAPER = "PAPER"         # 모의투자 주문
    CBT = "CBT"             # 가상 체결 (주문 없음)
    SIGNAL_ONLY = "SIGNAL_ONLY"  # 신호만 (체결도 없음)


@dataclass
class TradeResult:
    """
    거래 결과 데이터 클래스
    
    ★ 매수/매도 결과를 담는 그릇
    """
    success: bool
    message: str
    symbol: str = ""
    side: str = ""  # BUY / SELL
    price: float = 0.0
    quantity: int = 0
    order_no: str = ""
    pnl: float = 0.0
    pnl_percent: float = 0.0
    mode: str = ""
    executed_at: datetime = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "message": self.message,
            "symbol": self.symbol,
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
            "order_no": self.order_no,
            "pnl": self.pnl,
            "pnl_percent": self.pnl_percent,
            "mode": self.mode,
            "executed_at": self.executed_at.isoformat() if self.executed_at else None
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 데이터베이스 연동 트레이더
# ═══════════════════════════════════════════════════════════════════════════════

class DatabaseTrader:
    """
    MySQL 연동 트레이더 클래스
    
    ★ 이 클래스가 하는 일:
        - 매수/매도 요청을 받음
        - 트레이딩 모드에 따라 주문 실행 (또는 가상 체결)
        - 결과를 MySQL에 저장
        - 텔레그램으로 알림 전송
    
    ★ 중학생도 이해할 수 있는 비유:
        - 주식 중개인 역할
        - "삼성전자 10주 사줘" → 사고 → 장부에 기록 → 문자 보냄
    
    사용 예시:
        trader = DatabaseTrader()
        
        # DB 연결
        trader.initialize()
        
        # 매수
        result = trader.buy(
            symbol="005930",
            price=70000,
            quantity=10,
            stop_loss=67000,
            take_profit=75000,
            atr=1500
        )
        
        if result.success:
            print(f"매수 성공! 주문번호: {result.order_no}")
        
        # 매도
        result = trader.sell(
            symbol="005930",
            price=72000,
            reason="TAKE_PROFIT"
        )
    """
    
    def __init__(
        self,
        mode: TradingMode = None,
        db: MySQLManager = None,
        position_repo: PositionRepository = None,
        trade_repo: TradeRepository = None,
        snapshot_repo: AccountSnapshotRepository = None,
        telegram: TelegramNotifier = None,
        api_client = None
    ):
        """
        트레이더 초기화
        
        Args:
            mode: 트레이딩 모드 (환경변수에서 자동 로드)
            db: MySQLManager
            position_repo: 포지션 Repository
            trade_repo: 거래 Repository
            snapshot_repo: 스냅샷 Repository
            telegram: 텔레그램 알림기
            api_client: KIS API 클라이언트 (실주문용)
        """
        # 트레이딩 모드 결정
        env_mode = os.getenv("TRADING_MODE", "PAPER").upper()
        
        if mode:
            self.mode = mode
        elif env_mode == "SIGNAL_ONLY":
            self.mode = TradingMode.SIGNAL_ONLY
        elif env_mode == "CBT":
            self.mode = TradingMode.CBT
        elif env_mode == "LIVE":
            self.mode = TradingMode.LIVE
        else:
            self.mode = TradingMode.PAPER
        
        # 의존성 주입
        self.db = db or get_db_manager()
        self.position_repo = position_repo or get_position_repository()
        self.trade_repo = trade_repo or get_trade_repository()
        self.snapshot_repo = snapshot_repo or get_account_snapshot_repository()
        self.telegram = telegram or get_telegram_notifier()
        self.api_client = api_client  # 실주문 시 필요
        
        # 초기화 상태
        self._initialized = False
        
        logger.info(f"[TRADER] 데이터베이스 트레이더 생성: 모드={self.mode.value}")
    
    def initialize(self) -> bool:
        """
        트레이더를 초기화합니다.
        
        ★ DB 연결 및 스키마 초기화
        
        Returns:
            bool: 초기화 성공 여부
        """
        try:
            # DB 연결
            if not self.db.is_connected():
                self.db.connect()
            
            # 스키마 초기화 (테이블 생성)
            self.db.initialize_schema()
            
            self._initialized = True
            logger.info("[TRADER] 초기화 완료")
            
            return True
            
        except Exception as e:
            logger.error(f"[TRADER] 초기화 실패: {e}")
            return False
    
    def _ensure_initialized(self) -> None:
        """초기화 확인"""
        if not self._initialized:
            self.initialize()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 매수 로직
    # ═══════════════════════════════════════════════════════════════════════════
    
    def buy(
        self,
        symbol: str,
        price: float,
        quantity: int,
        atr: float,
        stop_loss: float,
        take_profit: float = None,
        trailing_stop: float = None
    ) -> TradeResult:
        """
        매수를 실행합니다.
        
        ★ 실행 순서:
            1. DB에서 동일 종목 OPEN 포지션 확인
            2. 있으면 → 중복 진입 차단
            3. 없으면:
               - 모드에 따라 실주문 또는 가상 체결
               - positions 테이블에 INSERT
               - trades 테이블에 BUY 기록
               - 텔레그램 알림
        
        Args:
            symbol: 종목 코드
            price: 매수 가격
            quantity: 수량
            atr: 진입 시 ATR (★ 고정값으로 저장)
            stop_loss: 손절가
            take_profit: 익절가 (선택)
            trailing_stop: 트레일링 스탑 (선택)
        
        Returns:
            TradeResult: 매수 결과
        """
        self._ensure_initialized()
        
        executed_at = datetime.now()
        trailing_stop = trailing_stop or stop_loss
        
        logger.info(
            f"[TRADER] 매수 시작: {symbol} @ {price:,.0f}원 x {quantity}주, "
            f"모드={self.mode.value}"
        )
        
        # 1. 중복 진입 확인 (DB 기준)
        if self.position_repo.has_open_position(symbol):
            logger.warning(f"[TRADER] 중복 진입 차단: {symbol}에 이미 OPEN 포지션 존재")
            return TradeResult(
                success=False,
                message="이미 해당 종목에 열린 포지션이 있습니다",
                symbol=symbol,
                side="BUY",
                mode=self.mode.value
            )
        
        # 2. 모드별 처리
        order_no = ""
        
        if self.mode == TradingMode.SIGNAL_ONLY:
            # 신호만 기록 (체결 없음)
            order_no = f"SIGNAL-{executed_at.strftime('%Y%m%d%H%M%S')}"
            
            # trades에 SIGNAL_ONLY로 기록
            self.trade_repo.save_signal_only(
                symbol=symbol,
                side="BUY",
                price=price,
                quantity=quantity,
                reason="SIGNAL_ONLY",
                executed_at=executed_at
            )
            
            # 텔레그램 알림
            self.telegram.notify_cbt_signal(
                signal_type="📈 매수 신호 (SIGNAL_ONLY)",
                stock_code=symbol,
                price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                atr=atr,
                trend="UPTREND",
                reason="신호만 기록 - 실주문 없음"
            )
            
            return TradeResult(
                success=True,
                message="[SIGNAL_ONLY] 매수 신호 기록 완료",
                symbol=symbol,
                side="BUY",
                price=price,
                quantity=quantity,
                order_no=order_no,
                mode=self.mode.value,
                executed_at=executed_at
            )
        
        elif self.mode == TradingMode.CBT:
            # 가상 체결 (DB 기록)
            order_no = f"CBT-{executed_at.strftime('%Y%m%d%H%M%S')}"
            
        elif self.mode in (TradingMode.LIVE, TradingMode.PAPER):
            # 실제 주문
            if not self.api_client:
                return TradeResult(
                    success=False,
                    message="API 클라이언트가 설정되지 않았습니다",
                    symbol=symbol,
                    side="BUY",
                    mode=self.mode.value
                )
            
            try:
                result = self.api_client.place_buy_order(
                    stock_code=symbol,
                    quantity=quantity,
                    price=0,  # 시장가
                    order_type="01"
                )
                
                if not result["success"]:
                    return TradeResult(
                        success=False,
                        message=f"주문 실패: {result.get('message')}",
                        symbol=symbol,
                        side="BUY",
                        mode=self.mode.value
                    )
                
                order_no = result.get("order_no", "")
                
            except Exception as e:
                logger.error(f"[TRADER] 주문 API 오류: {e}")
                return TradeResult(
                    success=False,
                    message=f"주문 API 오류: {e}",
                    symbol=symbol,
                    side="BUY",
                    mode=self.mode.value
                )
        
        # 3. DB에 기록 (트랜잭션)
        try:
            with self.db.transaction() as cursor:
                # positions 테이블에 INSERT
                cursor.execute(
                    """
                    INSERT INTO positions (
                        symbol, entry_price, quantity, entry_time,
                        atr_at_entry, stop_price, take_profit_price,
                        trailing_stop, highest_price, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN')
                    """,
                    (
                        symbol, price, quantity, executed_at,
                        atr, stop_loss, take_profit,
                        trailing_stop, price
                    )
                )
                
                # trades 테이블에 BUY 기록
                cursor.execute(
                    """
                    INSERT INTO trades (
                        symbol, side, price, quantity, executed_at, order_no
                    ) VALUES (%s, 'BUY', %s, %s, %s, %s)
                    """,
                    (symbol, price, quantity, executed_at, order_no)
                )
            
            logger.info(
                f"[TRADER] 매수 완료: {symbol} @ {price:,.0f}원 x {quantity}주, "
                f"주문번호={order_no}"
            )
            
            # 4. 텔레그램 알림
            if self.mode == TradingMode.CBT:
                self.telegram.notify_cbt_signal(
                    signal_type="📈 매수 (CBT)",
                    stock_code=symbol,
                    price=price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    atr=atr,
                    trend="UPTREND",
                    reason="CBT 모드 가상 체결"
                )
            else:
                self.telegram.notify_buy_order(
                    stock_code=symbol,
                    price=price,
                    quantity=quantity,
                    stop_loss=stop_loss,
                    take_profit=take_profit or 0
                )
            
            return TradeResult(
                success=True,
                message="매수 체결 완료",
                symbol=symbol,
                side="BUY",
                price=price,
                quantity=quantity,
                order_no=order_no,
                mode=self.mode.value,
                executed_at=executed_at
            )
            
        except Exception as e:
            logger.error(f"[TRADER] 매수 DB 기록 실패: {e}")
            return TradeResult(
                success=False,
                message=f"DB 기록 실패: {e}",
                symbol=symbol,
                side="BUY",
                mode=self.mode.value
            )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 매도 로직
    # ═══════════════════════════════════════════════════════════════════════════
    
    def sell(
        self,
        symbol: str,
        price: float,
        reason: str = None
    ) -> TradeResult:
        """
        매도(청산)를 실행합니다.
        
        ★ 실행 순서:
            1. DB에서 해당 종목 OPEN 포지션 조회
            2. 없으면 → 에러 반환
            3. 있으면:
               - 손익 계산
               - 모드에 따라 실주문 또는 가상 체결
               - positions → CLOSED
               - trades에 SELL 기록 (손익 포함)
               - 텔레그램 알림
        
        Args:
            symbol: 종목 코드
            price: 매도 가격
            reason: 청산 사유 (ATR_STOP, TAKE_PROFIT, TRAILING_STOP, ...)
        
        Returns:
            TradeResult: 매도 결과
        """
        self._ensure_initialized()
        
        executed_at = datetime.now()
        
        logger.info(
            f"[TRADER] 매도 시작: {symbol} @ {price:,.0f}원, "
            f"사유={reason}, 모드={self.mode.value}"
        )
        
        # 1. OPEN 포지션 조회
        position = self.position_repo.get_by_symbol(symbol)
        
        if not position or position.status != "OPEN":
            logger.warning(f"[TRADER] 매도 실패: {symbol}에 열린 포지션 없음")
            return TradeResult(
                success=False,
                message="해당 종목에 열린 포지션이 없습니다",
                symbol=symbol,
                side="SELL",
                mode=self.mode.value
            )
        
        # 2. 손익 계산
        entry_price = position.entry_price
        quantity = position.quantity
        pnl = (price - entry_price) * quantity
        pnl_percent = ((price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
        
        # 보유 일수 계산
        holding_days = 1
        if position.entry_time:
            entry_date = position.entry_time.date() if isinstance(position.entry_time, datetime) else position.entry_time
            holding_days = (date.today() - entry_date).days + 1
        
        # 3. 모드별 처리
        order_no = ""
        
        if self.mode == TradingMode.SIGNAL_ONLY:
            # 신호만 기록 (체결 없음)
            order_no = f"SIGNAL-{executed_at.strftime('%Y%m%d%H%M%S')}"
            
            # trades에 SIGNAL_ONLY로 기록
            self.trade_repo.save_signal_only(
                symbol=symbol,
                side="SELL",
                price=price,
                quantity=quantity,
                reason="SIGNAL_ONLY",
                entry_price=entry_price,
                executed_at=executed_at
            )
            
            # 텔레그램 알림
            self.telegram.notify_cbt_signal(
                signal_type=f"📉 매도 신호 ({reason or 'SIGNAL_ONLY'})",
                stock_code=symbol,
                price=price,
                stop_loss=position.stop_price,
                take_profit=position.take_profit_price,
                atr=position.atr_at_entry,
                trend="",
                reason=f"예상 손익: {pnl:+,.0f}원 ({pnl_percent:+.2f}%)"
            )
            
            return TradeResult(
                success=True,
                message="[SIGNAL_ONLY] 매도 신호 기록 완료",
                symbol=symbol,
                side="SELL",
                price=price,
                quantity=quantity,
                order_no=order_no,
                pnl=pnl,
                pnl_percent=pnl_percent,
                mode=self.mode.value,
                executed_at=executed_at
            )
        
        elif self.mode == TradingMode.CBT:
            order_no = f"CBT-{executed_at.strftime('%Y%m%d%H%M%S')}"
            
        elif self.mode in (TradingMode.LIVE, TradingMode.PAPER):
            # 실제 주문
            if not self.api_client:
                return TradeResult(
                    success=False,
                    message="API 클라이언트가 설정되지 않았습니다",
                    symbol=symbol,
                    side="SELL",
                    mode=self.mode.value
                )
            
            try:
                result = self.api_client.place_sell_order(
                    stock_code=symbol,
                    quantity=quantity,
                    price=0,  # 시장가
                    order_type="01"
                )
                
                if not result["success"]:
                    return TradeResult(
                        success=False,
                        message=f"주문 실패: {result.get('message')}",
                        symbol=symbol,
                        side="SELL",
                        mode=self.mode.value
                    )
                
                order_no = result.get("order_no", "")
                
            except Exception as e:
                logger.error(f"[TRADER] 매도 주문 API 오류: {e}")
                return TradeResult(
                    success=False,
                    message=f"주문 API 오류: {e}",
                    symbol=symbol,
                    side="SELL",
                    mode=self.mode.value
                )
        
        # 4. DB에 기록 (트랜잭션)
        try:
            with self.db.transaction() as cursor:
                # positions → CLOSED
                cursor.execute(
                    """
                    UPDATE positions 
                    SET status = 'CLOSED'
                    WHERE symbol = %s AND status = 'OPEN'
                    """,
                    (symbol,)
                )
                
                # trades에 SELL 기록
                cursor.execute(
                    """
                    INSERT INTO trades (
                        symbol, side, price, quantity, executed_at,
                        reason, pnl, pnl_percent, entry_price, holding_days, order_no
                    ) VALUES (%s, 'SELL', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        symbol, price, quantity, executed_at,
                        reason, pnl, pnl_percent, entry_price, holding_days, order_no
                    )
                )
            
            pnl_str = f"{pnl:+,.0f}원 ({pnl_percent:+.2f}%)"
            logger.info(
                f"[TRADER] 매도 완료: {symbol} @ {price:,.0f}원, "
                f"손익={pnl_str}, 사유={reason}"
            )
            
            # 5. 텔레그램 알림
            self._send_sell_notification(
                symbol=symbol,
                entry_price=entry_price,
                exit_price=price,
                quantity=quantity,
                pnl=pnl,
                pnl_percent=pnl_percent,
                reason=reason
            )
            
            return TradeResult(
                success=True,
                message="매도 체결 완료",
                symbol=symbol,
                side="SELL",
                price=price,
                quantity=quantity,
                order_no=order_no,
                pnl=pnl,
                pnl_percent=pnl_percent,
                mode=self.mode.value,
                executed_at=executed_at
            )
            
        except Exception as e:
            logger.error(f"[TRADER] 매도 DB 기록 실패: {e}")
            return TradeResult(
                success=False,
                message=f"DB 기록 실패: {e}",
                symbol=symbol,
                side="SELL",
                mode=self.mode.value
            )
    
    def _send_sell_notification(
        self,
        symbol: str,
        entry_price: float,
        exit_price: float,
        quantity: int,
        pnl: float,
        pnl_percent: float,
        reason: str
    ) -> None:
        """청산 유형별 텔레그램 알림"""
        if self.mode == TradingMode.CBT:
            self.telegram.notify_cbt_signal(
                signal_type=f"📉 매도 ({reason or 'CBT'})",
                stock_code=symbol,
                price=exit_price,
                stop_loss=0,
                take_profit=None,
                atr=0,
                trend="",
                reason=f"손익: {pnl:+,.0f}원 ({pnl_percent:+.2f}%)"
            )
        elif reason == "ATR_STOP" or reason == "ATR_STOP_LOSS":
            self.telegram.notify_stop_loss(
                stock_code=symbol,
                entry_price=entry_price,
                exit_price=exit_price,
                pnl=pnl,
                pnl_pct=pnl_percent
            )
        elif reason == "TAKE_PROFIT" or reason == "ATR_TAKE_PROFIT":
            self.telegram.notify_take_profit(
                stock_code=symbol,
                entry_price=entry_price,
                exit_price=exit_price,
                pnl=pnl,
                pnl_pct=pnl_percent
            )
        else:
            self.telegram.notify_sell_order(
                stock_code=symbol,
                price=exit_price,
                quantity=quantity,
                reason=reason or "청산",
                pnl=pnl,
                pnl_pct=pnl_percent
            )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 포지션 관리
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_open_positions(self) -> List[PositionRecord]:
        """
        열린 포지션 목록을 반환합니다.
        
        Returns:
            List[PositionRecord]: 열린 포지션 목록
        """
        self._ensure_initialized()
        return self.position_repo.get_open_positions()
    
    def has_position(self, symbol: str = None) -> bool:
        """
        포지션 보유 여부를 확인합니다.
        
        Args:
            symbol: 종목 코드 (None이면 전체)
        
        Returns:
            bool: 포지션 보유 여부
        """
        self._ensure_initialized()
        return self.position_repo.has_open_position(symbol)
    
    def get_position(self, symbol: str) -> Optional[PositionRecord]:
        """
        특정 종목의 포지션을 조회합니다.
        
        Args:
            symbol: 종목 코드
        
        Returns:
            PositionRecord: 포지션 (없으면 None)
        """
        self._ensure_initialized()
        pos = self.position_repo.get_by_symbol(symbol)
        
        if pos and pos.status == "OPEN":
            return pos
        return None
    
    def update_trailing_stop(
        self,
        symbol: str,
        trailing_stop: float,
        highest_price: float
    ) -> bool:
        """
        트레일링 스탑을 업데이트합니다.
        
        Args:
            symbol: 종목 코드
            trailing_stop: 새 트레일링 스탑
            highest_price: 새 최고가
        
        Returns:
            bool: 업데이트 성공 여부
        """
        self._ensure_initialized()
        return self.position_repo.update_trailing_stop(
            symbol, trailing_stop, highest_price
        )
    
    def restore_positions_from_db(self) -> List[PositionRecord]:
        """
        DB에서 포지션을 복원합니다.
        
        ★ 프로그램 재시작 시 호출
        
        Returns:
            List[PositionRecord]: 복원된 포지션 목록
        """
        self._ensure_initialized()
        
        positions = self.position_repo.get_open_positions()
        
        if positions:
            logger.info(f"[TRADER] {len(positions)}개 포지션 복원됨")
            
            for pos in positions:
                logger.info(
                    f"  - {pos.symbol} @ {pos.entry_price:,.0f}원 x {pos.quantity}주, "
                    f"ATR={pos.atr_at_entry:,.0f} (고정)"
                )
        else:
            logger.info("[TRADER] 복원할 포지션 없음")
        
        return positions
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 계좌 스냅샷
    # ═══════════════════════════════════════════════════════════════════════════
    
    def save_account_snapshot(
        self,
        total_equity: float,
        cash: float,
        unrealized_pnl: float = 0.0,
        realized_pnl: float = 0.0
    ) -> None:
        """
        계좌 스냅샷을 저장합니다.
        
        Args:
            total_equity: 총 평가금액
            cash: 현금
            unrealized_pnl: 미실현 손익
            realized_pnl: 실현 손익
        """
        self._ensure_initialized()
        
        position_count = len(self.position_repo.get_open_positions())
        
        self.snapshot_repo.save(
            total_equity=total_equity,
            cash=cash,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=realized_pnl,
            position_count=position_count
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 일일 요약
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_daily_summary(self, trade_date: date = None) -> Dict[str, Any]:
        """
        일일 거래 요약을 반환합니다.
        
        Args:
            trade_date: 날짜 (None이면 오늘)
        
        Returns:
            Dict: 일일 요약
        """
        self._ensure_initialized()
        return self.trade_repo.get_daily_summary(trade_date or date.today())
    
    def send_daily_summary(self, trade_date: date = None) -> bool:
        """
        일일 요약을 텔레그램으로 전송합니다.
        
        Args:
            trade_date: 날짜 (None이면 오늘)
        
        Returns:
            bool: 전송 성공 여부
        """
        summary = self.get_daily_summary(trade_date)
        
        return self.telegram.notify_daily_summary(
            date=summary["date"],
            total_trades=summary["total_trades"],
            buy_count=summary["buy_count"],
            sell_count=summary["sell_count"],
            daily_pnl=summary["total_pnl"],
            daily_pnl_pct=0.0,  # 계산 필요
            win_rate=summary["win_rate"],
            max_profit=summary["max_profit"],
            max_loss=summary["max_loss"]
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 싱글톤 인스턴스
# ═══════════════════════════════════════════════════════════════════════════════

_db_trader: Optional[DatabaseTrader] = None


def get_db_trader(**kwargs) -> DatabaseTrader:
    """
    싱글톤 DatabaseTrader 인스턴스를 반환합니다.
    
    Returns:
        DatabaseTrader: 트레이더 인스턴스
    """
    global _db_trader
    
    if _db_trader is None:
        _db_trader = DatabaseTrader(**kwargs)
    
    return _db_trader

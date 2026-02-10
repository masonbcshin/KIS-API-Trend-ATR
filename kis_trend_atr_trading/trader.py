"""
═══════════════════════════════════════════════════════════════════════════════
KIS Trend-ATR Trading System - 주문 실행 모듈 (안전장치 포함)
═══════════════════════════════════════════════════════════════════════════════

이 모듈은 주문 실행을 담당하며, 실계좌 주문 사고를 방지하기 위한
2단계 안전장치를 구현합니다.

★★★ 핵심 안전장치 ★★★

┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│   [1단계] 설정 파일 확인 (config/prod.yaml)                                 │
│   ───────────────────────────────────────────                               │
│   allow_order 값이 true인지 확인합니다.                                     │
│   기본값은 false이므로, 명시적으로 true로 변경해야 합니다.                   │
│                                                                             │
│   [2단계] 사용자 콘솔 입력 확인                                             │
│   ────────────────────────────                                              │
│   주문 실행 직전에 "실계좌 주문을 실행하려면 YES를 입력하세요" 메시지가     │
│   출력되며, 사용자가 정확히 "YES"를 입력해야만 주문이 실행됩니다.           │
│   다른 입력이나 취소 시 즉시 예외가 발생합니다.                             │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘

★ 구조적 안전장치:
    1. DEV 환경: 안전장치 없이 즉시 주문 실행 (모의투자이므로 안전)
    2. PROD 환경: 반드시 2단계 안전장치 통과 필요
    3. 전략 코드(strategy/trend_atr.py)는 이 모듈의 존재를 모름

★ 사용 방법:
    from trader import Trader
    
    trader = Trader()
    
    # 매수 신호 발생 시 (안전장치 자동 적용)
    result = trader.buy(stock_code="005930", quantity=1, price=70000)
    
    # 매도 신호 발생 시 (안전장치 자동 적용)
    result = trader.sell(stock_code="005930", quantity=1)

═══════════════════════════════════════════════════════════════════════════════
"""

import sys
from typing import Dict, Any, Optional
from dataclasses import dataclass

from env import get_environment, is_prod, is_dev, Environment
from config_loader import get_config, is_order_allowed
from kis_client import get_kis_client, KISClient, KISClientError


# ═══════════════════════════════════════════════════════════════════════════════
# 예외 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class OrderNotAllowedError(Exception):
    """
    주문이 허용되지 않음 에러
    
    ★ 안전장치 1단계 실패 시 발생
    """
    pass


class OrderConfirmationError(Exception):
    """
    주문 확인 실패 에러
    
    ★ 안전장치 2단계 실패 시 발생
    """
    pass


class OrderExecutionError(Exception):
    """주문 실행 에러"""
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# 주문 결과 데이터 클래스
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class OrderResult:
    """주문 결과"""
    success: bool
    order_no: str = ""
    message: str = ""
    data: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.data is None:
            self.data = {}


# ═══════════════════════════════════════════════════════════════════════════════
# 안전장치 구현
# ═══════════════════════════════════════════════════════════════════════════════

class SafetyGuard:
    """
    실계좌 주문 안전장치
    
    ★ 2단계 안전장치를 구현합니다:
        1단계: 설정 파일 allow_order 확인
        2단계: 사용자 콘솔 입력 확인
    """
    
    # 2단계 확인 메시지
    CONFIRMATION_PROMPT = "실계좌 주문을 실행하려면 YES를 입력하세요: "
    REQUIRED_INPUT = "YES"
    
    def __init__(self):
        """안전장치 초기화"""
        self._is_confirmed_for_session: bool = False
    
    def check_order_allowed(self) -> bool:
        """
        ★ 안전장치 1단계: 설정 파일에서 allow_order=true 확인
        
        - DEV 환경: 항상 True (모의투자는 안전)
        - PROD 환경: config/prod.yaml의 allow_order 값 확인
        
        Returns:
            bool: 주문 허용 여부
        
        Raises:
            OrderNotAllowedError: PROD에서 allow_order=false인 경우
        """
        # DEV 환경은 항상 허용
        if is_dev():
            return True
        
        # PROD 환경: 설정 파일 확인
        if not is_order_allowed():
            raise OrderNotAllowedError(
                "\n"
                "═══════════════════════════════════════════════════════════════\n"
                "❌ 실계좌 주문이 허용되지 않았습니다.\n"
                "═══════════════════════════════════════════════════════════════\n"
                "\n"
                "★ 안전장치 1단계 실패\n"
                "\n"
                "config/prod.yaml 파일에서 allow_order 값이 false입니다.\n"
                "실계좌 주문을 허용하려면:\n"
                "\n"
                "  1. config/prod.yaml 파일을 엽니다.\n"
                "  2. order.allow_order 값을 true로 변경합니다.\n"
                "  3. 변경 전 충분한 모의투자 테스트를 완료했는지 확인합니다.\n"
                "\n"
                "═══════════════════════════════════════════════════════════════"
            )
        
        return True
    
    def confirm_order(self, stock_code: str, quantity: int, is_buy: bool) -> bool:
        """
        ★ 안전장치 2단계: 사용자 콘솔 입력 확인 (세션당 1회)
        
        - DEV 환경: 확인 없이 True
        - PROD 환경: 세션 최초 주문 시 "YES" 입력 필요
        """
        if is_dev():
            return True

        if self._is_confirmed_for_session:
            print(f"✅ 세션 확인 완료됨. 자동 주문을 진행합니다. ({stock_code})")
            return True

        order_type = "매수" if is_buy else "매도"
        
        print("\n")
        print("╔═══════════════════════════════════════════════════════════════╗")
        print("║  ⚠️⚠️⚠️  실계좌 주문 확인  ⚠️⚠️⚠️                              ║")
        print("╠═══════════════════════════════════════════════════════════════╣")
        print("║                                                               ║")
        print(f"║  주문 유형: {order_type:40}║")
        print(f"║  종목 코드: {stock_code:40}║")
        print(f"║  주문 수량: {quantity:40}║")
        print("║                                                               ║")
        print("║  ⚠️ 이 주문은 실제 계좌에서 실행됩니다!                       ║")
        print("║  ⚠️ 실제 돈이 거래됩니다!                                     ║")
        print("║                                                               ║")
        print("╚═══════════════════════════════════════════════════════════════╝")
        print("")

        try:
            user_input = input(self.CONFIRMATION_PROMPT).strip()
            
            if user_input != self.REQUIRED_INPUT:
                raise OrderConfirmationError(
                    "\n"
                    "═══════════════════════════════════════════════════════════════\n"
                    "❌ 주문이 취소되었습니다.\n"
                    "═══════════════════════════════════════════════════════════════\n"
                    "\n"
                    "★ 안전장치 2단계 실패\n"
                    "\n"
                    f"입력값: '{user_input}'\n"
                    f"필요한 입력값: '{self.REQUIRED_INPUT}'\n"
                    "\n"
                    "주문을 실행하려면 정확히 'YES'를 입력하세요.\n"
                    "\n"
                    "═══════════════════════════════════════════════════════════════"
                )

            print("✅ 최초 주문 확인 완료. 이후 이 세션의 모든 주문은 자동으로 실행됩니다.")
            self._is_confirmed_for_session = True
            return True
            
        except (EOFError, KeyboardInterrupt):
            raise OrderConfirmationError(
                "\n"
                "═══════════════════════════════════════════════════════════════\n"
                "❌ 주문이 취소되었습니다.\n"
                "═══════════════════════════════════════════════════════════════\n"
                "\n"
                "사용자에 의해 입력이 취소되었습니다.\n"
                "\n"
                "═══════════════════════════════════════════════════════════════"
            )
    
    def pass_all_checks(self, stock_code: str, quantity: int, is_buy: bool) -> bool:
        """
        모든 안전장치 검사를 수행합니다.
        
        Args:
            stock_code: 종목 코드
            quantity: 주문 수량
            is_buy: 매수 여부
        
        Returns:
            bool: 모든 검사 통과 여부
        
        Raises:
            OrderNotAllowedError: 1단계 실패
            OrderConfirmationError: 2단계 실패
        """
        # 1단계: 설정 파일 확인
        self.check_order_allowed()
        
        # 2단계: 사용자 확인
        self.confirm_order(stock_code, quantity, is_buy)
        
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# 트레이더 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class Trader:
    """
    주문 실행 클래스
    
    ★ 핵심 기능:
        - DEV 환경: 모의투자 주문 즉시 실행
        - PROD 환경: 2단계 안전장치 통과 후 실계좌 주문 실행
    
    ★ 전략 코드와의 분리:
        - 전략 코드(strategy/trend_atr.py)는 이 클래스를 직접 알지 못합니다.
        - main.py나 실행 엔진에서 전략 시그널을 받아 이 클래스를 호출합니다.
    """
    
    def __init__(self, client: KISClient = None):
        """
        트레이더 초기화
        
        Args:
            client: KIS API 클라이언트 (없으면 자동 생성)
        """
        self._client = client or get_kis_client()
        self._safety_guard = SafetyGuard()
        self._config = get_config()
        
        # 환경 정보 출력
        env = get_environment()
        env_label = "모의투자(DEV)" if env == Environment.DEV else "실계좌(PROD)"
        
        print(f"[Trader] 초기화 완료 - 환경: {env_label}")
        
        if is_prod():
            print("[Trader] ⚠️ 실계좌 환경입니다. 2단계 안전장치가 적용됩니다.")
            print("[Trader]    1단계: config/prod.yaml의 allow_order=true 확인")
            print("[Trader]    2단계: 주문 시 사용자 콘솔 입력 확인")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 주문 실행 메서드
    # ═══════════════════════════════════════════════════════════════════════════
    
    def buy(
        self,
        stock_code: str,
        quantity: int,
        price: int = 0,
        order_type: str = "01"
    ) -> OrderResult:
        """
        매수 주문을 실행합니다.
        
        ★ 실행 흐름:
            1. DEV 환경: 안전장치 없이 즉시 실행
            2. PROD 환경: 2단계 안전장치 통과 후 실행
        
        Args:
            stock_code: 종목 코드 (6자리)
            quantity: 주문 수량
            price: 주문 가격 (0이면 시장가)
            order_type: 주문 유형 (00: 지정가, 01: 시장가)
        
        Returns:
            OrderResult: 주문 결과
        
        Raises:
            OrderNotAllowedError: 안전장치 1단계 실패 (PROD에서 allow_order=false)
            OrderConfirmationError: 안전장치 2단계 실패 (사용자가 YES 미입력)
        """
        return self._execute_order(
            stock_code=stock_code,
            quantity=quantity,
            price=price,
            order_type=order_type,
            is_buy=True
        )
    
    def sell(
        self,
        stock_code: str,
        quantity: int,
        price: int = 0,
        order_type: str = "01"
    ) -> OrderResult:
        """
        매도 주문을 실행합니다.
        
        ★ 실행 흐름:
            1. DEV 환경: 안전장치 없이 즉시 실행
            2. PROD 환경: 2단계 안전장치 통과 후 실행
        
        Args:
            stock_code: 종목 코드 (6자리)
            quantity: 주문 수량
            price: 주문 가격 (0이면 시장가)
            order_type: 주문 유형 (00: 지정가, 01: 시장가)
        
        Returns:
            OrderResult: 주문 결과
        
        Raises:
            OrderNotAllowedError: 안전장치 1단계 실패
            OrderConfirmationError: 안전장치 2단계 실패
        """
        return self._execute_order(
            stock_code=stock_code,
            quantity=quantity,
            price=price,
            order_type=order_type,
            is_buy=False
        )
    
    def _execute_order(
        self,
        stock_code: str,
        quantity: int,
        price: int,
        order_type: str,
        is_buy: bool
    ) -> OrderResult:
        """
        주문 실행 내부 메서드
        
        ★ 핵심 로직:
            1. PROD 환경이면 2단계 안전장치 수행
            2. 안전장치 통과 후 KIS API 호출
        """
        order_side = "매수" if is_buy else "매도"
        
        try:
            # ════════════════════════════════════════════════════════════════
            # ★ 안전장치 실행 (PROD 환경에서만 작동)
            # ════════════════════════════════════════════════════════════════
            if is_prod():
                print(f"\n[Trader] {order_side} 주문 안전장치 검사 시작...")
                
                # 2단계 안전장치 모두 통과해야 함
                self._safety_guard.pass_all_checks(
                    stock_code=stock_code,
                    quantity=quantity,
                    is_buy=is_buy
                )
                
                print(f"[Trader] ✅ 안전장치 검사 통과 - 주문 실행 진행")
            
            # ════════════════════════════════════════════════════════════════
            # API 호출 실행
            # ════════════════════════════════════════════════════════════════
            if is_buy:
                result = self._client.place_buy_order(
                    stock_code=stock_code,
                    quantity=quantity,
                    price=price,
                    order_type=order_type
                )
            else:
                result = self._client.place_sell_order(
                    stock_code=stock_code,
                    quantity=quantity,
                    price=price,
                    order_type=order_type
                )
            
            return OrderResult(
                success=result.get("success", False),
                order_no=result.get("order_no", ""),
                message=result.get("message", ""),
                data=result.get("data", {})
            )
            
        except OrderNotAllowedError as e:
            # 안전장치 1단계 실패
            print(str(e))
            raise
            
        except OrderConfirmationError as e:
            # 안전장치 2단계 실패
            print(str(e))
            raise
            
        except KISClientError as e:
            # API 호출 에러
            print(f"[Trader] ❌ {order_side} 주문 API 에러: {e}")
            return OrderResult(
                success=False,
                message=str(e)
            )
            
        except Exception as e:
            # 기타 에러
            print(f"[Trader] ❌ {order_side} 주문 실행 에러: {e}")
            return OrderResult(
                success=False,
                message=str(e)
            )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 조회 메서드 (안전장치 불필요)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_current_price(self, stock_code: str) -> Dict[str, Any]:
        """
        현재가를 조회합니다.
        
        ★ 시세 조회는 안전장치가 필요 없습니다.
        
        Args:
            stock_code: 종목 코드
        
        Returns:
            Dict: 현재가 정보
        """
        return self._client.get_current_price(stock_code)
    
    def get_daily_ohlcv(
        self,
        stock_code: str,
        start_date: str = None,
        end_date: str = None
    ):
        """
        일봉 데이터를 조회합니다.
        
        ★ 시세 조회는 안전장치가 필요 없습니다.
        
        Args:
            stock_code: 종목 코드
            start_date: 시작일
            end_date: 종료일
        
        Returns:
            DataFrame: OHLCV 데이터
        """
        return self._client.get_daily_ohlcv(stock_code, start_date, end_date)
    
    def get_account_balance(self) -> Dict[str, Any]:
        """
        계좌 잔고를 조회합니다.
        
        ★ 잔고 조회는 안전장치가 필요 없습니다.
        
        Returns:
            Dict: 잔고 정보
        """
        return self._client.get_account_balance()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 유틸리티 메서드
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_environment_info(self) -> Dict[str, Any]:
        """
        현재 환경 정보를 반환합니다.
        
        Returns:
            Dict: 환경 정보
        """
        env = get_environment()
        
        return {
            "environment": env.value,
            "is_dev": is_dev(),
            "is_prod": is_prod(),
            "allow_order": is_order_allowed(),
            "api_base_url": self._config.api.base_url,
            "safety_guards": {
                "step1_config_check": "allow_order in config file",
                "step2_user_confirmation": "YES input required" if is_prod() else "not required"
            }
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 전역 트레이더 인스턴스
# ═══════════════════════════════════════════════════════════════════════════════

_trader: Optional[Trader] = None


def get_trader() -> Trader:
    """
    전역 트레이더 인스턴스를 반환합니다.
    
    Returns:
        Trader: 트레이더 인스턴스
    """
    global _trader
    if _trader is None:
        _trader = Trader()
    return _trader

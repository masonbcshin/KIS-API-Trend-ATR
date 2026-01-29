"""
KIS Trend-ATR Trading System - 한국투자증권 API 클라이언트

한국투자증권 Open API와 통신하는 클라이언트 클래스입니다.
모의투자 전용으로 설계되었습니다.

API 문서 참고: https://apiportal.koreainvestment.com/

⚠️ 주의: 이 모듈은 모의투자 전용입니다. 실계좌 사용을 금지합니다.
"""

import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, Any
import requests
import pandas as pd

from config import settings
from utils.logger import get_logger, TradeLogger

logger = get_logger("kis_api")
trade_logger = TradeLogger("kis_api")


class KISApiError(Exception):
    """KIS API 에러 클래스"""
    pass


class KISApi:
    """
    한국투자증권 Open API 클라이언트
    
    모의투자 환경에서 주식 데이터 조회 및 주문을 실행합니다.
    
    Attributes:
        base_url: API 기본 URL
        app_key: API 앱 키
        app_secret: API 앱 시크릿
        account_no: 계좌번호
        access_token: OAuth 액세스 토큰
        token_expires_at: 토큰 만료 시간
    """
    
    def __init__(
        self,
        app_key: str = None,
        app_secret: str = None,
        account_no: str = None,
        is_paper_trading: bool = True
    ):
        """
        KIS API 클라이언트 초기화
        
        Args:
            app_key: API 앱 키 (미입력 시 settings에서 로드)
            app_secret: API 앱 시크릿 (미입력 시 settings에서 로드)
            account_no: 계좌번호 (미입력 시 settings에서 로드)
            is_paper_trading: 모의투자 여부 (항상 True 유지)
        """
        # 실계좌 사용 방지
        if not is_paper_trading:
            raise KISApiError("⚠️ 실계좌 사용이 금지되어 있습니다. 모의투자만 가능합니다.")
        
        self.app_key = app_key or settings.APP_KEY
        self.app_secret = app_secret or settings.APP_SECRET
        self.account_no = account_no or settings.ACCOUNT_NO
        self.account_product_code = settings.ACCOUNT_PRODUCT_CODE
        self.base_url = settings.KIS_BASE_URL
        
        # 토큰 관리
        self.access_token: Optional[str] = None
        self.token_expires_at: Optional[datetime] = None
        
        # Rate Limit 관리
        self._last_api_call_time: float = 0.0
        
        logger.info(f"KIS API 클라이언트 초기화 완료 (모의투자: {is_paper_trading})")
    
    def _wait_for_rate_limit(self) -> None:
        """
        Rate Limit을 준수하기 위해 대기합니다.
        KIS API는 초당 20회 제한이 있습니다.
        """
        elapsed = time.time() - self._last_api_call_time
        if elapsed < settings.RATE_LIMIT_DELAY:
            time.sleep(settings.RATE_LIMIT_DELAY - elapsed)
        self._last_api_call_time = time.time()
    
    def _request_with_retry(
        self,
        method: str,
        url: str,
        headers: Dict,
        params: Dict = None,
        json_data: Dict = None,
        max_retries: int = None
    ) -> requests.Response:
        """
        재시도 로직이 포함된 HTTP 요청
        
        Args:
            method: HTTP 메서드 (GET, POST)
            url: 요청 URL
            headers: 요청 헤더
            params: URL 파라미터
            json_data: JSON 바디
            max_retries: 최대 재시도 횟수
        
        Returns:
            requests.Response: 응답 객체
        
        Raises:
            KISApiError: API 호출 실패 시
        """
        if max_retries is None:
            max_retries = settings.MAX_RETRIES
        
        last_exception = None
        
        for attempt in range(max_retries + 1):
            try:
                self._wait_for_rate_limit()
                
                start_time = time.time()
                
                if method.upper() == "GET":
                    response = requests.get(
                        url,
                        headers=headers,
                        params=params,
                        timeout=settings.API_TIMEOUT
                    )
                elif method.upper() == "POST":
                    response = requests.post(
                        url,
                        headers=headers,
                        json=json_data,
                        timeout=settings.API_TIMEOUT
                    )
                else:
                    raise KISApiError(f"지원하지 않는 HTTP 메서드: {method}")
                
                elapsed = time.time() - start_time
                trade_logger.log_api_call(url, response.ok, elapsed)
                
                # 성공적인 응답 확인
                if response.status_code == 200:
                    return response
                
                # 에러 응답 처리
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.warning(f"API 호출 실패 (시도 {attempt + 1}/{max_retries + 1}): {error_msg}")
                last_exception = KISApiError(error_msg)
                
            except requests.exceptions.Timeout as e:
                logger.warning(f"API 타임아웃 (시도 {attempt + 1}/{max_retries + 1}): {e}")
                last_exception = KISApiError(f"API 타임아웃: {e}")
                
            except requests.exceptions.RequestException as e:
                logger.warning(f"API 요청 실패 (시도 {attempt + 1}/{max_retries + 1}): {e}")
                last_exception = KISApiError(f"API 요청 실패: {e}")
            
            # 재시도 전 대기
            if attempt < max_retries:
                wait_time = settings.RETRY_DELAY * (2 ** attempt)  # 지수 백오프
                logger.info(f"{wait_time}초 후 재시도...")
                time.sleep(wait_time)
        
        raise last_exception
    
    def _get_auth_headers(self, tr_id: str) -> Dict:
        """
        인증 헤더를 생성합니다.
        
        Args:
            tr_id: 거래 ID (API 별로 다름)
        
        Returns:
            Dict: 인증 헤더
        """
        if not self.access_token:
            self.get_access_token()
        
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }
    
    # ════════════════════════════════════════════════════════════════
    # 인증 관련 API
    # ════════════════════════════════════════════════════════════════
    
    def get_access_token(self) -> str:
        """
        OAuth 액세스 토큰을 발급받습니다.
        
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        KIS API Endpoint: POST /oauth2/tokenP
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        
        Returns:
            str: 액세스 토큰
        
        Raises:
            KISApiError: 토큰 발급 실패 시
        """
        # 토큰이 유효한 경우 재사용
        if self.access_token and self.token_expires_at:
            if datetime.now() < self.token_expires_at - timedelta(minutes=10):
                return self.access_token
        
        url = f"{self.base_url}/oauth2/tokenP"
        headers = {"content-type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        
        logger.info("액세스 토큰 발급 요청...")
        
        response = self._request_with_retry("POST", url, headers, json_data=body)
        data = response.json()
        
        if "access_token" not in data:
            raise KISApiError(f"토큰 발급 실패: {data}")
        
        self.access_token = data["access_token"]
        
        # 토큰 만료 시간 설정 (KIS 토큰은 24시간 유효)
        expires_in = int(data.get("expires_in", 86400))
        self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)
        
        logger.info(f"액세스 토큰 발급 완료 (만료: {self.token_expires_at})")
        
        return self.access_token
    
    # ════════════════════════════════════════════════════════════════
    # 시세 조회 API
    # ════════════════════════════════════════════════════════════════
    
    def get_current_price(self, stock_code: str) -> Dict:
        """
        주식 현재가를 조회합니다.
        
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        KIS API Endpoint: GET /uapi/domestic-stock/v1/quotations/inquire-price
        TR_ID: FHKST01010100
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        
        Args:
            stock_code: 종목 코드 (6자리)
        
        Returns:
            Dict: 현재가 정보
                - current_price: 현재가
                - change_rate: 등락률
                - volume: 거래량
                - high_price: 고가
                - low_price: 저가
                - open_price: 시가
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        
        # 모의투자용 TR_ID
        tr_id = "FHKST01010100"
        headers = self._get_auth_headers(tr_id)
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",  # 주식
            "FID_INPUT_ISCD": stock_code
        }
        
        response = self._request_with_retry("GET", url, headers, params=params)
        data = response.json()
        
        if data.get("rt_cd") != "0":
            raise KISApiError(f"현재가 조회 실패: {data.get('msg1', 'Unknown error')}")
        
        output = data.get("output", {})
        
        return {
            "stock_code": stock_code,
            "current_price": float(output.get("stck_prpr", 0)),
            "change_rate": float(output.get("prdy_ctrt", 0)),
            "volume": int(output.get("acml_vol", 0)),
            "high_price": float(output.get("stck_hgpr", 0)),
            "low_price": float(output.get("stck_lwpr", 0)),
            "open_price": float(output.get("stck_oprc", 0)),
        }
    
    def get_daily_ohlcv(
        self,
        stock_code: str,
        start_date: str = None,
        end_date: str = None,
        period_type: str = "D"
    ) -> pd.DataFrame:
        """
        일봉/주봉/월봉 OHLCV 데이터를 조회합니다.
        
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        KIS API Endpoint: GET /uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice
        TR_ID: FHKST03010100
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        
        Args:
            stock_code: 종목 코드 (6자리)
            start_date: 조회 시작일 (YYYYMMDD, 미입력 시 100일 전)
            end_date: 조회 종료일 (YYYYMMDD, 미입력 시 오늘)
            period_type: 기간 타입 (D: 일봉, W: 주봉, M: 월봉)
        
        Returns:
            pd.DataFrame: OHLCV 데이터프레임
                - date: 날짜
                - open: 시가
                - high: 고가
                - low: 저가
                - close: 종가
                - volume: 거래량
        """
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=100)).strftime("%Y%m%d")
        
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        
        # 모의투자용 TR_ID
        tr_id = "FHKST03010100"
        headers = self._get_auth_headers(tr_id)
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": period_type,
            "FID_ORG_ADJ_PRC": "0",  # 수정주가 미반영
        }
        
        all_data = []
        
        # 페이징 처리 (최대 100개씩)
        while True:
            response = self._request_with_retry("GET", url, headers, params=params)
            data = response.json()
            
            if data.get("rt_cd") != "0":
                raise KISApiError(f"일봉 데이터 조회 실패: {data.get('msg1', 'Unknown error')}")
            
            output2 = data.get("output2", [])
            
            if not output2:
                break
            
            for item in output2:
                try:
                    row = {
                        "date": item.get("stck_bsop_date"),
                        "open": float(item.get("stck_oprc", 0)),
                        "high": float(item.get("stck_hgpr", 0)),
                        "low": float(item.get("stck_lwpr", 0)),
                        "close": float(item.get("stck_clpr", 0)),
                        "volume": int(item.get("acml_vol", 0)),
                    }
                    if row["date"] and row["close"] > 0:
                        all_data.append(row)
                except (ValueError, TypeError) as e:
                    logger.warning(f"데이터 파싱 오류: {e}")
                    continue
            
            # 데이터가 100개 미만이면 마지막 페이지
            if len(output2) < 100:
                break
            
            # 다음 페이지 조회를 위한 시작일 업데이트
            last_date = output2[-1].get("stck_bsop_date")
            if last_date:
                params["FID_INPUT_DATE_2"] = last_date
        
        if not all_data:
            logger.warning(f"조회된 데이터 없음: {stock_code}")
            return pd.DataFrame()
        
        # DataFrame 생성 및 정렬
        df = pd.DataFrame(all_data)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date", ascending=True).reset_index(drop=True)
        
        # 중복 제거
        df = df.drop_duplicates(subset=["date"], keep="last")
        
        logger.info(f"일봉 데이터 조회 완료: {stock_code}, {len(df)}개")
        
        return df
    
    # ════════════════════════════════════════════════════════════════
    # 주문 API (모의투자 전용)
    # ════════════════════════════════════════════════════════════════
    
    def place_buy_order(
        self,
        stock_code: str,
        quantity: int,
        price: int = 0,
        order_type: str = "00"
    ) -> Dict:
        """
        매수 주문을 실행합니다 (모의투자 전용).
        
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        KIS API Endpoint: POST /uapi/domestic-stock/v1/trading/order-cash
        TR_ID: VTTC0802U (모의투자 매수)
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        
        Args:
            stock_code: 종목 코드 (6자리)
            quantity: 주문 수량
            price: 주문 가격 (0이면 시장가)
            order_type: 주문 유형 (00: 지정가, 01: 시장가)
        
        Returns:
            Dict: 주문 결과
                - success: 주문 성공 여부
                - order_no: 주문 번호
                - message: 응답 메시지
        """
        return self._place_order(
            stock_code=stock_code,
            quantity=quantity,
            price=price,
            order_type=order_type,
            is_buy=True
        )
    
    def place_sell_order(
        self,
        stock_code: str,
        quantity: int,
        price: int = 0,
        order_type: str = "00"
    ) -> Dict:
        """
        매도 주문을 실행합니다 (모의투자 전용).
        
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        KIS API Endpoint: POST /uapi/domestic-stock/v1/trading/order-cash
        TR_ID: VTTC0801U (모의투자 매도)
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        
        Args:
            stock_code: 종목 코드 (6자리)
            quantity: 주문 수량
            price: 주문 가격 (0이면 시장가)
            order_type: 주문 유형 (00: 지정가, 01: 시장가)
        
        Returns:
            Dict: 주문 결과
                - success: 주문 성공 여부
                - order_no: 주문 번호
                - message: 응답 메시지
        """
        return self._place_order(
            stock_code=stock_code,
            quantity=quantity,
            price=price,
            order_type=order_type,
            is_buy=False
        )
    
    def _place_order(
        self,
        stock_code: str,
        quantity: int,
        price: int,
        order_type: str,
        is_buy: bool
    ) -> Dict:
        """
        주문 실행 내부 메서드 (모의투자 전용).
        
        Args:
            stock_code: 종목 코드
            quantity: 주문 수량
            price: 주문 가격
            order_type: 주문 유형
            is_buy: 매수 여부
        
        Returns:
            Dict: 주문 결과
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        
        # 모의투자 TR_ID
        # VTTC0802U: 모의투자 매수
        # VTTC0801U: 모의투자 매도
        tr_id = "VTTC0802U" if is_buy else "VTTC0801U"
        headers = self._get_auth_headers(tr_id)
        
        body = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_product_code,
            "PDNO": stock_code,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price) if price > 0 else "0",
        }
        
        order_side = "매수" if is_buy else "매도"
        logger.info(f"{order_side} 주문 요청: {stock_code}, {quantity}주, 가격: {price}")
        
        try:
            response = self._request_with_retry("POST", url, headers, json_data=body)
            data = response.json()
            
            success = data.get("rt_cd") == "0"
            order_no = data.get("output", {}).get("ODNO", "")
            message = data.get("msg1", "")
            
            if success:
                trade_logger.log_order(
                    order_type=order_side.upper(),
                    stock_code=stock_code,
                    quantity=quantity,
                    price=price,
                    order_no=order_no
                )
                logger.info(f"{order_side} 주문 성공: 주문번호 {order_no}")
            else:
                logger.error(f"{order_side} 주문 실패: {message}")
            
            return {
                "success": success,
                "order_no": order_no,
                "message": message,
                "data": data
            }
            
        except KISApiError as e:
            logger.error(f"{order_side} 주문 에러: {e}")
            return {
                "success": False,
                "order_no": "",
                "message": str(e),
                "data": {}
            }
    
    def get_order_status(self, order_no: str = None) -> Dict:
        """
        주문 체결 내역을 조회합니다 (모의투자 전용).
        
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        KIS API Endpoint: GET /uapi/domestic-stock/v1/trading/inquire-daily-ccld
        TR_ID: VTTC8001R (모의투자)
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        
        Args:
            order_no: 주문 번호 (미입력 시 당일 전체 조회)
        
        Returns:
            Dict: 주문 체결 내역
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        
        # 모의투자용 TR_ID
        tr_id = "VTTC8001R"
        headers = self._get_auth_headers(tr_id)
        
        today = datetime.now().strftime("%Y%m%d")
        
        params = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_product_code,
            "INQR_STRT_DT": today,
            "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00",  # 전체
            "INQR_DVSN": "00",  # 역순
            "PDNO": "",
            "CCLD_DVSN": "00",  # 전체
            "ORD_GNO_BRNO": "",
            "ODNO": order_no or "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        
        response = self._request_with_retry("GET", url, headers, params=params)
        data = response.json()
        
        if data.get("rt_cd") != "0":
            raise KISApiError(f"주문 조회 실패: {data.get('msg1', 'Unknown error')}")
        
        orders = []
        for item in data.get("output1", []):
            orders.append({
                "order_no": item.get("odno"),
                "stock_code": item.get("pdno"),
                "order_type": "매수" if item.get("sll_buy_dvsn_cd") == "02" else "매도",
                "order_qty": int(item.get("ord_qty", 0)),
                "exec_qty": int(item.get("tot_ccld_qty", 0)),
                "remain_qty": int(item.get("ord_qty", 0)) - int(item.get("tot_ccld_qty", 0)),
                "order_price": float(item.get("ord_unpr", 0)),
                "exec_price": float(item.get("avg_prvs", 0)),
                "status": "체결완료" if int(item.get("tot_ccld_qty", 0)) == int(item.get("ord_qty", 0)) else (
                    "부분체결" if int(item.get("tot_ccld_qty", 0)) > 0 else "미체결"
                ),
            })
        
        return {
            "success": True,
            "orders": orders,
            "total_count": len(orders)
        }
    
    def wait_for_execution(
        self,
        order_no: str,
        expected_qty: int,
        timeout_seconds: int = 30,
        check_interval: float = 2.0
    ) -> Dict:
        """
        주문 체결을 동기적으로 대기합니다.
        
        ★ 핵심 안전장치:
            - 체결 완료될 때까지 대기
            - 타임아웃 시 미체결 주문 취소
            - 부분체결 상황 명시적 처리
        
        Args:
            order_no: 주문 번호
            expected_qty: 예상 체결 수량
            timeout_seconds: 최대 대기 시간 (초)
            check_interval: 체결 확인 간격 (초)
        
        Returns:
            Dict: 체결 결과
                - success: 완전 체결 여부
                - exec_qty: 실제 체결 수량
                - exec_price: 평균 체결가
                - status: "FILLED" / "PARTIAL" / "TIMEOUT" / "CANCELLED"
                - message: 상세 메시지
        """
        start_time = time.time()
        last_exec_qty = 0
        
        logger.info(f"체결 대기 시작: 주문번호={order_no}, 예상수량={expected_qty}, 타임아웃={timeout_seconds}초")
        
        while time.time() - start_time < timeout_seconds:
            try:
                status_result = self.get_order_status(order_no)
                
                if not status_result.get("success") or not status_result.get("orders"):
                    time.sleep(check_interval)
                    continue
                
                order = status_result["orders"][0]
                exec_qty = order.get("exec_qty", 0)
                exec_price = order.get("exec_price", 0)
                remain_qty = order.get("remain_qty", expected_qty)
                
                # 완전 체결
                if exec_qty >= expected_qty:
                    logger.info(f"체결 완료: {exec_qty}주 @ {exec_price:,.0f}원")
                    return {
                        "success": True,
                        "exec_qty": exec_qty,
                        "exec_price": exec_price,
                        "status": "FILLED",
                        "message": f"완전 체결: {exec_qty}주 @ {exec_price:,.0f}원"
                    }
                
                # 부분 체결 진행 중
                if exec_qty > last_exec_qty:
                    logger.info(f"부분 체결 진행: {exec_qty}/{expected_qty}주")
                    last_exec_qty = exec_qty
                
                time.sleep(check_interval)
                
            except KISApiError as e:
                logger.warning(f"체결 확인 중 오류: {e}")
                time.sleep(check_interval)
        
        # 타임아웃 - 미체결분 취소 시도
        logger.warning(f"체결 타임아웃: {timeout_seconds}초 경과")
        
        # 최종 상태 확인
        try:
            final_status = self.get_order_status(order_no)
            if final_status.get("orders"):
                final_order = final_status["orders"][0]
                final_exec_qty = final_order.get("exec_qty", 0)
                final_exec_price = final_order.get("exec_price", 0)
                final_remain = final_order.get("remain_qty", 0)
                
                if final_exec_qty > 0:
                    # 부분 체결된 경우 - 미체결분 취소 시도
                    if final_remain > 0:
                        cancel_result = self.cancel_order(order_no)
                        logger.info(f"미체결분 취소 시도: {cancel_result}")
                    
                    return {
                        "success": False,
                        "exec_qty": final_exec_qty,
                        "exec_price": final_exec_price,
                        "status": "PARTIAL",
                        "message": f"부분 체결: {final_exec_qty}/{expected_qty}주, 미체결 취소 시도"
                    }
                else:
                    # 완전 미체결 - 주문 취소
                    cancel_result = self.cancel_order(order_no)
                    return {
                        "success": False,
                        "exec_qty": 0,
                        "exec_price": 0,
                        "status": "CANCELLED",
                        "message": f"미체결로 주문 취소됨: {cancel_result}"
                    }
        except Exception as e:
            logger.error(f"최종 상태 확인 실패: {e}")
        
        return {
            "success": False,
            "exec_qty": last_exec_qty,
            "exec_price": 0,
            "status": "TIMEOUT",
            "message": f"타임아웃 - 마지막 확인 체결수량: {last_exec_qty}주"
        }
    
    def cancel_order(self, order_no: str) -> Dict:
        """
        미체결 주문을 취소합니다 (모의투자 전용).
        
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        KIS API Endpoint: POST /uapi/domestic-stock/v1/trading/order-rvsecncl
        TR_ID: VTTC0803U (모의투자 주문취소)
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        
        Args:
            order_no: 취소할 주문 번호
        
        Returns:
            Dict: 취소 결과
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl"
        
        # 모의투자 취소 TR_ID
        tr_id = "VTTC0803U"
        headers = self._get_auth_headers(tr_id)
        
        body = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_product_code,
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO": order_no,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",  # 취소
            "ORD_QTY": "0",  # 전량 취소
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",  # 전량
        }
        
        logger.info(f"주문 취소 요청: 주문번호={order_no}")
        
        try:
            response = self._request_with_retry("POST", url, headers, json_data=body)
            data = response.json()
            
            success = data.get("rt_cd") == "0"
            message = data.get("msg1", "")
            
            if success:
                logger.info(f"주문 취소 성공: {order_no}")
            else:
                logger.warning(f"주문 취소 실패: {message}")
            
            return {
                "success": success,
                "order_no": order_no,
                "message": message
            }
            
        except KISApiError as e:
            logger.error(f"주문 취소 에러: {e}")
            return {
                "success": False,
                "order_no": order_no,
                "message": str(e)
            }
    
    def get_account_balance(self) -> Dict:
        """
        계좌 잔고를 조회합니다 (모의투자 전용).
        
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        KIS API Endpoint: GET /uapi/domestic-stock/v1/trading/inquire-balance
        TR_ID: VTTC8434R (모의투자)
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        
        Returns:
            Dict: 계좌 잔고 정보
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        
        # 모의투자용 TR_ID
        tr_id = "VTTC8434R"
        headers = self._get_auth_headers(tr_id)
        
        params = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_product_code,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        
        response = self._request_with_retry("GET", url, headers, params=params)
        data = response.json()
        
        if data.get("rt_cd") != "0":
            raise KISApiError(f"잔고 조회 실패: {data.get('msg1', 'Unknown error')}")
        
        # 보유 종목
        holdings = []
        for item in data.get("output1", []):
            if int(item.get("hldg_qty", 0)) > 0:
                holdings.append({
                    "stock_code": item.get("pdno"),
                    "stock_name": item.get("prdt_name"),
                    "quantity": int(item.get("hldg_qty", 0)),
                    "avg_price": float(item.get("pchs_avg_pric", 0)),
                    "current_price": float(item.get("prpr", 0)),
                    "eval_amount": float(item.get("evlu_amt", 0)),
                    "pnl_amount": float(item.get("evlu_pfls_amt", 0)),
                    "pnl_rate": float(item.get("evlu_pfls_rt", 0)),
                })
        
        # 계좌 요약
        output2 = data.get("output2", [{}])[0] if data.get("output2") else {}
        
        return {
            "success": True,
            "holdings": holdings,
            "total_eval": float(output2.get("tot_evlu_amt", 0)),
            "cash_balance": float(output2.get("dnca_tot_amt", 0)),
            "total_pnl": float(output2.get("evlu_pfls_smtl_amt", 0)),
        }

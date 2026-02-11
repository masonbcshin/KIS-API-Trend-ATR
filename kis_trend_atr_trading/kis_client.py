"""
═══════════════════════════════════════════════════════════════════════════════
KIS Trend-ATR Trading System - 한국투자증권 API 클라이언트
═══════════════════════════════════════════════════════════════════════════════

이 모듈은 한국투자증권 Open API와 통신하는 클라이언트입니다.
환경(DEV/PROD)에 따라 자동으로 올바른 URL과 TR_ID를 사용합니다.

★ 구조적 안전장치:
    1. 환경에 따라 자동으로 올바른 API URL 사용
    2. 환경에 따라 자동으로 올바른 TR_ID 사용
    3. 시세 조회 API는 환경과 무관하게 동일하게 동작
    4. 주문 API는 trader.py의 안전장치를 통해서만 호출

★ 이 모듈의 책임:
    - API 통신만 담당
    - 주문 허용 여부는 판단하지 않음 (trader.py가 담당)
    - 환경 판별은 env.py에 위임

═══════════════════════════════════════════════════════════════════════════════
"""

import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Any
import requests
import pandas as pd

from env import get_environment, is_prod, Environment
from config_loader import get_config
from utils.market_hours import KST


class KISClientError(Exception):
    """KIS API 클라이언트 에러"""
    pass


class KISClient:
    """
    한국투자증권 Open API 클라이언트
    
    ★ 환경별 자동 분기:
        - DEV: 모의투자 URL + 모의투자 TR_ID
        - PROD: 실계좌 URL + 실계좌 TR_ID
    
    ★ 이 클래스는 API 통신만 담당합니다.
        주문 허용 여부 판단은 trader.py의 책임입니다.
    """
    
    def __init__(self):
        """
        KIS API 클라이언트 초기화
        
        설정은 config_loader를 통해 자동으로 로드됩니다.
        """
        # 설정 로드
        self._config = get_config()
        
        # API 설정
        self.base_url = self._config.api.base_url
        self.timeout = self._config.api.timeout
        self.max_retries = self._config.api.max_retries
        self.retry_delay = self._config.api.retry_delay
        self.rate_limit_delay = self._config.api.rate_limit_delay
        
        # 인증 정보
        self.app_key = self._config.credentials.app_key
        self.app_secret = self._config.credentials.app_secret
        self.account_no = self._config.credentials.account_no
        self.account_product_code = self._config.credentials.account_product_code
        
        # TR_ID 설정
        self.tr_id = self._config.order.tr_id
        
        # 토큰 관리
        self.access_token: Optional[str] = None
        self.token_expires_at: Optional[datetime] = None
        
        # Rate Limit 관리
        self._last_api_call_time: float = 0.0
        
        # 환경 로깅
        env = get_environment()
        env_label = "모의투자(DEV)" if env == Environment.DEV else "실계좌(PROD)"
        print(f"[KISClient] 초기화 완료 - 환경: {env_label}")
        print(f"[KISClient] API URL: {self.base_url}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 내부 유틸리티 메서드
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _wait_for_rate_limit(self) -> None:
        """Rate Limit 준수를 위한 대기"""
        elapsed = time.time() - self._last_api_call_time
        if elapsed < self.rate_limit_delay:
            time.sleep(self.rate_limit_delay - elapsed)
        self._last_api_call_time = time.time()
    
    def _request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        params: Dict[str, Any] = None,
        json_data: Dict[str, Any] = None
    ) -> requests.Response:
        """
        HTTP 요청을 실행합니다 (재시도 로직 포함).
        
        Args:
            method: HTTP 메서드
            url: 요청 URL
            headers: 요청 헤더
            params: URL 파라미터
            json_data: JSON 바디
        
        Returns:
            Response: 응답 객체
        
        Raises:
            KISClientError: API 호출 실패 시
        """
        last_exception = None
        
        for attempt in range(self.max_retries + 1):
            try:
                self._wait_for_rate_limit()
                
                if method.upper() == "GET":
                    response = requests.get(
                        url, headers=headers, params=params, timeout=self.timeout
                    )
                elif method.upper() == "POST":
                    response = requests.post(
                        url, headers=headers, json=json_data, timeout=self.timeout
                    )
                else:
                    raise KISClientError(f"지원하지 않는 HTTP 메서드: {method}")
                
                if response.status_code == 200:
                    return response
                
                error_msg = f"HTTP {response.status_code}: {response.text}"
                last_exception = KISClientError(error_msg)
                
            except requests.exceptions.Timeout as e:
                last_exception = KISClientError(f"API 타임아웃: {e}")
            except requests.exceptions.RequestException as e:
                last_exception = KISClientError(f"API 요청 실패: {e}")
            
            if attempt < self.max_retries:
                wait_time = self.retry_delay * (2 ** attempt)
                time.sleep(wait_time)
        
        raise last_exception
    
    def _get_auth_headers(self, tr_id: str) -> Dict[str, str]:
        """
        인증 헤더를 생성합니다.
        
        Args:
            tr_id: 거래 ID
        
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
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 인증 API
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_access_token(self) -> str:
        """
        OAuth 액세스 토큰을 발급받습니다.
        
        Returns:
            str: 액세스 토큰
        
        Raises:
            KISClientError: 토큰 발급 실패 시
        """
        # 토큰이 유효한 경우 재사용
        if self.access_token and self.token_expires_at:
            if datetime.now(KST) < self.token_expires_at - timedelta(minutes=10):
                return self.access_token
        
        url = f"{self.base_url}/oauth2/tokenP"
        headers = {"content-type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        
        print("[KISClient] 액세스 토큰 발급 요청...")
        
        response = self._request("POST", url, headers, json_data=body)
        data = response.json()
        
        if "access_token" not in data:
            raise KISClientError(f"토큰 발급 실패: {data}")
        
        self.access_token = data["access_token"]
        expires_in = int(data.get("expires_in", 86400))
        self.token_expires_at = datetime.now(KST) + timedelta(seconds=expires_in)
        
        print(f"[KISClient] 토큰 발급 완료 (만료: {self.token_expires_at})")
        
        return self.access_token
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 시세 조회 API (환경 무관)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_current_price(self, stock_code: str) -> Dict[str, Any]:
        """
        주식 현재가를 조회합니다.
        
        ★ 시세 조회 API는 DEV/PROD 모두 동일한 TR_ID를 사용합니다.
        
        Args:
            stock_code: 종목 코드 (6자리)
        
        Returns:
            Dict: 현재가 정보
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        
        # 시세 조회용 TR_ID (DEV/PROD 동일)
        tr_id = "FHKST01010100"
        headers = self._get_auth_headers(tr_id)
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code
        }
        
        response = self._request("GET", url, headers, params=params)
        data = response.json()
        
        if data.get("rt_cd") != "0":
            raise KISClientError(f"현재가 조회 실패: {data.get('msg1', 'Unknown error')}")
        
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
        
        Args:
            stock_code: 종목 코드
            start_date: 시작일 (YYYYMMDD)
            end_date: 종료일 (YYYYMMDD)
            period_type: 기간 타입 (D/W/M)
        
        Returns:
            DataFrame: OHLCV 데이터
        """
        if end_date is None:
            end_date = datetime.now(KST).strftime("%Y%m%d")
        if start_date is None:
            start_date = (datetime.now(KST) - timedelta(days=100)).strftime("%Y%m%d")
        
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        
        # 시세 조회용 TR_ID (DEV/PROD 동일)
        tr_id = "FHKST03010100"
        headers = self._get_auth_headers(tr_id)
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": period_type,
            "FID_ORG_ADJ_PRC": "0",
        }
        
        all_data = []
        
        while True:
            response = self._request("GET", url, headers, params=params)
            data = response.json()
            
            if data.get("rt_cd") != "0":
                raise KISClientError(f"일봉 조회 실패: {data.get('msg1', 'Unknown error')}")
            
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
                except (ValueError, TypeError):
                    continue
            
            if len(output2) < 100:
                break
            
            last_date = output2[-1].get("stck_bsop_date")
            if last_date:
                params["FID_INPUT_DATE_2"] = last_date
        
        if not all_data:
            return pd.DataFrame()
        
        df = pd.DataFrame(all_data)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date", ascending=True).reset_index(drop=True)
        df = df.drop_duplicates(subset=["date"], keep="last")
        
        return df
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 주문 API (환경별 TR_ID 자동 선택)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def place_buy_order(
        self,
        stock_code: str,
        quantity: int,
        price: int = 0,
        order_type: str = "01"
    ) -> Dict[str, Any]:
        """
        매수 주문을 실행합니다.
        
        ★ 주의: 이 메서드는 trader.py를 통해서만 호출되어야 합니다.
            직접 호출 시 안전장치가 적용되지 않습니다.
        
        Args:
            stock_code: 종목 코드
            quantity: 수량
            price: 가격 (0이면 시장가)
            order_type: 주문 유형 (00: 지정가, 01: 시장가)
        
        Returns:
            Dict: 주문 결과
        """
        return self._place_order(stock_code, quantity, price, order_type, is_buy=True)
    
    def place_sell_order(
        self,
        stock_code: str,
        quantity: int,
        price: int = 0,
        order_type: str = "01"
    ) -> Dict[str, Any]:
        """
        매도 주문을 실행합니다.
        
        ★ 주의: 이 메서드는 trader.py를 통해서만 호출되어야 합니다.
            직접 호출 시 안전장치가 적용되지 않습니다.
        
        Args:
            stock_code: 종목 코드
            quantity: 수량
            price: 가격 (0이면 시장가)
            order_type: 주문 유형
        
        Returns:
            Dict: 주문 결과
        """
        return self._place_order(stock_code, quantity, price, order_type, is_buy=False)
    
    def _place_order(
        self,
        stock_code: str,
        quantity: int,
        price: int,
        order_type: str,
        is_buy: bool
    ) -> Dict[str, Any]:
        """
        주문 실행 내부 메서드
        
        ★ 환경별 자동 TR_ID 선택:
            - DEV: VTTC0802U(매수), VTTC0801U(매도)
            - PROD: TTTC0802U(매수), TTTC0801U(매도)
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        
        # ★ 환경별 TR_ID 자동 선택
        tr_id_key = "buy" if is_buy else "sell"
        tr_id = self.tr_id.get(tr_id_key, "")
        
        if not tr_id:
            raise KISClientError(f"TR_ID가 설정되지 않았습니다: {tr_id_key}")
        
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
        env_label = "실계좌" if is_prod() else "모의투자"
        print(f"[KISClient] {order_side} 주문 ({env_label}): {stock_code}, {quantity}주")
        
        try:
            response = self._request("POST", url, headers, json_data=body)
            data = response.json()
            
            success = data.get("rt_cd") == "0"
            order_no = data.get("output", {}).get("ODNO", "")
            message = data.get("msg1", "")
            
            if success:
                print(f"[KISClient] {order_side} 주문 성공: 주문번호 {order_no}")
            else:
                print(f"[KISClient] {order_side} 주문 실패: {message}")
            
            return {
                "success": success,
                "order_no": order_no,
                "message": message,
                "data": data
            }
            
        except KISClientError as e:
            print(f"[KISClient] {order_side} 주문 에러: {e}")
            return {
                "success": False,
                "order_no": "",
                "message": str(e),
                "data": {}
            }
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 계좌 조회 API
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_account_balance(self) -> Dict[str, Any]:
        """
        계좌 잔고를 조회합니다.
        
        Returns:
            Dict: 잔고 정보
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        
        # ★ 환경별 TR_ID 자동 선택
        tr_id = self.tr_id.get("balance", "")
        if not tr_id:
            raise KISClientError("잔고조회 TR_ID가 설정되지 않았습니다.")
        
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
        
        response = self._request("GET", url, headers, params=params)
        data = response.json()
        
        if data.get("rt_cd") != "0":
            raise KISClientError(f"잔고 조회 실패: {data.get('msg1', 'Unknown error')}")
        
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
        
        output2 = data.get("output2", [{}])[0] if data.get("output2") else {}
        
        return {
            "success": True,
            "holdings": holdings,
            "total_eval": float(output2.get("tot_evlu_amt", 0)),
            "cash_balance": float(output2.get("dnca_tot_amt", 0)),
            "total_pnl": float(output2.get("evlu_pfls_smtl_amt", 0)),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 전역 클라이언트 인스턴스
# ═══════════════════════════════════════════════════════════════════════════════

_client: Optional[KISClient] = None


def get_kis_client() -> KISClient:
    """
    전역 KIS 클라이언트를 반환합니다.
    
    Returns:
        KISClient: 클라이언트 인스턴스
    """
    global _client
    if _client is None:
        _client = KISClient()
    return _client

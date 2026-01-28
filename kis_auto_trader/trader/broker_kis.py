"""
trader/broker_kis.py - 한국투자증권 API 래퍼

실제 주문 실행 및 시세 조회 기능을 담당합니다.
모든 API 호출은 이 모듈을 통해서만 이루어집니다.
"""

import time
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

import requests
import pandas as pd
from requests.exceptions import RequestException, Timeout

from config.settings import get_settings


@dataclass
class OrderResult:
    """주문 결과 데이터 클래스"""
    success: bool
    order_no: str = ""
    message: str = ""
    executed_price: float = 0
    executed_qty: int = 0


@dataclass
class Position:
    """보유 포지션 데이터 클래스"""
    stock_code: str
    stock_name: str
    quantity: int
    avg_price: float
    current_price: float
    pnl: float
    pnl_pct: float


class KISBroker:
    """
    한국투자증권 API 클라이언트
    
    모의투자/실계좌 전환 가능하며,
    모든 주문 및 시세 조회 기능을 제공합니다.
    """
    
    def __init__(self):
        self.settings = get_settings()
        
        self.base_url = self.settings.KIS_BASE_URL
        self.app_key = self.settings.KIS_APP_KEY
        self.app_secret = self.settings.KIS_APP_SECRET
        self.account_no = self.settings.KIS_ACCOUNT_NO
        
        self._access_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        
        self.is_paper = self.settings.IS_PAPER_TRADING
        
        # 계좌번호 분리 (XXXXXXXX-XX 형식)
        if "-" in self.account_no:
            parts = self.account_no.split("-")
            self.cano = parts[0]
            self.acnt_prdt_cd = parts[1]
        else:
            self.cano = self.account_no[:8]
            self.acnt_prdt_cd = self.account_no[8:] or "01"
    
    # ═══════════════════════════════════════════════════════════════
    # 인증
    # ═══════════════════════════════════════════════════════════════
    
    def _get_access_token(self) -> str:
        """액세스 토큰 발급/갱신"""
        # 토큰이 유효하면 재사용
        if self._access_token and self._token_expires_at:
            if datetime.now() < self._token_expires_at - timedelta(minutes=5):
                return self._access_token
        
        url = f"{self.base_url}/oauth2/tokenP"
        headers = {"Content-Type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        
        response = requests.post(url, json=body, headers=headers, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        self._access_token = data["access_token"]
        
        # 토큰 만료 시간 설정 (약 24시간)
        expires_in = int(data.get("expires_in", 86400))
        self._token_expires_at = datetime.now() + timedelta(seconds=expires_in)
        
        return self._access_token
    
    def _get_headers(self, tr_id: str) -> Dict[str, str]:
        """API 요청 헤더 생성"""
        token = self._get_access_token()
        
        return {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id
        }
    
    def _get_hashkey(self, data: Dict) -> str:
        """해시키 생성 (주문 시 필요)"""
        url = f"{self.base_url}/uapi/hashkey"
        headers = {
            "Content-Type": "application/json",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        
        response = requests.post(url, json=data, headers=headers, timeout=10)
        return response.json().get("HASH", "")
    
    # ═══════════════════════════════════════════════════════════════
    # 시세 조회
    # ═══════════════════════════════════════════════════════════════
    
    def get_current_price(self, stock_code: str) -> Dict[str, Any]:
        """
        현재가 조회
        
        Args:
            stock_code: 종목코드 (6자리)
        
        Returns:
            Dict: 현재가 정보
        """
        # 모의투자: FHKST01010100 / 실전: FHKST01010100
        tr_id = "FHKST01010100"
        
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = self._get_headers(tr_id)
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        output = data.get("output", {})
        
        return {
            "stock_code": stock_code,
            "current_price": float(output.get("stck_prpr", 0)),
            "open_price": float(output.get("stck_oprc", 0)),
            "high_price": float(output.get("stck_hgpr", 0)),
            "low_price": float(output.get("stck_lwpr", 0)),
            "prev_close": float(output.get("stck_sdpr", 0)),
            "volume": int(output.get("acml_vol", 0)),
            "change_rate": float(output.get("prdy_ctrt", 0))
        }
    
    def get_daily_ohlcv(
        self,
        stock_code: str,
        period: str = "D",
        count: int = 100
    ) -> pd.DataFrame:
        """
        일봉 OHLCV 데이터 조회
        
        Args:
            stock_code: 종목코드
            period: 기간 (D: 일, W: 주, M: 월)
            count: 조회 개수
        
        Returns:
            pd.DataFrame: OHLCV 데이터
        """
        tr_id = "FHKST01010400"
        
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-price"
        headers = self._get_headers(tr_id)
        
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=count * 2)).strftime("%Y%m%d")
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_PERIOD_DIV_CODE": period,
            "FID_ORG_ADJ_PRC": "0",
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        output = data.get("output", [])
        
        if not output:
            return pd.DataFrame()
        
        records = []
        for item in output[:count]:
            records.append({
                "date": item.get("stck_bsop_date"),
                "open": float(item.get("stck_oprc", 0)),
                "high": float(item.get("stck_hgpr", 0)),
                "low": float(item.get("stck_lwpr", 0)),
                "close": float(item.get("stck_clpr", 0)),
                "volume": int(item.get("acml_vol", 0))
            })
        
        df = pd.DataFrame(records)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        
        return df
    
    # ═══════════════════════════════════════════════════════════════
    # 계좌 조회
    # ═══════════════════════════════════════════════════════════════
    
    def get_balance(self) -> Dict[str, Any]:
        """
        계좌 잔고 조회
        
        Returns:
            Dict: 잔고 정보 (예수금, 총평가, 보유종목 등)
        """
        # 모의투자: VTTC8434R / 실전: TTTC8434R
        tr_id = "VTTC8434R" if self.is_paper else "TTTC8434R"
        
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        headers = self._get_headers(tr_id)
        params = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": ""
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        output1 = data.get("output1", [])
        output2 = data.get("output2", [{}])[0]
        
        # 보유 종목 파싱
        holdings = []
        for item in output1:
            if int(item.get("hldg_qty", 0)) > 0:
                holdings.append(Position(
                    stock_code=item.get("pdno", ""),
                    stock_name=item.get("prdt_name", ""),
                    quantity=int(item.get("hldg_qty", 0)),
                    avg_price=float(item.get("pchs_avg_pric", 0)),
                    current_price=float(item.get("prpr", 0)),
                    pnl=float(item.get("evlu_pfls_amt", 0)),
                    pnl_pct=float(item.get("evlu_pfls_rt", 0))
                ))
        
        return {
            "cash_balance": float(output2.get("dnca_tot_amt", 0)),
            "total_eval": float(output2.get("tot_evlu_amt", 0)),
            "total_pnl": float(output2.get("evlu_pfls_smtl_amt", 0)),
            "holdings": holdings
        }
    
    def get_positions(self) -> List[Position]:
        """보유 포지션 목록 반환"""
        balance = self.get_balance()
        return balance.get("holdings", [])
    
    # ═══════════════════════════════════════════════════════════════
    # 주문 실행 (핵심!)
    # ═══════════════════════════════════════════════════════════════
    
    def place_buy_order(
        self,
        stock_code: str,
        quantity: int,
        price: int = 0,
        order_type: str = "01"
    ) -> OrderResult:
        """
        매수 주문
        
        Args:
            stock_code: 종목코드
            quantity: 주문 수량
            price: 주문 가격 (시장가일 경우 0)
            order_type: 주문 유형 (01: 시장가, 00: 지정가)
        
        Returns:
            OrderResult: 주문 결과
        """
        # 모의투자: VTTC0802U / 실전: TTTC0802U
        tr_id = "VTTC0802U" if self.is_paper else "TTTC0802U"
        
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        
        body = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "PDNO": stock_code,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price)
        }
        
        headers = self._get_headers(tr_id)
        headers["hashkey"] = self._get_hashkey(body)
        
        try:
            response = requests.post(url, json=body, headers=headers, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            if data.get("rt_cd") == "0":
                return OrderResult(
                    success=True,
                    order_no=data.get("output", {}).get("ODNO", ""),
                    message="매수 주문 접수 완료",
                    executed_qty=quantity
                )
            else:
                return OrderResult(
                    success=False,
                    message=data.get("msg1", "주문 실패")
                )
                
        except Exception as e:
            return OrderResult(
                success=False,
                message=str(e)
            )
    
    def place_sell_order(
        self,
        stock_code: str,
        quantity: int,
        price: int = 0,
        order_type: str = "01"
    ) -> OrderResult:
        """
        매도 주문
        
        Args:
            stock_code: 종목코드
            quantity: 주문 수량
            price: 주문 가격 (시장가일 경우 0)
            order_type: 주문 유형 (01: 시장가, 00: 지정가)
        
        Returns:
            OrderResult: 주문 결과
        """
        # 모의투자: VTTC0801U / 실전: TTTC0801U
        tr_id = "VTTC0801U" if self.is_paper else "TTTC0801U"
        
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        
        body = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "PDNO": stock_code,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price)
        }
        
        headers = self._get_headers(tr_id)
        headers["hashkey"] = self._get_hashkey(body)
        
        try:
            response = requests.post(url, json=body, headers=headers, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            if data.get("rt_cd") == "0":
                return OrderResult(
                    success=True,
                    order_no=data.get("output", {}).get("ODNO", ""),
                    message="매도 주문 접수 완료",
                    executed_qty=quantity
                )
            else:
                return OrderResult(
                    success=False,
                    message=data.get("msg1", "주문 실패")
                )
                
        except Exception as e:
            return OrderResult(
                success=False,
                message=str(e)
            )
    
    # ═══════════════════════════════════════════════════════════════
    # 유틸리티
    # ═══════════════════════════════════════════════════════════════
    
    def is_market_open(self) -> bool:
        """장 운영 시간 여부 확인"""
        now = datetime.now()
        
        # 주말 제외
        if now.weekday() >= 5:
            return False
        
        # 장 운영 시간 (09:00 ~ 15:30)
        market_start = now.replace(hour=9, minute=0, second=0, microsecond=0)
        market_end = now.replace(hour=15, minute=30, second=0, microsecond=0)
        
        return market_start <= now <= market_end
    
    def can_place_new_order(self) -> bool:
        """신규 주문 가능 시간 여부 (장 마감 전 진입 금지)"""
        now = datetime.now()
        settings = self.settings
        
        # 장 운영 시간 체크
        if not self.is_market_open():
            return False
        
        # 신규 진입 마감 시간 체크
        end_time = now.replace(
            hour=settings.strategy.trading_end_hour,
            minute=settings.strategy.trading_end_minute,
            second=0,
            microsecond=0
        )
        
        return now < end_time

"""
KIS Trend-ATR Trading System - 한국투자증권 API 클라이언트

한국투자증권 Open API와 통신하는 클라이언트 클래스입니다.
모의투자 전용으로 설계되었습니다.

API 문서 참고: https://apiportal.koreainvestment.com/

⚠️ 주의: 이 모듈은 모의투자 전용입니다. 실계좌 사용을 금지합니다.
"""

import json
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple, Any, List
import requests
import pandas as pd

from config import settings
from utils.logger import get_logger, TradeLogger
from utils.market_hours import KST

logger = get_logger("kis_api")
trade_logger = TradeLogger("kis_api")

DEFAULT_TOKEN_RETRY_DELAY_SECONDS = 61.0
DEFAULT_TOKEN_REFRESH_MARGIN_MINUTES = 10
DEFAULT_TOKEN_CACHE_FILE_NAME = "access_token_cache.json"


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
            is_paper_trading: 모의투자 여부
        """
        self.is_paper_trading = bool(is_paper_trading)
        self.app_key = app_key or settings.APP_KEY
        self.app_secret = app_secret or settings.APP_SECRET
        self.account_no = account_no or settings.ACCOUNT_NO
        self.account_product_code = settings.ACCOUNT_PRODUCT_CODE
        self.base_url = settings.KIS_BASE_URL
        
        # 토큰 관리
        self.access_token: Optional[str] = None
        self.token_expires_at: Optional[datetime] = None
        self._token_lock = threading.Lock()
        self._token_retry_delay = float(
            getattr(settings, "TOKEN_RETRY_DELAY_SECONDS", DEFAULT_TOKEN_RETRY_DELAY_SECONDS)
        )
        self._token_refresh_margin = timedelta(
            minutes=max(
                int(
                    getattr(
                        settings,
                        "TOKEN_REFRESH_MARGIN_MINUTES",
                        DEFAULT_TOKEN_REFRESH_MARGIN_MINUTES,
                    )
                ),
                1,
            )
        )
        self._token_prewarm_hour = int(getattr(settings, "TOKEN_PREWARM_HOUR", 8))
        self._token_prewarm_minute = int(getattr(settings, "TOKEN_PREWARM_MINUTE", 0))
        self._token_cache_file = self._build_token_cache_file_path()
        self._last_token_prewarm_date = None

        # Rate Limit 관리
        self._last_api_call_time: float = 0.0
        
        # 네트워크 상태 관리 (1분 이상 단절 시 거래 중단 판단)
        self._network_down_since: Optional[float] = None
        self._was_disconnected: bool = False
        
        logger.info(f"KIS API 클라이언트 초기화 완료 (모의투자: {self.is_paper_trading})")
        logger.info(
            "[KIS] api_mode=%s, order_tr_ids={buy:%s,sell:%s,status:%s,cancel:%s,balance:%s}",
            "PAPER" if self.is_paper_trading else "REAL",
            self._resolve_tr_id("order_buy"),
            self._resolve_tr_id("order_sell"),
            self._resolve_tr_id("order_status"),
            self._resolve_tr_id("order_cancel"),
            self._resolve_tr_id("balance"),
        )
        self._load_token_cache()

    def _resolve_tr_id(self, purpose: str) -> str:
        """
        API 목적별 TR ID를 모의/운영 모드에 맞게 반환합니다.
        """
        paper_map = {
            "order_buy": "VTTC0802U",
            "order_sell": "VTTC0801U",
            "order_status": "VTTC8001R",
            "order_cancel": "VTTC0803U",
            "balance": "VTTC8434R",
        }
        real_map = {
            "order_buy": "TTTC0802U",
            "order_sell": "TTTC0801U",
            "order_status": "TTTC8001R",
            "order_cancel": "TTTC0803U",
            "balance": "TTTC8434R",
        }
        table = paper_map if self.is_paper_trading else real_map
        tr_id = table.get(purpose, "")
        if not tr_id:
            raise KISApiError(f"지원하지 않는 TR ID 목적: {purpose}")
        return tr_id

    def _build_token_cache_file_path(self) -> Path:
        data_dir = Path(
            getattr(
                settings,
                "DATA_DIR",
                Path(__file__).resolve().parent.parent / "data",
            )
        )
        return data_dir / DEFAULT_TOKEN_CACHE_FILE_NAME

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=KST)
            return dt.astimezone(KST)
        except Exception:
            return None

    def _is_token_usable(self, now_kst: Optional[datetime] = None) -> bool:
        if not self.access_token or not self.token_expires_at:
            return False

        expires_at = self.token_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=KST)

        now = now_kst or datetime.now(KST)
        return now < (expires_at - self._token_refresh_margin)

    def _load_token_cache(self) -> bool:
        path = self._token_cache_file
        if not path.exists():
            return False

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            token = str(payload.get("access_token", "")).strip()
            expires_at = self._parse_datetime(payload.get("token_expires_at"))
            prewarm_date_raw = str(payload.get("last_prewarm_date", "")).strip()

            if token and expires_at:
                self.access_token = token
                self.token_expires_at = expires_at

            if prewarm_date_raw:
                try:
                    self._last_token_prewarm_date = datetime.strptime(
                        prewarm_date_raw, "%Y-%m-%d"
                    ).date()
                except Exception:
                    self._last_token_prewarm_date = None

            if self._is_token_usable():
                logger.info(f"[KIS] 토큰 캐시 재사용 (만료: {self.token_expires_at})")
                return True
            return bool(self.access_token and self.token_expires_at)
        except Exception as e:
            logger.warning(f"[KIS] 토큰 캐시 로드 실패: {e}")
            return False

    def _save_token_cache(self) -> None:
        try:
            if not self.access_token or not self.token_expires_at:
                return

            payload = {
                "access_token": self.access_token,
                "token_expires_at": self.token_expires_at.astimezone(KST).isoformat(),
                "updated_at": datetime.now(KST).isoformat(),
            }
            if self._last_token_prewarm_date:
                payload["last_prewarm_date"] = self._last_token_prewarm_date.isoformat()

            self._token_cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._token_cache_file.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self._token_cache_file)
        except Exception as e:
            logger.warning(f"[KIS] 토큰 캐시 저장 실패: {e}")
    
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
        max_retries: int = None,
        retry_delay: float = None,
        use_exponential_backoff: bool = True,
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
            retry_delay: 재시도 대기 시간(초). None이면 settings.RETRY_DELAY 사용
            use_exponential_backoff: 지수 백오프 사용 여부
        
        Returns:
            requests.Response: 응답 객체
        
        Raises:
            KISApiError: API 호출 실패 시
        """
        if max_retries is None:
            max_retries = min(int(settings.MAX_RETRIES), 3)
        else:
            max_retries = min(int(max_retries), 3)
        base_retry_delay = float(settings.RETRY_DELAY if retry_delay is None else retry_delay)
        
        last_exception = None
        did_auth_refresh = False
        
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
                    if self._network_down_since is not None:
                        down_seconds = time.time() - self._network_down_since
                        logger.warning(
                            f"네트워크 복구 감지: 단절 {down_seconds:.1f}초 후 정상화"
                        )
                    self._network_down_since = None
                    self._was_disconnected = False
                    return response
                
                if (
                    response.status_code == 401
                    and isinstance(headers, dict)
                    and "authorization" in headers
                    and "/oauth2/tokenP" not in url
                    and not did_auth_refresh
                ):
                    logger.warning("401 인증 오류 감지: 토큰 강제 갱신 후 재시도")
                    try:
                        self.get_access_token(force_refresh=True)
                        headers["authorization"] = f"Bearer {self.access_token}"
                        did_auth_refresh = True
                        continue
                    except Exception as refresh_error:
                        logger.warning(f"401 처리 중 토큰 갱신 실패: {refresh_error}")
                        did_auth_refresh = True

                # 에러 응답 처리
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.warning(f"API 호출 실패 (시도 {attempt + 1}/{max_retries + 1}): {error_msg}")
                last_exception = KISApiError(error_msg)
                
            except requests.exceptions.Timeout as e:
                logger.warning(f"API 타임아웃 (시도 {attempt + 1}/{max_retries + 1}): {e}")
                last_exception = KISApiError(f"API 타임아웃: {e}")
                self._mark_network_disconnected()
                
            except requests.exceptions.RequestException as e:
                logger.warning(f"API 요청 실패 (시도 {attempt + 1}/{max_retries + 1}): {e}")
                last_exception = KISApiError(f"API 요청 실패: {e}")
                self._mark_network_disconnected()
            
            # 재시도 전 대기
            if attempt < max_retries:
                if use_exponential_backoff:
                    wait_time = base_retry_delay * (2 ** attempt)
                else:
                    wait_time = base_retry_delay
                logger.info(f"{wait_time}초 후 재시도...")
                time.sleep(wait_time)
        
        raise last_exception

    def _mark_network_disconnected(self) -> None:
        """네트워크 단절 상태를 기록합니다."""
        if self._network_down_since is None:
            self._network_down_since = time.time()
            self._was_disconnected = True

    def is_network_disconnected_for(self, seconds: int = 60) -> bool:
        """지정 시간 이상 네트워크 단절 상태인지 확인합니다."""
        if self._network_down_since is None:
            return False
        return (time.time() - self._network_down_since) >= seconds
    
    def _get_auth_headers(self, tr_id: str) -> Dict:
        """
        인증 헤더를 생성합니다.
        
        Args:
            tr_id: 거래 ID (API 별로 다름)
        
        Returns:
            Dict: 인증 헤더
        """
        if not self._is_token_usable():
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
    
    def prewarm_access_token_if_due(self) -> bool:
        """
        장 시작 전 프리워밍 시각(기본 08:00 KST) 이후 하루 1회 토큰을 준비합니다.
        """
        now_kst = datetime.now(KST)
        prewarm_at = now_kst.replace(
            hour=self._token_prewarm_hour,
            minute=self._token_prewarm_minute,
            second=0,
            microsecond=0,
        )
        today = now_kst.date()

        if now_kst < prewarm_at:
            return False
        if self._last_token_prewarm_date == today:
            return False

        try:
            self.get_access_token()
            self._last_token_prewarm_date = today
            self._save_token_cache()
            logger.info(
                f"[KIS] 토큰 프리워밍 완료 (기준 시각: {self._token_prewarm_hour:02d}:{self._token_prewarm_minute:02d} KST)"
            )
            return True
        except Exception as e:
            logger.warning(f"[KIS] 토큰 프리워밍 실패: {e}")
            return False

    def get_access_token(self, force_refresh: bool = False) -> str:
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
        with self._token_lock:
            now_kst = datetime.now(KST)

            # 메모리 토큰이 유효하면 즉시 재사용
            if not force_refresh and self._is_token_usable(now_kst):
                return self.access_token

            # 재기동/멀티프로세스 대비: 파일 캐시에서 재로드 후 유효하면 재사용
            if not force_refresh:
                self._load_token_cache()
                if self._is_token_usable(now_kst):
                    return self.access_token

            url = f"{self.base_url}/oauth2/tokenP"
            headers = {"content-type": "application/json"}
            body = {
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret
            }
            
            logger.info("액세스 토큰 발급 요청...")
            
            response = self._request_with_retry(
                "POST",
                url,
                headers,
                json_data=body,
                max_retries=3,
                retry_delay=self._token_retry_delay,
                use_exponential_backoff=False,
            )
            data = response.json()
            
            if "access_token" not in data:
                raise KISApiError(f"토큰 발급 실패: {data}")
            
            self.access_token = data["access_token"]
            
            # 토큰 만료 시간 설정 (KIS 토큰은 24시간 유효)
            expires_in = int(data.get("expires_in", 86400))
            self.token_expires_at = datetime.now(KST) + timedelta(seconds=expires_in)
            self._save_token_cache()
            
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
            end_date = datetime.now(KST).strftime("%Y%m%d")
        if start_date is None:
            start_date = (datetime.now(KST) - timedelta(days=100)).strftime("%Y%m%d")
        
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
    # 시장 랭킹/유니버스 API
    # ════════════════════════════════════════════════════════════════

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            if isinstance(value, str):
                value = value.replace(",", "").strip()
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        try:
            if value is None:
                return default
            if isinstance(value, str):
                value = value.replace(",", "").strip()
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _first_present(item: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
        for key in keys:
            if key in item and item.get(key) not in (None, ""):
                return item.get(key)
        return default

    def get_market_top_by_trade_value(self, top_n: int = 200) -> List[Dict[str, Any]]:
        """
        거래량 순위 API(volume-rank)를 이용해 거래대금 상위 후보를 조회합니다.

        NOTE:
            - 일부 환경(특히 모의투자)에서는 미지원일 수 있습니다.
            - 호출 실패 시 KISApiError를 raise하며, 상위 로직에서 fallback 처리합니다.
        """
        limit = max(int(top_n), 1)
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/volume-rank"

        # 시세 조회 계열 TR_ID (운영/모의 환경에서 동일 키 사용, 미지원 시 서버 오류 응답)
        headers = self._get_auth_headers("FHPST01710000")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "000000",
            "FID_INPUT_PRICE_1": "0",
            "FID_INPUT_PRICE_2": "0",
            "FID_VOL_CNT": "0",
            "FID_INPUT_DATE_1": "",
        }

        response = self._request_with_retry("GET", url, headers, params=params)
        data = response.json()
        if data.get("rt_cd") != "0":
            raise KISApiError(f"거래량 순위 조회 실패: {data.get('msg1', 'Unknown error')}")

        output = data.get("output") or data.get("output1") or []
        rows: List[Dict[str, Any]] = []
        for item in output:
            code = str(
                self._first_present(
                    item,
                    ["mksc_shrn_iscd", "stck_shrn_iscd", "pdno", "iscd", "code"],
                    "",
                )
            )
            if not (len(code) == 6 and code.isdigit()):
                continue

            current_price = self._to_float(
                self._first_present(item, ["stck_prpr", "prpr", "current_price"], 0)
            )
            volume = self._to_float(
                self._first_present(item, ["acml_vol", "acml_voln", "volume"], 0)
            )
            trade_value = self._to_float(
                self._first_present(
                    item,
                    ["acml_tr_pbmn", "stck_acml_tr_pbmn", "trade_value"],
                    0,
                )
            )
            if trade_value <= 0 and current_price > 0 and volume > 0:
                trade_value = current_price * volume

            pct_from_open = self._to_float(
                self._first_present(item, ["prdy_ctrt", "change_rate"], 0)
            )
            market_cap = self._to_float(
                self._first_present(item, ["hts_avls", "market_cap"], 0)
            )

            rows.append(
                {
                    "code": code,
                    "trade_value": trade_value,
                    "current_price": current_price,
                    "volume": volume,
                    "market_cap": market_cap,
                    "is_suspended": False,
                    "is_management": False,
                    "pct_from_open": pct_from_open,
                }
            )

        rows.sort(key=lambda x: float(x.get("trade_value", 0.0)), reverse=True)
        return rows[:limit]

    def get_market_universe_codes(self, limit: int = 200) -> List[str]:
        """
        시장 후보 종목 코드를 반환합니다.
        우선순위:
            1) volume-rank API 기반 상위 코드
            2) 실패 시 빈 리스트 반환 (상위 호출부가 안전 fallback 수행)
        """
        try:
            rows = self.get_market_top_by_trade_value(top_n=limit)
            return [str(r["code"]) for r in rows if str(r.get("code", "")).isdigit()]
        except Exception as e:
            logger.warning(f"시장 후보군 조회 실패(volume-rank): {e}")
            return []
    
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
        
        tr_id = self._resolve_tr_id("order_buy" if is_buy else "order_sell")
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
            # 주문 API는 재시도 시 중복 주문 위험이 있어 무조건 1회 호출
            response = self._request_with_retry(
                "POST", url, headers, json_data=body, max_retries=0
            )
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
        
        tr_id = self._resolve_tr_id("order_status")
        headers = self._get_auth_headers(tr_id)
        
        today = datetime.now(KST).strftime("%Y%m%d")
        
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
        
        tr_id = self._resolve_tr_id("order_cancel")
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
        
        tr_id = self._resolve_tr_id("balance")
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

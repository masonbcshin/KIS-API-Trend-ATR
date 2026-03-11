"""
KIS Trend-ATR Trading System - 한국투자증권 API 클라이언트

한국투자증권 Open API와 통신하는 클라이언트 클래스입니다.
모의투자 전용으로 설계되었습니다.

API 문서 참고: https://apiportal.koreainvestment.com/

⚠️ 주의: 이 모듈은 모의투자 전용입니다. 실계좌 사용을 금지합니다.
"""

import json
import os
import time
import threading
from copy import deepcopy
from datetime import date as dt_date, datetime, timedelta
from decimal import Decimal, InvalidOperation
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
DEFAULT_TOKEN_REFRESH_MARGIN_MINUTES = 30
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

    _shared_balance_payload_lock = threading.Condition(threading.Lock())
    _shared_balance_payload_cache: Dict[str, Dict[str, Any]] = {}
    _shared_balance_payload_cache_ts: Dict[str, float] = {}
    _shared_balance_payload_inflight: set[str] = set()
    
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

        # 계좌 잔고 조회 단기 캐시 (다종목 초기화 시 과도한 연속 호출 완화)
        self._balance_cache: Optional[Dict[str, Any]] = None
        self._balance_cache_ts: float = 0.0
        self._balance_cache_ttl_sec: float = float(
            getattr(settings, "ACCOUNT_BALANCE_CACHE_TTL_SEC", 2.0)
        )
        self._balance_raw_cache: Optional[Dict[str, Any]] = None
        self._balance_raw_cache_ts: float = 0.0
        self._holdings_cache: Optional[List[Dict[str, Any]]] = None
        self._holdings_cache_ts: float = 0.0
        self._holdings_cache_ttl_sec: float = float(
            getattr(settings, "ACCOUNT_HOLDINGS_CACHE_TTL_SEC", self._balance_cache_ttl_sec)
        )
        
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
            "order_status": "VTTC0081R",
            "order_status_historical": "VTSC9215R",
            "order_cancel": "VTTC0803U",
            "balance": "VTTC8434R",
        }
        real_map = {
            "order_buy": "TTTC0802U",
            "order_sell": "TTTC0801U",
            "order_status": "TTTC0081R",
            "order_status_historical": "CTSC9215R",
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

    @staticmethod
    def _parse_numeric_float(value: Any, default: float = 0.0) -> float:
        """문자열/숫자 입력을 float로 안전 변환합니다."""
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        raw = str(value).strip().replace(",", "")
        if not raw:
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _parse_numeric_int(cls, value: Any, default: int = 0) -> int:
        """문자열/숫자 입력을 int로 안전 변환합니다."""
        if value is None:
            return default
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)

        raw = str(value).strip().replace(",", "")
        if not raw:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            try:
                return int(float(raw))
            except (TypeError, ValueError):
                return default

    @staticmethod
    def _format_order_query_date(value: Any) -> str:
        """
        주문/체결 조회용 날짜 인자를 YYYYMMDD 문자열로 정규화합니다.

        허용 입력:
            - datetime/date
            - "YYYYMMDD" / "YYYY-MM-DD" 문자열
            - None(호출 시점 KST 오늘)
        """
        if value in (None, ""):
            return datetime.now(KST).strftime("%Y%m%d")

        if isinstance(value, datetime):
            dt = value.astimezone(KST) if value.tzinfo else KST.localize(value)
            return dt.strftime("%Y%m%d")

        if isinstance(value, dt_date):
            return value.strftime("%Y%m%d")

        raw = str(value).strip()
        if not raw:
            return datetime.now(KST).strftime("%Y%m%d")

        compact = raw.replace("-", "")
        if len(compact) == 8 and compact.isdigit():
            return compact

        raise ValueError(f"지원하지 않는 날짜 형식: {value}")

    def _resolve_order_status_tr_id(self, start_date: str, end_date: str) -> str:
        """
        주문/체결 조회 TR_ID를 조회 기간에 맞춰 선택합니다.

        - 최근 3개월 이내: order_status (TTTC/VTTC0081R)
        - 3개월 이전 포함: order_status_historical (CTSC/VTSC9215R)
        """
        try:
            start_dt = datetime.strptime(str(start_date), "%Y%m%d").date()
            end_dt = datetime.strptime(str(end_date), "%Y%m%d").date()
            oldest = min(start_dt, end_dt)
            cutoff = datetime.now(KST).date() - timedelta(days=90)
            if oldest <= cutoff:
                return self._resolve_tr_id("order_status_historical")
        except Exception:
            # 날짜 파싱이 실패하면 기본(최근) TR로 폴백합니다.
            pass
        return self._resolve_tr_id("order_status")

    @staticmethod
    def _normalize_order_no(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""

        digits = "".join(ch for ch in raw if ch.isdigit())
        if not digits:
            return raw

        try:
            return str(int(digits))
        except (TypeError, ValueError):
            return digits.lstrip("0") or "0"

    @classmethod
    def _order_no_matches(cls, actual: Any, expected: Any) -> bool:
        if expected in (None, ""):
            return True

        actual_raw = str(actual or "").strip()
        expected_raw = str(expected or "").strip()
        if not actual_raw or not expected_raw:
            return False
        if actual_raw == expected_raw:
            return True

        actual_norm = cls._normalize_order_no(actual_raw)
        expected_norm = cls._normalize_order_no(expected_raw)
        return bool(actual_norm and expected_norm and actual_norm == expected_norm)

    @staticmethod
    def _parse_numeric_decimal(
        value: Any,
        default: Optional[Decimal] = Decimal("0"),
    ) -> Optional[Decimal]:
        """문자열/숫자 입력을 Decimal로 안전 변환합니다."""
        if value is None:
            return default
        if isinstance(value, Decimal):
            return value

        raw = str(value).strip().replace(",", "")
        if not raw:
            return default
        try:
            return Decimal(str(raw))
        except (InvalidOperation, TypeError):
            return default

    @staticmethod
    def _pick_first_value(source: Dict[str, Any], keys: List[str]) -> Any:
        for key in keys:
            if key in source:
                return source.get(key)
        return None

    @staticmethod
    def _resolve_first_list_path(
        payload: Dict[str, Any],
        candidate_paths: List[Tuple[str, ...]],
    ) -> Tuple[List[Dict[str, Any]], str]:
        def _walk(obj: Any, path: Tuple[str, ...]) -> Any:
            current = obj
            for key in path:
                if not isinstance(current, dict):
                    return None
                current = current.get(key)
            return current

        for path in candidate_paths:
            found = _walk(payload, path)
            if isinstance(found, list):
                return [row for row in found if isinstance(row, dict)], ".".join(path)

        # 후보 경로를 모두 시도한 뒤에만 완화 탐색을 허용합니다.
        if isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)], key
                if isinstance(value, dict):
                    for sub_key, sub_value in value.items():
                        if isinstance(sub_value, list):
                            path = f"{key}.{sub_key}"
                            return [row for row in sub_value if isinstance(row, dict)], path
        return [], ""

    @staticmethod
    def _resolve_first_dict_path(
        payload: Dict[str, Any],
        candidate_paths: List[Tuple[str, ...]],
    ) -> Tuple[Dict[str, Any], str]:
        def _walk(obj: Any, path: Tuple[str, ...]) -> Any:
            current = obj
            for key in path:
                if not isinstance(current, dict):
                    return None
                current = current.get(key)
            return current

        for path in candidate_paths:
            found = _walk(payload, path)
            if isinstance(found, dict):
                return found, ".".join(path)
            if isinstance(found, list):
                for idx, row in enumerate(found):
                    if isinstance(row, dict):
                        return row, f"{'.'.join(path)}[{idx}]"

        if isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(value, dict):
                    for sub_key, sub_value in value.items():
                        if isinstance(sub_value, dict):
                            return sub_value, f"{key}.{sub_key}"
                        if isinstance(sub_value, list):
                            for idx, row in enumerate(sub_value):
                                if isinstance(row, dict):
                                    return row, f"{key}.{sub_key}[{idx}]"
                    return value, key
                if isinstance(value, list):
                    for idx, row in enumerate(value):
                        if isinstance(row, dict):
                            return row, f"{key}[{idx}]"
        return {}, ""

    @staticmethod
    def _is_truthy(value: Any) -> bool:
        return str(value or "").strip().lower() in ("1", "true", "yes", "on", "y")

    @classmethod
    def _should_log_order_request(cls) -> bool:
        return cls._is_truthy(os.getenv("KIS_ORDER_API_DEBUG_REQUEST", "false")) or cls._is_truthy(
            os.getenv("KIS_API_DEBUG_REQUEST", "false")
        )

    @classmethod
    def _should_log_order_status_request(cls) -> bool:
        return cls._is_truthy(os.getenv("KIS_ORDER_STATUS_DEBUG_REQUEST", "false")) or cls._is_truthy(
            os.getenv("KIS_API_DEBUG_REQUEST", "false")
        )

    @classmethod
    def _should_log_order_response(cls) -> bool:
        return cls._is_truthy(os.getenv("KIS_ORDER_API_DEBUG_RESPONSE", "false")) or cls._is_truthy(
            os.getenv("KIS_API_DEBUG_RESPONSE", "false")
        )

    @classmethod
    def _should_log_order_status_response(cls) -> bool:
        return cls._is_truthy(os.getenv("KIS_ORDER_STATUS_DEBUG_RESPONSE", "false")) or cls._is_truthy(
            os.getenv("KIS_API_DEBUG_RESPONSE", "false")
        )

    @staticmethod
    def _api_request_log_max_len() -> int:
        raw = str(os.getenv("KIS_API_DEBUG_REQUEST_MAX_LEN", "12000")).strip()
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            return 12000
        return max(parsed, 1000)

    @staticmethod
    def _api_response_log_max_len() -> int:
        raw = str(os.getenv("KIS_API_DEBUG_RESPONSE_MAX_LEN", "12000")).strip()
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            return 12000
        return max(parsed, 1000)

    @staticmethod
    def _truncate_for_log(text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        omitted = len(text) - max_len
        return f"{text[:max_len]}... <truncated {omitted} chars>"

    @staticmethod
    def _safe_json_dumps(data: Any) -> str:
        try:
            return json.dumps(data, ensure_ascii=False, default=str)
        except Exception:
            return str(data)

    @staticmethod
    def _mask_sensitive_value(key: str, value: Any) -> Any:
        key_upper = str(key or "").upper()
        raw = str(value or "")
        if key_upper in ("CANO", "ACCOUNT_NO"):
            if len(raw) <= 4:
                return "***"
            return f"{raw[:4]}***"
        if key_upper in ("ACNT_PRDT_CD", "ACCOUNT_PRODUCT_CODE"):
            return "***"
        if any(token in key_upper for token in ("APPKEY", "APPSECRET", "TOKEN", "AUTH")):
            if not raw:
                return ""
            return "***"
        return value

    @classmethod
    def _sanitize_for_log(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {
                str(key): cls._sanitize_for_log(cls._mask_sensitive_value(str(key), value))
                for key, value in data.items()
            }
        if isinstance(data, list):
            return [cls._sanitize_for_log(item) for item in data]
        return data

    def _request_balance_payload(self) -> Dict[str, Any]:
        """잔고/보유현황 원본 payload를 조회합니다."""
        now_ts = time.time()
        if (
            self._balance_raw_cache is not None
            and (now_ts - self._balance_raw_cache_ts) < max(self._balance_cache_ttl_sec, 0.0)
        ):
            logger.info(
                "[KIS][BAL_RAW] 캐시 재사용: age=%.2fs",
                now_ts - self._balance_raw_cache_ts,
            )
            return deepcopy(self._balance_raw_cache)

        cache_key = self._balance_payload_cache_key()
        ttl_sec = max(self._balance_cache_ttl_sec, 0.0)
        waited_for_inflight = False
        while True:
            with self.__class__._shared_balance_payload_lock:
                shared_payload = self.__class__._shared_balance_payload_cache.get(cache_key)
                shared_ts = self.__class__._shared_balance_payload_cache_ts.get(cache_key, 0.0)
                shared_age_sec = max(now_ts - shared_ts, 0.0)
                if shared_payload is not None and shared_age_sec < ttl_sec:
                    self._balance_raw_cache = deepcopy(shared_payload)
                    self._balance_raw_cache_ts = shared_ts
                    logger.info(
                        "[KIS][BAL_RAW] %s: age=%.2fs key=%s",
                        "in-flight coalesced 재사용" if waited_for_inflight else "공유 캐시 재사용",
                        shared_age_sec,
                        cache_key,
                    )
                    return deepcopy(shared_payload)
                if cache_key not in self.__class__._shared_balance_payload_inflight:
                    self.__class__._shared_balance_payload_inflight.add(cache_key)
                    break
                waited_for_inflight = True
                self.__class__._shared_balance_payload_lock.wait(timeout=max(ttl_sec, 0.1))
                now_ts = time.time()

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"

        tr_id = self._resolve_tr_id("balance")
        if self._is_token_usable() or (self.access_token and self.token_expires_at is None):
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self.access_token}",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
                "tr_id": tr_id,
            }
        else:
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

        max_attempts = 3
        data: Dict[str, Any] = {}
        logger.info("[KIS][BAL_RAW] 실조회 수행: key=%s", cache_key)
        try:
            for attempt in range(1, max_attempts + 1):
                response = self._request_with_retry("GET", url, headers, params=params)
                data = response.json()
                rt_cd = str(data.get("rt_cd", ""))
                if rt_cd == "0":
                    fetched_at = time.time()
                    payload_copy = deepcopy(data)
                    self._balance_raw_cache = payload_copy
                    self._balance_raw_cache_ts = fetched_at
                    with self.__class__._shared_balance_payload_lock:
                        self.__class__._shared_balance_payload_cache[cache_key] = deepcopy(payload_copy)
                        self.__class__._shared_balance_payload_cache_ts[cache_key] = fetched_at
                    return data

                msg = str(data.get("msg1", "Unknown error"))
                should_retry_invalid_acno = (
                    "INVALID_CHECK_ACNO" in msg and attempt < max_attempts
                )
                if should_retry_invalid_acno:
                    logger.warning(
                        "[KIS][BAL] 계좌 검증 오류 재시도(%s/%s): %s",
                        attempt,
                        max_attempts,
                        msg,
                    )
                    time.sleep(0.3 * attempt)
                    continue

                raise KISApiError(f"잔고 조회 실패: {msg}")

            raise KISApiError(
                f"잔고 조회 실패: {str(data.get('msg1', 'Unknown error'))}"
            )
        finally:
            with self.__class__._shared_balance_payload_lock:
                self.__class__._shared_balance_payload_inflight.discard(cache_key)
                self.__class__._shared_balance_payload_lock.notify_all()

    def _balance_payload_cache_key(self) -> str:
        return ":".join(
            [
                "paper" if self.is_paper_trading else "real",
                str(self.account_no or "").strip(),
                str(self.account_product_code or "").strip(),
            ]
        )

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
            expires_at = self._parse_datetime(
                payload.get("token_expires_at") or payload.get("expire_at")
            )
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
                "expire_at": self.token_expires_at.astimezone(KST).isoformat(),
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
                    response.status_code in (401, 403)
                    and isinstance(headers, dict)
                    and "authorization" in headers
                    and "/oauth2/tokenP" not in url
                    and not did_auth_refresh
                ):
                    logger.warning(
                        "%s 인증 오류 감지: 토큰 강제 갱신 후 1회 재시도",
                        response.status_code,
                    )
                    try:
                        self.get_access_token(force_refresh=True)
                        headers["authorization"] = f"Bearer {self.access_token}"
                        did_auth_refresh = True
                        continue
                    except Exception as refresh_error:
                        logger.warning(
                            "%s 처리 중 토큰 갱신 실패: %s",
                            response.status_code,
                            refresh_error,
                        )
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

            if not self.app_key or not self.app_secret:
                raise KISApiError(
                    "KIS_APP_KEY/KIS_APP_SECRET가 설정되지 않아 토큰 발급을 중단합니다."
                )

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
                - stock_name: 종목명(제공 시)
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
        
        # SESSION FULL은 단기적으로 해소되는 경우가 있어 소폭 재시도합니다.
        session_full_retries = 2
        for attempt in range(session_full_retries + 1):
            response = self._request_with_retry("GET", url, headers, params=params)
            data = response.json()
            if data.get("rt_cd") == "0":
                break

            msg = str(data.get("msg1", "Unknown error"))
            if "SESSION FULL" in msg.upper() and attempt < session_full_retries:
                wait_sec = 0.4 * (attempt + 1)
                logger.warning(
                    "[KIS][PRICE] SESSION FULL - %.1fs 후 재시도 (%s/%s) stock=%s",
                    wait_sec,
                    attempt + 1,
                    session_full_retries,
                    stock_code,
                )
                time.sleep(wait_sec)
                continue
            raise KISApiError(f"현재가 조회 실패: {msg}")
        
        output = data.get("output", {})
        stock_name = (
            str(
                output.get("hts_kor_isnm")
                or output.get("prdt_name")
                or output.get("isnm_nm")
                or ""
            )
            .strip()
            or None
        )
        
        return {
            "stock_code": stock_code,
            "stock_name": stock_name,
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
    def _to_bool_flag(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return value != 0
        text = str(value).strip().upper()
        if text in {"", "N", "NO", "FALSE", "F", "0", "00", "000", "NONE", "NULL"}:
            return False
        if text in {"Y", "YES", "TRUE", "T", "1"}:
            return True
        if text.isdigit():
            return text not in {"0", "00", "000"}
        if "정지" in text or "SUSPEND" in text or "HALT" in text:
            return True
        if "관리" in text or "MANAGE" in text:
            return True
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
            stock_name = str(
                self._first_present(
                    item,
                    ["hts_kor_isnm", "prdt_name", "isnm_nm", "stck_shrn_iscd_nm", "stock_name"],
                    "",
                )
            ).strip()
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
            market_cap_raw = self._first_present(item, ["hts_avls", "market_cap"], None)
            market_cap = (
                self._to_float(market_cap_raw, 0.0)
                if market_cap_raw not in (None, "")
                else None
            )
            is_suspended = self._to_bool_flag(
                self._first_present(
                    item,
                    [
                        "is_suspended",
                        "suspended",
                        "trht_yn",
                        "halt_yn",
                        "trading_halt_yn",
                        "stck_stop_yn",
                    ],
                    False,
                ),
                False,
            )
            is_management = self._to_bool_flag(
                self._first_present(
                    item,
                    [
                        "is_management",
                        "management_yn",
                        "mang_issu_yn",
                        "mang_issu_cls_code",
                    ],
                    False,
                ),
                False,
            )

            rows.append(
                {
                    "code": code,
                    "stock_name": stock_name or None,
                    "trade_value": trade_value,
                    "current_price": current_price,
                    "volume": volume,
                    "market_cap": market_cap,
                    "is_suspended": is_suspended,
                    "is_management": is_management,
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
        if self._should_log_order_request():
            req_payload = {
                "endpoint": "/uapi/domestic-stock/v1/trading/order-cash",
                "tr_id": tr_id,
                "side": order_side,
                "is_paper_trading": bool(self.is_paper_trading),
                "body": self._sanitize_for_log(body),
            }
            logger.info(
                "[KIS][ORDER][REQ] %s",
                self._truncate_for_log(
                    self._safe_json_dumps(req_payload),
                    self._api_request_log_max_len(),
                ),
            )
        
        try:
            # 주문 API는 재시도 시 중복 주문 위험이 있어 무조건 1회 호출
            response = self._request_with_retry(
                "POST", url, headers, json_data=body, max_retries=0
            )
            data = response.json()
            if self._should_log_order_response():
                resp_payload = {
                    "endpoint": "/uapi/domestic-stock/v1/trading/order-cash",
                    "tr_id": tr_id,
                    "side": order_side,
                    "is_paper_trading": bool(self.is_paper_trading),
                    "response": self._sanitize_for_log(data),
                }
                logger.info(
                    "[KIS][ORDER][RESP] %s",
                    self._truncate_for_log(
                        self._safe_json_dumps(resp_payload),
                        self._api_response_log_max_len(),
                    ),
                )
            
            success = data.get("rt_cd") == "0"
            output_data = data.get("output") if isinstance(data.get("output"), dict) else {}
            order_no = str(output_data.get("ODNO") or "").strip()
            branch_no = str(
                output_data.get("KRX_FWDG_ORD_ORGNO")
                or output_data.get("ORD_GNO_BRNO")
                or ""
            ).strip()
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
                "branch_no": branch_no,
                "message": message,
                "data": data
            }
            
        except KISApiError as e:
            logger.error(f"{order_side} 주문 에러: {e}")
            return {
                "success": False,
                "order_no": "",
                "branch_no": "",
                "message": str(e),
                "data": {}
            }
    
    def get_order_status(
        self,
        order_no: str = None,
        trade_date: Any = None,
        end_date: Any = None,
        ord_gno_brno: Optional[str] = None,
    ) -> Dict:
        """
        주문 체결 내역을 조회합니다 (모의투자 전용).
        
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        KIS API Endpoint: GET /uapi/domestic-stock/v1/trading/inquire-daily-ccld
        TR_ID:
          - 최근 3개월: VTTC0081R(모의) / TTTC0081R(실전)
          - 3개월 이전: VTSC9215R(모의) / CTSC9215R(실전)
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        
        Args:
            order_no: 주문 번호 (미입력 시 당일 전체 조회)
            trade_date: 조회 시작일 (미입력 시 오늘)
            end_date: 조회 종료일 (미입력 시 trade_date와 동일)
            ord_gno_brno: 주문지점번호 (미입력 시 전체)
        
        Returns:
            Dict: 주문 체결 내역
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"

        start_date = self._format_order_query_date(trade_date)
        query_end_date = self._format_order_query_date(end_date) if end_date is not None else start_date
        tr_id = self._resolve_order_status_tr_id(start_date, query_end_date)
        headers = self._get_auth_headers(tr_id)

        params = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_product_code,
            "INQR_STRT_DT": start_date,
            "INQR_END_DT": query_end_date,
            "SLL_BUY_DVSN_CD": "00",  # 전체
            "INQR_DVSN": "00",  # 역순
            "PDNO": "",
            "CCLD_DVSN": "00",  # 전체
            "ORD_GNO_BRNO": str(ord_gno_brno or "").strip(),
            "ODNO": order_no or "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        if self._should_log_order_status_request():
            req_payload = {
                "endpoint": "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                "tr_id": tr_id,
                "is_paper_trading": bool(self.is_paper_trading),
                "params": self._sanitize_for_log(params),
            }
            logger.info(
                "[KIS][ORDER_STATUS][REQ] %s",
                self._truncate_for_log(
                    self._safe_json_dumps(req_payload),
                    self._api_request_log_max_len(),
                ),
            )
        
        response = self._request_with_retry("GET", url, headers, params=params)
        data = response.json()
        if self._should_log_order_status_response():
            resp_payload = {
                "endpoint": "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                "tr_id": tr_id,
                "is_paper_trading": bool(self.is_paper_trading),
                "response": self._sanitize_for_log(data),
            }
            logger.info(
                "[KIS][ORDER_STATUS][RESP] %s",
                self._truncate_for_log(
                    self._safe_json_dumps(resp_payload),
                    self._api_response_log_max_len(),
                ),
            )
        
        if data.get("rt_cd") != "0":
            raise KISApiError(f"주문 조회 실패: {data.get('msg1', 'Unknown error')}")
        
        requested_order_no = str(order_no or "").strip()
        candidate_paths: List[Tuple[str, ...]] = [
            ("output1",),
            ("output2",),
            ("output",),
            ("output", "output1"),
            ("output", "output2"),
        ]
        rows, resolved_path = self._resolve_first_list_path(data, candidate_paths)
        summary_row, summary_path = self._resolve_first_dict_path(
            data,
            [
                ("output2",),
                ("output", "output2"),
            ],
        )
        summary = {}
        if summary_row:
            summary = {
                "tot_ord_qty": self._parse_numeric_int(summary_row.get("tot_ord_qty"), 0),
                "tot_ccld_qty": self._parse_numeric_int(summary_row.get("tot_ccld_qty"), 0),
                "tot_ccld_amt": self._parse_numeric_float(summary_row.get("tot_ccld_amt"), 0.0),
                "pchs_avg_pric": self._parse_numeric_float(summary_row.get("pchs_avg_pric"), 0.0),
                "resolved_path": summary_path,
            }

        order_no_keys = ["odno", "ODNO", "order_no", "ord_no", "odno1", "ordn_no", "orgn_odno"]
        stock_code_keys = ["pdno", "PDNO", "stock_code", "symbol", "iscd", "mksc_shrn_iscd"]
        side_code_keys = ["sll_buy_dvsn_cd", "sll_buy_dvsn", "side", "order_type", "buy_sell_type"]
        order_qty_keys = ["ord_qty", "order_qty", "tot_ord_qty", "qty", "ord_qty1"]
        exec_qty_keys = ["tot_ccld_qty", "ccld_qty", "exec_qty", "filled_qty", "tot_ccld_qty1"]
        remain_qty_keys = ["remain_qty", "remaining_qty", "ord_remn_qty", "rmn_qty"]
        order_price_keys = ["ord_unpr", "order_price", "ord_price", "unpr"]
        exec_price_keys = ["avg_prvs", "avg_ccld_prc", "exec_price", "filled_price", "ccld_avg_pric"]
        order_date_keys = ["ord_dt", "order_date", "ord_date", "trad_dt"]
        order_time_keys = ["ord_tmd", "ord_tm", "order_time", "trad_tm", "ccld_tm"]
        exec_id_keys = ["exec_id", "ccld_no", "exec_no", "ord_seqno", "trad_no", "odno2"]

        orders = []
        for item in rows:
            if not isinstance(item, dict):
                continue

            row_order_no = str(self._pick_first_value(item, order_no_keys) or "").strip()
            if not row_order_no and requested_order_no:
                # 일부 응답 포맷은 주문번호 키가 비표준/누락일 수 있어 요청값으로 보정
                row_order_no = requested_order_no

            if requested_order_no and not self._order_no_matches(row_order_no, requested_order_no):
                continue

            order_date = str(self._pick_first_value(item, order_date_keys) or start_date).strip()
            order_time = str(self._pick_first_value(item, order_time_keys) or "").strip()
            executed_at_iso = None
            try:
                if len(order_date) == 8 and order_time:
                    clean_time = "".join(ch for ch in order_time if ch.isdigit())
                    if len(clean_time) >= 6:
                        dt = datetime.strptime(f"{order_date}{clean_time[:6]}", "%Y%m%d%H%M%S")
                        executed_at_iso = KST.localize(dt).isoformat()
            except Exception:
                executed_at_iso = None

            raw_exec_id = self._pick_first_value(item, exec_id_keys)
            exec_id = str(raw_exec_id).strip() if raw_exec_id not in (None, "") else None

            side_raw = str(self._pick_first_value(item, side_code_keys) or "").strip().upper()
            if side_raw in ("02", "BUY", "B", "매수"):
                side = "BUY"
            elif side_raw in ("01", "SELL", "S", "매도"):
                side = "SELL"
            else:
                side = "BUY" if "매수" in side_raw else "SELL"

            order_qty = self._parse_numeric_int(self._pick_first_value(item, order_qty_keys), 0)
            exec_qty = self._parse_numeric_int(self._pick_first_value(item, exec_qty_keys), 0)
            remain_qty_raw = self._pick_first_value(item, remain_qty_keys)
            remain_qty = self._parse_numeric_int(remain_qty_raw, max(order_qty - exec_qty, 0))

            stock_code = str(self._pick_first_value(item, stock_code_keys) or "").strip()
            order_price = self._parse_numeric_float(self._pick_first_value(item, order_price_keys), 0.0)
            exec_price = self._parse_numeric_float(self._pick_first_value(item, exec_price_keys), 0.0)

            orders.append({
                "order_no": row_order_no,
                "stock_code": stock_code,
                "order_type": "매수" if side == "BUY" else "매도",
                "side": side,
                "order_qty": order_qty,
                "exec_qty": exec_qty,
                "remain_qty": max(remain_qty, 0),
                "order_price": order_price,
                "exec_price": exec_price,
                "executed_at": executed_at_iso,
                "exec_id": exec_id,
                "status": "체결완료" if (order_qty > 0 and exec_qty >= order_qty) else (
                    "부분체결" if exec_qty > 0 else "미체결"
                ),
            })

        if requested_order_no and not orders:
            logger.warning(
                "[KIS][ORDER_STATUS] 주문번호 미매칭: requested=%s, path=%s, raw_rows=%s",
                requested_order_no,
                resolved_path or "N/A",
                len(rows),
            )
        
        return {
            "success": True,
            "orders": orders,
            "total_count": len(orders),
            "resolved_path": resolved_path,
            "summary": summary,
        }
    
    def wait_for_execution(
        self,
        order_no: str,
        expected_qty: int,
        timeout_seconds: int = 30,
        check_interval: float = 2.0,
        ord_gno_brno: Optional[str] = None,
        stock_code: Optional[str] = None,
        side: Optional[str] = None,
        holding_before_qty: Optional[int] = None,
        holding_before_avg_price: Optional[float] = None,
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
            ord_gno_brno: 주문지점번호
            stock_code: 체결 보정 대상 종목코드(선택)
            side: 주문 방향("BUY"/"SELL", 선택)
            holding_before_qty: 주문 전 보유수량(선택)
            holding_before_avg_price: 주문 전 평균단가(선택)
        
        Returns:
            Dict: 체결 결과
                - success: 완전 체결 여부
                - exec_qty: 실제 체결 수량
                - exec_price: 평균 체결가
                - status: "FILLED" / "PARTIAL" / "TIMEOUT" / "CANCELLED"
                - message: 상세 메시지
        """
        start_time = time.time()
        query_branch_no = str(ord_gno_brno or "").strip()
        last_exec_qty = 0
        poll_count = 0
        empty_result_polls = 0
        unmatched_result_polls = 0
        last_total_count = 0
        last_observed_order_nos: List[str] = []
        probe_symbol = str(stock_code or "").strip()
        probe_side = str(side or "").strip().upper()
        probe_before_qty = (
            self._parse_numeric_int(holding_before_qty, 0)
            if holding_before_qty is not None
            else None
        )
        probe_before_avg_price = self._parse_numeric_float(holding_before_avg_price, 0.0)
        baseline_tot_ccld_qty: Optional[int] = None
        baseline_tot_ccld_amt: Optional[float] = None

        def _target_orders(order_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            matches = [
                row
                for row in (order_rows or [])
                if self._order_no_matches(row.get("order_no"), order_no)
            ]
            return matches

        def _collect_fills(order_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            fills: List[Dict[str, Any]] = []
            for row in order_rows:
                fill_qty = int(row.get("exec_qty", 0) or 0)
                fill_price = float(row.get("exec_price", 0) or 0)
                if fill_qty <= 0 or fill_price <= 0:
                    continue
                fills.append(
                    {
                        "order_no": row.get("order_no") or order_no,
                        "exec_id": row.get("exec_id"),
                        "executed_at": row.get("executed_at") or datetime.now(KST).isoformat(),
                        "price": fill_price,
                        "qty": fill_qty,
                        "side": row.get("side", "BUY"),
                    }
                )
            return fills

        def _extract_summary_totals(status_payload: Dict[str, Any]) -> Optional[Tuple[int, float]]:
            if not isinstance(status_payload, dict):
                return None
            summary = status_payload.get("summary")
            if not isinstance(summary, dict):
                return None
            return (
                self._parse_numeric_int(summary.get("tot_ccld_qty"), 0),
                self._parse_numeric_float(summary.get("tot_ccld_amt"), 0.0),
            )

        def _lookup_holding_snapshot(target_symbol: str) -> Optional[Dict[str, float]]:
            if not target_symbol:
                return None
            try:
                holdings_rows = self.get_holdings()
            except Exception as e:
                logger.debug(
                    "[KIS][ORDER_STATUS] holdings probe 실패: symbol=%s, err=%s",
                    target_symbol,
                    e,
                )
                return None

            for row in holdings_rows or []:
                if not isinstance(row, dict):
                    continue
                symbol = str(
                    row.get("stock_code")
                    or row.get("pdno")
                    or row.get("symbol")
                    or ""
                ).strip()
                if symbol != target_symbol:
                    continue
                qty = self._parse_numeric_int(
                    row.get("qty")
                    or row.get("hldg_qty")
                    or row.get("quantity"),
                    0,
                )
                avg_price = self._parse_numeric_float(
                    row.get("avg_price")
                    or row.get("pchs_avg_pric"),
                    0.0,
                )
                return {"qty": max(qty, 0), "avg_price": max(avg_price, 0.0)}
            return {"qty": 0, "avg_price": 0.0}

        def _infer_execution_from_holdings(status_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            # PAPER/REAL 공통: 체결조회 지연 시 보유수량 변동으로 보정합니다.
            if not probe_symbol or probe_side not in ("BUY", "SELL"):
                return None
            if probe_before_qty is None:
                return None

            after_snapshot = _lookup_holding_snapshot(probe_symbol)
            if after_snapshot is None:
                return None

            before_qty = max(int(probe_before_qty), 0)
            after_qty = self._parse_numeric_int(after_snapshot.get("qty"), 0)
            if probe_side == "BUY":
                delta_qty = max(after_qty - before_qty, 0)
            else:
                delta_qty = max(before_qty - after_qty, 0)
            if delta_qty <= 0:
                return None

            expected = max(int(expected_qty), 0)
            exec_qty = min(delta_qty, expected) if expected > 0 else delta_qty
            if exec_qty <= 0:
                return None

            exec_price = 0.0
            if probe_side == "BUY":
                exec_price = self._parse_numeric_float(after_snapshot.get("avg_price"), 0.0)
                if exec_price <= 0:
                    exec_price = probe_before_avg_price
            else:
                totals = _extract_summary_totals(status_payload)
                if (
                    totals
                    and baseline_tot_ccld_qty is not None
                    and baseline_tot_ccld_amt is not None
                ):
                    now_qty, now_amt = totals
                    delta_total_qty = max(now_qty - baseline_tot_ccld_qty, 0)
                    delta_total_amt = max(now_amt - baseline_tot_ccld_amt, 0.0)
                    if delta_total_qty > 0 and delta_total_amt > 0:
                        exec_price = delta_total_amt / float(delta_total_qty)

            if exec_price <= 0:
                return None

            is_filled = exec_qty >= expected if expected > 0 else True
            status = "FILLED" if is_filled else "PARTIAL"
            logger.warning(
                "[KIS][ORDER_STATUS] holdings 기반 %s 보정: order_no=%s, symbol=%s, before=%s, after=%s, exec_qty=%s, exec_price=%.4f",
                probe_side,
                order_no,
                probe_symbol,
                before_qty,
                after_qty,
                exec_qty,
                exec_price,
            )
            return {
                "success": is_filled,
                "exec_qty": exec_qty,
                "exec_price": exec_price,
                "status": status,
                "message": (
                    f"체결조회 미응답 - 보유수량 변동으로 {status} 보정: "
                    f"{before_qty}→{after_qty} (Δ{delta_qty})"
                ),
                "fills": [
                    {
                        "order_no": order_no,
                        "exec_id": None,
                        "executed_at": datetime.now(KST).isoformat(),
                        "price": exec_price,
                        "qty": exec_qty,
                        "side": probe_side,
                    }
                ],
            }
        
        logger.info(
            "체결 대기 시작: 주문번호=%s, 주문지점=%s, 예상수량=%s, 타임아웃=%s초",
            order_no,
            query_branch_no or "-",
            expected_qty,
            timeout_seconds,
        )
        
        while time.time() - start_time < timeout_seconds:
            try:
                status_result = self.get_order_status(
                    order_no,
                    ord_gno_brno=query_branch_no or None,
                )
                poll_count += 1
                summary_totals = _extract_summary_totals(status_result)
                if summary_totals and baseline_tot_ccld_qty is None:
                    baseline_tot_ccld_qty, baseline_tot_ccld_amt = summary_totals
                all_rows = status_result.get("orders") or []
                last_total_count = int(
                    status_result.get("total_count", len(all_rows)) or len(all_rows)
                )
                
                if not status_result.get("success") or not all_rows:
                    empty_result_polls += 1
                    inferred = _infer_execution_from_holdings(status_result)
                    if inferred is not None:
                        return inferred
                    time.sleep(check_interval)
                    continue

                matched_orders = _target_orders(all_rows)
                if not matched_orders:
                    unmatched_result_polls += 1
                    last_observed_order_nos = [
                        str(row.get("order_no") or "").strip()
                        for row in all_rows[:5]
                    ]
                    inferred = _infer_execution_from_holdings(status_result)
                    if inferred is not None:
                        return inferred
                    time.sleep(check_interval)
                    continue

                order = max(
                    matched_orders,
                    key=lambda row: self._parse_numeric_int(row.get("exec_qty"), 0),
                )
                exec_qty = order.get("exec_qty", 0)
                exec_price = order.get("exec_price", 0)
                remain_qty = order.get("remain_qty", expected_qty)
                
                # 완전 체결
                if exec_qty >= expected_qty:
                    logger.info(f"체결 완료: {exec_qty}주 @ {exec_price:,.0f}원")
                    fills = _collect_fills(matched_orders)
                    if not fills and exec_qty > 0 and exec_price > 0:
                        fills = [
                            {
                                "order_no": order_no,
                                "exec_id": None,
                                "executed_at": datetime.now(KST).isoformat(),
                                "price": exec_price,
                                "qty": exec_qty,
                                "side": order.get("side", "BUY"),
                            }
                        ]
                    return {
                        "success": True,
                        "exec_qty": exec_qty,
                        "exec_price": exec_price,
                        "status": "FILLED",
                        "message": f"완전 체결: {exec_qty}주 @ {exec_price:,.0f}원",
                        "fills": fills,
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
        logger.warning(
            "체결 타임아웃: %s초 경과 (order_no=%s, polls=%s, empty_polls=%s, unmatched_polls=%s, last_total=%s, sample_order_nos=%s)",
            timeout_seconds,
            order_no,
            poll_count,
            empty_result_polls,
            unmatched_result_polls,
            last_total_count,
            last_observed_order_nos,
        )
        
        # 최종 상태 확인
        try:
            final_status = self.get_order_status(
                order_no,
                ord_gno_brno=query_branch_no or None,
            )
            final_totals = _extract_summary_totals(final_status)
            if final_totals and baseline_tot_ccld_qty is None:
                baseline_tot_ccld_qty, baseline_tot_ccld_amt = final_totals
            inference_status = final_status
            final_orders = _target_orders(final_status.get("orders", []))
            if not final_orders:
                fallback_status = self.get_order_status(
                    None,
                    ord_gno_brno=query_branch_no or None,
                )
                inference_status = fallback_status
                final_orders = _target_orders(fallback_status.get("orders", []))
            if not final_orders:
                today = datetime.now(KST).date()
                prev_day = today - timedelta(days=1)
                window_status = self.get_order_status(
                    order_no=None,
                    trade_date=prev_day,
                    end_date=today,
                    ord_gno_brno=query_branch_no or None,
                )
                inference_status = window_status
                final_orders = _target_orders(window_status.get("orders", []))
            if not final_orders and query_branch_no:
                # 주문지점번호가 맞지 않는 경우를 대비해 마지막으로 지점번호 없이 재조회
                unscoped_status = self.get_order_status(order_no)
                inference_status = unscoped_status
                final_orders = _target_orders(unscoped_status.get("orders", []))

            if not final_orders:
                inferred = _infer_execution_from_holdings(inference_status)
                if inferred is not None:
                    return inferred

            if final_orders:
                final_order = max(
                    final_orders,
                    key=lambda row: self._parse_numeric_int(row.get("exec_qty"), 0),
                )
                final_exec_qty = final_order.get("exec_qty", 0)
                final_exec_price = final_order.get("exec_price", 0)
                final_remain = final_order.get("remain_qty", 0)
                
                if final_exec_qty >= expected_qty:
                    fills = _collect_fills(final_orders)
                    if not fills and final_exec_qty > 0 and final_exec_price > 0:
                        fills = [
                            {
                                "order_no": order_no,
                                "exec_id": None,
                                "executed_at": datetime.now(KST).isoformat(),
                                "price": final_exec_price,
                                "qty": final_exec_qty,
                                "side": final_order.get("side", "BUY"),
                            }
                        ]
                    return {
                        "success": True,
                        "exec_qty": final_exec_qty,
                        "exec_price": final_exec_price,
                        "status": "FILLED",
                        "message": f"최종 확인 완전 체결: {final_exec_qty}주 @ {final_exec_price:,.0f}원",
                        "fills": fills,
                    }
                elif final_exec_qty > 0:
                    # 부분 체결된 경우 - 미체결분 취소 시도
                    if final_remain > 0:
                        cancel_result = self.cancel_order(order_no)
                        logger.info(f"미체결분 취소 시도: {cancel_result}")
                    fills = _collect_fills(final_orders)
                    if not fills and final_exec_qty > 0 and final_exec_price > 0:
                        fills = [
                            {
                                "order_no": order_no,
                                "exec_id": None,
                                "executed_at": datetime.now(KST).isoformat(),
                                "price": final_exec_price,
                                "qty": final_exec_qty,
                                "side": final_order.get("side", "BUY"),
                            }
                        ]
                    return {
                        "success": False,
                        "exec_qty": final_exec_qty,
                        "exec_price": final_exec_price,
                        "status": "PARTIAL",
                        "message": f"부분 체결: {final_exec_qty}/{expected_qty}주, 미체결 취소 시도",
                        "fills": fills,
                    }
                else:
                    # 완전 미체결 - 주문 취소
                    cancel_result = self.cancel_order(order_no)
                    return {
                        "success": False,
                        "exec_qty": 0,
                        "exec_price": 0,
                        "status": "CANCELLED",
                        "message": f"미체결로 주문 취소됨: {cancel_result}",
                        "fills": [],
                    }
        except Exception as e:
            logger.error(f"최종 상태 확인 실패: {e}")
        
        return {
            "success": False,
            "exec_qty": last_exec_qty,
            "exec_price": 0,
            "status": "TIMEOUT",
            "message": f"타임아웃 - 마지막 확인 체결수량: {last_exec_qty}주",
            "fills": [],
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
        now_ts = time.time()
        if (
            self._balance_cache is not None
            and (now_ts - self._balance_cache_ts) < max(self._balance_cache_ttl_sec, 0.0)
        ):
            logger.info(
                "[KIS][BAL] 캐시 재사용: age=%.2fs",
                now_ts - self._balance_cache_ts,
            )
            return deepcopy(self._balance_cache)
        data = self._request_balance_payload()
        
        # 보유 종목
        holdings = []
        for item in data.get("output1", []):
            holding_qty = self._parse_numeric_int(item.get("hldg_qty"), 0)
            sellable_qty = self._parse_numeric_int(item.get("ord_psbl_qty"), holding_qty)
            # 모의투자 환경에서 hldg_qty가 축소 보고되는 사례가 있어 주문가능수량을 보정값으로 사용
            effective_qty = max(holding_qty, sellable_qty)

            if effective_qty > 0:
                if effective_qty != holding_qty:
                    logger.warning(
                        "[KIS][BAL] 수량 보정 적용: symbol=%s, hldg_qty=%s, ord_psbl_qty=%s -> quantity=%s",
                        item.get("pdno"),
                        holding_qty,
                        sellable_qty,
                        effective_qty,
                    )
                holdings.append({
                    "stock_code": item.get("pdno"),
                    "stock_name": item.get("prdt_name"),
                    "quantity": effective_qty,
                    "holding_qty": holding_qty,
                    "sellable_qty": sellable_qty,
                    "avg_price": self._parse_numeric_float(item.get("pchs_avg_pric"), 0.0),
                    "current_price": self._parse_numeric_float(item.get("prpr"), 0.0),
                    "eval_amount": self._parse_numeric_float(item.get("evlu_amt"), 0.0),
                    "pnl_amount": self._parse_numeric_float(item.get("evlu_pfls_amt"), 0.0),
                    "pnl_rate": self._parse_numeric_float(item.get("evlu_pfls_rt"), 0.0),
                })
        
        # 계좌 요약
        output2 = data.get("output2", [{}])[0] if data.get("output2") else {}
        
        result = {
            "success": True,
            "holdings": holdings,
            "total_eval": self._parse_numeric_float(output2.get("tot_evlu_amt"), 0.0),
            "cash_balance": self._parse_numeric_float(output2.get("dnca_tot_amt"), 0.0),
            "total_pnl": self._parse_numeric_float(output2.get("evlu_pfls_smtl_amt"), 0.0),
        }
        self._balance_cache = deepcopy(result)
        self._balance_cache_ts = time.time()
        return result

    def get_holdings(self) -> List[Dict[str, Any]]:
        """
        계좌 보유현황을 SSOT 형식으로 정규화해 반환합니다.

        Returns:
            List[Dict]: [{"stock_code": str, "qty": int, "avg_price": Decimal, "stock_name": Optional[str]}]
        """
        now_ts = time.time()
        if (
            self._holdings_cache is not None
            and (now_ts - self._holdings_cache_ts) < max(self._holdings_cache_ttl_sec, 0.0)
        ):
            logger.info(
                "[KIS][HOLDINGS] 캐시 재사용: age=%.2fs",
                now_ts - self._holdings_cache_ts,
            )
            return deepcopy(self._holdings_cache)

        data = self._request_balance_payload()

        candidate_paths: List[Tuple[str, ...]] = [
            ("output1",),
            ("output2",),
            ("output", "output1"),
            ("output", "output2"),
        ]
        rows, resolved_path = self._resolve_first_list_path(data, candidate_paths)
        if not rows:
            # path는 찾았지만 결과가 빈 배열인 경우(정상 무보유)는 경고 대상이 아닙니다.
            if resolved_path:
                logger.info(
                    "[KIS][HOLDINGS] parsed path=%s count=0",
                    resolved_path,
                )
                return []
            logger.warning(
                "[KIS][HOLDINGS] 보유 배열 경로를 찾지 못함: candidates=%s",
                ["/".join(path) for path in candidate_paths],
            )
            return []

        stock_code_keys = ["pdno", "prdt_no", "stock_code", "symbol"]
        qty_keys = ["hldg_qty", "hold_qty", "qty", "quantity"]
        avg_price_keys = ["pchs_avg_pric", "avg_buy_price", "avg_price", "pchs_avrg_pric"]
        stock_name_keys = ["prdt_name", "name", "stock_name"]

        holdings: List[Dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue

            stock_code_raw = self._pick_first_value(item, stock_code_keys)
            qty_raw = self._pick_first_value(item, qty_keys)
            avg_raw = self._pick_first_value(item, avg_price_keys)
            stock_name_raw = self._pick_first_value(item, stock_name_keys)

            stock_code = str(stock_code_raw or "").strip()
            qty = self._parse_numeric_int(qty_raw, 0)
            avg_price = self._parse_numeric_decimal(avg_raw, Decimal("0"))
            if avg_price is None:
                avg_price = Decimal("0")

            if not stock_code:
                logger.warning(
                    "[KIS][HOLDINGS] 항목 스킵(stock_code 미존재): keys=%s",
                    list(item.keys()),
                )
                continue
            if qty <= 0:
                continue

            holdings.append(
                {
                    "stock_code": stock_code,
                    "qty": int(qty),
                    "avg_price": avg_price,
                    "stock_name": str(stock_name_raw).strip() if stock_name_raw else None,
                }
            )

        logger.info(
            "[KIS][HOLDINGS] parsed path=%s count=%s",
            resolved_path,
            len(holdings),
        )
        self._holdings_cache = deepcopy(holdings)
        self._holdings_cache_ts = time.time()
        logger.info(
            "[KIS][HOLDINGS] 실조회 결과 캐시 갱신: count=%s",
            len(holdings),
        )
        return holdings

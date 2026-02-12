"""
Universe selector for safe pre-market stock selection.

Design goals:
- Select once before market open (KST), never change during market hours.
- Cache selection for deterministic restart behavior.
- Keep fixed mode fully backward compatible.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from env import get_trading_mode
from utils.logger import get_logger
from utils.market_hours import KST


logger = get_logger("universe_selector")


@dataclass
class UniverseSelectionConfig:
    selection_method: str = "fixed"
    max_stocks: int = 5
    min_volume: float = 1_000_000_000
    min_market_cap: float = 1_000.0
    min_atr_pct: float = 1.0
    max_atr_pct: float = 8.0
    atr_period: int = 14
    volume_top_n: int = 50
    exclude_management: bool = True
    fallback_to_fixed: bool = True
    halt_on_fallback_in_real: bool = False
    universe_cache_file: str = "data/universe_cache.json"
    candidate_pool_mode: str = "yaml"  # yaml | kospi200 | volume_top | market
    candidate_stocks: List[str] = field(default_factory=list)
    stocks: List[str] = field(default_factory=list)
    cache_refresh_enabled: bool = False
    cache_refresh_on_restart: bool = False
    cache_refresh_on_market_open: bool = False
    cache_refresh_interval_minutes: int = 0
    cache_refresh_methods: List[str] = field(
        default_factory=lambda: ["combined", "volume_top", "atr_filter"]
    )
    market_scan_size: int = 200


class UniverseSelector:
    def __init__(self, config: UniverseSelectionConfig, kis_client: Any, db: Any = None):
        self.config = config
        self.kis_client = kis_client
        self.db = db
        self.trading_mode = get_trading_mode()
        root = Path(__file__).resolve().parent.parent
        self.cache_file = root / self.config.universe_cache_file
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_yaml(cls, yaml_path: str, kis_client: Any, db: Any = None) -> "UniverseSelector":
        if yaml is None:
            raise RuntimeError("PyYAML이 설치되어 있지 않아 universe.yaml 로딩이 불가합니다.")
        path = Path(yaml_path)
        if not path.is_absolute():
            # 실행 cwd와 무관하게 프로젝트 루트 기준으로 해석
            path = Path(__file__).resolve().parent.parent / path
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        section = data.get("universe", {})
        cfg = UniverseSelectionConfig(
            selection_method=str(section.get("selection_method", "fixed")).lower(),
            max_stocks=int(section.get("max_stocks", 5)),
            min_volume=float(section.get("min_volume", 1_000_000_000)),
            min_market_cap=float(section.get("min_market_cap", 1_000)),
            min_atr_pct=float(section.get("min_atr_pct", 1.0)),
            max_atr_pct=float(section.get("max_atr_pct", 8.0)),
            atr_period=int(section.get("atr_period", 14)),
            volume_top_n=int(section.get("volume_top_n", 50)),
            exclude_management=bool(section.get("exclude_management", True)),
            fallback_to_fixed=bool(section.get("fallback_to_fixed", True)),
            halt_on_fallback_in_real=bool(section.get("halt_on_fallback_in_real", False)),
            universe_cache_file=str(section.get("universe_cache_file", "data/universe_cache.json")),
            candidate_pool_mode=str(section.get("candidate_pool_mode", "yaml")).lower(),
            candidate_stocks=[str(x) for x in section.get("candidate_stocks", [])],
            stocks=[str(x) for x in section.get("stocks", [])],
            cache_refresh_enabled=bool(section.get("cache_refresh_enabled", False)),
            cache_refresh_on_restart=bool(section.get("cache_refresh_on_restart", False)),
            cache_refresh_on_market_open=bool(section.get("cache_refresh_on_market_open", False)),
            cache_refresh_interval_minutes=int(section.get("cache_refresh_interval_minutes", 0)),
            cache_refresh_methods=[
                str(x).lower()
                for x in section.get(
                    "cache_refresh_methods", ["combined", "volume_top", "atr_filter"]
                )
            ],
            market_scan_size=int(section.get("market_scan_size", 200)),
        )
        return cls(config=cfg, kis_client=kis_client, db=db)

    def select(self) -> List[str]:
        now = datetime.now(KST)
        method = self.config.selection_method
        cache_key = now.strftime("%Y-%m-%d")
        logger.info(
            f"[UNIVERSE] select start: method={method}, cache_file={self.cache_file}, "
            f"market_hours={self._is_market_hours(now)}"
        )
        if self._is_market_hours(now):
            payload = self._load_cache_payload_for_today(now)
            if payload:
                cached_method = str(payload.get("selection_method", "")).lower()
                cached_method_base = cached_method.split("_")[0] if cached_method else ""
                if cached_method_base != method:
                    logger.info(
                        f"[UNIVERSE][CACHE] MISS reason=method_mismatch "
                        f"(expected={method}, cached={cached_method}, "
                        f"cache_key={payload.get('cache_key')}, cache_file={self.cache_file})"
                    )
                    return self._select_and_cache(now, method_suffix="refresh_method_mismatch")
                cached = self._finalize(payload.get("stocks") or [])
                refresh_reason = self._cache_refresh_reason(now, payload)
                if refresh_reason:
                    logger.info(
                        f"[UNIVERSE][CACHE] MISS reason={refresh_reason} "
                        f"(method={method}, cache_key={payload.get('cache_key')}, "
                        f"cache_file={self.cache_file})"
                    )
                    return self._select_and_cache(now, method_suffix=f"refresh_{refresh_reason}")
                logger.info(
                    f"[UNIVERSE][CACHE] HIT stocks={cached} "
                    f"(method={method}, cached_method={cached_method}, "
                    f"cache_key={payload.get('cache_key')}, cache_date={payload.get('date')}, "
                    f"saved_at={payload.get('saved_at')}, cache_file={self.cache_file})"
                )
                return cached
            logger.info(
                f"[UNIVERSE][CACHE] MISS reason=no_cache "
                f"(method={method}, cache_key={cache_key}, cache_file={self.cache_file})"
            )
            logger.warning("[UNIVERSE] 장중 캐시 없음 - 금일 1회 bootstrap 선정 후 캐시 저장")
            return self._select_and_cache(now, method_suffix="intra_bootstrap")

        # Pre-market: always reselect once for today
        logger.info(
            f"[UNIVERSE][CACHE] MISS reason=premarket_reselect "
            f"(method={method}, cache_key={cache_key}, cache_file={self.cache_file})"
        )
        return self._select_and_cache(now)

    def _select_and_cache(self, now: datetime, method_suffix: str = "") -> List[str]:
        try:
            method = self.config.selection_method
            logger.info(f"[UNIVERSE] 재선정 시작: method={method}")
            if method == "fixed":
                selected = self._select_fixed()
            elif method == "volume_top":
                selected = self._select_volume_top(self.config.volume_top_n)
            elif method == "atr_filter":
                selected = self._select_atr_filter_from_pool()
            elif method == "combined":
                selected = self._select_combined()
            else:
                raise ValueError(f"지원하지 않는 selection_method: {method}")

            validated = self._finalize(selected)
            cache_method = method if not method_suffix else f"{method}_{method_suffix}"
            self._save_cache(now, validated, cache_method)
            logger.info(
                f"[UNIVERSE] 최종 종목: {validated} "
                f"(method={method}, cache_method={cache_method}, cache_file={self.cache_file})"
            )
            return validated
        except Exception as e:
            logger.exception(f"[UNIVERSE] selection 실패: {e}")
            return self._fallback_fixed_or_raise(str(e))

    # ----------------------------
    # Selection methods
    # ----------------------------
    def _select_fixed(self) -> List[str]:
        # backward compatibility: preserve fixed.stocks behavior
        return self.config.stocks[: self.config.max_stocks]

    def _select_volume_top(self, limit: int) -> List[str]:
        mode = self.config.candidate_pool_mode
        candidates = self._candidate_pool_for_volume_scan()
        pool_size = len(candidates)
        volume_source = "market_scan" if mode == "market" else "restricted_pool"
        logger.info(
            f"[UNIVERSE] volume_top scope={volume_source}, pool_mode={mode}, "
            f"pool_size={pool_size}, limit={limit}"
        )
        rows: List[Tuple[str, float]] = []
        bulk_ok = False
        if mode == "market" and hasattr(self.kis_client, "get_market_top_by_trade_value"):
            try:
                top_n = max(limit * 5, self.config.max_stocks * 5)
                market_rows = self.kis_client.get_market_top_by_trade_value(top_n=top_n)
                for snap in market_rows:
                    if self._passes_safety_filters(snap):
                        rows.append((str(snap["code"]), float(snap["trade_value"])))
                bulk_ok = True
            except Exception:
                bulk_ok = False

        if not bulk_ok and hasattr(self.kis_client, "get_market_snapshot_bulk"):
            try:
                bulk = self.kis_client.get_market_snapshot_bulk(candidates)
                for snap in bulk:
                    if self._passes_safety_filters(snap):
                        rows.append((str(snap["code"]), float(snap["trade_value"])))
                bulk_ok = True
            except Exception:
                bulk_ok = False

        if not bulk_ok:
            scan_candidates = candidates[: max(limit * 5, self.config.max_stocks * 5)]
            for idx, code in enumerate(scan_candidates):
                try:
                    snap = self._snapshot_for_symbol(code)
                    if not self._passes_safety_filters(snap):
                        continue
                    rows.append((code, snap["trade_value"]))
                    # rate limit friendly
                    if idx % 10 == 0:
                        time.sleep(0.05)
                except Exception:
                    continue
        rows.sort(key=lambda x: x[1], reverse=True)
        selected = [c for c, _ in rows[: max(limit, self.config.max_stocks)]]
        logger.info(f"[UNIVERSE] volume_top 통과={len(selected)}")
        return selected[: self.config.max_stocks]

    def _select_atr_filter_from_pool(self) -> List[str]:
        pool = self._resolve_atr_candidate_pool()
        logger.info(f"[UNIVERSE] atr_filter 후보={len(pool)}")
        selected: List[str] = []
        for code in pool:
            try:
                ratio = self._atr_ratio_pct(code)
                if ratio is None:
                    continue
                if self.config.min_atr_pct <= ratio <= self.config.max_atr_pct:
                    selected.append(code)
            except Exception:
                continue
        logger.info(f"[UNIVERSE] atr_filter 통과={len(selected)}")
        return selected[: self.config.max_stocks]

    def _select_combined(self) -> List[str]:
        first_stage = self._select_volume_top(self.config.max_stocks * 3)
        logger.info(f"[UNIVERSE] combined stage1={len(first_stage)}")
        second_stage: List[str] = []
        for code in first_stage:
            ratio = self._atr_ratio_pct(code)
            if ratio is None:
                continue
            if self.config.min_atr_pct <= ratio <= self.config.max_atr_pct:
                second_stage.append(code)
        logger.info(f"[UNIVERSE] combined stage2={len(second_stage)}")
        if (
            self.config.candidate_pool_mode == "yaml"
            and len(second_stage) > 0
            and self._dedupe(second_stage) == self._dedupe(self.config.candidate_stocks)[: len(second_stage)]
        ):
            logger.info(
                "[UNIVERSE] restricted pool 모드(yaml): 최종 선정이 candidate_stocks와 동일합니다."
            )
        return second_stage[: self.config.max_stocks]

    # ----------------------------
    # Helpers
    # ----------------------------
    def _fallback_fixed_or_raise(self, reason: str) -> List[str]:
        logger.error(f"[UNIVERSE] fallback 사유: {reason}")
        if self.trading_mode == "REAL":
            print("\n" + "!" * 72)
            print("⚠️ REAL 모드에서 Universe 자동선정 실패 - fallback 발생")
            print("⚠️ 10초 후 처리 계속")
            print("!" * 72 + "\n")
            time.sleep(10)
        if not self.config.fallback_to_fixed:
            raise RuntimeError(f"Universe selection failed: {reason}")
        fallback = self._finalize(self._select_fixed())
        if self.trading_mode == "REAL" and self.config.halt_on_fallback_in_real:
            raise RuntimeError("REAL 모드 fallback 발생으로 거래 중단(halt_on_fallback_in_real=true)")
        self._save_cache(datetime.now(KST), fallback, "fixed_fallback")
        logger.warning(f"[UNIVERSE] fallback 적용 종목={fallback}")
        return fallback

    def _candidate_pool_for_volume_scan(self) -> List[str]:
        mode = self.config.candidate_pool_mode
        if mode == "market":
            return self._load_market_codes()
        # yaml/kospi200/volume_top은 제한 후보군 스캔
        if self.config.candidate_stocks:
            return self._dedupe(self.config.candidate_stocks)
        if self.config.stocks:
            return self._dedupe(self.config.stocks)
        return self._load_kospi200_codes()

    def _resolve_atr_candidate_pool(self) -> List[str]:
        mode = self.config.candidate_pool_mode
        if mode == "kospi200":
            return self._load_kospi200_codes()
        if mode in ("volume_top", "market"):
            return self._select_volume_top(self.config.max_stocks * 3)
        return self._dedupe(self.config.candidate_stocks or self.config.stocks)

    def _load_market_codes(self) -> List[str]:
        if hasattr(self.kis_client, "get_market_universe_codes"):
            try:
                codes = self.kis_client.get_market_universe_codes(limit=self.config.market_scan_size)
                if codes:
                    return self._dedupe([str(c) for c in codes])
            except Exception:
                pass
        return self._load_kospi200_codes()

    def _load_kospi200_codes(self) -> List[str]:
        # Optional local list file
        path = Path(__file__).resolve().parent.parent / "config" / "kospi200_codes.txt"
        if path.exists():
            codes = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            return self._dedupe(codes)
        # safe fallback seed set
        seed = [
            "005930", "000660", "005380", "035420", "035720",
            "051910", "006400", "207940", "068270", "105560",
            "012330", "055550", "066570", "323410", "096770",
        ]
        return seed

    def _snapshot_for_symbol(self, code: str) -> Dict[str, Any]:
        price = self.kis_client.get_current_price(code)
        current_price = float(price.get("current_price") or 0)
        open_price = float(price.get("open_price") or 0)
        volume = float(price.get("volume") or 0)
        if current_price <= 0:
            raise ValueError("price==0")
        trade_value = current_price * volume
        pct = 0.0
        if open_price > 0:
            pct = ((current_price - open_price) / open_price) * 100.0
        # KIS API 기본 응답에는 관리/정지/시총 필드가 없어 best-effort 처리
        market_cap = 0.0
        return {
            "code": code,
            "current_price": current_price,
            "open_price": open_price,
            "volume": volume,
            "trade_value": trade_value,
            "market_cap": market_cap,
            "is_suspended": False,
            "is_management": False,
            "pct_from_open": pct,
        }

    def _passes_safety_filters(self, snap: Dict[str, Any]) -> bool:
        if float(snap.get("trade_value") or 0.0) < self.config.min_volume:
            return False
        if snap["market_cap"] > 0 and snap["market_cap"] < self.config.min_market_cap:
            return False
        if snap.get("is_suspended"):
            return False
        if self.config.exclude_management and snap.get("is_management"):
            return False
        if abs(float(snap.get("pct_from_open") or 0.0)) >= 28.0:
            return False
        return True

    def _atr_ratio_pct(self, code: str) -> Optional[float]:
        df = self.kis_client.get_daily_ohlcv(code, period_type="D")
        if df is None or len(df) < 20:
            return None
        closes = df["close"].astype(float).tolist()
        highs = df["high"].astype(float).tolist()
        lows = df["low"].astype(float).tolist()
        if not closes or closes[-1] <= 0:
            return None
        period = max(int(self.config.atr_period), 1)
        tr_list: List[float] = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            tr_list.append(max(tr, 0.0))
        if len(tr_list) < period:
            return None
        atr = sum(tr_list[-period:]) / period
        return (atr / closes[-1]) * 100.0

    def _is_market_hours(self, now: datetime) -> bool:
        t = now.time()
        return (t.hour > 9 or (t.hour == 9 and t.minute >= 0)) and (
            t.hour < 15 or (t.hour == 15 and t.minute < 30)
        )

    def _load_cache_payload_for_today(self, now: datetime) -> Dict[str, Any]:
        if not self.cache_file.exists():
            return {}
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if payload.get("date") != now.strftime("%Y-%m-%d"):
                return {}
            if not payload.get("saved_at"):
                payload["saved_at"] = now.isoformat()
            return payload
        except Exception:
            return {}

    def _cache_refresh_reason(self, now: datetime, payload: Dict[str, Any]) -> str:
        """
        장중 캐시 강제 갱신 사유를 반환합니다.
        빈 문자열이면 캐시 재사용.
        """
        method = self.config.selection_method
        if not self.config.cache_refresh_enabled:
            return ""
        if method not in set(self.config.cache_refresh_methods):
            return ""
        if method == "fixed":
            return ""

        if self.config.cache_refresh_on_restart:
            return "restart"

        if self.config.cache_refresh_on_market_open:
            if not bool(payload.get("market_open_refreshed", False)):
                return "market_open"

        interval_min = max(int(self.config.cache_refresh_interval_minutes), 0)
        if interval_min > 0:
            saved_at_raw = payload.get("saved_at")
            saved_at = None
            if isinstance(saved_at_raw, str):
                try:
                    saved_at = datetime.fromisoformat(saved_at_raw)
                except ValueError:
                    saved_at = None
            if saved_at is None:
                return "interval"
            if now - saved_at >= timedelta(minutes=interval_min):
                return "interval"

        return ""

    def _save_cache(self, now: datetime, stocks: List[str], method: str) -> None:
        market_open_refreshed = ("refresh_" in method) or ("intra_bootstrap" in method)
        payload = {
            "date": now.strftime("%Y-%m-%d"),
            "stocks": stocks,
            "selection_method": method,
            "saved_at": now.isoformat(),
            "cache_key": now.strftime("%Y-%m-%d"),
            "market_open_refreshed": market_open_refreshed,
        }
        self.cache_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _finalize(self, stocks: Iterable[str]) -> List[str]:
        deduped = self._dedupe([str(s) for s in stocks])
        valid = [s for s in deduped if self._is_valid_code(s)]
        limited = valid[: self.config.max_stocks]
        if len(limited) == 0:
            raise RuntimeError("Universe 종목 수가 0개입니다. 거래를 중단합니다.")
        return limited

    @staticmethod
    def _dedupe(stocks: Iterable[str]) -> List[str]:
        seen = set()
        out: List[str] = []
        for s in stocks:
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out

    @staticmethod
    def _is_valid_code(code: str) -> bool:
        return len(code) == 6 and code.isdigit()

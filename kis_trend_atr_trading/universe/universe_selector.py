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
from datetime import datetime
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
    candidate_pool_mode: str = "yaml"  # kospi200 | yaml | volume_top
    candidate_stocks: List[str] = field(default_factory=list)
    stocks: List[str] = field(default_factory=list)


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
        )
        return cls(config=cfg, kis_client=kis_client, db=db)

    def select(self) -> List[str]:
        now = datetime.now(KST)
        if self._is_market_hours(now):
            cached = self._load_cache_for_today(now)
            if cached:
                logger.info(f"[UNIVERSE] 장중 재시작: 캐시 재사용 {cached}")
                return cached
            logger.error("[UNIVERSE] 장중 캐시 없음 - fallback 고정 목록 사용")
            return self._fallback_fixed_or_raise("장중 캐시 미존재")

        # Pre-market: always reselect once for today
        try:
            method = self.config.selection_method
            logger.info(f"[UNIVERSE] pre-market 재선정 시작: method={method}")
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
            self._save_cache(now, validated, method)
            logger.info(f"[UNIVERSE] 최종 종목: {validated}")
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
        candidates = self._candidate_pool_for_volume_scan()
        logger.info(f"[UNIVERSE] volume_top 후보={len(candidates)}")
        rows: List[Tuple[str, float]] = []
        bulk_ok = False
        if hasattr(self.kis_client, "get_market_snapshot_bulk"):
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
        # KIS bulk universe endpoint가 없어서 제한된 후보군 내 snapshot 스캔
        if self.config.candidate_stocks:
            return self._dedupe(self.config.candidate_stocks)
        if self.config.stocks:
            return self._dedupe(self.config.stocks)
        return self._load_kospi200_codes()

    def _resolve_atr_candidate_pool(self) -> List[str]:
        mode = self.config.candidate_pool_mode
        if mode == "kospi200":
            return self._load_kospi200_codes()
        if mode == "volume_top":
            return self._select_volume_top(self.config.max_stocks * 3)
        return self._dedupe(self.config.candidate_stocks or self.config.stocks)

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

    def _load_cache_for_today(self, now: datetime) -> List[str]:
        if not self.cache_file.exists():
            return []
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if payload.get("date") != now.strftime("%Y-%m-%d"):
                return []
            stocks = payload.get("stocks") or []
            return self._finalize(stocks)
        except Exception:
            return []

    def _save_cache(self, now: datetime, stocks: List[str], method: str) -> None:
        payload = {
            "date": now.strftime("%Y-%m-%d"),
            "stocks": stocks,
            "selection_method": method,
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

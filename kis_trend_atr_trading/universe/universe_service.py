from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from utils.logger import get_logger
from utils.market_hours import KST
from .universe_selector import UniverseSelector

logger = get_logger("universe_service")


@dataclass
class UniversePolicy:
    selection_method: str
    universe_size: int
    max_positions: int
    fixed_stocks: List[str]
    cache_file: Path
    params: Dict[str, Any]


class UniverseService:
    """
    Daily universe service.

    - holdings_symbols: OPEN 포지션 종목 집합
    - todays_universe: 오늘 신규 진입 후보 원본
    - entry_candidates: todays_universe - holdings_symbols
    """

    def __init__(self, yaml_path: str, kis_client: Any, data_dir: Optional[Path] = None):
        self.yaml_path = Path(yaml_path)
        self.kis_client = kis_client
        self.data_dir = data_dir or (Path(__file__).resolve().parent.parent / "data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.policy = self._load_policy()

    def _load_policy(self) -> UniversePolicy:
        if yaml is None:
            raise RuntimeError("PyYAML이 필요합니다.")
        with self.yaml_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        uni = raw.get("universe", {})
        stocks = [str(x) for x in raw.get("stocks", [])]
        max_stocks = int(uni.get("max_stocks", 5))
        universe_size = int(uni.get("universe_size", max_stocks))
        max_positions = int(uni.get("max_positions", max_stocks))
        cache_rel = str(uni.get("universe_cache_file", "data/universe_cache.json"))
        cache_file = Path(__file__).resolve().parent.parent / cache_rel
        params = {
            "min_volume": uni.get("min_volume"),
            "min_market_cap": uni.get("min_market_cap"),
            "min_atr_pct": uni.get("min_atr_pct"),
            "max_atr_pct": uni.get("max_atr_pct"),
            "candidate_pool_mode": uni.get("candidate_pool_mode"),
            "market_scan_size": uni.get("market_scan_size"),
        }
        return UniversePolicy(
            selection_method=str(uni.get("selection_method", "fixed")).lower(),
            universe_size=max(universe_size, 0),
            max_positions=max(max_positions, 0),
            fixed_stocks=stocks,
            cache_file=cache_file,
            params=params,
        )

    def load_holdings_symbols(self) -> List[str]:
        symbols: Set[str] = set()
        for path in self.data_dir.glob("positions*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                pos = payload.get("position") if isinstance(payload, dict) else None
                if not isinstance(pos, dict):
                    continue
                code = str(pos.get("stock_code") or "").strip()
                qty = int(pos.get("quantity") or 0)
                if len(code) == 6 and code.isdigit() and qty > 0:
                    symbols.add(code)
            except Exception:
                continue
        return sorted(symbols)

    def _read_cache_for_date(self, trade_date: str) -> Optional[Dict[str, Any]]:
        if not self.policy.cache_file.exists():
            return None
        try:
            payload = json.loads(self.policy.cache_file.read_text(encoding="utf-8"))
            if payload.get("date") != trade_date:
                return None
            return payload
        except Exception:
            return None

    def _save_cache(
        self,
        trade_date: str,
        symbols: List[str],
        selection_method: str,
    ) -> None:
        payload = {
            "date": trade_date,
            "selection_method": selection_method,
            "universe_size": self.policy.universe_size,
            "max_positions": self.policy.max_positions,
            "params": self.policy.params,
            "universe_symbols": symbols,
            "stocks": symbols,  # backward compatibility
            "created_at": datetime.now(KST).isoformat(),
            "cache_key": trade_date,
            "saved_at": datetime.now(KST).isoformat(),
        }
        self.policy.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.policy.cache_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_or_create_todays_universe(self, trade_date: str) -> List[str]:
        cached = self._read_cache_for_date(trade_date)
        if cached:
            symbols = [str(s) for s in (cached.get("universe_symbols") or cached.get("stocks") or [])]
            logger.info("[UNIVERSE] reuse cached universe for today")
            logger.info(
                f"[UNIVERSE] today={trade_date} method={self.policy.selection_method} "
                f"universe_size={self.policy.universe_size} -> symbols={symbols}"
            )
            return symbols

        selector = UniverseSelector.from_yaml(
            yaml_path=str(self.yaml_path),
            kis_client=self.kis_client,
            db=None,
        )
        selector.config.max_stocks = self.policy.universe_size

        try:
            symbols = selector.select()
            self._save_cache(trade_date, symbols, self.policy.selection_method)
            logger.info(
                f"[UNIVERSE] today={trade_date} method={self.policy.selection_method} "
                f"universe_size={self.policy.universe_size} -> symbols={symbols}"
            )
            return symbols
        except Exception as e:
            reason = str(e)
            cached = self._read_cache_for_date(trade_date)
            if cached:
                symbols = [str(s) for s in (cached.get("universe_symbols") or cached.get("stocks") or [])]
                logger.warning(
                    f"[UNIVERSE] refresh failed -> fallback reason={reason} using=today_cache"
                )
                return symbols

            if self.policy.fixed_stocks:
                symbols = self.policy.fixed_stocks[: self.policy.universe_size]
                self._save_cache(trade_date, symbols, "fixed_fallback")
                logger.warning(
                    f"[UNIVERSE] refresh failed -> fallback reason={reason} using=fixed_stocks"
                )
                return symbols

            logger.warning(
                f"[UNIVERSE] refresh failed -> fallback reason={reason} using=empty_universe"
            )
            self._save_cache(trade_date, [], "empty_fallback")
            return []

    @staticmethod
    def compute_entry_candidates(holdings: List[str], todays_universe: List[str]) -> List[str]:
        holding_set = set(holdings)
        return [s for s in todays_universe if s not in holding_set]

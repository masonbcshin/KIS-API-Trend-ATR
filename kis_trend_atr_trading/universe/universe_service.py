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
try:
    from kis_trend_atr_trading.env import get_db_namespace_mode
except Exception:  # pragma: no cover
    from env import get_db_namespace_mode
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

    @staticmethod
    def _current_db_mode() -> str:
        try:
            mode = str(get_db_namespace_mode() or "PAPER").upper().strip()
        except Exception:
            mode = "PAPER"
        return mode if mode in ("DRY_RUN", "PAPER", "REAL") else "PAPER"

    def _load_holdings_symbols_from_api(self, mode: str) -> Set[str]:
        """
        PAPER/REAL 모드에서는 브로커 계좌 보유를 우선 사용합니다.
        """
        if mode not in ("PAPER", "REAL"):
            return set()

        fetch_balance = getattr(self.kis_client, "get_account_balance", None)
        if not callable(fetch_balance):
            return set()

        try:
            balance = fetch_balance()
        except Exception as e:
            logger.warning(f"[UNIVERSE] API holdings 조회 실패: {e}")
            return set()

        holdings = balance.get("holdings", []) if isinstance(balance, dict) else []
        symbols: Set[str] = set()
        for item in holdings if isinstance(holdings, list) else []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("stock_code") or "").strip()
            qty = int(item.get("qty") or item.get("quantity") or 0)
            if len(code) == 6 and code.isdigit() and qty > 0:
                symbols.add(code)
        return symbols

    def load_holdings_symbols(self) -> List[str]:
        symbols: Set[str] = set()
        mode = self._current_db_mode()

        # 계좌 동기화 모드(PAPER/REAL)는 API 보유를 우선 반영
        symbols.update(self._load_holdings_symbols_from_api(mode))

        # 모드 네임스페이스 파일만 로드 (예: positions_REAL_005930.json)
        for path in self.data_dir.glob(f"positions_{mode}_*.json"):
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

        logger.info(f"[UNIVERSE] holdings symbols loaded: mode={mode}, count={len(symbols)}")
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

    @staticmethod
    def _normalize_symbol_list(values: Any) -> List[str]:
        out: List[str] = []
        if not isinstance(values, list):
            return out
        for value in values:
            code = str(value or "").strip()
            if len(code) == 6 and code.isdigit() and code not in out:
                out.append(code)
        return out

    def get_todays_universe_snapshot(self, trade_date: str) -> Dict[str, Any]:
        payload = self._read_cache_for_date(trade_date) or {}
        final_symbols = self._normalize_symbol_list(
            payload.get("universe_symbols") or payload.get("stocks") or []
        )
        candidate_symbols = self._normalize_symbol_list(
            payload.get("candidate_symbols")
            or payload.get("pre_limit_symbols")
            or payload.get("selected_symbols")
            or []
        )
        if not candidate_symbols:
            candidate_symbols = list(final_symbols)
        selection_meta = payload.get("selection_meta")
        if not isinstance(selection_meta, dict):
            selection_meta = {}
        return {
            "trade_date": trade_date,
            "selection_method": str(payload.get("selection_method") or self.policy.selection_method),
            "candidate_symbols": candidate_symbols,
            "universe_symbols": final_symbols,
            "selection_meta": dict(selection_meta),
        }

    def _save_cache(
        self,
        trade_date: str,
        symbols: List[str],
        selection_method: str,
        selection_snapshot: Optional[Dict[str, Any]] = None,
    ) -> None:
        snapshot = selection_snapshot or {}
        final_symbols = self._normalize_symbol_list(symbols)
        candidate_symbols = self._normalize_symbol_list(
            snapshot.get("candidate_symbols") or final_symbols
        )
        pre_limit_symbols = self._normalize_symbol_list(
            snapshot.get("pre_limit_symbols") or candidate_symbols
        )
        selected_symbols = self._normalize_symbol_list(
            snapshot.get("selected_symbols") or final_symbols
        )
        payload = {
            "date": trade_date,
            "selection_method": selection_method,
            "universe_size": self.policy.universe_size,
            "max_positions": self.policy.max_positions,
            "params": self.policy.params,
            "candidate_symbols": candidate_symbols,
            "pre_limit_symbols": pre_limit_symbols,
            "selected_symbols": selected_symbols,
            "universe_symbols": final_symbols,
            "stocks": final_symbols,  # backward compatibility
            "created_at": datetime.now(KST).isoformat(),
            "cache_key": trade_date,
            "saved_at": datetime.now(KST).isoformat(),
        }
        if isinstance(snapshot.get("meta"), dict) and snapshot.get("meta"):
            payload["selection_meta"] = dict(snapshot.get("meta"))
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
            snapshot: Dict[str, Any] = {}
            get_meta = getattr(selector, "get_last_selection_meta", None)
            if callable(get_meta):
                try:
                    snapshot = dict(get_meta() or {})
                except Exception:
                    snapshot = {}
            self._save_cache(
                trade_date,
                symbols,
                self.policy.selection_method,
                selection_snapshot=snapshot,
            )
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
                self._save_cache(
                    trade_date,
                    symbols,
                    "fixed_fallback",
                    selection_snapshot={
                        "candidate_symbols": symbols,
                        "pre_limit_symbols": symbols,
                        "selected_symbols": symbols,
                        "meta": {"strategy": "fixed_fallback", "reason": reason},
                    },
                )
                logger.warning(
                    f"[UNIVERSE] refresh failed -> fallback reason={reason} using=fixed_stocks"
                )
                return symbols

            logger.warning(
                f"[UNIVERSE] refresh failed -> fallback reason={reason} using=empty_universe"
            )
            self._save_cache(
                trade_date,
                [],
                "empty_fallback",
                selection_snapshot={
                    "candidate_symbols": [],
                    "pre_limit_symbols": [],
                    "selected_symbols": [],
                    "meta": {"strategy": "empty_fallback", "reason": reason},
                },
            )
            return []

    @staticmethod
    def compute_entry_candidates(holdings: List[str], todays_universe: List[str]) -> List[str]:
        holding_set = set(holdings)
        return [s for s in todays_universe if s not in holding_set]

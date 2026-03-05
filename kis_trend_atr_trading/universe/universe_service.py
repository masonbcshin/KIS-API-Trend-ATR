from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from utils.logger import get_logger
from utils.market_hours import KST, is_holiday, is_weekend
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
    out_of_universe_warn_days: int
    out_of_universe_reduce_days: int
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
    CACHE_SCHEMA_VERSION = 2
    LEGACY_CACHE_SCHEMA_VERSION = 1

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
        out_of_universe_warn_days = max(int(uni.get("out_of_universe_warn_days", 20)), 0)
        out_of_universe_reduce_days = max(int(uni.get("out_of_universe_reduce_days", 30)), 0)
        if out_of_universe_reduce_days > 0 and out_of_universe_reduce_days < out_of_universe_warn_days:
            out_of_universe_reduce_days = out_of_universe_warn_days
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
            out_of_universe_warn_days=out_of_universe_warn_days,
            out_of_universe_reduce_days=out_of_universe_reduce_days,
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

    def _build_cache_identity(self, trade_date: str) -> Dict[str, str]:
        db_mode = self._current_db_mode()
        normalized_fixed = [str(x).strip() for x in (self.policy.fixed_stocks or []) if str(x).strip()]
        signature_source = {
            "db_mode": db_mode,
            "selection_method": str(self.policy.selection_method or "").strip().lower(),
            "universe_size": int(self.policy.universe_size),
            "max_positions": int(self.policy.max_positions),
            "fixed_stocks": normalized_fixed,
            "params": dict(self.policy.params or {}),
        }
        raw = json.dumps(signature_source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        policy_signature = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        cache_key = f"{trade_date}|{db_mode}|{policy_signature[:12]}"
        return {
            "db_mode": db_mode,
            "policy_signature": policy_signature,
            "cache_key": cache_key,
        }

    def _legacy_policy_is_compatible(self, payload: Dict[str, Any]) -> bool:
        cached_method = str(payload.get("selection_method") or "").strip().lower()
        current_method = str(self.policy.selection_method or "").strip().lower()
        if cached_method and not cached_method.endswith("_fallback"):
            if cached_method.split("_")[0] != current_method.split("_")[0]:
                return False

        cached_universe_size = payload.get("universe_size")
        if cached_universe_size is not None:
            try:
                if int(cached_universe_size) != int(self.policy.universe_size):
                    return False
            except Exception:
                return False

        cached_max_positions = payload.get("max_positions")
        if cached_max_positions is not None:
            try:
                if int(cached_max_positions) != int(self.policy.max_positions):
                    return False
            except Exception:
                return False

        cached_params = payload.get("params")
        if cached_params is not None and isinstance(cached_params, dict):
            current_params = dict(self.policy.params or {})
            for key, value in current_params.items():
                if cached_params.get(key) != value:
                    return False
        return True

    def _migrate_legacy_cache_payload(
        self,
        payload: Dict[str, Any],
        trade_date: str,
        from_version: int,
    ) -> Optional[Dict[str, Any]]:
        if from_version != self.LEGACY_CACHE_SCHEMA_VERSION:
            return None
        if not self._legacy_policy_is_compatible(payload):
            logger.info("[UNIVERSE][CACHE] MISS reason=legacy_policy_mismatch")
            return None

        has_final_key = ("universe_symbols" in payload) or ("stocks" in payload)
        if not has_final_key:
            logger.info("[UNIVERSE][CACHE] MISS reason=legacy_missing_final_symbols")
            return None

        identity = self._build_cache_identity(trade_date)
        now_iso = datetime.now(KST).isoformat()
        final_symbols = self._normalize_symbol_list(
            payload.get("universe_symbols") or payload.get("stocks") or []
        )
        candidate_symbols = self._normalize_symbol_list(
            payload.get("candidate_symbols")
            or payload.get("pre_limit_symbols")
            or payload.get("selected_symbols")
            or final_symbols
        )
        pre_limit_symbols = self._normalize_symbol_list(
            payload.get("pre_limit_symbols") or candidate_symbols
        )
        selected_symbols = self._normalize_symbol_list(
            payload.get("selected_symbols") or final_symbols
        )
        migrated = {
            "schema_version": self.CACHE_SCHEMA_VERSION,
            "date": trade_date,
            "db_mode": identity["db_mode"],
            "policy_signature": identity["policy_signature"],
            "cache_key": identity["cache_key"],
            "selection_method": str(payload.get("selection_method") or self.policy.selection_method),
            "universe_size": int(payload.get("universe_size") or self.policy.universe_size),
            "max_positions": int(payload.get("max_positions") or self.policy.max_positions),
            "params": dict(payload.get("params") or self.policy.params or {}),
            "candidate_symbols": candidate_symbols,
            "pre_limit_symbols": pre_limit_symbols,
            "selected_symbols": selected_symbols,
            "universe_symbols": final_symbols,
            "stocks": final_symbols,
            "created_at": str(payload.get("created_at") or now_iso),
            "saved_at": str(payload.get("saved_at") or now_iso),
        }
        selection_meta = payload.get("selection_meta")
        if isinstance(selection_meta, dict) and selection_meta:
            migrated["selection_meta"] = dict(selection_meta)
        logger.info(
            "[UNIVERSE][CACHE] migrated legacy schema v%s -> v%s",
            from_version,
            self.CACHE_SCHEMA_VERSION,
        )
        return migrated

    def _write_cache_payload(self, payload: Dict[str, Any]) -> None:
        self.policy.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.policy.cache_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _read_cache_for_date(self, trade_date: str) -> Optional[Dict[str, Any]]:
        if not self.policy.cache_file.exists():
            return None
        try:
            payload = json.loads(self.policy.cache_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            if payload.get("date") != trade_date:
                return None
            raw_version = payload.get("schema_version", self.LEGACY_CACHE_SCHEMA_VERSION)
            try:
                schema_version = int(raw_version)
            except Exception:
                logger.info("[UNIVERSE][CACHE] MISS reason=schema_version_invalid")
                return None
            if schema_version > self.CACHE_SCHEMA_VERSION:
                logger.info(
                    "[UNIVERSE][CACHE] MISS reason=schema_version_unsupported cached=%s supported=%s",
                    schema_version,
                    self.CACHE_SCHEMA_VERSION,
                )
                return None
            if schema_version < self.CACHE_SCHEMA_VERSION:
                migrated = self._migrate_legacy_cache_payload(payload, trade_date, schema_version)
                if not migrated:
                    return None
                payload = migrated
                self._write_cache_payload(payload)
            identity = self._build_cache_identity(trade_date)
            cached_db_mode = str(payload.get("db_mode") or "").strip().upper()
            cached_signature = str(payload.get("policy_signature") or "").strip()
            cached_key = str(payload.get("cache_key") or "").strip()
            if (
                cached_db_mode != identity["db_mode"]
                or cached_signature != identity["policy_signature"]
                or cached_key != identity["cache_key"]
            ):
                logger.info(
                    "[UNIVERSE][CACHE] MISS reason=identity_mismatch expected_key=%s cached_key=%s "
                    "expected_mode=%s cached_mode=%s",
                    identity["cache_key"],
                    cached_key or "none",
                    identity["db_mode"],
                    cached_db_mode or "none",
                )
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
        identity = self._build_cache_identity(trade_date)
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
            "schema_version": self.CACHE_SCHEMA_VERSION,
            "date": trade_date,
            "db_mode": identity["db_mode"],
            "policy_signature": identity["policy_signature"],
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
            "cache_key": identity["cache_key"],
            "saved_at": datetime.now(KST).isoformat(),
        }
        if isinstance(snapshot.get("meta"), dict) and snapshot.get("meta"):
            payload["selection_meta"] = dict(snapshot.get("meta"))
        self._write_cache_payload(payload)

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

    @staticmethod
    def _parse_trade_date(trade_date: str) -> Optional[date]:
        token = str(trade_date or "").strip()
        if not token:
            return None
        try:
            return datetime.strptime(token, "%Y-%m-%d").date()
        except Exception:
            return None

    @staticmethod
    def is_business_trading_day(trade_day: date) -> bool:
        return not is_weekend(trade_day) and not is_holiday(trade_day)

    @classmethod
    def count_business_day_advances(cls, previous_trade_date: str, current_trade_date: str) -> int:
        current = cls._parse_trade_date(current_trade_date)
        if current is None:
            return 0

        previous = cls._parse_trade_date(previous_trade_date)
        if previous is None:
            return 1 if cls.is_business_trading_day(current) else 0
        if current <= previous:
            return 0

        count = 0
        cursor = previous + timedelta(days=1)
        while cursor <= current:
            if cls.is_business_trading_day(cursor):
                count += 1
            cursor += timedelta(days=1)
        return count

    @staticmethod
    def compute_entry_capacity(holdings: List[str], max_positions: int) -> int:
        holdings_count = len({str(s).strip() for s in list(holdings or []) if str(s).strip()})
        return max(int(max_positions) - holdings_count, 0)

    @staticmethod
    def limit_entry_candidates(entry_candidates: List[str], capacity: int) -> List[str]:
        cap = max(int(capacity), 0)
        return list(entry_candidates[:cap])

    @staticmethod
    def compute_out_of_universe_ages(
        previous_ages: Dict[str, int],
        holdings: List[str],
        todays_universe: List[str],
        advance_day: bool = True,
        advance_days: Optional[int] = None,
    ) -> Dict[str, int]:
        holdings_list = UniverseService._normalize_symbol_list(list(holdings or []))
        universe_set = set(UniverseService._normalize_symbol_list(list(todays_universe or [])))
        day_increment = max(int(advance_days), 0) if advance_days is not None else (1 if advance_day else 0)

        normalized_prev: Dict[str, int] = {}
        for raw_code, raw_days in dict(previous_ages or {}).items():
            code = str(raw_code or "").strip()
            if len(code) != 6 or not code.isdigit():
                continue
            try:
                days = max(int(raw_days), 0)
            except Exception:
                days = 0
            normalized_prev[code] = days

        updated: Dict[str, int] = {}
        for code in holdings_list:
            if code in universe_set:
                updated[code] = 0
                continue
            base = normalized_prev.get(code, 0)
            updated[code] = base + day_increment if day_increment > 0 else base
        return updated

    @staticmethod
    def summarize_out_of_universe_aging(
        ages: Dict[str, int],
        warn_days: int,
        reduce_days: int,
    ) -> Dict[str, Any]:
        warn_threshold = max(int(warn_days), 0)
        reduce_threshold = max(int(reduce_days), 0)

        normalized: Dict[str, int] = {}
        for raw_code, raw_days in dict(ages or {}).items():
            code = str(raw_code or "").strip()
            if len(code) != 6 or not code.isdigit():
                continue
            try:
                days = max(int(raw_days), 0)
            except Exception:
                days = 0
            normalized[code] = days

        out_of_universe = {code: days for code, days in normalized.items() if days > 0}
        warn_symbols = [
            code
            for code, days in sorted(out_of_universe.items(), key=lambda item: (-item[1], item[0]))
            if warn_threshold > 0 and days >= warn_threshold
        ]
        reduce_symbols = [
            code
            for code, days in sorted(out_of_universe.items(), key=lambda item: (-item[1], item[0]))
            if reduce_threshold > 0 and days >= reduce_threshold
        ]
        return {
            "tracked_count": len(normalized),
            "out_of_universe_count": len(out_of_universe),
            "warn_count": len(warn_symbols),
            "reduce_count": len(reduce_symbols),
            "warn_symbols": warn_symbols,
            "reduce_symbols": reduce_symbols,
            "out_of_universe_days": dict(sorted(out_of_universe.items(), key=lambda item: item[0])),
        }

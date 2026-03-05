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
    stock_quota: int = 0
    etf_quota: int = 0
    etf_symbols: List[str] = field(default_factory=list)
    etf_name_keywords: List[str] = field(
        default_factory=lambda: [
            "KODEX",
            "TIGER",
            "KOSEF",
            "KBSTAR",
            "KINDEX",
            "HANARO",
            "ARIRANG",
            "ACE",
            "SOL",
            "PLUS",
            "TIMEFOLIO",
            "TREX",
            "FOCUS",
            "KIWOOM",
            "KIS",
            "WON",
        ]
    )
    exclude_management: bool = True
    allow_unknown_market_cap: bool = False
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
        self._last_market_codes_source = "not_used"
        self._last_volume_data_source = "not_used"
        self._last_volume_snapshot_map: Dict[str, Dict[str, Any]] = {}
        self._last_selection_meta: Dict[str, Any] = {}
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
        # Backward compatibility: fixed stocks can be defined at root-level "stocks".
        raw_stocks = section.get("stocks")
        if raw_stocks is None:
            raw_stocks = data.get("stocks", [])
        if not isinstance(raw_stocks, list):
            raw_stocks = []
        cfg = UniverseSelectionConfig(
            selection_method=str(section.get("selection_method", "fixed")).lower(),
            max_stocks=int(section.get("max_stocks", 5)),
            min_volume=float(section.get("min_volume", 1_000_000_000)),
            min_market_cap=float(section.get("min_market_cap", 1_000)),
            min_atr_pct=float(section.get("min_atr_pct", 1.0)),
            max_atr_pct=float(section.get("max_atr_pct", 8.0)),
            atr_period=int(section.get("atr_period", 14)),
            volume_top_n=int(section.get("volume_top_n", 50)),
            stock_quota=int(section.get("stock_quota", 0)),
            etf_quota=int(section.get("etf_quota", 0)),
            etf_symbols=[str(x) for x in section.get("etf_symbols", [])],
            etf_name_keywords=[str(x) for x in section.get("etf_name_keywords", [])]
            or UniverseSelectionConfig().etf_name_keywords,
            exclude_management=bool(section.get("exclude_management", True)),
            allow_unknown_market_cap=bool(section.get("allow_unknown_market_cap", False)),
            fallback_to_fixed=bool(section.get("fallback_to_fixed", True)),
            halt_on_fallback_in_real=bool(section.get("halt_on_fallback_in_real", False)),
            universe_cache_file=str(section.get("universe_cache_file", "data/universe_cache.json")),
            candidate_pool_mode=str(section.get("candidate_pool_mode", "yaml")).lower(),
            candidate_stocks=[str(x) for x in section.get("candidate_stocks", [])],
            stocks=[str(x) for x in raw_stocks],
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

    def _normalize_symbol_list(self, symbols: Iterable[Any]) -> List[str]:
        out: List[str] = []
        for value in symbols:
            code = str(value or "").strip()
            if not self._is_valid_code(code):
                continue
            out.append(code)
        return self._dedupe(out)

    def _set_last_selection_meta(
        self,
        candidate_symbols: Iterable[Any],
        pre_limit_symbols: Iterable[Any],
        selected_symbols: Iterable[Any],
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._last_selection_meta = {
            "candidate_symbols": self._normalize_symbol_list(candidate_symbols),
            "pre_limit_symbols": self._normalize_symbol_list(pre_limit_symbols),
            "selected_symbols": self._normalize_symbol_list(selected_symbols),
            "meta": dict(meta or {}),
        }

    def get_last_selection_meta(self) -> Dict[str, Any]:
        payload = self._last_selection_meta or {}
        return {
            "candidate_symbols": [str(x) for x in payload.get("candidate_symbols") or []],
            "pre_limit_symbols": [str(x) for x in payload.get("pre_limit_symbols") or []],
            "selected_symbols": [str(x) for x in payload.get("selected_symbols") or []],
            "meta": dict(payload.get("meta") or {}),
        }

    def _select_and_cache(self, now: datetime, method_suffix: str = "") -> List[str]:
        try:
            method = self.config.selection_method
            logger.info(f"[UNIVERSE] 재선정 시작: method={method}")
            self._last_selection_meta = {}
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
            snapshot = self.get_last_selection_meta()
            self._set_last_selection_meta(
                candidate_symbols=snapshot.get("candidate_symbols") or selected,
                pre_limit_symbols=snapshot.get("pre_limit_symbols") or selected,
                selected_symbols=validated,
                meta={**dict(snapshot.get("meta") or {}), "selection_method": method},
            )
            self._save_cache(
                now,
                validated,
                cache_method,
                selection_meta=self.get_last_selection_meta(),
            )
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
        selected = self.config.stocks[: self.config.max_stocks]
        self._set_last_selection_meta(
            candidate_symbols=selected,
            pre_limit_symbols=selected,
            selected_symbols=selected,
            meta={"strategy": "fixed"},
        )
        return selected

    def _select_volume_top(self, limit: int) -> List[str]:
        mode = self.config.candidate_pool_mode
        effective_limit = max(int(limit), 1)
        candidates = self._candidate_pool_for_volume_scan()
        self._last_volume_snapshot_map = {}
        snapshot_map: Dict[str, Dict[str, Any]] = {}
        pool_size = len(candidates)
        volume_source = "market_scan" if mode == "market" else "restricted_pool"
        candidate_source = self._last_market_codes_source if mode == "market" else "configured_pool"
        logger.info(
            f"[UNIVERSE] volume_top scope={volume_source}, pool_mode={mode}, "
            f"pool_size={pool_size}, limit={effective_limit}, "
            f"candidate_source={candidate_source}"
        )
        rows: List[Tuple[str, float]] = []
        bulk_ok = False
        data_source = "none"
        if mode == "market" and hasattr(self.kis_client, "get_market_top_by_trade_value"):
            try:
                top_n = min(
                    max(effective_limit * 5, self.config.max_stocks * 5),
                    max(int(self.config.market_scan_size), effective_limit),
                )
                market_rows = self.kis_client.get_market_top_by_trade_value(top_n=top_n)
                for snap in market_rows:
                    code = str(snap.get("code") or "").strip()
                    if not self._is_valid_code(code):
                        continue
                    normalized = dict(snap)
                    normalized["code"] = code
                    stock_name = str(normalized.get("stock_name") or "").strip()
                    if stock_name:
                        normalized["stock_name"] = stock_name
                    if self._passes_safety_filters(normalized):
                        rows.append((code, float(normalized["trade_value"])))
                        snapshot_map[code] = normalized
                min_required = min(
                    max(int(effective_limit), 1),
                    max(int(self.config.max_stocks), 1),
                    max(int(pool_size), 1),
                )
                if len(rows) < min_required:
                    logger.warning(
                        "[UNIVERSE] volume_rank_api 결과 부족: passed=%s, required>=%s -> bulk_snapshot 재시도",
                        len(rows),
                        min_required,
                    )
                    rows = []
                    bulk_ok = False
                else:
                    bulk_ok = True
                    data_source = "volume_rank_api"
            except Exception:
                bulk_ok = False

        if not bulk_ok and hasattr(self.kis_client, "get_market_snapshot_bulk"):
            try:
                bulk = self.kis_client.get_market_snapshot_bulk(candidates)
                for snap in bulk:
                    code = str(snap.get("code") or "").strip()
                    if not self._is_valid_code(code):
                        continue
                    normalized = dict(snap)
                    normalized["code"] = code
                    stock_name = str(normalized.get("stock_name") or "").strip()
                    if stock_name:
                        normalized["stock_name"] = stock_name
                    if self._passes_safety_filters(normalized):
                        rows.append((code, float(normalized["trade_value"])))
                        snapshot_map[code] = normalized
                bulk_ok = True
                data_source = "bulk_snapshot"
            except Exception:
                bulk_ok = False

        if not bulk_ok:
            scan_window = min(
                len(candidates),
                max(int(self.config.market_scan_size), effective_limit * 5, self.config.max_stocks * 5),
            )
            scan_candidates = candidates[:scan_window]
            logger.info(
                f"[UNIVERSE] volume_top fallback snapshot scan: candidates={len(scan_candidates)}"
            )
            for idx, code in enumerate(scan_candidates):
                try:
                    snap = self._snapshot_for_symbol(code)
                    if not self._passes_safety_filters(snap):
                        continue
                    rows.append((code, snap["trade_value"]))
                    snapshot_map[code] = dict(snap)
                    # rate limit friendly
                    if mode == "market":
                        time.sleep(0.12)
                    elif idx % 10 == 0:
                        time.sleep(0.05)
                except Exception:
                    continue
            data_source = "single_snapshot"
        rows.sort(key=lambda x: x[1], reverse=True)
        selected = [c for c, _ in rows[:effective_limit]]
        self._last_volume_snapshot_map = {
            code: dict(snapshot_map.get(code) or {})
            for code in selected
            if code in snapshot_map
        }
        self._last_volume_data_source = data_source
        self._set_last_selection_meta(
            candidate_symbols=selected,
            pre_limit_symbols=selected,
            selected_symbols=selected,
            meta={
                "strategy": "volume_top",
                "pool_size": int(pool_size),
                "effective_limit": int(effective_limit),
                "data_source": str(data_source),
            },
        )
        logger.info(f"[UNIVERSE] volume_top data_source={data_source}")
        logger.info(f"[UNIVERSE] volume_top 통과={len(selected)}")
        return selected

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
        limited = selected[: self.config.max_stocks]
        self._set_last_selection_meta(
            candidate_symbols=pool,
            pre_limit_symbols=selected,
            selected_symbols=limited,
            meta={"strategy": "atr_filter"},
        )
        return limited

    def _select_combined(self) -> List[str]:
        stage1_limit = max(int(self.config.volume_top_n), self.config.max_stocks)
        first_stage = self._select_volume_top(stage1_limit)
        logger.info(f"[UNIVERSE] combined stage1={len(first_stage)} (limit={stage1_limit})")
        stage1_rank = {code: idx for idx, code in enumerate(first_stage)}
        second_stage: List[Dict[str, Any]] = []
        for code in first_stage:
            metrics = self._evaluate_combined_candidate(code)
            if metrics is None:
                continue
            second_stage.append(metrics)
        logger.info(f"[UNIVERSE] combined stage2={len(second_stage)}")
        if (
            self.config.candidate_pool_mode == "yaml"
            and len(second_stage) > 0
            and self._dedupe([str(row["code"]) for row in second_stage])
            == self._dedupe(self.config.candidate_stocks)[: len(second_stage)]
        ):
            logger.info(
                "[UNIVERSE] restricted pool 모드(yaml): 최종 선정이 candidate_stocks와 동일합니다."
            )
        ranked = sorted(
            second_stage,
            key=lambda row: (
                -float(row.get("trend_score") or 0.0),
                stage1_rank.get(str(row.get("code")), 10**9),
            ),
        )
        ranked_codes = [str(row["code"]) for row in ranked]
        limited, quota_meta = self._apply_asset_type_quota(ranked)
        self._set_last_selection_meta(
            candidate_symbols=first_stage,
            pre_limit_symbols=ranked_codes,
            selected_symbols=limited,
            meta={
                "strategy": "combined",
                "stage1_count": len(first_stage),
                "stage2_count": len(second_stage),
                "stage1_limit": stage1_limit,
                "rank_basis": "trend_score_then_stage1_rank",
                **quota_meta,
                "top_ranked": [
                    {
                        "code": str(row.get("code")),
                        "stock_name": str(row.get("stock_name") or ""),
                        "trend_score": round(float(row.get("trend_score") or 0.0), 2),
                        "adx": round(float(row.get("adx") or 0.0), 2),
                        "trend_up": bool(row.get("trend_up")),
                        "breakout": bool(row.get("breakout")),
                        "is_etf": bool(row.get("is_etf")),
                    }
                    for row in ranked[: min(5, len(ranked))]
                ],
            },
        )
        return limited

    # ----------------------------
    # Helpers
    # ----------------------------
    def _apply_asset_type_quota(self, ranked: List[Dict[str, Any]]) -> Tuple[List[str], Dict[str, Any]]:
        max_stocks = max(int(self.config.max_stocks), 0)
        ranked_codes = [str(row.get("code") or "") for row in ranked]
        stock_quota = max(int(self.config.stock_quota), 0)
        etf_quota = max(int(self.config.etf_quota), 0)

        if stock_quota == 0 and etf_quota == 0:
            return ranked_codes[:max_stocks], {
                "asset_quota_enabled": False,
                "stock_quota": 0,
                "etf_quota": 0,
                "stage2_stock_count": 0,
                "stage2_etf_count": 0,
                "selected_stock_count": 0,
                "selected_etf_count": 0,
            }

        target_total = stock_quota + etf_quota
        if target_total <= 0:
            return ranked_codes[:max_stocks], {
                "asset_quota_enabled": False,
                "stock_quota": stock_quota,
                "etf_quota": etf_quota,
                "stage2_stock_count": 0,
                "stage2_etf_count": 0,
                "selected_stock_count": 0,
                "selected_etf_count": 0,
            }

        # Quotas define priority allocation by asset type. Final count should still
        # target max_stocks (universe_size) when positive, with ranked backfill.
        final_target = target_total if max_stocks <= 0 else max_stocks
        stage2_etf_rows = [row for row in ranked if self._row_is_etf_candidate(row)]
        stage2_stock_rows = [row for row in ranked if not self._row_is_etf_candidate(row)]
        selected_rows = stage2_stock_rows[:stock_quota] + stage2_etf_rows[:etf_quota]
        selected_set = {
            str(row.get("code") or "")
            for row in selected_rows
            if self._is_valid_code(str(row.get("code") or ""))
        }

        if len(selected_set) < final_target:
            for row in ranked:
                code = str(row.get("code") or "")
                if not self._is_valid_code(code) or code in selected_set:
                    continue
                selected_rows.append(row)
                selected_set.add(code)
                if len(selected_set) >= final_target:
                    break

        # 쿼터 선발은 자산군 분리 우선이지만, 최종 결과는 원래 ranked 순서를 유지합니다.
        ranked_codes = [
            str(row.get("code") or "")
            for row in ranked
            if self._is_valid_code(str(row.get("code") or ""))
        ]
        limited = [code for code in ranked_codes if code in selected_set][:final_target]
        ranked_rows_by_code = {
            str(row.get("code") or ""): row
            for row in ranked
            if self._is_valid_code(str(row.get("code") or ""))
        }
        selected_etf_count = sum(
            1
            for code in limited
            if self._row_is_etf_candidate(ranked_rows_by_code.get(code) or {"code": code})
        )
        selected_stock_count = max(len(limited) - selected_etf_count, 0)

        logger.info(
            "[UNIVERSE] asset quota applied: stock=%s/%s, etf=%s/%s, total=%s",
            selected_stock_count,
            stock_quota,
            selected_etf_count,
            etf_quota,
            len(limited),
        )

        return limited, {
            "asset_quota_enabled": True,
            "stock_quota": stock_quota,
            "etf_quota": etf_quota,
            "stage2_stock_count": len(stage2_stock_rows),
            "stage2_etf_count": len(stage2_etf_rows),
            "selected_stock_count": selected_stock_count,
            "selected_etf_count": selected_etf_count,
        }

    def _row_is_etf_candidate(self, row: Dict[str, Any]) -> bool:
        if "is_etf" in row:
            return bool(row.get("is_etf"))
        code = str(row.get("code") or "").strip()
        stock_name = str(row.get("stock_name") or "").strip() or self._stock_name_for_code(code)
        return self._is_etf_candidate(code, stock_name)

    def _stock_name_for_code(self, code: str) -> str:
        snapshot = self._last_volume_snapshot_map.get(code) or {}
        return str(snapshot.get("stock_name") or "").strip()

    def _is_etf_candidate(self, code: str, stock_name: str) -> bool:
        if code and code in set(self.config.etf_symbols or []):
            return True
        if not stock_name:
            return False
        upper_name = str(stock_name).upper()
        for token in self.config.etf_name_keywords or []:
            key = str(token or "").strip().upper()
            if key and key in upper_name:
                return True
        return False

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
        snapshot = self.get_last_selection_meta()
        self._set_last_selection_meta(
            candidate_symbols=snapshot.get("candidate_symbols") or fallback,
            pre_limit_symbols=snapshot.get("pre_limit_symbols") or fallback,
            selected_symbols=fallback,
            meta={**dict(snapshot.get("meta") or {}), "strategy": "fixed_fallback", "reason": str(reason)},
        )
        self._save_cache(
            datetime.now(KST),
            fallback,
            "fixed_fallback",
            selection_meta=self.get_last_selection_meta(),
        )
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
            stage1_limit = max(int(self.config.volume_top_n), self.config.max_stocks)
            return self._select_volume_top(stage1_limit)
        return self._dedupe(self.config.candidate_stocks or self.config.stocks)

    def _load_market_codes(self) -> List[str]:
        self._last_market_codes_source = "unknown"
        if hasattr(self.kis_client, "get_market_universe_codes"):
            try:
                codes = self.kis_client.get_market_universe_codes(limit=self.config.market_scan_size)
                if codes:
                    deduped = self._dedupe([str(c) for c in codes])
                    self._last_market_codes_source = "market_api"
                    logger.info(
                        f"[UNIVERSE] market code source=market_api, requested={self.config.market_scan_size}, "
                        f"received={len(deduped)}"
                    )
                    return deduped
                self._last_market_codes_source = "market_api_empty"
            except Exception as e:
                self._last_market_codes_source = "market_api_error"
                logger.warning(f"[UNIVERSE] market code source=market_api_error: {e}")
        fallback = self._load_kospi200_codes()
        self._last_market_codes_source = "fallback_kospi_seed"
        logger.info(
            f"[UNIVERSE] market code source=fallback_kospi_seed, fallback_count={len(fallback)}"
        )
        return fallback

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
            "028260", "034730", "015760", "017670", "003550",
            "018260", "009150", "000270", "011200", "047050",
            "003490", "032830", "006800", "010130", "011170",
            "010950", "000810", "000100", "086790", "138040",
            "009540", "001040", "036570", "003670", "000720",
            "008770", "042660", "071050", "241560", "282330",
            "316140", "302440", "034220", "090430", "329180",
        ]
        return seed

    def _snapshot_for_symbol(self, code: str) -> Dict[str, Any]:
        price = self.kis_client.get_current_price(code)
        stock_name = str(price.get("stock_name") or "").strip() or None
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
        market_cap = self._to_float(
            self._first_present(price, ["market_cap", "hts_avls", "stck_avls"], 0),
            0.0,
        )
        is_suspended = self._to_bool_flag(
            self._first_present(
                price,
                ["is_suspended", "suspended", "trht_yn", "halt_yn", "trading_halt_yn"],
                False,
            ),
            False,
        )
        is_management = self._to_bool_flag(
            self._first_present(
                price,
                ["is_management", "management_yn", "mang_issu_yn", "mang_issu_cls_code"],
                False,
            ),
            False,
        )
        return {
            "code": code,
            "stock_name": stock_name,
            "current_price": current_price,
            "open_price": open_price,
            "volume": volume,
            "trade_value": trade_value,
            "market_cap": market_cap,
            "is_suspended": is_suspended,
            "is_management": is_management,
            "pct_from_open": pct,
        }

    def _passes_safety_filters(self, snap: Dict[str, Any]) -> bool:
        if float(snap.get("trade_value") or 0.0) < self.config.min_volume:
            return False
        market_cap = self._to_float(snap.get("market_cap"), 0.0)
        if self.config.min_market_cap > 0:
            if market_cap <= 0 and not self.config.allow_unknown_market_cap:
                return False
            if market_cap > 0 and market_cap < self.config.min_market_cap:
                return False
        if self._to_bool_flag(snap.get("is_suspended"), False):
            return False
        if self.config.exclude_management and self._to_bool_flag(snap.get("is_management"), False):
            return False
        if abs(float(snap.get("pct_from_open") or 0.0)) >= 28.0:
            return False
        return True

    def _evaluate_combined_candidate(self, code: str) -> Optional[Dict[str, Any]]:
        df = self.kis_client.get_daily_ohlcv(code, period_type="D")
        atr_ratio = self._atr_ratio_pct_from_df(df)
        if atr_ratio is None:
            return None
        if not (self.config.min_atr_pct <= atr_ratio <= self.config.max_atr_pct):
            return None
        trend_score, trend_meta = self._trend_entry_score_from_df(df)
        stock_name = self._stock_name_for_code(code)
        return {
            "code": code,
            "stock_name": stock_name,
            "atr_ratio": atr_ratio,
            "trend_score": trend_score,
            "is_etf": self._is_etf_candidate(code, stock_name),
            **trend_meta,
        }

    def _atr_ratio_pct(self, code: str) -> Optional[float]:
        df = self.kis_client.get_daily_ohlcv(code, period_type="D")
        return self._atr_ratio_pct_from_df(df)

    def _atr_ratio_pct_from_df(self, df: Any) -> Optional[float]:
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

    def _trend_entry_score_from_df(self, df: Any) -> Tuple[float, Dict[str, Any]]:
        if df is None or len(df) < 20:
            return 0.0, {"adx": 0.0, "trend_up": False, "breakout": False, "prev_high": 0.0}
        closes = df["close"].astype(float).tolist()
        highs = df["high"].astype(float).tolist()
        lows = df["low"].astype(float).tolist()
        if len(closes) < 3:
            return 0.0, {"adx": 0.0, "trend_up": False, "breakout": False, "prev_high": 0.0}

        ma_period = min(20, len(closes) - 1)
        ma_value = sum(closes[-ma_period:]) / ma_period if ma_period > 0 else 0.0
        latest_close = closes[-1]
        prev_high = highs[-2] if len(highs) >= 2 else 0.0
        trend_up = ma_value > 0 and latest_close > ma_value
        breakout = prev_high > 0 and latest_close > prev_high
        adx = self._calculate_adx(highs, lows, closes, period=max(14, self.config.atr_period))

        # 진입 조건(상승 추세 + 고가 돌파 + 추세 강도)과 동일한 방향으로 점수화
        score = 0.0
        score += 120.0 if trend_up else -60.0
        score += 80.0 if breakout else -20.0
        score += min(max(adx, 0.0), 60.0)
        if ma_value > 0:
            score += max((latest_close / ma_value - 1.0) * 100.0, -10.0) * 1.5

        return score, {
            "adx": adx,
            "trend_up": trend_up,
            "breakout": breakout,
            "prev_high": prev_high,
            "ma": ma_value,
        }

    @staticmethod
    def _calculate_adx(
        highs: List[float],
        lows: List[float],
        closes: List[float],
        period: int = 14,
    ) -> float:
        if len(closes) < 3:
            return 0.0
        period = max(int(period), 2)
        alpha = 1.0 / float(period)
        atr_ema: Optional[float] = None
        plus_dm_ema: Optional[float] = None
        minus_dm_ema: Optional[float] = None
        adx_ema: Optional[float] = None

        for i in range(1, len(closes)):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            plus_dm = up_move if up_move > down_move and up_move > 0 else 0.0
            minus_dm = down_move if down_move > up_move and down_move > 0 else 0.0
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )

            if atr_ema is None:
                atr_ema = tr
                plus_dm_ema = plus_dm
                minus_dm_ema = minus_dm
            else:
                atr_ema += alpha * (tr - atr_ema)
                plus_dm_ema += alpha * (plus_dm - plus_dm_ema)
                minus_dm_ema += alpha * (minus_dm - minus_dm_ema)

            if not atr_ema or atr_ema <= 0:
                continue

            plus_di = 100.0 * (plus_dm_ema or 0.0) / atr_ema
            minus_di = 100.0 * (minus_dm_ema or 0.0) / atr_ema
            di_sum = plus_di + minus_di
            dx = 0.0 if di_sum <= 0 else (100.0 * abs(plus_di - minus_di) / di_sum)

            if adx_ema is None:
                adx_ema = dx
            else:
                adx_ema += alpha * (dx - adx_ema)

        return float(adx_ema or 0.0)

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

    def _save_cache(
        self,
        now: datetime,
        stocks: List[str],
        method: str,
        selection_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        market_open_refreshed = ("refresh_" in method) or ("intra_bootstrap" in method)
        snapshot = selection_meta or {}
        candidate_symbols = self._normalize_symbol_list(snapshot.get("candidate_symbols") or stocks)
        pre_limit_symbols = self._normalize_symbol_list(snapshot.get("pre_limit_symbols") or candidate_symbols)
        selected_symbols = self._normalize_symbol_list(snapshot.get("selected_symbols") or stocks)
        extra_meta = dict(snapshot.get("meta") or {})
        payload = {
            "date": now.strftime("%Y-%m-%d"),
            "stocks": stocks,
            "candidate_symbols": candidate_symbols,
            "pre_limit_symbols": pre_limit_symbols,
            "selected_symbols": selected_symbols,
            "selection_method": method,
            "saved_at": now.isoformat(),
            "cache_key": now.strftime("%Y-%m-%d"),
            "market_open_refreshed": market_open_refreshed,
        }
        if extra_meta:
            payload["selection_meta"] = extra_meta
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

    @staticmethod
    def _first_present(item: Dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
        for key in keys:
            if key in item and item.get(key) not in (None, ""):
                return item.get(key)
        return default

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

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
import types
import datetime as dt
from unittest.mock import patch

try:
    import pytz  # noqa: F401
except ModuleNotFoundError:
    # 테스트 환경 최소 의존성 보강 (localize 지원)
    class _FakeKST(dt.tzinfo):
        def utcoffset(self, _dt):
            return dt.timedelta(hours=9)

        def dst(self, _dt):
            return dt.timedelta(0)

        def tzname(self, _dt):
            return "KST"

        def localize(self, value):
            return value.replace(tzinfo=self)

    fake_pytz = types.ModuleType("pytz")
    fake_pytz.timezone = lambda _name: _FakeKST()
    sys.modules["pytz"] = fake_pytz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "universe"))
from universe_selector import UniverseSelectionConfig, UniverseSelector  # type: ignore
from utils.market_hours import KST


class _DummyKIS:
    def __init__(self):
        self._market_codes = [f"{i:06d}" for i in range(100001, 100021)]

    def get_current_price(self, stock_code):
        return {"current_price": 70000, "open_price": 69000, "volume": 100000}

    def get_daily_ohlcv(self, stock_code, period_type="D"):
        import pandas as pd
        rows = []
        price = 70000
        for i in range(30):
            rows.append(
                {
                    "date": f"2026-01-{i+1:02d}",
                    "open": price,
                    "high": price * 1.01,
                    "low": price * 0.99,
                    "close": price,
                    "volume": 100000,
                }
            )
        return pd.DataFrame(rows)

    def get_market_universe_codes(self, limit=200):
        return self._market_codes[:limit]

    def get_market_snapshot_bulk(self, codes):
        snaps = []
        for idx, code in enumerate(codes):
            trade_value = 5_000_000_000 - (idx * 100_000_000)
            snaps.append(
                {
                    "code": code,
                    "trade_value": trade_value,
                    "market_cap": 2000 if idx < 10 else 500,
                    "is_suspended": False,
                    "is_management": False,
                    "pct_from_open": 1.5,
                }
            )
        return snaps


class _DummyKISNoMarketCodes(_DummyKIS):
    def get_market_universe_codes(self, limit=200):
        return []


class _DummyKISVolumeRankEmpty(_DummyKIS):
    def get_market_top_by_trade_value(self, top_n=200):
        return []


class _DummyKISNoBulkUnknownCap(_DummyKIS):
    def get_market_snapshot_bulk(self, codes):
        raise RuntimeError("bulk snapshot unavailable")

    def get_current_price(self, stock_code):
        # market_cap/is_management/is_suspended intentionally missing
        return {"current_price": 70000, "open_price": 69000, "volume": 100000}


class UniverseSelectorFixedTests(unittest.TestCase):
    def test_fixed_mode_preserves_order_and_max(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = UniverseSelectionConfig(
                selection_method="fixed",
                max_stocks=3,
                stocks=["005930", "000660", "035420", "051910"],
                universe_cache_file=str(Path(td) / "universe_cache.json"),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            selected = selector.select()
            self.assertEqual(selected, ["005930", "000660", "035420"])

    def test_cache_schema_saved(self):
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "universe_cache.json"
            cfg = UniverseSelectionConfig(
                selection_method="fixed",
                max_stocks=1,
                stocks=["005930"],
                universe_cache_file=str(cache_path),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            selected = selector.select()
            self.assertEqual(selected, ["005930"])
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertIn("date", payload)
            self.assertEqual(payload["stocks"], ["005930"])
            self.assertIn("selection_method", payload)

    def test_from_yaml_reads_root_level_stocks_for_backward_compatibility(self):
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "universe_cache.json"
            yaml_path = Path(td) / "universe.yaml"
            yaml_path.write_text(
                json.dumps(
                    {
                        "universe": {
                            "selection_method": "fixed",
                            "max_stocks": 3,
                            "universe_cache_file": str(cache_path),
                        },
                        "stocks": ["005930", "000660", "035420"],
                    }
                ),
                encoding="utf-8",
            )

            fake_yaml = types.SimpleNamespace(safe_load=lambda stream: json.loads(stream.read()))
            with patch("universe_selector.yaml", new=fake_yaml):
                selector = UniverseSelector.from_yaml(str(yaml_path), kis_client=_DummyKIS(), db=None)
            selected = selector.select()
            self.assertEqual(selected, ["005930", "000660", "035420"])

    def test_combined_refresh_on_restart_rebuilds_cached_single_symbol(self):
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "universe_cache.json"
            today = datetime.now(KST)
            cache_path.write_text(
                json.dumps(
                    {
                        "date": today.strftime("%Y-%m-%d"),
                        "stocks": ["005930"],
                        "selection_method": "combined",
                        "saved_at": (today - timedelta(minutes=30)).isoformat(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            cfg = UniverseSelectionConfig(
                selection_method="combined",
                max_stocks=3,
                universe_cache_file=str(cache_path),
                cache_refresh_enabled=True,
                cache_refresh_on_restart=True,
                cache_refresh_methods=["combined"],
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            selector._is_market_hours = lambda now: True  # type: ignore
            selector._select_combined = lambda: ["005930", "000660", "035420"]  # type: ignore
            selected = selector.select()
            self.assertEqual(selected, ["005930", "000660", "035420"])

    def test_combined_cache_can_stay_fixed_when_refresh_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "universe_cache.json"
            today = datetime.now(KST)
            cache_path.write_text(
                json.dumps(
                    {
                        "date": today.strftime("%Y-%m-%d"),
                        "stocks": ["005930"],
                        "selection_method": "combined",
                        "saved_at": today.isoformat(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            cfg = UniverseSelectionConfig(
                selection_method="combined",
                max_stocks=3,
                universe_cache_file=str(cache_path),
                cache_refresh_enabled=False,
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            selector._is_market_hours = lambda now: True  # type: ignore
            selector._select_combined = lambda: ["000660", "035420", "051910"]  # type: ignore
            selected = selector.select()
            self.assertEqual(selected, ["005930"])

    def test_cache_method_mismatch_forces_reselect(self):
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "universe_cache.json"
            today = datetime.now(KST)
            cache_path.write_text(
                json.dumps(
                    {
                        "date": today.strftime("%Y-%m-%d"),
                        "stocks": ["005930"],
                        "selection_method": "fixed",
                        "saved_at": today.isoformat(),
                        "cache_key": today.strftime("%Y-%m-%d"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            cfg = UniverseSelectionConfig(
                selection_method="combined",
                max_stocks=3,
                universe_cache_file=str(cache_path),
                cache_refresh_enabled=False,
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            selector._is_market_hours = lambda now: True  # type: ignore
            selector._select_combined = lambda: ["000660", "035420", "051910"]  # type: ignore
            selected = selector.select()
            self.assertEqual(selected, ["000660", "035420", "051910"])

    def test_restricted_mode_keeps_yaml_pool_size(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = UniverseSelectionConfig(
                selection_method="volume_top",
                candidate_pool_mode="yaml",
                candidate_stocks=["005930", "000660", "035420", "035720", "051910"],
                max_stocks=5,
                universe_cache_file=str(Path(td) / "universe_cache.json"),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            candidates = selector._candidate_pool_for_volume_scan()
            self.assertEqual(len(candidates), 5)

    def test_market_mode_scans_and_limits_from_market_pool(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = UniverseSelectionConfig(
                selection_method="volume_top",
                candidate_pool_mode="market",
                max_stocks=5,
                min_volume=1_000_000_000,
                min_market_cap=1000,
                universe_cache_file=str(Path(td) / "universe_cache.json"),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            selected = selector._select_volume_top(limit=10)
            self.assertEqual(len(selected), 10)
            self.assertEqual(selector._last_market_codes_source, "market_api")

    def test_market_mode_fallbacks_to_bulk_when_volume_rank_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = UniverseSelectionConfig(
                selection_method="volume_top",
                candidate_pool_mode="market",
                max_stocks=5,
                min_volume=1_000_000_000,
                min_market_cap=1000,
                universe_cache_file=str(Path(td) / "universe_cache.json"),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKISVolumeRankEmpty(), db=None)
            selected = selector._select_volume_top(limit=10)
            self.assertEqual(len(selected), 10)
            self.assertEqual(selector._last_volume_data_source, "bulk_snapshot")

    def test_market_mode_falls_back_to_seed_codes_when_market_codes_empty(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = UniverseSelectionConfig(
                selection_method="volume_top",
                candidate_pool_mode="market",
                max_stocks=5,
                universe_cache_file=str(Path(td) / "universe_cache.json"),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKISNoMarketCodes(), db=None)
            _ = selector._candidate_pool_for_volume_scan()
            self.assertEqual(selector._last_market_codes_source, "fallback_kospi_seed")

    def test_fallback_seed_codes_are_extended_to_50(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = UniverseSelectionConfig(
                selection_method="fixed",
                max_stocks=5,
                universe_cache_file=str(Path(td) / "universe_cache.json"),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            codes = selector._load_kospi200_codes()
            self.assertGreaterEqual(len(codes), 50)

    def test_combined_stage1_can_exceed_max_stocks(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = UniverseSelectionConfig(
                selection_method="combined",
                candidate_pool_mode="market",
                max_stocks=5,
                market_scan_size=20,
                min_volume=1_000_000_000,
                min_market_cap=0,
                universe_cache_file=str(Path(td) / "universe_cache.json"),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            stage1 = selector._select_volume_top(limit=cfg.max_stocks * 3)
            self.assertEqual(len(stage1), 15)

    def test_combined_uses_volume_top_n_for_stage1_limit(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = UniverseSelectionConfig(
                selection_method="combined",
                max_stocks=5,
                volume_top_n=8,
                universe_cache_file=str(Path(td) / "universe_cache.json"),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            observed = {}

            def _fake_stage1(limit):
                observed["limit"] = limit
                return ["005930", "000660", "035420", "051910", "068270", "207940", "006400", "105560"]

            selector._select_volume_top = _fake_stage1  # type: ignore
            selector._evaluate_combined_candidate = lambda code: {  # type: ignore
                "code": code,
                "trend_score": 10.0,
                "adx": 25.0,
                "trend_up": True,
                "breakout": False,
            }

            selected = selector._select_combined()
            self.assertEqual(observed["limit"], 8)
            self.assertEqual(len(selected), 5)

    def test_atr_filter_market_pool_uses_volume_top_n(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = UniverseSelectionConfig(
                selection_method="atr_filter",
                candidate_pool_mode="market",
                max_stocks=5,
                volume_top_n=9,
                universe_cache_file=str(Path(td) / "universe_cache.json"),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            observed = {}

            def _fake_stage1(limit):
                observed["limit"] = limit
                return ["005930", "000660", "035420", "051910", "068270", "207940", "006400", "105560", "012330"]

            selector._select_volume_top = _fake_stage1  # type: ignore
            pool = selector._resolve_atr_candidate_pool()
            self.assertEqual(observed["limit"], 9)
            self.assertEqual(len(pool), 9)

    def test_combined_reranks_by_trend_score_not_stage1_order(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = UniverseSelectionConfig(
                selection_method="combined",
                max_stocks=2,
                volume_top_n=3,
                universe_cache_file=str(Path(td) / "universe_cache.json"),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            selector._select_volume_top = lambda limit: ["005930", "000660", "035420"]  # type: ignore
            score_map = {"005930": 10.0, "000660": 95.0, "035420": 60.0}
            selector._evaluate_combined_candidate = lambda code: {  # type: ignore
                "code": code,
                "trend_score": score_map[code],
                "adx": 20.0,
                "trend_up": True,
                "breakout": code == "000660",
            }

            selected = selector._select_combined()
            self.assertEqual(selected, ["000660", "035420"])

    def test_combined_applies_stock_etf_quota(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = UniverseSelectionConfig(
                selection_method="combined",
                max_stocks=8,
                volume_top_n=8,
                stock_quota=5,
                etf_quota=3,
                universe_cache_file=str(Path(td) / "universe_cache.json"),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            selector._select_volume_top = lambda limit: [  # type: ignore
                "005930", "000660", "035420", "051910", "006400", "069500", "229200", "396500"
            ]
            metrics_map = {
                "005930": {"trend_score": 250.0, "is_etf": False},
                "000660": {"trend_score": 240.0, "is_etf": False},
                "035420": {"trend_score": 230.0, "is_etf": False},
                "051910": {"trend_score": 220.0, "is_etf": False},
                "006400": {"trend_score": 210.0, "is_etf": False},
                "069500": {"trend_score": 205.0, "is_etf": True},
                "229200": {"trend_score": 200.0, "is_etf": True},
                "396500": {"trend_score": 195.0, "is_etf": True},
            }
            selector._evaluate_combined_candidate = lambda code: {  # type: ignore
                "code": code,
                "stock_name": code,
                "trend_score": metrics_map[code]["trend_score"],
                "is_etf": metrics_map[code]["is_etf"],
                "adx": 25.0,
                "trend_up": True,
                "breakout": True,
            }

            selected = selector._select_combined()
            meta = selector.get_last_selection_meta().get("meta", {})
            self.assertEqual(len(selected), 8)
            self.assertEqual(int(meta.get("selected_stock_count", 0)), 5)
            self.assertEqual(int(meta.get("selected_etf_count", 0)), 3)

    def test_combined_quota_shortage_fills_from_remaining_ranked(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = UniverseSelectionConfig(
                selection_method="combined",
                max_stocks=8,
                volume_top_n=8,
                stock_quota=5,
                etf_quota=3,
                universe_cache_file=str(Path(td) / "universe_cache.json"),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            selector._select_volume_top = lambda limit: [  # type: ignore
                "005930", "000660", "035420", "051910", "006400", "069500", "229200", "114800"
            ]
            metrics_map = {
                "005930": {"trend_score": 250.0, "is_etf": False},
                "000660": {"trend_score": 240.0, "is_etf": False},
                "035420": {"trend_score": 230.0, "is_etf": False},
                "051910": {"trend_score": 220.0, "is_etf": False},
                "006400": {"trend_score": 210.0, "is_etf": False},
                "069500": {"trend_score": 205.0, "is_etf": True},
                "229200": {"trend_score": 200.0, "is_etf": True},
                "114800": {"trend_score": 195.0, "is_etf": False},
            }
            selector._evaluate_combined_candidate = lambda code: {  # type: ignore
                "code": code,
                "stock_name": code,
                "trend_score": metrics_map[code]["trend_score"],
                "is_etf": metrics_map[code]["is_etf"],
                "adx": 25.0,
                "trend_up": True,
                "breakout": True,
            }

            selected = selector._select_combined()
            meta = selector.get_last_selection_meta().get("meta", {})
            self.assertEqual(len(selected), 8)
            self.assertEqual(int(meta.get("selected_etf_count", 0)), 2)
            self.assertEqual(int(meta.get("selected_stock_count", 0)), 6)

    def test_combined_quota_shortage_preserves_rank_order(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = UniverseSelectionConfig(
                selection_method="combined",
                max_stocks=8,
                volume_top_n=8,
                stock_quota=5,
                etf_quota=3,
                universe_cache_file=str(Path(td) / "universe_cache.json"),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            selector._select_volume_top = lambda limit: [  # type: ignore
                "005930", "000660", "035420", "051910", "006400", "114800", "069500", "229200"
            ]
            metrics_map = {
                "005930": {"trend_score": 250.0, "is_etf": False},
                "000660": {"trend_score": 240.0, "is_etf": False},
                "035420": {"trend_score": 230.0, "is_etf": False},
                "051910": {"trend_score": 220.0, "is_etf": False},
                "006400": {"trend_score": 210.0, "is_etf": False},
                "114800": {"trend_score": 208.0, "is_etf": False},
                "069500": {"trend_score": 205.0, "is_etf": True},
                "229200": {"trend_score": 200.0, "is_etf": True},
            }
            selector._evaluate_combined_candidate = lambda code: {  # type: ignore
                "code": code,
                "stock_name": code,
                "trend_score": metrics_map[code]["trend_score"],
                "is_etf": metrics_map[code]["is_etf"],
                "adx": 25.0,
                "trend_up": True,
                "breakout": True,
            }

            selected = selector._select_combined()
            self.assertEqual(
                selected,
                ["005930", "000660", "035420", "051910", "006400", "114800", "069500", "229200"],
            )

    def test_unknown_market_cap_is_blocked_when_strict(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = UniverseSelectionConfig(
                selection_method="volume_top",
                candidate_pool_mode="yaml",
                candidate_stocks=["005930", "000660", "035420"],
                max_stocks=3,
                min_market_cap=1000,
                allow_unknown_market_cap=False,
                universe_cache_file=str(Path(td) / "universe_cache.json"),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKISNoBulkUnknownCap(), db=None)
            selected = selector._select_volume_top(limit=3)
            self.assertEqual(selected, [])

    def test_unknown_market_cap_can_pass_when_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = UniverseSelectionConfig(
                selection_method="volume_top",
                candidate_pool_mode="yaml",
                candidate_stocks=["005930", "000660", "035420"],
                max_stocks=3,
                min_market_cap=1000,
                allow_unknown_market_cap=True,
                universe_cache_file=str(Path(td) / "universe_cache.json"),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKISNoBulkUnknownCap(), db=None)
            selected = selector._select_volume_top(limit=3)
            self.assertEqual(len(selected), 3)


if __name__ == "__main__":
    unittest.main()

import datetime as dt
import sys
import types
import unittest
from types import SimpleNamespace
from pathlib import Path


def _ensure_fake_dependencies() -> None:
    if "pytz" not in sys.modules:
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

    if "pandas" not in sys.modules:
        fake_pd = types.ModuleType("pandas")
        fake_pd.DataFrame = object
        fake_pd.Series = object
        fake_pd.isna = lambda _x: False
        sys.modules["pandas"] = fake_pd

    if "numpy" not in sys.modules:
        fake_np = types.ModuleType("numpy")
        fake_np.nan = float("nan")
        fake_np.where = lambda cond, a, b: a
        sys.modules["numpy"] = fake_np

    if "dotenv" not in sys.modules:
        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = lambda *args, **kwargs: None
        sys.modules["dotenv"] = fake_dotenv

    if "requests" not in sys.modules:
        fake_requests = types.ModuleType("requests")

        class _DummySession:
            def request(self, *args, **kwargs):
                raise RuntimeError("dummy requests session should not be used in this test")

        class _DummyResponse:
            status_code = 200

        fake_requests.Session = _DummySession
        fake_requests.Response = _DummyResponse
        fake_exceptions = types.ModuleType("requests.exceptions")
        fake_exceptions.RequestException = Exception
        fake_exceptions.Timeout = Exception
        fake_exceptions.ConnectionError = Exception
        fake_requests.exceptions = fake_exceptions
        sys.modules["requests"] = fake_requests
        sys.modules["requests.exceptions"] = fake_exceptions


_ensure_fake_dependencies()
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine.multiday_executor import MultidayExecutor  # type: ignore  # noqa: E402


class _DummyMarketChecker:
    def is_tradeable(self):
        return False, "폐장 - 주문 불가"


class TestMultidayMarketClosedSkip(unittest.TestCase):
    def test_resync_mode_overrides_cbt_to_real_with_injected_real_api(self):
        mode = MultidayExecutor._resolve_resync_mode(
            trading_mode="CBT",
            api_obj=SimpleNamespace(is_paper_trading=False),
            api_was_injected=True,
        )
        self.assertEqual(mode, "REAL")

    def test_resync_mode_overrides_cbt_to_paper_with_injected_paper_api(self):
        mode = MultidayExecutor._resolve_resync_mode(
            trading_mode="CBT",
            api_obj=SimpleNamespace(is_paper_trading=True),
            api_was_injected=True,
        )
        self.assertEqual(mode, "PAPER")

    def test_resync_mode_keeps_cbt_without_injected_api(self):
        mode = MultidayExecutor._resolve_resync_mode(
            trading_mode="CBT",
            api_obj=SimpleNamespace(is_paper_trading=False),
            api_was_injected=False,
        )
        self.assertEqual(mode, "CBT")

    def test_run_once_skips_entry_signal_generation_when_market_closed_and_no_position(self):
        ex = MultidayExecutor.__new__(MultidayExecutor)
        ex.trading_mode = "PAPER"
        ex.stock_code = "005930"
        ex.market_checker = _DummyMarketChecker()
        ex.risk_manager = SimpleNamespace(
            check_kill_switch=lambda: SimpleNamespace(passed=True, should_exit=False, reason="")
        )
        ex.strategy = SimpleNamespace(has_position=False)
        ex._last_market_closed_skip_log_at = None

        def _unexpected_fetch_data():
            raise AssertionError("fetch_market_data should not be called when market is closed and no position")

        ex.fetch_market_data = _unexpected_fetch_data

        result = ex.run_once()
        self.assertIn("market_closed_skip", result["error"])

    def test_can_place_orders_allows_real_mode(self):
        ex = MultidayExecutor.__new__(MultidayExecutor)
        ex.trading_mode = "REAL"
        self.assertTrue(ex._can_place_orders())

    def test_can_place_orders_blocks_cbt_mode(self):
        ex = MultidayExecutor.__new__(MultidayExecutor)
        ex.trading_mode = "CBT"
        self.assertFalse(ex._can_place_orders())

    def test_restore_position_sends_info_on_auto_recovery(self):
        info_messages = []
        warning_messages = []

        ex = MultidayExecutor.__new__(MultidayExecutor)
        ex.order_synchronizer = SimpleNamespace(recover_pending_orders=lambda: [])
        ex.position_resync = SimpleNamespace(
            synchronize_on_startup=lambda: {
                "success": True,
                "position": None,
                "action": "AUTO_RECOVERED_CLEARED",
                "warnings": [],
                "recoveries": ["API 기준 자동복구: 저장 포지션 정리(005930)"],
            }
        )
        ex._pending_exit_state = None
        ex.telegram = SimpleNamespace(
            notify_warning=lambda msg: warning_messages.append(msg),
            notify_info=lambda msg: info_messages.append(msg),
            notify_error=lambda *args, **kwargs: None,
            notify_position_restored=lambda **kwargs: None,
        )

        MultidayExecutor._pending_recovery_done = False
        MultidayExecutor._pending_recovery_count = 0

        restored = ex.restore_position_on_start()

        self.assertFalse(restored)
        self.assertEqual(len(warning_messages), 0)
        self.assertEqual(len(info_messages), 1)
        self.assertIn("포지션 자동복구 완료", info_messages[0])

    def test_run_once_routes_buy_with_foreign_signal_enum_instance(self):
        ex = MultidayExecutor.__new__(MultidayExecutor)
        ex.trading_mode = "PAPER"
        ex.stock_code = "069500"
        ex.market_checker = SimpleNamespace(is_tradeable=lambda: (True, "정규장"))
        ex.risk_manager = SimpleNamespace(
            check_kill_switch=lambda: SimpleNamespace(passed=True, should_exit=False, reason="")
        )
        ex.api = SimpleNamespace()
        ex._entry_allowed = True
        ex._entry_block_reason = ""
        ex._last_market_closed_skip_log_at = None
        ex.fetch_market_data = lambda: SimpleNamespace(empty=False)
        ex.fetch_current_price = lambda: (86295.0, 86000.0)
        ex._persist_account_snapshot = lambda force=False: None
        ex._check_and_send_alerts = lambda _signal, _price: None
        ex._execute_exit_with_pending_control = lambda _signal: {"success": True, "message": "sell"}

        buy_calls = []
        ex.execute_buy = lambda _signal: (buy_calls.append("buy") or {"success": True, "message": "ok"})
        ex.strategy = SimpleNamespace(
            has_position=False,
            generate_signal=lambda **_kwargs: SimpleNamespace(
                # Enum class identity가 달라도 value가 BUY면 매수 분기로 라우팅되어야 함
                signal_type=SimpleNamespace(value="BUY"),
                price=86295.0,
                stop_loss=84000.0,
                take_profit=89000.0,
                trailing_stop=None,
                exit_reason=None,
                reason="foreign enum buy",
                atr=1000.0,
                trend=SimpleNamespace(value="UPTREND"),
            ),
        )

        result = ex.run_once()

        self.assertEqual(len(buy_calls), 1)
        self.assertTrue(result["order_result"]["success"])
        self.assertEqual(result["signal"]["type"], "BUY")


if __name__ == "__main__":
    unittest.main()

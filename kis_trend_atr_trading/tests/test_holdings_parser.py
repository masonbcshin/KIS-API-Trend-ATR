import logging
from decimal import Decimal
from types import SimpleNamespace
import threading
import time
from unittest.mock import patch

from api.kis_api import KISApi


def _build_api_with_payload(payload):
    api = KISApi(is_paper_trading=True)
    api._request_balance_payload = lambda: payload  # type: ignore[method-assign]
    return api


def test_parse_candidate_keys_from_output2():
    payload = {
        "output2": [
            {
                "prdt_no": "005930",
                "hold_qty": "3",
                "avg_buy_price": "70123.45",
                "name": "Samsung",
            }
        ]
    }
    api = _build_api_with_payload(payload)

    holdings = api.get_holdings()

    assert len(holdings) == 1
    assert holdings[0]["stock_code"] == "005930"
    assert holdings[0]["qty"] == 3
    assert holdings[0]["avg_price"] == Decimal("70123.45")
    assert holdings[0]["stock_name"] == "Samsung"


def test_qty_zero_filtered_and_empty_avg_safe():
    payload = {
        "output1": [
            {"pdno": "000660", "hldg_qty": "0", "pchs_avg_pric": "90000"},
            {"pdno": "005930", "hldg_qty": "2", "pchs_avg_pric": None},
            {"pdno": "229200", "hldg_qty": "1", "pchs_avg_pric": ""},
        ]
    }
    api = _build_api_with_payload(payload)

    holdings = api.get_holdings()

    assert [h["stock_code"] for h in holdings] == ["005930", "229200"]
    assert [h["qty"] for h in holdings] == [2, 1]
    assert holdings[0]["avg_price"] == Decimal("0")
    assert holdings[1]["avg_price"] == Decimal("0")


def test_array_path_is_resolved_before_key_candidates():
    payload = {
        "output1": [{"unknown_key": "x"}],  # 후보 키가 전혀 없는 잘못된 row
        "output2": [{"pdno": "005930", "hldg_qty": "1", "pchs_avg_pric": "70000"}],
    }
    api = _build_api_with_payload(payload)

    holdings = api.get_holdings()

    # output1 경로가 먼저 확정되므로 output2로 넘어가지 않고 빈 결과여야 함
    assert holdings == []


def test_empty_holdings_list_does_not_log_missing_path_warning(caplog):
    payload = {"output1": []}
    api = _build_api_with_payload(payload)

    with caplog.at_level(logging.WARNING, logger="kis_api"):
        holdings = api.get_holdings()

    assert holdings == []
    assert "보유 배열 경로를 찾지 못함" not in caplog.text


def test_get_holdings_reuses_ttl_cache_and_logs_cache_source(caplog):
    payload = {
        "output1": [{"pdno": "005930", "hldg_qty": "2", "pchs_avg_pric": "70000", "prdt_name": "Samsung"}]
    }
    api = _build_api_with_payload(payload)
    calls = {"count": 0}

    def _request_payload():
        calls["count"] += 1
        return payload

    api._request_balance_payload = _request_payload  # type: ignore[method-assign]
    api._holdings_cache_ttl_sec = 5.0

    with caplog.at_level(logging.INFO, logger="kis_api"):
        first = api.get_holdings()
        second = api.get_holdings()

    assert calls["count"] == 1
    assert first == second
    assert "실조회 결과 캐시 갱신" in caplog.text
    assert "[KIS][HOLDINGS] 캐시 재사용" in caplog.text


def test_balance_payload_inflight_requests_are_coalesced_across_instances(caplog):
    payload = {
        "rt_cd": "0",
        "output1": [{"pdno": "005930", "hldg_qty": "2", "pchs_avg_pric": "70000", "prdt_name": "Samsung"}],
        "output2": [{"tot_evlu_amt": "1000000", "dnca_tot_amt": "500000", "evlu_pfls_smtl_amt": "10000"}],
    }
    call_count = {"count": 0}
    call_lock = threading.Lock()
    errors = []
    results = []

    with patch.object(KISApi, "_wait_for_rate_limit", return_value=None):
        api1 = KISApi(app_key="k", app_secret="s", account_no="00000000", is_paper_trading=True)
        api2 = KISApi(app_key="k", app_secret="s", account_no="00000000", is_paper_trading=True)

    for api in (api1, api2):
        api.access_token = "token"
        api.token_expires_at = None
        api._balance_cache_ttl_sec = 5.0
        api._holdings_cache_ttl_sec = 0.0
        api._balance_raw_cache = None
        api._balance_raw_cache_ts = 0.0

    KISApi._shared_balance_payload_cache.clear()
    KISApi._shared_balance_payload_cache_ts.clear()
    KISApi._shared_balance_payload_inflight.clear()

    def _fake_request(self, method, url, headers, params=None):
        del self, method, url, headers, params
        with call_lock:
            call_count["count"] += 1
        time.sleep(0.05)
        return SimpleNamespace(json=lambda: payload)

    def _fetch(api):
        try:
            results.append(api.get_holdings())
        except Exception as exc:  # pragma: no cover - failure path assertion below
            errors.append(exc)

    with patch.object(KISApi, "_request_with_retry", new=_fake_request), \
         caplog.at_level(logging.INFO, logger="kis_api"):
        thread1 = threading.Thread(target=_fetch, args=(api1,))
        thread2 = threading.Thread(target=_fetch, args=(api2,))
        thread1.start()
        thread2.start()
        thread1.join(timeout=1.0)
        thread2.join(timeout=1.0)

    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    assert call_count["count"] == 1
    assert "[KIS][BAL_RAW] 실조회 수행" in caplog.text
    assert "coalesced 재사용" in caplog.text
    KISApi._shared_balance_payload_cache.clear()
    KISApi._shared_balance_payload_cache_ts.clear()
    KISApi._shared_balance_payload_inflight.clear()

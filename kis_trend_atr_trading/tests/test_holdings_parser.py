from decimal import Decimal

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

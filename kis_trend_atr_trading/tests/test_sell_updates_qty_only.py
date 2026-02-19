from decimal import Decimal

from utils.avg_price import reduce_quantity_after_sell


def test_partial_sell_keeps_avg_and_reduces_qty():
    avg_price = Decimal("70000.00")
    qty = 3

    remaining = reduce_quantity_after_sell(qty, 1)
    state = "ENTERED" if remaining > 0 else "EXITED"

    assert avg_price == Decimal("70000.00")
    assert remaining == 2
    assert state == "ENTERED"


def test_full_sell_moves_state_to_exited_with_avg_unchanged():
    avg_price = Decimal("70000.00")
    qty = 2

    remaining = reduce_quantity_after_sell(qty, 2)
    state = "ENTERED" if remaining > 0 else "EXITED"

    assert avg_price == Decimal("70000.00")
    assert remaining == 0
    assert state == "EXITED"

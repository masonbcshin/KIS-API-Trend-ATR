from decimal import Decimal

from utils.avg_price import calc_weighted_avg


def test_first_fill_uses_fill_price():
    avg = calc_weighted_avg(
        existing_avg=Decimal("0"),
        existing_qty=0,
        fill_price=Decimal("70000"),
        fill_qty=1,
    )
    assert avg == Decimal("70000.00")


def test_weighted_average_half_up_2dp():
    # 1주 70,000 + 2주 71,000 = 70,666.666... -> 70,666.67 (HALF_UP, 2dp)
    avg = calc_weighted_avg(
        existing_avg=Decimal("70000"),
        existing_qty=1,
        fill_price=Decimal("71000"),
        fill_qty=2,
    )
    assert avg == Decimal("70666.67")


def test_decimal_str_conversion_for_float_input():
    # float 입력도 Decimal(str(x)) 경로로 처리되어 2dp가 안정적으로 유지되어야 함
    avg = calc_weighted_avg(
        existing_avg=0,
        existing_qty=0,
        fill_price=70000.1,
        fill_qty=1,
    )
    assert avg == Decimal("70000.10")

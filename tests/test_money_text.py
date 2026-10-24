"""The text surfaces share the app's dollar formatting rules."""

from decimal import Decimal

import pytest

from hermes_finance.money import money, money2


@pytest.mark.parametrize("value, expected", [
    (1250, "$1,250"),
    (12.5, "$12.50"),
    (-1250, "-$1,250"),
    (-12.5, "-$12.50"),
    (0, "$0"),
    (-0.004, "$0"),
    (-0.0, "$0"),
    (12.999, "$13"),
    (-12.999, "-$13"),
    (12.004, "$12"),
    (12.005, "$12.01"),
    (-12.005, "-$12.01"),
    (1234567.5, "$1,234,567.50"),
    (Decimal("1250.00"), "$1,250"),
    (None, "-"),
])
def test_money(value, expected):
    assert money(value) == expected


@pytest.mark.parametrize("value, expected", [
    (1250, "$1,250.00"),
    (12.5, "$12.50"),
    (-1250, "-$1,250.00"),
    (-12.5, "-$12.50"),
    (-0.004, "$0.00"),
    (12.999, "$13.00"),
    (None, "-"),
])
def test_cents_and_money2(value, expected):
    assert money(value, cents=True) == expected
    assert money2(value) == expected


def test_none_placeholder_is_customizable():
    assert money(None, none="no cap") == "no cap"
    assert money2(None, none="—") == "—"


@pytest.mark.parametrize("amount, expected", [(12.5, "+$12.50"), (-12.5, "-$12.50"), (0, "+$0.00")])
def test_spending_change_keeps_an_explicit_sign(amount, expected):
    from hermes_finance.reads import spend_text

    period = {"kind": "month", "month": "2026-09", "start": "2026-09-01", "end": "2026-09-30"}
    payload = {"groups": [], "by": None, "pending": {"count": 0}, "period": period,
               "filters": {}, "total": 1250, "count": 1,
               "previous": {**period, "total": 1250 - amount}, "change": {"amount": amount, "pct": None}}
    assert f"({expected})" in spend_text(payload)


def test_display_helpers_share_the_formatter():
    from hermes_finance import chat, insights, report, syncnow, tooldefs, valuation

    for display in (report.money, chat._usd, insights.money_whole, syncnow.usd, tooldefs._money, valuation._money):
        assert display(-1250) == "-$1,250"
        assert display(12.999) == "$13"
        assert display(-0.004) == "$0"
    assert chat._usd(None) == tooldefs._money(None) == "no cap"
    assert syncnow.usd(None) == "—"

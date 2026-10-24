"""Shared dollar formatting for human-facing text; numeric payloads stay numeric."""

from decimal import Decimal, ROUND_HALF_UP


def money(value, cents: bool = False, none: str = "-") -> str:
    """Format dollars, rounding to cents before deciding whether the amount is whole.

    Round half up on the magnitude, as Intl.NumberFormat does. Suppress a negative
    sign when the rounded amount is zero. ``cents`` always keeps two decimals.
    """
    if value is None:
        return none
    amount = Decimal(str(value))
    if not amount.is_finite():
        return none
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if amount < 0 else ""
    magnitude = abs(amount)
    digits = 2 if cents or magnitude != magnitude.to_integral_value() else 0
    return f"{sign}${magnitude:,.{digits}f}"


def money2(value, none: str = "-") -> str:
    """Format dollars with two decimal places."""
    return money(value, cents=True, none=none)

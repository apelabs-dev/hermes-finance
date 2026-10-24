"""Hotfix 2026-09-29: a month-based bill's next charge is the same day of the month as its last one.

Before, next = last charge + the median interval in days, so the gym (Aug 1, Sep 1: a 31-day gap) landed on Oct 2
instead of Oct 1, and every monthly bill drifted a day or two each month."""
from datetime import date

from hermes_finance import insights as I


def _next(last, cadence, step, days=()):
    charges = [(date.fromisoformat(d), 1.0) for d in days] or [(last, 1.0)]
    return I._next_charge(last, cadence, step, charges)


def test_gym_aug1_sep1_is_due_oct1_not_oct2():
    assert _next(date(2026, 9, 1), "monthly", 31, ["2026-08-01", "2026-09-01"]) == date(2026, 10, 1)


def test_same_day_of_month_across_short_months():
    assert _next(date(2026, 1, 31), "monthly", 31, ["2025-12-31", "2026-01-31"]) == date(2026, 2, 28)  # no Feb 31
    assert _next(date(2026, 9, 30), "monthly", 30, ["2026-08-30", "2026-09-30"]) == date(2026, 10, 30)
    assert _next(date(2026, 8, 30), "bimonthly", 61, ["2026-06-30", "2026-08-30"]) == date(2026, 10, 30)
    assert _next(date(2026, 6, 14), "yearly", 365, ["2025-06-14", "2026-06-14"]) == date(2027, 6, 14)
    assert _next(date(2024, 2, 29), "yearly", 366, ["2023-02-28", "2024-02-29"]) == date(2025, 2, 28)


def test_one_charge_or_a_day_that_moves_keeps_the_interval():
    assert _next(date(2026, 8, 30), "bimonthly", 60) == date(2026, 10, 29)      # a single charge: no day proven yet
    assert _next(date(2026, 9, 12), "monthly", 31, ["2026-08-12", "2026-09-12"]) == date(2026, 10, 12)
    assert _next(date(2026, 9, 13), "monthly", 31, ["2026-08-12", "2026-09-13"]) == date(2026, 10, 14)  # moved a day


def test_week_based_cadences_keep_the_fixed_step():
    assert _next(date(2026, 9, 1), "weekly", 7) == date(2026, 9, 8)
    assert _next(date(2026, 9, 1), "biweekly", 14) == date(2026, 9, 15)


def test_a_bill_whose_day_drifts_keeps_its_interval():
    # every 55 days: Jun 10, Aug 4, Sep 28 -> no fixed day of the month, so last + 55
    assert _next(date(2026, 9, 28), "bimonthly", 55, ["2026-06-10", "2026-08-04", "2026-09-28"]) == date(2026, 11, 22)


def test_month_end_billing_stays_on_the_month_end():
    # Jul 31, Aug 31 (and a 30th in a 30-day month) count as the same day
    assert _next(date(2026, 8, 31), "monthly", 31, ["2026-06-30", "2026-07-31", "2026-08-31"]) == date(2026, 9, 30)

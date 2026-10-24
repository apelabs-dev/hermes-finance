"""insights.py on fixture DBs: month maths, pace, caps, suggestions, recurring, alerts, cash flow, net worth."""

from __future__ import annotations

from datetime import date

import pytest

from factory import acct, bank, basic, cap, snapshot, tx
from hermes_finance import insights

TODAY = date(2026, 9, 25)


@pytest.fixture
def c(conn):
    basic(conn)
    return conn


# --- month helpers ---------------------------------------------------------------------------------

@pytest.mark.parametrize("month,days", [("2026-02", 28), ("2028-02", 29), ("2026-04", 30), ("2026-03", 31),
                                        ("2026-12", 31)])
def test_days_in_month(month, days):
    assert insights.days_in_month(month) == days


def test_add_months_crosses_years():
    assert insights.add_months("2026-01", -1) == "2025-12"
    assert insights.add_months("2025-12", 1) == "2026-01"
    assert insights.add_months("2026-09", -12) == "2025-09"


@pytest.mark.parametrize("bad", ["2026-13", "2026-00", "26-09", "2026-9", "2026-09-01", "", None, "abcd-ef"])
def test_parse_month_rejects(bad):
    with pytest.raises(ValueError):
        insights.parse_month(bad)


# --- 1. month_summary ------------------------------------------------------------------------------

def test_month_summary_current_month(c):
    tx(c, "2026-09-01", 100, "Rent Co", cat="Rent & Utilities")
    tx(c, "2026-09-03", 40, "Trader Joe's", cat="Groceries")
    tx(c, "2026-09-03", -10, "Trader Joe's", cat="Groceries")           # refund nets
    tx(c, "2026-09-10", 25, "Chipotle", cat="Eating Out", pending=True)
    tx(c, "2026-09-12", 500, "To Savings", cat="Transfers")             # never spend
    tx(c, "2026-09-15", -2000, "Payroll", cat="Income")
    tx(c, "2026-08-02", 50, "Amazon", cat="Other Shopping")
    tx(c, "2026-08-25", 30, "Amazon", cat="Other Shopping")
    tx(c, "2026-08-31", 70, "Amazon", cat="Other Shopping")
    tx(c, "2026-08-15", -1500, "Payroll", cat="Income")

    s = insights.month_summary(c, "2026-09", TODAY)

    assert s["is_current"] and s["day"] == 25 and s["days_in_month"] == 30 and s["days_left"] == 5
    assert s["mtd"] == 155.0
    assert len(s["cum"]) == 25 and s["cum"][0] == 100.0 and s["cum"][2] == 130.0 and s["cum"][-1] == 155.0
    assert len(s["cum_prev"]) == 31 and s["cum_prev"][-1] == 150.0
    assert s["prev_same_day"] == 80.0                  # Aug 1-25
    assert s["delta"] == 75.0 and s["prev_total"] == 150.0
    assert s["income"] == 2000.0 and s["income_prev"] == 1500.0
    assert (s["pending_spend"], s["pending_count"]) == (25.0, 1)


def test_month_summary_boundaries_28_30_31(c):
    tx(c, "2026-01-31", 11, "A", cat="Other Shopping")
    tx(c, "2026-02-01", 5, "A", cat="Other Shopping")
    tx(c, "2026-02-28", 7, "A", cat="Other Shopping")
    tx(c, "2026-03-01", 3, "A", cat="Other Shopping")
    tx(c, "2026-03-31", 2, "A", cat="Other Shopping")
    tx(c, "2026-04-30", 9, "A", cat="Other Shopping")

    feb = insights.month_summary(c, "2026-02", date(2026, 3, 31))      # a finished 28-day month
    assert (feb["days_in_month"], feb["day"], feb["days_left"], len(feb["cum"])) == (28, 28, 0, 28)
    assert feb["mtd"] == 12.0 and len(feb["cum_prev"]) == 31 and feb["prev_total"] == 11.0
    assert feb["prev_same_day"] == 11.0                # finished month: compared with January's total

    mar = insights.month_summary(c, "2026-03", date(2026, 3, 31))      # 31 days after a 28-day month
    assert (mar["days_in_month"], mar["day"], len(mar["cum"]), len(mar["cum_prev"])) == (31, 31, 31, 28)
    assert mar["mtd"] == 5.0 and mar["prev_same_day"] == 12.0

    apr = insights.month_summary(c, "2026-04", date(2026, 4, 30))      # 30 days after a 31-day month
    assert (apr["days_in_month"], apr["day"], apr["days_left"]) == (30, 30, 0)
    assert apr["prev_same_day"] == 3.0                 # March 1-30: the 31st isn't the same day
    assert apr["mtd"] == 9.0

    early = insights.month_summary(c, "2026-03", date(2026, 3, 1))
    # a row dated after today still counts: it lands on today, so the chart ends at the month-to-date total
    assert early["day"] == 1 and early["cum"] == [5.0] == [early["mtd"]]
    assert early["prev_same_day"] == 5.0              # February 1

    future = insights.month_summary(c, "2026-05", date(2026, 4, 30))
    assert future["day"] == 0 and future["cum"] == [] and future["mtd"] == 0.0 and future["days_left"] == 31


def test_leap_february(c):
    tx(c, "2028-02-29", 4, "A", cat="Other Shopping")
    s = insights.month_summary(c, "2028-02", date(2028, 3, 5))
    assert s["days_in_month"] == 29 and len(s["cum"]) == 29 and s["mtd"] == 4.0


# --- 2. pace_projection ----------------------------------------------------------------------------

def _monthly(c, merchant, amount, days, cat):
    for d in days:
        tx(c, d, amount, merchant, cat=cat)


def test_pace_projection_parts_add_up(c):
    # rent (fixed category) + a monthly car payment (recurring merchant) + day-to-day spending
    _monthly(c, "Rent Co", 1500, ["2026-06-01", "2026-07-01", "2026-08-01", "2026-09-01"], "Rent & Utilities")
    _monthly(c, "Zenith Financial Services", 465.10, ["2026-06-05", "2026-07-05", "2026-08-05", "2026-09-05"],
             "Car payment")
    _monthly(c, "GEICO", 165.03, ["2026-06-26", "2026-07-26", "2026-08-26"], "Insurance")   # due Sep 26, not yet seen
    tx(c, "2026-09-02", 60, "Trader Joe's", cat="Groceries")
    tx(c, "2026-09-20", 140, "Amazon", cat="Other Shopping")

    p = insights.pace_projection(c, "2026-09", TODAY)

    assert p["mtd"] == 2165.10
    assert p["fixed_mtd"] == 1965.10                   # rent + the recurring car payment
    assert p["discretionary_mtd"] == 200.0
    assert (p["days_elapsed"], p["days_left"]) == (25, 5)
    assert p["daily_pace"] == 8.0 and p["pace_add"] == 40.0
    assert [b["merchant"] for b in p["bills_due"]] == ["GEICO"]
    assert p["bills_due_total"] == 165.03
    assert p["projection"] == round(p["mtd"] + p["pace_add"] + p["bills_due_total"], 2) == 2370.13


def test_pace_projection_past_month_is_the_total(c):
    tx(c, "2026-08-10", 90, "Amazon", cat="Other Shopping")
    p = insights.pace_projection(c, "2026-08", TODAY)
    assert p["days_left"] == 0 and p["pace_add"] == 0.0 and p["bills_due"] == []
    assert p["projection"] == p["mtd"] == 90.0


def test_pace_projection_counts_a_one_time_purchase_once(c):
    # $640 RAM off Reddit on day 6 is not "$107 a day for the rest of the month"
    tx(c, "2026-09-02", 60, "Trader Joe's", cat="Groceries")
    tx(c, "2026-09-06", 640, "Robert Nyari", cat="Other Shopping")
    p = insights.pace_projection(c, "2026-09", TODAY)
    assert p["one_offs"] == [{"date": "2026-09-06", "merchant": "Robert Nyari", "category": "Other Shopping",
                              "amount": 640.0}]
    assert p["one_off_total"] == 640.0
    assert p["discretionary_mtd"] == 60.0 and p["pace_add"] == 12.0          # 60 / 25 days * 5 left
    assert p["projection"] == 700.0 + 12.0
    assert insights.category_pace(c, "2026-09", TODAY)["Other Shopping"] == 640.0  # same rule per category


def test_pace_projection_keeps_a_regular_merchants_big_charge_in_the_pace(c):
    for d in ("2026-06-10", "2026-07-10", "2026-08-10"):
        tx(c, d, 120, "Costco", cat="Groceries")
    tx(c, "2026-09-05", 300, "Costco", cat="Groceries")            # big, but Costco is a regular
    tx(c, "2026-09-07", 299.99, "Best Buy", cat="Other Shopping")        # just under the threshold
    p = insights.pace_projection(c, "2026-09", TODAY)
    assert p["one_offs"] == [] and p["one_off_total"] == 0.0
    assert p["discretionary_mtd"] == 599.99


def test_pace_projection_empty_month(c):
    p = insights.pace_projection(c, "2026-09", date(2026, 9, 1))
    assert p["projection"] == 0.0 and p["daily_pace"] == 0.0


# --- 3. category_spend / 4. budgets ----------------------------------------------------------------

def test_category_spend_statuses(c):
    tx(c, "2026-09-03", 100, "CVS", cat="Other Health")            # cap 100 -> exactly 100%: over
    tx(c, "2026-09-03", 80, "Chipotle", cat="Eating Out")     # cap 100 -> exactly 80%: warn
    tx(c, "2026-09-03", 79.99, "Amazon", cat="Other Shopping")      # cap 100 -> ok
    tx(c, "2026-09-03", 45, "Shell", cat="Other Transport")         # no cap
    for m, v in (("2026-08", 30), ("2026-07", 60), ("2026-06", 90), ("2026-05", 999)):
        tx(c, f"{m}-10", v, "CVS", cat="Other Health")
    cap(c, "Other Health", 100)
    cap(c, "Eating Out", 100)
    cap(c, "Other Shopping", 100)

    cats = {x["category"]: x for x in insights.category_spend(c, "2026-09", TODAY)}

    assert (cats["Other Health"]["status"], cats["Other Health"]["pct"]) == ("over", 100.0)
    assert (cats["Eating Out"]["status"], cats["Eating Out"]["pct"]) == ("warn", 80.0)
    assert cats["Other Shopping"]["status"] == "ok"
    trans = cats["Other Transport"]
    assert (trans["status"], trans["pct"], trans["cap"]) == ("nocap", None, None)
    health = cats["Other Health"]
    assert health["last_month"] == 30.0 and health["avg3"] == 60.0   # May is outside the 3 months
    assert cats["Other Health"]["count"] == 1
    assert "Income" not in cats and "Transfers" not in cats
    assert list(cats)[0] == "Other Health"                          # sorted by spend


@pytest.mark.parametrize("spend,cap_,status", [(0, 100, "ok"), (79.99, 100, "ok"), (80, 100, "warn"),
                                               (99.99, 100, "warn"), (100, 100, "over"), (180, 225, "warn"),
                                               (0, 0, "ok"), (1, 0, "over"), (5, None, "nocap")])
def test_cap_status(spend, cap_, status):
    assert insights.cap_status(spend, cap_) == status


def test_budgets_left_to_spend_and_allowance(c):
    tx(c, "2026-09-03", 120, "CVS", cat="Other Health")        # cap 100: 20 over, which counts against what's left
    tx(c, "2026-09-03", 50, "Chipotle", cat="Eating Out")  # cap 250: 200 left
    tx(c, "2026-09-03", 10, "Shell", cat="Other Transport")    # no cap: ignored
    cap(c, "Other Health", 100)
    cap(c, "Eating Out", 250)

    b = insights.budgets(c, "2026-09", TODAY)
    assert b["count"] == 2 and [x["category"] for x in b["categories"]] == ["Other Health", "Eating Out"]
    assert b["left"] == 180.0 and b["days_left"] == 5 and b["per_day"] == 36.0    # (350 - 170) / 5
    assert (b["total_cap"], b["total_spend"], b["over"], b["warn"]) == (350.0, 170.0, 1, 0)
    assert (b["under"], b["over_amount"]) == (200.0, 20.0)

    last_day = insights.budgets(c, "2026-09", date(2026, 9, 30))
    assert last_day["days_left"] == 0 and last_day["per_day"] == 180.0      # all of it is for today
    past = insights.budgets(c, "2026-08", TODAY)
    assert past["per_day"] is None and past["left"] == 350.0


# The owner's real September (TASK-11 c/d, measured 2026-09-26): 7 caps, $1,940 budgeted, $1,649.33 spent in them.
SEPT_CAPS = {  # category: (cap, spent)
    "Other Transport": (800, 161.35), "Groceries": (300, 293.78), "Entertainment": (150, 12.79), "Travel": (100, 52.99),
    "Other Shopping": (200, 688.15), "Eating Out": (150, 197.21), "Other Health": (240, 243.06)}
SEPT_26 = date(2026, 9, 26)


def _real_september(c):
    for category, (value, spent) in SEPT_CAPS.items():
        tx(c, "2026-09-05", spent, f"{category} shop", cat=category)
        cap(c, category, value)


def test_budgets_left_counts_overspending_against_it(c):
    """(c) Left to spend is the net, budgeted - spent: the $538.42 over in Other Shopping, Eating Out and Other Health
    counts against it (it used to count 0, so the tile said $829.09 left while the ring said 85% used)."""
    _real_september(c)
    b = insights.budgets(c, "2026-09", SEPT_26)
    assert (b["count"], b["total_cap"], b["total_spend"]) == (7, 1940.0, 1649.33)
    assert b["left"] == round(b["total_cap"] - b["total_spend"], 2) == 290.67
    assert (b["under"], b["under_count"], b["over_amount"], b["over"]) == (829.09, 4, 538.42, 3)
    assert b["days_left"] == 4 and b["per_day"] == 72.67                  # about $73 a day, not $207
    lefts = {x["category"]: x["left"] for x in b["categories"]}
    assert lefts["Other Shopping"] == -488.15 and lefts["Other Transport"] == 638.65  # per category: cap - spend


def test_budgets_left_goes_negative_with_no_allowance(c):
    tx(c, "2026-09-03", 160, "CVS", cat="Other Health")
    cap(c, "Other Health", 100)
    b = insights.budgets(c, "2026-09", TODAY)
    assert b["left"] == -60.0 and b["per_day"] == 0.0 and b["over"] == 1 and b["over_amount"] == 60.0


def test_budgets_count_bills_still_due_in_capped_categories(c):
    """(d) GEICO ($165.03, expected Sep 26) and Zenith ($465.10, Sep 29) are recurring Other Transport bills not paid
    yet: only $8.52 of Other Transport is free, and the month is $339.46 over once they clear."""
    _real_september(c)
    _monthly(c, "GEICO", 165.03, ["2026-06-26", "2026-07-26", "2026-08-26"], "Other Transport")
    _monthly(c, "Zenith Financial Services", 465.10, ["2026-06-29", "2026-07-29", "2026-08-29"],
             "Other Transport")           # filed in Other Transport (from v16 the car-payment code is Car Payment)
    b = insights.budgets(c, "2026-09", SEPT_26)
    assert b["total_due"] == 630.13
    assert b["left"] == round(b["total_cap"] - b["total_spend"] - b["total_due"], 2) == -339.46
    assert b["per_day"] == 0.0
    assert [x["merchant"] for x in b["bills_due"]] == ["GEICO", "Zenith Financial Services"]
    t = next(x for x in b["categories"] if x["category"] == "Other Transport")
    assert (t["spend"], t["due"], t["left"]) == (161.35, 630.13, 8.52)
    assert t["status"] == "warn"                                           # 791.48 of 800: from spent + due
    assert (b["under"], b["over_amount"], b["over"]) == (198.96, 538.42, 3)
    # the same numbers everywhere a category's status shows: the category list and the cap alerts
    cats = {x["category"]: x for x in insights.category_spend(c, "2026-09", SEPT_26)}
    assert (cats["Other Transport"]["due"], cats["Other Transport"]["status"]) == (630.13, "warn")
    alert = next(a for a in insights.alerts(c, "2026-09", SEPT_26) if a.get("category") == "Other Transport")
    assert alert["level"] == "warn" and alert["due"] == 630.13
    assert alert["message"] == ("Other Transport is at 99% of its cap once bills clear: $161.35 spent + $630.13 still "
                                "due of $800")
    # a past month has no bills still due
    assert insights.budgets(c, "2026-08", SEPT_26)["total_due"] == 0.0


def test_no_caps(c):
    tx(c, "2026-09-03", 50, "Chipotle", cat="Eating Out")
    b = insights.budgets(c, "2026-09", TODAY)
    assert (b["count"], b["left"], b["categories"], b["over"]) == (0, 0.0, [], 0)
    assert all(x["status"] == "nocap" for x in insights.category_spend(c, "2026-09", TODAY))
    assert insights.alerts(c, "2026-09", TODAY) == []


# --- 5. suggest_caps -------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [(0.01, 25), (25, 25), (25.01, 50), (211, 225), (250, 250),
                                            (290, 300), (299.99, 300), (300, 300), (300.01, 350), (1234, 1250)])
def test_round_cap(value, expected):
    assert insights.round_cap(value) == expected


def test_suggest_caps_median_of_complete_months_and_read_only(c):
    for m, v in (("2026-06", 180), ("2026-07", 260), ("2026-08", 211)):   # median 211 -> 225
        tx(c, f"{m}-10", v, "Chipotle", cat="Eating Out")
    for m, v in (("2026-06", 900), ("2026-07", 1020), ("2026-08", 305)):  # median 900 -> 900
        tx(c, f"{m}-10", v, "Amazon", cat="Other Shopping")
    tx(c, "2026-09-10", 5000, "Amazon", cat="Other Shopping")                  # current month is ignored
    tx(c, "2026-05-10", 5000, "Chipotle", cat="Eating Out")              # older than 3 months
    tx(c, "2026-08-10", 40, "CVS", cat="Other Health")                         # median of [0, 0, 40] = 0
    cap(c, "Other Shopping", 700)
    before = c.total_changes

    s = insights.suggest_caps(c, 3, TODAY)

    assert c.total_changes == before
    assert s["months"] == ["2026-06", "2026-07", "2026-08"]
    by = {x["category"]: x for x in s["categories"]}
    assert by["Eating Out"]["values"] == [180.0, 260.0, 211.0]
    assert (by["Eating Out"]["median"], by["Eating Out"]["suggested"]) == (211.0, 225.0)
    assert (by["Other Shopping"]["suggested"], by["Other Shopping"]["cap"]) == (900.0, 700)
    assert by["Other Health"]["suggested"] is None and by["Other Health"]["values"] == [0.0, 0.0, 40.0]
    assert s["total"] == 1125.0
    assert "Income" not in by and "Transfers" not in by


def test_suggest_caps_skips_fixed_and_catch_all_categories(c):
    for m in ("2026-06", "2026-07", "2026-08"):
        tx(c, f"{m}-01", 2252, "Innago", cat="Rent & Utilities")
        tx(c, f"{m}-12", 900, "Misc", cat="Other")
        tx(c, f"{m}-15", 35, "Overdraft", cat="Fees")
        tx(c, f"{m}-20", 180, "Chipotle", cat="Eating Out")

    s = insights.suggest_caps(c, 3, TODAY)

    assert insights.NO_SUGGEST == {"Rent & Utilities", "Internet & Phone", "Other", "Fees"}
    assert not {x["category"] for x in s["categories"]} & insights.NO_SUGGEST
    assert s["total"] == 200.0                                           # only Eating Out: 180 -> 200


# --- 6. recurring ----------------------------------------------------------------------------------

def _rec(c, today=TODAY):
    return {r["merchant"]: r for r in insights.recurring(c, today)["items"]}


def test_recurring_monthly_weekly_yearly(c):
    for d in ("2026-05-12", "2026-06-12", "2026-07-12", "2026-08-12", "2026-09-12"):
        tx(c, d, 15.49, "Netflix", cat="Entertainment", logo="https://plaid-merchant-logos.plaid.com/netflix.png")
    for d in ("2026-08-28", "2026-09-04", "2026-09-11", "2026-09-18"):
        tx(c, d, 7.0 if d != "2026-09-11" else 8.0, "SpotHero", cat="Other Transport")
    tx(c, "2024-10-01", 12.0, "Namecheap", cat="Other")
    tx(c, "2025-10-01", 12.99, "Namecheap", cat="Other")

    r = _rec(c)

    nf = r["Netflix"]
    assert (nf["cadence"], nf["kind"], nf["category"], nf["amount"]) == ("monthly", "subscription", "Entertainment", 15.49)
    assert (nf["last_date"], nf["interval_days"], nf["next_expected"]) == ("2026-09-12", 31, "2026-10-12")   # hotfix 2026-09-29: the 12th, every month
    assert nf["paid_this_month"] and nf["status"] == "upcoming" and nf["charges"] == 5
    assert nf["logo_url"] == "https://plaid-merchant-logos.plaid.com/netflix.png"

    sh = r["SpotHero"]
    assert (sh["cadence"], sh["interval_days"], sh["next_expected"]) == ("weekly", 7, "2026-09-25")
    assert sh["status"] == "due_soon" and sh["days_until"] == 0
    assert sh["monthly_amount"] == round(7.0 * 52 / 12, 2)

    nc = r["Namecheap"]
    assert (nc["cadence"], nc["next_expected"], nc["status"], nc["days_until"]) == ("yearly", "2026-10-01", "upcoming", 6)
    assert nc["monthly_amount"] == round(12.99 / 12, 2)


def test_recurring_rejects_noisy_irregular_excluded_and_transfers(c):
    for d, a in (("2026-06-01", 10), ("2026-07-01", 60), ("2026-08-01", 25), ("2026-09-01", 90)):
        tx(c, d, a, "Noisy Co", cat="Entertainment")                       # monthly, but unstable amounts
    for d in ("2026-04-01", "2026-04-20", "2026-07-15", "2026-09-10"):
        tx(c, d, 20, "Random Shop", cat="Entertainment")                   # irregular intervals
    for d in ("2026-06-03", "2026-07-03", "2026-08-03", "2026-09-03"):
        tx(c, d, 150, "Costco", cat="Groceries")                           # excluded category
        tx(c, d, 200, "To Savings - 7731", cat="Other")                    # transfer-looking name
        tx(c, d, 50, "Emergency Fund Vault", cat="Other")
        tx(c, d, 300, "Card Payment", cat="Card payment")                  # Transfers
        tx(c, d, -2000, "Payroll", cat="Income")                           # money in
    assert _rec(c) == {}


def test_recurring_amount_stability_by_cv(c):
    # max/min = 1.4 (> 1.35) but the coefficient of variation is < 0.2
    for d, a in (("2026-04-10", 100), ("2026-05-10", 100), ("2026-06-10", 100), ("2026-07-10", 100),
                 ("2026-08-10", 100), ("2026-09-10", 140)):
        tx(c, d, a, "Gym", cat="Other Health")
    assert _rec(c)["Gym"]["cadence"] == "monthly"


def test_recurring_bill_kinds(c):
    days = ("2026-06-05", "2026-07-05", "2026-08-05", "2026-09-05")
    for d in days:
        tx(c, d, 2252, "Innago", cat="Rent & Utilities")
        tx(c, d, 465.10, "Zenith Financial Services", cat="Car payment")
        tx(c, d, 165.03, "GEICO", cat="Insurance")
        tx(c, d, 45.23, "AT&T", cat="Phone")
        tx(c, d, 20, "OpenAI", cat="Other")
    r = _rec(c)
    assert {m: x["kind"] for m, x in r.items()} == {"Innago": "bill", "Zenith Financial Services": "bill",
                                                     "GEICO": "bill", "AT&T": "bill", "OpenAI": "subscription"}
    totals = insights.recurring(c, TODAY)["monthly_totals"]
    assert totals == {"bill": round(2252 + 465.10 + 165.03 + 45.23, 2), "subscription": 20.0}


def test_recurring_late_and_stopped(c):
    for d in ("2026-05-10", "2026-06-10", "2026-07-10", "2026-08-10"):
        tx(c, d, 9.99, "Late Sub", cat="Entertainment")        # expected ~Sep 10, now 15 days late
    for d in ("2026-03-01", "2026-04-01", "2026-05-01", "2026-06-01"):
        tx(c, d, 9.99, "Cancelled Sub", cat="Entertainment")   # expected ~Jul 1: missed two cycles -> gone
    for d in ("2026-06-22", "2026-07-22", "2026-08-22"):
        tx(c, d, 5, "Soon Sub", cat="Entertainment")           # expected Sep 22: 3 days ago -> due soon, not late

    r = _rec(c)

    assert set(r) == {"Late Sub", "Soon Sub"}
    late = r["Late Sub"]
    assert (late["status"], late["next_expected"], late["paid_this_month"]) == ("late", "2026-09-10", False)
    assert late["days_until"] == -15
    assert (r["Soon Sub"]["status"], r["Soon Sub"]["days_until"]) == ("due_soon", -3)


def test_recurring_survives_a_merchant_rename(c):
    """(e) Plaid renamed Anomaly's merchant_name to "Opencode Anoma.ly" for the September charge (2026-09-16, $10).
    Grouped by merchant, the charge started a new group and Anomaly showed "late, not paid this month"."""
    today = date(2026, 9, 26)
    for d in ("2026-05-16", "2026-06-16", "2026-07-16", "2026-08-16"):
        tx(c, d, 10.0, "Anomaly", name="ANOMALY", cat="Other")
    tx(c, "2026-09-16", 10.0, "Opencode Anoma.ly", name="OPENCODE ANOMA.LY", cat="Other")

    r = _rec(c, today)

    assert list(r) == ["Opencode Anoma.ly"]                                    # one item, under its new name
    a = r["Opencode Anoma.ly"]
    assert (a["status"], a["paid_this_month"], a["last_date"], a["next_expected"], a["charges"]) == \
        ("upcoming", True, "2026-09-16", "2026-10-16", 5)       # hotfix 2026-09-29: the 16th, every month
    assert sorted(a["keys"]) == ["anomaly", "opencode anoma.ly"]
    assert insights.pace_projection(c, "2026-09", today)["bills_due"] == []   # nothing is still due from it
    assert insights.pace_projection(c, "2026-09", today)["fixed_mtd"] == 10.0  # its September charge isn't paced


def test_recurring_rename_needs_the_same_amount_and_no_overlap(c):
    today = date(2026, 9, 26)
    for d in ("2026-05-16", "2026-06-16", "2026-07-16", "2026-08-16"):
        tx(c, d, 10.0, "Anomaly", name="ANOMALY", cat="Other")
    tx(c, "2026-09-16", 25.0, "Opencode Anoma.ly", name="OPENCODE ANOMA.LY", cat="Other")   # +150%: not the same bill
    for d in ("2026-05-03", "2026-06-03", "2026-07-03", "2026-08-03", "2026-09-03"):
        tx(c, d, 9.99, "Acme Cloud", cat="Entertainment")
    for d in ("2026-06-20", "2026-07-20", "2026-08-20", "2026-09-20"):
        tx(c, d, 9.99, "Acme Storage", cat="Entertainment")                   # same word and price, both running
    for d in ("2026-06-10", "2026-07-10", "2026-08-10"):
        tx(c, d, 30.0, "Online Payment Svc", cat="Entertainment")
    tx(c, "2026-09-10", 30.0, "Payment Online Co", cat="Entertainment")         # shares only generic words

    r = _rec(c, today)

    assert r["Anomaly"]["status"] == "late" and "Opencode Anoma.ly" not in r
    assert r["Acme Cloud"]["charges"] == 5 and r["Acme Storage"]["charges"] == 4
    assert r["Online Payment Svc"]["status"] == "late"


def test_recurring_ignores_pending_duplicates_on_the_same_day(c):
    for d in ("2026-06-15", "2026-07-15", "2026-08-15", "2026-09-15"):
        tx(c, d, 10.99, "Spotify", cat="Entertainment")
    tx(c, "2026-09-15", 0.01, "Spotify", cat="Entertainment", pending=True)   # same-day charges are summed
    assert _rec(c)["Spotify"]["amount"] == 11.0


def test_recurring_ignores_a_one_off_extra_from_the_same_merchant(c):
    # Innago (rent) and OpenAI as synced: one extra charge each used to fail the amount-stability check
    for d, a in (("2026-02-02", 2202), ("2026-03-02", 2202), ("2026-03-04", 51), ("2026-04-01", 2252),
                 ("2026-05-03", 2252), ("2026-06-01", 2252), ("2026-07-01", 2252), ("2026-08-03", 2252),
                 ("2026-09-03", 2252)):
        tx(c, d, a, "Innago", cat="Rent & Utilities")
    for d, a in (("2026-05-23", 20), ("2026-06-23", 20), ("2026-07-23", 20), ("2026-07-31", 85.64),
                 ("2026-08-31", 20)):
        tx(c, d, a, "OpenAI", cat="Other")

    r = _rec(c)

    rent = r["Innago"]
    assert (rent["kind"], rent["cadence"], rent["amount"]) == ("bill", "monthly", 2252.0)
    assert (rent["last_date"], rent["interval_days"], rent["next_expected"]) == ("2026-09-03", 31, "2026-10-04")
    assert rent["paid_this_month"] and rent["charges"] == 7              # Mar-Sep core charges; the $51 isn't a cycle
    ai = r["OpenAI"]
    assert (ai["kind"], ai["cadence"], ai["amount"]) == ("subscription", "monthly", 20.0)
    assert (ai["last_date"], ai["next_expected"], ai["paid_this_month"]) == ("2026-08-31", "2026-10-01", False)
    assert insights.recurring(c, TODAY)["monthly_totals"] == {"bill": 2252.0, "subscription": 20.0}


def test_recurring_a_one_off_alone_does_not_mark_the_month_paid(c):
    for d, a in (("2026-05-05", 20), ("2026-06-05", 20), ("2026-07-05", 20), ("2026-08-05", 20),
                 ("2026-09-02", 85.64)):
        tx(c, d, a, "OpenAI", cat="Other")
    day = date(2026, 9, 4)

    ai = _rec(c, day)["OpenAI"]

    assert (ai["amount"], ai["last_date"], ai["next_expected"]) == (20.0, "2026-08-05", "2026-09-05")
    assert not ai["paid_this_month"] and ai["status"] == "due_soon"
    assert [b["merchant"] for b in insights.pace_projection(c, "2026-09", day)["bills_due"]] == ["OpenAI"]


def test_recurring_still_rejects_noisy_amounts_with_irregular_dates(c):
    for d, a in (("2026-04-03", 5), ("2026-04-29", 300), ("2026-06-11", 47), ("2026-07-02", 120),
                 ("2026-08-30", 18)):
        tx(c, d, a, "Noisy Mart", cat="Entertainment")
    assert _rec(c) == {}


@pytest.mark.parametrize("extras,found", [((10, 400), True),          # 3 of 5 charges are core: 60%
                                          ((10, 200, 400), False)])   # 3 of 6: 50%
def test_recurring_needs_60_percent_core_charges(c, extras, found):
    for d in ("2026-06-10", "2026-07-10", "2026-08-10"):
        tx(c, d, 50, "Mostly Extras", cat="Entertainment")
    for d, a in zip(("2026-06-20", "2026-07-25", "2026-09-01"), extras):
        tx(c, d, a, "Mostly Extras", cat="Entertainment")
    assert ("Mostly Extras" in _rec(c)) is found


# --- 7. alerts -------------------------------------------------------------------------------------

def test_alerts_caps_and_connections(conn):
    bank(conn, "item_ok", "Chase")
    bank(conn, "item_login", "Wells Fargo", status="login_required", error_code="ITEM_LOGIN_REQUIRED")
    bank(conn, "item_err", "Citi", status="error", error_code="INSTITUTION_DOWN")
    bank(conn, "item_gone", "Old Bank", status="login_required", error_code="ITEM_LOGIN_REQUIRED", removed=True)
    acct(conn, "acc_chk", "item_ok")
    tx(conn, "2026-09-03", 100, "CVS", cat="Other Health", item_id="item_ok")
    tx(conn, "2026-09-03", 85, "Chipotle", cat="Eating Out", item_id="item_ok")
    cap(conn, "Other Health", 90)
    cap(conn, "Eating Out", 100)
    cap(conn, "Other Shopping", 100)

    al = insights.alerts(conn, "2026-09", TODAY)

    by = {a["key"]: a for a in al}
    assert set(by) == {"cap:Other Health:2026-09:over", "cap:Eating Out:2026-09:warn",
                       "item:item_login:ITEM_LOGIN_REQUIRED", "item:item_err:INSTITUTION_DOWN"}
    assert by["cap:Other Health:2026-09:over"]["level"] == "over"
    assert by["cap:Eating Out:2026-09:warn"]["message"] == "Eating Out is at 85% of its cap: $85 of $100"
    login = by["item:item_login:ITEM_LOGIN_REQUIRED"]
    assert login["item_id"] == "item_login" and login["message"] == "Wells Fargo needs a re-login"
    assert login["kind"] == "connection" and login["level"] == "error"
    assert "connection problem (INSTITUTION_DOWN)" in by["item:item_err:INSTITUTION_DOWN"]["message"]


def test_alert_for_error_status_without_code(conn):
    bank(conn, "item_x", "Ally", status="error")
    (a,) = insights.alerts(conn, "2026-09", TODAY)
    assert a["key"] == "item:item_x:ERROR"


# --- 8. cashflow -----------------------------------------------------------------------------------

def test_cashflow_partial_coverage(conn):
    bank(conn, "item_old", "Nova")
    bank(conn, "item_new", "U.S. Bank")
    acct(conn, "acc_old", "item_old")
    acct(conn, "acc_new", "item_new")
    tx(conn, "2024-09-15", 10, "A", cat="Other Shopping", account_id="acc_old", item_id="item_old")
    tx(conn, "2026-04-15", -3000, "Payroll", cat="Income", account_id="acc_old", item_id="item_old")
    tx(conn, "2026-04-20", 1200, "Amazon", cat="Other Shopping", account_id="acc_old", item_id="item_old")
    tx(conn, "2026-04-21", 400, "To Savings", cat="Transfers", account_id="acc_old", item_id="item_old")
    tx(conn, "2026-09-01", 50, "Shell", cat="Other Transport", account_id="acc_new", item_id="item_new")
    tx(conn, "2026-09-02", -100, "Refund", cat="Income", account_id="acc_new", item_id="item_new")

    f = insights.cashflow(conn, 6, TODAY)

    months = f["months"]
    assert [m["month"] for m in months] == ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
    apr, sep = months[0], months[-1]
    assert (apr["income"], apr["spending"], apr["net"]) == (3000.0, 1200.0, 1800.0)
    assert (apr["coverage"], apr["missing"]) == ("partial", ["U.S. Bank"])
    assert all(m["coverage"] == "partial" for m in months[:5])
    assert (sep["coverage"], sep["missing"], sep["is_current"]) == ("full", [], True)   # starts on U.S. Bank's first day
    assert (sep["income"], sep["spending"]) == (100.0, 50.0)
    assert f["first_dates"] == {"Nova": "2024-09-15", "U.S. Bank": "2026-09-01"}
    assert not any(m["is_current"] for m in months[:5])


def test_cashflow_ends_at_a_chosen_month(conn):
    bank(conn)
    acct(conn)
    tx(conn, "2026-01-05", 20, "A", cat="Other Shopping")
    f = insights.cashflow(conn, 3, TODAY, end="2026-02")
    assert [m["month"] for m in f["months"]] == ["2025-12", "2026-01", "2026-02"]
    # history starts Jan 5, so December and January start before it; February is complete
    assert [m["coverage"] for m in f["months"]] == ["partial", "partial", "full"]
    assert f["months"][1]["missing"] == ["Chase"]


# --- 9. net worth ----------------------------------------------------------------------------------

def test_net_worth_groups_and_banks(conn):
    bank(conn, "item_a", "Nova")
    bank(conn, "item_b", "Chase")
    bank(conn, "item_c", "Gone Bank", removed=True)
    acct(conn, "chk", "item_a", current=1000.0)
    acct(conn, "sav", "item_a", subtype="savings", current=9000.0)
    acct(conn, "inv", "item_a", type_="investment", subtype="brokerage", current=500.0)
    acct(conn, "cc", "item_b", type_="credit", subtype="credit card", current=800.0, limit=5000.0)
    acct(conn, "loan", "item_b", type_="loan", subtype="auto", current=12000.0)
    acct(conn, "odd", "item_b", type_="other", subtype="other", current=77.0)
    acct(conn, "old", "item_c", current=5.0)

    nw = insights.net_worth(conn, history=False)

    assert (nw["assets"], nw["debts"], nw["net"]) == (10500.0, 12800.0, -2300.0)
    assert nw["groups"] == {"depository": 10000.0, "investment": 500.0, "credit": 800.0, "loan": 12000.0}
    banks = {b["institution_name"]: b for b in nw["institutions"]}
    assert set(banks) == {"Nova", "Chase"}
    assert banks["Nova"]["net"] == 10500.0 and banks["Chase"]["net"] == -12800.0
    signed = {a["account_id"]: a["signed"] for a in banks["Chase"]["accounts"]}
    assert signed == {"cc": -800.0, "loan": -12000.0, "odd": None}
    assert "history" not in nw


def test_net_worth_history_from_real_snapshots_only(conn):
    bank(conn)
    acct(conn, "chk", current=1200.0)
    acct(conn, "cc", type_="credit", subtype="credit card", current=300.0)
    assert insights.net_worth(conn)["history"] == [] and insights.net_worth(conn)["tracking_since"] is None

    snapshot(conn, "chk", "2026-09-20", 1000.0)
    one = insights.net_worth(conn)
    assert one["history"] == [{"date": "2026-09-20", "assets": 1000.0, "debts": 0.0, "net": 1000.0,   # TASK-15: the
                               "custom_assets": 1000.0, "custom_debts": 0.0, "custom_net": 1000.0}]   # Custom line too
    assert one["tracking_since"] == "2026-09-20"

    snapshot(conn, "cc", "2026-09-21", 200.0)          # card snapshots start a day later: never back-filled
    snapshot(conn, "chk", "2026-09-22", 1100.0)
    h = insights.net_worth(conn)["history"]
    assert [(p["date"], p["net"]) for p in h] == [("2026-09-20", 1000.0), ("2026-09-21", 800.0), ("2026-09-22", 900.0)]


@pytest.fixture
def pacific():
    """The machine's clock in California (archived_at is stored in UTC; history days are local dates)."""
    import os
    import time
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_an_account_archived_in_the_evening_leaves_history_that_day(conn, pacific):
    """Archived at 17:20 on Sep 26 in California is 00:20 UTC on Sep 27: comparing the stored UTC timestamp's date
    with local days kept the account in Sep 26's net worth (the suite failed after 5pm Pacific)."""
    from hermes_finance import manual, portfolio
    bank(conn)
    acct(conn, "chk", current=1000.0)
    for d in ("2026-09-25", "2026-09-26"):
        snapshot(conn, "chk", d, 1000.0)
    iou = manual.create(conn, {"kind": "other_liability", "name": "IOU", "subtype": "Family", "balance": 300.0,
                               "balance_as_of": "2026-09-20"}, date(2026, 9, 26))
    fund = manual.create(conn, {"kind": "investment", "name": "IRA", "institution": "Vanguard", "subtype": "roth_ira",
                                "balance": 500.0, "balance_as_of": "2026-09-20"}, date(2026, 9, 26))
    with conn:
        conn.execute("UPDATE manual_accounts SET archived_at = '2026-09-27T00:20:00Z' WHERE id IN (?, ?)",
                     (iou["id"], fund["id"]))
    h = {p["date"]: p["net"] for p in insights.net_worth(conn)["history"]}
    assert h == {"2026-09-25": 1200.0, "2026-09-26": 1000.0}           # gone on the day it was archived, locally
    series = portfolio._investment_series(conn)[f"manual:{fund['id']}"]
    assert series["until"] == "2026-09-26"

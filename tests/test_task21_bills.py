"""TASK-21: bills with changing amounts and any schedule (LADWP, SoCalGas, ...).

Detection (insights.recurring): every new cadence and its edges, the 2-charge rule for bill-like merchants only, a
utility whose amount changes found by its schedule alone (where a merchant with the same spread and a steady-amount
category is not), its estimate (median of the last 3) and usual range, one bill across accounts, late logic that scales
with the cadence, an every-2-months bill in "still due" / "Left to spend" only in its month, PER_MONTH totals. Tracked
bills (bills.py): the "usually" rule and its outlier tags, CRUD through the routes (behind the gate and CSRF), the CLI
(`finance bills track / untrack`), the plugin's finance_bills and finance_track_bill (approval card, every refusal), a
real MCP stdio client whose pending actions are approved in Settings into the same row the dashboard saves, and what
the Sunday summary reads (/api/overview, /api/budgets, /api/recurring)."""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import anyio
import mcp_types as types
import pytest
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

from agent_tools import plugin_ctx, run_cli, table_hashes
from factory import basic, cap, tx
from hermes_finance import api, auth, billcal, bills, cli, config, db, insights, mcp_state, prefs, reads, tooldefs
from hermes_finance.web import App

TODAY = date(2026, 9, 28)
LADWP = "Los Angeles Department of Water and Power"
GAS = (39.77, 31.2, 22.4, 12.05, 8.4, 5.3, 4.12, 4.9, 6.2)       # SoCalGas as Plaid has it: 9 monthly charges, $4.12-$39.77
LOCAL = {"Host": "127.0.0.1:8765"}
JSONH = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    monkeypatch.setattr(tooldefs, "today", lambda: TODAY)


@pytest.fixture
def c(conn):
    basic(conn)
    return conn


def rec(conn, today=TODAY) -> dict[str, dict]:
    return {r["merchant"]: r for r in insights.recurring(conn, today)["items"]}


def ago(days: int, today: date = TODAY) -> str:
    return (today - timedelta(days=days)).isoformat()


def series(conn, merchant, interval, n, amount=50.0, cat="Entertainment", last=5, **kw):
    """n charges `interval` days apart, the last one `last` days ago; amount: a number or one per charge."""
    amounts = amount if isinstance(amount, (list, tuple)) else [amount] * n
    return [tx(conn, ago(last + interval * (n - 1 - k)), amounts[k], merchant, cat=cat, **kw) for k in range(n)]


def socalgas(conn, cat="Gas & electric", merchant="SoCalGas"):
    return [tx(conn, f"2026-{m:02d}-04", a, merchant, name="SO CAL GAS", cat=cat) for m, a in zip(range(1, 10), GAS)]


def ladwp(conn, day="2026-08-30", amount=295.50, account="acc_cc", tid=None):
    return tx(conn, day, amount, LADWP, name="CITY OF LA DWP", cat="Water", account_id=account, tid=tid)


def track(conn, merchant=LADWP, today=TODAY, **kw):
    return bills.apply(conn, bills.plan(conn, today, "track", merchant=merchant, **kw), today)


def call(app, method, path, headers, body=None):
    status, ctype, out, _ = app.respond(method, path, headers, json.dumps(body).encode() if body is not None else b"")
    return status, json.loads(out)


@pytest.fixture
def app(c):
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


def row(conn, key="los angeles department of water and power"):
    r = conn.execute("SELECT * FROM tracked_bills WHERE merchant_key = ?", (key,)).fetchone()
    return dict(r) if r else None


def count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM tracked_bills").fetchone()[0]


# --- 1. schedules -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cadence, interval, n, cat, found", [
    ("biweekly", 13, 3, "Entertainment", True), ("biweekly", 14, 3, "Entertainment", True),
    ("biweekly", 16, 3, "Entertainment", True), ("biweekly", 12, 3, "Entertainment", False),
    ("biweekly", 17, 3, "Entertainment", False),
    ("biweekly", 14, 2, "Water", False),                                  # 2 charges only for every 2 / 3 / 6 months
    ("bimonthly", 55, 2, "Water", True), ("bimonthly", 60, 2, "Water", True), ("bimonthly", 70, 2, "Water", True),
    ("bimonthly", 54, 2, "Water", False), ("bimonthly", 71, 2, "Water", False),
    ("quarterly", 85, 2, "Insurance", True), ("quarterly", 91, 2, "Insurance", True), ("quarterly", 100, 2, "Insurance", True),
    ("quarterly", 84, 2, "Insurance", False), ("quarterly", 101, 2, "Insurance", False),
    ("semiannual", 170, 2, "Insurance", True), ("semiannual", 182, 2, "Insurance", True),
    ("semiannual", 195, 2, "Insurance", True), ("semiannual", 169, 2, "Insurance", False),
    ("semiannual", 196, 2, "Insurance", False),
    ("quarterly", 91, 3, "Entertainment", True), ("semiannual", 182, 3, "Entertainment", True),  # 3 charges: any merchant
])
def test_every_new_cadence_detects_and_its_edges_dont(c, cadence, interval, n, cat, found):
    series(c, "Pacific Co", interval, n, cat=cat)
    r = rec(c).get("Pacific Co")
    assert (r is not None) is found, r
    if found:
        assert (r["cadence"], r["cadence_label"]) == (cadence, insights.CADENCE_LABELS[cadence])
        assert r["cadence_label"] in ("every 2 weeks", "every 2 months", "every 3 months", "every 6 months")
        assert r["monthly_amount"] == round(50 * insights.PER_MONTH[cadence], 2) and r["interval_days"] == interval
        assert r["next_expected"] == ago(5 - interval) and r["status"] in ("upcoming", "due_soon")


def test_the_lookback_scales_with_the_cadence():
    assert insights.LOOKBACK_MONTHS == {"weekly": 7, "biweekly": 7, "monthly": 7, "bimonthly": 13, "quarterly": 13,
                                        "semiannual": 25, "yearly": 25}
    assert insights.PER_MONTH["bimonthly"] == 0.5 and insights.PER_MONTH["quarterly"] == 1 / 3
    assert insights.PER_MONTH["semiannual"] == 1 / 6 and insights.PER_MONTH["biweekly"] == 26 / 12


def test_every_6_months_needs_its_longer_window(c):
    """Two insurance premiums 182 days apart, the first 13+ months ago: only the 25-month window has both."""
    series(c, "Allstate", 182, 2, amount=600.0, cat="Insurance", last=220)     # the first 402 days ago
    assert ago(402) < insights._shift_months(TODAY, -13).isoformat()
    r = rec(c)["Allstate"]
    assert (r["cadence"], r["next_expected"], r["status"]) == ("semiannual", ago(220 - 182), "late")   # 220 > 195 + 5


@pytest.mark.parametrize("merchant, cat, found", [
    ("Random Gym", "Entertainment", False),              # a random merchant charged twice 60 days apart is NOT a bill
    ("Corner Store", "Other", False),
    ("Pacific Water", "Water", True),                     # a utility (Plaid RENT_AND_UTILITIES_WATER)
    ("Allstate", "Other", True),                          # BILL_NAME_RE (insurance)
    ("Mutual", "Insurance", True),                        # Plaid GENERAL_SERVICES_INSURANCE
    ("Acme Mortgage", "Other", True),                     # BILL_NAME_RE (mortgage)
])
def test_two_charges_count_only_for_a_bill_like_merchant(c, merchant, cat, found):
    series(c, merchant, 60, 2, amount=120.0, cat=cat)
    r = rec(c).get(merchant)
    assert (r is not None) is found
    if found:
        assert (r["cadence"], r["kind"], r["charges"]) == ("bimonthly", "bill", 2)


def test_a_non_bill_merchant_needs_three_charges_every_2_months(c):
    series(c, "Random Gym", 60, 3, amount=120.0)
    r = rec(c)["Random Gym"]
    assert (r["cadence"], r["kind"], r["charges"]) == ("bimonthly", "subscription", 3)


def test_irregular_visits_are_not_a_new_cadence(c):
    """Median 57 days (every 2 months) but the gaps are 19, 86 and 57: most gaps must fit too."""
    for d in ("2026-04-01", "2026-04-20", "2026-07-15", "2026-09-10"):
        tx(c, d, 20, "Random Shop", cat="Entertainment")
    tx(c, "2026-07-01", 90.0, "Pacific Water", cat="Water")
    tx(c, "2026-09-18", 90.0, "Pacific Water", cat="Water")   # 79 days: no cadence
    assert rec(c) == {}


# --- 2. amounts that change ------------------------------------------------------------------------------------------

def test_socalgas_a_utility_whose_amount_changes_is_found_by_its_schedule(c):
    socalgas(c)
    r = rec(c)["SoCalGas"]
    assert (r["cadence"], r["kind"], r["variable"], r["source"]) == ("monthly", "bill", True, "detected")
    assert r["amount"] == r["estimate"] == 4.9                       # the median of the last 3: 4.12, 4.90, 6.20
    assert r["amount_label"] == "~$5 est."
    assert r["usual"] == {"low": 4.12, "high": 39.77, "source": "charges"}   # all 9 (13 months): "usually $4–40"
    assert insights.usual_text(r["usual"]["low"], r["usual"]["high"]) == "$4–40"
    assert (r["last_date"], r["last_amount"], r["next_expected"]) == ("2026-09-04", 6.2, "2026-10-04")   # hotfix 2026-09-29: the 4th, every month
    assert r["charges"] == 7                                          # the 7-month window: March to September
    assert r["monthly_amount"] == 4.9 and r["outlier"] is None


@pytest.mark.parametrize("cat, merchant", [("Entertainment", "Noisy Gym"), ("Rent & Utilities", "Rent Co"),
                                           ("Other", "Corner Store")])
def test_the_same_spread_is_not_a_bill_without_a_utility(c, cat, merchant):
    """Not a utility (rent keeps the band too): the ±25% band still applies, and $4-$40 fails it."""
    socalgas(c, cat=cat, merchant=merchant)
    assert merchant not in rec(c)


def test_a_utility_with_a_steady_amount_stays_exact(c):
    series(c, "AT&T", 30, 5, amount=45.23, cat="Phone", last=14)
    series(c, "Multifamily Utility Company", 30, 5, amount=[81.2, 84.5, 79.9, 83.1, 82.0], cat="Other", last=10)
    r = rec(c)
    att, mf = r["AT&T"], r["Multifamily Utility Company"]
    assert (att["variable"], att["amount"], att["amount_label"], att["usual"], att["estimate"]) == (False, 45.23, "$45.23", None, None)
    assert (mf["variable"], mf["amount"], mf["amount_label"]) == (False, 82.0, "$82")   # its last charge, as before


def test_the_estimate_is_the_median_of_the_last_three(c):
    series(c, "Pacific Power", 30, 6, amount=[80, 15, 60, 50, 10, 20], cat="Gas & electric")
    r = rec(c)["Pacific Power"]
    assert r["variable"] and r["amount"] == 20.0 and r["amount_label"] == "~$20 est."   # not the mean (26.67), not 20's last
    assert r["usual"] == {"low": 10.0, "high": 80.0, "source": "charges"}


# --- 3. one bill, any card -------------------------------------------------------------------------------------------

def test_a_bill_is_the_merchant_not_the_account(c):
    """LADWP moved cards: the older charges on the card, the newer ones from checking. One bill; last paid from checking."""
    for k, d in enumerate(("2026-02-10", "2026-04-11", "2026-06-10")):
        tx(c, d, 110.0 + k, "Pacific Water", cat="Water", account_id="acc_cc")
    tx(c, "2026-08-09", 131.0, "Pacific Water", cat="Water", account_id="acc_chk")
    r = rec(c)
    assert list(r) == ["Pacific Water"]
    w = r["Pacific Water"]
    assert (w["cadence"], w["charges"]) == ("bimonthly", 4)
    assert w["last_account"] == {"name": "Checking", "mask": "1234", "institution": "Chase"}


def test_a_tracked_bill_counts_charges_from_every_account(c):
    ladwp(c, "2026-04-28", 118.0, account="acc_chk")
    ladwp(c, "2026-06-29", 142.0, account="acc_chk")
    ladwp(c)                                                              # Aug 30 on the card
    track(c, every="2m", varies=True, usually=[100, 150])
    b = rec(c)[LADWP]
    assert b["charges"] == 3 and b["amount"] == 142.0                     # 3 charges: the median of the last 3
    assert b["last_account"]["mask"] == "9999" and b["interval_days"] == 62


# --- 4. the "usually" rule ---------------------------------------------------------------------------------------------

def test_one_charge_and_usually_100_150_estimates_125_not_296(c):
    ladwp(c)
    out = track(c, every="2m", varies=True, usually=[100, 150])
    b = out["bill"]
    assert (b["cadence"], b["cadence_label"], b["next_expected"]) == ("bimonthly", "every 2 months", "2026-10-29")
    assert (b["amount"], b["amount_label"], b["variable"], b["source"]) == (125.0, "~$125 est.", True, "tracked")
    assert b["usual"] == {"low": 100.0, "high": 150.0, "source": "you"} and b["outlier"] == "higher"
    assert b["last_account"] == {"name": "Card", "mask": "9999", "institution": "Chase"} and b["last_amount"] == 295.5
    assert out["summary"] == ("Track Los Angeles Department of Water and Power as a bill every 2 months, usually $100–150 "
                              "(next ~Oct 29, ~$125 est.).")


def test_without_usually_the_single_charge_is_all_it_has(c):
    ladwp(c)
    b = track(c, every="2m", varies=True)["bill"]
    assert (b["amount"], b["amount_label"], b["usual"], b["outlier"]) == (295.5, "~$296 est.", None, None)


def test_two_charges_with_usually_still_use_its_middle(c):
    ladwp(c, "2026-06-30", 240.0)
    ladwp(c)
    b = track(c, every="2m", varies=True, usually=120)["bill"]           # one amount: low = high
    assert b["amount"] == 120.0 and b["usual"] == {"low": 120.0, "high": 120.0, "source": "you"}


def test_after_three_charges_the_median_of_the_last_three_and_the_range_stays(c):
    ladwp(c)
    track(c, every="2m", varies=True, usually=[100, 150])
    ladwp(c, "2026-06-30", 120.0)        # older charges from the data count too (any account)
    b = rec(c)[LADWP]
    assert b["amount"] == 125.0          # still two
    ladwp(c, "2026-09-26", 140.0)        # a third: median(120, 295.50, 140) = 140, one big bill doesn't drag it
    b = rec(c)[LADWP]
    assert (b["charges"], b["amount"], b["amount_label"]) == (3, 140.0, "~$140 est.")
    assert b["usual"] == {"low": 100.0, "high": 150.0, "source": "you"} and b["outlier"] is None   # the last, $140, is usual
    out = bills.apply(c, bills.plan(c, TODAY, "update", merchant="dwp", usually="110-170"), TODAY)   # still editable
    assert out["bill"]["usual"] == {"low": 110.0, "high": 170.0, "source": "you"} and out["bill"]["amount"] == 140.0


@pytest.mark.parametrize("amount, want", [(225.01, "higher"), (225.00, None), (295.5, "higher"), (150.0, None),
                                          (50.0, None), (49.99, "lower"), (12.0, "lower")])
def test_outlier_thresholds_both_ways(c, amount, want):
    """> 1.5x the top of $100–150 is higher; < 0.5x its bottom is lower. On the Recurring row and the transaction."""
    assert insights.outlier(amount, {"low": 100.0, "high": 150.0, "source": "you"}) == want
    assert insights.outlier(amount, {"low": 100.0, "high": 150.0, "source": "charges"}) is None   # only what you said
    tid = ladwp(c, amount=amount)
    track(c, every="2m", varies=True, usually=[100, 150])
    assert rec(c)[LADWP]["outlier"] == want
    rows = api.transactions(c, {"month": "2026-08"}, TODAY)["rows"]
    assert next(r for r in rows if r["transaction_id"] == tid)["bill_outlier"] == want
    assert all(r["bill_outlier"] is None for r in rows if r["transaction_id"] != tid)


def test_the_outlier_tag_never_changes_the_budget_math(c):
    cap(c, "Rent & Utilities", 3000)
    ladwp(c, "2026-09-02", 295.5)
    before = insights.budgets(c, "2026-09", TODAY)
    track(c, every="2m", varies=True, usually=[100, 150])
    after = insights.budgets(c, "2026-09", TODAY)
    assert rec(c)[LADWP]["outlier"] == "higher"
    assert (after["total_spend"], after["total_due"], after["left"]) == (before["total_spend"], 0.0, before["left"])  # the real charge


# --- 5. late / due ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cadence", [c for c, _, _ in insights.CADENCES])
def test_late_only_after_the_cadence_upper_bound_plus_late_after_days(cadence):
    lo, hi = insights.CADENCE_RANGE[cadence]
    last = TODAY - timedelta(days=hi + insights.LATE_AFTER_DAYS)
    nxt = last + timedelta(days=insights.NOMINAL_DAYS[cadence])
    assert insights._status(last, nxt, cadence, TODAY)[1] != "late"
    assert insights._status(last - timedelta(days=1), nxt - timedelta(days=1), cadence, TODAY)[1] == "late"


@pytest.mark.parametrize("days, status", [(40, "upcoming"), (60, "due_soon"), (75, "due_soon"), (76, "late")])
def test_an_every_2_months_bill_40_days_on_is_not_late(c, days, status):
    ladwp(c, ago(days))
    track(c, every="2m", varies=True)
    assert rec(c)[LADWP]["status"] == status


@pytest.mark.parametrize("days, status", [(40, "due_soon"), (41, "late")])
def test_a_monthly_bill_is_late_after_40_days(c, days, status):
    series(c, "Netflix", 31, 4, amount=15.49, last=days)
    assert rec(c)["Netflix"]["status"] == status


# --- 6. budgets: still due, Left to spend, the pace, totals ----------------------------------------------------------

def test_an_every_2_months_bill_counts_in_left_to_spend_only_in_its_month(c):
    cap(c, "Rent & Utilities", 3000)
    ladwp(c)                                                             # Aug 30: next ~Oct 29
    track(c, every="2m", varies=True, usually=[100, 150])
    sep = insights.budgets(c, "2026-09", TODAY)
    assert (sep["total_due"], sep["left"], sep["bills_due"]) == (0.0, 3000.0, [])
    oct_day = date(2026, 10, 2)
    octb = insights.budgets(c, "2026-10", oct_day)
    assert octb["total_due"] == 125.0 and octb["left"] == 2875.0          # at its estimate, not the $295.50 charge
    assert octb["bills_due"] == [{"merchant": LADWP, "amount": 125.0, "next_expected": "2026-10-29",
                                  "category": "Rent & Utilities", "variable": True}]
    pace = insights.pace_projection(c, "2026-10", oct_day)
    assert pace["bills_due_total"] == 125.0 and pace["bills_due"][0]["variable"] is True
    nov = insights.budgets(c, "2026-11", date(2026, 11, 2))              # expected Oct 29, not charged: not November's
    assert nov["total_due"] == 0.0
    status = reads.budget_status(c, "2026-10", oct_day)
    assert (status["totals"]["due"], status["totals"]["left"]) == (125.0, 2875.0)


def test_weekly_and_every_2_weeks_are_paced_not_due():
    assert insights.PERIODIC == ("monthly", "bimonthly", "quarterly", "semiannual", "yearly")


def test_monthly_totals_are_normalized_with_per_month(c):
    amounts = {"1w": 12.0, "2w": 50.0, "1m": 40.0, "2m": 125.0, "3m": 300.0, "6m": 600.0, "1y": 120.0}
    for every, amount in amounts.items():
        tx(c, ago(2), amount, f"Co {every}", cat="Other")
        track(c, f"Co {every}", every=every, varies=False)
    r = insights.recurring(c, TODAY)
    per = {x["merchant"]: x["monthly_amount"] for x in r["items"]}
    want = {f"Co {e}": round(a * insights.PER_MONTH[bills.EVERY[e]], 2) for e, a in amounts.items()}
    assert per == want and want["Co 2m"] == 62.5 and want["Co 3m"] == 100.0 and want["Co 2w"] == 108.33
    # (hotfix 2026-09-29: a tracked merchant's kind follows the same rule as a detected one, and these "Other" merchants
    # are no bill by that rule, so they count as subscriptions; the normalized total is the same either way)
    assert r["monthly_totals"] == {"bill": 0.0, "subscription": round(sum(want.values()), 2)}


def test_the_calendar_steps_every_2_months_and_shows_the_paid_charge_and_the_estimate(c):
    ladwp(c)
    track(c, every="2m", varies=True, usually=[100, 150])
    item = rec(c)[LADWP]
    days = [(o["date"], o["state"]) for o in billcal.occurrences(item, date(2026, 8, 1), date(2027, 3, 31), TODAY)]
    assert days == [("2026-08-30", "paid"), ("2026-10-29", "due"), ("2026-12-29", "due"), ("2027-02-28", "due")]
    aug = billcal.calendar_month(c, "2026-10", TODAY)
    [b] = [x for x in aug["bills"] if x["merchant"] == LADWP]
    assert (b["amount"], b["variable"], b["cadence"], b["cadence_label"]) == (125.0, True, "bimonthly", "every 2 months")
    with pytest.raises(billcal.CalendarError):
        billcal.calendar_month(c, "2026-08", TODAY)                       # (the calendar starts this month)
    sep = billcal.calendar_month(c, "2026-09", TODAY)
    assert all(x["merchant"] != LADWP for x in sep["bills"])              # nothing of it in September


# --- 7. tracked bills: routes, gate, CSRF ----------------------------------------------------------------------------

def test_track_update_stop_through_the_routes(c, app):
    tid = ladwp(c)
    status, out = call(app, "POST", "/api/bills/track", JSONH, {"transaction_id": tid, "every": "2m", "varies": True,
                                                                  "usually": "100-150"})
    assert status == 200 and out["bill"]["amount_label"] == "~$125 est." and out["bill"]["next_expected"] == "2026-10-29"
    r = row(c)
    assert {k: r[k] for k in ("merchant", "cadence", "variable", "usual_low", "usual_high", "anchor_date", "anchor_amount",
                              "anchor_transaction_id", "stopped_at")} == {
        "merchant": LADWP, "cadence": "bimonthly", "variable": 1, "usual_low": 100.0, "usual_high": 150.0,
        "anchor_date": "2026-08-30", "anchor_amount": 295.5, "anchor_transaction_id": tid, "stopped_at": None}
    key = r["merchant_key"]
    status, out = call(app, "POST", "/api/bills/track", JSONH, {"action": "update", "key": key, "every": "3m"})
    assert status == 200 and out["bill"]["cadence"] == "quarterly" and out["bill"]["next_expected"] == "2026-11-29"
    r2 = row(c)
    assert (r2["cadence"], r2["usual_low"], r2["anchor_date"], r2["created_at"]) == ("quarterly", 100.0, "2026-08-30", r["created_at"])
    status, look = call(app, "GET", f"/api/bills/lookup?transaction_id={tid}", LOCAL)
    assert status == 200 and look["tracked"] and look["bill"]["cadence"] == "quarterly" and look["outlier"] == "higher"
    assert look["suggest"] == {"every": "3m", "varies": True, "usually": [100.0, 150.0]}
    status, out = call(app, "POST", "/api/bills/stop", JSONH, {"key": key})
    assert status == 200 and out["bill"] is None and row(c)["stopped_at"] is not None and LADWP not in rec(c)
    status, look = call(app, "GET", f"/api/bills/lookup?key={key}", LOCAL)
    assert look["hidden"] and look["bill"] is None and look["trackable"]
    status, out = call(app, "POST", "/api/bills/track", JSONH, {"key": key, "every": "2m", "varies": False})
    assert status == 200 and row(c)["stopped_at"] is None and rec(c)[LADWP]["amount_label"] == "$295.50"
    assert count(c) == 1


def test_stopping_a_detected_bill_hides_it(c, app):
    series(c, "Netflix", 31, 4, amount=15.49, cat="Entertainment", last=16)
    key = rec(c)["Netflix"]["key"]
    status, out = call(app, "POST", "/api/bills/stop", JSONH, {"key": key})
    assert status == 200 and "no longer listed" in out["summary"] and "Netflix" not in rec(c)
    r = row(c, key)
    assert (r["cadence"], r["variable"], r["stopped_at"] is not None) == ("monthly", 0, True)
    assert call(app, "POST", "/api/bills/stop", JSONH, {"key": key})[0] == 400            # not a bill any more


def test_the_preview_changes_nothing(c, app):
    tid = ladwp(c)
    status, out = call(app, "GET", f"/api/bills/preview?transaction_id={tid}&every=2m&varies=1&usually=100-150", LOCAL)
    assert status == 200 and out["bill"]["amount"] == 125.0 and out["summary"].startswith("Track Los Angeles")
    assert count(c) == 0
    status, out = call(app, "GET", f"/api/bills/preview?transaction_id={tid}&every=2m&varies=1&usually=150-100", LOCAL)
    assert status == 400 and out["field"] == "usually"


@pytest.mark.parametrize("body, status, field", [
    ({"key": "no such merchant", "every": "2m"}, 404, "merchant"),
    ({"transaction_id": "nope"}, 404, "transaction_id"),
    ({"merchant_key": "x"}, 400, None),                                   # an unknown field
    ({"every": "2m"}, 400, "merchant"),                                   # which merchant?
    ({"KEY": 1, "every": "5m"}, 400, "every"),
    ({"KEY": 1, "every": "2m", "usually": "150-100"}, 400, "usually"),
    ({"KEY": 1, "every": "2m", "usually": "0"}, 400, "usually"),
    ({"KEY": 1, "every": "2m", "usually": "-5"}, 400, "usually"),
    ({"KEY": 1, "every": "2m", "usually": "lots"}, 400, "usually"),
    ({"KEY": 1, "every": "2m", "usually": [1, 2, 3]}, 400, "usually"),
    ({"KEY": 1, "every": "2m", "varies": False, "usually": "100-150"}, 400, "usually"),
    ({"KEY": 1, "every": "2m", "varies": "yes"}, 400, "varies"),
    ({"KEY": 1, "action": "stop"}, 400, "action"),
    ({"KEY": 2, "action": "update", "every": "2m"}, 400, "merchant"),     # not tracked or detected
])
def test_bad_requests_are_refused_and_save_nothing(c, app, body, status, field):
    ladwp(c)
    tx(c, ago(3), 12.0, "Corner Store", cat="Other")
    keys = {1: "los angeles department of water and power", 2: "corner store"}
    body = {("key" if k == "KEY" else k): (keys[v] if k == "KEY" else v) for k, v in body.items()}
    got, out = call(app, "POST", "/api/bills/track", JSONH, body)
    assert got == status and out.get("field") == field, out
    assert count(c) == 0
    assert call(app, "POST", "/api/bills/stop", JSONH, {"key": "corner store"})[0] == 400   # stop: tracked or detected only
    assert count(c) == 0


def test_merchant_matching_exact_unique_ambiguous_unknown(c):
    ladwp(c)
    socalgas(c)
    tx(c, ago(4), 9.0, "Coffee Co", cat="Eating Out")
    key = "los angeles department of water and power"
    for text in (LADWP, LADWP.lower(), "city of la dwp", "  CITY OF LA DWP ", "dwp", "Water and Power"):
        assert bills.find_merchant(c, text) == (key, LADWP), text
    with pytest.raises(bills.BillError, match=r"'o' matches 3 merchants: Coffee Co, Los Angeles Department of Water and "
                                              r"Power, SoCalGas\. Use one of these names exactly\."):
        bills.find_merchant(c, "o")
    assert bills.find_merchant(c, "co") == ("coffee co", "Coffee Co")        # "so cal gas" has no "co" in it
    with pytest.raises(bills.BillError, match=r"no merchant matches 'LA Water Department' in your transactions\. Close ones: "
                                              f"{LADWP}"):
        bills.find_merchant(c, "LA Water Department")
    with pytest.raises(bills.BillError, match="merchant is required"):
        bills.find_merchant(c, " ")


@pytest.fixture
def builtin(c):
    from argon2 import PasswordHasher
    auth._hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    with c:
        c.execute("INSERT INTO auth_users (username, password_hash, totp_secret, created_at, password_changed_at) "
                  "VALUES ('owner', ?, ?, ?, ?)", (auth.hash_secret("pw"), auth.new_totp_secret(), db.now(), db.now()))
    prefs.save(c, {"access.mode": "builtin"}, allowed=["access.mode"])
    token, csrf = auth.create_session(c, 1)
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a, {"Cookie": f"{auth.COOKIE}={token}"}, csrf


@pytest.mark.parametrize("path", ["/api/bills/track", "/api/bills/stop", "/api/mcp/pending/approve", "/api/mcp/pending/decline"])
def test_every_new_write_needs_the_session_and_its_csrf_token(builtin, c, path):
    app, cookie, csrf = builtin
    tid = ladwp(c)
    series(c, "Netflix", 31, 4, amount=15.49, last=16)
    pending = mcp_state.record_pending(c, "finance_track_bill", {"action": "track", "merchant": LADWP, "every": "2m"}, "x", "t")
    body = {"transaction_id": tid, "every": "2m", "key": "netflix", "id": pending["id"]}
    body = {k: v for k, v in body.items() if (k == "id") == path.startswith("/api/mcp")
            and not (path.endswith("/stop") and k in ("transaction_id", "every"))}
    before = table_hashes(config.db_path())
    assert call(app, "POST", path, JSONH, body)[0] == 401                                     # no session
    for headers in ({**JSONH, **cookie}, {**JSONH, **cookie, "X-CSRF-Token": "wrong"}):
        status, out = call(app, "POST", path, headers, body)
        assert status == 403 and "CSRF" in out["error"]
    assert table_hashes(config.db_path()) == before                                           # nothing written
    status, out = call(app, "POST", path, {**JSONH, **cookie, "X-CSRF-Token": csrf}, body)
    assert status == 200, out


def test_the_task20_walk_covers_the_new_writes():
    import test_task20_merge as walk
    paths = {p for _, p in walk.WRITES}
    assert {"/api/bills/track", "/api/bills/stop", "/api/mcp/pending/approve", "/api/mcp/pending/decline"} <= paths
    assert {"tracked_bills", "pending_actions"} <= set(walk.TABLES)


# --- 8. the CLI --------------------------------------------------------------------------------------------------------

def test_finance_bills_track_and_untrack(c, capsys):
    ladwp(c)
    socalgas(c)
    code, out, err = run_cli(["bills", "track", LADWP, "--every", "2m", "--varies"], capsys)
    assert code == 0 and "every 2 months" in out and "~$296 est." in out and "from Card ••9999" in out, (out, err)
    code, out, err = run_cli(["bills", "track", LADWP, "--every", "2m", "--varies", "--usually", "100-150"], capsys)
    assert code == 0 and "next expected 2026-10-29, ~$125 est. (usually $100–150)" in out
    assert "that charge is higher than usual" in out
    code, out, _ = run_cli(["bills", "--next", "400d", "--json"], capsys)
    items = {b["merchant"]: b for b in json.loads(out)["items"]}
    lad = items[LADWP]
    assert {k: lad[k] for k in ("cadence", "cadence_label", "variable", "estimate", "amount_label", "usual", "source",
                                "last_account", "outlier", "next_expected")} == {
        "cadence": "bimonthly", "cadence_label": "every 2 months", "variable": True, "estimate": 125.0,
        "amount_label": "~$125 est.", "usual": {"low": 100.0, "high": 150.0, "source": "you"}, "source": "tracked",
        "last_account": {"name": "Card", "mask": "9999", "institution": "Chase"}, "outlier": "higher",
        "next_expected": "2026-10-29"}
    assert items["SoCalGas"]["source"] == "detected" and items["SoCalGas"]["variable"] is True
    code, out, _ = run_cli(["bills", "--next", "400d"], capsys)
    assert "~$125 est. (usually $100–150)" in out and "tracked, last charge higher than usual" in out
    assert "~$5 est. (usually $4–40)" in out
    code, out, _ = run_cli(["bills", "untrack", "dwp"], capsys)
    assert code == 0 and out.startswith("Stopped. Stop tracking Los Angeles Department of Water and Power as a bill")
    code, out, _ = run_cli(["bills", "--next", "400d", "--json"], capsys)
    assert LADWP not in {b["merchant"] for b in json.loads(out)["items"]}


@pytest.mark.parametrize("argv, message", [
    (["bills", "track", "Nowhere Inc", "--every", "2m"], "no merchant matches 'Nowhere Inc'"),
    (["bills", "track", "o", "--every", "2m"], "matches 2 merchants"),
    (["bills", "track", LADWP, "--every", "5w"], "every must be one of"),
    (["bills", "track", LADWP, "--every", "2m", "--usually", "150-100"], "the low end ($150) is more than the high end"),
    (["bills", "track", LADWP, "--same", "--usually", "100"], "usually is for a bill whose amount changes"),
    (["bills", "untrack", "SoCalGas"], "isn't a bill right now"),
])
def test_finance_bills_track_refuses_and_saves_nothing(c, capsys, argv, message):
    ladwp(c)
    tx(c, ago(2), 5.0, "SoCalGas", cat="Other")
    code, out, err = run_cli(argv, capsys)
    assert code == 2 and message in err and out == "", err
    assert count(c) == 0


def test_the_list_still_takes_next_and_the_writes_need_a_merchant(c, capsys):
    assert cli.build_parser().parse_args(["bills"]).next == "14d"
    assert cli.build_parser().parse_args(["bills", "--next", "30d"]).func is cli.cmd_bills
    assert cli.build_parser().parse_args(["bills", "track", "x", "--every", "2m"]).func is cli.cmd_bills_track
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["bills", "track"])


# --- 9. the plugin and MCP -------------------------------------------------------------------------------------------

def test_the_plugin_finance_bills_output(c):
    ladwp(c)
    socalgas(c)
    track(c, every="2m", varies=True, usually=[100, 150])
    p = plugin_ctx()
    out = p.call("finance_bills", {"next": "400d"})
    assert out == reads.bills(c, TODAY, 400)
    lad = next(b for b in out["items"] if b["merchant"] == LADWP)
    for k in ("cadence", "variable", "estimate", "usual", "source", "last_account", "outlier"):
        assert k in lad
    assert (lad["estimate"], lad["source"], lad["outlier"], lad["last_account"]["mask"]) == (125.0, "tracked", "higher", "9999")
    assert out["monthly_totals"]["bill"] == round(62.5 + 4.9, 2)


def test_the_plugin_tracks_updates_and_stops_with_the_approval_card(c):
    ladwp(c)
    series(c, "Netflix", 31, 4, amount=15.49, last=16)
    p = plugin_ctx()
    args = {"action": "track", "merchant": "los angeles department of water and power", "every": "2m", "varies": True,
            "usually": [100, 150]}
    before = table_hashes(config.db_path())
    card = p.call("finance_track_bill", args)["directive"]
    assert card == {"action": "approve", "message": "Track Los Angeles Department of Water and Power as a bill every 2 "
                                                    "months, usually $100–150 (next ~Oct 29, ~$125 est.)."}
    assert table_hashes(config.db_path()) == before                                   # asking changes nothing
    out = p.approved("finance_track_bill", args)
    assert out["bill"]["amount"] == 125.0 and out["bill"]["source"] == "tracked" and row(c)["cadence"] == "bimonthly"
    card = p.call("finance_track_bill", {"action": "update", "merchant": "dwp", "every": "3m"})["directive"]
    assert card["message"].startswith("Change Los Angeles Department of Water and Power's bill to every 3 months, usually "
                                      "$100–150 (now every 2 months, usually $100–150; next ~Nov 29, ~$125 est.)")
    assert p.approved("finance_track_bill", {"action": "update", "merchant": "dwp", "every": "3m"})["bill"]["cadence"] == "quarterly"
    card = p.call("finance_track_bill", {"action": "stop", "merchant": "Netflix"})["directive"]
    assert card["message"].startswith("Stop tracking Netflix as a bill: it's no longer listed with your bills (found every "
                                      "month, $15.49)")
    assert p.approved("finance_track_bill", {"action": "stop", "merchant": "Netflix"})["bill"] is None
    assert p.approved("finance_track_bill", {"action": "stop", "merchant": LADWP})["bill"] is None
    assert row(c)["stopped_at"] and row(c, "netflix")["stopped_at"]


REFUSALS = [
    ({"action": "track", "merchant": "Nowhere Inc", "every": "2m"}, "no merchant matches 'Nowhere Inc' in your transactions."),
    ({"action": "track", "merchant": "e", "every": "2m"}, "'e' matches 2 merchants: Los Angeles Department of Water and "
                                                          "Power, Netflix. Use one of these names exactly."),
    ({"action": "track", "merchant": LADWP, "every": "5w"}, "every must be one of 1w, 2w, 1m, 2m, 3m, 6m, 1y"),
    ({"action": "track", "merchant": LADWP, "usually": [150, 100]}, "usually: the low end ($150) is more than the high end"),
    ({"action": "track", "merchant": LADWP, "usually": 0}, "usually must be more than 0"),
    ({"action": "track", "merchant": LADWP, "usually": -20}, "usually must be more than 0"),
    ({"action": "track", "merchant": LADWP, "usually": [100, "150"]}, "usually must be a number or a list of two numbers"),
    ({"action": "track", "merchant": LADWP, "usually": [1, 2, 3]}, "usually takes one amount or two"),
    ({"action": "track", "merchant": LADWP, "varies": False, "usually": 120}, "usually is for a bill whose amount changes"),
    ({"action": "stop", "merchant": LADWP}, "isn't a bill right now (not tracked or detected), so there's nothing to stop"),
    ({"action": "update", "merchant": LADWP, "every": "2m"}, "isn't tracked or detected as a bill yet"),
    ({"action": "stop", "merchant": "Netflix", "every": "1m"}, "stop takes only the merchant"),
    ({"action": "pause", "merchant": LADWP}, "action must be one of track, update, stop"),
    ({"merchant": LADWP}, "action is required"),
    ({"action": "track"}, "merchant is required"),
    ({"action": "track", "merchant": LADWP, "varies": "yes"}, "varies must be true or false"),
    ({"action": "track", "merchant": LADWP, "usually": "100-150"}, "usually must be a number or a list or null"),
]


@pytest.mark.parametrize("args, message", REFUSALS)
def test_every_refusal_is_blocked_before_the_card_and_changes_nothing(c, args, message):
    ladwp(c)
    series(c, "Netflix", 31, 4, amount=15.49, last=16)
    p = plugin_ctx()
    before = table_hashes(config.db_path())
    directive = p.call("finance_track_bill", args)["directive"]
    assert directive["action"] == "block" and message in directive["message"], directive
    handler = json.loads(p.tools["finance_track_bill"](args))                       # the handler refuses too
    assert message in handler["error"]
    assert table_hashes(config.db_path()) == before


def test_the_tool_list_is_15_with_one_hook():
    """(TASK-22: finance_sync is the 15th.)"""
    p = plugin_ctx()
    assert len(p.tools) == 16 and len(p.hooks["pre_tool_call"]) == 1 and "finance_track_bill" in p.tools  # TASK-24: + finance_rules
    assert tooldefs.WRITES == ("finance_set_category", "finance_add_rule", "finance_set_cap", "finance_track_bill")
    schema = tooldefs.TOOLS["finance_track_bill"].schema()
    assert schema["required"] == ["action", "merchant"] and schema["properties"]["every"]["enum"] == list(bills.EVERY)
    manifest = (Path(__file__).resolve().parents[1] / "hermes_plugin" / "plugin.yaml").read_text()
    assert "  - finance_track_bill\n" in manifest


def _stdio(data_dir, calls, client="Claude Desktop"):
    """A real MCP client on stdio: `finance mcp` spawned as a child process (only the environment given), then calls."""
    finance = Path(sys.executable).with_name("finance")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PLAID_") and k not in ("MARKETCHECK_API_KEY", "RENTCAST_API_KEY")}

    async def session():
        params = StdioServerParameters(command=str(finance), args=["mcp"], env=env, cwd=str(data_dir.parent))
        async with Client(params, mode="legacy", client_info=types.Implementation(name=client, version="1")) as cl:
            listed = [t.name for t in (await cl.list_tools()).tools]
            out = []
            for name, args in calls:
                r = await cl.call_tool(name, args)
                out.append((bool(r.is_error), r.content[0].text))
            return listed, out

    return anyio.run(session)


def _ladwp_db(path: Path, today: date) -> str:
    conn = db.connect(path)
    basic(conn)
    tid = ladwp(conn, (today - timedelta(days=29)).isoformat(), tid="ladwp-aug")   # the same id in both copies
    series(conn, "Netflix", 31, 4, amount=15.49, last=16)
    mcp_state.set_allow_writes(conn, True)
    conn.close()
    return tid


def test_a_real_mcp_stdio_client_asks_the_owner_approves_in_settings_and_the_row_is_the_uis(data_dir, tmp_path, monkeypatch):
    today = date.today()                                  # the child's clock is the real one
    monkeypatch.setattr(tooldefs, "today", lambda: today)
    tid = _ladwp_db(config.db_path(), today)
    args = {"action": "track", "merchant": LADWP, "every": "2m", "varies": True, "usually": [100, 150]}
    before = table_hashes(config.db_path())
    listed, out = _stdio(data_dir, [
        ("finance_track_bill", args),
        ("finance_track_bill", {"action": "track", "merchant": "Nowhere Inc", "every": "2m"}),     # refused, not recorded
        ("finance_track_bill", {"action": "track", "merchant": LADWP, "usually": [150, 100]}),
        ("finance_track_bill", {"action": "stop", "merchant": LADWP}),                            # not a bill yet
        ("finance_track_bill", {"action": "update", "merchant": "Netflix", "every": "1m", "varies": False}),
        ("finance_track_bill", {"action": "stop", "merchant": "Netflix"})])
    assert "finance_track_bill" in listed and len(listed) == 16          # (TASK-22: + finance_sync; TASK-24: + finance_rules)
    first = json.loads(out[0][1])
    assert not out[0][0] and first["status"] == "pending_approval" and first["applied"] is False
    assert first["pending_action"]["summary"].startswith("Track Los Angeles Department of Water and Power as a bill every "
                                                         "2 months, usually $100–150 (next ~")
    assert [e for e, _ in out[1:4]] == [True, True, True]
    assert "no merchant matches" in out[1][1] and "low end" in out[2][1] and "nothing to stop" in out[3][1]
    assert not out[4][0] and not out[5][0]
    after = table_hashes(config.db_path())
    assert {t for t in after if after[t] != before[t]} == {"pending_actions", "settings"}   # nothing applied (last_used)
    conn = db.connect(config.db_path())
    assert count(conn) == 0 and [a["tool"] for a in mcp_state.pending(conn)] == ["finance_track_bill"] * 3
    ids = [a["id"] for a in mcp_state.pending(conn)]
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    app.today = lambda: today
    status, res = call(app, "POST", "/api/mcp/pending/approve", JSONH, {"id": ids[0]})     # Settings -> Approve
    assert status == 200 and res["action"]["status"] == "applied" and res["action"]["result"]["bill"]["amount"] == 125.0
    assert res["pending"] == 2 and call(app, "POST", "/api/mcp/pending/approve", JSONH, {"id": ids[0]})[0] == 409
    status, res = call(app, "POST", "/api/mcp/pending/decline", JSONH, {"id": ids[1]})     # Decline: nothing changes
    assert status == 200 and res["action"]["status"] == "rejected" and row(conn, "netflix") is None
    status, res = call(app, "POST", "/api/mcp/pending/approve", JSONH, {"id": ids[2]})     # stop Netflix: applied
    assert res["action"]["status"] == "applied" and row(conn, "netflix")["stopped_at"]
    assert call(app, "POST", "/api/mcp/pending/approve", JSONH, {"id": 999})[0] == 404
    assert call(app, "POST", "/api/mcp/pending/approve", JSONH, {"id": "1"})[0] == 400
    mcp_row = row(conn)
    conn.close()
    # the same change from the dashboard (Track as a bill on its latest charge) on a copy of the same data
    other = tmp_path / "ui" / "finance.db"
    other.parent.mkdir()
    monkeypatch.setenv("FINANCE_DATA_DIR", str(other.parent))
    ui_tid = _ladwp_db(other, today)
    ui = App(other, lambda: None, "127.0.0.1", 8765)
    ui.today = lambda: today
    assert call(ui, "POST", "/api/bills/track", JSONH, {"transaction_id": ui_tid, "every": "2m", "varies": True,
                                                        "usually": "100-150"})[0] == 200
    conn = db.connect(other)
    ui_row = row(conn)
    conn.close()
    skip = ("id", "created_at", "updated_at")
    assert {k: v for k, v in mcp_row.items() if k not in skip} == {k: v for k, v in ui_row.items() if k not in skip}
    assert mcp_row["anchor_transaction_id"] == tid and tid == ui_tid


def test_a_pending_action_that_no_longer_applies_fails_with_the_reason(c, app):
    ladwp(c)
    a = mcp_state.record_pending(c, "finance_track_bill", {"action": "stop", "merchant": LADWP}, "Stop tracking LADWP", "x")
    status, res = call(app, "POST", "/api/mcp/pending/approve", JSONH, {"id": a["id"]})
    assert status == 200 and res["action"]["status"] == "failed" and "nothing to stop" in res["action"]["result"]["error"]
    assert count(c) == 0
    status, page = call(app, "GET", "/api/settings/page", LOCAL)
    assert [x["status"] for x in page["mcp"]["pending_actions"]] == []            # decided: off the list


# --- 10. everywhere bills show up: API = CLI = plugin, and the Sunday summary's inputs ---------------------------------

def test_bill_totals_still_due_and_left_agree_between_api_cli_and_plugin(c, app, capsys):
    cap(c, "Rent & Utilities", 3000)
    socalgas(c)
    ladwp(c, "2026-07-31")                                               # next ~Sep 29: still due this month
    track(c, every="2m", varies=True, usually=[100, 150])
    p = plugin_ctx()
    rec_api = call(app, "GET", "/api/recurring", LOCAL)[1]
    bud_api = call(app, "GET", "/api/budgets", LOCAL)[1]["budgets"]
    code, out, _ = run_cli(["bills", "--next", "730d", "--json"], capsys)
    bills_cli = json.loads(out)
    code, out, _ = run_cli(["budget-status", "--json"], capsys)
    bud_cli = json.loads(out)["totals"]
    bills_plug, bud_plug = p.call("finance_bills", {"next": "730d"}), p.call("finance_budget_status", {})["totals"]
    assert rec_api["monthly_totals"] == bills_cli["monthly_totals"] == bills_plug["monthly_totals"] == {
        "bill": round(4.9 + 62.5, 2), "subscription": 0.0}
    assert bud_api["total_due"] == bud_cli["due"] == bud_plug["due"] == 125.0
    assert bud_api["left"] == bud_cli["left"] == bud_plug["left"] == round(3000 - bud_api["total_spend"] - 125.0, 2)
    fields = reads.BILL_FIELDS
    assert [{k: r[k] for k in fields} for r in rec_api["items"]] == bills_cli["items"] == bills_plug["items"]


def test_the_sunday_summary_inputs_carry_the_new_bills(c, app):
    """The weekly summary reads /api/overview, /api/budgets and /api/recurring: each shows a bill's estimate label,
    schedule, whether it changes and was tracked by hand."""
    socalgas(c)
    ladwp(c, "2026-07-31")
    track(c, every="2m", varies=True, usually=[100, 150])
    ov = call(app, "GET", "/api/overview", LOCAL)[1]
    recp = call(app, "GET", "/api/recurring", LOCAL)[1]
    for items in (ov["bills"], ov["upcoming"], recp["items"]):
        lad = next(b for b in items if b["merchant"] == LADWP)
        assert (lad["amount_label"], lad["cadence_label"], lad["variable"], lad["source"]) == \
            ("~$125 est.", "every 2 months", True, "tracked")
    assert next(b for b in ov["pace"]["bills_due"] if b["merchant"] == LADWP)["variable"] is True
    assert ov["recurring_totals"] == recp["monthly_totals"]

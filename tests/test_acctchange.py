"""The Accounts page's month change: each breakdown group's `month_start` (its total at the start of the current month)
and `month_change` ({amount, pct} on the signed basis, i.e. the growth of its net-worth contribution), both None when
any live account in the group has no balance on or before the first of the month."""

from __future__ import annotations

from datetime import date

import pytest

from factory import acct, bank, snapshot
from hermes_finance import insights, manual, reads

TODAY = date(2026, 10, 15)


def group(nw, key):
    return next(g for g in nw["breakdown"] if g["key"] == key)


def nw(conn):
    return insights.net_worth(conn, history=False, today=TODAY)


# --- Plaid baselines ---------------------------------------------------------------------------------

def test_an_increase(conn):
    bank(conn)
    acct(conn, "chk", current=2000.0)
    snapshot(conn, "chk", "2026-10-01", 1500.0)
    g = group(nw(conn), "cash")
    assert g["month_start"] == 1500.0
    assert g["month_change"] == {"amount": 500.0, "pct": pytest.approx(33.33, abs=0.01)}


def test_a_decrease(conn):
    bank(conn)
    acct(conn, "chk", current=1200.0)
    snapshot(conn, "chk", "2026-10-01", 1500.0)
    g = group(nw(conn), "cash")
    assert g["month_start"] == 1500.0
    assert g["month_change"] == {"amount": -300.0, "pct": pytest.approx(-20.0)}


def test_a_debt_that_came_down_is_a_positive_change(conn):
    """On the signed basis a paid-down debt is growth (good), and pct is of the start's absolute value."""
    bank(conn)
    acct(conn, "cc", type_="credit", subtype="credit card", current=403.0)
    snapshot(conn, "cc", "2026-10-01", 1000.0)
    g = group(nw(conn), "cards")
    assert g["month_start"] == 1000.0
    assert g["month_change"] == {"amount": 597.0, "pct": pytest.approx(59.7, abs=0.01)}


def test_a_debt_that_went_up_is_a_negative_change(conn):
    bank(conn)
    acct(conn, "cc", type_="credit", subtype="credit card", current=1200.0)
    snapshot(conn, "cc", "2026-10-01", 1000.0)
    g = group(nw(conn), "cards")
    assert g["month_start"] == 1000.0
    assert g["month_change"] == {"amount": -200.0, "pct": pytest.approx(-20.0)}


def test_no_history_at_month_start_is_none(conn):
    """A snapshot that only starts after the 1st means there is no baseline: both fields are None, not 0."""
    bank(conn)
    acct(conn, "chk", current=1000.0)
    snapshot(conn, "chk", "2026-10-05", 1000.0)
    g = group(nw(conn), "cash")
    assert g["month_start"] is None and g["month_change"] is None


def test_partial_baseline_is_none(conn):
    """One account with a baseline and one without: the whole group is unknown (None)."""
    bank(conn)
    acct(conn, "chk", current=2000.0)
    acct(conn, "sav", current=500.0)
    snapshot(conn, "chk", "2026-10-01", 1800.0)
    snapshot(conn, "sav", "2026-10-10", 500.0)   # after the 1st: no baseline
    g = group(nw(conn), "cash")
    assert g["month_start"] is None and g["month_change"] is None


# --- manual account baselines ------------------------------------------------------------------------

def test_a_manual_account_baseline(conn):
    m = manual.create(conn, {"kind": "bank", "name": "Piggy", "institution": "Ally", "subtype": "savings",
                             "balance": 600.0, "balance_as_of": "2026-09-30"}, TODAY)
    with conn:
        conn.execute("UPDATE manual_accounts SET balance = 900.0 WHERE id = ?", (m["id"],))
    g = group(nw(conn), "cash")
    assert g["month_start"] == 600.0
    assert g["month_change"] == {"amount": 300.0, "pct": pytest.approx(50.0)}


def test_a_manual_account_added_this_month_has_no_baseline(conn):
    manual.create(conn, {"kind": "bank", "name": "Piggy", "institution": "Ally", "subtype": "savings",
                         "balance": 600.0, "balance_as_of": "2026-10-02"}, TODAY)
    g = group(nw(conn), "cash")
    assert g["month_start"] is None and g["month_change"] is None


# --- pass-through to the reads payload --------------------------------------------------------------

def test_reads_networth_passes_the_change_through_and_narrows_to_none(conn):
    bank(conn)
    acct(conn, "chk", current=2000.0)
    snapshot(conn, "chk", "2026-10-01", 1500.0)
    full = reads.networth(conn, TODAY, history=False)
    cash = next(g for g in full["groups"] if g["key"] == "cash")
    assert cash["month_start"] == 1500.0 and cash["month_change"] == {"amount": 500.0, "pct": pytest.approx(33.33, abs=0.01)}
    custom = reads.networth(conn, TODAY, history=False, custom=True)
    ccash = next(g for g in custom["groups"] if g["key"] == "cash")
    assert ccash["month_start"] is None and ccash["month_change"] is None

"""Baseline K6 (2026-09-30): the gate's `features` DB (tests/gate_dbs.py) builds on any day, with every state its
docstring promises. At 0390032 it failed on 182 of the 365 days from 2026-09-30:
- on 43, no Groceries charge this month to split (a TypeError: Oct 1-8, Dec 1-6, ...);
- on 139, no Netflix charge for the agent's pending bill (a BillError): demo_data's Chase history starts on May 23 of the
  run's own year, so before June 12 there is none.
Every browser test and gate run on `features` errored in its setup on those days. On others a state went missing:
- no Eating Out charge this month for test_browser_task19's split in the sheet;
- fewer than 3 "trip" tags;
- the gas bill found from 8 charges on the 4th, 7 on every other day.

Each day builds its own DB and reads the features back through the app's own readers (the pages' API, insights, goals,
bills, transit), so the states are those the browser tests and the gates see. The days:
- month starts and ends;
- the 4th (the gas bill's window edge) and the 11th/12th (Netflix's day);
- a year's end and start;
- Feb 28/29 and Mar 1 of a leap year;
- a day in every month from January to June (May 22/23: demo_data's Chase history starts);
- the run's own day.

HF_FEATURES_DAILY=<first day>:<n> runs n days in a row instead (the acceptance sweep: 365 days)."""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

import gate_dbs
from hermes_finance import alpaca, api, bills, db, features, goals, insights, mcp_state, rollover, transit, txmeta

FIXED = ["2026-09-30", "2026-10-01", "2026-10-04", "2026-10-08", "2026-10-31", "2026-11-01", "2026-12-06", "2026-12-31",
         "2027-01-01", "2027-01-15", "2027-02-01", "2027-02-28", "2027-03-01", "2027-03-15", "2027-04-11", "2027-04-12",
         "2027-04-30", "2027-05-22", "2027-05-23", "2027-06-11", "2027-06-13", "2028-02-28", "2028-02-29", "2028-03-01"]


def _days() -> list[date]:
    spec = os.environ.get("HF_FEATURES_DAILY")
    if spec:
        first, n = spec.split(":")
        return [date.fromisoformat(first) + timedelta(days=k) for k in range(int(n))]
    return sorted({date.fromisoformat(d) for d in FIXED} | {date.today()})


DAYS = _days()
GOALS = ["Rainy day", "New laptop", "Concert tickets", "Trip to Lisbon", "Pay off Car Loan", "Monthly savings"]
NETFLIX_ASK = {"action": "track", "merchant": "Netflix", "every": "1m", "varies": False}
SCE = "Southern California Edison"


@pytest.fixture(scope="module", params=DAYS, ids=str)
def built(request, tmp_path_factory):
    day = request.param
    path = gate_dbs.build("features", tmp_path_factory.mktemp(f"f{day:%Y%m%d}") / "finance.db", day)
    conn = db.connect(path)
    yield day, conn
    conn.close()


def month_of(d: date, months: int = 0) -> str:
    return gate_dbs.demo_data_month_day(d, months, 1)[:7]


def spent(conn, month: str, category: str) -> float:
    """What `category` spent in `month`, summed from the tables (a split charge by its lines), not by insights."""
    whole = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM v_transactions WHERE month = ? AND category = ? AND "
                         "transaction_id NOT IN (SELECT transaction_id FROM transaction_splits)", (month, category)).fetchone()[0]
    lines = conn.execute("SELECT COALESCE(SUM(s.amount), 0) FROM transaction_splits s JOIN transactions t USING (transaction_id) "
                         "JOIN categories c ON c.id = s.category_id WHERE substr(t.date, 1, 7) = ? AND c.name = ?",
                         (month, category)).fetchone()[0]
    return round(whole + lines, 2)


def rows(conn, day: date, **params) -> list[dict]:
    return api.transactions(conn, {"limit": "500", **params}, day)["rows"]


def test_every_feature_state_is_there(built):
    day, conn = built
    month, prev = month_of(day), month_of(day, -1)
    # splits, this month and last: lines adding up to the charge, each under its own category; the note and tag
    now = [r for r in rows(conn, day, month=month, kind="spend") if r["split"]]
    last = [r for r in rows(conn, day, month=prev, kind="spend") if r["split"]]
    assert len(now) == len(last) == 1, (now, last)
    g, o = now[0], last[0]
    assert [x["category"] for x in g["split"]] == ["Groceries", "Other Shopping"], g
    assert g["split"][1]["amount"] == 22.25, g
    assert [x["category"] for x in o["split"]] == ["Other Shopping", "Entertainment"], o
    assert o["split"][1]["amount"] == 35.0, o
    for r, low in ((g, 40), (o, 60)):
        assert round(sum(x["amount"] for x in r["split"]), 2) == r["amount"] > low and not r["pending"], r
    assert g["date"] <= day.isoformat() and g["note"] == "Paper towels, detergent and trash bags on the Shopping line."
    assert g["tags"] == ["household"]
    trip = rows(conn, day, tag="trip")
    assert len(trip) == 3 and {r["category"] for r in trip} <= {"Other Transport", "Eating Out"}, trip
    assert all(not r["pending"] and r["tags"] == ["trip"] for r in trip), trip
    assert [(f["name"], f["filters"]) for f in txmeta.saved_filters(conn)] == [("Tagged: trip", {"tag": "trip"}),
                                                                                ("Amazon", {"q": "amazon"})]
    # test_browser_task19 splits this month's newest Eating Out charge in the sheet; the layout gate's tx-sheet-split
    # clicks the newest posted spending row and needs its Split button (a row not split yet)
    assert any(r["category"] == "Eating Out" and not r["pending"] and r["amount"] > 10 and not r["split"]
               for r in rows(conn, day, month=month, kind="spend"))
    newest = next(r for r in rows(conn, day, kind="spend") if not r["pending"])
    assert newest["split"] is None, newest
    # rollover: Eating Out's unused cap rolls in; Other Transport was overspent, so nothing rolls
    cats = {c["category"]: c for c in insights.category_spend(conn, month, day)}
    eat, move = cats["Eating Out"], cats["Other Transport"]
    carry = rollover.carry(250, spent(conn, prev, "Eating Out"))
    assert carry > 0 and eat["rollover"] == {"amount": carry, "from": prev} and eat["cap"] == round(250 + carry, 2), eat
    assert spent(conn, prev, "Other Transport") > 600 and move["rollover"] == {"amount": 0.0, "from": prev}, move
    # goals: every kind, the two earmarks in one account holding less than they set aside, the entries logged by hand
    p = goals.payload(conn, day)
    assert [x["name"] for x in p["goals"]] == GOALS
    short = [(x["name"], x["short"]) for x in p["goals"] if x["short"]]
    assert short == [("Concert tickets", {"amount": 179.63, "covered": 220.37})]
    assert [len(x["entries"] or []) for x in p["goals"] if x["name"] == "Trip to Lisbon"] == [4]
    # Alpaca: a week of ACTIVE snapshots up to today, equity = cash + its two positions
    snap = alpaca.latest(conn)
    assert (snap["date"], snap["status"], snap["positions"], snap["equity"]) == (day.isoformat(), "ACTIVE", 2, 1230.0)
    week = conn.execute("SELECT COUNT(*), MIN(date) FROM alpaca_snapshots").fetchone()
    assert tuple(week) == (7, (day - timedelta(days=6)).isoformat())
    # bills: the gas bill whose amount changes, LADWP by hand every 2 months, Netflix for the agent's pending change
    rec = {i["merchant"]: i for i in insights.recurring(conn, day)["items"]}
    lad, sce, nf = rec[gate_dbs.LADWP], rec[SCE], rec["Netflix"]
    pick = lambda i, *keys: tuple(i[k] for k in keys)  # noqa: E731
    assert pick(lad, "source", "cadence", "variable", "amount_label") == ("tracked", "bimonthly", True, "~$125 est.")
    assert lad["usual"] == {"low": 100.0, "high": 150.0, "source": "you"}
    assert pick(sce, "source", "cadence", "variable", "charges", "amount_label") == ("detected", "monthly", True, 7, "~$5 est.")
    assert pick(nf, "kind", "cadence", "variable", "amount") == ("subscription", "monthly", False, 15.49) and nf["charges"] >= 4
    ask = mcp_state.pending(conn)
    nxt = date.fromisoformat(nf["next_expected"])
    assert [(a["tool"], a["arguments"], a["client"]) for a in ask] == [("finance_track_bill", NETFLIX_ASK, "Claude Desktop")]
    assert ask[0]["summary"] == f"Track Netflix as a bill every month, the same amount each time (next ~{nxt:%b} {nxt.day}, $15.49)."
    assert bills.plan(conn, day, **NETFLIX_ASK)["summary"] == ask[0]["summary"]
    # the calendar's chips: more than 5 bills this month, with a logo, a category icon and initials among them
    cal = features.bills_calendar(conn, {}, day)["bills"]
    assert len(cal) > 5 and {"logo" if b.get("logo_url") else "icon" if b.get("icon_url") else "initials" for b in cal} == {
        "logo", "icon", "initials"}
    # the payment in transit: Nova Checking to the Chase card, yesterday
    moving = transit.entries(conn, day)
    assert [(e["amount"], e["card_id"], e["date"], e["from_account"]["name"]) for e in moving] == [
        (212.34, "acc_chase_cc", (day - timedelta(days=1)).isoformat(), "Nova Checking")]


def test_the_charges_behind_them_are_the_fixtures_own(built):
    """The charges the states rest on are the fixture's, dated from the day: the two split charges and a dinner on Nova
    Checking, Netflix on the Freedom Unlimited card on the 12th of each of the last 4 months, the 9 gas bills on the 4th
    (this month's from the 5th)."""
    day, conn = built
    first = day.replace(day=1).isoformat()
    got = {r[0]: tuple(r[1:]) for r in conn.execute(
        "SELECT transaction_id, date, account_id, amount, category, pending FROM v_transactions "
        "WHERE transaction_id IN ('feat_split_groceries', 'feat_split_order', 'feat_dinner')")}
    assert got == {"feat_split_groceries": (first, "acc_nova_chk", 86.47, "Groceries", 0),
                   "feat_split_order": (gate_dbs.demo_data_month_day(day, -1, 18), "acc_nova_chk", 148.60,
                                        "Other Shopping", 0),
                   "feat_dinner": (first, "acc_nova_chk", 36.85, "Eating Out", 0)}
    want = sorted(gate_dbs.demo_data_month_day(day, -k - (day.day < 12), 12) for k in range(4))
    assert all(d <= day.isoformat() for d in want)
    netflix = conn.execute("SELECT date, account_id, amount, category FROM v_transactions WHERE merchant_name = 'Netflix' "
                           "AND date >= ? ORDER BY date", (want[0],)).fetchall()
    assert [tuple(r) for r in netflix] == [(d, "acc_chase_cc", 15.49, "Entertainment") for d in want]
    gas = [r[0] for r in conn.execute("SELECT date FROM transactions WHERE transaction_id LIKE 'sce_%' ORDER BY date")]
    k0 = 0 if day.day > 4 else -1
    assert gas == [gate_dbs.demo_data_month_day(day, k0 - 8 + k, 4) for k in range(9)] and gas[-1] <= day.isoformat()


def test_the_days_cover_the_edges():
    """The fixed days hold the edges named above (and the 2 days that failed at 0390032 in the owner's run)."""
    days = {date.fromisoformat(d) for d in FIXED}
    assert {date(2026, 10, 1), date(2027, 1, 15), date(2026, 9, 30)} <= days
    assert date(2028, 2, 29) in days and {d.month for d in days if d.year == 2027} >= {1, 2, 3, 4, 5, 6}
    assert any(d.day == 1 for d in days) and any((d + timedelta(days=1)).day == 1 for d in days)
    assert {date(2026, 12, 31), date(2027, 1, 1)} <= days and {4, 11, 12} <= {d.day for d in days}

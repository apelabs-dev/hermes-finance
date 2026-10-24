"""TASK-19 (Batch C): the bills calendar on Recurring, laid out from /api/recurring's own items (not a copy of its
detection): paid on the last charge's day, due on the next expected day and the ones after it at the cadence. The
month total is the sum of that month's bills; the week starts on the Region setting's day (read defensively)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

import factory as f
import gate_dbs
from hermes_finance import api, billcal, config, db, features
from hermes_finance.web import App

TODAY = date(2026, 9, 28)


def item(merchant, cadence, last, nxt, amount, status="upcoming", category="Rent & Utilities"):
    return {"merchant": merchant, "key": merchant.lower(), "cadence": cadence, "last_date": last, "next_expected": nxt,
            "amount": amount, "status": status, "category": category, "kind": "bill", "logo_url": None, "icon_url": None}


@pytest.mark.parametrize("it, month, want", [
    (item("Rent", "monthly", "2026-09-03", "2026-10-04", 2252.0), "2026-09", [("2026-09-03", "paid", False)]),
    (item("Rent", "monthly", "2026-09-03", "2026-10-04", 2252.0), "2026-10", [("2026-10-04", "due", False)]),
    (item("Rent", "monthly", "2026-09-03", "2026-10-04", 2252.0), "2026-12", [("2026-12-04", "due", True)]),
    (item("Car", "monthly", "2026-08-30", "2026-09-29", 465.10), "2026-09", [("2026-09-29", "due", False)]),
    (item("Car", "monthly", "2026-08-31", "2026-10-31", 20.0), "2027-02", [("2027-02-28", "due", True)]),       # clamped
    (item("Gym", "weekly", "2026-09-26", "2026-10-03", 12.0), "2026-10",
     [("2026-10-03", "due", False), ("2026-10-10", "due", True), ("2026-10-17", "due", True), ("2026-10-24", "due", True),
      ("2026-10-31", "due", True)]),
    (item("Domain", "yearly", "2025-10-01", "2026-10-01", 12.99), "2026-10", [("2026-10-01", "due", False)]),
    (item("Domain", "yearly", "2025-10-01", "2026-10-01", 12.99), "2026-11", []),
    (item("Cable", "monthly", "2026-08-14", "2026-09-14", 89.99, status="late"), "2026-09", [("2026-09-14", "late", False)]),
    (item("Cable", "monthly", "2026-08-14", "2026-09-14", 89.99, status="late"), "2026-10", [("2026-10-14", "due", True)]),
    (item("Short", "monthly", "2026-09-01", "2026-09-29", 5.0), "2026-09", [("2026-09-01", "paid", False), ("2026-09-29", "due", False)])])
def test_occurrences(it, month, want):
    y, m = int(month[:4]), int(month[5:])
    start = date(y, m, 1)
    end = (date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1))
    assert [(o["date"], o["state"], o["projected"]) for o in billcal.occurrences(it, start, end, TODAY)] == want


def test_the_month_total_is_the_sum_of_its_bills_from_the_recurring_endpoint(data_dir):
    path = config.data_dir() / "full.db"
    gate_dbs.build("full", path, TODAY)
    conn = db.connect(path)
    rec = api.recurring_page(conn, {}, TODAY)
    for m in ("2026-09", "2026-10", "2027-08"):
        cal = billcal.calendar_month(conn, m, TODAY, rec)
        y, mm = int(m[:4]), int(m[5:])
        start, end = date(y, mm, 1), date(y + mm // 12, mm % 12 + 1, 1) - timedelta(days=1)
        want = sorted(((o["date"], r["merchant"], r["amount"]) for r in rec["items"] for o in billcal.occurrences(r, start, end, TODAY)),
                      key=lambda w: (w[0], w[1].casefold()))
        assert [(b["date"], b["merchant"], b["amount"]) for b in cal["bills"]] == want
        assert cal["total"] == round(sum(w[2] for w in want), 2) == round(cal["paid_total"] + cal["due_total"], 2)
        assert cal["count"] == len(want)
    sep = billcal.calendar_month(conn, "2026-09", TODAY, rec)
    paid = [r for r in rec["items"] if r["last_date"].startswith("2026-09")]
    assert sep["paid_count"] == len(paid) and sep["paid_total"] == round(sum(r["amount"] for r in paid), 2)
    assert (sep["prev"], sep["next"], sep["first_month"], sep["last_month"]) == (None, "2026-10", "2026-09", "2027-08")
    with pytest.raises(billcal.CalendarError, match="2026-09 to 2027-08"):
        billcal.calendar_month(conn, "2026-08", TODAY, rec)
    with pytest.raises(billcal.CalendarError):
        billcal.calendar_month(conn, "2027-09", TODAY, rec)
    conn.close()


@pytest.mark.parametrize("rows, want", [
    ({}, (0, "default")),
    ({"region": '{"week_start": "monday", "currency": "USD"}'}, (1, "settings:region.week_start")),
    ({"region": '{"week_starts": "Sunday"}'}, (0, "settings:region.week_starts")),
    ({"region": '{"first_day_of_week": 1}'}, (1, "settings:region.first_day_of_week")),
    ({"region": '{"weekStart": "mon"}'}, (1, "settings:region.weekStart")),
    ({"week_start": '"monday"'}, (1, "settings:week_start")), ({"week_start": "monday"}, (1, "settings:week_start")),
    ({"region": "not json {"}, (0, "default")), ({"region": '{"week_start": "friday"}'}, (0, "default")),
    ({"region": '{"week_start": true}'}, (0, "default")), ({"region": "[1, 2]"}, (0, "default"))])
def test_the_week_start_is_read_defensively(conn, rows, want):
    with conn:
        for k, v in rows.items():
            conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, 'x')", (k, v))
    assert billcal.week_start(conn) == want


def test_the_route(conn, client):
    f.bank(conn, "item_a", "Chase")
    f.acct(conn, "acc_chk", current=1000.0)
    for back in (90, 60, 30):
        f.tx(conn, (TODAY - timedelta(days=back)).isoformat(), 64.84, "Multifamily Utility Company", cat="Rent & Utilities",
             logo="https://evil.example/x.png")
    app = App(config.db_path(), lambda: client)
    app.today = lambda: TODAY
    status, _, raw = app.handle("GET", "/api/bills-calendar?month=2026-10", {"Host": "127.0.0.1:8765"}, b"")
    d = json.loads(raw)
    assert status == 200 and d["month"] == "2026-10" and d["total"] == 64.84 and d["bills"][0]["merchant"] == "Multifamily Utility Company"
    assert d["bills"][0]["logo_url"] is None                                  # the recurring endpoint's own image check
    assert app.handle("GET", "/api/bills-calendar?month=2025-01", {"Host": "127.0.0.1:8765"}, b"")[0] == 400
    assert app.handle("GET", "/api/bills-calendar?month=nope", {"Host": "127.0.0.1:8765"}, b"")[0] == 400
    assert features.bills_calendar(conn, {}, TODAY)["month"] == "2026-09"

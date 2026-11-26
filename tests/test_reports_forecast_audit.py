"""Forecast edge cases recomputed from the demo database, with only the relevant rows retained or changed."""
from contextlib import closing
from datetime import date, timedelta

import pytest

import demo_data
from hermes_finance import billcal, db, insights, reports, reports_page

TODAY = date(2026, 11, 15)
PAYCHECK = "acc_nova_chk|USD|electronic deposit orbitex l"


@pytest.fixture
def demo(tmp_path):
    with closing(db.connect(tmp_path / "forecast.sqlite")) as conn:
        demo_data.build(conn, today=TODAY)
        yield conn


def test_refund_only_history_does_not_project_new_cash(demo):
    with demo:
        demo.execute("DELETE FROM transactions WHERE merchant_name IS NOT 'Trader Joe''s' OR date != "
                     "(SELECT MIN(date) FROM transactions WHERE merchant_name = 'Trader Joe''s' AND date >= '2026-10-01')")
        demo.execute("UPDATE transactions SET amount = -31 WHERE merchant_name = 'Trader Joe''s'")
    f = reports.forecast(demo, TODAY, 30)
    assert f["everyday"]["total"] < 0  # still show the actual signed history
    assert f["everyday"]["rate"] == 0
    assert f["expected"] == {"in": 0, "out": 0, "net": 0}
    for line in f["lines"].values():
        assert {p["balance"] for p in line["points"]} == {4200.37}


@pytest.mark.parametrize("year,last,next_due,after", [
    # The first projection clamps to February, then recovers the 31st.
    (2024, "2024-01-31", "2024-02-29", "2024-03-31"),
    (2025, "2025-01-31", "2025-02-28", "2025-03-31"),
    # The latest actual charge itself was clamped; recover the 31st from its predecessors.
    (2024, "2024-02-29", "2024-03-31", "2024-04-30"),
])
def test_month_end_bill_recovers_day_31(demo, year, last, next_due, after):
    today = date.fromisoformat(last)
    charges = [f"{year - 1}-12-31", f"{year}-01-31", last] if today.month == 2 else [
        f"{year - 1}-11-30", f"{year - 1}-12-31", last]
    with demo:
        ids = [r[0] for r in demo.execute("SELECT transaction_id FROM transactions WHERE merchant_name = 'Netflix' LIMIT 3")]
        demo.execute("DELETE FROM transactions WHERE transaction_id NOT IN (?, ?, ?)", ids)
        demo.executemany("UPDATE transactions SET date = ? WHERE transaction_id = ?", zip(charges, ids, strict=True))
    item, = insights.recurring(demo, today)["items"]
    assert item["next_expected"] == next_due
    f = reports.forecast(demo, today, 90)
    assert [(e["date"], e["amount"]) for e in f["events"]][:2] == [(next_due, -15.49), (after, -15.49)]
    # Bills uses the same calendar; even a window beginning after the clamped date keeps its anchor.
    month = date.fromisoformat(after).replace(day=1)
    want = billcal.calendar_month(demo, month.isoformat()[:7], today)
    assert [b["date"] for b in want["bills"]] == [after]


def test_monthly_paycheck_uses_calendar_date_at_both_window_boundaries(demo):
    today = date(2024, 2, 29)
    with demo:
        ids = [r[0] for r in demo.execute("SELECT transaction_id FROM transactions WHERE pfc_detailed = 'INCOME_WAGES' LIMIT 3")]
        demo.execute("DELETE FROM transactions WHERE transaction_id NOT IN (?, ?, ?)", ids)
        demo.executemany("UPDATE transactions SET date = ? WHERE transaction_id = ?",
                         zip(("2023-11-30", "2023-12-31", "2024-01-31"), ids, strict=True))
    src, = reports.detected_paychecks(demo, today)["sources"]
    assert src["cadence"] == "monthly" and src["next_expected"] == "2024-02-29" and src["amount"] == 2353.10
    reports.save_paycheck_answer(demo, src["key"], "add")
    f = reports_page.forecast(demo, {"until": "2024-03-31"}, today)
    assert f == reports_page.forecast(demo, {"days": "32"}, today)
    assert [e["date"] for e in f["events"]] == ["2024-02-29", "2024-03-31"]
    assert f["expected"]["in"] == 4706.20


def test_late_paycheck_is_counted_once_today_like_a_late_bill(demo):
    """Characterization (passes on the audited commit too): a paycheck expected before today lands once today,
    flagged overdue, and its later cycles come from the window; the missed cycles are not one deposit each."""
    today = date(2026, 11, 26)
    src, = reports.detected_paychecks(demo, today)["sources"]
    assert src["next_expected"] == "2026-11-24"
    f = reports.forecast(demo, today, 14, paychecks=[PAYCHECK])
    assert [(e["date"], e["amount"], e["overdue"]) for e in f["events"] if e["kind"] == "paycheck"] == [
        ("2026-11-26", 2353.10, True), ("2026-12-08", 2353.10, False)]
    assert f["expected"]["in"] == 4706.20


def test_empty_history_is_not_full_coverage(demo):
    with demo:
        demo.execute("DELETE FROM transactions")
    f = reports.forecast(demo, TODAY, 1)
    assert f["everyday"]["coverage"] == "partial" and f["everyday"]["history_from"] is None
    assert f["everyday"]["total"] == f["everyday"]["rate"] == 0
    assert f["expected"] == {"in": 0, "out": 0, "net": 0}
    assert f["lowest"] == {"date": TODAY.isoformat(), "balance": 4200.37}


def test_foreign_history_does_not_count_as_display_currency_coverage(demo):
    with demo:
        demo.execute("UPDATE transactions SET iso_currency = 'EUR' WHERE date < '2026-10-15'")
    f = reports.forecast(demo, TODAY, 1)
    assert f["everyday"]["coverage"] == "partial" and f["everyday"]["history_from"] >= "2026-10-15"
    assert any("everyday spending only has history from" in w for w in f["warnings"])


def _keep_charges(conn, merchant, charges):
    """Use demo rows so their classification, accounts and currencies stay real to the test."""
    rows = conn.execute("SELECT transaction_id FROM transactions WHERE merchant_name = ? ORDER BY transaction_id",
                        (merchant,)).fetchall()
    assert len(rows) >= len(charges), merchant
    conn.executemany("DELETE FROM transactions WHERE transaction_id = ?", rows[len(charges):])
    conn.executemany("UPDATE transactions SET date = ?, amount = ?, pending = 0 WHERE transaction_id = ?",
                     [(d, amount, tid) for (d, amount), (tid,) in zip(charges, rows[:len(charges)], strict=True)])


def test_demo_events_rates_and_daily_balances_reconcile_at_30_60_90_days(demo):
    with demo:
        demo.execute("DELETE FROM transactions WHERE COALESCE(merchant_name, '') NOT IN "
                     "('SpotHero', 'AT&T', 'Netflix', 'Namecheap', 'GEICO', 'Innago', 'Trader Joe''s') "
                     "AND pfc_detailed != 'INCOME_WAGES'")
        _keep_charges(demo, "SpotHero", [(d, 7) for d in ("2026-10-25", "2026-11-01", "2026-11-08")])
        _keep_charges(demo, "AT&T", [(d, 45) for d in ("2026-10-11", "2026-10-25", "2026-11-08")])
        _keep_charges(demo, "Netflix", [(d, 15) for d in ("2026-09-12", "2026-10-12", "2026-11-12")])
        _keep_charges(demo, "Namecheap", [(d, 13) for d in ("2024-12-14", "2025-12-14")])
        _keep_charges(demo, "GEICO", [(d, 165) for d in ("2026-08-14", "2026-09-14", "2026-10-14")])
        _keep_charges(demo, "Innago", [("2026-08-01", 2252)])  # fixed category, not a detected bill
        _keep_charges(demo, "Trader Joe's", [("2026-07-31", 600), ("2026-08-01", 380), ("2026-09-03", 600),
                                             ("2026-10-31", -60), ("2026-11-01", 900)])
    reports.save_paycheck_answer(demo, PAYCHECK, "add")
    want_bills = {
        "SpotHero": ("weekly", "2026-11-15", -7, range(0, 90, 7)),
        "AT&T": ("biweekly", "2026-11-22", -45, range(7, 90, 14)),
        "Netflix": ("monthly", "2026-12-12", -15, (27, 58, 89)),
        "Namecheap": ("yearly", "2026-12-14", -13, (29,)),
        "GEICO": ("monthly", "2026-11-14", -165, (0, 29, 60)),  # late once today, then 14 Dec and 14 Jan
    }
    assert {r["merchant"]: (r["cadence"], r["next_expected"]) for r in insights.recurring(demo, TODAY)["items"]} == {
        label: (cadence, due) for label, (cadence, due, _, _) in want_bills.items()}
    as_day = lambda offset: (TODAY + timedelta(days=offset)).isoformat()
    for days in (30, 60, 90):
        f = reports_page.forecast(demo, {"days": str(days)}, TODAY)
        assert f == reports_page.forecast(demo, {"until": as_day(days - 1)}, TODAY)
        assert f["horizon"] == {"days": days, "start": as_day(0), "end": as_day(days - 1)}
        assert f["available_date"] == as_day(days - 1)
        assert f["opening"]["balance"] == 4200.37 and f["opening"]["as_of"] == as_day(0)
        assert not f["opening"]["stale"]
        everyday = f["everyday"]  # 380 + 600 - 60 = 920; divide by the 92 days in Aug–Oct
        assert (everyday["start"], everyday["end"], everyday["days"], everyday["total"], everyday["rate"]) == (
            "2026-08-01", "2026-10-31", 92, 920, 10)
        bills = [(label, i, amt) for label, (_, _, amt, offsets) in want_bills.items() for i in offsets if i < days]
        assert sorted((e["label"], e["date"], e["amount"]) for e in f["events"] if e["amount"] < 0) == sorted(
            (label, as_day(i), amt) for label, i, amt in bills)
        assert [(e["label"], e["date"]) for e in f["events"] if e["overdue"]] == [("GEICO", as_day(0))]
        paydays = range(9, days, 14)  # Nov 10 + 14 = Nov 24, then every 14 days
        assert [(e["date"], e["amount"]) for e in f["events"] if e["kind"] == "paycheck"] == [
            (as_day(i), 2353.10) for i in paydays]
        money_in, bills_total = round(len(paydays) * 2353.10, 2), -sum(amt for _, _, amt in bills)
        money_out = bills_total + 10 * days
        assert f["bills_total"] == bills_total
        assert f["expected"] == {"in": money_in, "out": money_out, "net": round(money_in - money_out, 2)}
        for key, rate in (("lower", 7.5), ("expected", 10), ("higher", 12.5)):
            points = [{"date": as_day(i), "balance": round(4200.37 + 2353.10 * sum(p <= i for p in paydays)
                                                            + sum(amt for _, j, amt in bills if j <= i)
                                                            - rate * (i + 1), 2)} for i in range(days)]
            line = f["lines"][key]
            assert line["daily"] == rate and line["points"] == points and line["end"] == points[-1]["balance"]
            assert line["lowest"] == min(points, key=lambda p: p["balance"])
        lower_spend, expected, higher_spend = (f["lines"][k]["points"] for k in ("lower", "expected", "higher"))
        assert all(hi["balance"] <= mid["balance"] <= lo["balance"]
                   for hi, mid, lo in zip(higher_spend, expected, lower_spend, strict=True))
        assert f["lowest"] == f["lines"]["expected"]["lowest"]
        assert f["available"] == round(4200.37 + f["expected"]["net"], 2)


def test_demo_available_balance_freshness_and_saved_account_choice(demo):
    with demo:
        demo.execute("UPDATE accounts SET iso_currency = 'EUR' WHERE account_id = 'acc_nova_vault'")
        demo.execute("UPDATE accounts SET available_balance = NULL WHERE account_id = 'acc_nova_sav'")
        demo.execute("DELETE FROM balance_snapshots WHERE account_id = 'acc_nova_chk' AND date > '2026-11-12'")
    f = reports_page.forecast(demo, {"days": "1"}, TODAY)
    opening = f["opening"]
    assert (opening["balance"], opening["stale"], opening["stale_days"], opening["as_of"]) == (
        4200.37, True, 3, "2026-11-12")  # 2410.37 + 1790 available, not 1840 current
    assert [a["account_id"] for a in opening["accounts"]] == ["acc_nova_chk", "acc_usb_chk"]
    assert [a["account_id"] for a in f["unavailable_accounts"]] == ["acc_nova_sav"]
    assert [(a["currency"], a["available"]) for a in f["other_currencies"]] == [("EUR", 0.42)]
    assert any("bank balance from 2026-11-12" in w for w in f["warnings"])
    choice = reports.save_forecast_accounts(demo, ["acc_usb_chk", "acc_usb_chk", "acc_nova_vault", "acc_nova_sav", "acc_chase_cc"])
    assert choice == {"accounts": ["acc_usb_chk"], "dropped": ["acc_nova_vault", "acc_nova_sav", "acc_chase_cc"]}
    reports.save_paycheck_answer(demo, PAYCHECK, "add")
    saved = reports_page.forecast(demo, {"days": "30"}, TODAY)
    assert saved["opening"]["balance"] == 1790 and not saved["opening"]["stale"]
    assert saved["expected"]["in"] == 0 and saved["paychecks"]["excluded_by_account"][0]["key"] == PAYCHECK
    reports.save_forecast_accounts(demo, [])
    none = reports_page.forecast(demo, {"days": "30"}, TODAY)
    assert none["opening"] == {"balance": 0, "as_of": None, "stale": False, "stale_days": 0, "accounts": []}
    assert none["account_options"]["saved"] == [] and not any(a["ticked"] for a in none["account_options"]["accounts"])
    assert none["expected"]["in"] == 0 and "no cash account is in the line yet" in none["warnings"]


def test_paycheck_on_the_first_and_last_day_of_the_window_counts_once_each(demo):
    """Characterization (passes on the audited commit too): the horizon is inclusive at both ends."""
    with demo:
        ids = [r[0] for r in demo.execute("SELECT transaction_id FROM transactions WHERE pfc_detailed = 'INCOME_WAGES' LIMIT 3")]
        demo.execute("DELETE FROM transactions WHERE transaction_id NOT IN (?, ?, ?)", ids)
        days_ago = (TODAY - timedelta(days=42), TODAY - timedelta(days=28), TODAY - timedelta(days=14))
        demo.executemany("UPDATE transactions SET date = ? WHERE transaction_id = ?",
                         zip((d.isoformat() for d in days_ago), ids, strict=True))
    src, = reports.detected_paychecks(demo, TODAY)["sources"]
    assert src["next_expected"] == TODAY.isoformat() and src["cadence"] == "biweekly"
    one = reports.forecast(demo, TODAY, 1, paychecks=[src["key"]])
    assert [(e["date"], e["overdue"]) for e in one["events"]] == [(TODAY.isoformat(), False)]
    window = reports.forecast(demo, TODAY, 15, paychecks=[src["key"]])
    assert [e["date"] for e in window["events"]] == [TODAY.isoformat(), (TODAY + timedelta(days=14)).isoformat()]
    assert window["horizon"]["end"] == (TODAY + timedelta(days=14)).isoformat()
    assert len(reports.forecast(demo, TODAY, 14, paychecks=[src["key"]])["events"]) == 1

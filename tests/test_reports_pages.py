"""The Reports pages' numbers (0.5, B2): the year heatmap's per-day series, the 12-month view and its four KPIs, the
in-place drill-down, the forecast's saved account ticks and paycheck answers, and the HTTP payloads the three pages draw.

Synthetic data only. Every number is checked against the B1 functions it must equal (category_totals, money_flow,
remaining_after_spending, forecast), not a copy of it.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from hermes_finance import api, config, insights, reports, web

TODAY = date(2026, 11, 15)
TS = "2026-11-15T20:00:00Z"
LOCAL = {"Host": "127.0.0.1:8765"}
POST = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
PFC = {"groceries": ("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES"),
       "eating": ("FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT"),
       "rent": ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_RENT"),
       "wages": ("INCOME", "INCOME_WAGES")}


def _account(conn, account_id, name, type_, subtype, current, available, currency="USD"):
    conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, "
                 "available_balance, iso_currency, updated_at) VALUES (?, 'item_a', ?, ?, ?, ?, ?, ?, ?, ?)",
                 (account_id, name, account_id[-4:], type_, subtype, current, available, currency, TS))


def _txn(conn, tid, day, amount, name, pfc, account="acc_chk", currency="USD", merchant=None):
    primary, detailed = PFC[pfc]
    conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount, "
                 "iso_currency, pending, pfc_primary, pfc_detailed, removed, first_seen_at, updated_at) "
                 "VALUES (?, ?, 'item_a', ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?)",
                 (tid, account, day, name, merchant, amount, currency, primary, detailed, TS, TS))


@pytest.fixture
def books(conn):
    """14 months of history to 2026-11, so a year ending 2026-11 is full and one ending 2025-12 has hatched months.
    Two paycheck sources share the label ACME PAYROLL on different accounts (the source key question), rent is a
    monthly bill, and savings reports an available balance so the forecast can tick it in."""
    with conn:
        conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                     "VALUES ('item_a', 'ins_1', 'Bank', 'tok', 'active', ?)", (TS,))
        _account(conn, "acc_chk", "Everyday checking", "depository", "checking", 2100.00, 2000.00)
        _account(conn, "acc_sav", "Rainy-day savings", "depository", "savings", 5000.00, 5000.00)
        _account(conn, "acc_eur", "Lisbon account", "depository", "checking", 700.00, 700.00, "EUR")
        _account(conn, "acc_loan", "Car loan", "loan", "auto", 3000.00, None)
        _account(conn, "acc_inv", "Brokerage", "investment", "brokerage", 900.00, None)
        months = [insights.add_months("2025-09", i) for i in range(15)]          # 2025-09 .. 2026-11
        for i, m in enumerate(months):
            if m >= "2025-10":                                                  # the first transaction is 2025-10-03
                _txn(conn, f"g{i}", f"{m}-03", 100.00 + i * 10, "Market", "groceries")
                _txn(conn, f"e{i}", f"{m}-09", 40.00, "Diner", "eating")
            if m >= "2025-09":
                _txn(conn, f"r{i}", f"{m}-01", 1000.00, "Rent Co", "rent", merchant="Rent Co")
            if m in ("2026-09", "2026-10", "2026-11"):                          # 3 monthly paychecks per source
                _txn(conn, f"p{i}", f"{m}-05", -1500.00, "ACME PAYROLL", "wages", merchant="Acme Corp")
                _txn(conn, f"s{i}", f"{m}-06", -800.00, "ACME PAYROLL", "wages", account="acc_sav", merchant="Acme Corp")
        _txn(conn, "x1", "2026-11-03", 25.00, "Mercado", "groceries", account="acc_eur", currency="EUR")
        _txn(conn, "ref1", "2026-11-08", -30.00, "Market refund", "groceries")
        conn.execute("INSERT INTO balance_snapshots (account_id, date, current, available, iso_currency) "
                     "VALUES ('acc_chk', '2026-11-10', 2100.00, 2000.00, 'USD')")
    return conn


@pytest.fixture
def app(conn):
    return web.App(config.db_path(), lambda: None, "127.0.0.1", 8765, (), ())


def _get(app, path):
    status, ctype, body = app.handle("GET", path, LOCAL, b"")
    assert ctype == "application/json", (status, ctype, body[:200])
    return status, json.loads(body)


def _post(app, path, payload):
    status, ctype, body = app.handle("POST", path, POST, json.dumps(payload).encode())
    assert ctype == "application/json", (status, ctype, body[:200])
    return status, json.loads(body)


# --- 1. The per-day series is the one the month cells are built from (carry-over 4) ----------------------------

def test_the_day_series_sums_to_the_month(books):
    series = reports.spending_by_day(books, "2026-11-01", "2026-11-30")
    totals = reports.category_totals(books, "2026-11-01", "2026-11-30")
    assert sum(d["net"] for d in series["days"]) == pytest.approx(totals["totals"]["net"], abs=0.01)
    # net of refunds, exactly like category_totals: the 30.00 refund is a negative day
    assert [d["net"] for d in series["days"] if d["date"] == "2026-11-08"] == [-30.00]
    # the EUR account is never in the dollar series
    assert all(d["net"] != 25.00 for d in series["days"])


def test_year_cells_sum_to_each_months_net(books):
    year = reports.year_spending(books, TODAY, end_month="2026-11")
    assert [m["month"] for m in year["months"]][0] == "2025-12"
    assert [m["month"] for m in year["months"]][-1] == "2026-11" and len(year["months"]) == reports.YEAR_MONTHS
    for row in year["months"]:
        start, end = f"{row['month']}-01", f"{row['month']}-{insights.days_in_month(row['month']):02d}"
        expected = reports.category_totals(books, start, end)["totals"]["net"]
        cells = sum(c["net"] for cat in year["categories"] for c in cat["months"] if c["month"] == row["month"])
        assert row["spending"] == pytest.approx(expected, abs=0.01), row["month"]
        assert cells == pytest.approx(expected, abs=0.01), row["month"]


def test_a_month_before_the_first_transaction_is_nodata_not_zero(books):
    year = reports.year_spending(books, TODAY, end_month="2025-12")
    by_month = {m["month"]: m for m in year["months"]}
    assert year["data_from"] == "2025-09-01"                                # the rent charge is the first row
    assert all(by_month[f"2025-0{i}"]["nodata"] for i in range(1, 9))        # Jan-Aug: hatched
    assert not any(by_month[m]["nodata"] for m in ("2025-09", "2025-10", "2025-11", "2025-12"))
    assert all(c["nodata"] for cat in year["categories"] for c in cat["months"] if c["month"] == "2025-05")


# --- 2. The four KPIs (and their wording: remaining, never a percent of confidence) ----------------------------

def test_the_kpis_are_the_months_own_numbers(books):
    year = reports.year_spending(books, TODAY, end_month="2026-11")
    k = year["kpis"]
    data = [m for m in year["months"] if not m["nodata"]]
    complete = [m for m in data if m["month"] != "2026-11"]                  # the current month is partial
    assert k["months"] == len(complete)
    assert k["average"] == pytest.approx(round(sum(m["spending"] for m in complete) / len(complete), 2), abs=0.01)
    best = max(data, key=lambda m: m["remaining"])
    assert k["best"]["month"] == best["month"] and k["best"]["remaining"] == best["remaining"]
    assert k["savings_rate"]["now"] == data[-1]["share"]
    income = sum(m["income"] for m in complete)
    saved = sum(m["remaining"] for m in complete)
    assert k["savings_rate"]["average"] == pytest.approx(round(saved / income * 100, 2), abs=0.01)
    assert k["savings_rate"]["delta"] == pytest.approx(round(k["savings_rate"]["now"] - k["savings_rate"]["average"], 2), abs=0.01)
    assert set(k["most_variable"]) >= {"category", "from", "to", "spread"}


def test_the_kpis_never_say_confidence_or_a_percentage_of_one(books):
    year = reports.year_spending(books, TODAY)
    assert "confidence" not in json.dumps(year).lower()
    # every percentage is a share of income (savings rate), not an interval around the future
    assert year["kpis"]["savings_rate"]["now"] is not None


def test_bad_months_and_sizes_are_refused(books):
    with pytest.raises(reports.ReportError):
        reports.year_spending(books, TODAY, months=1)
    with pytest.raises(reports.ReportError):
        reports.year_spending(books, TODAY, months=reports.MAX_YEAR_MONTHS + 1)
    with pytest.raises(reports.ReportError):
        reports.year_spending(books, TODAY, end_month="2026-13")
    with pytest.raises(reports.ReportError):
        reports.year_spending(books, TODAY, end_month="2026-12")             # after this month


# --- 3. The drill-down (q3): the same Gross / Refunds / Net, its rows, nothing dropped --------------------------

def test_activity_matches_the_category_totals(books):
    totals = reports.category_totals(books, "2026-11-01", "2026-11-30")
    groceries = next(c for c in totals["categories"] if c["category"] == "Groceries")
    d = reports.category_activity(books, groceries["category_id"], "2026-11-01", "2026-11-30")
    assert (d["gross"], d["refunds"], d["net"]) == (groceries["gross"], groceries["refunds"], groceries["net"])
    assert d["count"] == len(d["rows"]) == groceries["count"]
    assert [r["amount"] for r in d["rows"] if r["amount"] < 0] == [-30.00]  # the refund is a row, not dropped
    assert sum(r["amount"] for r in d["rows"]) == pytest.approx(d["net"], abs=0.01)


def test_activity_refuses_an_unknown_category(books):
    with pytest.raises(reports.ReportError):
        reports.category_activity(books, 999999, "2026-11-01", "2026-11-30")
    with pytest.raises(reports.ReportError):
        reports.category_activity(books, 0, "2026-11-01", "2026-11-30")


# --- 4. The forecast's account ticks are saved and read (carry-over 1) -----------------------------------------

def test_the_account_choice_defaults_to_checking_then_reads_the_saved_choice(books):
    assert reports.forecast_account_choice(books) is None
    assert [a["account_id"] for a in reports.forecast(books, TODAY, 30)["accounts"]] == ["acc_chk"]
    saved = reports.save_forecast_accounts(books, ["acc_chk", "acc_sav", "acc_loan", "nope"])
    assert saved == {"accounts": ["acc_chk", "acc_sav"], "dropped": ["acc_loan", "nope"]}   # loans never, unknown never
    assert reports.forecast_account_choice(books) == ["acc_chk", "acc_sav"]
    payload = reports.forecast(books, TODAY, 30)
    assert [a["account_id"] for a in payload["accounts"]] == ["acc_chk", "acc_sav"]
    assert payload["opening"]["balance"] == pytest.approx(7000.00, abs=0.01)  # 2,000 + 5,000 available


def test_an_empty_choice_is_no_line_not_the_default(books):
    reports.save_forecast_accounts(books, [])
    assert reports.forecast_account_choice(books) == []
    payload = reports.forecast(books, TODAY, 30)
    assert payload["accounts"] == [] and payload["opening"]["balance"] == 0.0
    assert any("no cash account" in w for w in payload["warnings"])


def test_a_malformed_or_oversized_choice_reads_as_the_default(books):
    for value in ("{not json", json.dumps({"acc_chk": True}), json.dumps(["x" * 40] * 400)):
        reports.save_forecast_accounts(books, ["acc_chk"])
        with books:
            books.execute("UPDATE settings SET value = ? WHERE key = ?", (value, reports.FORECAST_ACCOUNTS_SETTING))
        assert reports.forecast_account_choice(books) is None
        assert [a["account_id"] for a in reports.forecast(books, TODAY, 30)["accounts"]] == ["acc_chk"]


def test_the_saved_choice_caps_at_the_entry_cap(books):
    ids = [f"a{i}" for i in range(reports.MAX_FORECAST_ACCOUNTS + 5)]
    saved = reports.save_forecast_accounts(books, ids)
    assert saved["accounts"] == [] and len(saved["dropped"]) == len(ids)


# --- 5. Paycheck answers are keyed by the source, exactly add / omit (carry-overs 2 and 3) ---------------------

def test_each_paycheck_source_has_its_own_key_even_with_one_label(books):
    found = reports.detected_paychecks(books, TODAY)
    keys = {s["account_id"]: s["key"] for s in found["sources"]}
    assert set(keys) == {"acc_chk", "acc_sav"}
    assert keys["acc_chk"] != keys["acc_sav"]
    assert all(s["key_label"] == "acme corp" for s in found["sources"])   # the merchant, not the bank line


def test_only_the_composite_key_answers_a_source(books):
    chk = next(s for s in reports.detected_paychecks(books, TODAY)["sources"] if s["account_id"] == "acc_chk")
    reports.save_paycheck_answer(books, "acme corp", "add")                 # the bare label: stored, never consulted
    payload = reports.forecast(books, TODAY, 30)
    assert payload["paychecks"]["included"] == []
    assert all(s["answer"] is None for s in payload["paychecks"]["detected"])
    reports.save_paycheck_answer(books, chk["key"], "add")
    payload = reports.forecast(books, TODAY, 30)
    assert payload["paychecks"]["included"] == [chk["key"]]
    assert any(e["kind"] == "paycheck" and e["label"] == "Acme Corp" for e in payload["events"])


def test_answers_are_exactly_add_or_omit_and_bounded(books):
    key = reports.detected_paychecks(books, TODAY)["sources"][0]["key"]
    for bad in (" add", "Add", "yes", 1, True):
        with pytest.raises(reports.ReportError):
            reports.save_paycheck_answer(books, key, bad)
    with pytest.raises(reports.ReportError):
        reports.save_paycheck_answers(books, {key: " add"})
    with pytest.raises(reports.ReportError):
        reports.save_paycheck_answers(books, {"": "add"})
    with pytest.raises(reports.ReportError):
        reports.save_paycheck_answers(books, {f"k{i}": "add" for i in range(reports.PAYCHECK_SETTING_MAX_ENTRIES + 1)})
    reports.save_paycheck_answer(books, key, "omit")
    assert reports.paycheck_answers(books) == {key: "omit"}
    reports.save_paycheck_answer(books, key, None)                          # cleared: the source waits again
    assert reports.paycheck_answers(books) == {}


def test_an_added_paycheck_on_an_unticked_account_is_listed_not_drawn(books):
    """The reviewer's question on t_4b666ab1: an Add on a source that pays into an account the line does not cover.
    Answer = warn and list it with its account; it is not counted until that account is ticked."""
    sav = next(s for s in reports.detected_paychecks(books, TODAY)["sources"] if s["account_id"] == "acc_sav")
    reports.save_paycheck_answer(books, sav["key"], "add")
    payload = reports.forecast(books, TODAY, 30)
    assert [s["key"] for s in payload["paychecks"]["excluded_by_account"]] == [sav["key"]]
    assert sav["key"] not in payload["paychecks"]["included"]
    assert payload["expected"]["in"] == pytest.approx(0.0, abs=0.01)       # the checking line gets no savings money
    assert any(sav["label"] in w and "not in this line" in w for w in payload["warnings"])
    assert all(e["source"] != sav["key"] for e in payload["events"])
    # tick savings in: now the same answer is drawn, in the same window
    reports.save_forecast_accounts(books, ["acc_chk", "acc_sav"])
    payload = reports.forecast(books, TODAY, 30)
    assert payload["paychecks"]["excluded_by_account"] == []
    assert sav["key"] in payload["paychecks"]["included"]
    assert payload["expected"]["in"] == pytest.approx(800.00, abs=0.01)    # one monthly savings paycheck, 12-06


# --- 6. The HTTP payloads the pages draw ----------------------------------------------------------------

def test_the_spending_payload_has_the_year_view(app, books):
    status, d = _get(app, "/api/reports/spending?month=2026-10")
    assert status == 200 and d["view"] == "spending" and d["month"] == "2026-10"
    assert d["period"] == {"start": "2026-10-01", "end": "2026-10-31"}
    assert [m["month"] for m in d["year"]["months"]][-1] == "2026-10"
    assert len(d["year"]["months"]) == 12 and d["year"]["categories"]
    assert set(d["year"]["kpis"]) == {"average", "months", "best", "most_variable", "savings_rate"}


def test_the_cashflow_payload_is_money_flows_own_numbers(app, books):
    status, d = _get(app, "/api/reports/cashflow?month=2026-11")
    flow = reports.money_flow(books, "2026-11-01", "2026-11-30")
    assert status == 200 and d["view"] == "cashflow"
    for key in ("money_in", "money_out", "remaining", "income", "groups", "moves"):
        assert d[key] == flow[key]


def test_the_forecast_payload_shows_the_ticks_and_the_waiting_sources(app, books):
    status, d = _get(app, "/api/reports/forecast")
    assert status == 200 and d["view"] == "forecast" and d["horizon"]["days"] == reports.FORECAST_DAYS
    opts = d["account_options"]
    assert [a["account_id"] for a in opts["accounts"] if a["ticked"]] == ["acc_chk"]
    assert {a["account_id"] for a in opts["accounts"]} >= {"acc_chk", "acc_sav"}
    assert len(d["paychecks"]["detected"]) == 2 and all(s["answer"] is None for s in d["paychecks"]["detected"])
    assert [line["label"] for line in (d["lines"]["lower"], d["lines"]["higher"])] == [
        "Lower everyday spending", "Higher everyday spending"]
    assert "confidence" not in json.dumps(d).lower()


def test_the_activity_payload_drills_into_one_category(app, books):
    groceries = next(c for c in reports.category_totals(books, "2026-11-01", "2026-11-30")["categories"]
                     if c["category"] == "Groceries")
    status, d = _get(app, f"/api/reports/activity?month=2026-11&category_id={groceries['category_id']}")
    assert status == 200 and d["category"] == "Groceries" and d["net"] == groceries["net"]
    status, d = _get(app, "/api/reports/activity?month=2026-11&category_id=999999")
    assert status == 404 and "no category" in d["error"]


@pytest.mark.parametrize("path", ["/api/reports/spending?month=2026-13", "/api/reports/cashflow?month=2027-01",
                                  "/api/reports/spending?month=Nov", "/api/reports/activity?month=2026-11",
                                  "/api/reports/forecast?days=0", "/api/reports/forecast?days=nope"])
def test_bad_query_strings_are_400(app, path):
    assert _get(app, path)[0] == 400


def test_the_two_settings_are_dashboard_writable(app, books):
    key = reports.detected_paychecks(books, TODAY)["sources"][0]["key"]
    status, d = _post(app, "/api/settings", {"reports_paychecks": {key: "add"}})
    assert status == 200 and d["reports_paychecks"] == {key: "add"}
    status, d = _post(app, "/api/settings", {"reports_forecast_accounts": ["acc_chk", "acc_sav", "acc_loan"]})
    assert status == 200 and d["reports_forecast_accounts"] == ["acc_chk", "acc_sav"]
    assert d["reports_accounts_dropped"] == ["acc_loan"]
    assert api.SETTING_KEYS[-2:] == ("reports_paychecks", "reports_forecast_accounts")
    status, d = _post(app, "/api/settings", {"reports_paychecks": {key: " add"}})
    assert status == 400 and d["field"] == "reports_paychecks"
    status, d = _post(app, "/api/settings", {"reports_forecast_accounts": ["a"] * (reports.MAX_FORECAST_ACCOUNTS + 1)})
    assert status == 400 and d["field"] == "reports_forecast_accounts"


def test_the_paycheck_writer_refuses_a_map_the_reader_would_refuse(app, books):
    """Security t_b8a0089f L1: the writer's entry cap let a 40 KB map through, which the reader then refused, silently
    wiping every saved Add. The writer now enforces the reader's byte cap, for the whole map and for one answer."""
    oversized = {f"{'k' * 186}{i:04d}": "add" for i in range(reports.PAYCHECK_SETTING_MAX_ENTRIES)}
    assert len(json.dumps(oversized, sort_keys=True).encode()) > reports.PAYCHECK_SETTING_MAX_BYTES
    with pytest.raises(reports.ReportError):
        reports.clean_paycheck_answers(oversized)
    with pytest.raises(reports.ReportError):
        reports.save_paycheck_answers(books, oversized)
    # a map inside both caps still saves and reads back exactly
    fits = {f"k{i}": "add" for i in range(50)}
    assert reports.save_paycheck_answers(books, fits) == fits
    assert reports.paycheck_answers(books) == fits
    # one more answer that would push the stored value past the byte cap is refused, and the saved map is untouched
    fill: dict[str, str] = {}
    i = 0
    while True:
        cand = {**fill, f"{'k' * 186}{i:04d}": "add"}
        if len(json.dumps(cand, sort_keys=True).encode()) > reports.PAYCHECK_SETTING_MAX_BYTES:
            break
        fill = cand
        i += 1
    assert len(fill) < reports.PAYCHECK_SETTING_MAX_ENTRIES
    reports.save_paycheck_answers(books, fill)
    with pytest.raises(reports.ReportError):
        reports.save_paycheck_answer(books, "z" * 190, "add")
    assert reports.paycheck_answers(books) == fill
    # the HTTP write is a 400 (not a silent save), and what was saved before it stays
    status, d = _post(app, "/api/settings", {"reports_paychecks": oversized})
    assert status == 400 and d["field"] == "reports_paychecks"
    assert reports.paycheck_answers(books) == fill


def test_the_byte_cap_is_exact_at_its_boundary(books):
    """B2 follow-up (3): both sides compare `>`, so a map of exactly PAYCHECK_SETTING_MAX_BYTES saves and reads back
    whole, and one byte more is refused. A `>=` on either side (the writer at reports.py:470, the reader at :395)
    survived the suite before this test: it would silently wipe a map that sits exactly on the cap."""
    limit = reports.PAYCHECK_SETTING_MAX_BYTES
    # 200 entries (the entry cap), 89 characters per key: the serialized map lands exactly on the cap (asserted below)
    keys = [f"k{i:03d}" + "x" * 85 for i in range(reports.PAYCHECK_SETTING_MAX_ENTRIES)]
    exact = {k: "add" for k in keys}
    assert len(json.dumps(exact, sort_keys=True).encode()) == limit

    assert reports.clean_paycheck_answers(exact) == exact
    assert reports.save_paycheck_answers(books, exact) == exact
    assert reports.paycheck_answers(books) == exact                    # the reader takes the exact cap

    over = {**{k: v for k, v in exact.items() if k != keys[-1]}, keys[-1] + "x": "add"}
    assert len(json.dumps(over, sort_keys=True).encode()) == limit + 1
    with pytest.raises(reports.ReportError):
        reports.clean_paycheck_answers(over)
    with pytest.raises(reports.ReportError):
        reports.save_paycheck_answers(books, over)
    assert reports.paycheck_answers(books) == exact                    # the saved map survives both refusals

    # the reader's own boundary, read straight from the settings row: the exact cap answers, one more byte is no answers
    with books:
        books.execute("UPDATE settings SET value = ? WHERE key = ?",
                      (json.dumps(exact, sort_keys=True), reports.PAYCHECK_SETTING))
    assert reports.paycheck_answers(books) == exact
    with books:
        books.execute("UPDATE settings SET value = ? WHERE key = ?",
                      (json.dumps(over, sort_keys=True), reports.PAYCHECK_SETTING))
    assert reports.paycheck_answers(books) == {}


def test_a_year_view_that_would_start_before_1900_is_a_400(app, books):
    """Security t_b8a0089f L2: month=1900-05 walked back into 1899 and raised an uncaught ValueError (a 500)."""
    with pytest.raises(reports.ReportError):
        reports.year_spending(books, TODAY, end_month="1900-05")
    status, d = _get(app, "/api/reports/spending?month=1900-05")
    assert status == 400 and "1900" in d["error"]
    status, d = _get(app, "/api/reports/spending?month=1900-12")     # twelve months back starts at 1900-01: fine
    assert status == 200 and d["year"]["months"][0]["month"] == "1900-01"


def test_the_bills_total_is_the_bill_events_own_total(app, books):
    """The page's "all N bills" footer is a server number, never a sum of two rows in the browser."""
    f = reports.forecast(books, TODAY, 30)
    bills = [e for e in f["events"] if e["amount"] < 0]
    assert f["bills_total"] == pytest.approx(-sum(e["amount"] for e in bills), abs=0.01)
    assert f["bills_total"] == pytest.approx(1000.00, abs=0.01)              # the one rent charge, 12-01
    everyday = f["everyday"]["rate"] * f["horizon"]["days"]
    assert f["expected"]["out"] == pytest.approx(f["bills_total"] + everyday, abs=0.02)
    status, d = _get(app, "/api/reports/forecast")
    assert d["bills_total"] == f["bills_total"]


# --- 7. The page is wired in and its script keeps the house rules ------------------------------------------------

def test_reports_is_a_page_and_its_files_are_served(app):
    assert "reports" in {p[0] for p in web.PAGES}
    assert web.STATIC_FILES["reports.js"] == "text/javascript; charset=utf-8"
    assert web.STATIC_FILES["reports.css"] == "text/css; charset=utf-8"
    for path in ("/static/reports.js", "/static/reports.css"):
        assert app.handle("GET", path, LOCAL, b"")[0] == 200
    assert "/static/reports.js" in app.handle("GET", "/", LOCAL, b"")[2].decode()


def test_the_script_never_writes_html():
    # The list below is the ban itself (AGENTS.md): the strings are searched for, never executed.
    js = (Path(web.__file__).parent / "static" / "reports.js").read_text()
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert bad not in js, bad
    assert "HF_EXT" in js and "RENDER.reports" in js


# --- 0.5 UI fix: the Reports pages take the Transactions window (a range, 30d, all time) ----------------------

def test_cashflow_for_a_range_is_money_flows_own_numbers(app, books):
    status, d = _get(app, "/api/reports/cashflow?since=2026-10-10&until=2026-11-20")
    flow = reports.money_flow(books, "2026-10-10", "2026-11-20")
    assert status == 200 and d["month"] is None and d["period"] == {"start": "2026-10-10", "end": "2026-11-20"}
    assert d["window"] == {"kind": "range", "days": 42}
    for key in ("money_in", "money_out", "remaining", "income", "groups", "moves"):
        assert d[key] == flow[key]


def test_a_last_n_days_window_ends_today_and_all_time_starts_at_the_first_transaction(app, books):
    status, d = _get(app, "/api/reports/cashflow?since=30d")
    today = date.today()
    assert status == 200 and d["period"]["end"] == d["today"] and d["window"]["days"] == 30
    status, d = _get(app, "/api/reports/cashflow?window=all")
    assert status == 200 and d["window"]["kind"] == "all" and d["period"]["start"] == "2025-09-01"


def test_spending_for_a_range_shows_the_whole_months_it_touches(app, books):
    status, d = _get(app, "/api/reports/spending?since=2026-08-15&until=2026-10-02")
    assert status == 200 and d["month"] == "2026-10"
    assert [m["month"] for m in d["year"]["months"]] == ["2026-08", "2026-09", "2026-10"]
    status, d = _get(app, "/api/reports/spending?since=2026-11-02&until=2026-11-09")      # one month still draws two
    assert status == 200 and [m["month"] for m in d["year"]["months"]] == ["2026-10", "2026-11"]
    status, d = _get(app, "/api/reports/spending?window=all")
    assert status == 200 and len(d["year"]["months"]) == reports.MAX_YEAR_MONTHS and d["window"]["kind"] == "all"


def test_the_month_form_is_unchanged_and_says_so(app, books):
    status, d = _get(app, "/api/reports/cashflow?month=2026-10")
    assert status == 200 and d["month"] == "2026-10" and d["window"]["kind"] == "month"


@pytest.mark.parametrize("query", [
    "since=2026-11-20&until=2026-11-01",           # backwards
    "since=2026-10-01&until=2999-01-01",           # ends in the future
    "since=2026-13-45&until=2026-11-01",           # not a date
    "since=1800-01-01&until=2026-11-01",           # before 1900
    "until=2026-11-01",                            # no beginning
    "since=nope",
    "since=0d", "since=99999999d",
    "month=2026-10&since=2026-10-01",              # two ways to say the period
    "window=week", "window=all&month=2026-10",
    "since=2026-11-01'%20OR%201=1--&until=2026-11-02",
])
def test_bad_windows_are_400_on_both_pages(app, books, query):
    for page in ("cashflow", "spending"):
        status, d = _get(app, f"/api/reports/{page}?{query}")
        assert status == 400 and "error" in d, (page, query, status)


def test_spending_refuses_more_than_24_months_but_cashflow_does_not(app, books):
    assert _get(app, "/api/reports/spending?since=2020-01-01&until=2026-11-01")[0] == 400
    assert _get(app, "/api/reports/cashflow?since=2020-01-01&until=2026-11-01")[0] == 200


def test_forecast_until_is_the_same_as_days(app, books):
    from datetime import timedelta
    until = (date.today() + timedelta(days=45)).isoformat()
    status, d = _get(app, f"/api/reports/forecast?until={until}")
    assert status == 200 and d["horizon"]["days"] == 45
    status, e = _get(app, "/api/reports/forecast?days=45")
    assert e["horizon"]["days"] == 45 and e["horizon"]["end"] == d["horizon"]["end"]


@pytest.mark.parametrize("query", ["until=2020-01-01", "until=2999-01-01", "until=nope", "until=2026-12-01&days=30"])
def test_bad_forecast_ends_are_400(app, books, query):
    assert _get(app, f"/api/reports/forecast?{query}")[0] == 400

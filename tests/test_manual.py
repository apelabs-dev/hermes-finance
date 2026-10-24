"""Manual accounts (validation, CRUD, archive) and loan payments (split, eligibility, unapply, payoff),
through manual.py, the JSON API and the CLI."""

from __future__ import annotations

import json
import math
from datetime import date

import pytest

from factory import acct, bank, tx
from hermes_finance import cli, config, insights, manual
from hermes_finance.web import App

TODAY = date(2026, 9, 26)
LOCAL = {"Host": "127.0.0.1:8765"}
POST = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}

LOAN = {"kind": "loan", "name": "Zenith", "institution": "Zenith Financial Services", "subtype": "auto",
        "original_amount": 30947.0, "balance": 5910.0, "monthly_payment": 465.10, "match_pattern": "ZENITH ACH RTL",
        "balance_as_of": "2026-09-01"}
VALID = {
    "bank": {"kind": "bank", "name": "Joint savings", "institution": "Ally Bank", "subtype": "savings", "balance": 1200.5},
    "credit_card": {"kind": "credit_card", "name": "Travel card", "institution": "American Express",
                    "credit_limit": 5000, "balance": 250},
    "investment": {"kind": "investment", "name": "Work 401(k)", "institution": "Fidelity", "subtype": "401k",
                   "balance": 15000},
    "loan": LOAN,
    "vehicle": {"kind": "vehicle", "vehicle_make": "Zenith", "vehicle_model": "Mesa", "vehicle_year": 2019,
                "balance": 20000},
    "property": {"kind": "property", "name": "Home", "balance": 450000},
    "other_asset": {"kind": "other_asset", "name": "Watch", "subtype": "Collectible", "balance": 900},
    "other_liability": {"kind": "other_liability", "name": "Loan from family", "subtype": "Personal IOU",
                        "balance": 1500},
}


@pytest.fixture
def ledger(conn):
    """A checking account with Zenith payments either side of the loan's balance date, a look-alike charge
    that must never match, a pending payment and a refund."""
    bank(conn, "item_a", "Nova")
    acct(conn, "chk", "item_a", name="Nova Checking", mask="7385", current=2400.0)
    tx(conn, "2026-07-28", 465.10, None, name="ZENITH ACH RTL", cat="Car payment", account_id="chk", tid="p_jul")
    tx(conn, "2026-08-30", 465.10, None, name="ZENITH ACH RTL", cat="Car payment", account_id="chk", tid="p_aug")
    tx(conn, "2026-09-02", 465.10, None, name="ZENITH ACH RTL 0902", cat="Car payment", account_id="chk", tid="p_sep")
    tx(conn, "2026-09-03", 465.10, "Best Buy", name="BESTBUY 00123", cat="Other Shopping", account_id="chk", tid="lookalike")
    tx(conn, "2026-09-20", 465.10, None, name="ZENITH ACH RTL", cat="Car payment", account_id="chk", pending=True,
       tid="p_pending")
    tx(conn, "2026-09-21", -20.0, None, name="ZENITH ACH RTL REFUND", cat="Car payment", account_id="chk", tid="refund")
    return conn


def balance(conn, loan_id):
    return conn.execute("SELECT balance FROM manual_accounts WHERE id = ?", (loan_id,)).fetchone()[0]


def hist(conn, loan_id):
    return [tuple(r) for r in conn.execute("SELECT date, balance FROM manual_balance_history "
                                           "WHERE manual_account_id = ? ORDER BY date", (loan_id,))]


# --- validation -------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", manual.KINDS)
def test_each_kind_creates_with_its_form_fields(conn, kind):
    m = manual.create(conn, VALID[kind], TODAY)
    assert m["kind"] == kind and m["manual"] is True
    assert m["signed"] == (-m["balance"] if kind in ("credit_card", "loan", "other_liability") else m["balance"])
    assert m["balance_as_of"] == ("2026-09-01" if kind == "loan" else TODAY.isoformat())
    assert hist(conn, m["id"]) == [(m["balance_as_of"], m["balance"])]      # history row written on create


def test_vehicle_name_defaults_to_year_make_model(conn):
    assert manual.create(conn, VALID["vehicle"], TODAY)["name"] == "2019 Zenith Mesa"


@pytest.mark.parametrize("kind", manual.KINDS)
def test_each_kind_requires_its_fields(conn, kind):
    for field in manual.REQUIRED[kind]:
        data = {k: v for k, v in VALID[kind].items() if k != field}
        with pytest.raises(manual.ManualError) as e:
            manual.create(conn, data, TODAY)
        assert e.value.field == field and "required" in str(e.value)
    assert conn.execute("SELECT COUNT(*) FROM manual_accounts").fetchone()[0] == 0


@pytest.mark.parametrize("kind,field,value", [
    ("loan", "balance", -1), ("loan", "balance", 100_000_001), ("loan", "balance", math.nan),
    ("loan", "balance", math.inf), ("loan", "balance", "5910"), ("loan", "balance", True),
    ("loan", "original_amount", 0), ("loan", "monthly_payment", 0), ("loan", "interest_rate", -0.1),
    ("loan", "interest_rate", 100.5), ("loan", "subtype", "boat"), ("loan", "match_pattern", "TO"),
    ("loan", "match_pattern", "   "), ("loan", "name", "x" * 81), ("loan", "name", ""), ("loan", "name", 5),
    ("loan", "name", "bad\x00name"), ("loan", "balance_as_of", "2026-13-01"), ("loan", "balance_as_of", "yesterday"),
    ("loan", "balance_as_of", "2026-09-27"),                                   # the future
    ("credit_card", "credit_limit", 0), ("bank", "subtype", "brokerage"), ("investment", "subtype", "checking"),
    ("vehicle", "vehicle_year", 1899), ("vehicle", "vehicle_year", 2028), ("vehicle", "vehicle_year", 2019.5),
    ("vehicle", "vehicle_year", "2019"),
    ("vehicle", "credit_limit", 100),                                          # not on this kind's form
    ("property", "interest_rate", 3.0), ("bank", "match_pattern", "NOVA"), ("loan", "vehicle_make", "Zenith"),
    ("loan", "nonsense", 1),
])
def test_bad_values_are_refused_and_nothing_is_written(conn, kind, field, value):
    with pytest.raises(manual.ManualError) as e:
        manual.create(conn, {**VALID[kind], field: value}, TODAY)
    assert e.value.field == field
    assert conn.execute("SELECT COUNT(*) FROM manual_accounts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM manual_balance_history").fetchone()[0] == 0


@pytest.mark.parametrize("kind", [None, "", "boat", 5, "Loan"])
def test_bad_kind(conn, kind):
    with pytest.raises(manual.ManualError) as e:
        manual.create(conn, {**VALID["bank"], "kind": kind}, TODAY)
    assert e.value.field == "kind"


def test_strings_are_trimmed_and_subtypes_normalized(conn):
    m = manual.create(conn, {**VALID["investment"], "name": "  Work   401(k) ", "subtype": "Roth IRA",
                             "institution": " Fidelity "}, TODAY)
    assert (m["name"], m["institution"], m["subtype"], m["subtype_label"]) == ("Work 401(k)", "Fidelity", "roth_ira",
                                                                              "Roth IRA")
    assert manual.create(conn, {**VALID["bank"], "subtype": "Money market"}, TODAY)["subtype"] == "money_market"


def test_edit_changes_only_given_fields_and_clears_optional_ones(conn):
    m = manual.create(conn, {**LOAN, "interest_rate": 4.9}, TODAY)
    e = manual.edit(conn, m["id"], {"name": "Car loan", "interest_rate": None}, TODAY)
    assert (e["name"], e["interest_rate"], e["balance"], e["balance_as_of"]) == ("Car loan", None, 5910.0, "2026-09-01")
    with pytest.raises(manual.ManualError) as err:
        manual.edit(conn, m["id"], {"monthly_payment": None}, TODAY)              # required: can't be cleared
    assert err.value.field == "monthly_payment"
    with pytest.raises(manual.ManualError):
        manual.edit(conn, m["id"], {"kind": "bank"}, TODAY)
    with pytest.raises(manual.ManualError) as err:
        manual.edit(conn, 999, {"name": "x"}, TODAY)
    assert err.value.status == 404


def test_edit_with_a_new_balance_is_a_new_entered_balance_as_of_today(conn):
    m = manual.create(conn, VALID["bank"], date(2026, 9, 1))
    e = manual.edit(conn, m["id"], {"balance": 1500.0}, TODAY)
    assert (e["balance"], e["balance_as_of"]) == (1500.0, TODAY.isoformat())
    assert hist(conn, m["id"]) == [("2026-09-01", 1200.5), ("2026-09-26", 1500.0)]


def test_archive_hides_from_totals_and_lists(conn):
    car = manual.create(conn, VALID["vehicle"], TODAY)
    loan = manual.create(conn, LOAN, TODAY)
    assert insights.net_worth(conn, history=False)["net"] == 20000.0 - 5910.0
    manual.archive(conn, loan["id"], TODAY)
    nw = insights.net_worth(conn, history=False)
    assert (nw["assets"], nw["debts"], nw["net"]) == (20000.0, 0.0, 20000.0)
    s = manual.summary(conn, TODAY)
    assert [a["id"] for a in s["accounts"]] == [car["id"]] and s["debts"] == 0.0
    assert {a["id"] for a in manual.summary(conn, TODAY, include_archived=True)["accounts"]} == {car["id"], loan["id"]}
    with pytest.raises(manual.ManualError) as e:
        manual.edit(conn, loan["id"], {"name": "back"}, TODAY)                   # archived: gone for writes too
    assert e.value.status == 404


# --- loan payments ---------------------------------------------------------------------------------

def test_apply_without_apr_counts_the_whole_payment_as_principal(ledger):
    loan = manual.create(ledger, LOAN, TODAY)
    p = manual.apply_payment(ledger, "p_sep", loan["id"], "ai")
    assert (p["interest"], p["principal"], p["balance_before"], p["balance_after"]) == (0.0, 465.10, 5910.0, 5444.90)
    assert p["apr_set"] is False and p["source"] == "ai"
    assert balance(ledger, loan["id"]) == 5444.90
    assert hist(ledger, loan["id"]) == [("2026-09-01", 5910.0), ("2026-09-02", 5444.90)]


def test_apply_with_apr_splits_interest_and_principal(ledger):
    loan = manual.create(ledger, {**LOAN, "interest_rate": 6.0}, TODAY)
    p = manual.apply_payment(ledger, "p_sep", loan["id"])
    assert p["interest"] == round(5910.0 * 6.0 / 100 / 12, 2) == 29.55
    assert p["principal"] == round(465.10 - 29.55, 2) == 435.55
    assert p["balance_after"] == balance(ledger, loan["id"]) == round(5910.0 - 435.55, 2)


def test_payment_before_or_on_the_balance_date_is_refused(ledger):
    loan = manual.create(ledger, LOAN, TODAY)
    for tid in ("p_jul", "p_aug"):
        with pytest.raises(manual.ManualError, match="on or before"):
            manual.apply_payment(ledger, tid, loan["id"])
    on_day = manual.create(ledger, {**LOAN, "name": "Same day", "balance_as_of": "2026-09-02"}, TODAY)
    with pytest.raises(manual.ManualError, match="on or before"):
        manual.apply_payment(ledger, "p_sep", on_day["id"])
    assert ledger.execute("SELECT COUNT(*) FROM loan_payments").fetchone()[0] == 0
    assert balance(ledger, loan["id"]) == 5910.0


def test_double_apply_is_refused(ledger):
    loan = manual.create(ledger, LOAN, TODAY)
    other = manual.create(ledger, {**LOAN, "name": "Second"}, TODAY)
    manual.apply_payment(ledger, "p_sep", loan["id"])
    with pytest.raises(manual.ManualError, match="already applied to this loan") as e:
        manual.apply_payment(ledger, "p_sep", loan["id"])
    assert e.value.status == 409
    with pytest.raises(manual.ManualError, match=f"already applied to loan #{loan['id']}"):
        manual.apply_payment(ledger, "p_sep", other["id"])
    assert balance(ledger, loan["id"]) == 5444.90 and balance(ledger, other["id"]) == 5910.0


@pytest.mark.parametrize("tid,reason", [("p_pending", "pending"), ("refund", "money in"), ("nope", "unknown")])
def test_pending_inflow_and_unknown_transactions_are_refused(ledger, tid, reason):
    loan = manual.create(ledger, LOAN, TODAY)
    with pytest.raises(manual.ManualError, match=reason):
        manual.apply_payment(ledger, tid, loan["id"])
    assert balance(ledger, loan["id"]) == 5910.0


def test_apply_to_a_non_loan_or_archived_account_is_refused(ledger):
    car = manual.create(ledger, VALID["vehicle"], TODAY)       # TASK-11: credit cards take payments too now
    with pytest.raises(manual.ManualError, match="not a loan or credit card"):
        manual.apply_payment(ledger, "p_sep", car["id"])
    loan = manual.create(ledger, LOAN, TODAY)
    manual.archive(ledger, loan["id"], TODAY)
    with pytest.raises(manual.ManualError, match="archived"):
        manual.apply_payment(ledger, "p_sep", loan["id"])


def test_unapply_restores_the_exact_balance_and_history(ledger):
    loan = manual.create(ledger, {**LOAN, "interest_rate": 6.0}, TODAY)
    before_hist = hist(ledger, loan["id"])
    tx(ledger, "2026-09-15", 465.10, None, name="ZENITH ACH RTL", cat="Car payment", account_id="chk", tid="p_mid")
    manual.apply_payment(ledger, "p_sep", loan["id"])
    after_one = (balance(ledger, loan["id"]), hist(ledger, loan["id"]))
    manual.apply_payment(ledger, "p_mid", loan["id"])
    assert balance(ledger, loan["id"]) < after_one[0]
    u = manual.unapply_payment(ledger, "p_mid")
    assert (balance(ledger, loan["id"]), hist(ledger, loan["id"])) == after_one == (u["balance_after"], after_one[1])
    manual.unapply_payment(ledger, "p_sep")
    assert balance(ledger, loan["id"]) == 5910.0 and hist(ledger, loan["id"]) == before_hist
    assert ledger.execute("SELECT COUNT(*) FROM loan_payments").fetchone()[0] == 0
    with pytest.raises(manual.ManualError, match="isn't applied") as e:
        manual.unapply_payment(ledger, "p_sep")
    assert e.value.status == 404


def test_unapplying_an_earlier_payment_re_splits_the_later_ones(ledger):
    """Order doesn't matter: applying B then A, and removing A, ends where applying only B would."""
    loan = manual.create(ledger, {**LOAN, "interest_rate": 6.0}, TODAY)
    tx(ledger, "2026-09-15", 465.10, None, name="ZENITH ACH RTL", cat="Car payment", account_id="chk", tid="p_mid")
    manual.apply_payment(ledger, "p_mid", loan["id"])
    only_mid = balance(ledger, loan["id"])
    manual.apply_payment(ledger, "p_sep", loan["id"])            # earlier date, applied later
    manual.unapply_payment(ledger, "p_sep")
    assert balance(ledger, loan["id"]) == only_mid


def test_dry_runs_write_nothing(ledger):
    loan = manual.create(ledger, LOAN, TODAY)
    p = manual.apply_payment(ledger, "p_sep", loan["id"], dry_run=True)
    assert p["balance_after"] == 5444.90 and p["dry_run"]
    assert balance(ledger, loan["id"]) == 5910.0 and ledger.execute("SELECT COUNT(*) FROM loan_payments").fetchone()[0] == 0
    manual.apply_payment(ledger, "p_sep", loan["id"])
    u = manual.unapply_payment(ledger, "p_sep", dry_run=True)
    assert u["balance_after"] == 5910.0 and balance(ledger, loan["id"]) == 5444.90


def test_balance_floors_at_zero_and_is_paid_off(ledger):
    loan = manual.create(ledger, {**LOAN, "balance": 300.0, "balance_as_of": "2026-09-01"}, TODAY)
    p = manual.apply_payment(ledger, "p_sep", loan["id"])
    assert (p["principal"], p["balance_after"], p["paid_off"]) == (300.0, 0.0, True)
    d = manual.describe(ledger, manual.get(ledger, loan["id"]), TODAY)
    assert d["paid_off"] is True and d["paid_pct"] == 100.0 and d["payoff"]["method"] == "paid"
    tx(ledger, "2026-09-10", 465.10, None, name="ZENITH ACH RTL", cat="Car payment", account_id="chk", tid="p_after")
    with pytest.raises(manual.ManualError, match="already paid off"):
        manual.apply_payment(ledger, "p_after", loan["id"])
    manual.unapply_payment(ledger, "p_sep")
    assert balance(ledger, loan["id"]) == 300.0


def test_edit_after_payments_rebuilds_from_the_entered_balance(ledger):
    loan = manual.create(ledger, LOAN, TODAY)
    manual.apply_payment(ledger, "p_sep", loan["id"])
    e = manual.edit(ledger, loan["id"], {"interest_rate": 6.0}, TODAY)    # adding the APR re-splits the payment
    assert e["balance"] == round(5910.0 - (465.10 - 29.55), 2)
    e = manual.edit(ledger, loan["id"], {"balance": 5000.0}, TODAY)       # a new balance, true today
    assert (e["balance"], e["balance_as_of"]) == (5000.0, "2026-09-26")
    manual.unapply_payment(ledger, "p_sep")                                # included in the new balance already
    assert balance(ledger, loan["id"]) == 5000.0


# --- loan-match ------------------------------------------------------------------------------------

def test_loan_match_applies_only_matching_posted_outflows_after_the_balance_date(ledger):
    loan = manual.create(ledger, LOAN, TODAY)
    out = manual.match_loans(ledger)
    assert [p["transaction_id"] for p in out["applied"]] == ["p_sep"]
    assert out["applied"][0]["source"] == "auto"
    assert [tuple(r) for r in ledger.execute("SELECT transaction_id, source FROM loan_payments")] == [("p_sep", "auto")]
    assert balance(ledger, loan["id"]) == 5444.90
    assert manual.match_loans(ledger)["applied"] == []                    # idempotent


def test_loan_match_does_not_ignore_the_pattern(ledger):
    """Non-vacuity: the Best Buy charge is the same $465.10, after the balance date, from the same account.
    It must not be applied; only the pattern tells them apart."""
    loan = manual.create(ledger, LOAN, TODAY)
    applied = {p["transaction_id"] for p in manual.match_loans(ledger)["applied"]}
    assert "lookalike" not in applied and applied == {"p_sep"}
    assert balance(ledger, loan["id"]) == round(5910.0 - 465.10, 2)
    other = manual.create(ledger, {**LOAN, "name": "Best Buy card plan", "match_pattern": "bestbuy"}, TODAY)
    assert [p["transaction_id"] for p in manual.match_loans(ledger)["applied"]] == ["lookalike"]  # case-insensitive
    assert balance(ledger, other["id"]) == round(5910.0 - 465.10, 2)


def test_loan_match_dry_run_and_skips_loans_without_a_pattern(ledger):
    manual.create(ledger, {**LOAN, "match_pattern": None}, TODAY)
    assert manual.match_loans(ledger)["applied"] == []
    loan = manual.create(ledger, {**LOAN, "name": "With pattern"}, TODAY)
    out = manual.match_loans(ledger, dry_run=True)
    assert [p["transaction_id"] for p in out["applied"]] == ["p_sep"] and out["dry_run"]
    assert balance(ledger, loan["id"]) == 5910.0 and ledger.execute("SELECT COUNT(*) FROM loan_payments").fetchone()[0] == 0


def test_matches_use_merchant_name_too(ledger):
    tx(ledger, "2026-09-12", 250.0, "Navient", name="ACH DEBIT 99812", cat="Car payment", account_id="chk", tid="nav")
    loan = manual.create(ledger, {**LOAN, "name": "Student", "subtype": "student", "match_pattern": "navient"}, TODAY)
    assert [p["transaction_id"] for p in manual.match_loans(ledger)["applied"]] == ["nav"]
    assert balance(ledger, loan["id"]) == 5660.0


# --- payoff projection ------------------------------------------------------------------------------

def test_payoff_without_apr_is_remaining_over_payment_rounded_up():
    p = manual.payoff(5910.0, None, 465.10, date(2026, 9, 30))
    assert p == {"payments_left": 13, "payoff_month": "2027-09", "method": "simple"}   # 12.7 -> 13
    assert manual.payoff(930.20, None, 465.10, date(2026, 10, 1))["payments_left"] == 2   # exact, in cents


def test_payoff_with_apr_is_amortized():
    p = manual.payoff(5910.0, 6.0, 465.10, date(2026, 9, 30))
    assert p["method"] == "amortized" and p["payments_left"] == 14 and p["payoff_month"] == "2027-10"
    # month by month in cents: 13 payments leave a remainder, the 14th clears it
    left = 591000
    for _ in range(13):
        left = left + round(left * 0.005) - 46510
    assert 0 < left < 46510
    assert manual.payoff(10000.0, 12.0, 100.0, date(2026, 9, 30))["method"] == "never"    # 1% a month = $100
    assert manual.payoff(0.0, 6.0, 465.10, date(2026, 9, 30))["method"] == "paid"
    assert manual.payoff(500.0, None, None, date(2026, 9, 30))["method"] == "none"


def test_describe_projects_from_the_next_expected_payment(ledger):
    loan = manual.create(ledger, LOAN, TODAY)
    d = manual.describe(ledger, manual.get(ledger, loan["id"]), TODAY)
    assert d["last_payment"] == {"date": "2026-09-02", "amount": 465.10, "name": "ZENITH ACH RTL 0902"}
    assert d["next_payment"] == "2026-10-02"
    assert d["payoff"] == {"payments_left": 13, "payoff_month": "2027-10", "method": "simple"}
    assert d["paid_pct"] == round((30947 - 5910) / 30947 * 100, 2)
    assert d["auto_updates"] is True and d["stale"] is False


def test_stale_after_45_days_except_auto_updating_loans(conn):
    old = {"balance_as_of": "2026-08-01"}
    car = manual.create(conn, {**VALID["vehicle"], **old}, TODAY)
    loan = manual.create(conn, {**LOAN, **old}, TODAY)
    plain = manual.create(conn, {**LOAN, **old, "name": "No pattern", "match_pattern": None}, TODAY)
    fresh = manual.create(conn, {**VALID["bank"], "balance_as_of": "2026-08-12"}, TODAY)   # 45 days: not yet
    got = {m["id"]: m["stale"] for m in manual.summary(conn, TODAY)["accounts"]}
    assert got == {car["id"]: True, loan["id"]: False, plain["id"]: True, fresh["id"]: False}


# --- JSON API ----------------------------------------------------------------------------------------

@pytest.fixture
def app(ledger, client):
    a = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


def call(app, method, path, body=None, headers=None):
    raw = json.dumps(body).encode() if body is not None else b""
    status, _, out = app.handle(method, path, headers or (POST if method == "POST" else LOCAL), raw)
    return status, json.loads(out)


def test_api_create_list_edit_archive(app, ledger):
    status, m = call(app, "POST", "/api/manual", LOAN)
    assert status == 200 and m["kind"] == "loan" and m["id"] == 1 and m["paid_pct"] > 80
    status, lst = call(app, "GET", "/api/manual")
    assert status == 200 and [a["name"] for a in lst["accounts"]] == ["Zenith"] and lst["debts"] == 5910.0
    status, e = call(app, "POST", "/api/manual/1", {"interest_rate": 4.9})
    assert status == 200 and e["interest_rate"] == 4.9
    status, a = call(app, "POST", "/api/manual/1/archive", {})
    assert status == 200 and a["archived_at"]
    assert call(app, "GET", "/api/manual")[1]["accounts"] == []
    assert len(call(app, "GET", "/api/manual?archived=1")[1]["accounts"]) == 1


@pytest.mark.parametrize("path,body,status,field", [
    ("/api/manual", {**LOAN, "balance": -5}, 400, "balance"),
    ("/api/manual", {**LOAN, "kind": "boat"}, 400, "kind"),
    ("/api/manual", {"kind": "loan"}, 400, "name"),
    ("/api/manual", {**LOAN, "monthly_payment": "465"}, 400, "monthly_payment"),
    ("/api/manual/99", {"name": "x"}, 404, "id"),
    ("/api/manual/99/archive", {}, 404, "id"),
])
def test_api_validation_errors_carry_the_field(app, ledger, path, body, status, field):
    got, d = call(app, "POST", path, body)
    assert got == status and d["error"] and d["field"] == field
    assert ledger.execute("SELECT COUNT(*) FROM manual_accounts").fetchone()[0] == 0


@pytest.mark.parametrize("path", ["/api/manual/0", "/api/manual/01", "/api/manual/abc", "/api/manual/1/archive/x",
                                  "/api/manual/1/delete", "/api/manual/9999999999"])
def test_api_odd_paths_are_404(app, path):
    assert call(app, "POST", path, {})[0] == 404


def test_api_writes_keep_the_csrf_gate(app, ledger):
    evil = {**POST, "Origin": "https://evil.example"}
    assert call(app, "POST", "/api/manual", LOAN, evil)[0] == 403
    assert call(app, "POST", "/api/manual", LOAN, {**POST, "Content-Type": "text/plain"})[0] == 415
    assert call(app, "POST", "/api/manual", LOAN, {"Host": "evil.example:80", **{k: v for k, v in POST.items()
                                                                                    if k != "Host"}})[0] == 403
    assert ledger.execute("SELECT COUNT(*) FROM manual_accounts").fetchone()[0] == 0


def test_match_preview(app):
    status, d = call(app, "GET", "/api/manual/match?pattern=zenith%20ach")
    assert status == 200 and d["count"] == 3 and d["typical"] == 465.10                  # posted outflows only
    assert d["last"]["date"] == "2026-09-02" and d["last"]["account_mask"] == "7385"
    assert call(app, "GET", "/api/manual/match?pattern=to")[0] == 400
    status, s = call(app, "GET", "/api/manual/match")
    assert status == 200 and s["suggestions"] == ["ZENITH ACH RTL"]


# --- CLI ------------------------------------------------------------------------------------------------

def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_manual_add_list_edit_archive(ledger, capsys):
    code, out, _ = run(capsys, "manual", "add", "--kind", "loan", "--name", "Zenith", "--institution", "Zenith FS",
                       "--subtype", "auto", "--original-amount", "30947", "--balance", "5910", "--monthly-payment",
                       "465.10", "--match-pattern", "ZENITH ACH RTL", "--as-of", "2026-09-01")
    assert code == 0 and "#1" in out and "-$5,910" in out
    code, out, _ = run(capsys, "manual", "list", "--json")
    assert code == 0 and json.loads(out)["accounts"][0]["match_pattern"] == "ZENITH ACH RTL"
    code, out, _ = run(capsys, "manual", "edit", "1", "--interest-rate", "5.5")
    assert code == 0 and ledger.execute("SELECT interest_rate FROM manual_accounts").fetchone()[0] == 5.5
    code, out, _ = run(capsys, "manual", "edit", "1", "--clear", "interest_rate")
    assert code == 0 and ledger.execute("SELECT interest_rate FROM manual_accounts").fetchone()[0] is None
    code, out, _ = run(capsys, "manual", "archive", "1")
    assert code == 0 and "archived" in out


@pytest.mark.parametrize("argv,needle", [
    (["manual", "add", "--kind", "loan", "--name", "X"], "Provider is required"),
    (["manual", "add", "--kind", "bank", "--name", "X", "--institution", "Y", "--subtype", "checking",
      "--balance", "-3"], "at least 0"),
    (["manual", "add", "--kind", "vehicle", "--vehicle-make", "Zenith", "--vehicle-model", "Mesa",
      "--vehicle-year", "2019", "--balance", "1", "--credit-limit", "5"], "doesn't apply"),
    (["manual", "edit", "42", "--name", "x"], "no manual account #42"),
    (["manual", "edit", "1"], "nothing to change"),
    (["manual", "archive", "7"], "no manual account #7"),
    (["loan-apply", "p_sep", "7"], "no manual account #7"),
    (["loan-unapply", "p_sep"], "isn't applied"),
])
def test_cli_bad_input_exits_2_and_writes_nothing(ledger, capsys, argv, needle):
    if argv[:2] == ["manual", "edit"] and argv[2] == "1":
        manual.create(ledger, LOAN, TODAY)
    before = [tuple(r) for r in ledger.execute("SELECT * FROM manual_accounts")]
    code, _, err = run(capsys, *argv)
    assert code == 2 and needle in err
    assert [tuple(r) for r in ledger.execute("SELECT * FROM manual_accounts")] == before


def test_cli_argparse_rejects_non_numbers(ledger, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["manual", "add", "--kind", "loan", "--balance", "lots"])
    assert e.value.code == 2 and "invalid float value" in capsys.readouterr().err


def test_cli_loan_match_apply_unapply(ledger, capsys):
    manual.create(ledger, LOAN, TODAY)
    code, out, _ = run(capsys, "loan-match", "--dry-run")
    assert code == 0 and "would apply p_sep" in out and "APR not set, whole payment counted as principal" in out
    assert ledger.execute("SELECT COUNT(*) FROM loan_payments").fetchone()[0] == 0
    code, out, _ = run(capsys, "loan-match", "--json")
    assert code == 0 and [p["transaction_id"] for p in json.loads(out)["applied"]] == ["p_sep"]
    code, out, _ = run(capsys, "loan-unapply", "p_sep")
    assert code == 0 and "$5,444.90 -> $5,910" in out
    code, out, err = run(capsys, "loan-apply", "lookalike", "1", "--source", "ai")    # the AI may apply any outflow
    assert code == 0 and "applied lookalike" in out
    assert ledger.execute("SELECT source FROM loan_payments").fetchone()[0] == "ai"
    code, _, err = run(capsys, "loan-apply", "lookalike", "1")
    assert code == 2 and "already applied" in err
    code, _, err = run(capsys, "loan-apply", "p_aug", "1")
    assert code == 2 and "on or before" in err


def test_vehicle_without_a_name_keeps_the_automatic_one(conn):
    car = manual.create(conn, VALID["vehicle"], TODAY)
    e = manual.edit(conn, car["id"], {"name": None, "vehicle_year": 2020}, TODAY)
    assert e["name"] == "2020 Zenith Mesa"
    e = manual.edit(conn, car["id"], {"name": "Blue car"}, TODAY)
    assert manual.edit(conn, car["id"], {"vehicle_model": "Corolla"}, TODAY)["name"] == "Blue car"   # a chosen name stays


def test_a_payment_the_bank_removed_stops_counting(ledger):
    loan = manual.create(ledger, LOAN, TODAY)
    manual.match_loans(ledger)
    assert balance(ledger, loan["id"]) == 5444.90
    with ledger:
        ledger.execute("UPDATE transactions SET removed = 1 WHERE transaction_id = 'p_sep'")
    manual.match_loans(ledger)
    assert balance(ledger, loan["id"]) == 5910.0 and hist(ledger, loan["id"]) == [("2026-09-01", 5910.0)]
    assert manual.payments(ledger, loan["id"]) == []
    with ledger:
        ledger.execute("UPDATE transactions SET removed = 0 WHERE transaction_id = 'p_sep'")   # Plaid restores it
    manual.match_loans(ledger)
    assert balance(ledger, loan["id"]) == 5444.90


# --- review regressions -------------------------------------------------------------------------------

def test_moving_only_the_balance_date_does_not_count_payments_twice(ledger):
    """The entered balance stays the entered balance: moving its date re-applies later payments to it,
    never to the balance they were already taken off."""
    tx(ledger, "2026-09-10", 500.0, None, name="ZENITH ACH RTL", cat="Car payment", account_id="chk", tid="p_10")
    loan = manual.create(ledger, {**LOAN, "balance": 10000.0, "balance_as_of": "2026-07-01", "match_pattern": None}, TODAY)
    for tid in ("p_aug", "p_sep", "p_10"):
        manual.apply_payment(ledger, tid, loan["id"])
    after_all = round(10000.0 - 465.10 - 465.10 - 500.0, 2)
    assert balance(ledger, loan["id"]) == after_all
    e = manual.edit(ledger, loan["id"], {"balance_as_of": "2026-07-15"}, TODAY)     # all three are still after it
    assert (e["balance"], e["balance_as_of"]) == (after_all, "2026-07-15")
    e = manual.edit(ledger, loan["id"], {"balance_as_of": "2026-09-05"}, TODAY)     # only p_10 is after it now
    assert e["balance"] == 9500.0
    e = manual.edit(ledger, loan["id"], {"balance_as_of": "2026-07-01"}, TODAY)     # and back
    assert e["balance"] == after_all
    e = manual.edit(ledger, loan["id"], {"balance": 9000.0, "balance_as_of": "2026-09-01"}, TODAY)   # explicit wins
    assert e["balance"] == round(9000.0 - 465.10 - 500.0, 2)


def test_an_earlier_balance_date_replaces_later_history(conn):
    home = manual.create(conn, VALID["property"], TODAY)                          # 450,000 on 09-26
    manual.edit(conn, home["id"], {"balance": 480000.0, "balance_as_of": "2026-09-01"}, TODAY)
    assert hist(conn, home["id"]) == [("2026-09-01", 480000.0)]
    assert insights.net_worth(conn)["history"][-1]["net"] == insights.net_worth(conn, history=False)["net"] == 480000.0


def test_payments_before_a_new_balance_date_count_as_included(ledger):
    loan = manual.create(ledger, LOAN, TODAY)
    manual.apply_payment(ledger, "p_sep", loan["id"])
    assert manual.describe(ledger, manual.get(ledger, loan["id"]), TODAY)["applied_count"] == 1
    manual.edit(ledger, loan["id"], {"balance": 0.0}, TODAY)                        # "Mark as paid off" today
    d = manual.describe(ledger, manual.get(ledger, loan["id"]), TODAY)
    assert d["applied_count"] == 0 and d["paid_off"] is True


@pytest.mark.parametrize("value", [10 ** 400, -(10 ** 400)])
def test_huge_integers_are_a_field_error_not_a_crash(conn, value):
    with pytest.raises(manual.ManualError) as e:
        manual.create(conn, {**VALID["bank"], "balance": value}, TODAY)
    assert e.value.field == "balance"


def test_apr_keeps_its_precision(conn):
    assert manual.create(conn, {**LOAN, "interest_rate": 6.125}, TODAY)["interest_rate"] == 6.125

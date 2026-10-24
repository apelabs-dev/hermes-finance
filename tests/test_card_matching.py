"""TASK-11: "Match payments from" on manual credit cards (the Best Buy Citi card), through manual.py, the API and the
CLI: the guard for a label shared with connected cards ("CITI CARD ONLINE"), balances that are only approximate (a
payment lowers them, purchases aren't seen), the form's 90-day dry run, idempotence, archive, and the suggestion
the daily run posts."""

from __future__ import annotations

import json
from datetime import date

import pytest

from factory import acct, bank, tx
from hermes_finance import cli, config, manual
from hermes_finance.web import App

AS_OF = "2026-09-26"
TODAY = date(2026, 10, 20)
LOCAL = {"Host": "127.0.0.1:8765"}
POST = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
BEST_BUY = {"kind": "credit_card", "name": "Best Buy", "institution": "Citi", "credit_limit": 4000.0,
            "balance": 1110.66, "balance_as_of": AS_OF}
PAY = ("Card payment",)  # factory category: LOAN_PAYMENTS_CREDIT_CARD_PAYMENT


@pytest.fixture
def citi(conn):
    """Nova Checking pays two connected Citi cards (Double Cash ••2417, Costco ••3842) as "CITI CARD ONLINE"; each of
    those shows up on the card as "ONLINE PAYMENT, THANK YOU". The Best Buy card isn't connected: its autopay has the
    same label and no inflow anywhere."""
    bank(conn, "item_nova", "Nova")
    bank(conn, "item_citi", "Citibank Online")
    acct(conn, "chk", "item_nova", name="Nova Checking", mask="7385", current=2400.0)
    acct(conn, "dc", "item_citi", name="Citi Double Cash", type_="credit", subtype="credit card", current=318.4,
         limit=7500.0, mask="2417")
    acct(conn, "costco", "item_citi", name="Costco Anywhere", type_="credit", subtype="credit card", current=90.0,
         limit=9000.0, mask="3842")
    # Double Cash: paid 10-02, posted on the card 10-03
    tx(conn, "2026-10-02", 250.00, None, name="CITI CARD ONLINE PAYMENT", cat="Card payment", account_id="chk", tid="dc_out")
    tx(conn, "2026-10-03", -250.00, None, name="ONLINE PAYMENT, THANK YOU", cat="Card payment", account_id="dc",
       item_id="item_citi", tid="dc_in")
    # Costco: posted on the card 5 days later (the edge of the window)
    tx(conn, "2026-10-06", 61.40, None, name="CITI CARD ONLINE PAYMENT", cat="Card payment", account_id="chk", tid="co_out")
    tx(conn, "2026-10-11", -61.40, None, name="ONLINE PAYMENT, THANK YOU", cat="Card payment", account_id="costco",
       item_id="item_citi", tid="co_in")
    # Best Buy minimum autopay: same label, no inflow on any connected card
    tx(conn, "2026-10-08", 35.00, None, name="CITI CARD ONLINE PAYMENT", cat="Card payment", account_id="chk", tid="bb_out")
    # before the balance date: already in the $1,110.66 he entered
    tx(conn, "2026-09-20", 35.00, None, name="CITI CARD ONLINE PAYMENT", cat="Card payment", account_id="chk", tid="bb_old")
    return conn


def balance(conn, account_id):
    return conn.execute("SELECT balance FROM manual_accounts WHERE id = ?", (account_id,)).fetchone()[0]


def applied(conn, account_id):
    return sorted(r[0] for r in conn.execute("SELECT transaction_id FROM loan_payments WHERE loan_id = ?", (account_id,)))


# --- the form field ----------------------------------------------------------------------------------------------

def test_credit_cards_take_a_match_pattern_with_the_same_rules(conn):
    card = manual.create(conn, {**BEST_BUY, "match_pattern": "  citi   card online "}, TODAY)
    assert card["match_pattern"] == "citi card online" and card["auto_updates"] is True
    with pytest.raises(manual.ManualError, match="at least 3 characters") as e:
        manual.create(conn, {**BEST_BUY, "match_pattern": "CI"}, TODAY)
    assert e.value.field == "match_pattern"
    with pytest.raises(manual.ManualError, match="doesn't apply to bank accounts"):
        manual.create(conn, {"kind": "bank", "name": "x", "institution": "y", "subtype": "checking", "balance": 1,
                             "match_pattern": "NOVA"}, TODAY)
    assert manual.edit(conn, card["id"], {"match_pattern": None}, TODAY)["auto_updates"] is False


# --- the guard for a label connected cards share ------------------------------------------------------------------

def test_a_payment_that_pairs_with_a_connected_card_is_never_applied(citi):
    card = manual.create(citi, {**BEST_BUY, "match_pattern": "CITI CARD ONLINE"}, TODAY)
    out = manual.match_payments(citi, today=TODAY)
    assert [p["transaction_id"] for p in out["applied"]] == ["bb_out"]
    assert applied(citi, card["id"]) == ["bb_out"] and balance(citi, card["id"]) == 1075.66
    skipped = {s["transaction_id"]: s["reason"] for s in out["skipped"]}
    assert "Citi Double Cash ••2417" in skipped["dc_out"] and "2026-10-03" in skipped["dc_out"]
    assert "Costco Anywhere ••3842" in skipped["co_out"]                        # 5 days later still pairs
    assert "bb_old" not in skipped                                              # before the balance date: included


def test_pairing_is_one_to_one_and_stops_at_five_days(citi):
    card = manual.create(citi, {**BEST_BUY, "match_pattern": "CITI CARD ONLINE"}, TODAY)
    # a second $250 two days after the Double Cash one: only one of them can be the payment that posted on 10-03
    tx(citi, "2026-10-04", 250.00, None, name="CITI CARD ONLINE PAYMENT", cat="Card payment", account_id="chk", tid="bb_250")
    # a $61.40 whose only same-amount inflow is 6 days away: not a pair
    tx(citi, "2026-10-12", 20.00, None, name="CITI CARD ONLINE PAYMENT", cat="Card payment", account_id="chk", tid="bb_20")
    tx(citi, "2026-10-18", -20.00, None, name="ONLINE PAYMENT, THANK YOU", cat="Card payment", account_id="dc",
       item_id="item_citi", tid="dc_in_late")
    manual.match_payments(citi, today=TODAY)
    assert applied(citi, card["id"]) == ["bb_20", "bb_250", "bb_out"]
    assert balance(citi, card["id"]) == round(1110.66 - 35 - 250 - 20, 2)


def test_card_payments_wait_until_the_connected_side_could_have_posted(citi):
    card = manual.create(citi, {**BEST_BUY, "match_pattern": "CITI CARD ONLINE"}, TODAY)
    tx(citi, "2026-10-18", 99.00, None, name="CITI CARD ONLINE PAYMENT", cat="Card payment", account_id="chk", tid="fresh")
    out = manual.match_payments(citi, today=TODAY)                               # 2 days old: could still pair
    assert "fresh" not in applied(citi, card["id"])
    assert "waiting" in {s["transaction_id"]: s["reason"] for s in out["skipped"]}["fresh"]
    manual.match_payments(citi, today=date(2026, 10, 24))                        # 6 days old, still no pair: it's his
    assert "fresh" in applied(citi, card["id"])


def test_an_auto_match_that_pairs_later_is_taken_back(citi):
    card = manual.create(citi, {**BEST_BUY, "match_pattern": "CITI CARD ONLINE"}, TODAY)
    manual.match_payments(citi, today=TODAY)
    assert balance(citi, card["id"]) == 1075.66
    # the bank back-fills the connected card's side of bb_out (it was a Double Cash payment after all)
    tx(citi, "2026-10-09", -35.00, None, name="ONLINE PAYMENT, THANK YOU", cat="Card payment", account_id="dc",
       item_id="item_citi", tid="dc_in_2")
    out = manual.match_payments(citi, today=TODAY)
    assert applied(citi, card["id"]) == [] and balance(citi, card["id"]) == 1110.66
    assert [u["transaction_id"] for u in out["unapplied"]] == ["bb_out"]


def test_a_pairing_label_never_moves_a_loan_either(citi):
    loan = manual.create(citi, {"kind": "loan", "name": "Oops", "institution": "Citi", "subtype": "personal",
                                "original_amount": 5000, "balance": 3000, "monthly_payment": 250,
                                "match_pattern": "CITI CARD ONLINE", "balance_as_of": AS_OF}, TODAY)
    manual.match_payments(citi, today=TODAY)
    assert "dc_out" not in applied(citi, loan["id"]) and "co_out" not in applied(citi, loan["id"])


# --- the balance goes both ways: payments lower it, purchases aren't seen -----------------------------------------

def test_a_matched_card_balance_is_shown_as_approximate(citi):
    card = manual.create(citi, {**BEST_BUY, "match_pattern": "CITI CARD ONLINE"}, TODAY)
    manual.match_payments(citi, today=TODAY)
    d = manual.describe(citi, manual.get(citi, card["id"]), TODAY)
    assert d["approx"] is True and d["balance"] == 1075.66
    assert (d["entered_balance"], d["balance_as_of"], d["matched_total"], d["matched_count"]) == (1110.66, AS_OF, 35.0, 1)
    assert d["last_payment"] == {"date": "2026-10-08", "amount": 35.0, "name": "CITI CARD ONLINE PAYMENT"}
    assert d["stale"] is False and d["nudge"] is False                          # 24 days since he set it
    late = manual.describe(citi, manual.get(citi, card["id"]), date(2026, 11, 1))
    assert late["nudge"] is True and late["stale"] is False                      # 36 days: "update from statement"
    # debt, net worth and the read command all use the lower balance
    from hermes_finance import insights, portfolio, reads
    debt_card = next(c for c in portfolio.debt(citi, TODAY)["cards"] if c["manual"])
    assert (debt_card["balance"], debt_card["approx"], debt_card["entered_balance"], debt_card["matched_total"]) == \
        (1075.66, True, 1110.66, 35.0)
    assert insights.net_worth(citi, history=False)["manual"]["debts"] == 1075.66
    group = next(g for g in reads.networth(citi, TODAY)["groups"] if g["key"] == "cards")
    assert next(a for a in group["accounts"] if a["source"] == "manual")["balance"] == 1075.66


def test_a_card_without_a_pattern_is_as_entered_and_goes_stale(conn):
    card = manual.create(conn, BEST_BUY, TODAY)
    d = manual.describe(conn, manual.get(conn, card["id"]), date(2026, 11, 20))
    assert d["approx"] is False and d["nudge"] is False and d["stale"] is True and d["balance"] == 1110.66


def test_a_new_statement_balance_restarts_from_that_date(citi):
    card = manual.create(citi, {**BEST_BUY, "match_pattern": "CITI CARD ONLINE"}, TODAY)
    manual.match_payments(citi, today=TODAY)
    manual.edit(citi, card["id"], {"balance": 980.12, "balance_as_of": "2026-10-15"}, TODAY)   # his new statement
    d = manual.describe(citi, manual.get(citi, card["id"]), TODAY)
    assert (d["balance"], d["entered_balance"], d["matched_count"]) == (980.12, 980.12, 0)  # bb_out is in it now
    manual.edit(citi, card["id"], {"name": "Best Buy Visa"}, TODAY)                         # other fields: no change
    assert balance(citi, card["id"]) == 980.12


# --- idempotence, archive, dry runs ------------------------------------------------------------------------------

def test_matching_twice_changes_nothing(citi):
    card = manual.create(citi, {**BEST_BUY, "match_pattern": "CITI CARD ONLINE"}, TODAY)
    first = manual.match_payments(citi, today=TODAY)
    history = list(citi.execute("SELECT date, balance FROM manual_balance_history WHERE manual_account_id = ?",
                                (card["id"],)))
    second = manual.match_payments(citi, today=TODAY)
    assert len(first["applied"]) == 1 and second["applied"] == [] and second["unapplied"] == []
    assert balance(citi, card["id"]) == 1075.66
    assert list(citi.execute("SELECT date, balance FROM manual_balance_history WHERE manual_account_id = ?",
                             (card["id"],))) == history


def test_an_archived_card_stops_matching(citi):
    card = manual.create(citi, {**BEST_BUY, "match_pattern": "CITI CARD ONLINE"}, TODAY)
    manual.archive(citi, card["id"], TODAY)
    assert manual.match_payments(citi, today=TODAY)["applied"] == []
    assert applied(citi, card["id"]) == []
    with pytest.raises(manual.ManualError, match="archived"):
        manual.apply_payment(citi, "bb_out", card["id"])


def test_a_dry_run_writes_nothing(citi):
    card = manual.create(citi, {**BEST_BUY, "match_pattern": "CITI CARD ONLINE"}, TODAY)
    out = manual.match_payments(citi, dry_run=True, today=TODAY)
    assert [p["transaction_id"] for p in out["applied"]] == ["bb_out"] and out["dry_run"] is True
    assert applied(citi, card["id"]) == [] and balance(citi, card["id"]) == 1110.66


def test_manual_apply_and_unapply_work_on_a_card(citi):
    card = manual.create(citi, BEST_BUY, TODAY)                                   # no pattern: by hand
    p = manual.apply_payment(citi, "bb_out", card["id"])
    assert (p["balance_before"], p["balance_after"], p["interest"], p["principal"]) == (1110.66, 1075.66, 0.0, 35.0)
    assert manual.unapply_payment(citi, "bb_out")["balance_after"] == 1110.66
    with pytest.raises(manual.ManualError, match="not a loan or credit card"):
        manual.apply_payment(citi, "bb_out", manual.create(citi, {"kind": "property", "name": "Home",
                                                                  "balance": 1}, TODAY)["id"])


# --- the form's dry-run preview ("This would have matched N payments ($T) in the last 90 days") -------------------

@pytest.fixture
def app(citi, client):
    a = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


def get(app, path):
    status, _, out = app.handle("GET", path, LOCAL, b"")
    return status, json.loads(out)


def test_the_form_previews_90_days_of_matches(app, citi):
    status, d = get(app, "/api/manual/match?pattern=CITI%20CARD%20ONLINE&kind=credit_card")
    assert status == 200
    assert d["preview"] == {"days": 90, "since": "2026-07-22", "count": 2, "total": 70.0, "paired": 2,
                            "paired_total": 311.4, "waiting": 0, "elsewhere": 0}   # bb_out + bb_old; dc/co pair
    # nothing matches today's label: 0, which is right and expected
    status, d = get(app, "/api/manual/match?pattern=CBNA%20BEST%20BUY&kind=credit_card")
    assert status == 200 and d["preview"]["count"] == 0 and d["preview"]["total"] == 0.0 and d["count"] == 0
    assert get(app, "/api/manual/match?pattern=CITI&kind=boat")[0] == 400
    assert citi.execute("SELECT COUNT(*) FROM loan_payments").fetchone()[0] == 0     # a preview writes nothing


def test_the_card_form_suggests_only_unpaired_card_payments(app, citi):
    assert get(app, "/api/manual/match?kind=credit_card")[1]["suggestions"] == ["CITI CARD ONLINE PAYMENT"]
    manual.create(citi, {**BEST_BUY, "match_pattern": "CITI CARD ONLINE"}, TODAY)
    manual.match_payments(citi, today=TODAY)
    assert get(app, "/api/manual/match?kind=credit_card")[1]["suggestions"] == []   # all paired or applied now


# --- the CLI: match-payments (loan-match stays as its alias) and the daily run's suggestion ------------------------

def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_match_payments_and_the_loan_match_alias(citi, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    card = manual.create(citi, {**BEST_BUY, "match_pattern": "CITI CARD ONLINE"}, TODAY)
    code, out, _ = run(capsys, "match-payments", "--dry-run")
    assert code == 0 and "would apply bb_out" in out and "(dry run, nothing written)" in out
    assert "pairs with a payment on Citi Double Cash ••2417" in out and applied(citi, card["id"]) == []
    code, out, _ = run(capsys, "loan-match", "--json")
    assert code == 0 and [p["transaction_id"] for p in json.loads(out)["applied"]] == ["bb_out"]
    code, out, _ = run(capsys, "match-payments")
    assert code == 0 and "match-payments: 0 payment(s) applied" in out


def test_cli_suggests_the_first_autopay_line_without_saving_it(citi, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    card = manual.create(citi, BEST_BUY, TODAY)                                   # no pattern yet
    tx(citi, "2026-10-14", 25.00, None, name="CBNA BEST BUY AUTOPAY", cat="Card payment", account_id="chk", tid="bby")
    tx(citi, "2026-10-15", 119.99, "Best Buy", name="BEST BUY 00123", cat="Other Shopping", account_id="chk", tid="store")
    code, out, _ = run(capsys, "match-payments", "--suggest", "--json")
    s = json.loads(out)["suggestions"]
    assert code == 0 and [(x["transaction_id"], x["card_id"], x["pattern"]) for x in s] == \
        [("bby", card["id"], "CBNA BEST BUY AUTOPAY"), ("bb_out", card["id"], "CITI CARD ONLINE PAYMENT")]
    assert all("store" != x["transaction_id"] for x in s)                       # a purchase at Best Buy isn't one
    assert manual.get(citi, card["id"])["match_pattern"] is None                  # suggest only: nothing saved
    code, out, _ = run(capsys, "match-payments", "--suggest")
    assert code == 0 and "CBNA BEST BUY AUTOPAY" in out and "Best Buy" in out
    manual.edit(citi, card["id"], {"match_pattern": "CBNA BEST BUY"}, TODAY)
    assert json.loads(run(capsys, "match-payments", "--suggest", "--json")[1])["suggestions"] == []

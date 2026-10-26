"""Card t_74ec34ac: an imported OFX row's TRNTYPE files money coming in under Income (importer.OFX_INCOME_HINTS).

DIRECTDEP / DEP / PAYROLL -> INCOME_WAGES, INT -> INCOME_INTEREST_EARNED (never CREDIT: it's also card payments and refunds), saved on the
row as its Plaid category (pfc_primary 'INCOME'), so classify.apply() files it under the one Income category on every
run, a rule still winning. Money in only: OFX TRNAMT > 0, which the app stores as amount < 0 (Plaid's sign). A CSV row
is filed exactly as before. Synthetic rows only (the ACCEPTANCE ... names of the onboarding acceptance run)."""

from __future__ import annotations

import base64
import json
from datetime import date

import pytest

from hermes_finance import classify, config, importer, insights, tooldefs
from hermes_finance.web import App

TODAY = date(2026, 9, 28)
H = {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}
HEAD = ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:USASCII\nCHARSET:1252\nCOMPRESSION:NONE\n"
        "OLDFILEUID:NONE\nNEWFILEUID:NONE\n\n")
HINTS = {"DIRECTDEP": "INCOME_WAGES", "DEP": "INCOME_WAGES", "PAYROLL": "INCOME_WAGES",
         "INT": "INCOME_INTEREST_EARNED"}


def trn(trntype: str, amount: str, name: str, fitid: str, posted: str = "20260915",
        memo: str | None = None) -> str:
    return (f"<STMTTRN><TRNTYPE>{trntype}<DTPOSTED>{posted}<TRNAMT>{amount}<FITID>{fitid}<NAME>{name}"
            f"{f'<MEMO>{memo}' if memo else ''}</STMTTRN>")


def ofx(*trns: str, acctid: str = "9990001234") -> bytes:
    """An OFX 1 (SGML) bank statement with these transactions; TRNAMT is the file's sign (negative = money out)."""
    return (HEAD + "<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS></SONRS></SIGNONMSGSRSV1>"
            "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>999999999"
            f"<ACCTID>{acctid}<ACCTTYPE>CHECKING</BANKACCTFROM><BANKTRANLIST>{''.join(trns)}</BANKTRANLIST>"
            "<LEDGERBAL><BALAMT>1000.00<DTASOF>20260926</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>").encode()


def card_ofx(*trns: str, acctid: str = "0000111122229876") -> bytes:
    """An OFX 1 (SGML) credit-card statement (CCSTMTRS) with these transactions."""
    return (HEAD + "<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS></SONRS></SIGNONMSGSRSV1>"
            "<CREDITCARDMSGSRSV1><CCSTMTTRNRS><TRNUID>1<CCSTMTRS><CURDEF>USD<CCACCTFROM>"
            f"<ACCTID>{acctid}</CCACCTFROM><BANKTRANLIST>{''.join(trns)}</BANKTRANLIST>"
            "<LEDGERBAL><BALAMT>-100.00<DTASOF>20260926</LEDGERBAL></CCSTMTRS></CCSTMTTRNRS>"
            "</CREDITCARDMSGSRSV1></OFX>").encode()


def creditline_ofx(*trns: str, acctid: str = "9990001234") -> bytes:
    """A bank statement whose ACCTTYPE is CREDITLINE: a card for the payment hint, a bank account for the target."""
    return (HEAD + "<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS></SONRS></SIGNONMSGSRSV1>"
            "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>999999999"
            f"<ACCTID>{acctid}<ACCTTYPE>CREDITLINE</BANKACCTFROM><BANKTRANLIST>{''.join(trns)}</BANKTRANLIST>"
            "<LEDGERBAL><BALAMT>1000.00<DTASOF>20260926</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>").encode()


CARD_TARGET = {"new": {"name": "Rewards Card", "kind": "credit_card", "credit_limit": 5000}}


def card_targets(label: str = "Credit card ••9876") -> dict:
    return {label: CARD_TARGET}


def mbp() -> bytes:
    """The onboarding acceptance file: two purchases and a direct deposit, inside the last 60 days of TODAY."""
    return ofx(trn("DEBIT", "-12.34", "ACCEPTANCE COFFEE", "ACC-1", "20260905"),
               trn("DEBIT", "-45.67", "ACCEPTANCE GROCER", "ACC-2", "20260912"),
               trn("DIRECTDEP", "1000.00", "ACCEPTANCE PAYROLL", "ACC-3", "20260915"))


def commit(conn, raw: bytes, filename: str = "acceptance.ofx", targets: dict | None = None,
           today: date = TODAY) -> dict:
    return importer.commit(conn, filename, raw, {"targets": targets} if targets else {}, today)


def row(conn, name: str) -> dict:
    r = conn.execute("SELECT v.transaction_id, v.amount, v.category, v.category_source, t.pfc_primary, t.pfc_detailed, "
                     "t.raw_json FROM v_transactions v JOIN transactions t USING (transaction_id) WHERE v.name = ?",
                     (name,)).fetchone()
    assert r is not None, name
    return {**dict(r), "raw": json.loads(r["raw_json"])}


def classifications(conn) -> list[tuple]:
    return [tuple(r) for r in conn.execute("SELECT transaction_id, category_id, source, rule_id, note, updated_at "
                                           "FROM classifications ORDER BY transaction_id")]


# --- the mapping ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("trntype", list(HINTS))
def test_each_income_type_coming_in_files_under_income(conn, trntype):
    commit(conn, ofx(trn(trntype, "250.00", "ACCEPTANCE INFLOW", "IN-1")))
    r = row(conn, "ACCEPTANCE INFLOW")
    assert r["amount"] == -250.0                       # TRNAMT +250.00: money in, stored negative (Plaid's sign)
    assert (r["category"], r["category_source"]) == ("Income", "plaid")
    assert (r["pfc_primary"], r["pfc_detailed"]) == ("INCOME", HINTS[trntype])
    assert r["raw"]["trntype"] == trntype


def test_the_three_hints_stay_apart_in_the_data(conn):
    """One Income category today; pay, interest and other income keep their own code, for a finer taxonomy later."""
    commit(conn, ofx(trn("DIRECTDEP", "900.00", "ACCEPTANCE PAY", "D-1"), trn("DEP", "50.00", "ACCEPTANCE DEPOSIT", "D-2"),
                     trn("PAYROLL", "800.00", "ACCEPTANCE PAYROLL RUN", "D-3"), trn("INT", "1.25", "ACCEPTANCE INTEREST", "D-4"),
                     trn("CREDIT", "9.99", "ACCEPTANCE CREDIT", "D-5")))   # (CREDIT: no hint, below)
    got = {r["name"]: (r["category"], r["pfc_detailed"]) for r in conn.execute(
        "SELECT v.name, v.category, t.pfc_detailed FROM v_transactions v JOIN transactions t USING (transaction_id)")}
    assert got == {"ACCEPTANCE PAY": ("Income", "INCOME_WAGES"), "ACCEPTANCE DEPOSIT": ("Income", "INCOME_WAGES"),
                   "ACCEPTANCE PAYROLL RUN": ("Income", "INCOME_WAGES"),
                   "ACCEPTANCE INTEREST": ("Income", "INCOME_INTEREST_EARNED"),
                   "ACCEPTANCE CREDIT": ("Other", None)}


@pytest.mark.parametrize("name, amount", [("PAYMENT THANK YOU", "512.34"), ("CAFE LUNA REFUND", "8.75"),
                                          ("PIXEL BOOKS RETURN", "24.99")])
def test_a_generic_credit_is_never_income(conn, name, amount):
    """CREDIT is any money in: a card payment or a refund is not income, so it gets no hint."""
    commit(conn, ofx(trn("CREDIT", amount, name, "CR-" + name[:4])))
    r = row(conn, name)
    assert r["category"] != "Income"
    assert (r["pfc_primary"], r["pfc_detailed"]) == (None, None)
    assert r["raw"]["trntype"] == "CREDIT"


def test_the_type_is_read_whatever_its_case(conn):
    commit(conn, ofx(trn("directdep", "100.00", "ACCEPTANCE PAYROLL", "C-1")))
    r = row(conn, "ACCEPTANCE PAYROLL")
    assert (r["category"], r["pfc_detailed"], r["raw"]["trntype"]) == ("Income", "INCOME_WAGES", "DIRECTDEP")


@pytest.mark.parametrize("trntype", list(HINTS))
@pytest.mark.parametrize("amount", ["-250.00", "0.00"])
def test_money_out_of_an_income_type_gets_no_hint(conn, trntype, amount):
    """The guard: OFX TRNAMT <= 0 (money out, or nothing; the app's amount >= 0) is filed like any unhinted import row."""
    commit(conn, ofx(trn(trntype, amount, "ACCEPTANCE OUTFLOW", "OUT-1")))
    r = row(conn, "ACCEPTANCE OUTFLOW")
    assert r["amount"] == -float(amount)
    assert (r["category"], r["category_source"]) == ("Other", "plaid")
    assert (r["pfc_primary"], r["pfc_detailed"]) == (None, None)
    assert r["raw"]["trntype"] == trntype             # the type is still kept, for the record


@pytest.mark.parametrize("trntype", ["XFER", "POS", "DEBIT", "CHECK", "OTHER", "ATM", "DIV", "FEE"])
def test_money_in_of_any_other_type_gets_no_hint(conn, trntype):
    commit(conn, ofx(trn(trntype, "250.00", "ACCEPTANCE INFLOW", "IN-1")))
    r = row(conn, "ACCEPTANCE INFLOW")
    assert r["amount"] == -250.0
    assert (r["category"], r["category_source"], r["pfc_primary"], r["pfc_detailed"]) == ("Other", "plaid", None, None)


# --- rules win; the hint lasts ----------------------------------------------------------------------------------------

def test_a_rule_saved_before_the_import_wins_over_the_hint(conn):
    rule = classify.add_rule(conn, "name", "ACCEPTANCE PAYROLL", "Transfers", created_from="user")
    commit(conn, mbp())
    r = row(conn, "ACCEPTANCE PAYROLL")
    assert (r["category"], r["category_source"]) == ("Transfers", "rule")
    assert r["pfc_detailed"] == "INCOME_WAGES"         # the hint is on the row, the rule decides
    assert conn.execute("SELECT rule_id FROM classifications WHERE transaction_id = ?",
                        (r["transaction_id"],)).fetchone()[0] == rule.id


def test_a_rule_saved_after_the_import_refiles_a_hinted_row(conn):
    commit(conn, mbp())
    assert row(conn, "ACCEPTANCE PAYROLL")["category"] == "Income"
    saved = classify.add_rule(conn, "name", "ACCEPTANCE PAYROLL", "Transfers", created_from="user")
    assert saved.refiled == 1
    assert (row(conn, "ACCEPTANCE PAYROLL")["category"], row(conn, "ACCEPTANCE PAYROLL")["category_source"]) == (
        "Transfers", "rule")
    classify.apply(conn)
    assert row(conn, "ACCEPTANCE PAYROLL")["category"] == "Transfers"


def test_the_hint_survives_every_later_classify_run(conn):
    commit(conn, mbp())
    before = classifications(conn)
    counts = classify.apply(conn)
    assert counts == {"rule": 0, "plaid": 0, "unchanged": 3}
    rule = classify.add_rule(conn, "name", "SOMETHING ELSE ENTIRELY", "Coffee", created_from="user")
    classify.apply(conn)
    classify.delete_rule(conn, rule.id)
    classify.apply(conn)
    assert classifications(conn) == before
    r = row(conn, "ACCEPTANCE PAYROLL")
    assert (r["category"], r["category_source"]) == ("Income", "plaid")


def test_ai_and_user_decisions_are_never_touched(conn):
    commit(conn, ofx(trn("DIRECTDEP", "1000.00", "ACCEPTANCE PAYROLL", "P-1"), trn("INT", "2.00", "ACCEPTANCE INTEREST", "P-2")))
    pay, interest = row(conn, "ACCEPTANCE PAYROLL")["transaction_id"], row(conn, "ACCEPTANCE INTEREST")["transaction_id"]
    assert classify.set_category(conn, pay, "Transfers", "ai", 0.9) == "set"
    assert classify.set_category(conn, interest, "Gifts & Charity", "user") == "set"
    classify.apply(conn)
    commit(conn, ofx(trn("CREDIT", "5.00", "ACCEPTANCE LATER", "P-3")))   # a later import runs apply() too
    assert (row(conn, "ACCEPTANCE PAYROLL")["category"], row(conn, "ACCEPTANCE PAYROLL")["category_source"]) == (
        "Transfers", "ai")
    assert (row(conn, "ACCEPTANCE INTEREST")["category"], row(conn, "ACCEPTANCE INTEREST")["category_source"]) == (
        "Gifts & Charity", "user")


def test_the_same_file_again_adds_nothing_and_refiles_nothing(conn, monkeypatch):
    first = commit(conn, mbp())
    assert (first["new"], first["skipped"]) == (3, 0)
    before = classifications(conn)
    txns = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    # a rewrite of any classification during the second import would carry this stamp
    monkeypatch.setattr(classify, "now", lambda: "2099-01-01T00:00:00Z")
    again = commit(conn, mbp())
    assert (again["new"], again["skipped"]) == (0, 3)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == txns == 3
    assert classifications(conn) == before
    # the user's own filing of the paycheck survives the next import of the same file
    pay = row(conn, "ACCEPTANCE PAYROLL")["transaction_id"]
    assert classify.set_category(conn, pay, "Transfers", "user") == "set"
    third = commit(conn, mbp())
    assert (third["new"], third["skipped"]) == (0, 3)
    r = row(conn, "ACCEPTANCE PAYROLL")
    assert (r["category"], r["category_source"]) == ("Transfers", "user")


def test_a_csv_inflow_is_filed_as_before(conn):
    raw = (b"Date,Description,Amount\n09/05/2026,ACCEPTANCE COFFEE,-12.34\n09/15/2026,ACCEPTANCE PAYROLL,1000.00\n")
    assert all(r.trntype is None for r in importer.importfile.csv_rows(
        raw, {"date": 0, "description": 1, "amount": 2}, "%m/%d/%Y", "minus_out", today=TODAY))
    commit(conn, raw, "acceptance.csv")
    r = row(conn, "ACCEPTANCE PAYROLL")
    assert r["amount"] == -1000.0
    assert (r["category"], r["category_source"], r["pfc_primary"], r["pfc_detailed"]) == ("Other", "plaid", None, None)
    assert set(r["raw"]) == {"file", "line", "fitid"}


# --- end to end: the onboarding acceptance file, through the import endpoint the page uses ----------------------------

@pytest.fixture
def app(conn):
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


def call(app, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    s, _, out, _ = app.respond(method, path, H, json.dumps(body).encode() if body is not None else b"")
    return s, json.loads(out)


def import_mbp(app) -> None:
    data = base64.b64encode(mbp()).decode()
    s, p = call(app, "POST", "/api/import/preview", {"filename": "acceptance.ofx", "data": data})
    assert s == 200 and (p["rows"], p["new"]) == (3, 3)
    s, r = call(app, "POST", "/api/import/commit", {"filename": "acceptance.ofx", "data": data,
                                                     "targets": {g["label"]: g["target"] for g in p["groups"]}})
    assert s == 200 and (r["new"], r["skipped"]) == (3, 0)


def numbers(app) -> tuple[float, float, float]:
    """(Overview's month-to-date spending, Overview's income, the agent tools' / MCP finance_spend total)."""
    s, ov = call(app, "GET", "/api/overview?month=2026-09")
    assert s == 200
    spend = tooldefs.run_read("finance_spend", {}, config.db_path(), TODAY)
    return ov["summary"]["mtd"], ov["summary"]["income"], spend["total"]


def test_acceptance_file_spending_and_income(app, conn):
    import_mbp(app)
    assert numbers(app) == (58.01, 1000.0, 58.01)    # was (-941.99, 0.0, -941.99): the paycheck counted as a refund
    # The wizard's suggested budgets stay empty for this exact file, and that's not the paycheck: both purchases are in
    # Other (no Plaid category, no rule), and admin.suggestions skips insights.NO_SUGGEST, which has "Other" (Needs
    # review). A change to the wizard or NO_SUGGEST shows up here as a deliberate test update.
    assert "Other" in insights.NO_SUGGEST
    assert {row(conn, n)["category"] for n in ("ACCEPTANCE COFFEE", "ACCEPTANCE GROCER")} == {"Other"}
    s, sug = call(app, "GET", "/api/setup/suggestions")
    assert s == 200 and sug["categories"] == []


def test_the_wizard_says_the_spending_is_uncategorized_not_missing(app, conn):
    """t_0db639c5: step 6 read "No spending in the last 60 days yet" for an import-only user whose debits are all in
    Other (skipped by insights.NO_SUGGEST). `other_spend` tells the page which sentence to use."""
    s, sug = call(app, "GET", "/api/setup/suggestions")
    assert s == 200 and (sug["categories"], sug["other_spend"]) == ([], 0.0)      # truly empty: no sentence is wrong
    import_mbp(app)
    s, sug = call(app, "GET", "/api/setup/suggestions")
    # both purchases are in Other, and the deposit is Income: the window's Other spend is the purchases only
    assert s == 200 and sug["categories"] == [] and sug["other_spend"] == 58.01


def test_other_spend_is_window_only_and_ignores_rows_filed_elsewhere(app, conn):
    import_mbp(app)
    conn.execute("UPDATE transactions SET date = '2026-01-05' WHERE name = 'ACCEPTANCE COFFEE'")   # outside the 60 days
    conn.commit()
    s, sug = call(app, "GET", "/api/setup/suggestions")
    assert s == 200 and sug["other_spend"] == 45.67
    classify.add_rule(conn, "name", "ACCEPTANCE GROCER", "Groceries", created_from="user")
    classify.apply(conn)
    s, sug = call(app, "GET", "/api/setup/suggestions")
    assert s == 200 and sug["other_spend"] == 0.0 and [c["category"] for c in sug["categories"]] == ["Groceries"]


def test_acceptance_file_with_the_users_rules_suggests_their_budgets(app, conn):
    classify.add_rule(conn, "name", "ACCEPTANCE COFFEE", "Coffee", created_from="user")
    classify.add_rule(conn, "name", "ACCEPTANCE GROCER", "Groceries", created_from="user")
    import_mbp(app)
    assert numbers(app) == (58.01, 1000.0, 58.01)
    s, sug = call(app, "GET", "/api/setup/suggestions")
    assert s == 200 and sug["days"] == 60
    # admin.suggestions: half the last 60 days' spend (30 / 60), rounded up by insights.round_cap ($25 steps)
    assert sug["categories"] == [
        {"category": "Groceries", "spent": 45.67, "days": 60, "suggested": insights.round_cap(45.67 * 30 / 60), "cap": None},
        {"category": "Coffee", "spent": 12.34, "days": 60, "suggested": insights.round_cap(12.34 * 30 / 60), "cap": None}]
    assert [c["suggested"] for c in sug["categories"]] == [25.0, 25.0]


# --- a credit-card statement's own payment line: a transfer, not Other ------------------------------------------------

@pytest.fixture
def oct_app(conn):
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    a.today = lambda: date(2026, 10, 25)
    return a


def test_a_card_statements_payment_row_is_a_transfer(app, conn):
    commit(conn, card_ofx(trn("DEBIT", "-58.01", "ACCEPTANCE MARKET", "CC-1", "20260905"),
                          trn("CREDIT", "512.34", "PAYMENT THANK YOU", "CC-2", "20260920")),
           "card.qfx", card_targets())
    r = row(conn, "PAYMENT THANK YOU")
    assert (r["pfc_primary"], r["pfc_detailed"]) == ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    assert (r["category"], r["category_source"], r["amount"]) == ("Transfers", "plaid", -512.34)
    assert r["raw"]["trntype"] == "CREDIT"
    assert numbers(app) == (58.01, 0.0, 58.01)     # the payment is out of spending


def test_the_same_payment_on_a_bank_statement_stays_in_other(app, conn):
    commit(conn, ofx(trn("DEBIT", "-58.01", "ACCEPTANCE MARKET", "B-1", "20260905"),
                     trn("CREDIT", "512.34", "PAYMENT THANK YOU", "B-2", "20260920")))
    r = row(conn, "PAYMENT THANK YOU")
    assert (r["category"], r["pfc_primary"], r["pfc_detailed"]) == ("Other", None, None)
    assert numbers(app) == (-454.33, 0.0, -454.33)    # still nets against spending


def test_a_card_refund_stays_in_other(app, conn):
    commit(conn, card_ofx(trn("DEBIT", "-58.01", "ACCEPTANCE MARKET", "C-1", "20260905"),
                          trn("CREDIT", "24.99", "PIXEL BOOKS &amp; GAMES", "C-2", "20260914", memo="RETURN")),
           "card.qfx", card_targets())
    r = row(conn, "PIXEL BOOKS & GAMES")
    assert (r["category"], r["pfc_primary"], r["pfc_detailed"]) == ("Other", None, None)
    assert r["amount"] == -24.99
    assert numbers(app) == (33.02, 0.0, 33.02)     # a refund still reduces spending


@pytest.mark.parametrize("before", [True, False])
def test_a_user_rule_wins_over_the_card_payment_hint(conn, before):
    if before:
        classify.add_rule(conn, "name", "PAYMENT THANK YOU", "Groceries", created_from="user")
    commit(conn, card_ofx(trn("CREDIT", "512.34", "PAYMENT THANK YOU", "R-1", "20260920")),
           "card.qfx", card_targets())
    r = row(conn, "PAYMENT THANK YOU")
    if before:
        assert (r["category"], r["category_source"]) == ("Groceries", "rule")
    else:
        assert (r["category"], r["category_source"]) == ("Transfers", "plaid")
        saved = classify.add_rule(conn, "name", "PAYMENT THANK YOU", "Groceries", created_from="user")
        assert saved.refiled == 1
    classify.apply(conn)
    r = row(conn, "PAYMENT THANK YOU")
    assert (r["category"], r["category_source"]) == ("Groceries", "rule")
    assert r["pfc_detailed"] == "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"   # the hint is on the row, the rule decides


def test_a_creditline_bank_statements_payment_row_is_a_transfer(conn):
    commit(conn, creditline_ofx(trn("CREDIT", "250.00", "ONLINE PAYMENT", "CL-1", "20260920")),
           "creditline.ofx", card_targets("Credit line ••1234"))
    r = row(conn, "ONLINE PAYMENT")
    assert (r["category"], r["category_source"], r["pfc_primary"], r["pfc_detailed"]) == (
        "Transfers", "plaid", "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")


def test_acceptance_a_card_payment_does_not_make_the_month_negative(oct_app, conn):
    """The retest shape: October purchases and a paycheck, then the card's CREDIT PAYMENT THANK YOU."""
    day = date(2026, 10, 25)
    commit(conn, ofx(trn("DEBIT", "-12.34", "ACCEPTANCE COFFEE", "ACC-1", "20261020"),
                     trn("DEBIT", "-45.67", "ACCEPTANCE GROCER", "ACC-2", "20261021"),
                     trn("DIRECTDEP", "1000.00", "ACCEPTANCE PAYROLL", "ACC-3", "20261022")),
           "acceptance.ofx", today=day)
    commit(conn, card_ofx(trn("CREDIT", "512.34", "CREDIT PAYMENT THANK YOU", "ACC-4", "20261022")),
           "card.qfx", card_targets(), today=day)
    s, ov = call(oct_app, "GET", "/api/overview?month=2026-10")
    assert s == 200
    assert (ov["summary"]["mtd"], ov["summary"]["income"]) == (58.01, 1000.0)
    assert tooldefs.run_read("finance_spend", {}, config.db_path(), day)["total"] == 58.01
    assert (row(conn, "CREDIT PAYMENT THANK YOU")["category"],
            row(conn, "CREDIT PAYMENT THANK YOU")["category_source"]) == ("Transfers", "plaid")

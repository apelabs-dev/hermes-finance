"""QA 0.5 F1 + F2: an imported money-in row is income, not negative Other spending.

A PDF / CSV statement carries no Plaid category and no TRNTYPE (an OFX row does: importer.OFX_INCOME_HINTS). A
payroll deposit therefore arrived with nothing to file it by, and classify.apply() filed it under Other - a
*spending* category - so the month's spending went negative. That is one gap with two faces:

  F1  Overview "Where it went": the donut centre said "$4,253 shown of $3,480 spent" with Misc at -$773, because
      a negative group is dropped from `shown` but still counted in `total`.
  F2  Reports: "Refunds over spending . Other", "Most variable: Other", a negative savings rate; the Cash flow
      page presented the imported payroll as a refund of spending.

The fix: classify.apply() gives a CSV / PDF money-in row the code classify.inflow_hint() already gives a synced
inflow with no provider category (SimpleFIN) - the one shared list of refund, tax-refund, card-payment and
transfer words - and saves it on the row as its Plaid category, so it files under Income (or Transfers) exactly
the way an OFX DEP / DIRECTDEP deposit does. A rule still wins (it always did), money-out rows are unchanged, and
a row imported before the fix is re-filed by the same apply() with no migration.

Synthetic rows only: the QA report's own F1 CSV, and the repo's PDF fixtures.
"""

from __future__ import annotations

import base64
import json
from datetime import date

import pytest

from hermes_finance import classify, config, importer
from hermes_finance.web import App

from pdf_fixtures import good_statement

TODAY = date(2026, 11, 24)
H = {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}
# The QA report's F1 CSV, verbatim: a payroll deposit and two purchases, no categories anywhere.
F1_CSV = (b"Date,Description,Amount\n"
          b"2026-11-05,QA PAYCHECK DEPOSIT,1000.00\n"
          b"2026-11-06,QA SHOP,-12.00\n"
          b"2026-11-07,QA CAFE,-4.50\n")


def commit(conn, raw: bytes, filename: str = "f1.csv", targets: dict | None = None,
           today: date = TODAY, sign: str | None = "minus_out") -> dict:
    """A commit of the file. `sign` says which way an unsigned amount points (money in or out); the QA CSVs are
    written with a positive deposit and negative purchases, so minus_out reads them as the app does."""
    data: dict = {"targets": targets} if targets else {}
    if sign:
        data["sign"] = sign
    return importer.commit(conn, filename, raw, data, today)


def row(conn, name: str) -> dict:
    r = conn.execute("SELECT v.transaction_id, v.amount, v.category, v.category_source, t.pfc_primary, "
                     "t.pfc_detailed FROM v_transactions v JOIN transactions t USING (transaction_id) "
                     "WHERE v.name = ?", (name,)).fetchone()
    assert r is not None, name
    return dict(r)


def classifications(conn) -> list[tuple]:
    return [tuple(r) for r in conn.execute("SELECT transaction_id, category_id, source, rule_id, note, updated_at "
                                           "FROM classifications ORDER BY transaction_id")]


@pytest.fixture
def app(conn):
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


def call(app, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    s, _, out, _ = app.respond(method, path, H, json.dumps(body).encode() if body is not None else b"")
    return s, json.loads(out)


# --- F2: the CSV payroll deposit is Income, the purchases are unchanged -------------------------------------------------

def test_the_qa_csv_paycheck_is_income_not_negative_spending(conn):
    commit(conn, F1_CSV)
    pay = row(conn, "QA PAYCHECK DEPOSIT")
    assert pay["amount"] == -1000.0                       # money in, Plaid's sign
    assert (pay["category"], pay["category_source"]) == ("Income", "plaid")
    # the code is on the row, the way an OFX direct deposit's is: the default filing reads it from there
    assert (pay["pfc_primary"], pay["pfc_detailed"]) == ("INCOME", "INCOME_OTHER_INCOME")
    # money out is unchanged: no category from the file, so it stays Other and waits for a rule / a person
    for name, amount in (("QA SHOP", 12.0), ("QA CAFE", 4.5)):
        r = row(conn, name)
        assert (r["amount"], r["category"], r["category_source"]) == (amount, "Other", "plaid")
        assert (r["pfc_primary"], r["pfc_detailed"]) == (None, None)


def test_a_pdf_statements_payroll_deposit_is_income(conn):
    """The check screen's own statement: ACH DEPOSIT PAYROLL +$1,600.00 and four purchases."""
    out = commit(conn, good_statement(), "good.pdf")
    assert out["new"] == 5
    pay = row(conn, "ACH DEPOSIT PAYROLL")
    assert pay["amount"] == -1600.0
    assert (pay["category"], pay["category_source"]) == ("Income", "plaid")
    assert (pay["pfc_primary"], pay["pfc_detailed"]) == ("INCOME", "INCOME_OTHER_INCOME")
    # every credit has a code and every debit keeps none (the QA report's "no NULL pfc on credits")
    credits = [r for r in conn.execute("SELECT v.name, v.amount, t.pfc_primary FROM v_transactions v JOIN transactions t "
                                       "USING (transaction_id) WHERE v.source = 'import' AND v.amount < 0")]
    assert [c["name"] for c in credits] == ["ACH DEPOSIT PAYROLL"]
    assert all(c["pfc_primary"] for c in credits)


def test_a_csv_credit_that_names_a_refund_stays_out_of_income(conn):
    """Owner rule: a refund is never Income. It stays undecided and nets against spending."""
    commit(conn, b"Date,Description,Amount\n2026-11-05,QA STORE REFUND,25.00\n")
    r = row(conn, "QA STORE REFUND")
    assert (r["category"], r["category_source"], r["pfc_primary"], r["pfc_detailed"]) == ("Other", "plaid", None, None)


def test_a_csv_transfer_in_is_a_transfer(conn):
    commit(conn, b"Date,Description,Amount\n2026-11-05,TRANSFER FROM SAVINGS,500.00\n")
    r = row(conn, "TRANSFER FROM SAVINGS")
    assert (r["category"], r["category_source"]) == ("Transfers", "plaid")
    assert (r["pfc_primary"], r["pfc_detailed"]) == ("TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER")


def test_a_card_imports_merchant_credit_is_still_not_income(conn):
    """On a card, an inflow that isn't the card's own payment is a merchant credit: it nets, never Income."""
    commit(conn, b"Date,Description,Amount\n2026-11-05,QA RETURNED GADGET,25.00\n",
           "card.csv", {"Card": {"new": {"name": "QA Card", "kind": "credit_card", "credit_limit": 1000}}})
    r = row(conn, "QA RETURNED GADGET")
    assert (r["category"], r["category_source"], r["pfc_primary"]) == ("Other", "plaid", None)


def test_a_rule_saved_before_the_import_still_wins(conn):
    rule = classify.add_rule(conn, "name", "QA PAYCHECK DEPOSIT", "Transfers", created_from="user")
    commit(conn, F1_CSV)
    r = row(conn, "QA PAYCHECK DEPOSIT")
    assert (r["category"], r["category_source"]) == ("Transfers", "rule")
    assert conn.execute("SELECT rule_id FROM classifications WHERE transaction_id = ?",
                        (r["transaction_id"],)).fetchone()[0] == rule.id
    assert r["pfc_detailed"] == "INCOME_OTHER_INCOME"     # the code is on the row, the rule decides


def test_the_same_file_again_adds_nothing_and_refiles_nothing(conn, monkeypatch):
    first = commit(conn, F1_CSV)
    assert (first["new"], first["skipped"]) == (3, 0)
    before, txns = classifications(conn), conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    monkeypatch.setattr(classify, "now", lambda: "2099-01-01T00:00:00Z")   # any rewrite would carry this stamp
    again = commit(conn, F1_CSV)
    assert (again["new"], again["skipped"]) == (0, 3)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == txns == 3
    assert classifications(conn) == before


def test_a_row_imported_before_the_fix_is_refiled_by_the_next_apply(conn, monkeypatch):
    """No migration: the same apply() re-files what an older import left in Other (it only touches rows with a
    Plaid-default filing or none, never a rule / the AI / a person's decision)."""
    # the old behaviour: a CSV credit got no code (the method didn't exist before the fix, so raising=False)
    monkeypatch.setattr(classify, "_import_hint", lambda t: (None, None), raising=False)
    commit(conn, F1_CSV)
    assert (row(conn, "QA PAYCHECK DEPOSIT")["category"], row(conn, "QA PAYCHECK DEPOSIT")["pfc_primary"]) == (
        "Other", None)
    monkeypatch.undo()
    counts = classify.apply(conn)
    pay = row(conn, "QA PAYCHECK DEPOSIT")
    assert (pay["category"], pay["category_source"], pay["pfc_primary"]) == ("Income", "plaid", "INCOME")
    assert counts["plaid"] == 1
    assert row(conn, "QA SHOP")["category"] == "Other"     # the purchases are untouched


def test_a_generic_ofx_credit_is_still_never_income(conn):
    """The control: an OFX row keeps its TRNTYPE rule (owner rule: a generic CREDIT is never Income), whatever
    its description says."""
    head = ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:USASCII\nCHARSET:1252\n"
            "COMPRESSION:NONE\nOLDFILEUID:NONE\nNEWFILEUID:NONE\n\n")
    raw = (head + "<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS></SONRS></SIGNONMSGSRSV1>"
           "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>999999999"
           "<ACCTID>9990001234<ACCTTYPE>CHECKING</BANKACCTFROM><BANKTRANLIST><STMTTRN><TRNTYPE>CREDIT"
           "<DTPOSTED>20260915<TRNAMT>1000.00<FITID>C-1<NAME>QA PAYCHECK DEPOSIT</STMTTRN></BANKTRANLIST>"
           "<LEDGERBAL><BALAMT>1000.00<DTASOF>20260926</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>").encode()
    commit(conn, raw, "qa.ofx")
    r = row(conn, "QA PAYCHECK DEPOSIT")
    assert (r["category"], r["pfc_primary"], r["pfc_detailed"]) == ("Other", None, None)


# --- round 2 (reviewer 855): the recorded file kind decides, not the absence of a TRNTYPE ----------------------------

OFX_HEAD = ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:USASCII\nCHARSET:1252\n"
            "COMPRESSION:NONE\nOLDFILEUID:NONE\nNEWFILEUID:NONE\n\n")


def bank_ofx(trns: str) -> bytes:
    """An OFX 1 (SGML) bank statement carrying each STMTTRN verbatim: the TRNTYPE tag can be left out or empty."""
    return (OFX_HEAD + "<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS></SONRS></SIGNONMSGSRSV1>"
            "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>999999999"
            "<ACCTID>9990001234<ACCTTYPE>CHECKING</BANKACCTFROM><BANKTRANLIST>" + trns + "</BANKTRANLIST>"
            "<LEDGERBAL><BALAMT>1000.00<DTASOF>20260926</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>").encode()


@pytest.mark.parametrize("type_tag", ["", "<TRNTYPE>", "<TRNTYPE>   "])
def test_an_ofx_credit_with_no_readable_trntype_is_still_not_income(conn, type_tag):
    """Reviewer 855, first fix. Provenance is the file kind recorded on the import, not the absence of a TRNTYPE: an
    OFX row whose type is missing or blank is still an OFX row, so the CSV/PDF description hint must not touch it
    (owner rule 2026-10-25: a generic credit - a card payment, a refund - is never Income)."""
    commit(conn, bank_ofx(f"<STMTTRN>{type_tag}<DTPOSTED>20260915<TRNAMT>145.00<FITID>T-1"
                          "<NAME>PAYMENT THANK YOU</STMTTRN>"), "typless.ofx")
    r = row(conn, "PAYMENT THANK YOU")
    assert r["amount"] == -145.0
    assert (r["category"], r["category_source"], r["pfc_primary"], r["pfc_detailed"]) == ("Other", "plaid", None, None)
    assert conn.execute("SELECT kind FROM imports").fetchone()[0] == "ofx"


def test_an_ofx_direct_deposit_with_no_trntype_does_not_become_income_either(conn):
    """The same guard, on a description that *would* be Income from a CSV: only the OFX type can file it, and there
    is none, so it stays Other and waits for a rule or a person (no guessing from the text)."""
    commit(conn, bank_ofx("<STMTTRN><DTPOSTED>20260915<TRNAMT>1000.00<FITID>T-2"
                          "<NAME>QA PAYCHECK DEPOSIT</STMTTRN>"), "typless.ofx")
    r = row(conn, "QA PAYCHECK DEPOSIT")
    assert (r["category"], r["pfc_primary"], r["pfc_detailed"]) == ("Other", None, None)


def test_a_card_purchase_refund_naming_a_payment_stays_spending(conn):
    """Reviewer 855, second fix. `QA STORE PAYMENT REFUND` contains a payment word but names a refund: a refund is
    never Income and it is not the card's own payment either, so it stays undecided and nets against spending."""
    commit(conn, b"Date,Description,Amount\n"
                 b"2026-11-04,QA MART,-30.00\n"
                 b"2026-11-05,QA STORE PAYMENT REFUND,12.00\n",
           "card.csv", {"Card": {"new": {"name": "QA Card", "kind": "credit_card", "credit_limit": 1000}}})
    refund = row(conn, "QA STORE PAYMENT REFUND")
    assert (refund["amount"], refund["category"], refund["category_source"], refund["pfc_primary"],
            refund["pfc_detailed"]) == (-12.0, "Other", "plaid", None, None)


def test_a_card_csvs_own_payment_line_is_still_a_transfer(conn):
    """The control beside the refund: a card file's real `PAYMENT THANK YOU` line is the transfer it always is."""
    commit(conn, b"Date,Description,Amount\n2026-11-06,PAYMENT THANK YOU,100.00\n",
           "card.csv", {"Card": {"new": {"name": "QA Card", "kind": "credit_card", "credit_limit": 1000}}})
    payment = row(conn, "PAYMENT THANK YOU")
    assert (payment["category"], payment["pfc_primary"], payment["pfc_detailed"]) == (
        "Transfers", "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")


def test_a_bank_tax_refund_is_still_income(conn):
    """The exception the card guard must not eat: on a bank account a tax refund stays Income."""
    commit(conn, b"Date,Description,Amount\n2026-11-05,IRS TREAS 310 TAX REF,420.00\n")
    r = row(conn, "IRS TREAS 310 TAX REF")
    assert (r["category"], r["category_source"], r["pfc_primary"], r["pfc_detailed"]) == (
        "Income", "plaid", "INCOME", "INCOME_TAX_REFUND")


# --- F1: Overview's "Where it went" card adds up, and Reports has no negative category -----------------------------

def test_the_qa_csv_through_the_import_endpoint_the_page_uses(app, conn):
    """The QA repro, end to end: the file goes in through the two routes the import sheet calls."""
    data = base64.b64encode(F1_CSV).decode()
    s, p = call(app, "POST", "/api/import/preview", {"filename": "f1.csv", "data": data, "sign": "minus_out"})
    assert s == 200 and (p["rows"], p["new"]) == (3, 3)
    s, r = call(app, "POST", "/api/import/commit", {"filename": "f1.csv", "data": data, "sign": "minus_out",
                                                    "targets": {g["label"]: g["target"] for g in p["groups"]}})
    assert s == 200 and (r["new"], r["skipped"]) == (3, 0)
    s, ov = call(app, "GET", "/api/overview?month=2026-11")
    assert s == 200 and (ov["summary"]["mtd"], ov["summary"]["income"]) == (16.50, 1000.0)
    assert all(g["spend"] >= 0 for g in ov["spend_groups"])
    s, sp = call(app, "GET", "/api/reports/spending?month=2026-11")
    assert s == 200 and sp["year"]["refunds_over"] == []


def test_the_overview_where_it_went_card_adds_up(app, conn):
    commit(conn, F1_CSV)
    s, ov = call(app, "GET", "/api/overview?month=2026-11")
    assert s == 200
    groups = ov["spend_groups"]
    assert [g["group"] for g in groups if g["spend"] < 0] == []          # no negative group
    # the card's centre reads `total` (every group) and says "shown of" only when `included` (the positive ones)
    # differs: after the import both are the month's spending, and the headline agrees with them.
    included = sum(g["spend"] for g in groups if g["spend"] > 0)
    total = sum(g["spend"] for g in groups)
    assert total == pytest.approx(ov["summary"]["mtd"], abs=0.01)
    assert included == pytest.approx(total, abs=0.01)
    assert ov["summary"]["mtd"] == pytest.approx(16.50, abs=0.01)
    assert ov["summary"]["income"] == pytest.approx(1000.0, abs=0.01)


def test_reports_spending_has_no_negative_category(app, conn):
    commit(conn, F1_CSV)
    s, sp = call(app, "GET", "/api/reports/spending?month=2026-11")
    assert s == 200
    year = sp["year"]
    assert year["refunds_over"] == []                                    # "Refunds over spending . Other" is gone
    assert [c["category"] for c in year["categories"] if c["net"] < 0] == []
    other = next(c for c in year["categories"] if c["category"] == "Other")
    assert (other["gross"], other["refunds"], other["net"]) == (16.50, 0.0, 16.50)
    cells = [m for c in year["categories"] for m in c["months"] if not m["nodata"]]
    assert [m for m in cells if m["net"] < 0] == []
    variable = year["kpis"]["most_variable"]     # "Most variable: Other -$2,690 to $80" is gone with the code
    assert variable is None or variable["from"] >= 0
    s, cf = call(app, "GET", "/api/reports/cashflow?month=2026-11")
    assert s == 200 and cf["refunds_over"] == []
    assert cf["income"]["total"] == pytest.approx(1000.0, abs=0.01)
    assert cf["spending"]["net"] == pytest.approx(16.50, abs=0.01)

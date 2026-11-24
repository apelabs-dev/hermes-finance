"""PDF statement import: number/date tokens, the generic layout, replay, and every refusal code."""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal

import pytest

from hermes_finance import importfile
from hermes_finance.pdfimport import PdfRefused, layout, parse_statement, templates, words
from hermes_finance.pdfimport.numbers import parse_date_prefix, parse_money
from hermes_finance.pdfimport.replay import replay

from pdf_fixtures import blank_page, credit_card, pdf, plain, statement, two_accounts

GOOD = [("05/03/2024", "Coffee", "10.00", "", "90.00"),
        ("14/03/2024", "Salary", "", "50.00", "140.00"),
        ("20/03/2024", "Rent", "40.00", "", "100.00")]
TEMPLATE_CFG = {"header_words": ["date", "description", "debit", "credit", "balance"],
                "columns": [{"role": "date", "x0": 40, "x1": 70}, {"role": "desc", "x0": 120, "x1": 200},
                            {"role": "debit", "x0": 300, "x1": 330}, {"role": "credit", "x0": 380, "x1": 410},
                            {"role": "balance", "x0": 460, "x1": 495}]}


def refused(conn, raw, code, **kw):
    with pytest.raises(PdfRefused) as caught:
        parse_statement(conn, raw, today=date(2026, 9, 28), **kw)
    assert caught.value.code == code
    assert caught.value.message and isinstance(caught.value.message, str)
    return caught.value


# ---------------------------------------------------------------- numbers

@pytest.mark.parametrize("text,value,explicit", [
    ("1,234.56", "1234.56", None), ("1.234,56", "1234.56", None), ("1 234,56", "1234.56", None),
    ("1'234.56", "1234.56", None), ("12,34,567.89", "1234567.89", None), ("$1,234.56", "1234.56", None),
    ("-1,234.56", "-1234.56", "neg"), ("(12.00)", "-12.00", "neg"), ("12.00-", "-12.00", "neg"),
    ("12.00 CR", "12.00", "pos"), ("12.00DR", "-12.00", "neg"), ("+5.00", "5.00", "pos"), ("€ 7,50", "7.50", None),
    ("4,425.36", "4425.36", None), ("\u22125.00", "-5.00", "neg"),
])
def test_money_formats(text, value, explicit):
    m = parse_money(text)
    assert m is not None and m.value == Decimal(value) and m.explicit == explicit


@pytest.mark.parametrize("text", ["1,23.45", "12.5", "1.234.56", "abc", "(12.00", "12.00)", "2024", "1,234"])
def test_not_money(text):
    assert parse_money(text) is None


def test_text_month_dates():
    raw, rest = parse_date_prefix("Jul 9, 2024 Interest")
    assert (raw.kind, raw.a, raw.b, raw.year) == ("text", 9, 7, 2024) and rest.strip() == "Interest"
    raw, rest = parse_date_prefix("31. Januar 2024")
    assert (raw.a, raw.b, raw.year) == (31, 1, 2024)


# ---------------------------------------------------------------- refusals

def test_pdf_unreadable(conn):
    refused(conn, b"not a pdf at all", "pdf_unreadable")


def test_too_big(conn):
    raw = importfile.PDF_MAGIC + b"0" * importfile.MAX_BYTES
    refused(conn, raw, "too_big")


def test_password_required_and_wrong(conn):
    raw = statement(GOOD, password="synthetic-password")
    e = refused(conn, raw, "password_required")
    assert e.field == "password"
    e = refused(conn, raw, "password_wrong", password="not-it")
    assert e.field == "password"


def test_password_ok(conn):
    st = parse_statement(conn, statement(GOOD, password="synthetic-password"), password="synthetic-password",
                         today=date(2026, 9, 28))
    assert [r.amount for r in st.rows] == [10.0, -50.0, 40.0]


def test_no_text_layer(conn):
    refused(conn, blank_page(), "no_text_layer")


def test_credit_card(conn):
    refused(conn, credit_card(), "credit_card")


def test_multi_account(conn):
    refused(conn, two_accounts(), "multi_account")


def test_no_header(conn):
    raw = statement(GOOD, header=None)
    e = refused(conn, raw, "no_header")
    assert "header_words" in e.extra["detail"]


def test_no_rows(conn):
    raw = statement([], opening="Opening balance 100.00", closing="Closing balance 100.00")
    refused(conn, raw, "no_rows")


def test_no_balances(conn):
    rows = [("05/03/2024", "Coffee", "10.00", "", ""), ("14/03/2024", "Salary", "", "50.00", "")]
    raw = statement(rows, opening=None, closing=None)
    e = refused(conn, raw, "no_balances")
    assert e.extra["detail"]["columns"]


def test_does_not_reconcile_and_returns_no_rows(conn):
    rows = GOOD[:2] + [("20/03/2024", "Rent", "40.00", "", "99.00")]
    e = refused(conn, statement(rows), "does_not_reconcile")
    assert not hasattr(e, "rows")
    assert e.extra["detail"]["replay"] == "does_not_reconcile"
    assert e.extra["detail"]["first_bad_row"] == 2


def test_unreadable_date(conn):
    rows = GOOD[:1] + [("Xx2y0", "Salary", "", "50.00", "140.00")] + GOOD[2:]
    refused(conn, statement(rows), "unreadable_date")


def test_no_period(conn):
    rows = [("05/03", "Coffee", "10.00", "", "90.00"), ("14/03", "Salary", "", "50.00", "140.00")]
    refused(conn, statement(rows, period=None), "no_period")


NO_YEAR_ROWS = [("05/06", "Coffee", "10.00", "", "90.00"),
                ("14/06", "Salary", "", "50.00", "140.00"),
                ("20/06", "Rent", "40.00", "", "100.00")]
NO_YEAR_PERIOD = "Statement period 1 June to 30 June"


def test_a_printed_period_without_a_year_is_refused_not_invented(conn):
    e = refused(conn, statement(NO_YEAR_ROWS, period=NO_YEAR_PERIOD), "no_period")
    assert e.extra.get("needs_year") is True
    assert "year" in e.message.lower() and "type" in e.message.lower()
    assert not hasattr(e, "rows")
    assert "rows" not in e.extra and "rows" not in e.extra.get("detail", {})


def test_a_typed_year_imports_a_printed_period_without_one(conn):
    st = parse_statement(conn, statement(NO_YEAR_ROWS, period=NO_YEAR_PERIOD), year=2025,
                         today=date(2026, 9, 28))
    assert [r.date for r in st.rows] == ["2025-06-05", "2025-06-14", "2025-06-20"]
    assert st.period == (date(2025, 6, 1), date(2025, 6, 30))


def test_a_period_year_covers_rows_that_print_none(conn):
    rows = [("05/03", "Coffee", "10.00", "", "90.00"), ("14/03", "Salary", "", "50.00", "140.00"),
            ("20/03", "Rent", "40.00", "", "100.00")]
    st = parse_statement(conn, statement(rows, period="Statement period 01/03/2024 to 31/03/2024"),
                         today=date(2026, 9, 28))
    assert [r.date for r in st.rows] == ["2024-03-05", "2024-03-14", "2024-03-20"]
    assert st.detail["year_placeholder"] is True      # set here too: never use it as the refusal test


def test_year_fixes_no_year_statement(conn):
    rows = [("05/03", "Coffee", "10.00", "", "90.00"), ("14/03", "Salary", "", "50.00", "140.00"),
            ("20/03", "Rent", "40.00", "", "100.00")]
    raw = statement(rows, period=None)
    st = parse_statement(conn, raw, year=2024, today=date(2026, 9, 28))
    assert [r.date for r in st.rows] == ["2024-03-05", "2024-03-14", "2024-03-20"]
    assert st.period == (date(2024, 3, 5), date(2024, 3, 20))


def test_template_invalid(conn):
    e = refused(conn, plain(GOOD), "template_invalid", template={"columns": "nope"})
    assert e.extra["detail"]["reason"]
    row = [("05/03/2024", "Coffee", "10.00", "", "90.00")]
    refused(conn, statement(row, period="no period here"), "template_invalid",
            template={"header_words": ["date", "not-on-this-page"],
                      "columns": [{"role": "date", "x0": 40, "x1": 60},
                                  {"role": "amount", "x0": 300, "x1": 340}]})


def test_missing_extra(conn, monkeypatch):
    monkeypatch.setitem(sys.modules, "pdfplumber", None)
    e = refused(conn, plain(GOOD), "missing_extra")
    assert "pip install" in e.message


def test_clean_config_accepts_table_end_y_and_rejects_bad_values():
    cfg = templates.clean_config({**TEMPLATE_CFG, "table_end_y": 400.5})
    assert isinstance(cfg["table_end_y"], float) and cfg["table_end_y"] == 400.5
    with pytest.raises(ValueError, match="number"):
        templates.clean_config({**TEMPLATE_CFG, "table_end_y": "400.5"})
    with pytest.raises(ValueError, match="out of range"):
        templates.clean_config({**TEMPLATE_CFG, "table_end_y": 10000.5})


def test_table_end_y_stops_the_table_before_a_printed_footer():
    raw = statement(GOOD, extra=[(40, 116, "Service fee"), (300, 116, "225.00")])
    pages, sizes = words.read(raw)
    footer = next(ln for ln in words.build_lines(pages) if "Service fee" in ln.text)

    without = layout.parse(pages, sizes)
    assert len(without.rows) == 4 and "unreadable_date" in without.problems
    assert without.rows[-1].desc == ["Service fee"] and without.rows[-1].amount == Decimal("-225.00")

    stopped = layout.parse(pages, sizes, {"table_end_y": footer.top})
    assert len(stopped.rows) == 3 and stopped.problems == []
    assert [r.amount for r in stopped.rows] == [Decimal("-10.00"), Decimal("50.00"), Decimal("-40.00")]
    assert stopped.closing == Decimal("100.00")


def test_is_pdf_tolerates_a_short_preamble_but_not_text():
    assert importfile.is_pdf(b"%PDF-1.7")
    assert importfile.is_pdf(b"\n\n%PDF-1.7")
    assert importfile.is_pdf(b"\xef\xbb\xbf%PDF-1.7")
    assert not importfile.is_pdf(b"JOBID-42\n" + importfile.PDF_MAGIC + b"1.7\n")
    assert not importfile.is_pdf(b"Date,Description,Amount\n09/01/2026,X,-1.00\n")

    not_a_pdf = b"a,b\n%PDF- inside\n"
    assert not importfile.is_pdf(not_a_pdf)
    with pytest.raises(importfile.ImportFileError) as caught:
        importfile.sniff("x.csv", not_a_pdf)
    assert "PDF" not in caught.value.message
    assert importfile.sniff("x.csv", b"a,b\n09/01/2026,%PDF- inside\n") == "csv"


# ---------------------------------------------------------------- formats and dates

def test_decimal_comma_and_trailing_minus(conn):
    rows = [("05.03.2024", "Coffee", "10,00", "", "90,00"),
            ("14.03.2024", "Salary", "", "50,00", "140,00"),
            ("20.03.2024", "Rent", "40,00", "", "100,00")]
    st = parse_statement(conn, statement(rows, period="Kontoauszug 01.03.2024 bis 31.03.2024"),
                         today=date(2026, 9, 28))
    assert [r.amount for r in st.rows] == [10.0, -50.0, 40.0]

    rows = [("05.03.2024", "Coffee", "10,00-", "", "90,00"),
            ("14.03.2024", "Salary", "", "50,00", "140,00"),
            ("20.03.2024", "Rent", "40,00-", "", "100,00")]
    st = parse_statement(conn, statement(rows, period="Kontoauszug 01.03.2024 bis 31.03.2024"),
                         today=date(2026, 9, 28))
    assert [r.amount for r in st.rows] == [10.0, -50.0, 40.0]


def test_parentheses_are_money_out(conn):
    rows = [("05/03/2024", "Coffee", "(10.00)", "", "90.00"),
            ("14/03/2024", "Salary", "", "50.00", "140.00"),
            ("20/03/2024", "Rent", "(40.00)", "", "100.00")]
    st = parse_statement(conn, plain(rows), today=date(2026, 9, 28))
    assert [r.amount for r in st.rows] == [10.0, -50.0, 40.0]


def test_cr_dr_suffixes(conn):
    header = ("Date", "Description", "Amount", "Balance")
    rows = [("05/03/2024", "Coffee", "10.00 DR", "90.00"),
            ("14/03/2024", "Salary", "50.00 CR", "140.00"),
            ("20/03/2024", "Rent", "40.00 DR", "100.00")]
    st = parse_statement(conn, statement(rows, header=header, columns=(40, 120, 300, 460)),
                         today=date(2026, 9, 28))
    assert [r.amount for r in st.rows] == [10.0, -50.0, 40.0]


def test_ambiguous_date_order(conn):
    rows = [("01/02/2024", "A", "10.00", "", "90.00"), ("02/02/2024", "B", "", "50.00", "140.00")]
    refused(conn, statement(rows, period="Statement period 01/01/2024 to 02/02/2024"), "unreadable_date")


def test_dot_separator_is_day_first(conn):
    rows = [("01.02.2024", "A", "10.00", "", "90.00"), ("02.02.2024", "B", "", "50.00", "140.00")]
    st = parse_statement(conn, statement(rows, period="Statement period 01/01/2024 to 28/02/2024"),
                         today=date(2026, 9, 28))
    assert [r.date for r in st.rows] == ["2024-02-01", "2024-02-02"]


def test_inherited_date_on_blank_cell(conn):
    rows = [("05/03/2024", "Coffee", "10.00", "", "90.00"),
            ("", "More coffee", "5.00", "", "85.00"),
            ("20/03/2024", "Rent", "40.00", "", "45.00")]
    st = parse_statement(conn, plain(rows), today=date(2026, 9, 28))
    assert [r.date for r in st.rows] == ["2024-03-05", "2024-03-05", "2024-03-20"]


def test_newest_first_replays_bottom_up(conn):
    rows = [("20/03/2024", "Rent", "40.00", "", "100.00"),
            ("14/03/2024", "Salary", "", "50.00", "140.00"),
            ("05/03/2024", "Coffee", "10.00", "", "90.00")]
    st = parse_statement(conn, statement(rows, closing="Closing balance 100.00"), today=date(2026, 9, 28))
    assert st.balance_mode == "running"
    assert [r.amount for r in st.rows] == [40.0, -50.0, 10.0]


def test_user_typed_opening_anchor(conn):
    raw = statement(GOOD, opening="Opening balance 1OO.OO")
    refused(conn, raw, "no_balances")
    st = parse_statement(conn, raw, opening="100.00", today=date(2026, 9, 28))
    assert st.opening == Decimal("100.00")
    refused(conn, raw, "does_not_reconcile", opening="99.00")


def test_user_typed_closing_anchor(conn):
    raw = statement(GOOD, closing="Closing balance 999.00")
    refused(conn, raw, "does_not_reconcile")
    st = parse_statement(conn, raw, closing="100.00", today=date(2026, 9, 28))
    assert st.closing == Decimal("100.00")


def test_period_from_text_months(conn):
    rows = [("5 Mar 2024", "Coffee", "10.00", "", "90.00"),
            ("14 Mar 2024", "Salary", "", "50.00", "140.00")]
    raw = statement(rows, period="Statement period 1 March 2024 to 31 March 2024")
    st = parse_statement(conn, raw, today=date(2026, 9, 28))
    assert [r.date for r in st.rows] == ["2024-03-05", "2024-03-14"]


def test_multiline_description(conn):
    rows = [("05/03/2024", "Coffee and\ncake", "10.00", "", "90.00"),
            ("14/03/2024", "Salary", "", "50.00", "140.00")]
    st = parse_statement(conn, plain(rows), today=date(2026, 9, 28))
    assert "Coffee and cake" in st.rows[0].description


def test_replay_function_direct():
    class R:
        def __init__(self, amount, balance, d=date(2024, 3, 1)):
            self.amount = Decimal(amount) if amount is not None else None
            self.balance = Decimal(balance) if balance is not None else None
            self.date = d

    bad = [R("-10.00", "90.00"), R("-50.00", "140.00")]
    v = replay(bad, Decimal("100.00"), Decimal("140.00"))
    assert not v.accepted and v.first_bad_row_index == 1
    assert replay([R("-10.00", None), R("50.00", None)], Decimal("100.00"), Decimal("140.00")).accepted
    assert replay([R("-10.00", None), R("50.00", None)], Decimal("100.00"), Decimal("141.00")).reason == \
        "does_not_reconcile"
    assert replay([], Decimal("1.00"), Decimal("1.00")).reason == "no_rows"

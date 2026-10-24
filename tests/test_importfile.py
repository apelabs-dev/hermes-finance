"""importfile: sniffing a bank's downloaded file, the CSV preview / mapping, and OFX / QFX statements.
Pure parsing: no database, no network. Fixtures in tests/fixtures/import/ are synthetic."""

from __future__ import annotations

import io
import math
from datetime import date
from pathlib import Path

import pytest
from ofxtools.Parser import OFXTree
from ofxtools.header import OFXHeaderError

from hermes_finance import importfile
from hermes_finance.importfile import (MAX_BYTES, SAMPLE_ROWS, ImportFileError, Row, csv_preview, csv_rows,
                                       normalize_description, ofx_statements, sniff)

FIXTURES = Path(__file__).parent / "fixtures" / "import"
TODAY = date(2026, 9, 28)
NO_GUESS = dict.fromkeys(importfile.FIELDS)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def csv_bytes(*lines: str, delimiter: str = ",") -> bytes:
    return "\n".join(lines).replace(",", delimiter).encode() + b"\n"


def rows_of(data: bytes, mapping: dict, date_format: str = "%m/%d/%Y", sign: str = "minus_out",
            today: date = TODAY) -> list[Row]:
    return csv_rows(data, mapping, date_format, sign, today=today)


BASIC = {**NO_GUESS, "date": 0, "description": 1, "amount": 2}


# --- sniff --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, kind", [
    ("everyday-export.csv", "csv"), ("card-export.csv", "csv"), ("split-columns.csv", "csv"),
    ("ambiguous-dates.csv", "csv"), ("checking.ofx", "ofx"), ("card.qfx", "ofx"), ("checking-v2.ofx", "ofx"),
    ("broken-header.qfx", "ofx"),
])
def test_sniff_fixtures(name, kind):
    assert sniff(name, fixture(name)) == kind


@pytest.mark.parametrize("filename, name, kind", [
    ("statement.csv", "checking.ofx", "ofx"),        # content first: an OFX file named .csv is OFX
    ("download.txt", "card.qfx", "ofx"),
    ("export", "checking-v2.ofx", "ofx"),            # no extension at all
    ("export.dat", "broken-header.qfx", "ofx"),
    ("transactions.txt", "everyday-export.csv", "csv"),
    ("transactions.ofx", "card-export.csv", "csv"),  # a CSV named .ofx is still a CSV
    ("", "split-columns.csv", "csv"),
])
def test_sniff_goes_by_content(filename, name, kind):
    assert sniff(filename, fixture(name)) == kind


def test_sniff_uses_the_extension_second():
    """An OFX body far down the file (no header near the start) counts as OFX only when the name says so."""
    data = b" \n" * 3000 + fixture("checking.ofx").split(b"\n\n", 1)[1]
    assert sniff("bank.qfx", data) == "ofx"
    with pytest.raises(ImportFileError, match="doesn't look like a bank export"):
        sniff("bank.txt", data)


def test_sniff_utf16_excel_unicode_text():
    """Excel's 'Unicode text' export: UTF-16 with a BOM, tab-delimited. NULs, but text."""
    text = "Date\tDescription\tAmount\r\n09/01/2026\tTRADER JOE'S #552\t-64.18\r\n09/02/2026\tPAYROLL ACME CO\t2450.00\r\n"
    data = text.encode("utf-16")
    assert b"\x00" in data
    assert sniff("export.txt", data) == "csv"
    preview = csv_preview(data, today=TODAY)
    assert preview.delimiter == "\t" and preview.row_count == 2
    assert [r.amount for r in rows_of(data, preview.guess)] == [64.18, -2450.0]


XLSX = b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 22 + b"[Content_Types].xml" + b"\x00" * 40 + b"xl/workbook.xml"


@pytest.mark.parametrize("filename, data, message", [
    ("empty.csv", b"", "empty"),
    ("blank.csv", b"  \r\n\t\n ", "empty"),
    ("bom.csv", b"\xef\xbb\xbf", "empty"),
    ("huge.csv", b"a" * (MAX_BYTES + 1), "up to 10 MB"),
    ("nul.csv", b"Date,Description,Amount\n09/01/2026,TRADER JOE\x00S,-1.00\n", "isn't text"),
    ("random.bin", bytes(range(256)) * 8, "isn't text"),
    ("controls.csv", bytes(range(1, 9)) * 50 + b"\x80\x81\x8d\x8f\x90\x9d" * 50, "isn't text"),
    ("image.png", b"\x89PNG\r\n\x1a\n" + b"\x10" * 64, "isn't text"),
    ("statement.pdf", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\n", "PDF"),
    ("statement.csv", b"%PDF-1.4\nnot really a csv\n", "PDF"),
    ("export.xlsx", XLSX, "Save it as CSV first"),
    ("export.zip", XLSX, "Save it as CSV first"),       # an .xlsx renamed: still a workbook
    ("export.xlsx", b"PK\x03\x04" + b"\x00" * 30, "Save it as CSV first"),
    ("archive.zip", b"PK\x03\x04\x14\x00" + b"\x00" * 24 + b"statement.csv", "ZIP"),
    ("export.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504, "Save it as CSV first"),
    ("export.xls", b"<html><table><tr><td>09/01/2026</td><td>-1.00</td></tr></table></html>", "Save it as CSV first"),
    ("export.xls", b"Date,Description,Amount\n09/01/2026,X,-1.00\n", "Save it as CSV first"),
    ("letter.txt", b"Dear customer,\nThank you for banking with us.\nYour statement is ready online.\n", "bank export"),
    ("page.csv", b"<!DOCTYPE html>\n<html><body>Session expired, please sign in again</body></html>\n", "bank export"),
    ("data.json", b'[{"date": "2026-09-01", "amount": -1.0},\n {"date": "2026-09-02", "amount": 2.0}]\n', "bank export"),
    ("stock.csv", b"name,qty\napple,3\npear,5\n", "bank export"),       # CSV-shaped, but no dates in it
    ("header-only.csv", b"Date,Description,Amount\n", "no transactions"),
    ("money.qif", b"!Type:Bank\nD09/01/2026\nT-64.18\nPTRADER JOE'S #552\n^\n", "QIF"),
    ("bank.qfx", b"This is not OFX at all\njust some words\n", "doesn't look like OFX"),
])
def test_sniff_rejections(filename, data, message):
    with pytest.raises(ImportFileError, match=message) as err:
        sniff(filename, data)
    assert isinstance(err.value, ValueError)
    assert err.value.field is None and err.value.row is None
    assert "Traceback" not in str(err.value) and str(err.value) == err.value.message


def test_parsers_refuse_what_sniff_refuses():
    for parse in (csv_preview, ofx_statements):
        with pytest.raises(ImportFileError, match="empty"):
            parse(b"")
        with pytest.raises(ImportFileError, match="up to 10 MB"):
            parse(b"x" * (MAX_BYTES + 1))
        with pytest.raises(ImportFileError, match="PDF"):
            parse(b"%PDF-1.7\n")
    with pytest.raises(ImportFileError, match="bank export"):
        csv_preview(b"just one line of words\n")
    with pytest.raises(ImportFileError, match="empty"):
        csv_rows(b"", BASIC, "%m/%d/%Y", "minus_out")
    with pytest.raises(ImportFileError, match="no transactions"):
        csv_rows(b"Date,Description,Amount\n", BASIC, "%m/%d/%Y", "minus_out")
    with pytest.raises(ImportFileError, match="no transactions"):
        csv_preview(b"Date,Description,Amount\n\nDate,Description,Amount\n")


# --- CSV preview --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("everyday-export.csv", dict(
        headers=["Posted Date", "Description", "Amount", "Account"], delimiter=",", row_count=37,
        guess={**NO_GUESS, "date": 0, "description": 1, "amount": 2, "account": 3},
        date_format="%m/%d/%Y", date_formats=["%m/%d/%Y"], sign="minus_out",
        accounts=[("Everyday Checking", 20), ("Rewards Card", 17)])),
    ("card-export.csv", dict(
        headers=["Transaction Date", "Post Date", "Description", "Category", "Amount"], delimiter=",", row_count=12,
        guess={**NO_GUESS, "date": 1, "description": 2, "amount": 4},       # the posted date, like OFX's DTPOSTED
        date_format="%Y-%m-%d", date_formats=["%Y-%m-%d"], sign="minus_in", accounts=[])),
    ("split-columns.csv", dict(
        headers=["Date", "Description", "Debit", "Credit", "Balance"], delimiter=";", row_count=10,
        guess={**NO_GUESS, "date": 0, "description": 1, "debit": 2, "credit": 3},    # never the Balance
        date_format="%d/%m/%Y", date_formats=["%d/%m/%Y"], sign="split", accounts=[])),
    ("ambiguous-dates.csv", dict(
        headers=["Date", "Description", "Amount"], delimiter=",", row_count=8,
        guess={**NO_GUESS, "date": 0, "description": 1, "amount": 2},
        date_format="%m/%d/%Y", date_formats=["%m/%d/%Y", "%d/%m/%Y"], sign="minus_out", accounts=[])),
])
def test_csv_preview_fixtures(name, expected):
    preview = csv_preview(fixture(name), today=TODAY)
    for key, value in expected.items():
        assert getattr(preview, key) == value, key
    assert len(preview.sample) == SAMPLE_ROWS
    assert all(len(cells) == len(preview.headers) for cells in preview.sample)


def test_csv_preview_sample_is_text_cells():
    preview = csv_preview(fixture("everyday-export.csv"), today=TODAY)
    assert preview.headers[0] == "Posted Date"                      # the BOM is gone
    assert preview.sample[1] == ["09/25/2026", "PAYROLL ACME CO, DIRECT DEP", "2,450.00", "Everyday Checking"]
    split = csv_preview(fixture("split-columns.csv"), today=TODAY)
    assert split.sample[0] == ["25/09/2026", "TRADER JOE'S #552", "64,18", "", "3.120,44"]


def test_csv_preview_skips_blank_and_repeated_header_lines():
    data = csv_bytes("Date,Description,Amount", "", "09/01/2026,TRADER JOE'S #552,-64.18", "",
                     "Date,Description,Amount", "09/02/2026,PAYROLL ACME CO,2450.00", "Total,,", "")
    preview = csv_preview(data, today=TODAY)
    assert preview.row_count == 2
    assert [r.line for r in rows_of(data, preview.guess)] == [1, 2]


def test_csv_preview_skips_a_summary_preamble():
    """A bank that puts an account summary above the transactions (fewer columns than the table)."""
    data = csv_bytes('Description,,Summary Amt.', 'Beginning balance as of 09/01/2026,,"1,000.00"',
                     'Total credits,,"0.00"', 'Total debits,,"-79.67"', 'Ending balance as of 09/26/2026,,"920.33"', "",
                     "Date,Description,Amount,Running Bal.",
                     '09/01/2026,Beginning balance as of 09/01/2026,,"1,000.00"',
                     "09/02/2026,TRADER JOE'S #552,-64.18,935.82", "09/03/2026,NETFLIX.COM,-15.49,920.33")
    preview = csv_preview(data, today=TODAY)
    assert preview.headers == ["Date", "Description", "Amount", "Running Bal."]
    assert preview.row_count == 3
    assert preview.guess == {**NO_GUESS, "date": 0, "description": 1, "amount": 2}
    assert preview.sign == "minus_out" and preview.date_format == "%m/%d/%Y"
    assert [(r.line, r.amount) for r in rows_of(data, preview.guess)] == [(2, 64.18), (3, 15.49)]


def test_csv_preview_headerless_file_is_guessed_by_content():
    data = csv_bytes('"09/25/2026","-64.18","*","","TRADER JOE\'S #552"',
                     '"09/24/2026","2450.00","*","","PAYROLL ACME CO"',
                     '"09/23/2026","-15.49","*","1042","NETFLIX.COM"')
    preview = csv_preview(data, today=TODAY)
    assert preview.headers == ["Column 1", "Column 2", "Column 3", "Column 4", "Column 5"]
    assert preview.row_count == 3                       # the first line is a transaction, not a header
    assert preview.guess == {**NO_GUESS, "date": 0, "amount": 1, "description": 4}
    assert preview.date_format == "%m/%d/%Y" and preview.sign == "minus_out"


def test_csv_preview_header_wider_than_the_rows():
    """A header naming a last column (Notes) that the rows leave off entirely."""
    data = csv_bytes("Date,Description,Amount,Notes", "09/01/2026,TRADER JOE'S #552,-64.18", "09/02/2026,NETFLIX.COM,-15.49")
    preview = csv_preview(data, today=TODAY)
    assert preview.headers == ["Date", "Description", "Amount", "Notes"] and preview.row_count == 2
    assert preview.sample[0] == ["09/01/2026", "TRADER JOE'S #552", "-64.18", ""]
    assert [r.amount for r in rows_of(data, preview.guess)] == [64.18, 15.49]


def test_csv_preview_trailing_delimiter_and_sep_line():
    data = b"sep=;\nDate;Description;Amount\n01/09/2026;SHELL OIL 57412;-39,88;\n02/09/2026;PAYROLL ACME CO;2.450,00;\n"
    preview = csv_preview(data, today=TODAY)
    assert preview.delimiter == ";" and preview.headers == ["Date", "Description", "Amount"]
    assert preview.date_formats == ["%d/%m/%Y", "%m/%d/%Y"]         # day first in a ';' file
    assert [r.amount for r in rows_of(data, preview.guess, preview.date_format)] == [39.88, -2450.0]


def test_csv_preview_drops_formats_that_put_dates_in_the_future():
    data = csv_bytes("Date,Description,Amount", "10/09/2026,TRADER JOE'S #552,-64.18", "03/09/2026,NETFLIX.COM,-15.49")
    assert csv_preview(data, today=TODAY).date_formats == ["%d/%m/%Y"]          # Oct 9 hasn't happened yet
    assert csv_preview(data, today=date(2026, 12, 1)).date_formats == ["%m/%d/%Y", "%d/%m/%Y"]


@pytest.mark.parametrize("header, cells, fmt", [
    ("Date", ["2026-09-26T08:15:00Z", "2026-09-25 17:40"], "%Y-%m-%d"),
    ("Date", ["26.09.2026", "25.09.2026"], "%d.%m.%Y"),
    ("Date", ["20260926", "20260925"], "%Y%m%d"),
    ("Date", ['"Sep 26, 2026"', '"Sep 25, 2026"'], "%b %d, %Y"),
    ("Date", ["26-Sep-26", "25-Sep-26"], "%d-%b-%y"),
    ("Trans. Date", ["09/26/26", "09/25/26"], "%m/%d/%y"),
])
def test_csv_preview_date_formats(header, cells, fmt):
    data = csv_bytes(f"{header},Description,Amount", *(f"{c},TRADER JOE'S #552,-1.00" for c in cells))
    preview = csv_preview(data, today=TODAY)
    assert preview.guess["date"] == 0 and preview.date_format == fmt
    assert rows_of(data, preview.guess, fmt)[0].date == "2026-09-26"


@pytest.mark.parametrize("amounts, sign", [
    (["-64.18", "-15.49", "2450.00", "-8.75"], "minus_out"),     # a bank: purchases minus
    (["64.18", "15.49", "-512.34", "8.75"], "minus_in"),         # a card: charges plus
])
def test_csv_preview_sign_by_majority(amounts, sign):
    names = ["TRADER JOE'S #552", "NETFLIX.COM", "SOMETHING", "CORNER CAFE"]
    lines = [f"09/0{i + 1}/2026,{n},{a}" for i, (n, a) in enumerate(zip(names, amounts, strict=True))]
    data = csv_bytes("Date,Description,Amount", *lines)
    assert csv_preview(data, today=TODAY).sign == sign


def test_csv_preview_sign_by_money_in_words():
    """A savings export that is mostly deposits: the payroll's sign decides, not the majority."""
    data = csv_bytes("Date,Description,Amount", "09/01/2026,PAYROLL ACME CO,2450.00", "09/02/2026,TRANSFER IN,500.00",
                     "09/03/2026,TRANSFER IN,300.00", "09/04/2026,TRANSFER OUT,-100.00")
    assert csv_preview(data, today=TODAY).sign == "minus_out"
    card = csv_bytes("Date,Description,Amount", "09/01/2026,PAYMENT - THANK YOU,-512.34", "09/02/2026,X,-1.00",
                     "09/03/2026,Y,-2.00")
    assert csv_preview(card, today=TODAY).sign == "minus_in"


@pytest.mark.parametrize("headers, expected", [
    ("Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit",
     {"date": 1, "account": 2, "description": 3, "debit": 5, "credit": 6}),
    ("Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #",
     {"date": 1, "description": 2, "amount": 3}),
    ("Date,Payee,Withdrawal (USD),Deposit (USD),Account Name",
     {"date": 0, "description": 1, "debit": 2, "credit": 3, "account": 4}),
    ("Status,Date,Memo,Amount (EUR)", {"date": 1, "description": 2, "amount": 3}),
])
def test_csv_preview_header_names(headers, expected):
    width = len(headers.split(","))
    row = ["09/01/2026", "TRADER JOE'S #552", "1.00", "2.00"] + ["x"] * (width - 4)
    guess = csv_preview(csv_bytes(headers, ",".join(row)), today=TODAY).guess
    assert guess == {**NO_GUESS, **expected}


# --- CSV rows -----------------------------------------------------------------------------------------------------

def test_csv_rows_minus_out_everyday_export():
    rows = rows_of(fixture("everyday-export.csv"), {**NO_GUESS, "date": 0, "description": 1, "amount": 2, "account": 3})
    assert len(rows) == 37
    assert rows[0] == Row("2026-09-25", "TRADER JOE'S #552", 64.18, "Everyday Checking", None, 1)   # spaces collapsed
    assert rows[1] == Row("2026-09-25", "PAYROLL ACME CO, DIRECT DEP", -2450.0, "Everyday Checking", None, 2)
    by_name = {r.description: r for r in rows}
    assert by_name['SMALL "BATCH" COFFEE'].amount == 4.85
    assert by_name["HILLSIDE PROPERTY MGMT RENT"].amount == 1650.0
    assert by_name["PAYMENT, THANK YOU"].amount == -389.5 and by_name["PAYMENT, THANK YOU"].account == "Rewards Card"
    assert rows[-1].line == 37 and rows[-1].date == "2026-08-20"


def test_csv_rows_minus_in_card_export():
    data = fixture("card-export.csv")
    mapping = {**NO_GUESS, "date": 1, "description": 2, "amount": 4}
    rows = rows_of(data, mapping, "%Y-%m-%d", "minus_in")
    assert [(r.date, r.description, r.amount) for r in rows[:4]] == [
        ("2026-09-25", "SHELL OIL 57412", 41.07), ("2026-09-23", "NETFLIX.COM", 15.49),
        ("2026-09-22", "TRADER JOE'S #552", 64.18), ("2026-09-20", "PAYMENT THANK YOU", -512.34)]
    assert all(r.account is None and r.fitid is None for r in rows)
    flipped = rows_of(data, mapping, "%Y-%m-%d", "minus_out")
    assert [r.amount for r in flipped] == [-r.amount for r in rows]


def test_csv_rows_split_columns():
    rows = rows_of(fixture("split-columns.csv"), {**NO_GUESS, "date": 0, "description": 1, "debit": 2, "credit": 3},
                   "%d/%m/%Y", "split")
    assert [(r.date, r.amount) for r in rows] == [
        ("2026-09-25", 64.18), ("2026-09-24", -2450.0), ("2026-09-22", 58.2), ("2026-09-19", 102.66),
        ("2026-09-15", 1650.0), ("2026-09-13", 39.88), ("2026-09-10", 45.0), ("2026-09-08", -15.49),
        ("2026-09-05", 87.43), ("2026-09-02", -0.42)]
    assert rows[6].description == "NORTHWIND MOBILE; MONTHLY PLAN"


SPLIT = {**NO_GUESS, "date": 0, "description": 1, "debit": 2, "credit": 3}


@pytest.mark.parametrize("debit, credit, amount", [
    ("64.18", "", 64.18),
    ("-64.18", "", 64.18),          # debits written with a minus: still money out
    ("", "2450.00", -2450.0),
    ("", "-2450.00", -2450.0),
    ("64.18", "0.00", 64.18),       # the other cell filled with zero
    ("0.00", "15.49", -15.49),
    ("100.00", "30.00", 70.0),      # both: the net
    ("$1,234.56", "", 1234.56),
    ("", "64.18 DR", 64.18),        # a suffix wins over the column
    ("-", "12.00", -12.0),          # a dash for an empty cell
])
def test_csv_rows_split_cells(debit, credit, amount):
    data = csv_bytes("Date,Description,Debit,Credit", f'09/01/2026,X,"{debit}","{credit}"')
    assert rows_of(data, SPLIT, sign="split")[0].amount == amount


def test_csv_rows_split_with_one_column_mapped():
    data = csv_bytes("Date,Description,Debit,Credit", "09/01/2026,A,64.18,", "09/02/2026,B,,2450.00")
    assert [(r.line, r.amount) for r in rows_of(data, {**SPLIT, "credit": None}, sign="split")] == [(1, 64.18)]
    assert [(r.line, r.amount) for r in rows_of(data, {**SPLIT, "debit": None}, sign="split")] == [(2, -2450.0)]
    # A 'minus' convention with no Amount column mapped still reads the Debit / Credit columns.
    assert [r.amount for r in rows_of(data, SPLIT, sign="minus_in")] == [64.18, -2450.0]


def test_csv_rows_split_bad_cell_names_its_column():
    data = csv_bytes("Date,Description,Debit,Credit", "09/01/2026,A,64.18,", "09/02/2026,B,,1O.00", "09/03/2026,C,,")
    with pytest.raises(ImportFileError, match=r"^Row 2: '1O.00' isn't an amount\.$") as err:
        rows_of(data, SPLIT, sign="split")
    assert (err.value.field, err.value.row) == ("credit", 2)
    assert [r.line for r in rows_of(data[:data.index(b"09/02")] + b"09/03/2026,C,,\n", SPLIT, sign="split")] == [1]


@pytest.mark.parametrize("cell, sign, amount", [
    ('"1,234.56"', "minus_out", -1234.56),
    ('"-1,234.56"', "minus_out", 1234.56),
    ("-64.18", "minus_out", 64.18),
    ("-64.18", "minus_in", -64.18),
    ("64.18", "minus_out", -64.18),
    ("64.18", "minus_in", 64.18),
    ("+15.49", "minus_out", -15.49),
    ("(64.18)", "minus_out", 64.18),           # parentheses = negative
    ("(64.18)", "minus_in", -64.18),
    ('"($1,234.56)"', "minus_out", 1234.56),
    ("$64.18", "minus_in", 64.18),
    ("-$64.18", "minus_out", 64.18),
    ("$-64.18", "minus_out", 64.18),
    ("US$64.18", "minus_in", 64.18),
    ("€64.18", "minus_in", 64.18),
    ("64.18-", "minus_out", 64.18),            # trailing minus
    ("64.18-", "minus_in", -64.18),
    ("\u221264.18", "minus_out", 64.18),        # a typographic minus
    ("USD 12.00", "minus_in", 12.0),           # a stray currency code
    ("12.00 USD", "minus_in", 12.0),
    ("usd -12.00", "minus_out", 12.0),
    (".50", "minus_in", 0.5),
    ("64.185", "minus_in", 64.19),             # rounded to cents, half up
    ("-64.184", "minus_out", 64.18),
    ('"64,18"', "minus_in", 64.18),            # a decimal comma, quoted in a ',' file
    ("1234", "minus_in", 1234.0),
    ('"1 234.56"', "minus_in", 1234.56),
    ("1'234.56", "minus_in", 1234.56),
])
def test_csv_rows_amount_formats(cell, sign, amount):
    data = csv_bytes("Date,Description,Amount") + f"09/01/2026,X,{cell}\n".encode()
    assert rows_of(data, BASIC, sign=sign)[0].amount == amount


@pytest.mark.parametrize("cell, sign, amount", [
    ("64.18 CR", "minus_out", -64.18),         # CR = money in, whatever the convention
    ("64.18 CR", "minus_in", -64.18),
    ("64.18CR", "minus_out", -64.18),
    ("64.18 cr", "minus_in", -64.18),
    ("-64.18 CR", "minus_out", -64.18),
    ("64.18 DR", "minus_out", 64.18),          # DR = money out
    ("64.18 DR", "minus_in", 64.18),
    ("$64.18 Dr.", "minus_out", 64.18),
    ('"1,234.56 DR"', "minus_out", 1234.56),
])
def test_csv_rows_cr_dr_suffixes(cell, sign, amount):
    data = csv_bytes("Date,Description,Amount") + f"09/01/2026,X,{cell}\n".encode()
    assert rows_of(data, BASIC, sign=sign)[0].amount == amount


@pytest.mark.parametrize("cell, sign, amount", [
    ("1.234,56", "minus_in", 1234.56),
    ("-1.234,56", "minus_out", 1234.56),
    ("64,18", "minus_in", 64.18),
    ("-64,18", "minus_out", 64.18),
    ("1.234", "minus_in", 1234.0),             # a thousands dot in a European export
    ("1 234,56", "minus_in", 1234.56),
    ("1,234.56", "minus_in", 1234.56),         # a US amount in a ';' file still reads
    ("64,18 CR", "minus_in", -64.18),
    ("EUR 64,18", "minus_in", 64.18),
    ("64,18 €", "minus_in", 64.18),
])
def test_csv_rows_european_amounts(cell, sign, amount):
    data = f"Date;Description;Amount\n01/09/2026;X;{cell}\n02/09/2026;Y;1,00\n".encode()
    assert rows_of(data, BASIC, "%d/%m/%Y", sign)[0].amount == amount


def test_csv_rows_zero_is_not_negative_zero():
    data = csv_bytes("Date,Description,Amount", "09/01/2026,X,0.00", "09/02/2026,Y,-0.00")
    for row in rows_of(data, BASIC, sign="minus_out"):
        assert row.amount == 0 and math.copysign(1, row.amount) == 1


@pytest.mark.parametrize("cell", ["abc", "12.34.56", "1,2,3", "12 apples", "$", "64.18 XX", "1.2.3,4"])
def test_csv_rows_bad_amount(cell):
    data = csv_bytes("Date,Description,Amount", "09/01/2026,A,-1.00") + f'09/02/2026,B,"{cell}"\n'.encode()
    with pytest.raises(ImportFileError) as err:
        rows_of(data, BASIC)
    assert (err.value.field, err.value.row) == ("amount", 2)
    assert str(err.value) == f"Row 2: '{cell}' isn't an amount."


def test_csv_rows_skip_rows_without_an_amount():
    """A balance line ("Beginning balance as of ...") has a date but no amount: not a transaction, not an error."""
    data = csv_bytes("Date,Description,Amount,Running Bal.", '09/01/2026,Beginning balance as of 09/01/2026,,"1,000.00"',
                     "09/02/2026,TRADER JOE'S #552,-64.18,935.82")
    assert csv_preview(data, today=TODAY).row_count == 2
    assert [(r.line, r.amount) for r in rows_of(data, BASIC)] == [(2, 64.18)]


def test_csv_rows_no_amount_anywhere_is_a_mapping_error():
    data = csv_bytes("Date,Description,Amount,Notes", "09/01/2026,A,-1.00,", "09/02/2026,B,-2.00,")
    with pytest.raises(ImportFileError, match="No row has an amount") as err:
        rows_of(data, {**BASIC, "amount": 3})
    assert (err.value.field, err.value.row) == ("amount", None)
    with pytest.raises(ImportFileError, match="No row has an amount") as err:
        rows_of(data, {**SPLIT, "debit": None, "credit": 3}, sign="split")
    assert err.value.field == "credit"


def test_csv_rows_bad_date_names_the_row_and_format():
    lines = ["Date,Description,Amount"] + [f"09/{d:02d}/2026,X,-1.00" for d in range(1, 12)] + ["31/31/2026,Y,-2.00"]
    with pytest.raises(ImportFileError) as err:
        rows_of(csv_bytes(*lines), BASIC)
    assert str(err.value) == "Row 12: '31/31/2026' isn't a date in MM/DD/YYYY."
    assert (err.value.field, err.value.row) == ("date", 12)


def test_csv_rows_row_numbers_count_data_rows():
    """Blank lines and a repeated header don't count: row 3 is the third transaction."""
    data = csv_bytes("Date,Description,Amount", "09/01/2026,A,-1.00", "", "Date,Description,Amount",
                     "09/02/2026,B,-2.00", "2026-09-03,C,-3.00")
    with pytest.raises(ImportFileError, match=r"^Row 3: '2026-09-03' isn't a date in MM/DD/YYYY\.$"):
        rows_of(data, BASIC)


def test_csv_rows_blank_date():
    with pytest.raises(ImportFileError, match="Row 1: the date is blank") as err:
        rows_of(csv_bytes("Date,Description,Amount", ",A,-1.00"), BASIC)
    assert (err.value.field, err.value.row) == ("date", 1)


@pytest.mark.parametrize("day, ok", [
    ("09/28/2026", True), ("09/29/2026", True),           # today, and one day ahead (time zones)
    ("09/30/2026", False), ("01/15/2027", False),
    ("01/01/1970", True), ("12/31/1969", False), ("06/01/1900", False),
])
def test_csv_rows_date_bounds(day, ok):
    data = csv_bytes("Date,Description,Amount", "09/01/2026,A,-1.00", f"{day},B,-2.00")
    if ok:
        assert rows_of(data, BASIC, today=TODAY)[1].date == date(*map(int, (day[6:], day[:2], day[3:5]))).isoformat()
        return
    with pytest.raises(ImportFileError, match=r"^Row 2: .*(in the future|before 1970)") as err:
        rows_of(data, BASIC, today=TODAY)
    assert (err.value.field, err.value.row) == ("date", 2)


def test_csv_rows_future_bound_follows_today():
    data = csv_bytes("Date,Description,Amount", "10/05/2026,A,-1.00")
    with pytest.raises(ImportFileError, match="in the future"):
        rows_of(data, BASIC, today=TODAY)
    assert rows_of(data, BASIC, today=date(2026, 10, 4))[0].date == "2026-10-05"


@pytest.mark.parametrize("mapping, sign, fmt, field", [
    ({**BASIC, "date": None}, "minus_out", "%m/%d/%Y", "date"),
    ({**BASIC, "description": None}, "minus_out", "%m/%d/%Y", "description"),
    ({**BASIC, "amount": None}, "minus_out", "%m/%d/%Y", "amount"),
    ({**BASIC, "amount": 7}, "minus_out", "%m/%d/%Y", "amount"),              # not a column in the file
    ({**BASIC, "account": -1}, "minus_out", "%m/%d/%Y", "account"),
    ({**BASIC, "date": "0"}, "minus_out", "%m/%d/%Y", "date"),
    (BASIC, "sideways", "%m/%d/%Y", "sign"),
    (BASIC, "split", "%m/%d/%Y", "debit"),                                    # split, but no Debit / Credit
    (BASIC, "minus_out", "", "date"),
    ({"date": 0, "description": 1}, "minus_out", "%m/%d/%Y", "amount"),       # keys may be missing, not just None
])
def test_csv_rows_mapping_errors(mapping, sign, fmt, field):
    with pytest.raises(ImportFileError) as err:
        csv_rows(csv_bytes("Date,Description,Amount", "09/01/2026,A,-1.00"), mapping, fmt, sign, today=TODAY)
    assert err.value.field == field and err.value.row is None


def test_csv_rows_account_column():
    data = csv_bytes("Date,Description,Amount,Account", "09/01/2026,A,-1.00,Everyday  Checking",
                     "09/02/2026,B,-2.00,")
    rows = rows_of(data, {**BASIC, "account": 3})
    assert [r.account for r in rows] == ["Everyday Checking", None]


def test_csv_rows_cp1252_and_multiline_cells():
    data = "Date,Description,Amount\r\n09/01/2026,\"CAFÉ LUNA\r\nTABLE 4\",-8.75\r\n".encode("cp1252")
    assert rows_of(data, BASIC)[0].description == "CAFÉ LUNA TABLE 4"


def test_csv_rows_unclosed_quote_is_a_plain_error():
    data = b'Date,Description,Amount\n09/01/2026,"TRADER JOE\'S,-1.00\n' + b"x" * 140000 + b"\n"
    with pytest.raises(ImportFileError, match="quoted cell never ends"):
        csv_preview(data)


# --- normalize_description ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("a, b", [
    ("TRADER JOE'S #552 ", "Trader Joes 552"),
    ("SHELL  OIL\t57412", "shell oil 57412"),
    ("NETFLIX.COM", "netflixcom"),
    ("CITY WATER DEPT.", "  City   Water  Dept "),
    ("PAYROLL - ACME CO", "payroll acme co"),
    ("CAFÉ LUNA", "café luna"),
    ("Straße", "STRASSE"),
])
def test_normalize_description_equivalences(a, b):
    assert normalize_description(a) == normalize_description(b)


@pytest.mark.parametrize("text, expected", [
    ("TRADER JOE'S #552 ", "trader joes 552"),
    ("  PAYMENT, THANK YOU  ", "payment thank you"),
    ("A_B*C", "abc"),
    ("", ""),
    ("***", ""),
])
def test_normalize_description_output(text, expected):
    assert normalize_description(text) == expected


def test_normalize_description_keeps_different_merchants_apart():
    assert normalize_description("TRADER JOE'S #552") != normalize_description("TRADER JOE'S #553")


# --- OFX / QFX ----------------------------------------------------------------------------------------------------

def test_ofx_checking_sgml():
    [st] = ofx_statements(fixture("checking.ofx"))
    assert (st.account_label, st.account_last4, st.kind, st.subtype, st.currency) == (
        "Checking ••1234", "1234", "bank", "checking", "USD")
    assert "9990001234" not in st.account_label
    assert (st.balance, st.balance_date) == (2345.67, "2026-09-26")         # LEDGERBAL, not AVAILBAL
    assert [(r.date, r.description, r.amount, r.fitid) for r in st.rows] == [
        ("2026-09-02", "TRADER JOE'S #552", 64.18, "202609020001"),         # NAME wins over MEMO
        ("2026-09-11", "PAYROLL ACME CO", -2450.0, "202609110001"),         # a deposit: money in
        ("2026-09-14", "SHELL OIL 57412", 39.88, "202609140001"),           # 20260914120000.000[-5:EST]
        ("2026-09-15", "NETFLIX.COM", 15.49, "202609150001"),
        ("2026-09-16", "CITY WATER DEPT", 58.2, "202609160001"),            # PAYEE/NAME
        ("2026-09-18", "ATM WITHDRAWAL 0042", 60.0, "202609180001"),        # MEMO when there's no NAME
        ("2026-09-21", "BLUE RIVER HARDWARE & GARDEN", 33.48, "202609210001"),   # &amp;
        ("2026-09-25", "CAFÉ LUNA REFUND", -8.75, "202609250001"),          # CHARSET:1252, a '+' amount
    ]
    assert [r.line for r in st.rows] == list(range(1, 9))
    assert {r.account for r in st.rows} == {"Checking ••1234"}


def test_ofx_card_qfx():
    data = fixture("card.qfx")
    assert b"\r\n" in data and b"<INTU.BID>" in data
    [st] = ofx_statements(data)
    assert (st.account_label, st.account_last4, st.kind, st.subtype, st.currency) == (
        "Credit card ••9876", "9876", "credit_card", None, "USD")
    assert "0000111122229876" not in st.account_label
    assert (st.balance, st.balance_date) == (1234.56, "2026-09-26")         # owed: positive
    assert [(r.date, r.amount) for r in st.rows] == [
        ("2026-09-03", 41.07), ("2026-09-06", 15.49), ("2026-09-10", 64.18), ("2026-09-14", -24.99),
        ("2026-09-18", 1150.0), ("2026-09-20", -512.34)]
    assert st.rows[3].description == "PIXEL BOOKS & GAMES"
    assert st.rows[5].description == "PAYMENT THANK YOU" and st.rows[5].fitid == "2026092024692160006"


def test_ofx_v2_xml_savings():
    data = fixture("checking-v2.ofx")
    assert data.startswith(b"<?xml") and b'OFXHEADER="200"' in data
    [st] = ofx_statements(data)
    assert (st.account_label, st.account_last4, st.kind, st.subtype) == ("Savings ••4321", "4321", "bank", "savings")
    assert (st.balance, st.balance_date, st.currency) == (10306.51, "2026-09-26", "USD")
    assert [(r.date, r.description, r.amount, r.fitid) for r in st.rows] == [
        ("2026-08-05", "TRANSFER FROM CHECKING", -500.0, "S-20260805-1"),   # its BANKACCTTO isn't the account
        ("2026-08-31", "INTEREST PAID", -3.21, "S-20260831-1"),
        ("2026-09-15", "TRANSFER TO CHECKING", 200.0, "S-20260915-1"),
        ("2026-09-25", "INTEREST PAID", -3.3, "S-20260925-1"),
    ]


def test_ofx_broken_header():
    data = fixture("broken-header.qfx")
    with pytest.raises(OFXHeaderError):     # ofxtools alone refuses it: the tidy-and-retry path is what reads it
        OFXTree().parse(io.BytesIO(data))
    [st] = ofx_statements(data)
    assert (st.account_label, st.account_last4, st.balance, st.balance_date) == (
        "Checking ••5555", "5555", 1519.4, "2026-09-27")
    assert [(r.date, r.description, r.amount, r.fitid) for r in st.rows] == [
        ("2026-09-04", "SUNRISE BAKERY", 22.4, "BH-0001"), ("2026-09-11", "PAYROLL ACME CO", -1200.0, "BH-0002"),
        ("2026-09-19", "CITY WATER DEPT", 58.2, "BH-0003")]


def _checking_parts() -> tuple[bytes, bytes]:
    raw = fixture("checking.ofx")
    head, body = raw.split(b"<OFX>", 1)
    return head.strip(), b"<OFX>" + body


def _variant(kind: str) -> bytes:
    head, body = _checking_parts()
    utf8_body = body.decode("cp1252").encode("utf-8")
    return {
        "bom": b"\xef\xbb\xbf" + head + b"\r\n\r\n" + body,
        "header and <OFX> on one line": head + body,
        "the whole header on one line": head.replace(b"\n", b" ") + b" " + body,
        "lower-case header": head.lower() + b"\n\n" + body,
        "CRLF and blank lines between header lines": head.replace(b"\n", b"\r\n\r\n") + b"\r\n\r\n" + body,
        "CHARSET NONE with a cp1252 byte": head.replace(b"CHARSET:1252", b"CHARSET:NONE") + b"\n\n" + body,
        "unknown VERSION": head.replace(b"VERSION:102", b"VERSION:100") + b"\n\n" + body,
        "no header at all": body,
        "v2 header, attributes reordered": b'<?xml version="1.0"?>\n<?OFX VERSION="220" OFXHEADER="200" '
                                           b'SECURITY="NONE" NEWFILEUID="NONE" OLDFILEUID="NONE"?>\n' + utf8_body,
        "lower-case tags": head + b"\n\n" + body.replace(b"<STMTTRN>", b"<stmttrn>").replace(b"</STMTTRN>",
                                                                                            b"</stmttrn>"),
    }[kind]


@pytest.mark.parametrize("kind", [
    "bom", "header and <OFX> on one line", "the whole header on one line", "lower-case header",
    "CRLF and blank lines between header lines", "CHARSET NONE with a cp1252 byte", "unknown VERSION",
    "no header at all", "v2 header, attributes reordered", "lower-case tags",
])
def test_ofx_sloppy_headers_read_like_the_clean_file(kind):
    clean = ofx_statements(fixture("checking.ofx"))
    assert ofx_statements(_variant(kind)) == clean


SGML_HEAD = _checking_parts()[0] + b"\n\n"


def _ofx(statements: str) -> bytes:
    return SGML_HEAD + f"<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS></SONRS>" \
                       f"</SIGNONMSGSRSV1>{statements}</OFX>".encode()


def _bank(acctid: str, trns: str, accttype: str = "CHECKING", extra: str = "") -> str:
    return (f"<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>999999999<ACCTID>{acctid}"
            f"<ACCTTYPE>{accttype}</BANKACCTFROM><BANKTRANLIST>{trns}</BANKTRANLIST>{extra}</STMTRS></STMTTRNRS>"
            f"</BANKMSGSRSV1>")


def _trn(amount: str, name: str = "TRADER JOE'S #552", fitid: str | None = "F1", posted: str = "20260901",
         extra: str = "") -> str:
    fit = f"<FITID>{fitid}" if fitid else ""
    return f"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>{posted}<TRNAMT>{amount}{fit}<NAME>{name}{extra}</STMTTRN>"


def test_ofx_empty_leaf_does_not_lose_transactions():
    """An SGML '<MEMO>' with no value makes ofxtools nest what follows inside it; every transaction still reads."""
    trns = _trn("-64.18", fitid="A", extra="<MEMO>\n") + _trn("-15.49", "NETFLIX.COM", "B") + _trn("2450", "PAYROLL ACME CO", "C")
    [st] = ofx_statements(_ofx(_bank("9990001234", trns, extra="<LEDGERBAL><BALAMT>100.00<DTASOF>20260926</LEDGERBAL>")))
    assert [(r.description, r.amount, r.fitid, r.line) for r in st.rows] == [
        ("TRADER JOE'S #552", 64.18, "A", 1), ("NETFLIX.COM", 15.49, "B", 2), ("PAYROLL ACME CO", -2450.0, "C", 3)]
    assert st.balance == 100.0


def test_ofx_transaction_without_fitid_is_kept():
    [st] = ofx_statements(_ofx(_bank("9990001234", _trn("-1.00", fitid=None))))
    assert st.rows == [Row("2026-09-01", "TRADER JOE'S #552", 1.0, "Checking ••1234", None, 1)]
    assert st.balance is None and st.balance_date is None


def test_ofx_several_statements_in_file_order():
    card = ("<CREDITCARDMSGSRSV1><CCSTMTTRNRS><TRNUID>2<CCSTMTRS><CURDEF>USD<CCACCTFROM><ACCTID>0000111122229876"
            "</CCACCTFROM><BANKTRANLIST>" + _trn("-41.07", "SHELL OIL 57412") + "</BANKTRANLIST><LEDGERBAL>"
            "<BALAMT>250.00<DTASOF>20260926</LEDGERBAL></CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1>")
    statements = ofx_statements(_ofx(_bank("9990001234", _trn("-1.00")) + _bank("7770004321", _trn("-2.00"), "MONEYMRKT")
                                     + card))
    assert [(s.account_label, s.kind, s.subtype) for s in statements] == [
        ("Checking ••1234", "bank", "checking"), ("Money market ••4321", "bank", "money_market"),
        ("Credit card ••9876", "credit_card", None)]
    assert statements[2].balance == -250.0          # a card in credit (OFX positive): negative owed
    assert [s.rows[0].line for s in statements] == [1, 1, 1]


@pytest.mark.parametrize("accttype, label, subtype", [
    ("CD", "CD ••1234", "cd"), ("CREDITLINE", "Credit line ••1234", None), ("", "Bank account ••1234", None),
])
def test_ofx_account_types(accttype, label, subtype):
    [st] = ofx_statements(_ofx(_bank("9990001234", _trn("-1.00"), accttype)))
    assert (st.account_label, st.subtype) == (label, subtype)


def test_ofx_masked_and_missing_account_ids():
    [st] = ofx_statements(_ofx(_bank("XXXX-XXXX-4242", _trn("-1.00"))))
    assert (st.account_label, st.account_last4) == ("Checking ••4242", "4242")
    no_id = _ofx(_bank("", _trn("-1.00")).replace("<ACCTID>", ""))
    [st] = ofx_statements(no_id)
    assert (st.account_label, st.account_last4) == ("Checking", None)


def test_ofx_description_falls_back_to_memo_and_payee():
    trns = (_trn("-1.00", name="", fitid="A", extra="<MEMO>POS PURCHASE CORNER CAFE")
            + "<STMTTRN><TRNTYPE>CHECK<DTPOSTED>20260902<TRNAMT>-2.00<FITID>B<PAYEE><NAME>CITY WATER DEPT</PAYEE>"
              "<MEMO>CHECK 1042</STMTTRN>"
            + _trn("-3.00", name="", fitid="C"))
    [st] = ofx_statements(_ofx(_bank("9990001234", trns)))
    assert [r.description for r in st.rows] == ["POS PURCHASE CORNER CAFE", "CITY WATER DEPT", ""]


def test_ofx_dtuser_when_dtposted_is_missing():
    trn = "<STMTTRN><TRNTYPE>DEBIT<DTUSER>20260903<TRNAMT>-1.00<FITID>A<NAME>X</STMTTRN>"
    [st] = ofx_statements(_ofx(_bank("9990001234", trn)))
    assert st.rows[0].date == "2026-09-03"


@pytest.mark.parametrize("trn, field", [
    ("<STMTTRN><TRNTYPE>DEBIT<TRNAMT>-1.00<FITID>A<NAME>X</STMTTRN>", "date"),
    ("<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261399<TRNAMT>-1.00<FITID>A<NAME>X</STMTTRN>", "date"),
    ("<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260901<FITID>A<NAME>X</STMTTRN>", "amount"),
    ("<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260901<TRNAMT>lots<FITID>A<NAME>X</STMTTRN>", "amount"),
])
def test_ofx_bad_transaction_names_it(trn, field):
    with pytest.raises(ImportFileError, match=r"^Transaction 2 of Checking ••1234") as err:
        ofx_statements(_ofx(_bank("9990001234", _trn("-1.00") + trn)))
    assert (err.value.field, err.value.row) == (field, 2)


@pytest.mark.parametrize("data, message", [
    (_ofx(""), "no bank or credit card statement"),
    (_ofx("<INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS><CURDEF>USD</INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1>"),
     "investment statement"),
    (SGML_HEAD + b"<OFX></OFX></OFX></OFX>", "couldn't be read: the tags in it are broken"),
    (SGML_HEAD + b"<OFX><CODE>0</CODE>stray text</OFX>", "couldn't be read"),
    (b"OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n", "couldn't be read: there's no <OFX> section"),
    (fixture("everyday-export.csv"), "couldn't be read"),
])
def test_ofx_unreadable_files(data, message):
    with pytest.raises(ImportFileError, match=message) as err:
        ofx_statements(data)
    assert "Traceback" not in str(err.value)

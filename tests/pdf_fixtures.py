"""Synthetic statement PDFs for the PDF import tests (fpdf2). Fictional names and figures only; no real statement."""

from __future__ import annotations

from fpdf import FPDF

PAGE = (612, 792)
COLUMNS = (40, 120, 300, 380, 460)      # date, description, debit, credit, balance
HEADER = ("Date", "Description", "Debit", "Credit", "Balance")


def pdf(pages: list[list[tuple[float, float, str]]], *, page=PAGE, password: str | None = None,
        font_size: float = 9.0) -> bytes:
    """One or more pages of text placed at (x, baseline y). Encrypted when `password` is given."""
    doc = FPDF(unit="pt", format=page)
    doc.set_auto_page_break(False)
    doc.set_margins(0, 0, 0)
    for lines in pages:
        doc.add_page()
        doc.set_font("Helvetica", size=font_size)
        for x, y, text in lines:
            doc.text(x, y, text)
    if password is not None:
        doc.set_encryption(owner_password="synthetic-owner", user_password=password)
    return bytes(doc.output())


def _row(x0: float, y: float, cells: list[tuple[float, str]]) -> list[tuple[float, float, str]]:
    return [(x, y, text) for x, text in cells if text]


def statement(rows, *, period="Statement period 01/03/2024 to 31/03/2024", opening="Opening balance 100.00",
              closing="Closing balance {last}", header=HEADER, header_y: float = 60,
              columns=COLUMNS,
              extra: list[tuple[float, float, str]] | None = None,
              after: list[list[tuple[float, float, str]]] | None = None,
              page=PAGE, password: str | None = None, row_y: float | None = None) -> bytes:
    """A one-account statement. Each row has one cell per header word; '' leaves a cell empty.

    A description cell with a newline continues on the following lines at the same x. `after` adds pages after
    page one. The default rows are (date, description, debit, credit, balance).
    """
    lines: list[tuple[float, float, str]] = []
    if period:
        lines.append((40, 20, period))
    if opening:
        lines.append((40, 34, opening))
    top = header_y
    width = len(header) if header else 5
    xs = tuple(columns)[:width]
    if header:
        lines += _row(40, top, [(x, text) for x, text in zip(xs, header)])
    top += 14
    last_balance = ""
    for row in rows:
        first = True
        desc = str(row[1]) if width > 1 else ""
        for part in desc.split("\n"):
            cells = []
            for i, (x, cell) in enumerate(zip(xs, row)):
                if not first and i != 1:
                    continue
                if i == 1:
                    cells.append((x, part))
                elif cell:
                    cells.append((x, str(cell)))
            lines += _row(40, top, cells)
            if first and row and row[-1]:
                last_balance = str(row[-1])
            top += 14 if row_y is None else row_y
            first = False
    if extra:
        for x, y, text in extra:
            lines.append((x, y, text))
    if closing:
        top += 16
        lines.append((40, top, closing.format(last=last_balance or "0.00")))
    pages = [lines] + list(after or [])
    return pdf(pages, page=page, password=password)


def two_accounts() -> bytes:
    """Two balance tables on one page: not one account per PDF."""
    lines: list[tuple[float, float, str]] = [(40, 20, "Statement period 01/03/2024 to 31/03/2024")]
    lines.append((40, 34, "Opening balance 100.00"))
    lines += _row(40, 60, [(x, text) for x, text in zip(COLUMNS, HEADER)])
    lines += _row(40, 74, [(40, "05/03/2024"), (120, "Coffee"), (300, "10.00"), (460, "90.00")])
    lines += _row(40, 100, [(x, text) for x, text in zip(COLUMNS, HEADER)])
    lines += _row(40, 114, [(40, "06/03/2024"), (120, "Other"), (300, "5.00"), (460, "20.00")])
    lines.append((40, 150, "Closing balance 20.00"))
    return pdf([lines])


def blank_page() -> bytes:
    """A scan with no text layer."""
    return pdf([[]])


def credit_card() -> bytes:
    lines = [(40, 20, "Credit Card Statement"), (40, 34, "Statement period 01/03/2024 to 31/03/2024"),
             (40, 48, "Opening balance 500.00")]
    lines += _row(40, 70, [(x, text) for x, text in zip(COLUMNS, HEADER)])
    lines += _row(40, 84, [(40, "05/03/2024"), (120, "Shop"), (300, "10.00"), (460, "490.00")])
    lines.append((40, 120, "Closing balance 490.00"))
    return pdf([lines])


def plain(rows, *, period="Statement period 01/03/2024 to 31/03/2024", opening="Opening balance 100.00",
          closing="Closing balance {last}"):
    return statement(rows, period=period, opening=opening, closing=closing)


# ---- the four statements the import screens are built and gated on (epic 0.5; synthetic, and the mock's own facts) ----
LOCKED_PASSWORD = "synthetic-pass"   # a made-up password for the locked fixture: never a real one

OCT_2026 = "Statement period 10/01/2026 to 10/31/2026"   # MM/DD, like the mock: 10/31 can only be month first
STATEMENT_ROWS = [("10/01/2026", "ACH DEPOSIT PAYROLL", "", "1,600.00", "4,010.37"),
                  ("10/02/2026", "TRADER JOE S #122", "84.12", "", "3,926.25"),
                  ("10/03/2026", "SHELL OIL 57444", "52.40", "", "3,873.85"),
                  ("10/03/2026", "SPOTIFY USA", "11.99", "", "3,861.86"),
                  ("10/05/2026", "ZELLE PAYMENT SENT", "120.00", "", "3,741.86")]


def good_statement(password: str | None = None) -> bytes:
    """The check screen's own statement: 5 rows, opening 2,410.37, closing 3,741.86, and it adds up."""
    return statement(STATEMENT_ROWS, period=OCT_2026, opening="Opening balance 2,410.37", password=password)


def no_balances_statement() -> bytes:
    """A statement with no balance column and no printed totals: refused with no_balances, teachable (opening 300.00,
    closing 173.48) and the one the gate types those two numbers into."""
    return statement([("10/01/2026", "TRADER JOE S #122", "84.12", ""),
                      ("10/03/2026", "SHELL OIL 57444", "52.40", ""),
                      ("10/04/2026", "REFUND", "", "10.00")],
                     period=OCT_2026, opening=None, closing=None,
                     header=("Date", "Description", "Debit", "Credit"), columns=(40, 120, 300, 380))


def off_statement() -> bytes:
    """The same rows as good_statement, with a closing balance $65.00 out: refused with does_not_reconcile."""
    rows = STATEMENT_ROWS[:-1] + [("10/05/2026", "ZELLE PAYMENT SENT", "120.00", "", "3,806.86")]
    return statement(rows, period=OCT_2026, opening="Opening balance 2,410.37")


def scanned_statement() -> bytes:
    """A page with no text at all, only ink: refused with no_text_layer, and nothing to teach."""
    doc = FPDF()
    doc.add_page()
    doc.set_fill_color(200, 200, 200)
    doc.rect(20, 20, 170, 250, "F")
    return _raw(doc)


def prose_statement() -> bytes:
    """A page of letter prose with no transaction table: refused with no_header."""
    doc = FPDF()
    doc.add_page()
    doc.set_font("helvetica", size=11)
    for i, line in enumerate(["Nova Federal Credit Union", "Statement of account", "Dear member,",
                              "This page explains the terms of your account.", "The annual percentage rate is 4.5%.",
                              "Nothing here is a table of transactions."]):
        doc.text(20, 30 + i * 10, line)
    return _raw(doc)


def split_row_statement() -> bytes:
    """The IN shape (docs/pdf-teach-once.md): each row's date is on one line and its money on the next, so the
    default row cut (a date OR an amount starts a row) splits every row in two. Taught with row_start=date it adds
    up: opening 1,000.00 + in 250.00 - out 130.00 = closing 1,120.00."""
    doc = FPDF()
    doc.add_page()
    doc.set_font("helvetica", size=9)
    doc.text(40, 28, OCT_2026)
    doc.text(40, 40, "Opening balance 1,000.00")
    cols = (40, 120, 300, 380, 460)
    for i, name in enumerate(("Date", "Description", "Debit", "Credit", "Balance")):
        doc.text(cols[i], 54, name)
    rows = [("10/01/2026", "ACH DEPOSIT PAYROLL", "", "250.00", "1,250.00"),
            ("10/02/2026", "TRADER JOE S #122", "84.12", "", "1,165.88"),
            ("10/03/2026", "SHELL OIL 57444", "45.88", "", "1,120.00")]
    y = 66
    for date, desc, debit, credit, balance in rows:
        doc.text(cols[0], y, date)                      # the date line: date and description only
        doc.text(cols[1], y, desc)
        y += 10
        for i, value in ((2, debit), (3, credit), (4, balance)):   # the money line: no date on it
            if value:
                doc.text(cols[i], y, value)
        y += 14
    return _raw(doc)


def _raw(doc) -> bytes:
    out = doc.output()
    return out.encode("latin-1") if isinstance(out, str) else out

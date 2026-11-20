"""Deterministic generator for the PDF import spike's synthetic statements.

Runs with the disposable venv (fpdf2 is not a repository dependency):

    .dispatch/venv/bin/python tests/pdfspike_corpus.py --out tests/fixtures/pdfspike

Writes 20 text-layer statements across 7 country layouts (the measurement denominator), four
negative-control PDFs, and manifest.json. Every amount, date and running balance is known here,
so the manifest can carry the expectations the runner checks against.

The bank names, people and references are invented. Nothing here is a real institution, a real
person, or a real account number, and no test fixture carries a home path, an e-mail or a
credential (the locked.pdf password lives only in this module).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import EncryptionMethod

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from hermes_finance.importfile import _cents  # noqa: E402

LOCKED_PASSWORD = "spike-pw"
CREATED = datetime(2026, 4, 1, tzinfo=timezone.utc)

PAGE_W, PAGE_H = 297.0, 210.0        # A4 landscape
MARGIN = 14.0
BOTTOM = PAGE_H - 16.0
LINE_H = 5.2
STACK_H = 4.3

REQUIRED_FEATURES = (
    "multi-line description",
    "cells stacked one-per-line",
    "CR/DR suffix",
    "DR/CR marker column",
    "separate debit/credit columns",
    "(+)/(-) token",
    "trailing minus",
    "parentheses negative",
    "preamble noise above the table",
    "repeated page header",
    "opening-balance row inside the table",
    "summary/total lines with an explicit sign",
    "page break in the middle of the table",
    "Indian lakh grouping",
    "decimal comma + dot thousands",
    "decimal dot + comma thousands",
    "ambiguous MDY/DMY date",
    "non-amount parenthesised document number",
    "header labels misaligned from values",
    "two-row header",
    "dates without a year",
    "no running-balance column",
)


@dataclass(frozen=True)
class Col:
    field: str
    center: float
    width: float
    align: str = "L"


LAYOUTS: dict[str, list[Col]] = {
    "split": [Col("date", 27, 24), Col("description", 98, 100), Col("debit", 178, 34, "R"),
              Col("credit", 222, 34, "R"), Col("balance", 266, 34, "R")],
    "signed": [Col("date", 27, 24), Col("description", 105, 120), Col("amount", 205, 40, "R"),
               Col("balance", 266, 34, "R")],
    "token": [Col("date", 27, 24), Col("description", 95, 90), Col("sign", 160, 24, "C"),
              Col("amount", 200, 40, "R"), Col("balance", 266, 34, "R")],
    "marker": [Col("date", 27, 24), Col("description", 90, 80), Col("marker", 160, 22, "C"),
               Col("amount", 200, 36, "R"), Col("balance", 266, 34, "R")],
    "cl": [Col("date", 27, 24), Col("description", 92, 80), Col("debit", 160, 30, "R"),
           Col("credit", 198, 30, "R"), Col("balance", 266, 34, "R")],
    "in": [Col("date", 27, 20), Col("description", 70, 56), Col("reference", 120, 24, "C"),
           Col("value", 156, 26, "C"), Col("debit", 190, 26, "R"), Col("credit", 224, 26, "R"),
           Col("balance", 266, 32, "R")],
    # The header labels of this layout are drawn at the column's left edge while the values are
    # right-aligned: the geometry the parser must not trust a header cell's centre for.
    "misaligned": [Col("date", 27, 24), Col("description", 105, 90), Col("debit", 175, 38, "R"),
                   Col("credit", 220, 38, "R"), Col("balance", 265, 38, "R")],
    # A4 portrait, the reviewer's probe geometry: the numeric header labels sit at the column's
    # left edge while the values are right-aligned at the column's right edge.
    "reviewer": [Col("date", 23, 22), Col("description", 73, 74), Col("debit", 127, 30, "R"),
                 Col("credit", 156, 28, "R"), Col("balance", 184, 28, "R")],
    "flat": [Col("date", 27, 24), Col("description", 120, 120), Col("amount", 226, 50, "R")],
}

HEADER_TEXT = {
    "AU": {"date": "Date", "description": "Description", "debit": "Debit", "credit": "Credit",
           "balance": "Balance"},
    "IN": {"date": "Date", "description": "Narration", "reference": "Chq./Ref.No.", "value": "Value Dt",
            "debit": "Withdrawal Amt.", "credit": "Deposit Amt.", "balance": "Closing Balance"},
    "SG": {"date": "Date", "description": "Transaction", "debit": "Withdrawal", "credit": "Deposit",
           "balance": "Balance", "marker": "DR/CR", "amount": "Amount"},
    "US": {"date": "Date", "description": "Description", "debit": "Debits", "credit": "Credits",
           "amount": "Amount", "balance": "Balance"},
    "BR": {"date": "Data", "description": "Histórico", "sign": "Tipo", "amount": "Valor", "balance": "Saldo"},
    "CL": {"date": "Fecha", "description": "Descripción", "debit": "Cargo", "credit": "Abono", "balance": "Saldo"},
    "CO": {"date": "Fecha", "description": "Descripción", "amount": "Valor", "balance": "Saldo"},
}

BANKS = {
    "AU": ("Harbourline Bank", "12 Tallow Street, Port Ellery"),
    "IN": ("Sundara Cooperative Bank", "44 Lantern Road, Pune"),
    "SG": ("Straits Lantern Bank", "8 Merbau Quay, Singapore"),
    "US": ("Cedar Ridge Savings", "900 Alder Avenue, Springfield"),
    "BR": ("Banco Pirilampo", "Av. das Palmeiras 220, Curitiba"),
    "CL": ("Banco Ventana Sur", "Calle Los Aromos 145, Valparaíso"),
    "CO": ("Banco Cumbre Alta", "Carrera 12 # 84-20, Bogotá"),
}

HOLDERS = {
    "AU": "Wren Ashcombe", "IN": "Priya Nandakumar", "SG": "Wei-Lin Tan", "US": "Jordan Ellery",
    "BR": "Ivo Marchetti", "CL": "Camila Duarte", "CO": "Nadia Voss",
}

DESCRIPTIONS = {
    "AU": ["Kookaburra Market", "Saltbush Cafe", "Blue Gum Transit", "Paperbark Books",
           "North Pier Fuel", "Wattle Lane Pharmacy", "Monthly account fee", "Harbourline Payroll"],
    "IN": ["UPI SUNDARA GROCERY", "NEFT CR SALARY", "IMPS P2A TRANSFER", "ATM CASH WITHDRAWAL",
           "BILL PAY ELECTRICITY", "CHEQUE DEPOSIT", "UPI MERCHANT PAYMENT", "INTEREST CREDIT"],
    "SG": ["GIRO PAYMENT", "PAYNOW TRANSFER", "FAST SALARY CREDIT", "CARD PURCHASE",
           "ATM WITHDRAWAL", "BILL PAYMENT", "INTERBANK TRANSFER"],
    "US": ["CARD PURCHASE COFFEE HOUSE", "ONLINE PAYMENT UTILITY", "PAYROLL DIRECT DEP",
           "ATM WITHDRAWAL", "CHECK 1042", "CARD PURCHASE MARKET", "TRANSFER TO SAVINGS"],
    "BR": ["COMPRA CARTAO", "TED RECEBIDA", "PAGAMENTO BOLETO", "SAQUE ATM", "SALARIO",
           "PIX RECEBIDO", "TARIFA MENSAL"],
    "CL": ["COMPRA POS", "TRANSFERENCIA RECIBIDA", "PAGO TARJETA", "GIRO CAJERO", "SUELDO",
           "PAGO SERVICIOS", "COMISION MENSUAL"],
    "CO": ["COMPRA DATAFONO", "PAGO NOMINA", "RETIRO CAJERO", "PAGO SERVICIOS",
           "TRANSFERENCIA RECIBIDA", "CUOTA MANEJO"],
}

CONTINUATIONS = {
    "AU": ["Card 4821", "Ref 7714", "Terminal 09"],
    "IN": ["REF 8842", "BRANCH 0142", "TT 5512"],
    "SG": ["Ref 3091", "Card 4821", "Auth 5512"],
    "US": ["Memo AUTH 5512", "Ref 7714", "POS 0042"],
    "BR": ["DOC 8842", "AG 0142", "REF 5512"],
    "CL": ["DOC 8842", "SUC 0142", "REF 5512"],
    "CO": ["DOC 8842", "SUC 0142", "REF 5512"],
}


@dataclass
class Spec:
    id: str
    country: str
    layout: str
    rows: int
    seed: int
    opening: str
    features: list[str] = field(default_factory=list)
    opening_inside: bool = False
    opening_above: bool = True
    summary_above: bool = False
    summary_below: bool = False
    negative_style: str = "minus"       # minus | trailing | paren | mixed
    suffix: bool = False                # CR/DR suffix in the amount cell
    token: bool = False                 # (+) / (-) column
    marker: bool = False                # DR/CR column
    stacked: bool = False               # one cell per line
    continuation: float = 0.0           # probability an extra description line is drawn
    references: bool = False
    document_number: bool = False
    ambiguous: bool = False
    page_break: bool = False
    misaligned: bool = False          # header labels left-aligned, values right-aligned
    two_row: bool = False             # header labels split over two visual lines
    yearless: bool = False            # dates printed without a year
    no_balance: bool = False          # no running-balance column
    no_opening: bool = False          # no printed opening balance
    no_closing: bool = False          # no printed closing balance
    closing_bias: str = "0"           # cents added to the printed closing line only
    closing_above: bool = False       # printed closing balance above the table, not below it
    first_amount_bias: str = "0"      # added to the first row's printed amount only
    portrait: bool = False            # A4 portrait instead of landscape


SPECS: tuple[Spec, ...] = (
    Spec("au-01", "AU", "split", 14, 101, "4820.55",
         ["multi-line description", "separate debit/credit columns", "preamble noise above the table",
          "opening-balance row inside the table", "decimal dot + comma thousands"],
         opening_inside=True, continuation=0.8),
    Spec("au-02", "AU", "split", 18, 102, "9350.10",
         ["multi-line description", "separate debit/credit columns", "preamble noise above the table",
          "opening-balance row inside the table", "decimal dot + comma thousands"],
         opening_inside=True, continuation=0.8),
    Spec("au-03", "AU", "split", 34, 103, "12880.40",
         ["separate debit/credit columns", "preamble noise above the table", "repeated page header",
          "opening-balance row inside the table", "page break in the middle of the table",
          "decimal dot + comma thousands"],
         opening_inside=True, page_break=True),
    Spec("au-04", "AU", "split", 12, 104, "2110.00",
         ["separate debit/credit columns", "preamble noise above the table", "opening-balance row inside the table",
          "ambiguous MDY/DMY date", "decimal dot + comma thousands"],
         opening_inside=True, ambiguous=True),

    Spec("in-01", "IN", "in", 12, 201, "123456.78",
         ["cells stacked one-per-line", "separate debit/credit columns", "preamble noise above the table",
          "repeated page header", "Indian lakh grouping", "decimal dot + comma thousands"],
         stacked=True),
    Spec("in-02", "IN", "in", 16, 202, "234567.89",
         ["cells stacked one-per-line", "separate debit/credit columns", "preamble noise above the table",
          "repeated page header", "Indian lakh grouping", "decimal dot + comma thousands",
          "non-amount parenthesised document number"],
         stacked=True, references=True, document_number=True),
    Spec("in-03", "IN", "in", 20, 203, "98765.43",
         ["cells stacked one-per-line", "separate debit/credit columns", "preamble noise above the table",
          "repeated page header", "page break in the middle of the table", "Indian lakh grouping",
          "decimal dot + comma thousands"],
         stacked=True, page_break=True),
    Spec("in-04", "IN", "in", 10, 204, "456789.01",
         ["multi-line description", "cells stacked one-per-line", "separate debit/credit columns",
          "preamble noise above the table", "repeated page header", "Indian lakh grouping",
          "decimal dot + comma thousands"],
         stacked=True, continuation=0.9),

    Spec("sg-01", "SG", "split", 12, 301, "18240.00",
         ["CR/DR suffix", "separate debit/credit columns", "preamble noise above the table",
          "summary/total lines with an explicit sign", "decimal dot + comma thousands"],
         suffix=True, summary_above=True, opening_above=False),
    Spec("sg-02", "SG", "marker", 14, 302, "7215.35",
         ["DR/CR marker column", "preamble noise above the table", "decimal dot + comma thousands"],
         marker=True),
    Spec("sg-03", "SG", "split", 26, 303, "30990.10",
         ["CR/DR suffix", "separate debit/credit columns", "preamble noise above the table",
          "repeated page header", "page break in the middle of the table", "decimal dot + comma thousands"],
         suffix=True, page_break=True),

    Spec("us-01", "US", "signed", 14, 401, "8420.66",
         ["trailing minus", "preamble noise above the table", "decimal dot + comma thousands"],
         negative_style="trailing"),
    Spec("us-02", "US", "signed", 14, 402, "5250.25",
         ["parentheses negative", "preamble noise above the table", "decimal dot + comma thousands"],
         negative_style="paren"),
    Spec("us-03", "US", "signed", 18, 403, "11760.80",
         ["multi-line description", "trailing minus", "parentheses negative", "preamble noise above the table",
          "decimal dot + comma thousands"],
         negative_style="mixed", continuation=0.8),
    Spec("us-04", "US", "signed", 16, 404, "6240.00",
         ["preamble noise above the table", "summary/total lines with an explicit sign",
          "ambiguous MDY/DMY date", "decimal dot + comma thousands"],
         negative_style="paren", summary_below=True, ambiguous=True),

    Spec("br-01", "BR", "token", 12, 501, "12345.67",
         ["(+)/(-) token", "preamble noise above the table", "decimal comma + dot thousands"],
         token=True),
    Spec("br-02", "BR", "token", 16, 502, "87654.32",
         ["(+)/(-) token", "preamble noise above the table", "opening-balance row inside the table",
          "decimal comma + dot thousands"],
         token=True, opening_inside=True),

    Spec("cl-01", "CL", "cl", 12, 601, "15432.10",
         ["separate debit/credit columns", "preamble noise above the table", "decimal comma + dot thousands"]),
    Spec("cl-02", "CL", "cl", 16, 602, "67890.12",
         ["separate debit/credit columns", "preamble noise above the table", "opening-balance row inside the table",
          "decimal comma + dot thousands"],
         opening_inside=True),

    Spec("co-01", "CO", "signed", 14, 701, "191778.17",
         ["preamble noise above the table", "decimal dot + comma thousands"],
         negative_style="minus"),
)

# Committed and sha-pinned, but outside the 20-file gate: each one isolates a structure the spike-2
# card adds support for (or refuses), so the committed tests cover it without a real statement.
EXTRA_SPECS: tuple[Spec, ...] = (
    Spec("x-misaligned", "US", "misaligned", 14, 801, "8420.66",
         ["header labels misaligned from values", "preamble noise above the table"], misaligned=True),
    Spec("x-two-row", "US", "signed", 10, 802, "6240.00",
         ["two-row header", "preamble noise above the table"], two_row=True),
    Spec("x-yearless", "AU", "split", 10, 803, "8120.00",
         ["dates without a year", "preamble noise above the table"], yearless=True),
    Spec("x-no-balance", "US", "flat", 10, 804, "5000.00",
         ["no running-balance column", "preamble noise above the table"], no_balance=True),
    Spec("x-no-balances", "US", "flat", 6, 805, "5000.00", [],
         no_balance=True, no_opening=True, no_closing=True),
    Spec("x-off-cent", "US", "signed", 6, 806, "5000.00", [], closing_bias="0.02"),
    Spec("x-no-opening", "US", "signed", 14, 807, "5000.00", [],
         no_opening=True, no_closing=True, first_amount_bias="1000.00"),
    Spec("x-reviewer-left", "AU", "reviewer", 14, 10, "5120.40",
         ["separate debit/credit columns", "preamble noise above the table",
          "header labels misaligned from values"],
         misaligned=True, portrait=True, closing_above=True, no_closing=True),
)


def fmt_dot(value: Decimal) -> str:
    return f"{value:,.2f}"


def fmt_comma(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", "\0").replace(".", ",").replace("\0", ".")


def fmt_inr(value: Decimal) -> str:
    sign = "-" if value < 0 else ""
    whole, frac = f"{abs(value):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}{whole}.{frac}"


def _money(value: Decimal, country: str, peso: bool = False) -> str:
    if country == "IN":
        return fmt_inr(value)
    if country in ("BR", "CL"):
        return fmt_comma(value)
    text = fmt_dot(value)
    return f"${text}" if peso else text


@dataclass
class Txn:
    day: date
    app_amount: Decimal               # > 0 money out, < 0 money in
    description: list[str]
    reference: str | None = None
    value_day: date | None = None
    balance: Decimal = Decimal(0)


def _txns(spec: Spec) -> tuple[list[Txn], Decimal, Decimal]:
    rng = random.Random(spec.seed)
    month = date(2026, 3, 1)
    days: list[date] = []
    for i in range(spec.rows):
        if spec.ambiguous and i < 4:
            days.append(month + timedelta(days=[4, 5, 6, 7][i]))
        else:
            days.append(month + timedelta(days=(i * 2) % 28))
    days.sort()
    balance = Decimal(spec.opening)
    out: list[Txn] = []
    for i, day in enumerate(days):
        if spec.country == "IN" and i == 0:
            amount = Decimal("123456.78")
        elif spec.country == "IN":
            amount = Decimal(rng.randint(250, 900000)) / 100
        else:
            amount = Decimal(rng.randint(250, 320000)) / 100
        if i != 0 and rng.random() < 0.45:
            amount = -amount
        desc = [rng.choice(DESCRIPTIONS[spec.country])]
        if spec.continuation and rng.random() < spec.continuation:
            desc.append(rng.choice(CONTINUATIONS[spec.country]))
        reference = None
        if spec.references and rng.random() < 0.7:
            reference = rng.choice(["REF 8842", "CHQ 1042", "BRANCH 0142"])
        if spec.document_number and i == 2:
            reference = "(1000055)"
        balance -= amount
        out.append(Txn(day, amount, desc, reference, day - timedelta(days=1), balance))
    if spec.first_amount_bias != "0" and out:
        out[0].app_amount += Decimal(spec.first_amount_bias)
    closing = out[-1].balance if out else Decimal(spec.opening)
    return out, Decimal(spec.opening), closing


class _Doc:
    def __init__(self, spec: Spec):
        self.spec = spec
        self.pdf = FPDF(orientation="P" if spec.portrait else "L", unit="mm", format="A4")
        self.pdf.set_creation_date(CREATED)
        self.pdf.set_producer("hermes-finance pdfspike")
        self.pdf.set_creator("hermes-finance pdfspike")
        self.pdf.set_auto_page_break(False)
        self.page_no = 0
        self.y = MARGIN
        self.page_w = 210.0 if spec.portrait else PAGE_W
        self.page_h = 297.0 if spec.portrait else PAGE_H
        self.bottom = self.page_h - 16.0
        self.line_h = 5.4 if spec.portrait else LINE_H
        self.body_size = 8.0 if spec.portrait else 8.5
        self.columns = {c.field: c for c in LAYOUTS[spec.layout]}
        self.repeat_header = False

    def _font(self, size: float, style: str = "") -> None:
        self.pdf.set_font("helvetica", style, size)

    def put(self, col: Col, y: float, text: str, size: float, style: str = "") -> None:
        self._font(size, style)
        width = self.pdf.get_string_width(text)
        if col.align == "R":
            x = col.center + col.width / 2 - width
        elif col.align == "C":
            x = col.center - width / 2
        else:
            x = col.center - col.width / 2
        self.pdf.text(x, y, text)

    def line(self, x: float, y: float, text: str, size: float = 9.0, style: str = "", align: str = "L") -> None:
        self._font(size, style)
        width = self.pdf.get_string_width(text)
        if align == "R":
            x -= width
        elif align == "C":
            x -= width / 2
        self.pdf.text(x, y, text)

    def wrap(self, text: str, width: float, size: float) -> list[str]:
        self._font(size)
        words, lines, current = text.split(), [], ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and self.pdf.get_string_width(candidate) > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines or [""]

    def new_page(self) -> None:
        self.pdf.add_page()
        self.page_no += 1
        self.line(self.page_w - MARGIN, 10, f"Page {self.page_no}", 8, align="R")
        if self.repeat_header:
            self.header(y=28)
            self.y = 36
        else:
            self.y = MARGIN

    def ensure(self, height: float) -> None:
        if self.y + height > self.bottom:
            self.new_page()

    def header(self, y: float) -> None:
        labels = HEADER_TEXT[self.spec.country]
        if self.spec.two_row:
            first = {"date": "Transaction", "description": "Description", "amount": "Amount",
                     "balance": "Running"}
            second = {"date": "Date", "balance": "Balance"}
            for row, offset in ((first, 0.0), (second, 4.0)):
                for name, col in self.columns.items():
                    if name in labels and name in row:
                        self.put(col, y + offset, row[name], 8.0, "B")
            underline = y + 6.0
        elif self.spec.misaligned:
            self._font(8.0, "B")
            for name, col in self.columns.items():
                if name in labels:
                    self.pdf.text(col.center - col.width / 2, y, labels[name])
            underline = y + 1.5
        else:
            for name, col in self.columns.items():
                if name in labels:
                    self.put(col, y, labels[name], 8.0, "B")
            underline = y + 1.5
        self.pdf.set_draw_color(90)
        self.pdf.line(MARGIN, underline, self.page_w - MARGIN, underline)
        self.pdf.set_draw_color(0)

    def preamble(self) -> None:
        bank, address = BANKS[self.spec.country]
        self.line(MARGIN + 6, 20, bank, 15, "B")
        self.line(MARGIN + 6, 26, address, 8)
        self.line(MARGIN + 6, 34, "Statement of account", 11, "B")
        self.line(MARGIN + 6, 40, f"Account holder: {HOLDERS[self.spec.country]}", 9)
        self.line(MARGIN + 6, 45, "Account: **** 4821", 9)
        self.line(MARGIN + 6, 50, "Statement period: 01/03/2026 to 31/03/2026", 9)
        self.y = 58

    def summary_block(self, opening: Decimal, credit_total: Decimal, debit_total: Decimal,
                      closing: Decimal) -> None:
        money = lambda v: _money(v, self.spec.country, peso=self.spec.country == "CO")
        self.line(MARGIN + 6, self.y, "Statement summary", 10, "B")
        self.y += 6
        for label, value in (("Opening Balance", money(opening)),
                             ("Total Credits", f"+{money(credit_total)}"),
                             ("Total Debits", f"-{money(debit_total)}"),
                             ("Closing Balance", money(closing))):
            self.line(MARGIN + 6, self.y, label, 9)
            self.line(MARGIN + 120, self.y, value, 9, align="R")
            self.y += 5
        self.y += 4

    def opening_line(self, opening: Decimal) -> None:
        money = _money(opening, self.spec.country, peso=self.spec.country == "CO")
        self.line(MARGIN + 6, self.y, "Opening Balance", 9)
        self.line(MARGIN + 120, self.y, money, 9, align="R")
        self.y += 7

    def closing_line(self, closing: Decimal) -> None:
        money = _money(closing, self.spec.country, peso=self.spec.country == "CO")
        self.line(MARGIN + 6, self.y + 3, "Closing Balance", 9)
        self.line(MARGIN + 120, self.y + 3, money, 9, align="R")
        self.y += 6

    def _cells(self, txn: Txn) -> dict[str, str]:
        spec = self.spec
        out: dict[str, str] = {}
        day_fmt = {"AU": "%d/%m/%Y", "IN": "%d-%m-%y", "SG": "%d %b %Y", "US": "%m/%d/%Y",
                   "BR": "%d/%m/%Y", "CL": "%d-%m-%Y", "CO": "%d/%m/%Y"}[spec.country]
        if spec.yearless:
            day_fmt = {"AU": "%d/%m", "IN": "%d-%m", "SG": "%d %b", "US": "%m/%d",
                       "BR": "%d/%m", "CL": "%d-%m", "CO": "%d/%m"}[spec.country]
        out["date"] = txn.day.strftime(day_fmt)
        amount = txn.app_amount
        if spec.layout == "in":
            out["reference"] = txn.reference or ""
            out["value"] = (txn.value_day or txn.day).strftime(day_fmt)
        if spec.layout in ("split", "cl", "in", "misaligned", "reviewer"):
            money = _money(abs(amount), spec.country)
            if spec.suffix:
                money = f"{money} {'DR' if amount > 0 else 'CR'}"
            out["debit" if amount > 0 else "credit"] = money
        elif spec.layout == "marker":
            out["marker"] = "DR" if amount > 0 else "CR"
            out["amount"] = _money(abs(amount), spec.country)
        elif spec.layout == "token":
            out["sign"] = "(-)" if amount > 0 else "(+)"
            out["amount"] = _money(abs(amount), spec.country)
        else:
            peso = spec.country == "CO"
            if amount > 0:
                style = spec.negative_style
                if style == "mixed":
                    style = "trailing" if txn.day.day % 2 else "paren"
                if style == "trailing":
                    out["amount"] = f"{_money(abs(amount), spec.country)}-"
                elif style == "paren":
                    out["amount"] = f"({_money(abs(amount), spec.country)})"
                else:
                    out["amount"] = f"-{_money(abs(amount), spec.country, peso=peso)}"
            else:
                out["amount"] = _money(abs(amount), spec.country, peso=peso)
        if not spec.no_balance:
            out["balance"] = _money(txn.balance, spec.country, peso=spec.country == "CO")
        return out

    def row(self, txn: Txn) -> None:
        cells = self._cells(txn)
        if self.spec.stacked:
            self._stacked_row(txn.description, cells)
        else:
            self._columnar_row(txn.description, cells)

    def _columnar_row(self, description: list[str], cells: dict[str, str]) -> None:
        desc_col = self.columns["description"]
        lines: list[str] = []
        for part in description:
            lines.extend(self.wrap(part, desc_col.width - 12, self.body_size))
        height = len(lines) * self.line_h
        self.ensure(height)
        for field, text in cells.items():
            col = self.columns.get(field)
            if col and text:
                self.put(col, self.y + 3.2, text, self.body_size)
        for i, text in enumerate(lines):
            self.put(desc_col, self.y + 3.2 + i * self.line_h, text, self.body_size)
        self.y += height

    def _stacked_row(self, description: list[str], cells: dict[str, str]) -> None:
        desc_col = self.columns["description"]
        lines: list[str] = []
        for part in description:
            lines.extend(self.wrap(part, desc_col.width - 6, 8.2))
        order = ["date", "description", "reference", "value", "debit", "credit", "balance"]
        self.ensure((len(order) + len(lines)) * STACK_H)
        for name in order:
            if name == "description":
                for text in lines:
                    self.put(desc_col, self.y + 3.0, text, 8.2)
                    self.y += STACK_H
                continue
            col = self.columns.get(name)
            text = cells.get(name, "")
            if col and text:
                self.put(col, self.y + 3.0, text, 8.2)
            self.y += STACK_H

    def opening_row(self, opening: Decimal) -> None:
        money = _money(opening, self.spec.country, peso=self.spec.country == "CO")
        desc = {"BR": "Saldo Anterior", "CL": "SALDO INICIAL"}.get(self.spec.country, "Opening Balance")
        cells = {"balance": money}
        if self.spec.stacked:
            self._stacked_row([desc], cells)
        else:
            self._columnar_row([desc], cells)

    def summary_below(self, credit_total: Decimal, debit_total: Decimal) -> None:
        money = lambda v: _money(v, self.spec.country, peso=self.spec.country == "CO")
        self.ensure(2 * self.line_h + 2)
        self.y += 2
        self.line(MARGIN + 6, self.y + 3, f"Deposits & Other Credits +{money(credit_total)}", 8.5)
        self.y += self.line_h
        self.line(MARGIN + 6, self.y + 3, f"Withdrawals & Other Debits -{money(debit_total)}", 8.5)
        self.y += self.line_h

    def build(self) -> tuple[bytes, int]:
        self.new_page()
        self.preamble()
        txns, opening, closing = _txns(self.spec)
        credit_total = sum((-t.app_amount for t in txns if t.app_amount < 0), Decimal(0))
        debit_total = sum((t.app_amount for t in txns if t.app_amount > 0), Decimal(0))
        if self.spec.summary_above:
            self.summary_block(opening, credit_total, debit_total, closing)
        if (self.spec.opening_above and not self.spec.opening_inside and not self.spec.summary_above
                and not self.spec.no_opening):
            self.opening_line(opening)
        if self.spec.closing_above:
            self.closing_line(closing + Decimal(self.spec.closing_bias))
        self.header(self.y)
        self.y += 10 if self.spec.two_row else 8
        self.repeat_header = True
        if self.spec.opening_inside and not self.spec.no_opening:
            self.ensure(self.line_h)
            self.opening_row(opening)
        for txn in txns:
            self.row(txn)
        self.ensure(8)
        if not self.spec.no_closing:
            self.closing_line(closing + Decimal(self.spec.closing_bias))
        if self.spec.summary_below:
            self.summary_below(credit_total, debit_total)
        return bytes(self.pdf.output()), self.page_no


def build_statement(spec: Spec) -> tuple[bytes, int]:
    return _Doc(spec).build()


def _detail(txns: list[Txn]) -> list[dict]:
    return [{"date": t.day.isoformat(), "amount": _cents(t.app_amount), "description": " ".join(t.description)}
            for t in txns]


def _locked_pdf() -> bytes:
    spec = Spec("locked", "AU", "split", 4, 999, "1000.00", [],
                opening_inside=True, continuation=0.5)
    doc = _Doc(spec)
    doc.pdf.set_encryption(owner_password="owner-not-secret", user_password=LOCKED_PASSWORD,
                           encryption_method=EncryptionMethod.AES_128)
    data, _ = doc.build()
    return data


def _scan_pdf() -> bytes:
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_creation_date(CREATED)
    pdf.set_producer("hermes-finance pdfspike")
    pdf.set_creator("hermes-finance pdfspike")
    pdf.add_page()
    pdf.set_fill_color(210)
    pdf.rect(12, 12, 273, 186, style="F")
    return bytes(pdf.output())


def _empty_pdf() -> bytes:
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_creation_date(CREATED)
    pdf.set_producer("hermes-finance pdfspike")
    pdf.set_creator("hermes-finance pdfspike")
    pdf.add_page()
    pdf.set_font("helvetica", "B", 14)
    pdf.text(20, 24, "Harbourline Bank")
    pdf.set_font("helvetica", "", 10)
    pdf.text(20, 36, "Dear account holder,")
    pdf.text(20, 44, "There were no transactions on your account during this statement period.")
    pdf.text(20, 52, "You can download your transactions as CSV or OFX at any time.")
    return bytes(pdf.output())


def _right(pdf: FPDF, x: float, y: float, text: str) -> None:
    pdf.text(x - pdf.get_string_width(text), y, text)


def _multi_account_pdf() -> bytes:
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_creation_date(CREATED)
    pdf.set_producer("hermes-finance pdfspike")
    pdf.set_creator("hermes-finance pdfspike")
    pdf.add_page()
    pdf.set_font("helvetica", "B", 14)
    pdf.text(18, 20, "Harbourline Bank")
    label_y = 30
    for section, (opening, rows) in enumerate((
            ("2,000.00", [("02/03/2026", "Saltbush Cafe", "12.50", "", "1,987.50"),
                          ("05/03/2026", "Kookaburra Market", "44.10", "", "1,943.40")]),
            ("8,000.00", [("03/03/2026", "Blue Gum Transit", "9.80", "", "7,990.20"),
                          ("07/03/2026", "Harbourline Payroll", "", "2,500.00", "10,490.20")])), 1):
        pdf.set_font("helvetica", "B", 10)
        pdf.text(18, label_y, f"Account {section} statement")
        label_y += 6
        pdf.set_font("helvetica", "B", 8)
        pdf.text(18, label_y, "Date")
        pdf.text(55, label_y, "Description")
        pdf.text(180, label_y, "Debit")
        pdf.text(220, label_y, "Credit")
        pdf.text(262, label_y, "Balance")
        label_y += 5
        pdf.set_font("helvetica", "", 8.5)
        pdf.text(55, label_y, "Opening Balance")
        _right(pdf, 282, label_y, opening)
        label_y += 5
        for row in rows:
            pdf.text(18, label_y, row[0])
            pdf.text(55, label_y, row[1])
            pdf.text(200, label_y, row[2])
            pdf.text(240, label_y, row[3])
            _right(pdf, 282, label_y, row[4])
            label_y += 5
        label_y += 4
    return bytes(pdf.output())


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate(spec: Spec, txns: list[Txn], pages: int) -> None:
    checks = {
        "multi-line description": any(len(t.description) > 1 for t in txns),
        "cells stacked one-per-line": spec.stacked,
        "CR/DR suffix": spec.suffix,
        "DR/CR marker column": spec.marker,
        "separate debit/credit columns": spec.layout in ("split", "cl", "in", "reviewer"),
        "(+)/(-) token": spec.token,
        "trailing minus": spec.negative_style in ("trailing", "mixed"),
        "parentheses negative": spec.negative_style in ("paren", "mixed"),
        "preamble noise above the table": True,
        "repeated page header": pages > 1,
        "opening-balance row inside the table": spec.opening_inside,
        "summary/total lines with an explicit sign": spec.summary_above or spec.summary_below,
        "page break in the middle of the table": pages > 1 and spec.page_break,
        "Indian lakh grouping": spec.country == "IN" and any(abs(t.app_amount) >= 100000 for t in txns),
        "decimal comma + dot thousands": spec.country in ("BR", "CL"),
        "decimal dot + comma thousands": spec.country in ("AU", "IN", "SG", "US", "CO"),
        "ambiguous MDY/DMY date": spec.ambiguous,
        "non-amount parenthesised document number": spec.document_number,
        "header labels misaligned from values": spec.misaligned,
        "two-row header": spec.two_row,
        "dates without a year": spec.yearless,
        "no running-balance column": spec.no_balance,
    }
    for feature in spec.features:
        if not checks.get(feature):
            raise AssertionError(f"{spec.id} claims {feature!r} but the generated file does not have it")


def _manifest_item(out: Path, spec: Spec) -> dict:
    data, pages = build_statement(spec)
    txns, opening, closing = _txns(spec)
    _validate(spec, txns, pages)
    (out / f"{spec.id}.pdf").write_bytes(data)
    return {
        "id": spec.id,
        "country": spec.country,
        "profile": spec.country,
        "pages": pages,
        "expected_rows": len(txns),
        "opening_balance": _cents(opening),
        "closing_balance": _cents(closing + Decimal(spec.closing_bias)),
        "sha256": _sha(data),
        "features": spec.features,
        "expected_rows_detail": _detail(txns),
    }


def write_corpus(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    files = [_manifest_item(out, spec) for spec in SPECS]
    extra_files = [_manifest_item(out, spec) for spec in EXTRA_SPECS]
    controls = []
    for name, data in (("locked.pdf", _locked_pdf()), ("scan.pdf", _scan_pdf()),
                       ("multi_account.pdf", _multi_account_pdf()), ("empty.pdf", _empty_pdf())):
        (out / name).write_bytes(data)
        controls.append({"id": name.removesuffix(".pdf"), "file": name, "sha256": _sha(data)})
    manifest = {
        "generator": "tests/pdfspike_corpus.py",
        "format": "A4 landscape, text layer, fpdf2 core fonts; the x-reviewer-left extra is A4 portrait",
        "creation_date": CREATED.isoformat(),
        "files": files,
        "extra_files": extra_files,
        "negative_controls": controls,
        "required_features": list(REQUIRED_FEATURES),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


def enabled_features(manifest: dict) -> set[str]:
    items = list(manifest["files"]) + list(manifest.get("extra_files", []))
    return {f for item in items for f in item["features"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures/pdfspike"))
    args = parser.parse_args()
    manifest = write_corpus(args.out)
    missing = set(REQUIRED_FEATURES) - enabled_features(manifest)
    print(f"wrote {len(manifest['files'])} gating statements, {len(manifest['extra_files'])} extra fixtures and "
          f"{len(manifest['negative_controls'])} controls to {args.out}")
    print(f"features covered: {len(enabled_features(manifest))}/{len(REQUIRED_FEATURES)}")
    if missing:
        print("MISSING FEATURES: " + ", ".join(sorted(missing)))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

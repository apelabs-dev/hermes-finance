"""Read a bank's downloaded file (CSV, OFX, QFX) into plain rows, before anything touches the database.

Pure parsing, no I/O:
  * sniff() says what a file is ('csv' or 'ofx'), or why it can't be imported;
  * csv_preview() guesses the column mapping, date format and sign convention for the import sheet;
  * csv_rows() applies the mapping the user confirmed;
  * ofx_statements() reads OFX 1 (SGML) / OFX 2 (XML) / QFX, one OfxStatement per account in the file.

Every amount comes out in the app's sign: > 0 money left the account, < 0 money came in. Every problem is an
ImportFileError whose message is shown to the user as it is (plain words, never a traceback).
"""

from __future__ import annotations

import csv
import io
import itertools
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import PurePath

from ofxtools.Parser import OFXTree
from ofxtools.header import OFXHeaderError

# ofxtools logs every tag it reads at DEBUG (the transactions themselves) and chatters at INFO: keep bank data out of
# the app's log.
logging.getLogger("ofxtools").setLevel(logging.WARNING)

MAX_BYTES = 10 * 1024 * 1024      # 10 MB
SAMPLE_ROWS = 5

FIELDS = ("date", "description", "amount", "debit", "credit", "account")
SIGNS = ("minus_out", "minus_in", "split")
OFX_EXTS = frozenset({".ofx", ".qfx", ".qbo"})
SPREADSHEET_EXTS = frozenset({".xls", ".xlsx", ".xlsm", ".xlsb", ".ods", ".numbers"})

# Tried in this order; a file whose dates fit several (every day <= 12) gets them all in CsvPreview.date_formats.
DATE_FORMATS = (
    "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d", "%m/%d/%y", "%d/%m/%y", "%Y/%m/%d", "%m-%d-%Y", "%d-%m-%Y", "%m-%d-%y",
    "%d-%m-%y", "%d.%m.%Y", "%d.%m.%y", "%Y.%m.%d", "%Y%m%d", "%d %b %Y", "%d-%b-%Y", "%d-%b-%y", "%b %d, %Y",
    "%b %d %Y", "%d %B %Y", "%B %d, %Y",
)
MIN_DATE = date(1970, 1, 1)

_EMPTY = "This file is empty."
_TOO_BIG = "This file is too big: an import can be up to 10 MB. Download a shorter date range from your bank."
_NOT_TEXT = ("This file isn't text, so it isn't a CSV, OFX or QFX export. Download the transactions from your bank "
             "as CSV, OFX or QFX.")
_PDF = ("This is a PDF, which can't be imported. Download the transactions from your bank as CSV, OFX or QFX "
        "instead.")
_SPREADSHEET = ("This is a spreadsheet (Excel or similar). Save it as CSV first (File > Save As > CSV), then import "
                "the CSV file.")
_ZIP = "This is a ZIP archive. Unzip it first, then import the CSV, OFX or QFX file inside."
_QIF = "QIF files can't be imported. Download the transactions from your bank as CSV, OFX or QFX instead."
_NEITHER = "This file doesn't look like a bank export. Import a CSV, OFX or QFX file downloaded from your bank."
_NOT_OFX = "This {ext} file doesn't look like OFX (there's no <OFX> section in it). Download it again from your bank."
_NO_ROWS = "This CSV file has no transactions in it."
_BAD_QUOTES = "This CSV file couldn't be read: a quoted cell never ends (look for a stray \" in a description)."

_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"      # old Excel .xls (and .doc)
_BINARY_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"\x1f\x8b", b"7z\xbc\xaf", b"Rar!", b"BZh")
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_OFX_MARK = re.compile(r"OFXHEADER\s*[:=]|<\?OFX\b|<OFX>", re.I)
_OFX_TAG = re.compile(r"<OFX>|<STMTTRN>", re.I)


class ImportFileError(ValueError):
    """A problem with the file, in plain words for the user (never a traceback). `field` names the mapping
    field it's about when there is one ("date", "amount", ...), `row` the 1-based data row number when there is one."""

    def __init__(self, message: str, field: str | None = None, row: int | None = None):
        super().__init__(message)
        self.message = message
        self.field = field
        self.row = row


@dataclass(frozen=True)
class Row:
    date: str            # ISO YYYY-MM-DD
    description: str     # as written in the file, whitespace collapsed
    amount: float        # app sign: > 0 money out, < 0 money in; rounded to cents
    account: str | None  # the account label from the file (a CSV "Account" column, or the OFX account's label)
    fitid: str | None    # OFX FITID (None for CSV)
    line: int            # 1-based data row number (CSV) / transaction index in its statement (OFX)
    trntype: str | None = None  # OFX TRNTYPE, upper-case ("DIRECTDEP", "DEBIT", ...); None for CSV


@dataclass
class CsvPreview:
    headers: list[str]
    sample: list[list[str]]          # first SAMPLE_ROWS data rows, as text cells
    row_count: int                   # data rows (blank lines, repeated header lines and one-cell notes skipped)
    delimiter: str                   # ',' ';' '\t' or '|'
    guess: dict[str, int | None]     # FIELDS -> column index or None
    date_format: str | None          # the strptime format that parses every date in the guessed column
    date_formats: list[str]          # every candidate that does (2+ when ambiguous, e.g. %m/%d/%Y and %d/%m/%Y)
    sign: str                        # "minus_out" | "minus_in" | "split"
    accounts: list[tuple[str, int]]  # (account label, rows) when an account column is guessed, in first-seen order


@dataclass
class OfxStatement:
    account_label: str               # e.g. "Checking ••1234": the type + the last 4 of ACCTID, never the full number
    account_last4: str | None
    kind: str                        # "bank" or "credit_card"
    subtype: str | None              # checking | savings | money_market | cd for a bank statement, else None
    currency: str | None
    balance: float | None            # LEDGERBAL as an app balance (a card's balance owed as a POSITIVE number)
    balance_date: str | None         # ISO date
    rows: list[Row]


# --- bytes -> text ------------------------------------------------------------------------------------------------

def _check_bytes(data: bytes, ext: str = "") -> None:
    """Refuse what can't be a bank export before decoding anything: empty, too big, a PDF / spreadsheet / archive."""
    if not data or not data.strip():
        raise ImportFileError(_EMPTY)
    if len(data) > MAX_BYTES:
        raise ImportFileError(_TOO_BIG)
    if data.startswith(b"%PDF"):
        raise ImportFileError(_PDF)
    if data.startswith(_OLE2):
        raise ImportFileError(_SPREADSHEET)
    if data.startswith(b"PK\x03\x04"):
        # .xlsx / .ods / .numbers are ZIPs too: name the spreadsheet so the user knows to save it as CSV.
        if ext in SPREADSHEET_EXTS or b"xl/workbook" in data or b"opendocument.spreadsheet" in data[:512]:
            raise ImportFileError(_SPREADSHEET)
        raise ImportFileError(_ZIP)
    if data.startswith(_BINARY_MAGIC):
        raise ImportFileError(_NOT_TEXT)
    # UTF-16 (Excel's "Unicode text") is full of NULs but is text; anything else with a NUL is binary.
    if b"\x00" in data and not data.startswith(_UTF16_BOMS):
        raise ImportFileError(_NOT_TEXT)


def _decode(data: bytes) -> str:
    """UTF-8 (BOM or not), UTF-16 with a BOM, else cp1252 (what Windows banking exports use), else latin-1."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    elif data.startswith(_UTF16_BOMS):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            raise ImportFileError(_NOT_TEXT) from None
    for codec in ("utf-8", "cp1252"):
        try:
            return data.decode(codec)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _looks_binary(text: str) -> bool:
    """Control characters (latin-1 turns any byte into one) in more than 1% of the text: not a text export."""
    sample = text[:65536]
    bad = sum(1 for ch in sample
              if (ch < " " and ch not in "\t\n\r\f\v") or "\x7f" <= ch <= "\x9f" or ch == "\ufffd")
    return bad > max(2, len(sample) // 100)


def _text(data: bytes, ext: str = "") -> str:
    _check_bytes(data, ext)
    text = _decode(data)
    if not text.strip():
        raise ImportFileError(_EMPTY)
    if _looks_binary(text):
        raise ImportFileError(_NOT_TEXT)
    return text


def sniff(filename: str, data: bytes) -> str:
    """'csv' or 'ofx' (QFX / QBO are OFX): by content first, the extension second. ImportFileError when it's neither."""
    ext = PurePath(filename or "").suffix.lower()
    text = _text(data, ext)
    if _OFX_MARK.search(text[:4096]) or (ext in OFX_EXTS and _OFX_TAG.search(text)):
        return "ofx"
    if ext in SPREADSHEET_EXTS:        # e.g. an HTML table a bank names .xls: not something to map columns on
        raise ImportFileError(_SPREADSHEET)
    start = text.lstrip()[:16].lower()
    if start.startswith(("!type:", "!account", "!option")):
        raise ImportFileError(_QIF)
    neither = _NOT_OFX.format(ext=ext) if ext in OFX_EXTS else _NEITHER
    if start.startswith(("<", "{", "[")):      # HTML, XML that isn't OFX, JSON
        raise ImportFileError(neither)
    try:
        table = _read_table(text)
    except ImportFileError:
        raise ImportFileError(neither) from None
    if not table.rows:
        raise ImportFileError(_NO_ROWS)
    # A bank export has dates: a CSV-shaped file without any can't be imported (csv_rows needs one per row).
    if not any(_is_date(cell) for cells in table.rows[:20] for cell in cells):
        raise ImportFileError(neither)
    return "csv"


def normalize_description(text: str) -> str:
    """For de-duplication: "TRADER JOE'S #552 " and "Trader Joes 552" come out the same."""
    s = unicodedata.normalize("NFKC", text or "").casefold()
    s = re.sub(r"[^\w\s]|_", "", s)
    return " ".join(s.split())


# --- dates and amounts ----------------------------------------------------------------------------------------------

_TIME_TAIL = re.compile(r"[ T]+\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*(?:[AaPp]\.?[Mm]\.?)?\s*(?:Z|[+-]\d{2}:?\d{2})?$")


def _parse_date(cell: str, fmt: str) -> date | None:
    """The date in `cell` under `fmt`, ignoring a time of day after it ("09/26/2026 12:00:00", "2026-09-26T08:15Z")."""
    for s in (cell, _TIME_TAIL.sub("", cell)):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# What a date cell starts like (09/26/2026, 2026-09-26, 20260926, 26 Sep 2026, Sep 26, 2026): a cheap filter, so
# descriptions and amounts don't go through every strptime format.
_DATE_SHAPE = re.compile(r"\d{1,4}([/.\-])\d{1,2}\1\d{2,4}|\d{8}|\d{1,2}[ \-][A-Za-z]{3,9}[ \-]\d{2,4}|"
                         r"[A-Za-z]{3,9}\.? \d{1,2},? \d{4}")


def _is_date(cell: str) -> bool:
    return (len(cell) <= 40 and _DATE_SHAPE.match(cell) is not None
            and any(_parse_date(cell, fmt) for fmt in DATE_FORMATS))


def _human(fmt: str) -> str:
    """'%m/%d/%Y' -> 'MM/DD/YYYY', for error messages."""
    for directive, shown in (("%m", "MM"), ("%d", "DD"), ("%Y", "YYYY"), ("%y", "YY"), ("%b", "Mon"),
                             ("%B", "Month")):
        fmt = fmt.replace(directive, shown)
    return fmt


def _date_formats(values: list[str], dayfirst: bool, today: date) -> list[str]:
    """Every candidate format that parses all of `values`. Formats that put a date outside 1970..tomorrow are
    dropped when another one fits (that settles '10/09/2026' on 2026-09-28: it can only be Oct 9 later on)."""
    distinct = {v for v in values if v}
    if not distinct:
        return []
    # Month-first reads first, except in a ';' file (European exports), where the day comes first.
    order = sorted(DATE_FORMATS, key=lambda f: f.startswith("%m")) if dayfirst else list(DATE_FORMATS)
    latest = today + timedelta(days=1)
    fits: list[tuple[str, bool]] = []
    for fmt in order:
        plausible = True
        for value in distinct:
            parsed = _parse_date(value, fmt)
            if parsed is None:
                break
            plausible = plausible and MIN_DATE <= parsed <= latest
        else:
            fits.append((fmt, plausible))
    return [f for f, ok in fits if ok] or [f for f, _ in fits]


_CR_DR = re.compile(r"(?<![A-Za-z])(CR|DR)\.?$", re.I)
_CURRENCY_SIGN = re.compile(r"[A-Z]{0,2}\$|[€£¥₹]")
_CURRENCY_CODE = re.compile(r"^[A-Za-z]{3}\s*(?=[-+(\d.,])|(?<=[\d).])\s*[A-Za-z]{3}$")
_BLANK_AMOUNTS = frozenset({"", "-", "--", "\u2013", "\u2014"})


def _number(s: str, decimal_comma: bool) -> Decimal:
    """'1,234.56', '1.234,56', '1 234,56', "1'234.56" -> Decimal. `decimal_comma`: a ';' file, where '1.234' is 1234."""
    s = s.replace(" ", "").replace("'", "")
    if not re.fullmatch(r"[\d.,]*\d[\d.,]*", s):
        raise ValueError(s)
    if "," in s and "." in s:
        decimal = "," if s.rfind(",") > s.rfind(".") else "."
    elif "," in s:
        # '1,234' is a thousand in a US file; '64,18' is always a decimal comma.
        decimal = "." if not decimal_comma and re.fullmatch(r"\d{1,3}(,\d{3})+", s) else ","
    elif "." in s and decimal_comma and re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        decimal = ","
    else:
        decimal = "."
    thousands = "." if decimal == "," else ","
    whole, _, fraction = s.rpartition(decimal) if decimal in s else (s, "", "")
    if thousands in whole:
        # Real grouping only (1,234,567 or India's 12,34,567): '1.2.3,4' is a typo, not 123.4.
        t = re.escape(thousands)
        if not re.fullmatch(rf"\d{{1,3}}({t}\d{{3}})+|\d{{1,2}}({t}\d{{2}})*{t}\d{{3}}", whole):
            raise ValueError(s)
        whole = whole.replace(thousands, "")
    if not re.fullmatch(r"\d*", whole) or not re.fullmatch(r"\d*", fraction) or not (whole or fraction):
        raise ValueError(s)
    try:
        return Decimal(f"{whole or 0}.{fraction or 0}")
    except InvalidOperation:
        raise ValueError(s) from None


def _parse_amount(text: str, decimal_comma: bool = False) -> tuple[Decimal | None, str | None]:
    """(signed value as written, 'in' / 'out' when a CR / DR suffix says so); (None, None) for a blank cell.
    Raises ValueError when it isn't an amount."""
    s = text.strip().replace("\u2212", "-")
    for space in ("\u00a0", "\u202f", "\u2009"):
        s = s.replace(space, " ")
    if s in _BLANK_AMOUNTS:
        return None, None
    override = None
    m = _CR_DR.search(s)
    if m:
        override = "in" if m.group(1).upper() == "CR" else "out"
        s = s[:m.start()].strip()
    s = _CURRENCY_CODE.sub("", _CURRENCY_SIGN.sub("", s)).strip()
    negative = False
    if s.startswith("(") and s.endswith(")"):       # accounting style: (64.18) is -64.18
        negative, s = True, s[1:-1].strip()
    if s.startswith(("-", "+")):
        negative, s = negative or s[0] == "-", s[1:].strip()
    elif s.endswith("-"):                           # '64.18-'
        negative, s = True, s[:-1].strip()
    value = _number(s, decimal_comma)
    return (-value if negative else value), override


def _cents(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) + 0.0     # + 0.0: no -0.0


def _is_amount(cell: str) -> bool:
    try:
        return _parse_amount(cell)[0] is not None
    except ValueError:
        return False


# --- CSV ----------------------------------------------------------------------------------------------------------

_DELIMITERS = (",", ";", "\t", "|")
_SEP_LINE = re.compile(r"\s*sep=(.)[ \t]*\r?\n", re.I)     # Excel's "sep=;" hint line


@dataclass
class _Table:
    headers: list[str]
    rows: list[list[str]]      # data rows, padded to len(headers); rows[i] is data row i + 1
    delimiter: str


def _mode(counts: list[int]) -> int:
    """The most common count; a tie goes to the wider one."""
    return max(Counter(counts).items(), key=lambda kv: (kv[1], kv[0]))[0]


def _detect_delimiter(text: str) -> str | None:
    """The delimiter that splits the first rows consistently into the most columns ('1.234,56' amounts don't make
    a ';' file look like a ',' one: ';' gives more columns), or None."""
    best, best_key = None, None
    for delimiter in _DELIMITERS:
        reader = csv.reader(io.StringIO(text[:65536], newline=""), delimiter=delimiter)
        try:
            rows = list(itertools.islice(reader, 200))
        except csv.Error:
            continue
        counts = [len(r) for r in rows if any(c.strip() for c in r)]
        if not counts:
            continue
        width = _mode(counts)
        consistency = counts.count(width) / len(counts)
        if width < 2 or consistency < 0.5:
            continue
        key = (1, width, consistency) if consistency >= 0.75 else (0, consistency, width)
        if best_key is None or key > best_key:
            best, best_key = delimiter, key
    return best


def _looks_like_data(cells: list[str]) -> bool:
    """A date and an amount: a transaction, not a header."""
    dates = [c for c in cells if _is_date(c)]
    return bool(dates) and any(_is_amount(c) for c in cells if c not in dates)


def _read_table(text: str) -> _Table:
    """The header and the data rows. Skips a preamble above the header (an account summary), blank lines, repeated
    header lines and one-cell notes; a file that starts with a transaction gets 'Column 1', 'Column 2', ... headers."""
    delimiter = None
    m = _SEP_LINE.match(text)
    if m:
        delimiter, text = m.group(1), text[m.end():]
    delimiter = delimiter or _detect_delimiter(text)
    if delimiter is None:
        raise ImportFileError(_NEITHER)
    try:
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)]
    except csv.Error:
        raise ImportFileError(_BAD_QUOTES) from None
    rows = [r for r in rows if any(r)]
    wide = [len(r) for r in rows if len(r) >= 2]
    if not wide:
        raise ImportFileError(_NEITHER)
    # The table's width is the transactions' (a summary above them can have more lines than a short statement).
    transactions = [len(r) for r in rows[:200] if len(r) >= 2 and _looks_like_data(r)]
    width = _mode(transactions or wide)
    full = [r for r in rows if len(r) == width]
    if width >= 3 and all(r[-1] == "" for r in full):
        # Every row ends with a delimiter ("a;b;c;") and the header may not: drop that always-empty column.
        rows = [r[:-1] if len(r) == width else r for r in rows]
        width -= 1
    # The header: the first row at least as wide as the table with (nearly) every column named (a header can name a
    # last column the rows leave off); rows above it are a preamble.
    first = next((i for i, r in enumerate(rows[:50]) if len(r) >= width and sum(1 for c in r if c) >= max(2, width - 1)),
                 next(i for i, r in enumerate(rows) if len(r) == width))
    header = rows[first]
    headerless = _looks_like_data(header)
    if headerless:
        headers, data = [f"Column {i + 1}" for i in range(width)], rows[first:]
    else:
        width = max(width, len(header))
        headers = [h or f"Column {i + 1}" for i, h in enumerate(header)]
        data = rows[first + 1:]
    repeated = None if headerless else [h.casefold() for h in header]
    table = []
    for r in data:
        if sum(1 for c in r if c) < 2:                          # a note or a footer: no transaction has one cell
            continue
        if [c.casefold() for c in r] == repeated:               # the header again (exports glued together)
            continue
        table.append(r + [""] * (width - len(r)))
    return _Table(headers, table, delimiter)


def _norm_header(name: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", name.casefold()).split())


# Header names by field, best first. Posted dates win over transaction dates: OFX (DTPOSTED) and Plaid date
# transactions the day they post, so a CSV import de-duplicates against them on the same day.
_NAMES = {
    "date": ("posted date", "post date", "posting date", "date posted", "posted", "date", "transaction date",
             "trans date", "txn date", "value date", "booking date", "effective date", "settlement date"),
    "amount": ("amount", "transaction amount", "amt", "net amount", "amount usd", "value", "debit credit",
               "credit debit"),
    "debit": ("debit", "debits", "debit amount", "withdrawal", "withdrawals", "withdrawal amount", "money out",
              "paid out", "outflow", "charges", "charge"),
    "credit": ("credit", "credits", "credit amount", "deposit", "deposits", "deposit amount", "money in", "paid in",
               "inflow"),
    "description": ("description", "transaction description", "desc", "payee", "merchant", "merchant name", "name",
                    "payee name", "details", "transaction details", "narrative", "original description", "memo",
                    "reference"),
    "account": ("account", "account name", "acct", "account label", "card no", "card", "card name"),
}
# A header whose words start with one of these is that field too ("Debit (USD)", "Amount (EUR)").
_FIRST_WORDS = {"date": {"date"}, "amount": {"amount"}, "debit": {"debit", "withdrawal"},
                "credit": {"credit", "deposit"}, "description": {"description"}}


def _column(table: _Table, index: int) -> list[str]:
    return [r[index] for r in table.rows]


def _guess(table: _Table, today: date) -> dict[str, int | None]:
    names = [_norm_header(h) for h in table.headers]
    used: set[int] = set()

    def by_name(field: str) -> int | None:
        for candidate in _NAMES[field]:
            for i, name in enumerate(names):
                if i not in used and name == candidate:
                    return i
        starts = _FIRST_WORDS.get(field, set())
        for i, name in enumerate(names):
            words = name.split()
            if i not in used and words and words[0] in starts and len(words) <= 2 and "balance" not in words:
                return i
        return None

    def take(i: int | None) -> int | None:
        if i is not None:
            used.add(i)
        return i

    sample = table.rows[:50]
    guess: dict[str, int | None] = dict.fromkeys(FIELDS)
    guess["date"] = take(by_name("date"))
    guess["amount"] = take(by_name("amount"))
    if guess["amount"] is None:
        guess["debit"] = take(by_name("debit"))
        guess["credit"] = take(by_name("credit"))
    guess["description"] = take(by_name("description"))
    guess["account"] = take(by_name("account"))

    # No name to go by (a headerless file, a bank's own words): go by what the cells hold.
    free = [i for i in range(len(table.headers)) if i not in used and "bal" not in names[i]]
    if guess["date"] is None:
        guess["date"] = take(next((i for i in free if _date_formats([r[i] for r in sample], False, today)), None))
    if guess["amount"] is None and guess["debit"] is None and guess["credit"] is None:
        def numeric(i: int) -> bool:
            cells = [r[i] for r in sample if r[i]]
            return bool(cells) and sum(map(_is_amount, cells)) >= 0.8 * len(cells)
        guess["amount"] = take(next((i for i in free if i not in used and numeric(i)), None))
    if guess["description"] is None:
        letters = {i: sum(sum(ch.isalpha() for ch in r[i]) for r in sample) for i in free if i not in used}
        best = max(letters, key=lambda i: letters[i], default=None)
        guess["description"] = take(best if best is not None and letters[best] else None)
    return guess


# Descriptions that mean money came in: which sign they carry tells the convention.
_MONEY_IN = re.compile(r"\b(payroll|direct dep(osit)?|deposit|salary|refund|interest (paid|earned)|thank you|"
                       r"payment received|reversal|cash ?back)\b", re.I)


def _guess_sign(table: _Table, guess: dict[str, int | None]) -> str:
    amount, description = guess["amount"], guess["description"]
    if amount is None:
        return "split" if guess["debit"] is not None or guess["credit"] is not None else "minus_out"
    votes: Counter[str] = Counter()
    signs: Counter[bool] = Counter()
    for cells in table.rows:
        try:
            value, override = _parse_amount(cells[amount], table.delimiter == ";")
        except ValueError:
            continue
        if not value or override:
            continue
        signs[value > 0] += 1
        if description is not None and _MONEY_IN.search(cells[description]):
            votes["minus_out" if value > 0 else "minus_in"] += 1
    if votes["minus_out"] != votes["minus_in"]:
        return "minus_out" if votes["minus_out"] > votes["minus_in"] else "minus_in"
    # Mostly positive amounts: a card export (charges positive, the payment negative). A bank's are mostly minus.
    return "minus_in" if signs[True] > signs[False] else "minus_out"


def csv_preview(data: bytes, *, today: date | None = None) -> CsvPreview:
    """What the import sheet shows: headers, a sample, and a guess at the mapping, date format and sign."""
    table = _read_table(_text(data))
    if not table.rows:
        raise ImportFileError(_NO_ROWS)
    today = today or date.today()
    guess = _guess(table, today)
    formats = ([] if guess["date"] is None
               else _date_formats(_column(table, guess["date"]), table.delimiter == ";", today))
    accounts: dict[str, int] = {}
    if guess["account"] is not None:
        for label in _column(table, guess["account"]):
            if label:
                label = " ".join(label.split())
                accounts[label] = accounts.get(label, 0) + 1
    return CsvPreview(
        headers=table.headers,
        sample=[list(r) for r in table.rows[:SAMPLE_ROWS]],
        row_count=len(table.rows),
        delimiter=table.delimiter,
        guess=guess,
        date_format=formats[0] if formats else None,
        date_formats=formats,
        sign=_guess_sign(table, guess),
        accounts=list(accounts.items()),
    )


_FIELD_WORDS = {"date": "dates", "description": "descriptions", "amount": "amounts", "debit": "debits",
                "credit": "credits", "account": "account names"}


def csv_rows(data: bytes, mapping: dict[str, int | None], date_format: str, sign: str, *,
             today: date | None = None) -> list[Row]:
    """Every data row under the mapping the user confirmed, in the app's sign. The first bad row raises; a row with
    no amount at all (a balance line) is skipped, so Row.line can jump."""
    table = _read_table(_text(data))
    if not table.rows:
        raise ImportFileError(_NO_ROWS)
    width = len(table.headers)
    col: dict[str, int | None] = {}
    for key in FIELDS:
        index = mapping.get(key)
        if index is not None and (isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < width):
            raise ImportFileError(f"The column chosen for the {_FIELD_WORDS[key]} isn't in this file.", field=key)
        col[key] = index
    if col["date"] is None:
        raise ImportFileError("Choose the column with the dates.", field="date")
    if col["description"] is None:
        raise ImportFileError("Choose the column with the descriptions.", field="description")
    if sign not in SIGNS:
        raise ImportFileError("Choose how the amounts are signed.", field="sign")
    split = sign == "split" or col["amount"] is None      # a minus convention with only Debit / Credit: split
    if split and col["debit"] is None and col["credit"] is None:
        if col["amount"] is None:
            raise ImportFileError("Choose the Amount column (or the Debit and Credit columns).", field="amount")
        raise ImportFileError("Choose the Debit and/or Credit column.", field="debit")
    if not date_format or "%" not in date_format:
        raise ImportFileError("Choose the date format.", field="date")

    decimal_comma = table.delimiter == ";"
    latest = (today or date.today()) + timedelta(days=1)
    human = _human(date_format)
    parsed: dict[str, date | None] = {}
    out: list[Row] = []
    for n, cells in enumerate(table.rows, 1):
        amount_col = col["amount"]
        if split or amount_col is None:
            amount = _split_amount(cells, col, decimal_comma, n)
        else:
            amount = _signed_amount(cells[amount_col], sign, decimal_comma, n)
        if amount is None:      # no amount at all: a balance line ("Beginning balance as of ..."), not a transaction
            continue
        raw = cells[col["date"]]
        if not raw:
            raise ImportFileError(f"Row {n}: the date is blank.", field="date", row=n)
        if raw not in parsed:
            parsed[raw] = _parse_date(raw, date_format)
        day = parsed[raw]
        if day is None:
            raise ImportFileError(f"Row {n}: '{raw}' isn't a date in {human}.", field="date", row=n)
        if day < MIN_DATE:
            raise ImportFileError(f"Row {n}: '{raw}' is before 1970. Is the date format right?", field="date", row=n)
        if day > latest:
            raise ImportFileError(f"Row {n}: '{raw}' is in the future. Is the date format right?", field="date",
                                  row=n)
        account = " ".join(cells[col["account"]].split()) if col["account"] is not None else ""
        out.append(Row(day.isoformat(), " ".join(cells[col["description"]].split()), _cents(amount),
                       account or None, None, n))
    if not out:
        field = "amount" if not split else "debit" if col["debit"] is not None else "credit"
        raise ImportFileError("No row has an amount in the column chosen for the amounts.", field=field)
    return out


def _amount_cell(cell: str, decimal_comma: bool, n: int, field: str) -> tuple[Decimal | None, str | None]:
    try:
        return _parse_amount(cell, decimal_comma)
    except ValueError:
        raise ImportFileError(f"Row {n}: '{cell}' isn't an amount.", field=field, row=n) from None


def _signed_amount(cell: str, sign: str, decimal_comma: bool, n: int) -> Decimal | None:
    value, override = _amount_cell(cell, decimal_comma, n, "amount")
    if value is None:
        return None
    if override:                        # '64.18 CR' is money in whatever the file's convention
        return abs(value) if override == "out" else -abs(value)
    return -value if sign == "minus_out" else value


def _split_amount(cells: list[str], col: dict[str, int | None], decimal_comma: bool, n: int) -> Decimal | None:
    """Debit column = money out, Credit column = money in, written with or without a minus. None: both blank."""
    total, filled = Decimal(0), False
    for field, direction in (("debit", 1), ("credit", -1)):
        index = col[field]
        if index is None:
            continue
        value, override = _amount_cell(cells[index], decimal_comma, n, field)
        if value is None:
            continue
        filled = True
        if override:
            direction = 1 if override == "out" else -1
        total += direction * abs(value)
    return total if filled else None


# --- OFX / QFX ----------------------------------------------------------------------------------------------------

_STATEMENT_TAGS = frozenset({"STMTRS", "CCSTMTRS"})
# Inside a statement, these hold another account's number (a transfer's other side) or another balance.
_NOT_THE_STATEMENT = frozenset({"STMTTRN", "BANKACCTTO", "CCACCTTO", "AVAILBAL", "BALLIST", "STMTRS", "CCSTMTRS"})
_NOT_THE_TRANSACTION = frozenset({"STMTTRN", "BANKACCTTO", "CCACCTTO", "CURRENCY", "ORIGCURRENCY"})
_ACCOUNT_TYPES = {"CHECKING": ("Checking", "checking"), "SAVINGS": ("Savings", "savings"),
                  "MONEYMRKT": ("Money market", "money_market"), "CD": ("CD", "cd"),
                  "CREDITLINE": ("Credit line", None)}
_OFX_OPEN = re.compile(rb"<\s*OFX\s*>", re.I)
_LOWER_TAG = re.compile(rb"</?[a-z][A-Za-z0-9._]*>")
_TIDY_HEADER = (b"OFXHEADER:100\r\nDATA:OFXSGML\r\nVERSION:102\r\nSECURITY:NONE\r\nENCODING:UTF-8\r\nCHARSET:NONE\r\n"
                b"COMPRESSION:NONE\r\nOLDFILEUID:NONE\r\nNEWFILEUID:NONE\r\n\r\n")
_ENTITY = re.compile(r"&(amp|lt|gt|quot|apos|nbsp);|&#(\d{1,6});|&#[xX]([0-9a-fA-F]{1,6});")
_ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": " "}


def _unescape(text: str) -> str:
    """The XML entities OFX writes ('AT&amp;T'). Not html.unescape: it also eats a bare '&not' in 'SMITH&notary'."""
    def one(m: re.Match[str]) -> str:
        if m.group(1):
            return _ENTITIES[m.group(1)]
        code = int(m.group(2)) if m.group(2) else int(m.group(3), 16)
        return chr(code) if 0 < code < 0x110000 else m.group(0)
    return _ENTITY.sub(one, text)


def _tidy_ofx(data: bytes) -> bytes:
    """A header ofxtools accepts + the body from <OFX> on, re-encoded as UTF-8 with upper-case tags. For files whose
    own header it refuses (blank lines, lower-case values, no CHARSET, a BOM, header and <OFX> on one line)."""
    m = _OFX_OPEN.search(data)
    if not m:
        raise ImportFileError("This OFX file couldn't be read: there's no <OFX> section in it.")
    body = _decode(data[m.start():])
    body = re.sub(r"<!--.*?-->|<\?.*?\?>", "", body, flags=re.S)
    body = re.sub(r"<(/?)([A-Za-z][A-Za-z0-9._]*)>", lambda t: f"<{t.group(1)}{t.group(2).upper()}>", body)
    return _TIDY_HEADER + body.encode("utf-8")


def _parse_ofx(data: bytes) -> ET.Element:
    root = OFXTree().parse(io.BytesIO(data))
    # A header running into '<OFX>' on one line makes ofxtools skip the '<' and root the tree at the next tag.
    if root is None or root.tag != "OFX":
        raise ValueError("no <OFX> root")
    return root


def _ofx_root(data: bytes) -> ET.Element:
    # ofxtools silently skips lower-case tags (the transactions would vanish): those files go straight to the tidy.
    if not _LOWER_TAG.search(data):
        try:
            return _parse_ofx(data)
        except Exception:   # noqa: BLE001 - ofxtools raises a zoo (header, parse, unicode, index errors): retry tidied
            pass
    try:
        return _parse_ofx(_tidy_ofx(data))
    except ImportFileError:
        raise
    except OFXHeaderError:
        reason = "its header isn't valid OFX"
    except Exception:   # noqa: BLE001
        reason = "the tags in it are broken"
    raise ImportFileError(f"This OFX file couldn't be read: {reason}. Download it again from your bank, or use its "
                          "CSV export instead.")


def _find_all(elem: ET.Element, tags: frozenset[str], stop: frozenset[str] = frozenset()) -> list[ET.Element]:
    """Elements tagged `tags` under `elem` in document order, not looking inside a `stop` element."""
    found, todo = [], list(reversed(list(elem)))
    while todo:
        node = todo.pop()
        if node.tag in tags:
            found.append(node)
        if node.tag not in stop:
            todo.extend(reversed(list(node)))
    return found


def _leaves(elem: ET.Element, skip: frozenset[str]) -> Iterator[tuple[tuple[str, ...], str, str]]:
    """(ancestor tags, tag, text) for each value under `elem`, in document order, not inside a `skip` element.
    By ancestors, not by exact path: an empty SGML leaf ('<MEMO>' with no value) swallows the tags after it."""
    todo = [(child, ()) for child in reversed(list(elem))]
    while todo:
        node, path = todo.pop()
        if node.tag in skip:
            continue
        text = (node.text or "").strip()
        if text:
            yield path, node.tag, text
        todo.extend((child, (*path, node.tag)) for child in reversed(list(node)))


def _ofx_date(text: str | None) -> str | None:
    """'20260926', '20260926120000', '20260926120000.000[-5:EST]' -> '2026-09-26'."""
    m = re.match(r"\s*(\d{4})(\d{2})(\d{2})", text or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def _ofx_amount(text: str | None) -> Decimal | None:
    try:
        return _parse_amount(text or "")[0]
    except ValueError:
        return None


def _statement(stmt: ET.Element) -> OfxStatement:
    card = stmt.tag == "CCSTMTRS"
    acctid = accttype = currency = balamt = dtasof = None
    for path, tag, text in _leaves(stmt, _NOT_THE_STATEMENT):
        if tag == "ACCTID" and acctid is None and ("BANKACCTFROM" in path or "CCACCTFROM" in path):
            acctid = text
        elif tag == "ACCTTYPE" and accttype is None and "BANKACCTFROM" in path:
            accttype = text.upper()
        elif tag == "CURDEF" and currency is None:
            currency = text.upper()
        elif tag == "BALAMT" and balamt is None and "LEDGERBAL" in path:
            balamt = text
        elif tag == "DTASOF" and dtasof is None and "LEDGERBAL" in path:
            dtasof = text
    last4 = re.sub(r"[^0-9A-Za-z]", "", acctid or "")[-4:] or None
    if card:
        name, subtype = "Credit card", None
    else:
        name, subtype = _ACCOUNT_TYPES.get(accttype or "", ("Bank account", None))
    label = f"{name} ••{last4}" if last4 else name
    balance = _ofx_amount(balamt)
    if balance is not None and card:
        balance = -balance          # OFX: what a card owes is negative; the app shows it owed as positive

    rows = []
    for i, trn in enumerate(_find_all(stmt, frozenset({"STMTTRN"}), _STATEMENT_TAGS), 1):
        values: dict[str, str] = {}
        for path, tag, text in _leaves(trn, _NOT_THE_TRANSACTION):
            values.setdefault("PAYEE/NAME" if tag == "NAME" and "PAYEE" in path else tag, text)
        posted = _ofx_date(values.get("DTPOSTED") or values.get("DTUSER"))
        if posted is None:
            raise ImportFileError(f"Transaction {i} of {label} has no date that can be read.", field="date", row=i)
        amount = _ofx_amount(values.get("TRNAMT"))
        if amount is None:
            raise ImportFileError(f"Transaction {i} of {label}: '{values.get('TRNAMT', '')}' isn't an amount.",
                                  field="amount", row=i)
        description = next((v for v in (values.get("NAME"), values.get("PAYEE/NAME"), values.get("MEMO")) if v), "")
        fitid = values.get("FITID")
        trntype = values.get("TRNTYPE", "").strip().upper() or None
        rows.append(Row(posted, " ".join(_unescape(description).split()), _cents(-amount), label,
                        _unescape(fitid) if fitid else None, i, trntype))   # OFX TRNAMT is negative for money out
    return OfxStatement(label, last4, "credit_card" if card else "bank", subtype, currency,
                        None if balance is None else _cents(balance), _ofx_date(dtasof), rows)


def ofx_statements(data: bytes) -> list[OfxStatement]:
    """Every bank / credit card statement in an OFX or QFX file, in file order."""
    _check_bytes(data, ".ofx")
    root = _ofx_root(data)
    statements = [_statement(stmt) for stmt in _find_all(root, _STATEMENT_TAGS)]
    if not statements:
        if _find_all(root, frozenset({"INVSTMTRS"})):
            raise ImportFileError("This OFX file is an investment statement: only bank and credit card statements "
                                  "can be imported.")
        raise ImportFileError("This OFX file has no bank or credit card statement in it.")
    return statements

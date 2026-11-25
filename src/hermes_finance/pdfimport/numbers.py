"""Money and date tokens as a statement prints them (pure functions, no I/O).

Money keeps the printed sign only in `explicit`; whether an unsigned figure is money in or out is a layout decision
(see layout.py). Dates stay raw until a statement period or an explicit year fixes the day/month order and the year.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

MINUS = "-\u2212\u2013\u2012"     # hyphen, unicode minus, en dash, figure dash
_MC = "\\-\u2212\u2013\u2012"      # the same inside a character class (a bare '+-' would be a range)
_CUR = r"(?:[$€£¥₹]|US\$|C\$|A\$|S\$|R\$|USD|EUR|CAD|AUD|GBP|INR|SGD|CHF|HKD)"

# Exactly two decimals. The thousands separator must differ from the decimal one.
_BODY = (r"(?P<body>"
         r"\d{1,3}(?:,\d{3})+\.\d{2}"           # 1,234.56
         r"|\d{1,2}(?:,\d{2})+,\d{3}\.\d{2}"    # 12,34,567.89 (Indian)
         r"|\d{1,3}(?:\.\d{3})+,\d{2}"          # 1.234,56
         r"|\d{1,3}(?:[  \u202f'\u2019]\d{3})+[.,]\d{2}"   # 1 234,56 / 1'234.56
         r"|\d+[.,]\d{2}"                       # 1234.56 / 1234,56
         r"|[.,]\d{2}"                          # .15
         r")")
MONEY_RE = re.compile(
    rf"^(?P<pre>[+{_MC}(]?)\s?{_CUR}?\s?(?P<pre2>[+{_MC}]?)\s?{_BODY}\s?{_CUR}?"
    rf"(?P<post>\)|[+{_MC}]|\s?(?:CR|DR|Cr|Dr|cr|dr|C|D|S|H|OD))?\.?$")


@dataclass(frozen=True)
class Money:
    value: Decimal          # signed by explicit markers only (unsigned -> positive)
    explicit: str | None    # "neg" | "pos" | None
    text: str


def parse_money(text: str) -> Money | None:
    t = (text or "").strip()
    m = MONEY_RE.match(t)
    if not m:
        return None
    body = m.group("body")
    pre, pre2, post = m.group("pre") or "", m.group("pre2") or "", (m.group("post") or "").strip()
    if pre == "(" and post != ")" or post == ")" and pre != "(":
        return None
    dec = body[-3]
    digits = body[:-3]
    for sep in ", .\u00a0\u202f'":
        if sep != dec:
            digits = digits.replace(sep, "")
    if dec in digits:
        return None
    value = Decimal((digits or "0") + "." + body[-2:])
    neg = pre == "(" or (pre and pre in MINUS) or (pre2 and pre2 in MINUS) or (post and post in MINUS) \
        or post.upper() in ("DR", "D", "S", "OD")
    pos = pre == "+" or pre2 == "+" or post == "+" or post.upper() in ("CR", "C", "H")
    if neg and pos:
        return None
    explicit = "neg" if neg else ("pos" if pos else None)
    return Money(-value if neg else value, explicit, t)


def money_like(text: str) -> bool:
    """A token that looks like a money figure (two decimals) even if parse_money rejects its decoration."""
    return bool(re.search(r"\d[.,]\d{2}(?!\d)", text or "")) and not DATE_NUM_RE.fullmatch((text or "").strip("()"))


# ---------------------------------------------------------------- dates

MONTHS = {
    "jan": 1, "january": 1, "januar": 1, "janvier": 1, "enero": 1, "jänner": 1, "janv": 1,
    "feb": 2, "february": 2, "februar": 2, "février": 2, "fevrier": 2, "febrero": 2, "févr": 2, "fev": 2,
    "mar": 3, "march": 3, "märz": 3, "maerz": 3, "mars": 3, "marzo": 3, "mrz": 3, "maart": 3, "mrt": 3,
    "apr": 4, "april": 4, "avril": 4, "abril": 4, "avr": 4,
    "may": 5, "mai": 5, "mayo": 5, "mei": 5,
    "jun": 6, "june": 6, "juni": 6, "juin": 6, "junio": 6,
    "jul": 7, "july": 7, "juli": 7, "juillet": 7, "julio": 7, "juil": 7,
    "aug": 8, "august": 8, "août": 8, "aout": 8, "agosto": 8, "augustus": 8,
    "sep": 9, "sept": 9, "september": 9, "septembre": 9, "septiembre": 9,
    "oct": 10, "october": 10, "oktober": 10, "octobre": 10, "octubre": 10, "okt": 10,
    "nov": 11, "november": 11, "novembre": 11, "noviembre": 11,
    "dec": 12, "december": 12, "dezember": 12, "décembre": 12, "decembre": 12, "diciembre": 12, "dez": 12,
}
DATE_NUM_RE = re.compile(r"(\d{1,4})([./-])(\d{1,2})(?:\2(\d{2,4}))?\.?")
_MON = r"([A-Za-zÀ-ÿ]{3,9})\.?"


@dataclass(frozen=True)
class RawDate:
    """A date token before the day/month order is fixed. kind: 'num' (a, b, y?) / 'text' (day, month, y?) / 'ymd'."""
    kind: str
    a: int
    b: int
    year: int | None
    sep: str
    text: str


def _year(y: str | None) -> int | None:
    if y is None:
        return None
    n = int(y)
    return n + 2000 if n < 100 else n


def parse_date_prefix(text: str) -> tuple[RawDate, str] | None:
    """Parse a date at the start of `text` (words joined by spaces). Returns (RawDate, rest)."""
    t = (text or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?![\d])", t)
    if m:
        return RawDate("ymd", int(m.group(2)), int(m.group(3)), int(m.group(1)), "-", m.group(0)), t[m.end():]
    m = re.match(r"(\d{1,2})([./-])(\d{1,2})(?:\2(\d{4}|\d{2}))?\.?(?![\d.,/-]?\d)", t)
    if m and 1 <= int(m.group(1)) <= 31 and 1 <= int(m.group(3)) <= 31:
        return RawDate("num", int(m.group(1)), int(m.group(3)), _year(m.group(4)), m.group(2), m.group(0)), t[m.end():]
    # 05 Jan 2024 / 5 January / 05-Jan-24 / 1. Juni 2021
    m = re.match(rf"(\d{{1,2}})\.?[ -]{_MON}(?:[ -,]+(\d{{4}}|\d{{2}}(?![\d.,])))?", t)
    if m and m.group(2).lower() in MONTHS:
        return (RawDate("text", int(m.group(1)), MONTHS[m.group(2).lower()], _year(m.group(3)), " ", m.group(0)),
                t[m.end():])
    # Jan 5, 2024 / May 1 / January 05
    m = re.match(rf"{_MON}[ -](\d{{1,2}})(?![\d.,]\d)(?:,?[ -](\d{{4}}))?", t)
    if m and m.group(1).lower() in MONTHS:
        return (RawDate("text", int(m.group(2)), MONTHS[m.group(1).lower()], _year(m.group(3)), " ", m.group(0)),
                t[m.end():])
    return None


def resolve(raw: RawDate, order: str, year: int | None) -> date | None:
    """A RawDate under a day/month order ('DMY' | 'MDY'), with `year` used when the token has none."""
    if raw.kind in ("text", "ymd"):
        d, mth = (raw.a, raw.b) if raw.kind == "text" else (raw.b, raw.a)
    elif order == "DMY":
        d, mth = raw.a, raw.b
    elif order == "MDY":
        d, mth = raw.b, raw.a
    else:
        return None
    y = raw.year if raw.year is not None else year
    if y is None:
        return None
    try:
        return date(y, mth, d)
    except ValueError:
        return None


def year_of(text: str) -> int | None:
    """The first four-digit year in a header line, if any."""
    m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text or "")
    return int(m.group(1)) if m else None

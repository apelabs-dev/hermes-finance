"""Shared money formatting for human-facing text; numeric payloads stay numeric.

Amounts are never converted: a currency code only decides how the number is written (its official
ISO 4217 exponent, and a symbol when the app knows one). Display currency comes from Settings >
Region; foreign amounts keep their own code end to end.
"""

from decimal import Decimal, ROUND_HALF_UP

try:   # the core dependency; the Hermes plugin's own Python 3.11 has no site-packages for it
    from iso4217 import Currency as _Currency
except ImportError:  # pragma: no cover - the fallback is the same table, generated from iso4217 1.16.20260101
    _Currency = None

# The same official exponents, used only where the package can't be imported (the plugin runs on Hermes's Python).
_FALLBACK = ("AFN:2,EUR:2,ALL:2,DZD:2,USD:2,AOA:2,XCD:2,XAD:2,ARS:2,AMD:2,AWG:2,AUD:2,AZN:2,BSD:2,BHD:3,BDT:2,BBD:2,BYN:2,BZD:2,XOF:0,BMD:2,INR:2,BTN:2,BOB:2,BOV:2,BAM:2,BWP:2,NOK:2,BRL:2,BND:2,BIF:0,CVE:2,KHR:2,XAF:0,CAD:2,KYD:2,CLP:0,CLF:4,CNY:2,COP:2,COU:2,KMF:0,CDF:2,NZD:2,CRC:2,CUP:2,XCG:2,CZK:2,DKK:2,DJF:0,DOP:2,EGP:2,SVC:2,ERN:2,SZL:2,ETB:2,FKP:2,FJD:2,XPF:0,GMD:2,GEL:2,GHS:2,GIP:2,GTQ:2,GBP:2,GNF:0,GYD:2,HTG:2,HNL:2,HKD:2,HUF:2,ISK:0,IDR:2,XDR:2,IRR:2,IQD:3,ILS:2,JMD:2,JPY:0,JOD:3,KZT:2,KES:2,KPW:2,KRW:0,KWD:3,KGS:2,LAK:2,LBP:2,LSL:2,ZAR:2,LRD:2,LYD:3,CHF:2,MOP:2,MKD:2,MGA:2,MWK:2,MYR:2,MVR:2,MRU:2,MUR:2,XUA:2,MXN:2,MXV:2,MDL:2,MNT:2,MAD:2,MZN:2,MMK:2,NAD:2,NPR:2,NIO:2,NGN:2,OMR:3,PKR:2,PAB:2,PGK:2,PYG:0,PEN:2,PHP:2,PLN:2,QAR:2,RON:2,RUB:2,RWF:0,SHP:2,WST:2,STN:2,SAR:2,RSD:2,SCR:2,SLE:2,SGD:2,XSU:2,SBD:2,SOS:2,SSP:2,LKR:2,SDG:2,SRD:2,SEK:2,CHE:2,CHW:2,SYP:2,TWD:2,TJS:2,TZS:2,THB:2,TOP:2,TTD:2,TND:3,TRY:2,TMT:2,UGX:0,UAH:2,AED:2,USN:2,UYU:2,UYI:0,UYW:4,UZS:2,VUV:0,VES:2,VED:2,VND:0,YER:2,ZMW:2,ZWG:2,XBA:2,XBB:2,XBC:2,XBD:2,XTS:2,XXX:2,XAU:2,XPD:2,XPT:2,XAG:2")
_FALLBACK_EXPONENTS = {c.split(":")[0]: int(c.split(":")[1]) for c in _FALLBACK.split(",")}

# A short symbol list, not a conversion table. Anything else is written as its code ("KRW 1,200"),
# so a currency the app has no symbol for stays unmistakable.
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}
DEFAULT = "USD"


def code(value) -> str | None:
    """A non-empty currency identifier as stored: trimmed, upper-case ISO codes and custom ids alike."""
    if value is None:
        return None
    text = str(value).strip()
    return text.upper() if text else None


def is_iso(value) -> bool:
    """True for an official ISO 4217 code (case-insensitive); False for a provider's custom identifier."""
    c = code(value)
    if c is None:
        return False
    if _Currency is None:
        return c in _FALLBACK_EXPONENTS
    try:
        _Currency(c)
    except (ValueError, KeyError):
        return False
    return True


def exponent(value) -> int:
    """The official number of decimals (JPY 0, most 2, KWD/BHD/JOD 3). A custom id uses 2, unchanged."""
    c = code(value) or ""
    if _Currency is None:
        return _FALLBACK_EXPONENTS.get(c, 2)
    try:
        places = _Currency(c).exponent
    except (ValueError, KeyError):
        return 2
    return places if places is not None else 2


def name(value) -> str | None:
    """The ISO 4217 name ("US Dollar"), or None for a custom identifier."""
    if _Currency is None:
        return None
    try:
        return _Currency(code(value) or "").name
    except (ValueError, KeyError):
        return None


def _prefix(currency_code: str) -> str:
    symbol = SYMBOLS.get(currency_code)
    return symbol if symbol else f"{currency_code} "


def money(value, cents: bool = False, none: str = "-", currency=None) -> str:
    """Format an amount in `currency` (None: USD, the legacy default), round half up on the magnitude.

    The ISO exponent decides the decimals: JPY never shows cents, KWD always three places. ``cents``
    always keeps the currency's full precision. A code with no symbol is written as ``KRW 1,200``.
    Suppress a negative sign when the rounded amount is zero.
    """
    if value is None:
        return none
    try:
        amount = Decimal(str(value))
    except Exception:  # noqa: BLE001 - text that isn't a number reads as none, like a missing value
        return none
    if not amount.is_finite():
        return none
    currency_code = code(currency) or DEFAULT
    places = exponent(currency_code)
    quantum = Decimal(1).scaleb(-places)
    amount = amount.quantize(quantum, rounding=ROUND_HALF_UP)
    sign = "-" if amount < 0 else ""
    magnitude = abs(amount)
    digits = places if (cents or magnitude != magnitude.to_integral_value()) else 0
    return f"{sign}{_prefix(currency_code)}{magnitude:,.{digits}f}"


def money2(value, none: str = "-", currency=None) -> str:
    """Format an amount with the currency's full number of decimal places."""
    return money(value, cents=True, none=none, currency=currency)

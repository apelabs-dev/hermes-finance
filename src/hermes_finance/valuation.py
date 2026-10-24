"""Automatic car and property values (TASK-12, TASK-13).

  * VIN decode: NHTSA vPIC's extended decode (free, no key), once per VIN when a vehicle is saved: fills year / make /
    model / trim the user left blank (they can edit them after) and keeps the specs and safety equipment for the page.
  * Car value: Carfax's own free value page (carfax.com/value): trade-in (its `wholesalePrice`), private sale and dealer
    retail, from the real VIN, mileage, ZIP and condition. Its API sits behind bot protection, so the lookup runs inside
    the local Chrome (cdp_client: a new background tab on carfax.com, two in-page fetches, the tab closed).
  * Property value: the Redfin Estimate (free, no key; plain HTTPS first, the same Chrome tab approach if that's
    refused). A condo unit is only ever valued from its own unit's record: never the building's, never a neighbour's.
    The same counted lookup notes the listing photo's address (aboveTheFold) for the page.

Values are fetched when a vehicle / property is saved with what the provider needs, on "Refresh" (one per account per
day) and by `finance revalue` in the daily run (one per account per 30 days). Each success is a row in
asset_valuations. The account's balance follows effective() while value_source is 'estimate'; a value the user types
wins (value_source 'user', "Your value") until "Use estimate again". A vehicle's estimate is its latest Carfax
trade-in, a property's its latest Redfin Estimate; without one there is no estimate. (Rows another provider wrote
before TASK-13b, MarketCheck or RentCast, stay in the table as history and are never shown or used.)

Every outbound call goes through usage.reserve() first (the free-tier guard: counted before it's sent, capped, never
retried; a bot-protection answer stops Carfax / Redfin for config.BLOCK_DAYS days, a refusal stops NHTSA for the rest
of the month). Carfax is only counted once Chrome is known to be there: without it the lookup is skipped ("Chrome not
running"), sending and counting nothing.

HTTP goes through `transport` and the browser through `browser` (tests replace both; they never reach a real provider
or the real Chrome).
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import re
import socket
import sqlite3
import urllib.error
import urllib.request
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from . import cdp_client, config, manual, redact, usage
from .money import money
from .db import now

log = logging.getLogger(__name__)

VALUE_KINDS = manual.VALUE_KINDS
SOURCE_OF = {"vehicle": "carfax", "property": "redfin"}   # the one value source of each kind
SOURCES = tuple(SOURCE_OF.values())
# What each provider looks at: an edit that changes one of these asks it again.
WATCHES = {"carfax": ("vin", "mileage", "zip_code", "vehicle_condition", "vehicle_trim"), "redfin": ("address",)}
NHTSA_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValuesExtended/{vin}?format=json"
CARFAX_PAGE = "https://www.carfax.com/value/"
CARFAX_TRIMS_URL = "https://consumerapi.carfax.com/hbv/cow/trims/{vin}"
CARFAX_PRICES_URL = "https://consumerapi.carfax.com/hbv/cow/prices"
REDFIN_PAGE = "https://www.redfin.com/"
REDFIN_AUTOCOMPLETE_URL = "https://www.redfin.com/stingray/do/location-autocomplete?location={q}&v=2"
REDFIN_AVM_URL = "https://www.redfin.com/stingray/api/home/details/avm?propertyId={pid}&accessLevel=1"
REDFIN_ABOVE_URL = "https://www.redfin.com/stingray/api/home/details/aboveTheFold?propertyId={pid}&accessLevel=1"
REDFIN_PHOTO_HOSTS = (".cdn-redfin.com",)   # a listing photo comes from Redfin's CDN; never a Street View URL (Redfin's key)
TIMEOUT = 10.0
MAX_BYTES = 512 * 1024
USER_AGENT = "hermes-finance/0.1 (personal net worth; one lookup per account per month)"
# Redfin's plain-HTTPS try looks like the browser it stands in for (its public pages make the same requests).
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/153.0.0.0 Safari/537.36",
                   "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}
RAW_MAX = 4000              # characters of trimmed response JSON kept per valuation
NUDGE_DAYS = 90             # "Update mileage for a better estimate"
# words in a refusal that mean "out of quota": stop for the month
QUOTA_WORDS = re.compile(r"quota|limit exceeded|exceeded|not (?:allowed|authori[sz]ed|permitted)", re.I)
# a bot-protection answer (DataDome's captcha page, or its JSON pointing at one)
CAPTCHA = re.compile(r"captcha|datadome|are you a (?:human|robot)", re.I)
SHAPE_ERRORS = (KeyError, TypeError, ValueError, AttributeError, IndexError)


# --- VIN (ISO 3779; the 9th character is the North American check digit) --------------------------------------

VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
_VIN_VALUES = {**{str(d): d for d in range(10)},
               **dict(zip("ABCDEFGH", range(1, 9))), **dict(zip("JKLMN", range(1, 6))), "P": 7, "R": 9,
               **dict(zip("STUVWXYZ", range(2, 10)))}
_VIN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)


def vin_check_digit(vin: str) -> str:
    total = sum(_VIN_VALUES[c] * w for c, w in zip(vin, _VIN_WEIGHTS))
    return "X" if total % 11 == 10 else str(total % 11)


def vin_problem(vin: str) -> str | None:
    """Why `vin` (already uppercased, spaces removed) isn't a valid VIN, or None."""
    if len(vin) != 17:
        return f"A VIN has 17 characters (this has {len(vin)})"
    bad = sorted({c for c in vin if c in "IOQ"})
    if bad:
        return f"A VIN never has the letters I, O or Q ({', '.join(bad)} here: maybe 1 or 0?)"
    if not VIN_RE.fullmatch(vin):
        return "A VIN has only letters and digits"
    if vin[8] != vin_check_digit(vin):
        return "That VIN doesn't check out (its 9th character, the check digit, doesn't match): look for a typo"
    return None


# --- HTTP and the browser --------------------------------------------------------------------------------------

Transport = Callable[..., tuple[int, bytes]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # an API answers, it doesn't redirect: a 3xx is a failure
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(url: str, headers: dict[str, str], timeout: float, max_bytes: int = MAX_BYTES) -> tuple[int, bytes]:
    """GET `url` (https only). An HTTP error status comes back as (status, body); network trouble raises OSError
    (TimeoutError for a timeout). `headers` may replace the default User-Agent / Accept; `max_bytes`: how much of the
    body is read (an image needs more than an API answer)."""
    if not url.startswith("https://"):
        raise ValueError("https only")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **headers})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(max_bytes)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(min(max_bytes, MAX_BYTES))
        except OSError:
            body = b""
        finally:
            e.close()
        return e.code, body
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            raise TimeoutError("timed out") from None
        raise OSError(type(e.reason).__name__ if e.reason is not None else "URLError") from None
    except http.client.HTTPException as e:  # a broken response (IncompleteRead, BadStatusLine): not an OSError
        raise OSError(type(e).__name__) from None


transport: Transport = urllib_transport
# The local Chrome (127.0.0.1:9222): available() -> None | why not; tab(url) -> a context manager giving a Tab whose
# fetch(url, method, headers, body) -> (status, text) runs inside that page.
browser = cdp_client.Chrome()


class ProviderError(Exception):
    """A call was made (and counted) and gave no value. `kind`: timeout | network | http | no_price | bad_response |
    blocked (bot protection) | browser (Chrome failed mid-lookup) | no_estimate (answered: none for this home)."""

    def __init__(self, provider: str, kind: str, message: str, status: int | None = None,
                 blocked_until: str | None = None):
        super().__init__(message)
        self.provider, self.kind, self.message, self.status, self.blocked_until = (provider, kind, message, status,
                                                                                    blocked_until)


class Unavailable(Exception):
    """The browser a lookup needs isn't there (Chrome not running): nothing was sent, nothing was counted."""

    def __init__(self, provider: str, message: str):
        super().__init__(message)
        self.provider, self.message = provider, message


def _body_text(body: bytes) -> str:
    """The whole body as text (tags stripped, the first 4 KB), scrubbed: what the quota words are looked for in."""
    text = re.sub(r"<[^>]*>", " ", body[:4096].decode("utf-8", "replace"))
    return " ".join(redact.scrub(text).split())


def _body_message(body: bytes) -> str:
    """The provider's own words from an error body ({"message": ...}, {"error": ...}), scrubbed, then shortened."""
    try:
        data = json.loads(body or b"null")
    except ValueError:
        data = None
    text = ""
    if isinstance(data, dict):
        for k in ("message", "error", "msg", "detail", "description", "Message", "reason"):
            if isinstance(data.get(k), str) and data[k].strip():
                text = data[k]
                break
            if isinstance(data.get(k), dict) and isinstance(data[k].get("message"), str):
                text = data[k]["message"]
                break
    elif not body.lstrip().startswith(b"<"):
        text = body[:400].decode("utf-8", "replace")
    return " ".join(redact.scrub(str(text)).split())[:140]


def _refusal(provider: str, status: int, said: str, whole: str = "") -> tuple[str, bool]:
    """(message, stop the provider?) for an error status of a plain API call (NHTSA, Wikimedia Commons, OpenStreetMap).
    A refusal (401 / 403, or quota words) stops it until usage.block_end(); a 429 alone is rate-limiting."""
    name = usage.LABELS[provider]
    tail = f": {said}" if said else ""
    quota = bool(QUOTA_WORDS.search(said) or QUOTA_WORDS.search(whole))
    if status in (401, 403):
        return f"{name} refused the request (HTTP {status}){tail}", True
    if status == 429:
        return (f"{name} says the quota is used up (HTTP 429){tail}", True) if quota else \
               (f"{name} is rate-limiting (HTTP 429){tail}; not retried", False)
    if quota:
        return f"{name} refused: quota (HTTP {status}){tail}", True
    if status >= 500:
        return f"{name} had a server error (HTTP {status}); not retried", False
    return f"{name} couldn't answer (HTTP {status}){tail}", False


# A block that couldn't be written (the DB stayed locked): kept for this process, so it never asks again anyway.
_process_blocks: dict[str, str] = {}


def _settle(conn: sqlite3.Connection, res: usage.Reservation, today: date, outcome: str, status: int | None,
            note: str, stop: bool) -> str | None:
    """Record how the call went and, for a refusal that stops the provider, the block, in one local transaction
    (retried a few times if the DB is busy: a local write, never the call). Returns the day the block lasts until."""
    for attempt in range(3):
        try:
            return usage.finish(conn, res, "blocked" if stop else outcome, status, note, block_reason=note if stop else None,
                                today=today)
        except sqlite3.OperationalError:
            if attempt == 2:
                if stop:
                    until = usage.block_end(res.provider, today).isoformat()
                    _process_blocks[res.provider] = until
                    log.error("%s: could not save the block (database busy); blocked in this process until %s",
                              usage.LABELS[res.provider], until)
                    return until
                return None
    return None


def not_paused(provider: str, today: date) -> None:
    if _process_blocks.get(provider, "") > today.isoformat():
        raise usage.Refused(provider, "blocked", f"{usage.LABELS[provider]} is paused until {_process_blocks[provider]}")


def request(conn: sqlite3.Connection, provider: str, url: str, headers: dict[str, str], today: date,
            account_id: int | None, trigger: str, parse: Callable[[dict[str, Any]], Any] | None = None,
            ids: list[int] | None = None) -> Any:
    """One guarded GET of a JSON API: reserve (usage.Refused: nothing sent), send once, classify, then `parse` the JSON
    object (which raises ProviderError when there's no usable answer in it). Returns what `parse` returns (the object
    without one); raises ProviderError (counted) otherwise. `ids` collects the api_calls row of the call."""
    not_paused(provider, today)
    res = usage.reserve(conn, provider, today, account_id, trigger)
    if ids is not None:
        ids.append(res.call_id)
    try:
        status, body = transport(url, headers, TIMEOUT)
    except TimeoutError:
        _settle(conn, res, today, "timeout", None, f"{usage.LABELS[provider]} timed out after {TIMEOUT:.0f} s", False)
        raise ProviderError(provider, "timeout", f"{usage.LABELS[provider]} timed out; not retried") from None
    except (OSError, ValueError) as e:
        note = f"{usage.LABELS[provider]} unreachable ({type(e).__name__})"
        _settle(conn, res, today, "error", None, note, False)
        raise ProviderError(provider, "network", f"{note}; not retried") from None
    if status != 200:
        message, stop = _refusal(provider, status, _body_message(body), _body_text(body))
        until = _settle(conn, res, today, "error", status, message, stop)
        if until:
            message = f"{message}. Paused until {until}"
        raise ProviderError(provider, "http", message, status, until)
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        _settle(conn, res, today, "error", status, f"{usage.LABELS[provider]} sent something unreadable", False)
        raise ProviderError(provider, "bad_response", f"{usage.LABELS[provider]} sent something unreadable", status)
    try:
        out = parse(data) if parse else data
    except ProviderError as e:  # a 200 without an answer
        _settle(conn, res, today, "error", status, e.message, False)
        raise ProviderError(provider, e.kind, e.message, status) from None
    except SHAPE_ERRORS as e:  # a shape we didn't expect
        _settle(conn, res, today, "error", status, f"unexpected response ({type(e).__name__})", False)
        raise ProviderError(provider, "bad_response", f"{usage.LABELS[provider]} sent an unexpected answer",
                            status) from None
    _settle(conn, res, today, "ok", status, "", False)
    return out


# --- providers -------------------------------------------------------------------------------------------------

@dataclass
class Value:
    """One provider's answer: the value (Carfax: the trade-in), the trimmed response, and what the page shows beside
    it."""
    value: float
    raw: str = ""
    details: dict[str, Any] | None = None


def _number(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return float(x)


def _pretty_make(make: str) -> str:
    """NHTSA writes makes in capitals: 'HONDA' -> 'Honda', 'LAND ROVER' -> 'Land Rover'; short ones stay (BMW, GMC)."""
    if make != make.upper():
        return make
    return " ".join(w if len(w) <= 3 else w.capitalize() for w in make.split())


# The extended decode's fields kept for the car page (specs, then driver assist / safety equipment).
SPEC_FIELDS = ("BodyClass", "Doors", "Seats", "SeatRows", "DisplacementL", "EngineCylinders", "EngineConfiguration",
               "EngineHP", "EngineModel", "FuelTypePrimary", "FuelTypeSecondary", "ElectrificationLevel",
               "BatteryKWh", "BatteryType", "ChargerLevel", "ChargerPowerKW", "TransmissionStyle", "TransmissionSpeeds",
               "DriveType", "CurbWeightLB", "GVWR", "WheelBaseShort", "WheelBaseLong", "WheelSizeFront", "WheelSizeRear",
               "Wheels", "PlantCity", "PlantState", "PlantCountry", "PlantCompanyName", "Manufacturer", "VehicleType",
               "Series", "Series2", "Trim", "Trim2")
SAFETY_FIELDS = ("AdaptiveCruiseControl", "LaneKeepSystem", "LaneCenteringAssistance", "LaneDepartureWarning",
                 "ForwardCollisionWarning", "CIB", "DynamicBrakeSupport", "PedestrianAutomaticEmergencyBraking",
                 "BlindSpotMon", "BlindSpotIntervention", "RearCrossTrafficAlert", "RearAutomaticEmergencyBraking",
                 "ParkAssist", "RearVisibilitySystem", "TPMS", "ABS", "ESC", "TractionControl", "AdaptiveHeadlights",
                 "AdaptiveDrivingBeam", "SemiautomaticHeadlampBeamSwitching", "DaytimeRunningLight", "KeylessIgnition",
                 "AutomaticPedestrianAlertingSound", "AutoReverseSystem", "CAN_AACN", "EDR", "AirBagLocFront",
                 "AirBagLocSide", "AirBagLocCurtain", "AirBagLocKnee")


def decode_vin(conn: sqlite3.Connection, vin: str, today: date, account_id: int | None, trigger: str,
               ids: list[int] | None = None, need_specs: bool = False) -> dict[str, Any]:
    """{vehicle_year, vehicle_make, vehicle_model, vehicle_trim} for a VIN (only what's known): from vin_decodes when
    it was decoded before, else from NHTSA (once per VIN; a successful decode is kept, with its specs). `need_specs`:
    decode again if the kept one predates the specs (a v7 decode), once."""
    hit = conn.execute("SELECT * FROM vin_decodes WHERE vin = ?", (vin,)).fetchone()
    if hit and not (need_specs and hit["specs_json"] is None):
        return {k: hit[k] for k in DECODED if hit[k] is not None}
    out = request(conn, "nhtsa", NHTSA_URL.format(vin=quote(vin)), {}, today, account_id, trigger,
                  parse=lambda data: _decoded(data, today), ids=ids)
    specs = out.pop("specs")
    with conn:
        conn.execute("INSERT OR REPLACE INTO vin_decodes (vin, vehicle_year, vehicle_make, vehicle_model, vehicle_trim, "
                     "fetched_at, specs_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (vin, *(out.get(k) for k in DECODED), now(), json.dumps(specs)))
    return out


def vin_specs(conn: sqlite3.Connection, vin: str | None) -> dict[str, Any] | None:
    """The kept NHTSA specs for a VIN ({"specs": {...}, "safety": {...}}), or None (never decoded with them)."""
    if not vin:
        return None
    r = conn.execute("SELECT specs_json FROM vin_decodes WHERE vin = ?", (vin,)).fetchone()
    return json.loads(r[0]) if r and r[0] else None


def _decoded(data: dict[str, Any], today: date) -> dict[str, Any]:
    results = data.get("Results")
    r = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else {}
    text = lambda k, n=40: " ".join(str(r.get(k) or "").split())[:n]  # noqa: E731
    out: dict[str, Any] = {}
    year = text("ModelYear")
    if year.isdigit() and 1900 <= int(year) <= today.year + 1:
        out["vehicle_year"] = int(year)
    if text("Make"):
        out["vehicle_make"] = _pretty_make(text("Make"))
    if text("Model"):
        out["vehicle_model"] = text("Model")
    # Only NHTSA's Trim. Its Series is a factory code (e.g. "ABC12L-XYZDBA / ABC12L-XYZGBA", seen on a real hybrid), not a
    # trim anyone would recognise; it overflowed the edit form's Trim field on real data (2026-09-27).
    if text("Trim"):
        out["vehicle_trim"] = text("Trim")
    if not {"vehicle_make", "vehicle_model"} <= set(out):
        said = text("ErrorText", 120)
        raise ProviderError("nhtsa", "no_price", "NHTSA couldn't read this VIN" + (f" ({said})" if said else ""))
    keep = lambda fields: {k: text(k, 80) for k in fields if text(k, 80) and text(k, 80) != "Not Applicable"}  # noqa: E731
    out["specs"] = {"specs": keep(SPEC_FIELDS), "safety": keep(SAFETY_FIELDS)}
    return out


def _trim_raw(data: dict[str, Any], keep: tuple[str, ...], sub: dict[str, tuple[str, ...]] | None = None) -> str:
    out: dict[str, Any] = {k: data[k] for k in keep if k in data and not isinstance(data[k], (dict, list))}
    for key, fields in (sub or {}).items():
        if isinstance(data.get(key), dict):
            out[key] = {k: data[key][k] for k in fields if k in data[key] and not isinstance(data[key][k], (dict, list))}
    return redact.scrub(json.dumps(out, default=str))[:RAW_MAX]


# --- Carfax (in the local Chrome) -------------------------------------------------------------------------------

def _bot_wall(status: int, text: str) -> bool:
    """A bot-protection answer: HTTP 403, a captcha page (or JSON pointing at one), or HTML where JSON was asked for."""
    head = text.lstrip()[:4096]
    return status == 403 or bool(CAPTCHA.search(head)) or head[:1] == "<"


def _answer(conn: sqlite3.Connection, res: usage.Reservation, today: date, status: int, text: str, what: str,
            parse: Callable[[str], dict[str, Any] | None]) -> dict[str, Any]:
    """The JSON object of a browser-page lookup step, or ProviderError (settled). A bot wall blocks the provider for
    config.BLOCK_DAYS days; another failure is counted and explained, never retried."""
    provider = res.provider
    name = usage.LABELS[provider]
    if _bot_wall(status, text):
        why = ("a captcha page" if CAPTCHA.search(text[:4096]) else f"HTTP {status}" if status == 403 else "a web page")
        note = f"{name} blocked the check ({what}: {why})"
        until = _settle(conn, res, today, "error", status or None, note, True)
        raise ProviderError(provider, "blocked", f"{note}. Paused until {until}", status, until)
    if status != 200:
        note = (f"{name} couldn't answer ({what}: HTTP {status})" if status
                else f"{name}'s {what} request failed in the page ({text[:40] or 'no answer'})")
        _settle(conn, res, today, "error", status or None, note, False)
        raise ProviderError(provider, "http", f"{note}; not retried", status or None)
    data = parse(text)
    if not isinstance(data, dict):
        note = f"{name} sent something unreadable ({what})"
        _settle(conn, res, today, "error", status, note, False)
        raise ProviderError(provider, "bad_response", note, status)
    return data


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _norm(x: Any) -> str:
    return " ".join(str(x or "").replace("_", " ").split()).casefold()


def carfax_trim(trims: dict[str, Any], want: str | None) -> dict[str, Any] | None:
    """The account's trim if it names one of Carfax's trimOptions (its trim or trimDisplay), else the one Carfax marks
    trimDefault, else the first; None when there are none."""
    options = [t for t in trims.get("trimOptions") or [] if isinstance(t, dict) and t.get("trim")]
    if not options:
        return None
    w = _norm(want)
    if w:
        for t in options:
            if w in (_norm(t.get("trim")), _norm(t.get("trimDisplay"))):
                return t
    return next((t for t in options if t.get("trimDefault") is True), options[0])


def carfax_body(row: sqlite3.Row | dict, trim: dict[str, Any]) -> dict[str, Any]:
    """The prices request exactly as carfax.com/value sends it (recorded 2026-09-26): the trim with its default
    exterior colour only, the colour re-shaped the page's way (componentId 0, EXTERIOR_COLOR, standardFrequency "")."""
    colours = [o for o in trim.get("options") or []
               if isinstance(o, dict) and _norm(o.get("featureCategory")) == "exterior color"]
    colour = next((o for o in colours if o.get("componentDefault") is True), colours[0] if colours else None)
    options = [{"componentId": 0, "component": str(colour.get("component") or ""),
                "featureCategory": "_".join(str(colour.get("featureCategory") or "").upper().replace("_", " ").split()),
                "standardFrequency": "", "componentDefault": True}] if colour else []
    return {"vin": row["vin"], "zip": str(row["zip_code"])[:5],
            "condition": row["vehicle_condition"] or manual.DEFAULT_CONDITION, "odometer": row["mileage"],
            "trimOptions": {"options": options, "subtrim": str(trim.get("subtrim") or ""), "trim": trim["trim"],
                            "trimDefault": trim.get("trimDefault") is True,
                            "trimDisplay": str(trim.get("trimDisplay") or trim["trim"])}}


def _browser_ready(provider: str) -> None:
    why = browser.available()
    if why:
        raise Unavailable(provider, f"{usage.LABELS[provider]} skipped: {why}")


def carfax_value(conn: sqlite3.Connection, row: sqlite3.Row, today: date, trigger: str) -> Value:
    """One Carfax lookup (trims, then prices) inside a new background tab on carfax.com/value, counted once. Refused
    (nothing sent or counted) by the guard, or Unavailable when Chrome isn't running; ProviderError otherwise."""
    not_paused("carfax", today)
    usage.check(conn, "carfax", today, row["id"], trigger)   # a refusal comes before asking Chrome anything
    _browser_ready("carfax")
    res = usage.reserve(conn, "carfax", today, row["id"], trigger)
    try:
        return _carfax_lookup(conn, res, row, today)
    except SHAPE_ERRORS as e:  # an answer shaped unlike the recorded one: counted, explained, never retried
        _settle(conn, res, today, "error", None, f"Carfax sent an unexpected answer ({type(e).__name__})", False)
        raise ProviderError("carfax", "bad_response", "Carfax sent an unexpected answer") from None


def _carfax_lookup(conn: sqlite3.Connection, res: usage.Reservation, row: sqlite3.Row, today: date) -> Value:
    try:
        with browser.tab(CARFAX_PAGE) as tab:
            status, text = tab.fetch(CARFAX_TRIMS_URL.format(vin=quote(row["vin"])))
            trims = _answer(conn, res, today, status, text, "trims", _json_object)
            trim = carfax_trim(trims, row["vehicle_trim"])
            if trim is None:
                note = "Carfax has no trims for this VIN"
                _settle(conn, res, today, "none", status, note, False)
                raise ProviderError("carfax", "no_price", note, status)
            body = carfax_body(row, trim)
            status, text = tab.fetch(CARFAX_PRICES_URL, "POST", {"Content-Type": "application/json"}, json.dumps(body))
            prices = _answer(conn, res, today, status, text, "prices", _json_object)
    except cdp_client.CDPError as e:
        note = f"Carfax: Chrome failed during the lookup ({str(e)[:120]})"
        _settle(conn, res, today, "error", None, note, False)
        raise ProviderError("carfax", "browser", f"{note}; not retried") from None
    vp = prices.get("vehiclePrice") if isinstance(prices.get("vehiclePrice"), dict) else {}
    trade = _number(vp.get("wholesalePrice"))
    if trade is None or not 0 < trade <= manual.MAX_AMOUNT:
        shown = str(prices.get("displayStatus") or "")[:40]
        note = "Carfax gave no price (vehiclePrice missing" + (f", displayStatus {shown}" if shown else "") + ")"
        until = _settle(conn, res, today, "error", status, note, True)
        raise ProviderError("carfax", "blocked", f"{note}. Paused until {until}", status, until)
    atom = prices.get("atomInfo") if isinstance(prices.get("atomInfo"), dict) else {}
    money = lambda k: (lambda x: round(x, 2) if x and 0 < x <= manual.MAX_AMOUNT else None)(_number(vp.get(k)))  # noqa: E731
    odometer = _number(trims.get("odometer"))
    records = _number(trims.get("numberOfRecords"))
    details = {"trade_in": round(trade, 2), "private_sale": money("fsboPrice"), "dealer_retail": money("consumerRetailPrice"),
               "records": int(records) if records is not None else None,
               "carfax_odometer": int(odometer) if odometer is not None else None,
               "trim": body["trimOptions"]["trimDisplay"], "condition": body["condition"], "zip": body["zip"],
               "colour": body["trimOptions"]["options"][0]["component"] if body["trimOptions"]["options"] else None,
               "display_status": str(prices.get("displayStatus") or "")[:40] or None,
               "specs": {k: (v[:80] if isinstance(v, str) else v) for k, v in atom.items()
                         if isinstance(v, (str, int, float)) and not isinstance(v, bool) and v not in ("", 0)}}
    raw = _trim_raw({**prices, "numberOfRecords": trims.get("numberOfRecords"), "odometer": trims.get("odometer")},
                    ("displayStatus", "numberOfRecords", "odometer"),
                    {"vehiclePrice": ("consumerRetailPrice", "fsboPrice", "wholesalePrice")})
    _settle(conn, res, today, "ok", status, "", False)
    return Value(round(trade, 2), raw=raw, details=details)


# --- Redfin (plain HTTPS, else the Chrome tab) ------------------------------------------------------------------
def stingray(text: str) -> dict[str, Any] | None:
    """Redfin's API JSON behind its `{}&&` prefix."""
    t = text.lstrip()
    return _json_object(t[4:] if t.startswith("{}&&") else t)


UNIT_RE = re.compile(r"(?:#\s*|\b(?:unit|apt|apartment|ste|suite)\b\.?\s*#?\s*)([A-Za-z0-9][A-Za-z0-9-]*)", re.I)
DIRECTIONS = frozenset({"n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west"})


@dataclass
class Address:
    number: str | None      # the house number
    street: str             # the street name's first word ("maple" for "N Maple Ave"), lowercase
    unit: str | None        # "#303", "Unit 303", "Apt 303", "Ste 303" -> "303" (lowercase)
    building: str           # the address without the unit


def parse_address(text: str | None) -> Address:
    t = " ".join(str(text or "").split())
    m = UNIT_RE.search(t)
    unit = m.group(1).strip("-").casefold() if m else None
    building = f"{t[:m.start()]} {t[m.end():]}" if m else t
    building = re.sub(r"\s*,(\s*,)+", ",", re.sub(r"\s+,", ",", " ".join(building.split()))).strip(" ,")
    words = re.findall(r"[a-z0-9]+", building.split(",")[0].casefold())
    number = words[0] if words and words[0][0].isdigit() else None
    rest = words[1:] if number else words
    street = next((w for w in rest if w not in DIRECTIONS), rest[0] if rest else "")
    return Address(number, street, unit or None, building)


def redfin_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    rows = [payload["exactMatch"]] if isinstance(payload.get("exactMatch"), dict) else []
    for sec in payload.get("sections") or []:
        if isinstance(sec, dict):
            rows += [r for r in sec.get("rows") or [] if isinstance(r, dict)]
    return rows


def redfin_pick(rows: list[dict[str, Any]], want: Address) -> dict[str, Any] | None:
    """The home row (type '1', id '1_<propertyId>') for exactly this address: the same house number and street, and
    the same unit (none for a house or a whole building). Never the building for a unit, never a neighbouring unit."""
    for r in rows:
        if str(r.get("type")) != "1" or not re.fullmatch(r"1_\d+", str(r.get("id") or "")):
            continue
        got = parse_address(str(r.get("name") or ""))
        if got.number == want.number and got.street == want.street and got.unit == want.unit:
            return r
    return None


def _find(data: Any, key: str, depth: int = 0) -> list[Any]:
    """Every value of `key` anywhere in a JSON tree (Redfin's aboveTheFold nests its photos deep)."""
    out: list[Any] = []
    if depth > 12:
        return out
    if isinstance(data, dict):
        for k, v in data.items():
            if k == key:
                out.append(v)
            out += _find(v, key, depth + 1)
    elif isinstance(data, list):
        for v in data[:200]:
            out += _find(v, key, depth + 1)
    return out


def redfin_photo_url(above: dict[str, Any]) -> str | None:
    """The listing photo in an aboveTheFold answer (photoUrls.fullScreenPhotoUrl): https on Redfin's CDN only, so
    never a Google Street View URL (it carries Redfin's own Google key)."""
    for url in _find(above, "fullScreenPhotoUrl"):
        if isinstance(url, str) and url.startswith("https://") and len(url) < 400:
            host = (urlsplit(url).hostname or "").casefold()
            if host.endswith(REDFIN_PHOTO_HOSTS) and "google" not in url.casefold():
                return url
    return None


def redfin_latlng(data: dict[str, Any]) -> tuple[float, float] | None:
    for ll in _find(data, "latLong"):
        if isinstance(ll, dict):
            lat, lng = _number(ll.get("latitude")), _number(ll.get("longitude"))
            if lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180:
                return lat, lng
    return None


class _Https:
    via = "https"

    def get(self, url: str) -> tuple[int, str]:
        status, body = transport(url, BROWSER_HEADERS, TIMEOUT)
        return status, body[:MAX_BYTES].decode("utf-8", "replace")


class _InChrome:
    via = "chrome"

    def __init__(self, stack: ExitStack, page: str):
        self.stack, self.page, self.tab = stack, page, None

    def get(self, url: str) -> tuple[int, str]:
        if self.tab is None:
            self.tab = self.stack.enter_context(browser.tab(self.page))
        return self.tab.fetch(url, headers={"Accept": "application/json, text/plain, */*"})


def _redfin_via_chrome_last(conn: sqlite3.Connection) -> bool:
    """The last Redfin lookup that worked went through Chrome (plain HTTPS was refused): start there."""
    r = conn.execute("SELECT note FROM api_calls WHERE provider = 'redfin' AND outcome = 'ok' ORDER BY id DESC LIMIT 1"
                     ).fetchone()
    return bool(r and (r["note"] or "").startswith("via chrome"))


class _RedfinFetch:
    """The requests of ONE counted Redfin lookup: plain HTTPS first; on a 403 / captcha / web page, the rest from a
    background tab on redfin.com. A failure settles the reservation and raises ProviderError."""

    def __init__(self, conn: sqlite3.Connection, res: usage.Reservation, today: date, stack: ExitStack,
                 chrome_first: bool):
        self.conn, self.res, self.today, self.stack = conn, res, today, stack
        self.session: Any = _InChrome(stack, REDFIN_PAGE) if chrome_first else _Https()
        self.refused = ""   # what plain HTTPS got, when it was refused and Chrome asked instead

    def _send(self, url: str) -> tuple[int, str]:
        conn, res, today = self.conn, self.res, self.today
        try:
            return self.session.get(url)
        except TimeoutError:
            _settle(conn, res, today, "timeout", None, f"Redfin timed out after {TIMEOUT:.0f} s ({self.session.via})", False)
            raise ProviderError("redfin", "timeout", "Redfin timed out; not retried") from None
        except cdp_client.CDPError as e:
            note = f"Redfin: Chrome failed during the lookup ({str(e)[:120]})"
            _settle(conn, res, today, "error", None, note, False)
            raise ProviderError("redfin", "browser", f"{note}; not retried") from None
        except (OSError, ValueError) as e:
            note = f"Redfin unreachable ({type(e).__name__})"
            _settle(conn, res, today, "error", None, note, False)
            raise ProviderError("redfin", "network", f"{note}; not retried") from None

    def get_json(self, url: str, what: str) -> dict[str, Any]:
        status, text = self._send(url)
        if self.session.via == "https" and _bot_wall(status, text):   # refused as a script: ask as the browser
            why = browser.available()
            if why:
                note = f"Redfin refused the plain request (HTTP {status}) and {why[0].lower()}{why[1:]}"
                _settle(self.conn, self.res, self.today, "error", status or None, note, False)
                raise ProviderError("redfin", "http", f"{note}; not retried", status)
            self.refused = "a captcha page" if CAPTCHA.search(text[:4096]) else f"HTTP {status}"
            self.session = _InChrome(self.stack, REDFIN_PAGE)
            status, text = self._send(url)
        return _answer(self.conn, self.res, self.today, status, text, what, stingray)

    def try_json(self, url: str) -> dict[str, Any] | None:
        """An extra request that mustn't cost the lookup its answer (the listing photo): None on any trouble."""
        try:
            status, text = self.session.get(url)
        except (OSError, ValueError, cdp_client.CDPError):
            return None
        return stingray(text) if status == 200 and not _bot_wall(status, text) else None

    def note(self) -> str:
        return f"via {self.session.via}" + (f" (plain HTTPS got {self.refused})" if self.refused else "")


def redfin_value(conn: sqlite3.Connection, row: sqlite3.Row, today: date, trigger: str) -> Value:
    """One Redfin lookup, counted once: autocomplete the address (for a unit, again for the building if needed), pick
    the row for exactly this home, then its Redfin Estimate and (same session, same count) its listing photo's address.
    "Estimate Not Available" or no row for this unit is a clear "no estimate" (ProviderError kind no_estimate)."""
    not_paused("redfin", today)
    usage.check(conn, "redfin", today, row["id"], trigger)
    chrome_first = _redfin_via_chrome_last(conn) and browser.available() is None
    res = usage.reserve(conn, "redfin", today, row["id"], trigger)
    try:
        return _redfin_lookup(conn, res, row, today, chrome_first)
    except SHAPE_ERRORS as e:
        _settle(conn, res, today, "error", None, f"Redfin sent an unexpected answer ({type(e).__name__})", False)
        raise ProviderError("redfin", "bad_response", "Redfin sent an unexpected answer") from None


def _redfin_lookup(conn: sqlite3.Connection, res: usage.Reservation, row: sqlite3.Row, today: date,
                   chrome_first: bool) -> Value:
    with ExitStack() as stack:
        fetch = _RedfinFetch(conn, res, today, stack, chrome_first)
        want = parse_address(row["address"])
        hit = None
        for q in [row["address"]] + ([want.building] if want.unit else []):
            hit = redfin_pick(redfin_rows(fetch.get_json(REDFIN_AUTOCOMPLETE_URL.format(q=quote(q)), "autocomplete")), want)
            if hit:
                break
        if hit is None:
            note = (f"Redfin has no record for unit {want.unit.upper()} (only the building or other units): no estimate "
                    "for this unit" if want.unit else "Redfin found no home at this address")
            _settle(conn, res, today, "none", 200, note, False)
            raise ProviderError("redfin", "no_estimate", note, 200)
        pid = str(hit["id"]).split("_", 1)[1]
        avm = fetch.get_json(REDFIN_AVM_URL.format(pid=pid), "estimate")
        p = avm.get("payload") if isinstance(avm.get("payload"), dict) else {}
        value = _number(p.get("predictedValue"))
        above = fetch.try_json(REDFIN_ABOVE_URL.format(pid=pid)) if value else None
        note_ok = fetch.note()
        via = fetch.session.via
    preview = " ".join(str(p.get("sectionPreviewText") or "").split())[:120]
    if value is None or not 0 < value <= manual.MAX_AMOUNT:
        note = f"Redfin has no estimate for {hit.get('name') or 'this home'}" + (f" ({preview})" if preview else "")
        _settle(conn, res, today, "none", 200, note, False)
        raise ProviderError("redfin", "no_estimate", note, 200)
    sqft = p.get("sqFt") if isinstance(p.get("sqFt"), dict) else {}
    price = p.get("priceInfo") if isinstance(p.get("priceInfo"), dict) else {}
    ll = redfin_latlng(avm)
    street = p.get("streetAddress")
    street = street.get("assembledAddress") if isinstance(street, dict) else street
    details = {"property_id": pid, "name": str(hit.get("name") or "")[:120], "sub_name": str(hit.get("subName") or "")[:120],
               "url": str(hit.get("url") or "")[:200], "preview": preview or None,
               "beds": _number(p.get("numBeds")), "baths": _number(p.get("numBaths")), "sqft": _number(sqft.get("value")),
               "last_sold": ({"amount": _number(price.get("amount")), "label": str(price.get("label") or "")[:40]}
                             if _number(price.get("amount")) else None),
               "lat": ll[0] if ll else None, "lng": ll[1] if ll else None,
               "street_address": str(street or "")[:120] or None, "via": via,
               "photo_url": redfin_photo_url(above) if above else None}
    raw = json.dumps({"predictedValue": value, "sectionPreviewText": preview, "propertyId": pid})[:RAW_MAX]
    _settle(conn, res, today, "ok", 200, note_ok, False)
    return Value(round(value, 2), raw=raw, details=details)


def redfin_place(conn: sqlite3.Connection, row: sqlite3.Row, today: date, trigger: str = "refresh") -> dict[str, Any]:
    """For the page's picture, one counted Redfin lookup (no account limit; the monthly cap applies): the listing photo
    and location of the home Redfin valued (its aboveTheFold), or, for a unit Redfin has no row for, only the location
    of its building (the building's value is never used). {"photo_url", "lat", "lng", "property_id"}; ProviderError /
    usage.Refused when it can't be asked or answered."""
    not_paused("redfin", today)
    usage.check(conn, "redfin", today, None, trigger)
    est = latest(conn, row, "redfin")
    pid = ((json.loads(est["details_json"] or "{}") or {}).get("property_id") if est else None)
    chrome_first = _redfin_via_chrome_last(conn) and browser.available() is None
    res = usage.reserve(conn, "redfin", today, None, trigger)
    try:
        with ExitStack() as stack:
            fetch = _RedfinFetch(conn, res, today, stack, chrome_first)
            out: dict[str, Any] = {"photo_url": None, "lat": None, "lng": None, "property_id": pid}
            if pid:
                above = fetch.get_json(REDFIN_ABOVE_URL.format(pid=quote(str(pid))), "photo")
                out["photo_url"] = redfin_photo_url(above)
                ll = redfin_latlng(above)
            else:
                want = parse_address(row["address"])
                building = redfin_pick(redfin_rows(fetch.get_json(
                    REDFIN_AUTOCOMPLETE_URL.format(q=quote(want.building)), "autocomplete")),
                    Address(want.number, want.street, None, want.building))
                ll = None
                if building:
                    bid = str(building["id"]).split("_", 1)[1]
                    ll = redfin_latlng(fetch.get_json(REDFIN_AVM_URL.format(pid=bid), "location"))
            if ll:
                out["lat"], out["lng"] = ll
            note = fetch.note() + (" · photo" if out["photo_url"] else "") + (" · location" if ll else "")
    except SHAPE_ERRORS as e:
        _settle(conn, res, today, "error", None, f"Redfin sent an unexpected answer ({type(e).__name__})", False)
        raise ProviderError("redfin", "bad_response", "Redfin sent an unexpected answer") from None
    _settle(conn, res, today, "ok", 200, note, False)
    return out


# --- what an account needs, and its state ---------------------------------------------------------------------

def source_on(kind: str, free: dict[str, bool] | None = None, conn: sqlite3.Connection | None = None) -> bool:
    """Carfax / Redfin are on unless Settings or the env file switches them off (CARFAX_ENABLED=0 / REDFIN_ENABLED=0
    win over the Settings switch)."""
    free = config.free_sources(conn=conn) if free is None else free
    return bool(free.get(SOURCE_OF[kind], False))


def off_note(kind: str) -> str:
    p = SOURCE_OF[kind]
    return f"{usage.LABELS[p]} is switched off ({config.FREE_SOURCE_NAMES[p]}=0 in the .env)"


def missing_inputs(row: sqlite3.Row | dict) -> list[str]:
    """The fields (their form labels) an estimate still needs."""
    need = ("vin", "mileage", "zip_code") if row["kind"] == "vehicle" else ("address",)
    return [manual.label(f, row["kind"]) for f in need if row[f] in (None, "")]


def _needs(labels: list[str]) -> str:
    """['VIN', 'Mileage', 'ZIP code'] -> 'VIN, mileage and ZIP code'."""
    words = [x if x.split()[0].isupper() else x[0].lower() + x[1:] for x in labels]
    return " and ".join(words) if len(words) < 3 else f"{', '.join(words[:-1])} and {words[-1]}"


def subject(row: sqlite3.Row | dict) -> str | None:
    """What an estimate values: the vehicle's VIN, or the property's address (lowercase, spaces collapsed)."""
    if row["kind"] == "vehicle":
        return row["vin"]
    return " ".join(row["address"].split()).casefold() if row["address"] else None


def latest(conn: sqlite3.Connection, row: sqlite3.Row | dict, source: str | None = None) -> sqlite3.Row | None:
    """The newest estimate of the account's value source (Carfax / Redfin; `source` may name it) for its current VIN /
    address. Rows of any other source (MarketCheck / RentCast history) never count."""
    source = source or SOURCE_OF[row["kind"]]
    if source not in SOURCES:
        return None
    return conn.execute("SELECT * FROM asset_valuations WHERE account_id = ? AND subject IS ? AND source = ? "
                        "ORDER BY fetched_at DESC, id DESC LIMIT 1", (row["id"], subject(row), source)).fetchone()


BASIS_LABELS = {"trade_in": "Trade-in", "estimate": "Estimated"}
BASIS_WORDS = {"trade_in": "trade-in", "estimate": "estimate"}


def effective(conn: sqlite3.Connection, row: sqlite3.Row | dict) -> dict[str, Any] | None:
    """What the account is worth by its estimate (the value net worth uses while it follows it), or None: a vehicle's
    latest Carfax trade-in, a property's latest Redfin Estimate."""
    est = latest(conn, row)
    if est is None:
        return None
    return {"row": est, "value": est["value"], "basis": "trade_in" if row["kind"] == "vehicle" else "estimate"}


def history(conn: sqlite3.Connection, account_id: int) -> list[dict[str, Any]]:
    """Every Carfax / Redfin estimate of the account, oldest first (never another source's)."""
    return [_estimate_view(r) for r in conn.execute(
        f"SELECT * FROM asset_valuations WHERE account_id = ? AND source IN ({', '.join('?' * len(SOURCES))}) "
        "ORDER BY fetched_at, id", (account_id, *SOURCES))]


def _estimate_view(r: sqlite3.Row) -> dict[str, Any]:
    return {"value": r["value"], "low": None, "high": None, "source": r["source"],
            "source_label": usage.LABELS[r["source"]], "miles": r["miles"], "fetched_at": r["fetched_at"],
            "date": usage.local_day(r["fetched_at"]), "id": r["id"],
            "details": json.loads(r["details_json"]) if r["details_json"] else None}


def _effective_view(eff: dict[str, Any] | None) -> dict[str, Any] | None:
    if eff is None:
        return None
    return {**_estimate_view(eff["row"]), "value": eff["value"], "basis": eff["basis"],
            "basis_label": BASIS_LABELS[eff["basis"]]}


def _money(x: float) -> str:
    return money(x)


def short_day(day: str | None) -> str:
    """'2026-09-26' -> 'Sep 26'."""
    if not day:
        return ""
    d = date.fromisoformat(day)
    return f"{d:%b} {d.day}"


def privacy_note(kind: str) -> str:
    """Who sees the VIN / address."""
    if kind == "vehicle":
        return ("Your VIN, mileage and ZIP code are sent to NHTSA (the free VIN lookup) and to Carfax (through your "
                "Chrome) to get an estimate")
    return "Your address is sent to Redfin to get an estimate"


def _source_view(conn: sqlite3.Connection, row: sqlite3.Row, on: bool, today: date) -> dict[str, Any]:
    """The value source's line on the page: its latest estimate, its last call, any block."""
    provider = SOURCE_OF[row["kind"]]
    est = latest(conn, row)
    last = usage.last_call(conn, provider, row["id"])
    blocked = usage.blocked(conn, provider, today)
    note = None
    if blocked:
        note = (f"{usage.LABELS[provider]} blocked the check, last value from {short_day(usage.local_day(est['fetched_at']))}"
                if est else f"{usage.LABELS[provider]} blocked the check; paused until {blocked['blocked_until']}")
    elif last and last["outcome"] == "none":
        note = last["note"]
    return {"provider": provider, "label": usage.LABELS[provider], "enabled": on,
            "estimate": _estimate_view(est) if est else None,
            "last_call": ({"date": last["day"], "outcome": last["outcome"], "message": last["note"]} if last else None),
            "blocked_until": blocked["blocked_until"] if blocked else None, "note": note}


def next_check(conn: sqlite3.Connection, row: sqlite3.Row, today: date) -> str:
    """When the daily run asks next: the value source's last call + 30 days (today if it never was asked)."""
    last = usage.last_call(conn, SOURCE_OF[row["kind"]], row["id"])
    return (max(date.fromisoformat(last["day"]) + timedelta(days=usage.SCHEDULED_DAYS), today) if last else today).isoformat()


def state(conn: sqlite3.Connection, row: sqlite3.Row, today: date, free: dict[str, bool] | None = None) -> dict[str, Any]:
    """What the page and the CLI show about a vehicle / property's value (reads only; never asks Chrome)."""
    kind = row["kind"]
    provider = SOURCE_OF[kind]
    enabled = source_on(kind, free, conn=conn)
    eff = effective(conn, row)
    last = usage.last_call(conn, provider, row["id"])
    missing = missing_inputs(row)
    can_refresh, note = False, None
    if row["archived_at"]:
        note = "Archived"
    elif not enabled:
        note = off_note(kind)
    elif missing:
        note = f"Add the {_needs(missing)} to get an estimate"
    else:
        try:
            usage.check(conn, provider, today, row["id"], "refresh")
            can_refresh = True
        except usage.Refused as e:
            note = str(e)
            if e.reason == "blocked" and last and last["note"] and note.endswith(last["note"]):
                note = note.split(": ", 1)[0]  # "Carfax is paused until 2026-10-03" (the reason is the last try's)
    age = (today - date.fromisoformat(row["mileage_as_of"])).days if row["mileage_as_of"] else None
    source = _source_view(conn, row, enabled, today)
    hint = None
    seen = ((source["estimate"] or {}).get("details") or {}).get("carfax_odometer") if kind == "vehicle" else None
    if seen and row["mileage"] is not None and seen > row["mileage"]:
        hint = {"miles": seen, "message": f"Carfax records show {seen:,} mi, update?"}
    return {
        "provider": provider, "provider_label": usage.LABELS[provider], "enabled": enabled,
        "source": row["value_source"] or "user",
        "estimate": _effective_view(eff),
        "last_error": ({"date": last["day"], "message": last["note"]}
                       if last and last["outcome"] not in ("ok", "pending") else None),
        "missing": missing, "can_refresh": can_refresh, "refresh_note": note,
        "mileage_age_days": age,
        "mileage_nudge": bool(kind == "vehicle" and enabled and row["vin"] and age is not None and age >= NUDGE_DAYS),
        "note": privacy_note(kind) if enabled else off_note(kind),
        "sources": {provider: source}, "odometer_hint": hint,
        "next_check": next_check(conn, row, today) if enabled and not missing and not row["archived_at"] else None,
    }


# --- fetching an estimate --------------------------------------------------------------------------------------

@dataclass
class Outcome:
    ok: bool
    provider: str
    message: str
    called: bool = False          # a request was sent (and counted)
    reason: str | None = None     # off | missing | archived | unavailable | Refused.reason | ProviderError.kind
    value: float | None = None
    blocked_until: str | None = None
    applied: bool = False         # the balance now follows it (value_source 'estimate')

    def json(self) -> dict[str, Any]:
        return asdict(self)


def _one(conn: sqlite3.Connection, row: sqlite3.Row, today: date, trigger: str) -> Outcome:
    """Ask the value source (guarded) and keep its answer. Never raises for a refused, skipped or failed lookup."""
    provider = SOURCE_OF[row["kind"]]
    try:
        v = carfax_value(conn, row, today, trigger) if provider == "carfax" else redfin_value(conn, row, today, trigger)
    except usage.Refused as e:
        return Outcome(False, provider, str(e), reason=e.reason)
    except Unavailable as e:
        return Outcome(False, provider, e.message, reason="unavailable")
    except ProviderError as e:
        log.warning("estimate for manual account #%s: %s", row["id"], e.message)
        return Outcome(False, provider, e.message, called=True, reason=e.kind, blocked_until=e.blocked_until)
    with conn:
        conn.execute("INSERT INTO asset_valuations (account_id, value, source, miles, subject, fetched_at, raw_json, "
                     "details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (row["id"], v.value, provider, row["mileage"] if row["kind"] == "vehicle" else None, subject(row),
                      now(), v.raw, json.dumps(v.details) if v.details is not None else None))
    if provider == "carfax":
        d = v.details or {}
        extra = ", ".join(f"{k} {_money(d[f])}" for k, f in (("private sale", "private_sale"), ("dealer retail", "dealer_retail"))
                          if d.get(f))
        message = f"Trade-in {_money(v.value)} by Carfax" + (f" ({extra})" if extra else "")
    else:
        message = f"Estimated {_money(v.value)} by Redfin"
    return Outcome(True, provider, message, called=True, value=v.value)


def estimate(conn: sqlite3.Connection, account_id: int, today: date, trigger: str) -> Outcome:
    """Ask the value source for this account's value once (guarded). On success: a new asset_valuations row, and while
    the account follows its estimate, its balance becomes it (a history row today). On failure the value stays."""
    row = manual.get(conn, account_id, include_archived=True)
    kind = row["kind"]
    if kind not in VALUE_KINDS:
        raise manual.ManualError(f"#{row['id']} {row['name']} is a {kind.replace('_', ' ')}: only vehicles and "
                                 "properties get an estimate", "id")
    provider = SOURCE_OF[kind]
    if row["archived_at"]:
        return Outcome(False, provider, "Archived: no estimate", reason="archived")
    if not source_on(kind, conn=conn):
        return Outcome(False, provider, off_note(kind), reason="off")
    missing = missing_inputs(row)
    if missing:
        return Outcome(False, provider, f"Add the {_needs(missing)} to get an estimate", reason="missing")
    out = _one(conn, row, today, trigger)
    if out.ok:
        with conn:  # decided in the write itself: a value typed while the lookup was out still wins
            out.applied = _follow(conn, row["id"], out.value, today, only_if_following=True)
    return out


def _follow(conn: sqlite3.Connection, account_id: int, value: float, today: date, only_if_following: bool = False) -> bool:
    """Inside the caller's transaction: the balance becomes `value`, true today, and follows the estimate.
    `only_if_following`: only while value_source is still 'estimate' (never over a value the user typed)."""
    cur = conn.execute("UPDATE manual_accounts SET balance = ?, balance_as_of = ?, value_source = 'estimate', updated_at = ? "
                       "WHERE id = ?" + (" AND value_source = 'estimate'" if only_if_following else ""),
                       (value, today.isoformat(), now(), account_id))
    if not cur.rowcount:
        return False
    conn.execute("INSERT INTO manual_balance_history (manual_account_id, date, balance) VALUES (?, ?, ?) "
                 "ON CONFLICT(manual_account_id, date) DO UPDATE SET balance = excluded.balance",
                 (account_id, today.isoformat(), value))
    return True


def use_estimate(conn: sqlite3.Connection, account_id: Any, today: date) -> dict[str, Any]:
    """"Use estimate again": the balance follows the estimate again (from today)."""
    row = manual.get(conn, account_id)
    if row["kind"] not in VALUE_KINDS:
        raise manual.ManualError("only vehicles and properties have an estimate", "id")
    eff = effective(conn, row)
    if eff is None:
        raise manual.ManualError(f"No estimate for {row['name']} yet" + (" (for this VIN)" if row["kind"] == "vehicle"
                                 else " (for this address)"), "id", 409)
    with conn:
        _follow(conn, row["id"], eff["value"], today)
    return manual.describe(conn, manual.get(conn, row["id"]), today)


def refresh(conn: sqlite3.Connection, account_id: Any, today: date) -> dict[str, Any]:
    """The Refresh button (and `finance revalue --account N`): one guarded call, at most one per account per day."""
    row = manual.get(conn, account_id)
    out = estimate(conn, row["id"], today, "refresh")
    return {**manual.describe(conn, manual.get(conn, row["id"]), today), "estimate_result": out.json()}


# --- saving a vehicle / property (the dashboard form and `finance manual add/edit`) ----------------------------

DECODED = ("vehicle_year", "vehicle_make", "vehicle_model", "vehicle_trim")
INPUTS = {"vehicle": ("vin", "mileage", "zip_code"), "property": ("address",)}


def _decode(conn: sqlite3.Connection, vin: str, today: date, account_id: int | None,
            ids: list[int] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """(decoded fields, None) or (None, why not). Never raises for a refused or failed lookup."""
    try:
        return decode_vin(conn, vin, today, account_id, "save", ids), None
    except usage.Refused as e:
        return None, str(e)
    except ProviderError as e:
        return None, e.message


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _vin_of(data: dict[str, Any], kind: str, today: date) -> str | None:
    if _blank(data.get("vin")):
        return None
    return manual.validate(kind, {"vin": data["vin"]}, today, partial=True)["vin"]


def _need_yourself(problem: str | None, field: str, kind: str) -> manual.ManualError:
    why = problem or f"NHTSA had no {manual.label(field, kind).lower()} for this VIN"
    return manual.ManualError(f"{why}. Enter the make, model and year yourself.", field)


def _cant_ask(conn: sqlite3.Connection, kind: str, today: date) -> str | None:
    """None if the value source can be asked right now for a new account (the guard, and Chrome for Carfax); else why
    not."""
    provider = SOURCE_OF[kind]
    try:
        not_paused(provider, today)
        usage.check(conn, provider, today, None, "save")
        if provider == "carfax":
            _browser_ready(provider)
    except usage.Refused as e:
        return str(e)
    except Unavailable as e:
        return e.message
    return None


def create(conn: sqlite3.Connection, data: dict[str, Any], today: date) -> dict[str, Any]:
    """manual.create, plus for a vehicle / property: the VIN decode (fills the year / make / model / trim left blank)
    and the first estimate when its source is on. Everything else is validated before any lookup; nothing is written
    when validation fails; a failed lookup never fails the save (unless the decode was needed for the make / model /
    year)."""
    kind = data.get("kind") if isinstance(data, dict) else None
    if kind not in VALUE_KINDS:
        return manual.create(conn, data, today)
    data = dict(data)
    if data.get("balance") is None:  # the form sends null for a value left to the estimate
        data.pop("balance", None)
    enabled = source_on(kind, conn=conn)
    vin = _vin_of(data, kind, today)
    blanks = [f for f in DECODED if _blank(data.get(f))] if vin else []
    for f in blanks:
        data.pop(f, None)
    manual.validate(kind, data, today, partial=True)
    for f in sorted(manual.REQUIRED[kind] - set(blanks) - {"balance"}, key=list(manual.LABELS).index):
        if _blank(data.get(f)):
            raise manual.ManualError(f"{manual.label(f, kind)} is required", f)
    has_inputs = all(not _blank(data.get(f)) for f in INPUTS[kind])
    estimating = enabled and has_inputs and data.get("balance") is None
    if estimating:  # no value to fall back on: only when the source can be asked right now
        why = _cant_ask(conn, kind, today)
        if why:
            raise manual.ManualError(f"{manual.label('balance', kind)} is required for now: {why}", "balance")
    if data.get("balance") is None and not estimating:
        label = usage.LABELS[SOURCE_OF[kind]]
        hint = "" if not enabled else (f" (or add the VIN, mileage and ZIP code to use the {label} estimate)"
                                       if kind == "vehicle" else f" (or add the address to use the {label} estimate)")
        raise manual.ManualError(f"{manual.label('balance', kind)} is required{hint}", "balance")
    decoded, problem, ids = None, None, []
    if vin:
        decoded, problem = _decode(conn, vin, today, None, ids)
        for f in blanks:
            if decoded and decoded.get(f) is not None:
                data[f] = decoded[f]
        for f in ("vehicle_year", "vehicle_make", "vehicle_model"):
            if _blank(data.get(f)):
                raise _need_yourself(problem, f, kind)
    acct = manual.create(conn, data, today, estimate=estimating)
    if vin:
        with conn:
            if decoded:
                conn.execute("UPDATE manual_accounts SET vin_decoded = ? WHERE id = ?", (vin, acct["id"]))
            conn.executemany("UPDATE api_calls SET account_id = ? WHERE id = ?", [(acct["id"], i) for i in ids])
    result = estimate(conn, acct["id"], today, "save") if enabled and has_inputs else None
    return {**manual.describe(conn, manual.get(conn, acct["id"]), today),
            "decoded": decoded, "decode_problem": problem, "estimate_result": result.json() if result else None}


def edit(conn: sqlite3.Connection, account_id: Any, data: dict[str, Any], today: date) -> dict[str, Any]:
    """manual.edit, plus for a vehicle / property: a new VIN is decoded (its year / make / model / trim replace the old
    car's unless sent in the same edit; a field sent blank is filled from the VIN), and the value source is asked again
    when what it looks at changed (WATCHES: VIN, mileage, ZIP code, condition, trim; address), within the guard."""
    row = manual.get(conn, account_id)
    kind = row["kind"]
    if kind not in VALUE_KINDS or not isinstance(data, dict):
        return manual.edit(conn, account_id, data, today)
    data = dict(data)
    vin = _vin_of(data, kind, today) if "vin" in data else row["vin"]
    # a required field sent blank is filled from the VIN (a blank trim just clears it)
    blanks = [f for f in DECODED[:3] if f in data and _blank(data[f])] if vin else []
    for f in blanks:
        data.pop(f)
    manual.validate(kind, {k: v for k, v in data.items() if k != "kind"}, today, partial=True)
    vin_changed = "vin" in data and vin is not None and vin != row["vin"]
    decoded, problem = None, None
    if vin and (vin_changed or blanks):
        decoded, problem = _decode(conn, vin, today, row["id"])
        for f in DECODED:
            if decoded and decoded.get(f) is not None and (f in blanks or (vin_changed and f not in data)):
                data[f] = decoded[f]
    for f in blanks:
        if f not in data:
            raise _need_yourself(problem, f, kind)
    acct = manual.edit(conn, row["id"], data, today)
    if decoded:
        with conn:
            conn.execute("UPDATE manual_accounts SET vin_decoded = ? WHERE id = ?", (vin, row["id"]))
    changed = [f for f in WATCHES[SOURCE_OF[kind]] if f in data and acct[f] != row[f]]
    result = None
    if changed and source_on(kind, conn=conn) and not missing_inputs(acct):
        result = estimate(conn, row["id"], today, "save")
    now_row = manual.get(conn, row["id"])
    if subject(now_row) != subject(row) and now_row["value_source"] == "estimate" and effective(conn, now_row) is None:
        with conn:  # the old car's / address's estimate doesn't value this one: $0 ("No estimate yet") until one does
            _follow(conn, row["id"], 0.0, today)
    return {**manual.describe(conn, manual.get(conn, row["id"]), today),
            "decoded": decoded, "decode_problem": problem, "estimate_result": result.json() if result else None}


# --- the daily run: `finance revalue` --------------------------------------------------------------------------

def revalue(conn: sqlite3.Connection, today: date, account_id: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Every active vehicle / property due for its monthly estimate (30+ days since its last call), or with
    `account_id` that one account now (a Refresh: at most one per day). A dry run checks the same limits and calls
    nothing. A failure is reported, never raised."""
    trigger = "refresh" if account_id is not None else "daily"
    if account_id is not None:
        row = manual.get(conn, account_id)
        if row["kind"] not in VALUE_KINDS:
            raise manual.ManualError(f"#{row['id']} {row['name']} is a {row['kind'].replace('_', ' ')}: only vehicles "
                                     "and properties get an estimate", "id")
        rows = [row]
    else:
        rows = conn.execute("SELECT * FROM manual_accounts WHERE kind IN ('vehicle', 'property') "
                            "AND archived_at IS NULL ORDER BY id").fetchall()
    free = config.free_sources(conn=conn)
    out = []
    for row in rows:
        provider = SOURCE_OF[row["kind"]]
        base = {"account_id": row["id"], "name": row["name"], "kind": row["kind"], "provider": provider,
                "source": row["value_source"] or "user", "balance": row["balance"]}
        missing = missing_inputs(row)
        if not source_on(row["kind"], free):
            out.append({**base, "action": "skipped", "message": off_note(row["kind"])})
            continue
        if missing:
            out.append({**base, "action": "skipped",
                        "message": f"needs the {_needs(missing)} for an estimate"})
            continue
        if dry_run:
            try:
                usage.check(conn, provider, today, row["id"], trigger)
                eff = effective(conn, row)
                out.append({**base, "action": "would_call", "message": f"would ask {usage.LABELS[provider]}"
                            + (f" (last estimate {_money(eff['value'])} on {usage.local_day(eff['row']['fetched_at'])})"
                               if eff else " (no estimate yet)")})
            except usage.Refused as e:
                out.append({**base, "action": "skipped", "message": str(e)})
            continue
        if row["kind"] == "vehicle":  # decoded once per VIN (its trim), again once for the specs of a v7 decode
            specs = vin_specs(conn, row["vin"])
            if row["vin"] != row["vin_decoded"] or specs is None:
                try:
                    decoded = decode_vin(conn, row["vin"], today, row["id"], trigger, need_specs=True)
                except (usage.Refused, ProviderError):
                    decoded = None
                if decoded:
                    with conn:
                        conn.execute("UPDATE manual_accounts SET vin_decoded = ?, vehicle_trim = COALESCE(vehicle_trim, ?) "
                                     "WHERE id = ?", (row["vin"], decoded.get("vehicle_trim"), row["id"]))
        r = estimate(conn, row["id"], today, trigger)
        action = "valued" if r.ok else ("failed" if r.called else "skipped")
        out.append({**base, "action": action, "message": r.message, "value": r.value, "applied": r.applied,
                    "blocked_until": r.blocked_until})
    return {"today": today.isoformat(), "dry_run": dry_run, "trigger": trigger, "results": out,
            "valued": sum(x["action"] == "valued" for x in out), "failed": sum(x["action"] == "failed" for x in out)}


def providers(conn: sqlite3.Connection, today: date) -> dict[str, Any]:
    """For the page: per value source, whether it's on, this month's usage and any block, and the note the forms show."""
    rep = {x["provider"]: x for x in usage.report(conn, today)["providers"]}
    free = config.free_sources(conn=conn)
    out = {}
    for kind, provider in SOURCE_OF.items():
        x = rep[provider]
        out[provider] = {"label": x["label"], "kind": kind, "enabled": bool(free.get(provider)), "calls": x["calls"],
                         "cap": x["cap"], "blocked_until": x["blocked_until"], "blocked_reason": x["blocked_reason"],
                         "privacy_note": privacy_note(kind), "off_note": off_note(kind)}
    return out


# --- the car / property page (TASK-13) -------------------------------------------------------------------------

SAFETY_LABELS = {"AdaptiveCruiseControl": "Adaptive cruise control", "LaneKeepSystem": "Lane keeping assist",
                 "LaneCenteringAssistance": "Lane centering", "LaneDepartureWarning": "Lane departure warning",
                 "ForwardCollisionWarning": "Forward collision warning", "CIB": "Automatic emergency braking",
                 "DynamicBrakeSupport": "Dynamic brake support",
                 "PedestrianAutomaticEmergencyBraking": "Pedestrian emergency braking",
                 "BlindSpotMon": "Blind spot warning", "BlindSpotIntervention": "Blind spot intervention",
                 "RearCrossTrafficAlert": "Rear cross traffic alert", "RearAutomaticEmergencyBraking": "Rear emergency braking",
                 "ParkAssist": "Park assist", "RearVisibilitySystem": "Backup camera", "TPMS": "Tire pressure monitoring",
                 "ABS": "Anti-lock brakes", "ESC": "Stability control", "TractionControl": "Traction control",
                 "AdaptiveDrivingBeam": "Adaptive driving beam", "SemiautomaticHeadlampBeamSwitching": "Auto high beams",
                 "DaytimeRunningLight": "Daytime running lights", "KeylessIgnition": "Keyless ignition",
                 "AutomaticPedestrianAlertingSound": "Pedestrian alert sound"}
# the driver-assist ones first, in this order (the page's chips); the rest follow. Only what NHTSA lists for the VIN.
ASSIST_FIRST = ("AdaptiveCruiseControl", "LaneKeepSystem", "LaneCenteringAssistance", "LaneDepartureWarning",
                "ForwardCollisionWarning", "CIB", "PedestrianAutomaticEmergencyBraking", "BlindSpotMon",
                "BlindSpotIntervention", "RearCrossTrafficAlert", "RearAutomaticEmergencyBraking", "TPMS")
SELL_URL = {"vehicle": ("Get a Carfax cash offer", "https://www.carfax.com/value/")}


def _title_word(w: str) -> str:
    return w if len(w) <= 3 and w.isupper() else w.capitalize()


def _pretty(text: Any) -> str:
    """'SPRING VALLEY' -> 'Spring Valley' (words capitalized; a mixed-case text stays)."""
    t = " ".join(str(text or "").split())
    return t if t != t.upper() else " ".join(_title_word(w) for w in t.split())


ACRONYMS = frozenset({"SUV", "CUV", "MPV", "EV", "AWD", "FWD", "RWD", "4WD", "LE", "XLE"})


def _sentence(text: Any) -> str:
    """'COMPACT SUV' -> 'Compact SUV', '4 DOOR HATCHBACK' -> '4 door hatchback' (acronyms stay in capitals)."""
    words = [w if w.upper() in ACRONYMS else w.lower() for w in str(text or "").split()]
    t = " ".join(words)
    return t[:1].upper() + t[1:]


def vehicle_title(row: sqlite3.Row | dict, carfax: dict[str, Any] | None) -> str:
    """'2019 Honda Accord Hybrid Touring': year, make, model (without NHTSA's "(PHEV)"), and Carfax's trim (else the
    account's, if it reads like a trim and not a factory code)."""
    model = re.sub(r"\s*\([^)]*\)", "", str(row["vehicle_model"] or "")).strip()
    trim = (carfax or {}).get("trim") or ""
    if not trim and row["vehicle_trim"] and "/" not in row["vehicle_trim"] and len(row["vehicle_trim"].split()) <= 3:
        trim = row["vehicle_trim"]
    return " ".join(x for x in (str(row["vehicle_year"] or ""), row["vehicle_make"] or "", model,
                                " ".join(_title_word(w) for w in str(trim).split())) if x)


def body_style(carfax_body: Any, nhtsa_class: Any) -> str | None:
    """Carfax's '4 DOOR HATCHBACK' -> 'Hatchback' (else NHTSA's 'Sedan/Saloon' -> 'Sedan')."""
    t = re.sub(r"^\s*\d+\s*DOOR\s+", "", str(carfax_body or ""), flags=re.I).strip()
    if t:
        return _sentence(t)
    return str(nhtsa_class).split("/")[0].strip() or None if nhtsa_class else None


def fuel_kind(carfax_fuel: Any, level: Any, primary: Any) -> str | None:
    """'PLUG-IN GAS/ELECTRIC HYBRID' or NHTSA's PHEV -> 'Plug-in hybrid'; hybrid, electric, gas, diesel likewise."""
    t = f"{carfax_fuel or ''} {level or ''}".upper()
    if "PLUG-IN" in t or "PHEV" in t:
        return "Plug-in hybrid"
    if "HYBRID" in t or "HEV" in t:
        return "Hybrid"
    if "BEV" in t or t.strip() in ("ELECTRIC", "ELECTRIC BEV"):
        return "Electric"
    f = str(carfax_fuel or primary or "").strip()
    return {"GASOLINE": "Gas", "GAS": "Gas", "DIESEL": "Diesel", "ELECTRIC": "Electric"}.get(f.upper(), _sentence(f) or None)


def _spec_rows(nhtsa: dict[str, Any], carfax: dict[str, Any] | None) -> list[list[str]]:
    """The Specs card: ONE engine row (Carfax's name, e.g. "1.8L Plug-in Hybrid I4 121hp", else NHTSA's parts), then
    transmission, drive, fuel, body, doors, seats, weight, wheelbase, wheels, segment, where it was built."""
    s = (nhtsa or {}).get("specs") or {}
    c = (carfax or {}).get("specs") or {}
    out: list[list[str]] = []
    engine = str(c.get("engineName") or "")
    if not engine:
        cyl = s.get("EngineCylinders")
        config_ = {"In-Line": "I", "V-Shaped": "V", "Flat": "H"}.get(s.get("EngineConfiguration", ""), "")
        engine = " ".join(x for x in (f"{float(s['DisplacementL']):.1f}L" if s.get("DisplacementL") else "",
                                      f"{config_ or 'I'}{cyl}" if cyl else "") if x)
        level = s.get("ElectrificationLevel", "")
        engine += " + PHEV" if engine and "PHEV" in level else " + hybrid" if engine and "HEV" in level else ""
    if engine:
        out.append(["Engine", engine])
    trans = s.get("TransmissionStyle", "")
    if trans:
        m = re.search(r"\(([^)]+)\)", trans)
        out.append(["Transmission", m.group(1) if m else trans])
    drive = c.get("driveType") or s.get("DriveType")   # Carfax's "FWD" reads better than NHTSA's "4x2"
    if drive:
        out.append(["Drive", str(drive).split("/")[0]])
    fuel = fuel_kind(c.get("fuelType"), s.get("ElectrificationLevel"), s.get("FuelTypePrimary"))
    if fuel:
        out.append(["Fuel", fuel])
    body = body_style(c.get("bodyType"), s.get("BodyClass"))
    if body:
        out.append(["Body", body])
    for key, label, unit in (("Doors", "Doors", ""), ("Seats", "Seats", ""), ("CurbWeightLB", "Curb weight", " lb"),
                             ("WheelBaseShort", "Wheelbase", " in"), ("WheelSizeFront", "Wheels", "\"")):
        if s.get(key):
            val = s[key]
            try:
                num = float(val)
                val = f"{num:,.0f}" if num >= 1000 else f"{num:g}"
            except ValueError:
                pass
            out.append([label, f"{val}{unit}"])
    if c.get("segmentationDesc"):
        out.append(["Segment", _sentence(c["segmentationDesc"])])
    if s.get("PlantCity") or s.get("PlantCountry"):
        country = re.sub(r"\s*\([^)]*\)", "", str(s.get("PlantCountry") or ""))
        out.append(["Built in", ", ".join(_pretty(x) for x in (s.get("PlantCity"), country) if x)])
    return out


def _safety(nhtsa: dict[str, Any]) -> list[dict[str, str]]:
    s = (nhtsa or {}).get("safety") or {}
    order = [k for k in ASSIST_FIRST if k in s] + [k for k in SAFETY_LABELS if k in s and k not in ASSIST_FIRST]
    return [{"key": k, "label": SAFETY_LABELS[k], "value": s[k]} for k in order]


def linked_loan(conn: sqlite3.Connection, row: sqlite3.Row, today: date) -> dict[str, Any] | None:
    """The loan linked to this car / property (manual_accounts.linked_loan_id), if it's still an active loan."""
    if not row["linked_loan_id"]:
        return None
    loan = conn.execute("SELECT * FROM manual_accounts WHERE id = ? AND kind = 'loan' AND archived_at IS NULL",
                        (row["linked_loan_id"],)).fetchone()
    if loan is None:
        return None
    d = manual.describe(conn, loan, today)
    return {"id": d["id"], "name": d["name"], "institution": d["institution"], "balance": d["balance"],
            "monthly_payment": d["monthly_payment"], "interest_rate": d["interest_rate"],
            "payoff_month": d["payoff"]["payoff_month"], "payments_left": d["payoff"]["payments_left"],
            "next_payment": d["next_payment"]}


def detail(conn: sqlite3.Connection, account_id: Any, today: date) -> dict[str, Any]:
    """Everything the car / property detail page shows: the value (and its change since the last check), the value
    table, the linked loan and equity, the value history, specs and safety (vehicle), the property facts, the picture,
    the editable details and the quiet notes. Reads only."""
    from . import photos  # photos imports this module
    row = manual.get(conn, account_id, include_archived=True)
    kind = row["kind"]
    if kind not in VALUE_KINDS:
        raise manual.ManualError("only vehicles and properties have a detail page", "id")
    m = manual.describe(conn, row, today)
    v = m["valuation"]
    src = v["sources"][SOURCE_OF[kind]]
    eff = v["estimate"]
    main = src["estimate"]
    prev = conn.execute("SELECT value, fetched_at FROM asset_valuations WHERE account_id = ? AND subject IS ? AND source = ? "
                        "ORDER BY fetched_at DESC, id DESC LIMIT 1 OFFSET 1",
                        (row["id"], subject(row), SOURCE_OF[kind])).fetchone()
    following = v["source"] == "estimate"
    if following and eff:
        line = [eff["basis_label"], eff["source_label"], short_day(eff["date"])]
        if kind == "vehicle" and eff["miles"] is not None:
            line.append(f"at {eff['miles']:,} mi")
    elif following:
        line = ["No estimate yet"]
    else:
        line = ["Your value", f"set {short_day(row['balance_as_of'])}"]
        if eff:
            line.append(f"{eff['source_label']} {BASIS_WORDS[eff['basis']]} {_money(eff['value'])} on {short_day(eff['date'])}")
    value = {"amount": row["balance"], "following": following, "line": " · ".join(line),
             "change": ({"amount": round(main["value"] - prev["value"], 2), "since": usage.local_day(prev["fetched_at"])}
                        if main and prev and following else None),
             "first_check": bool(main and not prev)}
    table: list[dict[str, Any]] = []
    d = (main or {}).get("details") or {}
    if kind == "vehicle":
        for key, label in (("trade_in", "Trade-in"), ("private_sale", "Private sale"), ("dealer_retail", "Dealer retail")):
            table.append({"label": label, "source": "Carfax", "value": d.get(key) if main else None,
                          "primary": key == "trade_in"})
        source_line = (f"Carfax {short_day(main['date'])} at {main['miles']:,} mi, {d.get('condition', 'good')} condition"
                       + (f", {_pretty(d['trim'])}" if d.get("trim") else "")) if main else ""
    else:
        table.append({"label": "Estimate", "source": "Redfin", "value": main["value"] if main else None, "primary": True})
        if d.get("last_sold"):
            table.append({"label": _sentence(d["last_sold"].get("label") or "Last sold"), "source": "Redfin",
                          "value": d["last_sold"]["amount"], "primary": False})
        source_line = f"Redfin {short_day(main['date'])}" + (f" · {d['preview']}" if d.get("preview") else "") if main else ""
    empty = "no estimate" if main or usage.last_call(conn, SOURCE_OF[kind], row["id"]) else "not checked yet"
    for r in table:
        if r["value"] is None:
            r["empty"] = empty
    loan = linked_loan(conn, row, today)
    if loan:
        worth = row["balance"]
        loan["equity"] = round(worth - loan["balance"], 2)
        loan["loan_share"] = round(min(max(loan["balance"] / worth, 0.0), 1.0), 4) if worth > 0 else None
    points = [{"date": x["date"], "value": x["value"], "source": x["source"]} for x in history(conn, row["id"])]
    out: dict[str, Any] = {
        "id": row["id"], "kind": kind, "name": row["name"], "archived": bool(row["archived_at"]),
        "title": vehicle_title(row, d) if kind == "vehicle" else row["name"],
        "value": value, "table": table, "source_line": source_line,
        "sell": ({"label": SELL_URL[kind][0], "url": SELL_URL[kind][1]} if kind in SELL_URL else None),
        "loan": loan, "linked_loan_id": row["linked_loan_id"], "history": points,
        "photo": photos.view(conn, row),
        "notes": {"odometer": v["odometer_hint"],
                  "blocked": src["note"] if src["blocked_until"] else None,
                  "no_estimate": (src["note"] if src["note"] and not src["blocked_until"] and not main else None),
                  "your_value": (f"Your value {_money(row['balance'])} counts in net worth"
                                 if not following and eff else None)},   # "Use the estimate" only when there is one
        "details": {"next_check": v["next_check"], "value_source": v["source"], "can_refresh": v["can_refresh"],
                    "refresh_note": v["refresh_note"], "provider_label": v["provider_label"], "enabled": v["enabled"]},
        "valuation": v,
    }
    if kind == "vehicle":
        kept = vin_specs(conn, row["vin"])
        nhtsa = kept or {}
        out["specs"] = _spec_rows(nhtsa, d)
        out["safety"] = _safety(nhtsa)
        # Why the safety list can be empty: no VIN, a decode that predates the specs (fetched at the next value check),
        # or NHTSA really lists none. The page said "lists none" for the second one (a real car, 2026-09-27).
        out["safety_state"] = "no_vin" if not row["vin"] else "not_fetched" if kept is None else "fetched"
        out["vin"] = row["vin"]
        out["vin_masked"] = ("•" * 11 + row["vin"][-6:]) if row["vin"] else None
        out["records"] = d.get("records")
        out["details"].update({"mileage": row["mileage"], "mileage_as_of": row["mileage_as_of"],
                               "condition": row["vehicle_condition"] or manual.DEFAULT_CONDITION,
                               "zip_code": row["zip_code"]})
    else:
        out["facts"] = {"beds": d.get("beds"), "baths": d.get("baths"), "sqft": d.get("sqft"), "last_sold": d.get("last_sold"),
                        "redfin_url": f"https://www.redfin.com{d['url']}" if str(d.get("url") or "").startswith("/") else None}
        out["details"].update({"address": row["address"], "unit": parse_address(row["address"]).unit})
    return out

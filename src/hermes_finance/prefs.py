"""Typed app settings (TASK-18): the Settings page's values, one JSON value per key in the `settings` table.

Every key has a type, a default and a check; `get()` never returns an invalid value (a bad or missing row reads as the
default) and `save()` validates everything before writing anything. Who reads what:

  sync.*, alerts.*           the crons (`finance settings --json`); nothing in the app schedules or sends yet
  ai.mode                    `finance ai-queue` queues nothing when it's "off"; ai.min_confidence: `finance ai-apply`'s
                             auto-apply threshold (ai.MIN_CONFIDENCE is its default); ai.preset / base_url / model /
                             monthly_cap are stored for the chat loop (a later task); the key is in the secrets file
  mcp.enabled                /mcp answers 404 and `finance mcp` refuses to start while it's off
  mcp.allow_writes           TASK-17's switch (mcp_state.py), shown and set here too
  valuations.*               config.free_sources(): Carfax, Redfin and the photos (an env-file flag still wins)
  region.*                   the page's number / date formatting (Intl.NumberFormat); week_start for the bills
                             calendar; budget_month_start is day 1 only for now (budget math is calendar months)
  appearance.theme           the page's theme (dark / light / system)
  access.*                   access.py (the three access modes)
  plaid.plan                 TASK-22: trial | paid (Plaid has no API for it); plaid.on_demand_refresh: whether "Ask banks
                             for fresh data" may call Plaid's refresh endpoints (unset: on in Sandbox or on Trial,
                             syncnow.refresh_policy); TASK-22g: a plan change unsets it (save)

Stdlib only (the Hermes plugin imports the read side under its own Python).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from .db import now

DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
# ISO 4217 codes offered in Settings (display only: amounts are never converted)
CURRENCIES = ("USD", "EUR", "GBP", "CAD", "AUD", "NZD", "CHF", "JPY", "SEK", "NOK", "DKK", "PLN", "CZK", "MXN", "BRL",
              "INR", "SGD", "HKD", "ZAR", "ILS", "KRW", "TRY", "UAH")
LOCALES = ("en-US", "en-GB", "en-CA", "en-AU", "en-IN", "de-DE", "fr-FR", "es-ES", "es-MX", "it-IT", "nl-NL", "pt-BR",
           "pl-PL", "sv-SE", "ja-JP", "uk-UA")
AI_PRESETS = {"openrouter": "https://openrouter.ai/api/v1", "openai": "https://api.openai.com/v1",
              "local": "http://127.0.0.1:11434/v1", "custom": ""}
ACCESS_MODES = ("local", "proxy", "builtin")
TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
MAX_TEXT = 120
MAX_LIST = 20


class PrefError(ValueError):
    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class Pref:
    default: Any
    check: Callable[[Any], Any]   # returns the clean value or raises PrefError (without a field; save() adds it)
    public: bool = True           # in /api/settings and `finance settings --json`


def _bool(v):
    if not isinstance(v, bool):
        raise PrefError("must be true or false")
    return v


def _choice(options):
    def check(v):
        if v not in options:
            raise PrefError(f"must be one of {', '.join(map(str, options))}")
        return v
    return check


def _time(v):
    if not isinstance(v, str) or not TIME_RE.fullmatch(v):
        raise PrefError("must be a time as HH:MM (24-hour)")
    return v


def _text(max_len=MAX_TEXT, empty=False):
    def check(v):
        if not isinstance(v, str):
            raise PrefError("must be text")
        v = " ".join(v.split())
        if not v and not empty:
            raise PrefError("can't be empty")
        if len(v) > max_len:
            raise PrefError(f"must be at most {max_len} characters")
        return v
    return check


def _number(lo, hi, step=None):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not lo <= v <= hi:
            raise PrefError(f"must be a number from {lo} to {hi}")
        v = round(float(v), 2)
        if step and abs(round(v / step) * step - v) > 1e-9:
            raise PrefError(f"must be a multiple of {step}")
        return v
    return check


def _url(v):
    if not isinstance(v, str):
        raise PrefError("must be a URL")
    v = v.strip()
    if v and (len(v) > 300 or not re.fullmatch(r"https?://[^\s\"'<>]+", v)):
        raise PrefError("must be an http(s) URL")
    return v.rstrip("/")


def _string_list(item_check, max_items=MAX_LIST):
    def check(v):
        if not isinstance(v, list) or len(v) > max_items:
            raise PrefError(f"must be a list of at most {max_items}")
        out = []
        for x in v:
            x = item_check(x)
            if x not in out:
                out.append(x)
        return out
    return check


def _identity(v):
    v = _text(200)(v).lower()
    if " " in v:
        raise PrefError("an identity is one word (a login or an email address)")
    return v


def _header(v):
    v = _text(60)(v)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", v):
        raise PrefError("must be an HTTP header name (letters, digits and dashes)")
    return v


SPEC: dict[str, Pref] = {
    "sync.daily_time": Pref("06:30", _time),
    "sync.weekly_day": Pref("sun", _choice(DAYS)),
    "sync.weekly_time": Pref("18:00", _time),
    "alerts.cap_80": Pref(True, _bool),
    "alerts.over_cap": Pref(True, _bool),
    "alerts.broken_connection": Pref(True, _bool),
    "alerts.destination": Pref("Hermes chat", _text(60)),
    "ai.mode": Pref("hermes", _choice(("hermes", "api", "off"))),
    "ai.preset": Pref("openrouter", _choice(tuple(AI_PRESETS))),
    "ai.base_url": Pref(AI_PRESETS["openrouter"], _url),
    "ai.model": Pref("", _text(120, empty=True)),
    "ai.monthly_cap": Pref(5.0, _number(0, 1000)),
    "ai.min_confidence": Pref(0.7, _number(0.5, 0.95, 0.05)),
    "mcp.enabled": Pref(True, _bool),
    "mcp.allow_writes": Pref(False, _bool, public=False),   # mcp_state.py's key (TASK-17); /api/settings shows it as mcp
    "valuations.carfax": Pref(True, _bool),
    "valuations.redfin": Pref(True, _bool),
    "valuations.photos": Pref(True, _bool),
    "region.currency": Pref("USD", _choice(CURRENCIES)),
    "region.locale": Pref("en-US", _choice(LOCALES)),
    "region.week_start": Pref("sun", _choice(("sun", "mon"))),
    "region.budget_month_start": Pref(1, _choice((1,))),   # v1: calendar months only
    "appearance.theme": Pref("dark", _choice(("dark", "light", "system"))),
    # access.py owns these (validated again there, against each other, before a mode can be chosen) and shows them
    # itself: the identities only masked
    "access.mode": Pref(None, _choice(ACCESS_MODES), public=False),
    "access.proxy_header": Pref("Tailscale-User-Login", _header, public=False),
    "access.allowed_identities": Pref([], _string_list(_identity), public=False),
    "access.public_urls": Pref([], _string_list(_text(300), 10), public=False),
    "access.last_proxy": Pref(None, lambda v: v if isinstance(v, dict) else None, public=False),
    # first run (admin.py): the set-up finished or skipped; demo mode (Plaid Sandbox only, never a Production Item)
    "setup.done": Pref(False, _bool, public=False),
    "plaid.demo": Pref(False, _bool, public=False),
    # TASK-22: Settings -> Connections (shown there with what they mean, syncnow.refresh_policy)
    "plaid.plan": Pref("trial", _choice(("trial", "paid")), public=False),
    "plaid.on_demand_refresh": Pref(None, _bool, public=False),
}
# what the Settings page saves through POST /api/prefs (the access.* keys go through their own routes)
PAGE_KEYS = tuple(k for k in SPEC if not k.startswith(("access.", "setup.", "plaid."))) + ("plaid.plan",
                                                                                         "plaid.on_demand_refresh")


def _raw(conn: sqlite3.Connection, keys) -> dict[str, Any]:
    keys = list(keys)
    marks = ", ".join("?" * len(keys))
    out = {}
    for key, value in conn.execute(f"SELECT key, value FROM settings WHERE key IN ({marks})", keys):
        try:
            out[key] = json.loads(value)
        except ValueError:
            continue
    return out


def get(conn: sqlite3.Connection, key: str) -> Any:
    return get_many(conn, [key])[key]


def get_many(conn: sqlite3.Connection, keys=None) -> dict[str, Any]:
    """{key: value}: the saved value when it's valid, else the default."""
    keys = list(SPEC) if keys is None else list(keys)
    raw = _raw(conn, keys)
    out = {}
    for k in keys:
        spec = SPEC[k]
        try:
            out[k] = spec.check(raw[k]) if k in raw and raw[k] is not None else spec.default
        except PrefError:
            out[k] = spec.default
    return out


def validate(data: dict[str, Any], allowed=None) -> dict[str, Any]:
    allowed = set(SPEC) if allowed is None else set(allowed)
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise PrefError(f"unknown setting {unknown[0]!r}", unknown[0])
    clean = {}
    for k, v in data.items():
        try:
            clean[k] = SPEC[k].check(v)
        except PrefError as e:
            raise PrefError(f"{k} {e}", k) from None
    return clean


def save(conn: sqlite3.Connection, data: dict[str, Any], allowed=None) -> dict[str, Any]:
    """Validate every value, then write them all in one transaction. Returns the clean values.

    TASK-22g: a different plaid.plan clears plaid.on_demand_refresh (back to the new plan's default: on for Trial, off
    for Paid), so a switch turned on while free doesn't stay on once Plaid bills each refresh. Saving the same plan keeps
    it, and so does a save that sets the switch too."""
    if not isinstance(data, dict) or not data:
        raise PrefError("expected at least one setting")
    clean = validate(data, allowed)
    with conn:
        if ("plaid.plan" in clean and "plaid.on_demand_refresh" not in clean
                and clean["plaid.plan"] != get(conn, "plaid.plan")):
            conn.execute("DELETE FROM settings WHERE key = 'plaid.on_demand_refresh'")
        for k, v in clean.items():
            conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                         "SET value = excluded.value, updated_at = excluded.updated_at", (k, json.dumps(v), now()))
    return clean


def public(conn: sqlite3.Connection) -> dict[str, Any]:
    """Every public setting as {group: {name: value}} (for /api/settings and `finance settings --json`)."""
    values = get_many(conn, [k for k, s in SPEC.items() if s.public])
    out: dict[str, dict[str, Any]] = {}
    for k, v in values.items():
        group, _, name = k.partition(".")
        out.setdefault(group, {})[name] = v
    return out


def valuation_switches(conn: sqlite3.Connection) -> dict[str, bool]:
    """{carfax, redfin, photos}: only the ones saved on the Settings page (config.free_sources adds env and defaults)."""
    raw = _raw(conn, ["valuations.carfax", "valuations.redfin", "valuations.photos"])
    return {k.split(".")[1]: v for k, v in raw.items() if isinstance(v, bool)}


def min_confidence(conn: sqlite3.Connection, default: float) -> float:
    raw = _raw(conn, ["ai.min_confidence"]).get("ai.min_confidence")
    try:
        return SPEC["ai.min_confidence"].check(raw) if raw is not None else default
    except PrefError:
        return default

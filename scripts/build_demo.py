#!/usr/bin/env python3
"""Build dist/demo/: the real UI with read-only, sanitized synthetic GET snapshots.

Cold clone: uv run python scripts/build_static_demo.py --synthetic-only --today 2026-10-24
Optional --serve uses only the standard-library static server. No deployment backend or model.
Publication still requires demo_canary.py with an explicit private list; see docs/demo.md.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib
import io
import html as html_module
import json
import os
import pkgutil
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "hermes_finance" / "static"
DEMO_SRC = ROOT / "demo"
DEMO_FILES = ("shim.js", "demo.css", "loader.js", "ask.js")   # demo-only code; static/ stays byte-identical
DEMO_LOGOS = DEMO_SRC / "logos"   # invented logos for the fictional banks and merchants, plus Alpaca's own (t_4cb4d435)
MERCHANT_LOGOS = "/api/merchant-logos/"
KIND = "features"          # tests/gate_dbs.py: the fullest synthetic DB (every page and feature in use)
MONEY_SCALE = 1.07         # every money value of the generated DB is multiplied by this (see sanitize_db)
SHARE_SCALE = 1.03         # a holding's quantity is multiplied by this and its price by MONEY_SCALE / SHARE_SCALE
HOST = {"Host": "127.0.0.1:8765"}
# environment variables that may carry a provider credential: never passed on (the build itself reads none)
CREDENTIAL_RE = re.compile(r"PLAID|APCA|ALPACA|MARKETCHECK|RENTCAST|OPENROUTER|ANTHROPIC|OPENAI|HERMES_API|(?:^|_)(?:KEY|SECRET|TOKEN|PASSWORD)(?:_|$)")
# the paths the Settings page prints, as a stranger's machine would show them
FAKE_HOME = "~"
FAKE_PATHS = {"data": "~/hermes-finance/data", "secrets": "~/.config/hermes-finance/secrets.env",
              "alpaca": "~/.config/hermes-finance/alpaca-live.env", "venv_bin": "~/hermes-finance/.venv/bin"}
BRAIN_LABEL = "canned demo answers"   # the Ask panel's chip: never a model name
# what index.html changes from the App's own page (the report lists these)
PAGE_CHANGES = [
    "<script src=/demo/shim.js> added first in <head> (before theme.js): the fetch shim, the pinned clock, the banner",
    "<link rel=stylesheet href=/demo/demo.css> added after the app's stylesheets (the banner, and the offsets that keep it clear)",
    "<meta http-equiv=Content-Security-Policy> added: the app's CSP without Plaid (self only: no request leaves the page)",
    "the Plaid Link <script> (cdn.plaid.com) removed: connecting a bank isn't part of the demo",
    f"data-ask-brain set to '{BRAIN_LABEL}' (the Ask chip; the App would name the model)",
    "stylesheets and deferred scripts are loaded sequentially by /demo/loader.js (unchanged product files, plain http.server backlog safe)",
    "<title> gets a 'Demo · ' prefix and <meta name=robots content=noindex> is added",
]
CSP = "; ".join(["default-src 'self'", "script-src 'self'", "connect-src 'self'", "img-src 'self' data:",
                 "style-src 'self' 'unsafe-inline'", "font-src 'self'", "manifest-src 'self'", "base-uri 'none'",
                 "form-action 'none'"])


# --- 1. a safe process: no credential, no network, a pinned clock ------------------------------------------------------

def safe_env(tmp: Path) -> dict[str, str]:
    """The environment for this build (and the canary it starts): credentials dropped, every outbound switch off, the
    app's files in `tmp`. Set before hermes_finance is imported."""
    for k in [k for k in os.environ if CREDENTIAL_RE.search(k)]:
        del os.environ[k]
    env_file = tmp / "demo.env"
    env_file.write_text("CARFAX_ENABLED=1\nREDFIN_ENABLED=1\nPHOTOS_ENABLED=1\n")   # the pages show them on, as the gates do
    os.environ.update({
        "TZ": "UTC", "HERMES_PLAID": "off", "HERMES_ALPACA": "off", "HERMES_CHROME_DEVTOOLS": "off",
        "HERMES_ENV_FILE": str(env_file), "FINANCE_SECRETS_FILE": str(tmp / "secrets.env"),
        "FINANCE_DATA_DIR": str(tmp / "data"), "HERMES_HOME": str(tmp / "no-hermes-home"),
        "HERMES_ALPACA_KEY_FILE": str(tmp / "no-alpaca.env"), "FINANCE_HERMES_URL": "http://127.0.0.1:9/v1",
        "FINANCE_HERMES_BIN": str(tmp / "no-hermes-bin"), "PYTHONDONTWRITEBYTECODE": "1"})
    time.tzset()
    return {"data": str(tmp / "data"), "secrets": str(tmp / "secrets.env"), "alpaca": str(tmp / "no-alpaca.env"),
            "venv_bin": str(Path(sys.executable).parent), "tmp": str(tmp)}


def _loopback(address) -> bool:
    if not isinstance(address, tuple):
        return True
    host = str(address[0]).split("%")[0]
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith("127.")


def no_network() -> None:
    """Below everything: a socket may only reach (and a name only resolve to) loopback; urllib refuses outright."""
    real_connect, real_connect_ex, real_gai = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def refuse(*a, **k):
        raise RuntimeError("build_demo: network access refused")

    def connect(sock, address):
        if not _loopback(address):
            refuse()
        return real_connect(sock, address)

    def connect_ex(sock, address):
        if not _loopback(address):
            refuse()
        return real_connect_ex(sock, address)

    def getaddrinfo(host, *a, **k):
        if host is not None and not _loopback((host.decode() if isinstance(host, bytes) else host, 0)):
            refuse()
        return real_gai(host, *a, **k)

    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, getaddrinfo
    urllib.request.urlopen = refuse
    urllib.request.OpenerDirector.open = refuse


def offline_app() -> None:
    """The app's own outbound paths, off (as scripts/run_gates.py and tests/conftest.py do)."""
    from hermes_finance import alpaca, photos, valuation

    class NoChrome:
        def available(self):
            return "Chrome not running here (the demo build has no browser)"

        def tab(self, url):
            raise RuntimeError("build_demo: no browser")

    def refuse(*a, **k):
        raise RuntimeError("build_demo: provider call refused")

    valuation.transport, valuation.browser = refuse, NoChrome()
    photos.schedule, photos.cutter, photos.sleep = (lambda *a, **k: None), refuse, (lambda s: None)
    alpaca.transport = refuse


class _Time(types.ModuleType):
    """The time module with time() pinned (monotonic, sleep, strftime... are the real ones)."""

    def __init__(self, at: float):
        super().__init__("time")
        self._at = at

    def time(self):
        return self._at

    def __getattr__(self, name):
        return getattr(time, name)


class _Secrets(types.ModuleType):
    """secrets with a seeded source: the MCP tokens the generator makes (shown masked) are the same on every build."""

    def __init__(self, seed: int):
        super().__init__("secrets")
        self._rnd = random.Random(seed)

    def token_bytes(self, n=32):
        return bytes(self._rnd.getrandbits(8) for _ in range(n))

    def token_hex(self, n=32):
        return self.token_bytes(n).hex()

    def token_urlsafe(self, n=32):
        return base64.urlsafe_b64encode(self.token_bytes(n)).rstrip(b"=").decode()

    def __getattr__(self, name):
        import secrets
        return getattr(secrets, name)


def freeze_clock(now: datetime) -> None:
    """Pin "now" (and today) in every hermes_finance module and the generator: the build is the same on any day it runs
    for the same --today, and the page's pinned clock (shim.js) agrees with every timestamp in the data."""
    import datetime as dt
    for info in pkgutil.iter_modules([str(ROOT / "src" / "hermes_finance")]):
        try:
            importlib.import_module(f"hermes_finance.{info.name}")
        except ImportError:   # an optional extra (mcp) that isn't installed
            pass
    day = now.date()

    class Meta(type):   # a date / datetime made anywhere else still passes isinstance(x, date) in a patched module
        def __instancecheck__(cls, obj):
            return isinstance(obj, cls.__mro__[1])

        def __subclasscheck__(cls, sub):
            return issubclass(sub, cls.__mro__[1])

    class FrozenDate(dt.date, metaclass=Meta):
        @classmethod
        def today(cls):
            return cls(day.year, day.month, day.day)

    class FrozenDateTime(dt.datetime, metaclass=Meta):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return now.replace(tzinfo=None)

        @classmethod
        def today(cls):
            return now.replace(tzinfo=None)

    pinned_time = _Time(now.timestamp())
    for name, mod in list(sys.modules.items()):
        if not (name.startswith("hermes_finance") or name in ("demo_data", "gate_dbs")) or mod is None:
            continue
        if getattr(mod, "date", None) is dt.date:
            mod.date = FrozenDate
        if getattr(mod, "datetime", None) is dt.datetime:
            mod.datetime = FrozenDateTime
        if getattr(mod, "time", None) is time:
            mod.time = pinned_time
        if getattr(mod, "secrets", None) is not None and getattr(mod.secrets, "__name__", "") == "secrets":
            mod.secrets = _Secrets(7)


# --- 2. the generator, then the sanitizer ----------------------------------------------------------------------------

GENERATOR_SEED = 4   # tests/demo_data.py build()'s own default: seed 0 is the generator's usual DB
SEEDS = range(7)     # the weekly variants (demo_weekly.sh): one per weekday, Monday 0
SALTS = range(5)     # a variant whose figures show a generator fixed amount (by chance) is built again with the next
                     # salt (demo_canary.py --fixed-amounts); the same seed and day still build the same bytes


def generate(path: Path, today: date, seed: int = 0, salt: int = 0) -> None:
    """The synthetic DB. `seed` 1-6 re-seeds the generator's discretionary spending (how many payments a merchant gets
    each month and their amounts), so a variant has the same structure, accounts, bills and names with a different mix
    of amounts; seed 0 is the usual DB. `salt` re-seeds it again (see SALTS)."""
    sys.path.insert(0, str(ROOT / "tests"))
    import demo_data
    import gate_dbs
    real = demo_data.build
    if seed or salt:
        demo_data.build = lambda conn, today=None, seed_=None: real(conn, today, seed=GENERATOR_SEED + 101 * seed + 1009 * salt)
    try:
        gate_dbs.build(KIND, path, today)
    finally:
        demo_data.build = real
    from hermes_finance import db
    conn = db.connect(path)
    gate_dbs.add_group_budgets(conn)
    conn.close()


def _scale(x, k=MONEY_SCALE, places=2):
    return None if x is None else round(x * k + 0.0, places)


MONEY_COLUMNS = {   # table: money columns (scaled by MONEY_SCALE); a key column makes the UPDATE exact
    "group_budgets": ("group_id", ("amount",)),
    "accounts": ("account_id", ("current_balance", "available_balance", "credit_limit")),
    "balance_snapshots": (("account_id", "date"), ("current", "available")),
    "transactions": ("transaction_id", ("amount",)),
    "liabilities": (("account_id", "kind"), ("last_statement_balance", "minimum_payment_amount", "last_payment_amount")),
    "manual_accounts": ("id", ("balance", "credit_limit", "original_amount", "monthly_payment")),
    "manual_balance_history": (("manual_account_id", "date"), ("balance",)),
    "asset_valuations": ("id", ("value", "low", "high")),
    "goals": ("id", ("target", "earmark", "start_balance")),
    "goal_entries": ("id", ("amount",)),
    "tracked_bills": ("id", ("usual_low", "usual_high", "anchor_amount")),
    "holdings": (("account_id", "security_id"), ("institution_value", "cost_basis")),
    "holding_snapshots": (("date", "account_id", "security_id"), ("value",)),
    "alpaca_snapshots": ("date", ("cash", "equity", "reported_equity")),
}


def _keyed_update(conn, table: str, key, cols, fn) -> None:
    keys = (key,) if isinstance(key, str) else key
    rows = conn.execute(f"SELECT {', '.join(keys + cols)} FROM {table}").fetchall()
    for r in rows:
        vals = [fn(c, r[c]) for c in cols]
        conn.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in cols)} WHERE "
                     + " AND ".join(f"{k} IS ?" for k in keys), (*vals, *[r[k] for k in keys]))


def _money_text(text: str | None, k: float = MONEY_SCALE) -> str | None:
    """"$432,000" / "$247K" inside a stored sentence, scaled like the numbers around it."""
    if not text:
        return text

    def one(m):
        v = float(m.group(2).replace(",", "")) * (1000 if m.group(3) else 1)
        v *= k
        return f"{m.group(1)}${v / 1000:,.0f}K" if m.group(3) else f"{m.group(1)}${v:,.{2 if '.' in m.group(2) else 0}f}"
    return re.sub(r"([+-]?)\$([0-9][0-9,]*(?:\.[0-9]+)?)(K?)", one, text)


def sanitize_db(conn: sqlite3.Connection, today: date, now: datetime, tmp_paths: dict[str, str]) -> dict[str, int]:
    """The generated DB, made publishable before any page is asked for: money scaled (MONEY_SCALE: a linear map keeps
    every sum, ratio, cap status and payment match), remote logos dropped (the page draws initials, as it does for a
    merchant without one), pictures credited as the synthetic images they are, the proxy URL a reserved example name,
    the VIN plant and ZIP generic, the sync 42 minutes before the pinned clock. Returns counts for the build log."""
    conn.row_factory = sqlite3.Row
    counts = {}
    with conn:
        for table, (key, cols) in MONEY_COLUMNS.items():
            _keyed_update(conn, table, key, cols, lambda c, v: _scale(v))
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        # a holding: neither its share count nor its price is the generator's, and quantity x price is still its value
        def share(c, v):
            return _scale(v, SHARE_SCALE, 6) if c == "quantity" else _scale(v, MONEY_SCALE / SHARE_SCALE)
        _keyed_update(conn, "holdings", ("account_id", "security_id"), ("quantity", "institution_price"), share)
        _keyed_update(conn, "holding_snapshots", ("date", "account_id", "security_id"), ("quantity", "price"), share)
        # caps: whole dollars, as a person sets them; APRs scaled too (the payoff dates follow)
        _keyed_update(conn, "categories", "id", ("monthly_cap",), lambda c, v: None if v is None else float(round(v * MONEY_SCALE)))
        _keyed_update(conn, "manual_accounts", "id", ("interest_rate",), lambda c, v: _scale(v))
        _keyed_update(conn, "liabilities", ("account_id", "kind"), ("interest_rate",), lambda c, v: _scale(v))
        # a split's lines still add up to its transaction: the last line takes the cent of rounding
        for (tid,) in conn.execute("SELECT DISTINCT transaction_id FROM transaction_splits").fetchall():
            lines = conn.execute("SELECT id, amount FROM transaction_splits WHERE transaction_id = ? ORDER BY line", (tid,)).fetchall()
            total = conn.execute("SELECT amount FROM transactions WHERE transaction_id = ?", (tid,)).fetchone()[0]
            scaled = [_scale(r["amount"]) for r in lines]
            scaled[-1] = round(total - sum(scaled[:-1]), 2)
            for r, v in zip(lines, scaled):
                conn.execute("UPDATE transaction_splits SET amount = ? WHERE id = ?", (v, r["id"]))
        # a loan payment is still its interest plus its principal
        for r in conn.execute("SELECT rowid, amount, interest FROM loan_payments").fetchall():
            amount, interest = _scale(r["amount"]), _scale(r["interest"])
            conn.execute("UPDATE loan_payments SET amount = ?, interest = ?, principal = ? WHERE rowid = ?",
                         (amount, interest, None if interest is None else round(amount - interest, 2), r["rowid"]))
        # money inside stored JSON / text
        for r in conn.execute("SELECT date, positions_json FROM alpaca_snapshots").fetchall():
            pos = json.loads(r["positions_json"] or "[]")
            for p in pos:
                for f, k, places in (("qty", SHARE_SCALE, 6), ("price", MONEY_SCALE / SHARE_SCALE, 2), ("value", MONEY_SCALE, 2),
                                     ("cost_basis", MONEY_SCALE, 2)):
                    if isinstance(p.get(f), (int, float)):
                        p[f] = _scale(p[f], k, places)
            conn.execute("UPDATE alpaca_snapshots SET positions_json = ? WHERE date = ?", (json.dumps(pos), r["date"]))
        for r in conn.execute("SELECT id, source, details_json FROM asset_valuations").fetchall():
            d = json.loads(r["details_json"]) if r["details_json"] else None
            if not d:
                continue
            for f in ("trade_in", "private_sale", "dealer_retail"):
                if isinstance(d.get(f), (int, float)):
                    d[f] = float(round(d[f] * MONEY_SCALE))
            if isinstance(d.get("last_sold"), dict) and isinstance(d["last_sold"].get("amount"), (int, float)):
                d["last_sold"]["amount"] = float(round(d["last_sold"]["amount"] * MONEY_SCALE))
            if d.get("preview"):
                d["preview"] = _money_text(d["preview"])
            # a listing's identity and place: generic (the address itself is renamed with the account's)
            for f in ("url", "photo_url"):
                if f in d:
                    d[f] = None
            for f in ("lat", "lng"):
                if isinstance(d.get(f), (int, float)):
                    d[f] = round(d[f], 1)
            if "zip" in d:
                d["zip"] = "00000"
            if "colour" in d:
                d["colour"] = "Silver"
            conn.execute("UPDATE asset_valuations SET details_json = ?, raw_json = '{}' WHERE id = ?", (json.dumps(d), r["id"]))
        for r in conn.execute("SELECT rowid, transaction_id, raw_json FROM transactions").fetchall():
            raw = json.loads(r["raw_json"] or "{}")
            raw.pop("logo_url", None)
            raw.pop("personal_finance_category_icon_url", None)
            if isinstance(raw.get("amount"), (int, float)):
                raw["amount"] = _scale(raw["amount"])
            conn.execute("UPDATE transactions SET raw_json = ?, logo_url = NULL WHERE rowid = ?", (json.dumps(raw), r["rowid"]))
        conn.execute("UPDATE holdings SET raw_json = NULL")
        conn.execute("UPDATE liabilities SET raw_json = NULL")
        # pictures: the generator draws them (tests/pngtools); say so instead of crediting a photographer or a map
        conn.execute("UPDATE asset_images SET credit = CASE WHEN file IS NULL THEN NULL ELSE 'Synthetic demo image' END, "
                     "credit_url = NULL, artist = NULL, license = NULL, note = 'Synthetic demo image', "
                     "lat = ROUND(lat, 1), lng = ROUND(lng, 1)")
        conn.execute("UPDATE institutions SET url = NULL")
        conn.execute("UPDATE manual_accounts SET zip_code = '00000' WHERE zip_code IS NOT NULL")
        for r in conn.execute("SELECT vin, specs_json FROM vin_decodes").fetchall():
            specs = json.loads(r["specs_json"] or "{}")
            for group in specs.values():
                if isinstance(group, dict):
                    for f in ("PlantCity", "PlantCountry", "PlantState", "PlantCompanyName"):
                        if f in group:
                            group[f] = "EXAMPLE"
            conn.execute("UPDATE vin_decodes SET specs_json = ? WHERE vin = ?", (json.dumps(specs), r["vin"]))
        # the last sync: 42 minutes before the pinned clock (the generator's own "42 minutes ago")
        conn.execute("UPDATE items SET last_sync_at = ? WHERE last_sync_at IS NOT NULL",
                     ((now - timedelta(minutes=42)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
        conn.execute("UPDATE items SET access_token = 'access-demo-not-a-token', cursor = NULL")
    # the proxy mode's public URL and its last request: a reserved example name (the generator's is a tailnet-shaped one)
    from hermes_finance import bills, prefs
    proxy = prefs.get(conn, "access.last_proxy")
    # (and the AI model's name, so nothing on the Settings page names a model either: the Ask panel answers canned)
    updates = {"access.public_urls": ["https://finance.example.com"], "ai.model": ""}
    if isinstance(proxy, dict):
        updates["access.last_proxy"] = {**proxy, "host": "finance.example.com", "at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    prefs.save(conn, updates, allowed=list(prefs.SPEC))
    # an agent's pending change states its amount in words: said again from the scaled data (bills.plan, as made)
    for r in conn.execute("SELECT id, tool, arguments FROM pending_actions WHERE status = 'pending'").fetchall():
        if r["tool"] == "finance_track_bill":
            args = json.loads(r["arguments"])
            summary = bills.plan(conn, today, **args)["summary"]
            with conn:
                conn.execute("UPDATE pending_actions SET summary = ? WHERE id = ?", (summary, r["id"]))
    conn.row_factory = None
    return counts


# --- the rename map ------------------------------------------------------------------------------------------------------

INSTITUTIONS = {   # by what the institution is to the account holder (a connected bank, a manual account's kind)
    "bank": ["Otterly Bank", "Harbor & Pine Bank", "Moonbeam Credit Union", "Stackwell Savings", "Copperleaf Trust",
             "Kitebird Bank"],
    "loan": ["Lendlark", "Summit & Sail Finance"], "credit_card": ["Orbit Card Co", "Prism Card Services"],
    "investment": ["Ridgeline Investments"]}
# when every pool above is used up (an institution then shows its colour and initials: it has no logo)
INSTITUTION_OVERFLOW = ["Bluebell Bank", "Tidewater Trust", "Lanternfish Savings", "Quarry Hill Credit Union"]
MERCHANTS = {   # by Plaid's detailed category: invented names, each with its own invented logo in demo/logos (t_d31fda2a)
    "FOOD_AND_DRINK_GROCERIES": ["Leaf & Ladle Market", "Basket Bros", "Sunny Crate Grocers"],
    "FOOD_AND_DRINK_RESTAURANT": ["Burrito Galaxy", "Ember & Oak Grill", "Noodle Thunder", "Midnight Diner Co",
                                  "Wingspan Bistro"],
    "FOOD_AND_DRINK_COFFEE": ["Bean Voyage"],
    "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES": ["Parcel Panda"],
    "GENERAL_MERCHANDISE_SUPERSTORES": ["Hyperbox Superstore"],
    "TRANSPORTATION_GAS": ["Zapfuel"],
    "TRANSPORTATION_TAXIS_AND_RIDE_SHARES": ["Zoomly Rides"],
    "TRANSPORTATION_PARKING": ["Spotfinder Parking"],
    "MEDICAL_PHARMACIES_AND_SUPPLEMENTS": ["Pillbox & Co"],
    "MEDICAL_DENTAL_CARE": ["Brightbite Dental"],
    "MEDICAL_OTHER_MEDICAL": ["Pillbox & Co", "Brightbite Dental"],   # (the generator files its pharmacy and dentist here)
    "ENTERTAINMENT_TV_AND_MOVIES": ["Popcorn Palace Cinemas", "Glowbox Streaming"],
    "ENTERTAINMENT_MUSIC_AND_AUDIO": ["Bassline Music"],
    "TRAVEL_FLIGHTS": ["Skylark Airways"],
    "RENT_AND_UTILITIES_RENT": ["Nestwell Rentals"],
    "RENT_AND_UTILITIES_GAS_AND_ELECTRICITY": ["Pilot Light Gas", "Voltage Valley Power"],
    "RENT_AND_UTILITIES_TELEPHONE": ["Chirp Wireless"],
    "RENT_AND_UTILITIES_WATER": ["Riverbend Water & Power"],
    "RENT_AND_UTILITIES_INTERNET_AND_CABLE": ["Fiberfox Internet"],
    "GENERAL_SERVICES_INSURANCE": ["Shieldwell Insurance"],
    "GENERAL_SERVICES_OTHER_GENERAL_SERVICES": ["Neuronest AI", "Cumulus Cloud", "Domainly", "Toolsmith", "Toolsmith Pro"],
}
# a merchant its category's pool has no name left for (a tracked bill, a second stationer): the next of these, in order
MERCHANT_OVERFLOW = ["Tagline Thrift", "Threadhaus Boutique", "Corner Pocket Goods", "Lumen Lane Shop",
                     "Velvet Fox Outfitters", "Pebble & Pine Supply", "Kiwi Kite Co", "Mango Mondays"]
# placeholder names the demo once used (Microsoft's sample-company family and plain "Example"): none may be shown
RETIRED_NAMES = re.compile(r"(?<![A-Za-z0-9.@/])(?:example(?![.-](?:com|org|net)\b)|contoso|northwind|fabrikam|woodgrove|"
                           r"tailspin|relecloud|litware|lucerne|adatum|margie's|proseware|wingtip|lamna|fourth coffee|"
                           r"blue yonder|tunewave|acme|globex|initech|roadrunner)(?![A-Za-z0-9])", re.I)
ACCOUNT_NAMES = {   # (type, subtype) of a Plaid account, or a manual account's kind
    ("depository", "checking"): ["Everyday Checking", "Bills Checking"],
    ("depository", "savings"): ["High-Yield Savings", "Emergency Vault"],
    ("investment", "brokerage"): ["Brokerage", "Robo Portfolio"],
    ("credit", "credit card"): ["Cashback Card", "Travel Rewards Card", "Double Rewards Card"],
    "credit_card": ["Premium Card", "Store Card"], "loan": ["Auto Loan", "Personal Loan"],
    "investment": ["Employer 401(k)"],
    "bank": ["Cash Account"], "other_asset": ["Collectibles"], "other_liability": ["Family Loan"],
}
# real cars (no VIN) and Los Angeles landmarks (never a home), with their frozen real valuations (t_d31fda2a)
ASSETS_FILE = ROOT / "scripts" / "demo_assets.json"
ASSETS = json.loads(ASSETS_FILE.read_text())
VEHICLES = ASSETS["vehicles"]
PROPERTIES = ASSETS["properties"]
PERSON, EMPLOYER = "Robin Fernwood", "000000 PIXELFORGE STUDIOS"
TICKER, SECURITY = "DEMO", "Demo Growth Fund"
VIN = "DEMO-VIN-{:04d}"
KEEP_INSTITUTIONS = {"Alpaca"}   # the app's own Alpaca integration (alpaca.py), not a person's bank
WAGES = "INCOME_WAGES"
P2P = re.compile(r"^(zelle|venmo|cash app|paypal)( payment)? (to|from) (?P<who>.+)$", re.I)
DATED = re.compile(r"^(?P<head>.*?\b\d{2}/\d{2})\s+(?P<tail>.+)$")


SCHEMA_KEY = re.compile(r"[a-z][a-z0-9_]*")   # a payload's own field names: never renamed


class RenameMap:
    """Every identifying value of the generated DB -> its fake stand-in, read from the DB (nothing real is written here).

    names: text found inside strings (case-insensitive, at word edges, longest first; an all-caps or all-lowercase hit
    gets the stand-in in the same case). ids: whole values (account, item, institution, security, transaction ids).
    masks: an account's last four digits, inside text too."""

    def __init__(self, conn: sqlite3.Connection, tmp_paths: dict[str, str]):
        conn.row_factory = sqlite3.Row
        self.names: dict[str, str] = {}
        self.ids: dict[str, str] = {}
        self.masks: dict[str, str] = {}
        self.paths = {tmp_paths[k]: FAKE_PATHS[k] for k in FAKE_PATHS}
        self.paths[tmp_paths["tmp"]] = "~/hermes-finance"
        self.paths[str(Path.home())] = FAKE_HOME
        # the app shows a path under the home folder as "~/...": map that spelling too, or a build dir under $HOME
        # (CI's TMPDIR is ~/work/_temp) leaks its random name and a seed no longer builds the same bytes
        home = str(Path.home())
        for p, fake in list(self.paths.items()):
            if p.startswith(home + os.sep):
                self.paths.setdefault("~" + p[len(home):], fake)
        self.images: dict[str, str] = {}
        self.provenance: dict[str, str] = {}   # stand-in -> what kind of value it replaced (for the report)
        banks = {k: iter(v) for k, v in INSTITUTIONS.items()}
        overflow_banks = iter(INSTITUTION_OVERFLOW)
        overflow = iter(MERCHANT_OVERFLOW)
        taken: set[str] = set()   # a pool name is used once (two categories may share one, e.g. the pharmacy)

        def put(orig, new, kind):
            if orig and orig.strip() and orig not in self.names and orig != new:
                self.names[orig] = new
                self.provenance.setdefault(new, kind)

        # banks: connected ones, then manual accounts' institutions (a lender, a card issuer, a broker by its account)
        insts = [(r[0], "bank") for r in conn.execute("SELECT institution_name FROM items ORDER BY created_at, item_id")]
        insts += [(r[0], "bank") for r in conn.execute("SELECT name FROM institutions ORDER BY institution_id")]
        insts += [(r[0], r[1]) for r in conn.execute("SELECT institution, kind FROM manual_accounts "
                                                     "WHERE institution IS NOT NULL ORDER BY id")]
        for name, kind in insts:
            if name and name not in KEEP_INSTITUTIONS and name not in self.names:
                pool = banks.get(kind, banks["bank"])
                put(name, next(pool, None) or next(banks["bank"], None) or next(overflow_banks), "institution")
        # accounts
        pools: dict = {}
        for r in conn.execute("SELECT account_id, name, type, subtype, mask FROM accounts ORDER BY rowid"):
            pool = pools.setdefault((r["type"], r["subtype"]), list(ACCOUNT_NAMES.get((r["type"], r["subtype"]), ["Account"])))
            put(r["name"], pool.pop(0) if pool else f"Account {len(self.names)}", "account name")
        # merchants: by detailed category, oldest first
        seen = conn.execute("""SELECT merchant_name, pfc_detailed, MIN(date) AS first FROM transactions
                               WHERE merchant_name IS NOT NULL GROUP BY merchant_name ORDER BY pfc_detailed, first, merchant_name""")
        for r in seen:
            if r["merchant_name"] in self.names:   # a lender that is also a merchant: one stand-in
                continue
            pool = [n for n in MERCHANTS.get(r["pfc_detailed"] or "", []) if n not in taken]
            new = pool[0] if pool else next(overflow)
            taken.add(new)
            put(r["merchant_name"], new, "merchant")
        for r in conn.execute("SELECT DISTINCT merchant FROM tracked_bills"):
            if r["merchant"] and r["merchant"] not in self.names:
                put(r["merchant"], next(overflow), "merchant")
        # a statement line that doesn't contain its merchant's name (e.g. an abbreviation): the stand-in, upper case
        for r in conn.execute("SELECT DISTINCT name, merchant_name FROM transactions WHERE merchant_name IS NOT NULL"):
            name, merchant = r["name"], r["merchant_name"]
            if name and merchant and merchant.casefold() not in name.casefold() and name not in self.names:
                put(name, self.names.get(merchant, merchant).upper(), "statement line")
        # employer descriptors (the part of a paycheck line after its date) and payees of person-to-person payments
        for r in conn.execute("SELECT DISTINCT name FROM transactions WHERE merchant_name IS NULL AND pfc_detailed = ?", (WAGES,)):
            m = DATED.match(r["name"] or "")
            put(m["tail"] if m else r["name"], EMPLOYER, "employer descriptor")
        for r in conn.execute("SELECT DISTINCT name FROM transactions WHERE merchant_name IS NULL"):
            m = P2P.match(r["name"] or "")
            if m:
                put(m["who"], PERSON, "person")
        # manual accounts: names by kind; vehicles: a real car each (demo_assets.json); properties: a landmark each
        mpools: dict = {}
        cars, places = iter(VEHICLES), iter(PROPERTIES)
        self.assets: dict[int, dict] = {}   # manual account id -> its demo_assets.json entry (apply_assets)
        for r in conn.execute("SELECT * FROM manual_accounts ORDER BY id"):
            if r["kind"] == "vehicle":
                car = next(cars)   # (a third vehicle needs a third car in demo_assets.json: no invented fallback)
                self.assets[r["id"]] = car
                put(r["name"], f"{car['year']} {car['make']} {car['model']}", "vehicle")
                put(r["vehicle_make"], car["make"], "vehicle make")
                put(r["vehicle_model"], car["model"], "vehicle model")
            elif r["kind"] == "property":
                place = next(places)
                self.assets[r["id"]] = place
                put(r["name"], place["name"], "manual account name")
            elif r["name"] not in self.names:
                pool = mpools.setdefault(r["kind"], list(ACCOUNT_NAMES.get(r["kind"], ["Account"])))
                put(r["name"], pool.pop(0) if pool else f"{r['kind'].title()} {r['id']}", "manual account name")
            if r["address"]:
                fake = self.assets[r["id"]]["address"] if r["id"] in self.assets else PROPERTIES[0]["address"]
                put(r["address"], fake, "address")
                put(r["address"].split(",")[0], fake.split(",")[0], "street")
                put(r["address"].split(",")[0].split("#")[0].strip(), fake.split(",")[0], "street")
            pattern = r["match_pattern"]
            if pattern and pattern not in self.names:   # the start of a renamed name (a lender's): that whole stand-in
                whole = next((o for o in self.names if o.casefold().startswith(pattern.casefold())), None)
                if whole:
                    put(pattern, self.names[whole], "match pattern")
                # otherwise it's statement text, renamed through the names inside it (a card issuer's)
        for r in conn.execute("SELECT DISTINCT vin FROM manual_accounts WHERE vin IS NOT NULL UNION "
                              "SELECT vin FROM vin_decodes"):
            put(r["vin"], VIN.format(len([v for v in self.names.values() if v.startswith('DEMO-VIN')]) + 1), "VIN")
        for r in conn.execute("SELECT DISTINCT ticker_symbol, security_name FROM holdings WHERE security_type <> 'cash'"):
            put(r["ticker_symbol"], TICKER, "ticker")
            put(r["security_name"], SECURITY, "security name")
        # masks: 0001, 0002, ...
        n = 0
        for (mask,) in [*conn.execute("SELECT mask FROM accounts WHERE mask IS NOT NULL ORDER BY rowid"),
                        *conn.execute("SELECT account_mask FROM alpaca_snapshots WHERE account_mask IS NOT NULL ORDER BY date")]:
            if mask not in self.masks:
                n += 1
                self.masks[mask] = f"{n:04d}"
        # ids
        for prefix, sql in (("acc", "SELECT account_id FROM accounts ORDER BY rowid"),
                            ("item", "SELECT item_id FROM items ORDER BY rowid"),
                            ("ins", "SELECT institution_id FROM items UNION SELECT institution_id FROM institutions"),
                            ("sec", "SELECT DISTINCT security_id FROM holdings ORDER BY security_id"),
                            ("txn", "SELECT transaction_id FROM transactions ORDER BY date, transaction_id")):
            k = 0
            for (v,) in conn.execute(sql):
                if v is None or v in self.ids or "alpaca" in v:   # the app's own Alpaca ids (settings.js names one)
                    continue
                k += 1
                self.ids[v] = f"{prefix}_{k:05d}" if prefix == "txn" else f"{prefix}_{k:02d}"
        self.index_images(conn)
        conn.row_factory = None
        keys = sorted(self.names, key=len, reverse=True)
        self._names_re = re.compile("|".join(f"(?<![A-Za-z0-9]){re.escape(k)}(?![A-Za-z0-9])" for k in keys), re.I) if keys else None
        self._folded = {k.casefold(): k for k in keys}
        self._masks_re = re.compile(r"(?<!\d)(" + "|".join(map(re.escape, self.masks)) + r")(?!\d)") if self.masks else None
        ids = sorted(self.ids, key=len, reverse=True)
        self._ids_re = re.compile("(?<![A-Za-z0-9_])(" + "|".join(map(re.escape, ids)) + ")(?![A-Za-z0-9_])") if ids else None
        self._paths = sorted(self.paths, key=len, reverse=True)

    def index_images(self, conn: sqlite3.Connection) -> None:
        """Each cached picture's demo file name, numbered in key order. Run again once apply_assets has put the demo's
        own photos in (demo_parity builds its map from the finished DB, so the build must name them the same way)."""
        self.images = {f: hashlib.sha1(f"demo-image-{i}".encode()).hexdigest()[:16] + Path(f).suffix
                       for i, (f,) in enumerate(conn.execute("SELECT file FROM asset_images WHERE file IS NOT NULL "
                                                             "ORDER BY key"), 1)}
        self._images_re = re.compile("|".join(map(re.escape, self.images))) if self.images else None

    def _name(self, m: re.Match) -> str:
        hit = m.group(0)
        orig = self._folded[hit.casefold()]
        new = self.names[orig]
        if hit == orig:
            return new
        if hit.isupper():
            return new.upper()
        if hit.islower():
            return new.lower()
        return new

    def text(self, s: str) -> str:
        if s in self.ids:
            return self.ids[s]
        for p in self._paths:
            if p and p in s:
                s = s.replace(p, self.paths[p])
        if self._images_re:
            s = self._images_re.sub(lambda m: self.images[m.group(0)], s)
        if self._ids_re:   # an id inside a value (a query string, a "manual:4"-style key)
            s = self._ids_re.sub(lambda m: self.ids[m.group(0)], s)
        if s.startswith(("http://", "https://")):   # the app's own links (a provider's API, Carfax's page): the data's
            return s                                 # URLs are gone by now (sanitize_db), so a name in a URL is the app's
        if self._names_re:
            s = self._names_re.sub(self._name, s)
        if self._masks_re:
            s = self._masks_re.sub(lambda m: self.masks[m.group(1)], s)
        return s

    def key(self, k: str) -> str:
        """An object key: an id or a name (a bank, a category) is renamed; a field name (target, kind) never is."""
        if k in self.ids:
            return self.ids[k]
        return k if SCHEMA_KEY.fullmatch(k) else self.text(k)

    VOCAB_FIELDS = frozenset({"icon", "icons", "colour", "colours"})   # the app's own word lists, never data

    def json(self, x, field: str | None = None):
        if field in self.VOCAB_FIELDS:   # a goal icon "target" is a glyph name, not the merchant Target
            return x
        if isinstance(x, str):
            return self.text(x)
        if isinstance(x, list):
            return [self.json(v) for v in x]
        if isinstance(x, dict):
            return {self.key(k): self.json(v, k) for k, v in x.items()}
        return x

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for new in self.names.values():
            kind = self.provenance.get(new, "?")
            out[kind] = out.get(kind, 0) + 1
        out.update(masks=len(self.masks), ids=len(self.ids), images=len(self.images))
        return out


# --- 3. the App's answers ----------------------------------------------------------------------------------------------

def months_between(first: str, last: str) -> list[str]:
    from hermes_finance import insights
    out, m = [], first
    while m <= last:
        out.append(m)
        m = insights.add_months(m, 1)
    return out


def fkey(path: str, params: dict | None = None) -> str:
    """The fixture key the shim looks a request up by: the path, then its non-empty query values sorted by name."""
    q = sorted((k, str(v)) for k, v in (params or {}).items() if v not in (None, ""))
    return path + ("?" + "&".join(f"{k}={v}" for k, v in q) if q else "")


class Answers:
    """The real App, in process, on one DB."""

    def __init__(self, db_path: Path, today: date):
        from hermes_finance import web

        def no_plaid():
            raise RuntimeError("build_demo: Plaid refused")
        self.app = web.App(db_path, no_plaid)
        self.app.today = lambda: today

    def get(self, path: str, params: dict | None = None, ok=(200,)):
        from urllib.parse import urlencode
        url = path + ("?" + urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")}) if params else "")
        status, ctype, body = self.app.handle("GET", url, HOST, b"")
        if status not in ok:
            raise RuntimeError(f"GET {url}: {status} {body[:300]!r}")
        return status, ctype, body

    def json(self, path: str, params: dict | None = None, ok=(200,)):
        status, _, body = self.get(path, params, ok)
        return status, json.loads(body)



def collect(db_path: Path, today: date) -> dict:
    """Every GET answer the page can ask for in the synthetic snapshot: {"core": {key: answer}, "months": {month: {...}},
    "files": {path: bytes}, "meta": {...}} (not renamed yet)."""
    from hermes_finance import insights
    a = Answers(db_path, today)
    core: dict[str, object] = {}
    errors: dict[str, object] = {}

    def put(path, params=None, ok=(200,)):
        status, payload = a.json(path, params, ok)
        (core if status == 200 else errors)[fkey(path, params)] = payload if status == 200 else {"status": status, "body": payload}
        return payload

    for path in ("/api/status", "/api/accounts", "/api/categories", "/api/recurring", "/api/manual", "/api/investments",
                 "/api/debt", "/api/settings", "/api/settings/page", "/api/goals", "/api/tags", "/api/alpaca", "/api/rules",
                 "/api/category-groups", "/api/chat", "/api/setup", "/api/setup/suggestions", "/api/mcp/tokens", "/api/auth/state", "/api/sync"):
        put(path)
    core["/api/chat"].update(label=BRAIN_LABEL, model=None)   # the Ask state: no model's name (the panel's chip agrees)
    put("/api/manual", {"archived": "1"})
    put("/api/goals", {"archived": "1"})
    for kind in ("loan", "credit_card"):
        put("/api/manual/match", {"kind": kind})
    conn = sqlite3.connect(db_path)
    detail_ids = [r[0] for r in conn.execute("SELECT id FROM manual_accounts WHERE kind IN ('vehicle', 'property') ORDER BY id")]
    first = conn.execute("SELECT MIN(date) FROM transactions").fetchone()[0][:7]
    conn.close()
    for i in detail_ids:
        put(f"/api/manual/{i}/detail")
    for item in core["/api/recurring"]["items"]:
        put("/api/bills/lookup", {"key": item["key"]})
    # the bills calendar: the months it accepts (it answers 400 out of its range)
    cal_months = []
    for m in months_between(insights.month_of(today), insights.add_months(insights.month_of(today), 23)):
        status, payload = a.json("/api/bills-calendar", {"month": m}, ok=(200, 400))
        if status == 200:
            core[fkey("/api/bills-calendar", {"month": m})] = payload
            cal_months.append(m)
    put("/api/bills-calendar")
    # month by month: one month before the first transaction (an empty month) to this one
    this = insights.month_of(today)
    months = {}
    for m in months_between(insights.add_months(first, -1), this):
        tx = a.json("/api/transactions", {"month": m, "limit": "500"})[1]
        if tx["total"] > 500:
            raise RuntimeError(f"{m}: more than 500 transactions; the shim pages through one list of at most 500")
        bundle = {"overview": a.json("/api/overview", {"month": m})[1], "budgets": a.json("/api/budgets", {"month": m})[1],
                  "counts": a.json("/api/counts", {"month": m})[1], "saved_filters": a.json("/api/saved-filters", {"month": m})[1],
                  "tx": tx["rows"]}
        # Sheets opened through all-time search need lookups for historical rows too.
        bundle["bill_lookups"] = {t["transaction_id"]: a.json("/api/bills/lookup", {"transaction_id": t["transaction_id"]})[1]
                                  for t in tx["rows"]}
        months[m] = bundle
    from hermes_finance import api
    tx_all = []
    orders = {}
    for sort in api.SORTS:
        rows, offset = [], 0
        while True:
            page = a.json('/api/transactions', {'limit': 500, 'offset': offset, 'sort': sort, 'facets': '1'})[1]
            rows.extend(page['rows'])
            offset += 500
            if offset >= page['total']:
                break
        orders[sort] = [r['transaction_id'] for r in rows]
        if sort == api.DEFAULT_SORT:
            tx_all = rows
            tx_base = page
    core['/api/saved-filters'] = a.json('/api/saved-filters', {'window': '1'})[1]
    tx_data = {'rows': tx_all, 'orders': orders, 'coverage_month': tx_base['coverage_month'],
               'tags': tx_base['facets']['tags'],
               'groups': {cat['name']: g['name'] for g in core['/api/category-groups']['groups'] for cat in g['categories']}}
    from hermes_finance import prefs
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    meta = {"today": today.isoformat(), "this_month": this, "months": list(months), "calendar_months": cal_months,
            "page_keys": list(prefs.PAGE_KEYS),
            "tx_month": {t["transaction_id"]: m for m, b in months.items() for t in b["tx"]}}
    files = {}
    for r in conn.execute("SELECT institution_id, logo_png FROM institutions WHERE logo_png IS NOT NULL"):
        files[f"api/institutions/{r['institution_id']}/logo"] = r["logo_png"]
    for r in conn.execute("SELECT ticker, png FROM security_logos WHERE png IS NOT NULL"):
        files[f"api/securities/{r['ticker']}/logo"] = r["png"]
    images = Path(db_path).parent / "images"
    for r in conn.execute("SELECT file FROM asset_images WHERE file IS NOT NULL"):
        if (images / r["file"]).is_file():
            files[f"api/images/{r['file']}"] = (images / r["file"]).read_bytes()
    conn.close()
    return {"core": core, "months": months, "meta": meta, "files": files, "errors": errors, "transactions": tx_data, "ask": canned_answers(db_path, today)}


def export_csv(db_path: Path, today: date) -> bytes:
    """The real Settings -> Data -> Export CSV of the same data (GET /api/export.csv)."""
    status, ctype, body = Answers(db_path, today).get("/api/export.csv")
    if hasattr(body, "open"):   # admin.FileBody: streamed from a temp file
        with body.open() as f:
            return f.read()
    return body


def canned_answers(db_path: Path, today: date) -> dict:
    """Ask v2 (t_d31fda2a): preset questions answered by the real read tools at build time, never a model
    (scripts/demo_ask.py)."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import demo_ask
    return demo_ask.build_answers(db_path, today)


# --- 4. dist/ ---------------------------------------------------------------------------------------------------------------

def static_files() -> list[str]:
    from hermes_finance import web
    return sorted(web.STATIC_FILES)


def page_html(db_path: Path, today: date, build_id: str, now: datetime) -> str:
    status, _, body = Answers(db_path, today).get("/")
    page = body.decode()
    plaid = '<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js" async></script>\n'
    if plaid not in page:
        raise RuntimeError("index.html: the Plaid Link script tag moved; update PAGE_CHANGES")
    page = page.replace(plaid, "")
    page = re.sub(r'data-ask-brain="[^"]*"', f'data-ask-brain="{BRAIN_LABEL}"', page, count=1)
    if f'data-ask-brain="{BRAIN_LABEL}"' not in page:
        raise RuntimeError("index.html: no Ask button (data-ask) on the page")
    page = page.replace("<title>Hermes Finances</title>", "<title>Demo · Hermes Finances</title>", 1)
    head = (f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n<meta name="robots" content="noindex">\n'
            f'<script src="/demo/shim.js?v={build_id}" data-build="{build_id}" '
            f'data-now="{now.strftime("%Y-%m-%dT%H:%M:%SZ")}"></script>\n')
    page = page.replace('<meta charset="utf-8">\n', '<meta charset="utf-8">\n' + head, 1)
    page = page.replace('<link rel="stylesheet" href="/static/tap.css">',
                        f'<link rel="stylesheet" href="/static/tap.css">\n<link rel="stylesheet" href="/demo/demo.css?v={build_id}">', 1)
    if "/demo/shim.js" not in page or "/demo/demo.css" not in page:
        raise RuntimeError("index.html: the shim or its stylesheet could not be placed")
    styles = re.findall(r'<link rel="stylesheet" href="([^"]+)">', page)
    scripts = re.findall(r'<script src="([^"]+)" defer></script>', page)
    if "/static/app.js" not in scripts:
        raise RuntimeError("index.html: no deferred /static/app.js to load the demo's Ask v2 before")
    scripts.insert(scripts.index("/static/app.js"), f"/demo/ask.js?v={build_id}")   # an HF_EXT, so before app.js
    page = re.sub(r'<link rel="stylesheet" href="[^"]+">', '', page)
    page = re.sub(r'<script src="[^"]+" defer></script>', '', page)
    loader = ('<script src="/demo/loader.js" defer data-styles="' + html_module.escape(json.dumps(styles), quote=True)
              + '" data-scripts="' + html_module.escape(json.dumps(scripts), quote=True) + '"></script>')
    page = page.replace('</head>', loader + '\n</head>')
    return page


HEADERS = f"""/*
  Content-Security-Policy: {CSP}; frame-ancestors 'none'
  Referrer-Policy: no-referrer
  X-Content-Type-Options: nosniff
  X-Robots-Tag: noindex
/api/institutions/*
  Content-Type: image/png
/api/merchant-logos/*
  Content-Type: image/png
/api/securities/*
  Content-Type: image/png
/demo/data/*
  Cache-Control: no-cache
"""


def logo_slug(name: str) -> str:
    """'Example City Water & Power' -> 'example-city-water-power': a logo's file name in demo/logos."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower().replace("&", " ")).strip("-")


def demo_logo(name) -> bytes | None:
    if not isinstance(name, str) or not name:
        return None
    p = DEMO_LOGOS / f"{logo_slug(name)}.png"
    return p.read_bytes() if p.is_file() else None


# A year of history before the generator's own two weeks of balances (t_d31fda2a): without it the net worth line starts
# 13 days ago at the cash alone and jumps by every car and property on the day their value was set.
HISTORY_DAYS = 364


def history_start(today: date) -> date:
    return today - timedelta(days=HISTORY_DAYS)


def pay_late_bills(conn: sqlite3.Connection, db_path: Path, today: date) -> int:
    """The fixture leaves some bills weeks late on purpose (a gate state). The demo's household pays its bills: each
    late one gets its payment on the day it was expected, a copy of its last charge (same account, amount and
    category), so "bills in the next 7 days" lists what is actually coming. Returns how many payments were added."""
    from hermes_finance import tooldefs
    n = 0
    for _ in range(12):   # a bill more than one cycle late gets one payment per pass
        conn.commit()
        late = [b for b in tooldefs.run_read("finance_bills", {"next": "7d"}, db_path, today)["items"]
                if b["status"] == "late"]
        if not late:
            return n
        with conn:
            for b in late:
                # (a bill without a merchant, e.g. a person paid by Zelle, is known by its statement line)
                row = conn.execute("SELECT * FROM transactions WHERE (merchant_name = ? OR (merchant_name IS NULL AND "
                                   "lower(name) = lower(?))) AND pending = 0 AND amount > 0 "
                                   "ORDER BY date DESC, transaction_id DESC LIMIT 1", (b["merchant"], b["merchant"])).fetchone()
                if row is None or b["next_expected"] > today.isoformat():
                    raise RuntimeError(f"pay_late_bills: no charge to copy for a late bill ({b['key']})")
                tid = f"{row['transaction_id']}-paid-{b['next_expected']}"
                raw = json.loads(row["raw_json"] or "{}")
                raw.update(transaction_id=tid, date=b["next_expected"], authorized_date=b["next_expected"])
                cols = list(row.keys())
                vals = {**dict(zip(cols, row)), "transaction_id": tid, "date": b["next_expected"],
                        "authorized_date": b["next_expected"], "raw_json": json.dumps(raw), "fitid": None}
                conn.execute(f"INSERT INTO transactions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             [vals[c] for c in cols])
                n += 1
    raise RuntimeError("pay_late_bills: bills still late after 12 passes")


def extend_history(conn: sqlite3.Connection, today: date) -> int:
    """Weekly balances back to history_start(): a bank account's balance is walked back through its own transactions
    (cash: a charge was money out, so the balance before it was higher), never below zero; a card, a loan and an
    investment account keep their first balance (the fixture's card charges and payments don't net out). A manual
    account's first value also stands at history_start(). Returns how many rows were added."""
    start, n = history_start(today), 0
    with conn:
        for acc, kind in conn.execute("SELECT account_id, type FROM accounts WHERE type IN "
                                      "('depository', 'investment', 'credit', 'loan') ORDER BY account_id").fetchall():
            first = conn.execute("SELECT date, current, available, iso_currency FROM balance_snapshots WHERE account_id = ? "
                                 "AND current IS NOT NULL ORDER BY date LIMIT 1", (acc,)).fetchone()
            if first is None:
                continue
            d0 = date.fromisoformat(first[0])
            flows = dict(conn.execute("SELECT date, SUM(amount) FROM transactions WHERE account_id = ? AND pending = 0 "
                                      "AND removed = 0 AND date <= ? GROUP BY date", (acc, first[0])).fetchall())
            bal, day = first[1], d0
            while True:
                if kind == "depository":
                    bal = max(0.0, bal + flows.get(day.isoformat(), 0.0))   # the end of the day before `day`
                day -= timedelta(days=1)
                if day < start:
                    break
                if (d0 - day).days % 7 == 0:
                    avail = None if first[2] is None else round(first[2] + (bal - first[1]), 2)
                    n += conn.execute("INSERT OR IGNORE INTO balance_snapshots (account_id, date, current, available, "
                                      "iso_currency) VALUES (?, ?, ?, ?, ?)",
                                      (acc, day.isoformat(), round(bal, 2), avail, first[3])).rowcount
        for mid, d, bal in conn.execute("SELECT manual_account_id, MIN(date), balance FROM manual_balance_history "
                                        "GROUP BY manual_account_id").fetchall():
            if d > start.isoformat():
                n += conn.execute("INSERT OR IGNORE INTO manual_balance_history (manual_account_id, date, balance) "
                                  "VALUES (?, ?, ?)", (mid, start.isoformat(), bal)).rowcount
    return n


PHOTOS_SRC = ROOT / "demo" / "photos"
ESTIMATE_DRIFT = {"vehicle": 0.009, "property": -0.0035}   # a month back: a car was worth ~0.9% more, a building ~0.35% less
ESTIMATE_WOBBLE = (0.0, 0.0, -0.003, 0.002, -0.004, 0.003, 0.0, -0.002, 0.004, -0.001, 0.002, -0.003)


def estimate_history(kind: str, value: float, today: date) -> list[tuple[date, float]]:
    """The demo's monthly Carfax / Redfin checks, oldest first, ending today on the frozen value: a car loses a little
    every month and a building gains a little (ESTIMATE_DRIFT), with a small fixed wobble so the line isn't straight (none a month back:
    the page's change since the last check is the drift alone, never $0). Same input, same points (a build is reproducible)."""
    step = 25 if kind == "vehicle" else 1000
    out = []
    for k in range(HISTORY_DAYS // 30, -1, -1):
        day = today - timedelta(days=30 * k)
        x = value if k == 0 else value * (1 + ESTIMATE_DRIFT[kind] * k + ESTIMATE_WOBBLE[k % len(ESTIMATE_WOBBLE)])
        out.append((max(day, history_start(today)), float(round(x / step) * step) if k else float(value)))
    return out


def apply_assets(conn: sqlite3.Connection, rename: RenameMap, today: date, db_path: Path) -> int:
    """The demo's cars and properties: real cars without a VIN and Los Angeles landmarks (demo_assets.json), each worth
    the one real valuation frozen there. The generator's own estimates, VIN decodes, provider notes and pictures for
    them described other things, so they go. In their place, so the car / property pages show what the app does:
    - the estimate: a car's Carfax check (trade-in = the frozen value; private sale / dealer retail above it; its
      odometer and the model's specs from demo_assets.json), a landmark's Redfin Estimate (= the frozen value), checked
      monthly over the history (estimate_history) and followed (value_source 'estimate'), so the value, net worth and
      the value chart all end on the frozen value;
    - the picture: the real Wikimedia Commons photo demo_photos_pull.py froze (a cut-out car, a landmark photo), with
      its credit, licence and source page.
    Returns how many accounts got a frozen value."""
    from hermes_finance import photos
    folder = photos.images_dir(db_path)
    factory, conn.row_factory = conn.row_factory, sqlite3.Row   # manual / photos / valuation read rows by name
    try:
        return _apply_assets(conn, rename, today, folder)
    finally:
        conn.row_factory = factory


def _apply_assets(conn: sqlite3.Connection, rename: RenameMap, today: date, folder: Path) -> int:
    from hermes_finance import manual, photos, valuation
    n = 0
    with conn:
        for acc, a in rename.assets.items():
            old = photos.key_for(manual.get(conn, acc, include_archived=True))
            if old:
                conn.execute("DELETE FROM asset_images WHERE key = ?", (old,))
            conn.execute("DELETE FROM asset_valuations WHERE account_id = ?", (acc,))
            conn.execute("DELETE FROM api_calls WHERE account_id = ?", (acc,))
            car = "make" in a
            if car:
                conn.execute("UPDATE manual_accounts SET name = ?, vehicle_year = ?, vehicle_make = ?, vehicle_model = ?, "
                             "vehicle_trim = ?, vin = NULL, vin_decoded = NULL, mileage = ?, mileage_as_of = ?, "
                             "zip_code = ?, vehicle_condition = 'good' WHERE id = ?",
                             (f"{a['year']} {a['make']} {a['model']}", a["year"], a["make"], a["model"], a["trim"],
                              a.get("mileage"), (today - timedelta(days=21)).isoformat() if a.get("mileage") else None,
                              a.get("zip"), acc))
            else:
                conn.execute("UPDATE manual_accounts SET name = ?, address = ? WHERE id = ?", (a["name"], a["address"], acc))
            row = manual.get(conn, acc, include_archived=True)
            v = a.get("valuation")
            if v:
                kind = row["kind"]
                points = estimate_history(kind, v["value"], today)
                conn.execute("UPDATE manual_accounts SET balance = ?, balance_as_of = ?, value_source = 'estimate' "
                             "WHERE id = ?", (v["value"], today.isoformat(), acc))
                conn.execute("DELETE FROM manual_balance_history WHERE manual_account_id = ?", (acc,))
                for i, (d, x) in enumerate(points):
                    miles = round(a["mileage"] - 950 * (len(points) - 1 - i)) if car and a.get("mileage") else None
                    if car:
                        details = {"trade_in": x, "private_sale": float(round(x * 1.09 / 25) * 25),
                                   "dealer_retail": float(round(x * 1.19 / 25) * 25), "records": 9 + i // 3,
                                   "carfax_odometer": miles, "trim": a["trim"], "condition": "good", "zip": a.get("zip"),
                                   "display_status": "Displayable", "specs": a.get("specs") or {}}
                    else:
                        details = {"name": a["address"].split(",")[0], "via": "https", "photo_url": None}
                    at = f"{d.isoformat()}T19:00:00Z"   # noon on the US west coast: the same local day
                    conn.execute("INSERT INTO asset_valuations (account_id, value, source, miles, subject, fetched_at, "
                                 "raw_json, details_json) VALUES (?, ?, ?, ?, ?, ?, '{}', ?)",
                                 (acc, x, "carfax" if car else "redfin", miles, valuation.subject(row), at,
                                  json.dumps(details)))
                    conn.execute("INSERT INTO api_calls (provider, account_id, trigger, at, day, status, outcome) "
                                 "VALUES (?, ?, 'daily', ?, ?, 200, 'ok')", ("carfax" if car else "redfin", acc, at,
                                                                             d.isoformat()))
                    conn.execute("INSERT OR REPLACE INTO manual_balance_history (manual_account_id, date, balance) "
                                 "VALUES (?, ?, ?)", (acc, d.isoformat(), x))
                n += 1
            p = a.get("photo")
            key = photos.key_for(row)
            if p and key:
                name = photos.file_name(key, Path(p["file"]).suffix.lstrip("."))
                photos._write(folder, name, (PHOTOS_SRC / p["file"]).read_bytes())
                photos._save(conn, key, row["kind"], "ready", source="commons", file=name, cutout=p["cutout"],
                             width=p["width"], height=p["height"], credit=p["credit"], credit_url=p["credit_url"],
                             artist=p["artist"], license=p["license"], note=p["title"])
        conn.execute("DELETE FROM vin_decodes")
    return n


# Each demo goal's look, from the goal picker's own set (goals.ICONS / COLOURS). The generator makes its goals the way an API
# caller would, without a look, so they get the app's legacy defaults (pencil / split / handcoins), which the picker
# doesn't offer: its edit sheet would show no icon selected. A goal not named here gets the picker's default.
GOAL_LOOKS = {"Rainy day": ("shield-check", "#5fc6e8"), "New laptop": ("briefcase", "#8ea8ff"),
              "Concert tickets": ("heart", "#ff8fb1"), "Trip to Lisbon": ("plane", "#c792ea"),
              "Monthly savings": ("sprout", "#4fd1a5")}
PAYOFF_LOOK = ("car", "#f5b454")


def apply_goal_looks(conn: sqlite3.Connection) -> int:
    """Every demo goal gets an icon and colour from the picker. Returns how many goals changed."""
    from hermes_finance import goals
    n = 0
    with conn:
        for gid, kind, name, icon, colour in conn.execute("SELECT id, kind, name, icon, colour FROM goals").fetchall():
            look = PAYOFF_LOOK if kind == "payoff" else GOAL_LOOKS.get(name, ("piggy-bank", goals.COLOURS[0]))
            if icon not in goals.ICONS or name in GOAL_LOOKS or kind == "payoff":
                conn.execute("UPDATE goals SET icon = ?, colour = ? WHERE id = ?", (*look, gid))
                n += (icon, colour) != look
        assert all(r[0] in goals.ICONS for r in conn.execute("SELECT icon FROM goals"))
    return n


def apply_bank_logos(conn: sqlite3.Connection, rename: RenameMap) -> int:
    """Each bank's logo is the invented one for the name the demo shows (Alpaca keeps its own); a bank without one keeps
    its colour and initials. Returns how many banks got one."""
    n = 0
    with conn:
        for r in conn.execute("SELECT institution_id, name FROM institutions").fetchall():
            png = demo_logo(rename.names.get(r[1], r[1]))
            if png:
                conn.execute("UPDATE institutions SET logo_png = ?, logo_source = 'site' WHERE institution_id = ?",
                             (png, r[0]))
                n += 1
    return n


def with_merchant_logos(x, used: set):
    """A renamed payload with each merchant row's logo_url pointed at the demo's own logo for that merchant (same
    origin; the page's safeImg takes only /api/merchant-logos/<slug>.png). Rows without one keep their initials."""
    if isinstance(x, list):
        return [with_merchant_logos(v, used) for v in x]
    if not isinstance(x, dict):
        return x
    y = {k: with_merchant_logos(v, used) for k, v in x.items()}
    if "logo_url" in y and not y["logo_url"]:
        for k in ("merchant_name", "merchant", "seed", "name"):
            slug = logo_slug(y[k]) if isinstance(y.get(k), str) else ""
            if slug and (DEMO_LOGOS / f"{slug}.png").is_file():
                y["logo_url"] = f"{MERCHANT_LOGOS}{slug}.png"
                used.add(slug)
                break
    return y


def write_dist(out: Path, base: dict, rename: RenameMap, html: str, csv_bytes: bytes, build_id: str, now: datetime) -> list[Path]:
    if out.exists():
        shutil.rmtree(out)
    (out / "static").mkdir(parents=True)
    written = []

    def write(rel: str, data: bytes | str):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else data)
        written.append(p)

    for name in static_files():
        write(f"static/{name}", (STATIC / name).read_bytes())
    from hermes_finance import web
    for root, name in web.ROOT_FILES.items():
        write(root.lstrip("/"), (STATIC / name).read_bytes())
    write("index.html", html)
    write("_headers", HEADERS)
    for name in DEMO_FILES:
        write(f"demo/{name}", (DEMO_SRC / name).read_bytes())
    used: set = set()

    def dump(x) -> str:
        return json.dumps(with_merchant_logos(rename.json(x), used), separators=(",", ":"), ensure_ascii=False,
                          sort_keys=False)
    meta = {**base["meta"], "build": build_id, "now": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "money_scale": MONEY_SCALE}
    write("demo/data/core.json", dump({"meta": meta, "core": base["core"], "errors": base["errors"]}))
    for m, bundle in base["months"].items():
        write(f"demo/data/m/{m}.json", dump(bundle))
    write('demo/data/transactions.json', dump(base['transactions']))
    write('demo/data/ask.json', dump(base['ask']))
    for rel, data in base["files"].items():
        write(rename.text(rel), data)
    write("api/export.csv", rename_csv(csv_bytes, rename))
    for slug in sorted(used):
        write(f"{MERCHANT_LOGOS.strip('/')}/{slug}.png", (DEMO_LOGOS / f"{slug}.png").read_bytes())
    return written


def rename_csv(data: bytes, rename: RenameMap) -> bytes:
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    for row in rows:
        w.writerow([rename.text(c) for c in row])
    return out.getvalue().encode()


# --- main -------------------------------------------------------------------------------------------------------------------

def build(out: Path, today: date, keep: Path | None = None, seed: int = 0, salt: int = 0) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="hf-demo-build-"))
    try:
        tmp_paths = safe_env(tmp)
        no_network()
        sys.path.insert(0, str(ROOT / "src"))
        sys.path.insert(0, str(ROOT / "tests"))
        import demo_data  # noqa: F401  (patched with the rest)
        import gate_dbs  # noqa: F401
        now = datetime(today.year, today.month, today.day, 12, 0, 0, tzinfo=timezone.utc)
        freeze_clock(now)
        offline_app()
        (tmp / "data").mkdir()
        db_path = tmp / "data" / "finance.db"
        t0 = time.monotonic()
        generate(db_path, today, seed, salt)
        from hermes_finance import db as hdb
        conn = hdb.connect(db_path)
        paid = pay_late_bills(conn, db_path, today)
        history = extend_history(conn, today)
        rename = RenameMap(conn, tmp_paths)   # read from the generated DB, before anything else is changed
        scaled = sanitize_db(conn, today, now, tmp_paths)
        bank_logos = apply_bank_logos(conn, rename)
        valued = apply_assets(conn, rename, today, db_path)
        rename.index_images(conn)   # the photos apply_assets put in get their demo names too
        goal_looks = apply_goal_looks(conn)
        conn.close()
        log = {"generated_s": round(time.monotonic() - t0, 2), "scaled_rows": scaled, "renamed": rename.summary(),
               "bank_logos": bank_logos, "frozen_valuations": valued,
               "goal_looks": goal_looks, "late_bills_paid": paid, "history_rows": history}
        base = collect(db_path, today)
        base["meta"]["seed"] = seed
        if salt:
            base["meta"]["salt"] = salt
        html = page_html(db_path, today, "", now)
        csv_bytes = export_csv(db_path, today)
        version = hashlib.sha256()
        for part in ('meta', 'core', 'months', 'transactions', 'ask'):
            version.update(json.dumps(rename.json(base[part]), sort_keys=True).encode())
        for file in [*(STATIC / n for n in static_files()), *(DEMO_SRC / n for n in DEMO_FILES)]:
            version.update(file.read_bytes())
        build_id = version.hexdigest()[:12]
        html = page_html(db_path, today, build_id, now)
        written = write_dist(out, base, rename, html, csv_bytes, build_id, now)
        if keep:
            keep.mkdir(parents=True, exist_ok=True)
            shutil.copy2(db_path, keep / "finance.db")
            (keep / "rename.json").write_text(json.dumps({"names": rename.names, "ids": rename.ids, "masks": rename.masks,
                                                          "images": rename.images, "paths": rename.paths}, indent=1))
        log.update(build=build_id, files=len(written), bytes=sum(p.stat().st_size for p in written),
                   months=len(base["months"]))
        return log
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_variant(out: Path, today: date, keep: Path | None = None, seed: int = 0) -> dict | None:
    """build() with the first salt whose figures show no generator fixed amount (SALTS); None if every salt does."""
    for salt in SALTS:
        log = build(out, today, keep, seed, salt)
        check = subprocess.run([sys.executable, str(ROOT / "scripts" / "demo_canary.py"), str(out), "--fixed-amounts"],
                               env={k: v for k, v in os.environ.items() if not CREDENTIAL_RE.search(k)}, capture_output=True, text=True)
        if check.returncode == 0:
            return {**log, "salt": salt}
        print(f"build_demo: seed {seed} salt {salt}: {check.stdout.strip() or check.stderr.strip()[-300:]}; building it again")
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "dist" / "demo", help="output directory (replaced), default dist/demo/")
    ap.add_argument("--today", type=date.fromisoformat, default=None,
                    help="the demo's today (YYYY-MM-DD); default: the local date. The same day builds the same bytes")
    ap.add_argument("--seed", type=int, choices=list(SEEDS), default=0,
                    help="the weekly variant (0-6, picked by the visitor's weekday, Monday 0): re-seeds the generator's "
                         "discretionary spending; 0 (the default) is the usual demo. The same seed and day build the same bytes")
    ap.add_argument("--no-canary", action="store_true", help="skip scripts/demo_canary.py (never for a publishable build)")
    ap.add_argument("--keep-db", type=Path, default=None,
                    help="also copy the sanitized synthetic DB and the rename map here (a private debugging aid: they "
                         "hold the generator's original names, so never inside --out)")
    ap.add_argument("--canary-arg", action="append", default=[], help="passed on to demo_canary.py (e.g. --private=FILE)")
    ap.add_argument('--synthetic-only', action='store_true', help='scan synthetic artifacts without owner files; NOT publication approval')
    ap.add_argument('--serve', action='store_true', help='serve the build on loopback with stdlib http.server')
    ap.add_argument('--port', type=int, default=8000)
    args = ap.parse_args(argv)
    if args.synthetic_only:
        # Public builds must never discover/read an owner file or an ambient private-token list.
        os.environ['HERMES_FINANCE_PRIVATE_FILE'] = '/nonexistent'
        os.environ.pop('HERMES_FINANCE_TEST_PRIVATE', None)
    today = args.today or date.today()   # (before safe_env sets TZ=UTC: the builder's own local day)
    out = args.out.resolve()
    if args.keep_db and (args.keep_db.resolve() == out or out in args.keep_db.resolve().parents):
        print("--keep-db must be outside --out")
        return 2
    if out == ROOT or out == ROOT / "src" or ROOT / "src" in out.parents:
        print(f"refusing to write the demo into {out}")
        return 2
    log = build_variant(out, today, args.keep_db, args.seed)
    if log is None:
        print(f"build_demo: FAILED: no salt in {SALTS.start}-{SALTS.stop - 1} builds seed {args.seed} on {today} without a generator amount")
        return 1
    salt = log["salt"]
    shown = os.path.relpath(out)   # (as the builder would type it: no home directory in the log)
    print(f"build_demo: {KIND} DB for {today}, seed {args.seed}{f' salt {salt}' if salt else ''} (pinned clock {today}T12:00:00Z), money x{MONEY_SCALE}, "
          f"generated in {log['generated_s']} s")
    print(f"build_demo: renamed {json.dumps(log['renamed'], sort_keys=True)}")
    print(f"build_demo: {log['months']} months, {log['files']} files, {log['bytes']:,} bytes -> {shown}"
          f" (build {log['build']})")
    sys.stdout.flush()
    if args.no_canary:
        print("build_demo: canary SKIPPED (--no-canary): this dist/ is not cleared for publication")
        return 0
    env = {k: v for k, v in os.environ.items() if not CREDENTIAL_RE.search(k)}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "demo_canary.py"), shown, *(['--synthetic-only'] if args.synthetic_only else []), *args.canary_arg], env=env)
    if r.returncode != 0:
        print(f"build_demo: FAILED: the canary found something (exit {r.returncode}); do not publish {shown}")
        return 1
    if args.serve:
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        from functools import partial
        server = ThreadingHTTPServer(('127.0.0.1', args.port), partial(SimpleHTTPRequestHandler, directory=str(out)))
        print(f'http://127.0.0.1:{server.server_port}/', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

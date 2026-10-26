"""`finance doctor`: every integration the app can use, and what each one is missing, up front.

Offline and read-only: no network call (not even to the local Chrome), no database write, no file or directory
created. It works with no data directory and no database at all. Keys are resolved by the app's own code
(config.key_values: the env file with the secrets file on top; prefs for the settings kept in the database; access /
auth for the login; bridge.executable for Hermes; alpaca.load_keys for Alpaca's key file), never re-implemented here.
It never prints a key's value, not even in part: only key names, paths and fixed sentences.

Where the settings live in the database (the AI mode, the access mode, the valuation switches, MCP on/off), it reads
them read-only: the database itself when its schema is current; an in-memory copy, upgraded in memory, when it's older
(what the next real start will do to it); and, with no database yet, an in-memory database with the schema applied,
so a fresh install gets exactly the defaults the app would give it.

Statuses: ok, off (switched off on purpose: by a setting, a default, or a kill switch), missing (something to set up),
error (set up but unusable). Exit 0 when no integration is missing or in error, 1 otherwise. The JSON shape (SCHEMA)
and the text lines are stable (scripts may parse them); docs/configuration.md documents both.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import backup, config, db

SCHEMA = 1
README = "README.md"
# (name, README anchor), in the order they're reported. The anchors are the headings of README.md's Configuration
# section (tests/test_doctor.py checks every one exists).
INTEGRATIONS = (
    ("database", "integration-data-directory-and-database"),
    ("plaid", "integration-plaid-banks"),
    ("ai", "integration-ai-provider-ask-and-classification"),
    ("login", "integration-access-and-login"),
    ("alpaca", "integration-alpaca-investments"),
    ("mcp", "integration-mcp-server"),
    ("carfax", "integration-carfax-car-values"),
    ("redfin", "integration-redfin-home-values"),
    ("photos", "integration-photos-car-and-home-pictures"),
)
ANCHORS = dict(INTEGRATIONS)
OPTIONAL = {"alpaca", "mcp", "carfax", "redfin", "photos"}   # the app runs without them (still listed, still counted)
BAD = ("missing", "error")


@dataclass
class Check:
    name: str
    status: str                         # ok | off | missing | error
    detail: str
    keys: list[str] = field(default_factory=list)
    missing_keys: list[str] = field(default_factory=list)
    optional: bool = False
    fix: str = ""

    def __post_init__(self):
        self.optional = self.name in OPTIONAL
        self.fix = f"{README}#{ANCHORS[self.name]}"


# --- the database, read-only -----------------------------------------------------------------------------------------

def _memory(source: sqlite3.Connection | None = None) -> sqlite3.Connection:
    """An in-memory database with the current schema: empty (a fresh install's defaults), or a copy of `source`
    upgraded in memory."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    if source is not None:
        source.backup(conn)
    db.init_schema(conn)
    return conn


def database() -> tuple[Check, sqlite3.Connection, dict[str, Any]]:
    """(the database's check, a connection to read the settings from, facts for the JSON)."""
    data, path = config.data_dir(), config.db_path()
    facts: dict[str, Any] = {"data_dir": str(data), "db_path": str(path), "schema_version": None,
                             "code_schema_version": db.SCHEMA_VERSION}
    keys = ["FINANCE_DATA_DIR"]
    if not path.exists():
        where = "no data directory" if not data.exists() else "no database"
        return (Check("database", "missing", f"{where} yet at {path}; `finance status` (or `finance serve`) creates it",
                      keys), _memory(), facts)
    try:
        conn = backup.open_readonly(path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    except sqlite3.Error as e:
        return Check("database", "error", f"{path} can't be read as a database ({type(e).__name__})", keys), _memory(), facts
    facts["schema_version"] = version
    writable = os.access(path, os.W_OK) and os.access(data, os.W_OK | os.X_OK)
    if version > db.SCHEMA_VERSION:
        conn.close()
        return (Check("database", "error", f"{path} has schema v{version}, newer than this code's v{db.SCHEMA_VERSION}: "
                      "upgrade the app (downgrades aren't supported)", keys), _memory(), facts)
    if not writable:
        return Check("database", "error", f"{path} or its directory isn't writable by this user", keys), conn, facts
    if version < db.SCHEMA_VERSION:
        try:
            mem = _memory(conn)
        except Exception as e:   # what the next real start would hit: say so, read the settings from the defaults
            conn.close()
            return (Check("database", "error", f"{path} (schema v{version}) can't be upgraded to v{db.SCHEMA_VERSION}: "
                          f"{type(e).__name__}", keys), _memory(), facts)
        conn.close()
        return (Check("database", "ok", f"{path}, schema v{version}: upgraded to v{db.SCHEMA_VERSION} the next time the "
                      "app opens it (back it up first: docs/backup-restore.md)", keys), mem, facts)
    return Check("database", "ok", f"{path}, schema v{version} (current)", keys), conn, facts


# --- the integrations ------------------------------------------------------------------------------------------------

def plaid(values: dict[str, str]) -> Check:
    from . import secretstore
    keys = list(config.PLAID_KEYS)
    missing = [k for k in config.PLAID_KEYS[:2] if not values.get(k)]
    env = values.get("PLAID_ENV")
    if env and env not in secretstore.PLAID_ENVS:
        missing.append("PLAID_ENV")
    if missing:
        what = " and ".join(missing) + (" (PLAID_ENV must be production or sandbox)" if "PLAID_ENV" in missing else "")
        if config.hermes_credentials():   # no file is looked at: the keys are Hermes's
            return Check("plaid", "missing", f"{what} not set in Hermes (hermes config set NAME VALUE)", keys, missing)
        return Check("plaid", "missing", f"{what} not set (env file {config.env_file_path()}, secrets file "
                     f"{config.secrets_file_path()})", keys, missing)
    if os.environ.get("HERMES_PLAID") == "off":
        return Check("plaid", "off", "keys set; Plaid calls are switched off (HERMES_PLAID=off)", keys)
    return Check("plaid", "ok", f"keys set ({env or 'production'})", keys)


def ai(conn: sqlite3.Connection, values: dict[str, str]) -> Check:
    from . import bridge, chat, secretstore
    b = chat.brain(conn)
    keys = ["ai.mode", "ai.base_url", "ai.model", secretstore.AI_KEY, bridge.BIN_ENV, "HERMES_HOME"]
    if b["mode"] == "off":
        return Check("ai", "off", "AI is off (Settings > AI provider): no Ask panel, no AI classification", keys)
    if b["mode"] == "hermes":
        if bridge.executable() is None:
            return Check("ai", "missing", f"ai.mode is hermes but no Hermes executable was found ({bridge.BIN_ENV}, else "
                         "$HERMES_HOME/hermes-agent/venv/bin/hermes)", keys, [bridge.BIN_ENV])
        return Check("ai", "ok", "via Hermes (the hermes-finance plugin's completion bridge; the plugin isn't checked)", keys)
    missing = [k for k, v in (("ai.base_url", b.get("url")), ("ai.model", b.get("model"))) if not v]
    if not values.get(secretstore.AI_KEY) and b.get("preset") != "local":
        missing.append(secretstore.AI_KEY)
    if missing:
        return Check("ai", "missing", f"ai.mode is api but {', '.join(missing)} {'is' if len(missing) == 1 else 'are'} "
                     "not set", keys, missing)
    return Check("ai", "ok", f"via an OpenAI-compatible API ({b.get('preset')})", keys)


def login(conn: sqlite3.Connection) -> Check:
    from . import access, auth
    keys = ["access.mode", "access.allowed_identities", "FINANCE_PUBLIC_URLS", "FINANCE_ALLOWED_USERS"]
    try:
        acc = access.load(conn, config.env_list("FINANCE_PUBLIC_URLS"), config.env_list("FINANCE_ALLOWED_USERS"))
    except ValueError:
        return Check("login", "error", "FINANCE_PUBLIC_URLS (or a saved public URL) isn't a valid https:// URL", keys,
                     ["FINANCE_PUBLIC_URLS"])
    if acc.mode == "proxy" and not acc.identities:
        return Check("login", "missing", "access is 'Behind your proxy login' with no allowed identity "
                     "(`finance access allow <login>` or FINANCE_ALLOWED_USERS)", keys, ["access.allowed_identities"])
    if acc.mode == "builtin" and not auth.configured(conn):
        return Check("login", "missing", "access is 'Built-in login' but its set-up isn't finished (Settings > Access & "
                     "security)", keys, ["access.mode"])
    what = {"local": "this computer only (no login needed)", "proxy": "behind your proxy login",
            "builtin": "built-in login set up"}[acc.mode]
    return Check("login", "ok", what, keys)


def alpaca_check() -> Check:
    from . import alpaca
    keys = ["APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "APCA_ENV", "HERMES_ALPACA_KEY_FILE"]
    if (os.environ.get(alpaca.KILL_ENV) or "").strip().lower() == "off":   # first: the key file isn't touched at all
        return Check("alpaca", "off", f"disabled ({alpaca.KILL_ENV}=off)", keys)
    if config.hermes_credentials():   # from Hermes; the key file isn't looked at
        try:
            k = alpaca.load_keys()
        except alpaca.Refused:
            return Check("alpaca", "error", "the key in Hermes is refused: it needs a live read-only key "
                         "(APCA_API_KEY_ID, APCA_API_SECRET_KEY, APCA_ENV=live)", keys)
        if k is None:
            return Check("alpaca", "missing", "not set in Hermes (hermes config set NAME VALUE)", keys, keys[:3])
        return Check("alpaca", "ok", "key in Hermes", keys)
    path = config.alpaca_key_path()
    try:
        k = alpaca.load_keys(path)
    except alpaca.Refused:   # its message may quote the file's values: a fixed sentence instead
        return Check("alpaca", "error", f"the key file {path} is refused: it needs a live read-only key (APCA_API_KEY_ID, "
                     "APCA_API_SECRET_KEY, APCA_ENV=live)", keys)
    except OSError as e:
        return Check("alpaca", "error", f"the key file {path} can't be read ({type(e).__name__})", keys)
    if k is None:
        return Check("alpaca", "missing", f"no key file at {path}", keys, keys[:3])
    return Check("alpaca", "ok", f"key file {path}", keys)


def mcp(conn: sqlite3.Connection) -> Check:
    from . import prefs
    keys = ["mcp.enabled"]
    if not prefs.get(conn, "mcp.enabled"):
        return Check("mcp", "off", "MCP is switched off (Settings > Connect other agents)", keys)
    if importlib.util.find_spec("mcp") is None:
        return Check("mcp", "missing", "the optional mcp package isn't installed (`uv sync --extra mcp`)", keys)
    return Check("mcp", "ok", "the mcp package is installed", keys)


def free_sources(conn: sqlite3.Connection) -> list[Check]:
    from .cdp_client import ENV as CDP_ENV, Chrome
    on = config.free_sources(conn=conn)
    flags = config.FREE_SOURCE_NAMES
    out = []
    for name, keys, what in (("carfax", [flags["carfax"], "valuations.carfax", CDP_ENV], "car values"),
                             ("redfin", [flags["redfin"], "valuations.redfin"], "home values"),
                             ("photos", [flags["photos"], "valuations.photos", "FINANCE_CONTACT"], "car and home pictures")):
        if not on[name]:
            out.append(Check(name, "off", f"{what} are switched off (Settings > Valuations, or {flags[name]}=0)", keys))
        elif name == "carfax" and Chrome().endpoint() is None:
            out.append(Check(name, "missing", f"Carfax is on but the local Chrome is switched off ({CDP_ENV}=off)", keys,
                             [CDP_ENV]))
        elif name == "carfax":
            host, port = Chrome().endpoint()
            out.append(Check(name, "ok", f"on; needs a Chrome with remote debugging at {host}:{port} (not checked: "
                             "doctor makes no connection)", keys))
        else:
            out.append(Check(name, "ok", "on (keyless)", keys))
    return out


def run() -> dict[str, Any]:
    """Every check, in INTEGRATIONS order, as the JSON shape (SCHEMA)."""
    values = config.key_values()
    dbcheck, conn, facts = database()
    try:
        checks = [dbcheck, plaid(values), ai(conn, values), login(conn), alpaca_check(), mcp(conn), *free_sources(conn)]
    finally:
        conn.close()
    assert [c.name for c in checks] == [n for n, _ in INTEGRATIONS]
    missing = [c.name for c in checks if c.status in BAD]
    files = ({"credentials": "Hermes"} if config.hermes_credentials() else
             {"env_file": str(config.env_file_path()), "secrets_file": str(config.secrets_file_path())})
    return {"schema": SCHEMA, "ok": not missing, "missing": missing,
            "integrations": [asdict(c) for c in checks], "files": {**files, **facts}}


def lines(report: dict[str, Any]) -> list[str]:
    """The text output: one line per integration, in order. `ok <name>`, else `<status> <name>: <detail> -> <fix>`."""
    out = []
    for c in report["integrations"]:
        out.append(f"ok {c['name']}" if c["status"] == "ok" else f"{c['status']} {c['name']}: {c['detail']} -> {c['fix']}")
    return out

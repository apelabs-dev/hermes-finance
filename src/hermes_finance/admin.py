"""The Settings page's server side (TASK-18): the routes web.App hands over, all under /api.

  GET  /api/settings/page            everything the Settings page shows (never a key, a hash or a whole identity)
  POST /api/prefs                    save settings (prefs.PAGE_KEYS), all or nothing
  POST /api/secrets/plaid            Replace keys: {client_id, secret, env} -> the secrets file
  POST /api/secrets/ai               {key} -> the secrets file (write-only)
  POST /api/access                   {mode}: choose an access mode
  POST /api/access/proxy             {proxy_header?, public_urls?}
  POST /api/access/identities        {add: login} | {remove: id}
  GET  /api/auth/state               signed in? which mode? may this browser set the login up?
  POST /api/auth/login | code | logout | logout-all | password | recovery
  POST /api/auth/enroll/start | qr | verify | finish      the built-in login's set-up (password, QR + code, codes)
  GET  /api/mcp/tokens               POST /api/mcp/tokens {name}   POST /api/mcp/tokens/revoke {id}
  GET  /api/export.csv               every transaction as CSV (a download)
  GET  /api/backup                   a consistent copy of the database with every credential taken out (a download)
  GET  /api/setup                    first-run: is it due, what's found
  POST /api/setup/plaid | done       GET /api/setup/suggestions
  POST /api/import/preview | commit  a CSV / OFX / QFX file into a manual account (importer.py)

Errors are api.ApiError (the page shows them next to their field).
"""

from __future__ import annotations

import base64
import csv
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from . import access, api, auth, backup, config, db, insights, mcp_state, prefs, redact, secretstore, syncnow, usage

JSON = "application/json"
PREFIXES = ("/api/settings/page", "/api/prefs", "/api/secrets/", "/api/access", "/api/auth/", "/api/mcp/tokens",
            "/api/mcp/pending", "/api/export.csv", "/api/backup", "/api/setup", "/api/import/")
# the built-in login's endpoints that work without a session (the set-up ones only while no login exists, from here)
OPEN = {"/api/auth/state", "/api/auth/login", "/api/auth/code", "/api/auth/logout"}
EXPORT_COLUMNS = ["date", "name", "merchant_name", "amount", "spend", "iso_currency", "category", "category_source",
                  "category_note", "ai_note", "pending", "account_name", "account_mask", "institution_name", "source",
                  "pfc_primary", "pfc_detailed", "transaction_id"]
TEXT_COLUMNS = {"name", "merchant_name", "category", "category_note", "ai_note", "account_name", "institution_name"}
SUGGEST_DAYS = 60


@dataclass
class Request:
    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes
    ip: str | None
    acc: access.Access
    plain_local: bool = False           # a localhost Host that came through no proxy
    session: dict[str, Any] | None = None
    login_ready: bool | None = None
    _data: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def secure(self) -> bool:
        """The session cookie is Secure unless this is plain http on localhost."""
        return not self.plain_local or self.headers.get("x-forwarded-proto", "").lower() == "https"

    def params(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(self.query, keep_blank_values=True).items()}

    def data(self) -> dict[str, Any]:
        if self._data is None:
            if not self.headers.get("content-type", "").startswith(JSON):
                raise api.ApiError("expected application/json", 415)
            try:
                d = json.loads(self.body or b"{}")
            except ValueError:
                raise api.ApiError("invalid JSON") from None
            if not isinstance(d, dict):
                raise api.ApiError("expected a JSON object")
            self._data = d
        return self._data


class FileBody:
    """A download streamed from a temp file (removed once it's sent)."""

    def __init__(self, path: Path):
        self.path = path
        self.size = path.stat().st_size

    @contextmanager
    def open(self):
        try:
            with open(self.path, "rb") as f:
                yield f
        finally:
            shutil.rmtree(self.path.parent, ignore_errors=True)


def handles(path: str) -> bool:
    return path.startswith(PREFIXES)


def open_without_session(req: Request) -> bool:
    if req.path in OPEN:
        return True
    return req.path.startswith("/api/auth/enroll/") and not req.login_ready and req.plain_local


def _json(status: int, payload: Any) -> tuple[int, str, bytes]:
    return status, JSON, redact.scrub(json.dumps(payload)).encode()


def _err(e: Exception, status: int | None = None) -> api.ApiError:
    return api.ApiError(str(e), status or getattr(e, "status", 400), getattr(e, "field", None))


@contextmanager
def _conn(app):
    conn = db.connect(app.db_path)
    try:
        yield conn
    finally:
        conn.close()


def route(app, req: Request):
    fn = ROUTES.get((req.method, req.path))
    if fn is None:
        return _json(404 if req.method in ("GET", "POST") else 405, {"error": "not found"})
    try:
        with _conn(app) as conn:
            return fn(app, conn, req)
    except (auth.AuthError, access.AccessError, prefs.PrefError, secretstore.SecretError) as e:
        if isinstance(e, auth.AuthError) and e.retry_after:
            app.add_header("Retry-After", str(e.retry_after))
        raise _err(e) from None


# --- first-run setup -----------------------------------------------------------------------------------------------

def plaid_keys_set() -> bool:
    values = config.key_values()
    return bool(values.get("PLAID_CLIENT_ID") and values.get("PLAID_SECRET"))


def needs_setup(conn: sqlite3.Connection) -> bool:
    """First run: no Plaid keys, no bank, nothing added by hand, and the set-up wasn't finished or skipped."""
    if prefs.get(conn, "setup.done"):
        return False
    has_data = conn.execute("SELECT (SELECT COUNT(*) FROM items) + (SELECT COUNT(*) FROM manual_accounts)").fetchone()[0]
    return not has_data and not plaid_keys_set()


def hermes_detected() -> bool:
    """Hermes is installed for this user (its home directory exists; nothing in it is read)."""
    home = os.environ.get("HERMES_HOME")
    return Path(home).is_dir() if home else (Path.home() / ".hermes").is_dir()


def setup_state(app, conn, req):
    s = secretstore.status()
    return _json(200, {"needed": needs_setup(conn), "done": prefs.get(conn, "setup.done"), "plaid": s["plaid"],
                       "alpaca": s["alpaca"],   # TASK-20: the setup's Alpaca step (whether its key file is there)
                       "keys_path": s["path"], "demo": prefs.get(conn, "plaid.demo"), "hermes": hermes_detected(),
                       "bind": f"{app.host}:{app.port}", "exposed": not access.is_loopback(app.host),
                       "access": req.acc.mode, "login_ready": auth.configured(conn),
                       "banks": conn.execute("SELECT COUNT(*) FROM items WHERE removed_at IS NULL").fetchone()[0],
                       "slots_used": db.slots_used(conn), "slot_limit": config.SLOT_LIMIT})


def setup_plaid(app, conn, req):
    """The first step's keys and the third step's choice: demo (Sandbox, fake data) or real banks (Production)."""
    d = req.data()
    demo = d.get("demo")
    if not isinstance(demo, bool):
        raise api.ApiError("demo must be true (Sandbox) or false (real banks)", field="demo")
    updates = {"PLAID_ENV": "sandbox" if demo else "production"}
    if d.get("client_id") or d.get("secret"):
        updates.update(PLAID_CLIENT_ID=d.get("client_id"), PLAID_SECRET=d.get("secret"))
    elif not plaid_keys_set():
        raise api.ApiError("Paste your Plaid client ID and secret first.", field="client_id")
    secretstore.write(updates)
    prefs.save(conn, {"plaid.demo": demo}, allowed=["plaid.demo"])
    app.reset_client()
    return _json(200, {"plaid": secretstore.status()["plaid"], "demo": demo})


def setup_done(app, conn, req):
    prefs.save(conn, {"setup.done": True}, allowed=["setup.done"])
    return _json(200, {"done": True})


def suggestions(conn: sqlite3.Connection, today: date) -> list[dict[str, Any]]:
    """Suggested monthly caps from the last 60 days (half the spend, rounded up like the Budgets page's
    suggestions: insights.round_cap), per spending category that has some; its cap if it has one already."""
    since = (today - timedelta(days=SUGGEST_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT c.name, c.monthly_cap, COALESCE(SUM(v.spend), 0) AS spent FROM categories c "
        "LEFT JOIN v_transactions v ON v.category_id = c.id AND v.date > ? AND v.date <= ? "
        "WHERE c.is_income = 0 AND c.is_transfer = 0 GROUP BY c.id ORDER BY spent DESC, c.name",
        (since, today.isoformat())).fetchall()
    out = []
    for r in rows:
        if r["name"] in insights.NO_SUGGEST or r["spent"] <= 0:
            continue
        out.append({"category": r["name"], "spent": round(r["spent"], 2), "days": SUGGEST_DAYS,
                    "suggested": insights.round_cap(r["spent"] * 30 / SUGGEST_DAYS), "cap": r["monthly_cap"]})
    return out


def setup_suggestions(app, conn, req):
    inst = conn.execute("SELECT institution_name FROM items WHERE removed_at IS NULL ORDER BY created_at").fetchall()
    return _json(200, {"days": SUGGEST_DAYS, "banks": [r[0] for r in inst if r[0]],
                       "categories": suggestions(conn, app.today())})


# --- the Settings page ---------------------------------------------------------------------------------------------

VALUATIONS = (("carfax", "Carfax", "Car values"), ("redfin", "Redfin", "Home values"),
              ("photos", "Photos", "Car and home pictures (Wikimedia Commons)"))
USAGE_OF = {"carfax": "carfax", "redfin": "redfin", "photos": "commons"}


def chrome_status() -> dict[str, Any]:
    """Whether Carfax can run: a Chrome with remote debugging answering on 127.0.0.1:9222 (valuation.browser)."""
    from . import valuation
    why = valuation.browser.available()
    return {"ready": why is None, "why": why}


def settings_page(app, conn, req):
    from . import chat
    mcp_state.reconcile(conn)   # review N11: what a stopped app left is settled here too, not only by Ask or a decide
    owner = chat._request_owner(req)
    pending_actions = mcp_state.pending_for_owner(conn, owner)
    today = app.today()
    month = today.isoformat()[:7]
    acc = req.acc
    on = config.free_sources(conn=conn)
    env_set = config.free_source_overrides()
    caps = config.api_caps()
    used = {r["provider"]: r["calls"] for r in conn.execute("SELECT provider, calls FROM api_usage WHERE month = ?",
                                                            (month,))}
    mcp_extra = importlib.util.find_spec("mcp") is not None   # the optional extra (/mcp answers 501 without it)
    s = secretstore.status()
    return _json(200, {
        "prefs": prefs.public(conn),
        "secrets": s,
        "plaid": {"slots_used": db.slots_used(conn), "slot_limit": config.SLOT_LIMIT, "demo": prefs.get(conn, "plaid.demo"),
                  "banks": conn.execute("SELECT COUNT(*) FROM items WHERE removed_at IS NULL").fetchone()[0],
                  # TASK-22: the plan (no API for it) and whether "Ask banks for fresh data" may call Plaid
                  "refresh": syncnow.refresh_policy(conn, s["plaid"].get("env"))},
        "access": access.summary(conn, acc, app.host, app.port),
        "login": auth.summary(conn, app._cookie(req.headers)),
        "signed_in": req.session is not None,
        "mcp": {**mcp_state.summary(conn), "pending": len(pending_actions), "enabled": prefs.get(conn, "mcp.enabled"),
                "installed": mcp_extra,
                "pending_actions": pending_actions,   # MCP changes plus this originating client's Ask changes
                "endpoint": f"http://127.0.0.1:{app.port}/mcp", "public_endpoints": [f"{u}/mcp" for u in acc.public_urls],
                "tokens": auth.mcp_tokens(conn), "token_needed": acc.mode != "local",
                # for the Claude Desktop snippet (it starts `finance mcp` itself, on stdio)
                "command": str(Path(sys.executable).with_name("finance")),
                "data_dir": os.environ.get("FINANCE_DATA_DIR")},
        "valuations": [{"key": k, "name": name, "what": what, "on": on[k], "env_flag": env_set.get(k),
                        "used": used.get(USAGE_OF[k], 0), "cap": caps[USAGE_OF[k]],
                        # Carfax only works through a local Chrome with remote debugging (checked, never started)
                        **({"chrome": chrome_status()} if k == "carfax" else {})} for k, name, what in VALUATIONS],
        "ai": {"hermes_detected": hermes_detected(), "key_set": s["ai_key"]["set"], "presets": prefs.AI_PRESETS,
               "min_confidence_default": 0.7,
               "spent": round(usage.ai_spent(conn, today), 2)},   # TASK-23: what Ask cost this month (usage.py)
        "options": {"currencies": list(prefs.CURRENCIES), "locales": list(prefs.LOCALES)},
        "counts": {"transactions": conn.execute("SELECT COUNT(*) FROM v_transactions").fetchone()[0]},
    })


def save_prefs(app, conn, req):
    return _json(200, {"saved": prefs.save(conn, req.data(), allowed=prefs.PAGE_KEYS), "prefs": prefs.public(conn)})


def secrets_plaid(app, conn, req):
    """Replace keys: the client ID and secret (both) and the environment. Demo mode ends with Production keys."""
    d = req.data()
    env = d.get("env", "production")
    if env not in secretstore.PLAID_ENVS:
        raise api.ApiError("env must be production or sandbox", field="env")
    if not (d.get("client_id") and d.get("secret")):
        raise api.ApiError("Paste both the client ID and the secret.", field="client_id" if not d.get("client_id") else "secret")
    secretstore.write({"PLAID_CLIENT_ID": d["client_id"], "PLAID_SECRET": d["secret"], "PLAID_ENV": env})
    if env == "production" and prefs.get(conn, "plaid.demo"):
        prefs.save(conn, {"plaid.demo": False}, allowed=["plaid.demo"])
    app.reset_client()
    return _json(200, {"plaid": secretstore.status()["plaid"]})


def secrets_ai(app, conn, req):
    key = req.data().get("key")
    secretstore.write({secretstore.AI_KEY: key} if key else {secretstore.AI_KEY: None})
    return _json(200, {"ai_key": secretstore.status()["ai_key"]})


def access_mode(app, conn, req):
    access.set_mode(conn, req.data().get("mode"), auth.configured(conn), req.acc)
    return _json(200, access.summary(conn, app.access(conn), app.host, app.port))


def access_proxy(app, conn, req):
    access.set_proxy(conn, req.data())
    return _json(200, access.summary(conn, app.access(conn), app.host, app.port))


def access_identities(app, conn, req):
    d = req.data()
    if "add" in d:
        access.add_identity(conn, d["add"])
    elif "remove" in d:
        access.remove_identity(conn, d["remove"], req.acc)
    else:
        raise api.ApiError("add (a login) or remove (an id) is required", field="identity")
    return _json(200, access.summary(conn, app.access(conn), app.host, app.port))


# --- the built-in login ----------------------------------------------------------------------------------------------

def _set_cookie(app, req: Request, token: str | None) -> None:
    attrs = "Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if req.secure else "")
    if token:
        app.add_header("Set-Cookie", f"{auth.COOKIE}={token}; {attrs}; Max-Age={auth.ABSOLUTE_SECONDS}")
    else:
        app.add_header("Set-Cookie", f"{auth.COOKIE}=; {attrs}; Max-Age=0")


def auth_state(app, conn, req):
    ready = auth.configured(conn)
    return _json(200, {"mode": req.acc.mode, "configured": ready, "signed_in": req.session is not None,
                       "username": req.session["username"] if req.session else None,
                       "setup_allowed": (not ready and req.plain_local) or req.acc.mode != "builtin" or req.session is not None,
                       "https": req.secure, "lockout": {"tries": auth.MAX_TRIES, "minutes": auth.LOCK_SECONDS // 60}})


def auth_login(app, conn, req):
    d = req.data()
    ticket = auth.login_password(conn, d.get("username"), d.get("password"), req.ip)
    return _json(200, {"ticket": ticket, "next": "code"})


def auth_code(app, conn, req):
    d = req.data()
    token, _csrf = auth.login_code(conn, d.get("ticket"), code=d.get("code"), recovery=d.get("recovery"), ip=req.ip,
                                   user_agent=req.headers.get("user-agent"))
    _set_cookie(app, req, token)
    return _json(200, {"signed_in": True})


def auth_logout(app, conn, req):
    auth.end_session(conn, app._cookie(req.headers))
    _set_cookie(app, req, None)
    return _json(200, {"signed_in": False})


def auth_logout_all(app, conn, req):
    n = auth.end_all_sessions(conn)
    _set_cookie(app, req, None)
    return _json(200, {"ended": n})


def enroll_start(app, conn, req):
    d = req.data()
    return _json(200, auth.enroll_start(d.get("username"), d.get("password"), d.get("confirm")))


def enroll_qr(app, conn, req):
    """The authenticator QR code as SVG, drawn here (qr.py): the key never goes to a third-party service."""
    from . import qr
    uri = auth.enroll_qr_uri(req.data().get("enroll_id"))
    return _json(200, {"svg": qr.svg(uri, label="QR code for your authenticator app")})


def enroll_verify(app, conn, req):
    d = req.data()
    return _json(200, auth.enroll_verify(d.get("enroll_id"), d.get("code")))


def enroll_finish(app, conn, req):
    """The last step: the account is written, the app switches to the built-in login, and this browser is signed in."""
    d = req.data()
    uid = auth.enroll_finish(conn, d.get("enroll_id"), d.get("saved"))
    prefs.save(conn, {"access.mode": "builtin"}, allowed=["access.mode"])
    token, _csrf = auth.create_session(conn, uid, req.ip, req.headers.get("user-agent"))
    _set_cookie(app, req, token)
    return _json(200, {"mode": "builtin", "signed_in": True})


def auth_password(app, conn, req):
    d = req.data()
    auth.change_password(conn, d.get("current"), d.get("new"), keep_token=app._cookie(req.headers))
    return _json(200, {"changed": True})


def auth_recovery(app, conn, req):
    return _json(200, {"recovery_codes": auth.regenerate_recovery_codes(conn)})


# --- MCP tokens -------------------------------------------------------------------------------------------------------

def tokens_list(app, conn, req):
    return _json(200, {"tokens": auth.mcp_tokens(conn)})


def tokens_new(app, conn, req):
    return _json(200, auth.mcp_token_create(conn, req.data().get("name")))


def tokens_revoke(app, conn, req):
    auth.mcp_token_revoke(conn, req.data().get("id"))
    return _json(200, {"tokens": auth.mcp_tokens(conn)})


def pending_decide(app, conn, req, approve: bool):
    """TASK-21: Approve / Decline on a change an MCP client asked for. Approved, it's made the way the Hermes plugin makes
    a write after its approval card (tooldefs.apply: checked again, then the dashboard's own functions)."""
    from . import chat, tooldefs
    d = req.data()
    action_id = d.get("id")
    rows = mcp_state.pending(conn, action_id) if isinstance(action_id, int) and not isinstance(action_id, bool) else []
    found = rows[0] if rows else None
    if found and found["origin"] in chat.ASK_ORIGINS and mcp_state.ask_owner(conn, action_id) != chat._request_owner(req):
        raise api.ApiError(f"no pending action #{action_id}", 404, field="id")
    try:
        action = mcp_state.decide(conn, action_id, approve,
                                  lambda tool, args: tooldefs.apply(tool, args, app.db_path, app.today()),
                                  (tooldefs.ToolError,))
    except mcp_state.DecideError as e:
        raise _err(e) from None
    visible = mcp_state.pending_for_owner(conn, chat._request_owner(req))
    return _json(200, {"action": action, "pending_actions": visible, "pending": len(visible)})


# --- export / backup ------------------------------------------------------------------------------------------------

def _safe_cell(column: str, value: Any) -> Any:
    """A text cell that a spreadsheet would run as a formula (=, +, -, @ first) is kept as text."""
    if column in TEXT_COLUMNS and isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _tmp(name: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="hf-dl-"))
    os.chmod(d, 0o700)
    return d / name


def export_csv(app, conn, req):
    """Every transaction (newest first) with its category, where the category came from, and the notes."""
    path = _tmp("export.csv")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(EXPORT_COLUMNS)
        for r in conn.execute(f"SELECT {', '.join(EXPORT_COLUMNS)} FROM v_transactions ORDER BY date DESC, transaction_id"):
            w.writerow([_safe_cell(c, r[c]) for c in EXPORT_COLUMNS])
    app.add_header("Content-Disposition", f'attachment; filename="hermes-finance-transactions-{app.today().isoformat()}.csv"')
    return 200, "text/csv; charset=utf-8", FileBody(path)


def backup_db(app, conn, req):
    """The database with every credential taken out (backup.make_backup: bank access tokens, the built-in login, the
    MCP tokens). After a restore: reconnect each bank, set the login up again. The secrets file is never part of it."""
    path = backup.make_backup(conn, _tmp("finance-backup.db"))
    app.add_header("Content-Disposition", f'attachment; filename="hermes-finance-backup-{app.today().isoformat()}.db"')
    return 200, "application/vnd.sqlite3", FileBody(path)


# --- import ---------------------------------------------------------------------------------------------------------

def _file(req: Request) -> tuple[str, bytes]:
    d = req.data()
    name = str(d.get("filename") or "file")[:200]
    try:
        raw = base64.b64decode(str(d.get("data") or ""), validate=True)
    except ValueError:
        raise api.ApiError("the file didn't arrive whole; choose it again", field="file") from None
    return name, raw


def import_preview(app, conn, req):
    from . import importer
    name, raw = _file(req)
    try:
        return _json(200, importer.preview(conn, name, raw, req.data(), app.today()))
    except importer.ImportRefused as e:
        raise api.ApiError(str(e), field=e.field) from None


def import_commit(app, conn, req):
    from . import importer
    name, raw = _file(req)
    try:
        return _json(200, importer.commit(conn, name, raw, req.data(), app.today()))
    except importer.ImportRefused as e:
        raise api.ApiError(str(e), field=e.field) from None


ROUTES = {
    ("GET", "/api/settings/page"): settings_page,
    ("POST", "/api/prefs"): save_prefs,
    ("POST", "/api/secrets/plaid"): secrets_plaid,
    ("POST", "/api/secrets/ai"): secrets_ai,
    ("POST", "/api/access"): access_mode,
    ("POST", "/api/access/proxy"): access_proxy,
    ("POST", "/api/access/identities"): access_identities,
    ("GET", "/api/auth/state"): auth_state,
    ("POST", "/api/auth/login"): auth_login,
    ("POST", "/api/auth/code"): auth_code,
    ("POST", "/api/auth/logout"): auth_logout,
    ("POST", "/api/auth/logout-all"): auth_logout_all,
    ("POST", "/api/auth/enroll/start"): enroll_start,
    ("POST", "/api/auth/enroll/qr"): enroll_qr,
    ("POST", "/api/auth/enroll/verify"): enroll_verify,
    ("POST", "/api/auth/enroll/finish"): enroll_finish,
    ("POST", "/api/auth/password"): auth_password,
    ("POST", "/api/auth/recovery"): auth_recovery,
    ("GET", "/api/mcp/tokens"): tokens_list,
    ("POST", "/api/mcp/tokens"): tokens_new,
    ("POST", "/api/mcp/tokens/revoke"): tokens_revoke,
    ("POST", "/api/mcp/pending/approve"): lambda app, conn, req: pending_decide(app, conn, req, True),    # TASK-21
    ("POST", "/api/mcp/pending/decline"): lambda app, conn, req: pending_decide(app, conn, req, False),
    ("GET", "/api/export.csv"): export_csv,
    ("GET", "/api/backup"): backup_db,
    ("GET", "/api/setup"): setup_state,
    ("POST", "/api/setup/plaid"): setup_plaid,
    ("POST", "/api/setup/done"): setup_done,
    ("GET", "/api/setup/suggestions"): setup_suggestions,
    ("POST", "/api/import/preview"): import_preview,
    ("POST", "/api/import/commit"): import_commit,
}

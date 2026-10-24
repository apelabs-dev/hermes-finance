"""Built-in login (TASK-18, Settings -> Access & security -> Built-in login) and the MCP bearer tokens.

One account: a username and a password (argon2id, argon2-cffi) plus a required authenticator app (TOTP, RFC 6238:
HMAC-SHA1, 30 s steps, 6 digits, the step before and after accepted, a code never accepted twice) and 8 one-time
recovery codes (argon2id too). Signing in is two steps: the password gives a short-lived ticket, the ticket and a
6-digit code (or a recovery code) give a session: a random 256-bit token in a cookie, of which only the SHA-256 is
stored, with an idle and an absolute expiry, and a CSRF token that every state-changing request must send back.

Wrong tries: 5 in a row lock that username and that address for 15 minutes (the password step and the code step both
count). Comparisons of secrets are constant-time (hmac.compare_digest; argon2's own verify); an unknown username costs
the same argon2 verify as a known one.

Setting up happens in three steps held in memory (enroll_*): the password, then the authenticator (confirmed with a
code), then the recovery codes; nothing is written until the last step, so an abandoned set-up leaves nothing behind.
argon2-cffi is imported only when a password is hashed or checked (the Hermes plugin never loads it).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import secrets
import sqlite3
import struct
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .db import now

ISSUER = "Hermes Finances"
STEP = 30
DIGITS = 6
SKEW = 1                          # steps accepted on either side of now
MIN_PASSWORD, MAX_PASSWORD = 12, 256
MAX_USERNAME = 64
RECOVERY_COUNT = 8
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o, 1/l/i
MAX_TRIES = 5
LOCK_SECONDS = 15 * 60
IDLE_SECONDS = 8 * 3600
ABSOLUTE_SECONDS = 7 * 86400
TOUCH_SECONDS = 60                # a session's last_seen_at is written at most once a minute
TICKET_SECONDS = 5 * 60           # between the password step and the code step
ENROLL_SECONDS = 15 * 60
MAX_ENROLLMENTS = 32              # unfinished set-ups held at once
COOKIE = "hf_session"
MCP_PREFIX = "hfm_"

_lock = threading.Lock()
_tickets: dict[str, dict[str, Any]] = {}   # sha256(ticket) -> {user_id, ip, until}
_enrollments: dict[str, dict[str, Any]] = {}
_hasher = None
_dummy_hash: str | None = None


class AuthError(Exception):
    """A refusal in plain words; status is the HTTP status to answer with, field the form field it's about."""

    def __init__(self, message: str, status: int = 400, field: str | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.field = field
        self.retry_after = retry_after


# --- hashing ---------------------------------------------------------------------------------------------------------

def hasher():
    """argon2id with argon2-cffi's defaults (RFC 9106's low-memory profile: t=3, 64 MiB, p=4)."""
    global _hasher
    if _hasher is None:
        from argon2 import PasswordHasher
        _hasher = PasswordHasher()
    return _hasher


def hash_secret(text: str) -> str:
    return hasher().hash(text)


def verify_secret(stored: str, text: str) -> bool:
    from argon2.exceptions import InvalidHashError, VerificationError
    try:
        return hasher().verify(stored, text)
    except (VerificationError, InvalidHashError):
        return False


def _dummy() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_secret(secrets.token_urlsafe(24))
    return _dummy_hash


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- TOTP (RFC 6238 over RFC 4226) -------------------------------------------------------------------------------------

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.upper().replace(" ", "")
    return base64.b32decode(s + "=" * (-len(s) % 8))


def hotp(secret: str, counter: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def totp_step(at: float | None = None) -> int:
    return int((time.time() if at is None else at) // STEP)


def totp(secret: str, at: float | None = None) -> str:
    return hotp(secret, totp_step(at))


def match_totp(secret: str, code: str, last_step: int = 0, at: float | None = None) -> int | None:
    """The step `code` is valid for (now, or one step either side), if it's after `last_step`; else None. Every
    candidate is compared, in constant time."""
    code = "".join(str(code or "").split())
    if len(code) != DIGITS or not code.isdigit():
        return None
    found = None
    here = totp_step(at)
    for step in range(here - SKEW, here + SKEW + 1):
        if hmac.compare_digest(hotp(secret, step), code) and step > last_step and found is None:
            found = step
    return found


def otpauth_uri(username: str, secret: str) -> str:
    label = quote(f"{ISSUER}:{username}", safe="")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER, safe='')}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP}")


def grouped(secret: str) -> str:
    """The key as people type it: groups of four."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


# --- recovery codes ----------------------------------------------------------------------------------------------------

def new_recovery_codes() -> list[str]:
    def one() -> str:
        s = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(8))
        return f"{s[:4]}-{s[4:]}"
    return [one() for _ in range(RECOVERY_COUNT)]


def _norm_code(code: str) -> str:
    return "".join(ch for ch in str(code or "").lower() if ch.isalnum())


def _recovery_hash(code: str) -> str:
    return hash_secret(_norm_code(code))


# --- checks ----------------------------------------------------------------------------------------------------------

def check_username(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise AuthError("Choose a username.", field="username")
    name = name.strip()
    if len(name) > MAX_USERNAME or any(c.isspace() for c in name) or not name.isprintable():
        raise AuthError(f"A username is one word of at most {MAX_USERNAME} characters.", field="username")
    return name


def check_password(pw: Any, field: str = "password") -> str:
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD:
        raise AuthError(f"Use at least {MIN_PASSWORD} characters; a long phrase works well.", field=field)
    if len(pw) > MAX_PASSWORD:
        raise AuthError(f"Use at most {MAX_PASSWORD} characters.", field=field)
    if len(set(pw)) < 4:
        raise AuthError("That password repeats too few characters; try a phrase.", field=field)
    return pw


# --- the account -------------------------------------------------------------------------------------------------------

def user(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM auth_users ORDER BY id LIMIT 1").fetchone()


def configured(conn: sqlite3.Connection) -> bool:
    return user(conn) is not None


def summary(conn: sqlite3.Connection, current_token: str | None = None) -> dict[str, Any]:
    """What Settings shows about the built-in login (never a hash or a key)."""
    u = user(conn)
    if u is None:
        return {"configured": False}
    left = conn.execute("SELECT COUNT(*) FROM auth_recovery_codes WHERE user_id = ? AND used_at IS NULL",
                        (u["id"],)).fetchone()[0]
    mine = sha256(current_token) if current_token else None
    sessions = [{"created_at": r["created_at"], "last_seen_at": r["last_seen_at"], "user_agent": r["user_agent"],
                 "this": r["token_hash"] == mine}
                for r in conn.execute("SELECT * FROM auth_sessions WHERE user_id = ? ORDER BY last_seen_at DESC",
                                      (u["id"],)) if _alive(r)]
    return {"configured": True, "username": u["username"], "created_at": u["created_at"],
            "password_changed_at": u["password_changed_at"], "recovery_left": left, "sessions": sessions,
            "lockout": {"tries": MAX_TRIES, "minutes": LOCK_SECONDS // 60}}


def reset(conn: sqlite3.Connection) -> None:
    """Remove the built-in login: the account, its codes and sessions (`finance access reset-login`)."""
    with conn:
        conn.execute("DELETE FROM auth_sessions")
        conn.execute("DELETE FROM auth_recovery_codes")
        conn.execute("DELETE FROM auth_users")
        conn.execute("DELETE FROM auth_failures")


# --- set-up (three steps, in memory until the last) ------------------------------------------------------------------

def _prune(store: dict, at: float) -> None:
    for k in [k for k, v in store.items() if v["until"] < at]:
        del store[k]


def enroll_start(username: Any, password: Any, confirm: Any = None, at: float | None = None) -> dict[str, Any]:
    """Step 1: a username and a password. Returns the enrollment id and the new authenticator key."""
    name = check_username(username)
    pw = check_password(password)
    if confirm is not None and not hmac.compare_digest(str(confirm), pw):
        raise AuthError("The two passwords are different.", field="confirm")
    at = time.time() if at is None else at
    secret = new_totp_secret()
    eid = secrets.token_urlsafe(24)
    entry = {"username": name, "password_hash": hash_secret(pw), "secret": secret, "until": at + ENROLL_SECONDS,
             "step": None, "codes": None}
    with _lock:
        _prune(_enrollments, at)
        while len(_enrollments) >= MAX_ENROLLMENTS:  # memory stays bounded; the oldest unfinished set-up goes first
            del _enrollments[min(_enrollments, key=lambda k: _enrollments[k]["until"])]
        _enrollments[sha256(eid)] = entry
    return {"enroll_id": eid, "username": name, "key": grouped(secret), "otpauth": otpauth_uri(name, secret)}


def _enrollment(eid: Any, at: float) -> dict[str, Any]:
    with _lock:
        _prune(_enrollments, at)
        entry = _enrollments.get(sha256(str(eid or "")))
    if entry is None:
        raise AuthError("This set-up has expired; start again.", 410)
    return entry


def enroll_qr_uri(eid: Any, at: float | None = None) -> str:
    entry = _enrollment(eid, time.time() if at is None else at)
    return otpauth_uri(entry["username"], entry["secret"])


def enroll_verify(eid: Any, code: Any, at: float | None = None) -> dict[str, Any]:
    """Step 2: a code from the authenticator app. Returns the 8 recovery codes (shown once)."""
    at = time.time() if at is None else at
    entry = _enrollment(eid, at)
    step = match_totp(entry["secret"], str(code or ""), 0, at)
    if step is None:
        raise AuthError("That code doesn't match. Check the time on your phone and try the newest code.", field="code")
    codes = new_recovery_codes()
    entry.update(step=step, codes=codes, code_hashes=[_recovery_hash(c) for c in codes])
    return {"recovery_codes": codes}


def enroll_finish(conn: sqlite3.Connection, eid: Any, saved: Any, at: float | None = None) -> int:
    """Step 3: "I saved them". Writes the account (replacing any earlier one) and its codes; returns its id. Every
    session of an earlier account ends."""
    at = time.time() if at is None else at
    entry = _enrollment(eid, at)
    if entry["step"] is None:
        raise AuthError("Confirm the authenticator first.", 409)
    if saved is not True:
        raise AuthError("Save the recovery codes first, then tick “I saved them”.", field="saved")
    ts = now()
    with conn:
        conn.execute("DELETE FROM auth_sessions")
        conn.execute("DELETE FROM auth_recovery_codes")
        conn.execute("DELETE FROM auth_users")
        cur = conn.execute("INSERT INTO auth_users (username, password_hash, totp_secret, totp_last_step, created_at, "
                           "password_changed_at) VALUES (?, ?, ?, ?, ?, ?)",
                           (entry["username"], entry["password_hash"], entry["secret"], entry["step"], ts, ts))
        uid = cur.lastrowid
        conn.executemany("INSERT INTO auth_recovery_codes (user_id, code_hash, created_at) VALUES (?, ?, ?)",
                         [(uid, h, ts) for h in entry["code_hashes"]])
    with _lock:
        _enrollments.pop(sha256(str(eid)), None)
    return uid


# --- lockout ---------------------------------------------------------------------------------------------------------

def _keys(username: str | None, ip: str | None) -> list[str]:
    keys = [f"ip:{ip or 'local'}"]
    if username:
        keys.insert(0, f"user:{username.strip().casefold()}")
    return keys


def _ts(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _at(ts: str | None) -> float:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() if ts else 0.0


def locked_for(conn: sqlite3.Connection, keys: list[str], at: float) -> int:
    """Seconds until the longest lock on any of `keys` ends (0: not locked)."""
    marks = ", ".join("?" * len(keys))
    until = [_at(r[0]) for r in conn.execute(f"SELECT locked_until FROM auth_failures WHERE key IN ({marks})", keys)]
    return max([math.ceil(u - at) for u in until if u > at] or [0])


def _fail(conn: sqlite3.Connection, keys: list[str], at: float) -> None:
    with conn:
        for key in keys:
            row = conn.execute("SELECT * FROM auth_failures WHERE key = ?", (key,)).fetchone()
            stale = row is None or _at(row["first_at"]) + LOCK_SECONDS < at or (
                row["locked_until"] and _at(row["locked_until"]) <= at)
            failures = 1 if stale else row["failures"] + 1
            first = _ts(at) if stale else row["first_at"]
            locked = _ts(at + LOCK_SECONDS) if failures >= MAX_TRIES else None
            conn.execute("INSERT INTO auth_failures (key, failures, first_at, locked_until) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET failures = excluded.failures, first_at = excluded.first_at, "
                         "locked_until = excluded.locked_until", (key, failures, first, locked))


def _clear(conn: sqlite3.Connection, keys: list[str]) -> None:
    with conn:
        conn.executemany("DELETE FROM auth_failures WHERE key = ?", [(k,) for k in keys])


def _locked_error(seconds: int) -> AuthError:
    minutes = max(1, -(-seconds // 60))
    return AuthError(f"Too many wrong tries. Try again in {minutes} min.", 429, retry_after=seconds)


# --- signing in --------------------------------------------------------------------------------------------------------

def login_password(conn: sqlite3.Connection, username: Any, password: Any, ip: str | None = None,
                   at: float | None = None) -> str:
    """Step 1 of signing in: returns a ticket for the code step."""
    at = time.time() if at is None else at
    name = str(username or "").strip()[:MAX_USERNAME]
    keys = _keys(name, ip)
    wait = locked_for(conn, keys, at)
    if wait:
        raise _locked_error(wait)
    u = conn.execute("SELECT * FROM auth_users WHERE username = ?", (name,)).fetchone() if name else None
    ok = verify_secret(u["password_hash"] if u else _dummy(), str(password or "")[:MAX_PASSWORD])
    if not (u and ok):
        _fail(conn, keys, at)
        wait = locked_for(conn, keys, at)
        raise _locked_error(wait) if wait else AuthError("Wrong username or password.", 401, field="password")
    ticket = secrets.token_urlsafe(32)
    with _lock:
        _prune(_tickets, at)
        _tickets[sha256(ticket)] = {"user_id": u["id"], "username": u["username"], "until": at + TICKET_SECONDS}
    return ticket


def login_code(conn: sqlite3.Connection, ticket: Any, code: Any = None, recovery: Any = None, ip: str | None = None,
               user_agent: str | None = None, at: float | None = None) -> tuple[str, str]:
    """Step 2: the 6-digit code (or a recovery code) for a ticket. Returns (session token, CSRF token)."""
    at = time.time() if at is None else at
    with _lock:
        _prune(_tickets, at)
        t = _tickets.get(sha256(str(ticket or "")))
    if t is None:
        raise AuthError("Sign in again: that step timed out.", 401)
    keys = _keys(t["username"], ip)
    wait = locked_for(conn, keys, at)
    if wait:
        raise _locked_error(wait)
    u = conn.execute("SELECT * FROM auth_users WHERE id = ?", (t["user_id"],)).fetchone()
    ok = False
    if u is not None and recovery:
        norm = _norm_code(recovery)
        for r in conn.execute("SELECT id, code_hash FROM auth_recovery_codes WHERE user_id = ? AND used_at IS NULL",
                              (u["id"],)).fetchall():
            if verify_secret(r["code_hash"], norm) and not ok:
                with conn:
                    conn.execute("UPDATE auth_recovery_codes SET used_at = ? WHERE id = ? AND used_at IS NULL",
                                 (now(), r["id"]))
                ok = True
    elif u is not None:
        step = match_totp(u["totp_secret"], str(code or ""), u["totp_last_step"], at)
        if step is not None:
            with conn:  # the step is claimed before the session exists: the same code can't open a second one
                claimed = conn.execute("UPDATE auth_users SET totp_last_step = ? WHERE id = ? AND totp_last_step < ?",
                                       (step, u["id"], step)).rowcount
            ok = claimed == 1
    if not ok:
        _fail(conn, keys, at)
        wait = locked_for(conn, keys, at)
        if wait:
            with _lock:
                _tickets.pop(sha256(str(ticket)), None)
            raise _locked_error(wait)
        raise AuthError("That recovery code doesn't match (or was used)." if recovery
                        else "That code doesn't match. Try the newest code.", 401, field="recovery" if recovery else "code")
    with _lock:
        _tickets.pop(sha256(str(ticket)), None)
    _clear(conn, keys)
    return create_session(conn, u["id"], ip, user_agent, at)


# --- sessions ----------------------------------------------------------------------------------------------------------

def create_session(conn: sqlite3.Connection, user_id: int, ip: str | None = None, user_agent: str | None = None,
                   at: float | None = None) -> tuple[str, str]:
    at = time.time() if at is None else at
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with conn:
        conn.execute("INSERT INTO auth_sessions (token_hash, user_id, csrf, created_at, last_seen_at, ip, user_agent) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (sha256(token), user_id, csrf, _ts(at), _ts(at), ip,
                                                      (user_agent or "")[:200] or None))
    return token, csrf


def _alive(row: sqlite3.Row, at: float | None = None) -> bool:
    at = time.time() if at is None else at
    return _at(row["created_at"]) + ABSOLUTE_SECONDS > at and _at(row["last_seen_at"]) + IDLE_SECONDS > at


def session(conn: sqlite3.Connection, token: str | None, at: float | None = None) -> dict[str, Any] | None:
    """The live session for a cookie's token (None: none, expired or ended). Expired rows are removed."""
    if not token:
        return None
    at = time.time() if at is None else at
    h = sha256(token)
    row = conn.execute("SELECT s.*, u.username FROM auth_sessions s JOIN auth_users u ON u.id = s.user_id "
                       "WHERE s.token_hash = ?", (h,)).fetchone()
    if row is None:
        return None
    if not _alive(row, at):
        with conn:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (h,))
        return None
    if _at(row["last_seen_at"]) + TOUCH_SECONDS <= at:
        with conn:
            conn.execute("UPDATE auth_sessions SET last_seen_at = ? WHERE token_hash = ?", (_ts(at), h))
    return {"user_id": row["user_id"], "username": row["username"], "csrf": row["csrf"], "token_hash": h}


def csrf_ok(sess: dict[str, Any] | None, sent: str | None) -> bool:
    return bool(sess and sent) and hmac.compare_digest(sess["csrf"], str(sent))


def end_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        with conn:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (sha256(token),))


def end_all_sessions(conn: sqlite3.Connection) -> int:
    with conn:
        return conn.execute("DELETE FROM auth_sessions").rowcount


def change_password(conn: sqlite3.Connection, current: Any, new: Any, keep_token: str | None = None) -> None:
    """Needs the current password; every other session ends."""
    u = user(conn)
    if u is None:
        raise AuthError("There is no built-in login yet.", 409)
    if not verify_secret(u["password_hash"], str(current or "")[:MAX_PASSWORD]):
        raise AuthError("That isn't the current password.", 401, field="current")
    pw = check_password(new, "new")
    with conn:
        conn.execute("UPDATE auth_users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
                     (hash_secret(pw), now(), u["id"]))
        conn.execute("DELETE FROM auth_sessions WHERE token_hash <> ?", (sha256(keep_token) if keep_token else "",))


def regenerate_recovery_codes(conn: sqlite3.Connection) -> list[str]:
    u = user(conn)
    if u is None:
        raise AuthError("There is no built-in login yet.", 409)
    codes = new_recovery_codes()
    hashes = [_recovery_hash(c) for c in codes]
    ts = now()
    with conn:
        conn.execute("DELETE FROM auth_recovery_codes WHERE user_id = ?", (u["id"],))
        conn.executemany("INSERT INTO auth_recovery_codes (user_id, code_hash, created_at) VALUES (?, ?, ?)",
                         [(u["id"], h, ts) for h in hashes])
    return codes


# --- MCP bearer tokens -------------------------------------------------------------------------------------------------

MAX_TOKEN_NAME = 60


def mcp_token_create(conn: sqlite3.Connection, name: Any) -> dict[str, Any]:
    """A new token for one client (hfm_ + 32 random bytes, urlsafe). The token is in this answer only."""
    if not isinstance(name, str) or not " ".join(name.split()):
        raise AuthError("Name the agent this token is for (e.g. Claude Desktop).", field="name")
    name = " ".join(name.split())[:MAX_TOKEN_NAME]
    if conn.execute("SELECT COUNT(*) FROM mcp_tokens WHERE revoked_at IS NULL").fetchone()[0] >= 20:
        raise AuthError("20 tokens are in use; revoke one first.", 409)
    token = MCP_PREFIX + secrets.token_urlsafe(32)
    with conn:
        cur = conn.execute("INSERT INTO mcp_tokens (name, token_hash, last4, created_at) VALUES (?, ?, ?, ?)",
                           (name, sha256(token), token[-4:], now()))
    return {"id": cur.lastrowid, "name": name, "token": token, "last4": token[-4:]}


def mcp_token_check(conn: sqlite3.Connection, token: str | None, at: float | None = None) -> int | None:
    """The id of the live token `token` is, else None (the last-used time is written at most once a minute)."""
    if not token or not token.startswith(MCP_PREFIX) or len(token) > 200:
        return None
    row = conn.execute("SELECT id, last_used_at FROM mcp_tokens WHERE token_hash = ? AND revoked_at IS NULL",
                       (sha256(token),)).fetchone()
    if row is None:
        return None
    at = time.time() if at is None else at
    if _at(row["last_used_at"]) + TOUCH_SECONDS <= at:
        with conn:
            conn.execute("UPDATE mcp_tokens SET last_used_at = ? WHERE id = ?", (_ts(at), row["id"]))
    return row["id"]


def mcp_token_revoke(conn: sqlite3.Connection, token_id: Any) -> None:
    if isinstance(token_id, bool) or not isinstance(token_id, int):
        raise AuthError("id must be a token's number", field="id")
    with conn:
        n = conn.execute("UPDATE mcp_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                         (now(), token_id)).rowcount
    if not n:
        raise AuthError("No such token (or it's revoked already).", 404)


def mcp_tokens(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [{"id": r["id"], "name": r["name"], "masked": f"{MCP_PREFIX}••••{r['last4']}",
             "created_at": r["created_at"], "last_used_at": r["last_used_at"]}
            for r in conn.execute("SELECT * FROM mcp_tokens WHERE revoked_at IS NULL ORDER BY id")]

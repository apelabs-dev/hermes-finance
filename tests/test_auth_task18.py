"""TASK-18: the built-in login (auth.py + the builtin gate in web.py): argon2id passwords, required TOTP (RFC 6238),
one-time recovery codes, sessions in an HttpOnly / Secure / SameSite=Strict cookie with idle and absolute expiry, CSRF
tokens, the 5-tries / 15-minute lockout per user and per address, the three-step set-up and the sign-in page."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

import pytest

from hermes_finance import auth, config, db, prefs
from hermes_finance.web import App

LOCAL = {"Host": "127.0.0.1:8765"}
JSONH = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
PUBLIC = {"Host": "money.example.com", "X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9",
          "Content-Type": "application/json"}
PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def quick_hashes(monkeypatch):
    """argon2id with small parameters so the suite stays fast (test_production_hasher checks the real ones)."""
    from argon2 import PasswordHasher
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    monkeypatch.setattr(auth, "_dummy_hash", None)
    auth._tickets.clear()
    auth._enrollments.clear()


@pytest.fixture
def app(conn):
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    return a


def call(app, method, path, headers, body=None, ip=None):
    status, ctype, out, extra = app.respond(method, path, headers, json.dumps(body).encode() if body is not None else b"", ip)
    return status, ctype, (json.loads(out) if ctype.startswith("application/json") else out.decode()), dict(extra)


def enroll(app, headers=JSONH, username="owner", at=None):
    """The three set-up steps through the API; returns (secret, recovery codes, Set-Cookie)."""
    s, _, r, _ = call(app, "POST", "/api/auth/enroll/start", headers, {"username": username, "password": PASSWORD, "confirm": PASSWORD})
    assert s == 200, r
    secret = r["key"].replace(" ", "")
    s, _, v, _ = call(app, "POST", "/api/auth/enroll/verify", headers, {"enroll_id": r["enroll_id"], "code": auth.totp(secret)})
    assert s == 200, v
    s, _, f, extra = call(app, "POST", "/api/auth/enroll/finish", headers, {"enroll_id": r["enroll_id"], "saved": True})
    assert s == 200 and f == {"mode": "builtin", "signed_in": True}, f
    return secret, v["recovery_codes"], extra["Set-Cookie"]


def cookie_of(set_cookie: str) -> dict[str, str]:
    token = set_cookie.split(";")[0].split("=", 1)[1]
    return {"Cookie": f"{auth.COOKIE}={token}"}


def csrf_of(app, headers) -> str:
    s, _, html, _ = call(app, "GET", "/", {k: v for k, v in headers.items() if k != "Content-Type"})
    assert s == 200
    return re.search(r'<meta name="csrf-token" content="([^"]+)">', html).group(1)


def sign_in(app, secret, headers=PUBLIC, ip="203.0.113.9", at=None):
    s, _, r, _ = call(app, "POST", "/api/auth/login", headers, {"username": "owner", "password": PASSWORD}, ip)
    assert s == 200, r
    step = auth.totp_step(at)
    code = auth.hotp(secret, step + 1)  # a code not used yet (the set-up claimed this step's)
    s, _, r, extra = call(app, "POST", "/api/auth/code", headers, {"ticket": r["ticket"], "code": code}, ip)
    return s, r, extra


# --- the pieces --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("t,code", [(59, "287082"), (1111111109, "081804"), (1111111111, "050471"), (1234567890, "005924"),
                                    (2000000000, "279037"), (20000000000, "353130")])
def test_totp_matches_rfc_6238_sha1_vectors(t, code):
    # RFC 6238 appendix B, SHA-1, secret "12345678901234567890"; the 8-digit codes' last six digits
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert auth.totp(secret, t) == code
    assert auth.match_totp(secret, code, 0, t) == t // 30


def test_totp_takes_one_step_either_side_and_never_the_same_step_twice():
    secret = auth.new_totp_secret()
    t = 1_800_000_000
    step = t // 30
    for k in (-1, 0, 1):
        assert auth.match_totp(secret, auth.hotp(secret, step + k), 0, t) == step + k
    assert auth.match_totp(secret, auth.hotp(secret, step + 2), 0, t) is None
    assert auth.match_totp(secret, auth.hotp(secret, step - 2), 0, t) is None
    assert auth.match_totp(secret, auth.hotp(secret, step), step, t) is None          # replay: that step was used
    assert auth.match_totp(secret, "12345", 0, t) is None and auth.match_totp(secret, "abcdef", 0, t) is None


def test_production_hasher_is_argon2id_with_the_library_defaults(monkeypatch):
    monkeypatch.setattr(auth, "_hasher", None)
    h = auth.hash_secret(PASSWORD)
    assert h.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert auth.verify_secret(h, PASSWORD) and not auth.verify_secret(h, PASSWORD + "x")
    assert auth.verify_secret("not a hash", PASSWORD) is False


def test_recovery_codes_look_right_and_otpauth_uri():
    codes = auth.new_recovery_codes()
    assert len(codes) == 8 == len(set(codes)) and all(re.fullmatch(r"[a-z2-9]{4}-[a-z2-9]{4}", c) for c in codes)
    uri = auth.otpauth_uri("owner", "JBSWY3DPEHPK3PXP")
    assert uri == ("otpauth://totp/Hermes%20Finances%3Aowner?secret=JBSWY3DPEHPK3PXP&issuer=Hermes%20Finances"
                   "&algorithm=SHA1&digits=6&period=30")


@pytest.mark.parametrize("pw,ok", [("short", False), ("aaaaaaaaaaaa", False), ("a" * 257, False), (PASSWORD, True)])
def test_password_rules(pw, ok):
    if ok:
        assert auth.check_password(pw) == pw
    else:
        with pytest.raises(auth.AuthError):
            auth.check_password(pw)


# --- the set-up ----------------------------------------------------------------------------------------------------

def test_setup_three_steps_write_nothing_until_the_last(app, conn):
    s, _, r, _ = call(app, "POST", "/api/auth/enroll/start", JSONH, {"username": "owner", "password": PASSWORD, "confirm": PASSWORD})
    assert s == 200 and set(r) == {"enroll_id", "username", "key", "otpauth"}
    secret = r["key"].replace(" ", "")
    assert r["otpauth"] == auth.otpauth_uri("owner", secret)
    # the QR code: SVG drawn here, of exactly that URI
    s, _, q, _ = call(app, "POST", "/api/auth/enroll/qr", JSONH, {"enroll_id": r["enroll_id"]})
    root = ET.fromstring(q["svg"])
    assert s == 200 and root.tag.endswith("svg") and "http" not in q["svg"].replace("http://www.w3.org/2000/svg", "")
    from hermes_finance import qr
    assert q["svg"] == qr.svg(r["otpauth"], label="QR code for your authenticator app")
    assert conn.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0] == 0
    s, _, bad, _ = call(app, "POST", "/api/auth/enroll/verify", JSONH, {"enroll_id": r["enroll_id"], "code": "000000"})
    assert s == 400 and bad["field"] == "code"
    s, _, v, _ = call(app, "POST", "/api/auth/enroll/verify", JSONH, {"enroll_id": r["enroll_id"], "code": auth.totp(secret)})
    assert s == 200 and len(v["recovery_codes"]) == 8
    s, _, e, _ = call(app, "POST", "/api/auth/enroll/finish", JSONH, {"enroll_id": r["enroll_id"], "saved": False})
    assert s == 400 and e["field"] == "saved" and conn.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0] == 0
    s, _, f, extra = call(app, "POST", "/api/auth/enroll/finish", JSONH, {"enroll_id": r["enroll_id"], "saved": True})
    assert s == 200 and prefs.get(conn, "access.mode") == "builtin"
    u = auth.user(conn)
    assert u["username"] == "owner" and u["password_hash"].startswith("$argon2id$") and u["totp_secret"] == secret
    hashes = [x[0] for x in conn.execute("SELECT code_hash FROM auth_recovery_codes")]
    assert len(hashes) == 8 and all(h.startswith("$argon2id$") for h in hashes)
    assert not any(c in " ".join(hashes) for c in v["recovery_codes"])            # stored hashed
    # plain http on localhost: the cookie drops Secure only there
    assert "HttpOnly" in extra["Set-Cookie"] and "SameSite=Strict" in extra["Set-Cookie"] and "Secure" not in extra["Set-Cookie"]
    # a finished set-up's id is gone
    assert call(app, "POST", "/api/auth/enroll/finish", JSONH, {"enroll_id": r["enroll_id"], "saved": True})[0] in (401, 410)


def test_passwords_and_codes_never_leave_the_server(app, conn):
    secret, codes, _ = enroll(app)
    ro = db.connect_readonly(config.db_path())
    u = ro.execute("SELECT password_hash, totp_secret FROM auth_users").fetchone()
    assert tuple(u) == (None, None)                                                # `finance query` sees NULL
    assert ro.execute("SELECT code_hash FROM auth_recovery_codes").fetchone()[0] is None
    assert ro.execute("SELECT token_hash, csrf FROM auth_sessions").fetchone()[:] == (None, None)
    ro.close()


def test_first_time_setup_only_from_this_computer(app, conn):
    prefs.save(conn, {"access.mode": "builtin"}, allowed=["access.mode"])   # chosen from the CLI, no login yet
    s, _, html, _ = call(app, "GET", "/", LOCAL)
    assert s == 200 and 'data-mode="setup"' in html and 'class="board signin"' in html
    s, _, html, _ = call(app, "GET", "/", {k: v for k, v in PUBLIC.items() if k != "Content-Type"})
    assert s == 200 and 'data-mode="signin"' in html                               # never the set-up from elsewhere
    s, _, r, _ = call(app, "POST", "/api/auth/enroll/start", PUBLIC, {"username": "x", "password": PASSWORD})
    assert s == 401 and r["signin"] is True
    # a rebinding page on 127.0.0.1 with its own Host doesn't count as this computer either
    s, _, r, _ = call(app, "POST", "/api/auth/enroll/start", {"Host": "evil.example:8765", "Content-Type": "application/json"},
                      {"username": "x", "password": PASSWORD})
    assert s == 401


# --- signing in ----------------------------------------------------------------------------------------------------

def test_sign_in_with_password_then_code_gives_a_secure_session(app, conn):
    secret, _, _ = enroll(app)
    s, _, page, _ = call(app, "GET", "/", {"Host": "money.example.com", "X-Forwarded-Proto": "https"})
    assert s == 200 and 'data-mode="signin"' in page and "/static/login.js" in page and "/static/app.js" not in page
    assert call(app, "GET", "/api/overview", PUBLIC)[:3:2] == (401, {"error": "sign in first", "signin": True})
    s, r, extra = sign_in(app, secret)
    assert s == 200 and r == {"signed_in": True}
    ck = extra["Set-Cookie"]
    assert ck.startswith(f"{auth.COOKIE}=") and "HttpOnly" in ck and "Secure" in ck and "SameSite=Strict" in ck
    token = ck.split(";")[0].split("=", 1)[1]
    assert len(token) >= 43                                                         # 32 random bytes, urlsafe
    assert conn.execute("SELECT COUNT(*) FROM auth_sessions WHERE token_hash = ?", (auth.sha256(token),)).fetchone()[0] == 1
    headers = {**PUBLIC, **cookie_of(ck)}
    assert call(app, "GET", "/api/overview", headers)[0] == 200
    s, _, html, _ = call(app, "GET", "/", {k: v for k, v in headers.items() if k != "Content-Type"})
    assert s == 200 and "/static/app.js" in html and 'name="csrf-token"' in html and "data-signed-in" in html


def test_every_post_with_a_session_needs_its_csrf_token(app, conn):
    secret, _, ck = enroll(app)
    headers = {**JSONH, **cookie_of(ck)}
    body = {"category": "Groceries", "monthly_cap": 321}
    assert call(app, "POST", "/api/budgets/cap", headers, body)[0] == 403
    assert call(app, "POST", "/api/budgets/cap", {**headers, "X-CSRF-Token": "nope"}, body)[0] == 403
    token = csrf_of(app, headers)
    assert call(app, "POST", "/api/budgets/cap", {**headers, "X-CSRF-Token": token}, body)[0] == 200
    # and the Origin check still runs first
    assert call(app, "POST", "/api/budgets/cap", {**headers, "X-CSRF-Token": token, "Origin": "https://evil.example"}, body)[0] == 403


def test_a_code_is_never_accepted_twice(app, conn):
    secret, _, _ = enroll(app)
    s, _, r, _ = call(app, "POST", "/api/auth/login", PUBLIC, {"username": "owner", "password": PASSWORD})
    code = auth.hotp(secret, auth.totp_step() + 1)
    assert call(app, "POST", "/api/auth/code", PUBLIC, {"ticket": r["ticket"], "code": code})[0] == 200
    s, _, r2, _ = call(app, "POST", "/api/auth/login", PUBLIC, {"username": "owner", "password": PASSWORD})
    s, _, e, _ = call(app, "POST", "/api/auth/code", PUBLIC, {"ticket": r2["ticket"], "code": code})
    assert s == 401 and "doesn't match" in e["error"]


def test_a_recovery_code_works_once(app, conn):
    secret, codes, _ = enroll(app)
    for expect in (200, 401):
        s, _, r, _ = call(app, "POST", "/api/auth/login", PUBLIC, {"username": "owner", "password": PASSWORD})
        s, _, out, _ = call(app, "POST", "/api/auth/code", PUBLIC, {"ticket": r["ticket"], "recovery": codes[0].upper()})
        assert s == expect, out
    assert conn.execute("SELECT COUNT(*) FROM auth_recovery_codes WHERE used_at IS NULL").fetchone()[0] == 7


def test_five_wrong_tries_lock_the_user_and_the_address_for_15_minutes(app, conn):
    secret, _, _ = enroll(app)
    t0 = 2_000_000_000.0
    for i in range(4):
        with pytest.raises(auth.AuthError) as e:
            auth.login_password(conn, "owner", "wrong password!", "198.51.100.1", at=t0 + i)
        assert e.value.status == 401
    with pytest.raises(auth.AuthError) as e:
        auth.login_password(conn, "owner", "wrong password!", "198.51.100.1", at=t0 + 5)
    assert e.value.status == 429 and "15 min" in str(e.value)
    # the right password is refused while locked, from any address (the user is locked) ...
    with pytest.raises(auth.AuthError, match="Too many"):
        auth.login_password(conn, "owner", PASSWORD, "192.0.2.50", at=t0 + 60)
    # ... and that address can't try another username either
    with pytest.raises(auth.AuthError, match="Too many"):
        auth.login_password(conn, "someone", "whatever else", "198.51.100.1", at=t0 + 60)
    # 15 minutes later it works again
    assert auth.login_password(conn, "owner", PASSWORD, "192.0.2.50", at=t0 + 5 + 15 * 60 + 1)


def test_wrong_codes_count_toward_the_lock_and_answers_carry_retry_after(app, conn):
    secret, _, _ = enroll(app)
    s, _, r, _ = call(app, "POST", "/api/auth/login", PUBLIC, {"username": "owner", "password": PASSWORD}, "203.0.113.9")
    for _ in range(4):
        assert call(app, "POST", "/api/auth/code", PUBLIC, {"ticket": r["ticket"], "code": "000000"}, "203.0.113.9")[0] == 401
    s, _, e, extra = call(app, "POST", "/api/auth/code", PUBLIC, {"ticket": r["ticket"], "code": "000000"}, "203.0.113.9")
    assert s == 429 and int(extra["Retry-After"]) > 800


def test_the_lock_counts_the_address_a_local_proxy_saw(app, conn):
    """Behind a proxy on this computer every request comes from 127.0.0.1: the last X-Forwarded-For entry is the address."""
    assert App._client_ip("127.0.0.1", {"x-forwarded-for": "1.2.3.4, 203.0.113.9"}) == "203.0.113.9"
    assert App._client_ip("198.51.100.7", {"x-forwarded-for": "10.0.0.1"}) == "198.51.100.7"   # not a local proxy
    assert App._client_ip("127.0.0.1", {}) == "127.0.0.1"


def test_unknown_user_costs_a_hash_check_too(app, conn, monkeypatch):
    enroll(app)
    seen = []
    real = auth.verify_secret
    monkeypatch.setattr(auth, "verify_secret", lambda stored, text: seen.append(stored) or real(stored, text))
    with pytest.raises(auth.AuthError):
        auth.login_password(conn, "nobody", PASSWORD)
    assert len(seen) == 1 and seen[0] == auth._dummy_hash


def test_sessions_end_when_idle_or_too_old(conn):
    conn.execute("INSERT INTO auth_users (username, password_hash, totp_secret, created_at, password_changed_at) "
                 "VALUES ('owner', 'x', 'y', 'z', 'z')")
    t0 = 2_000_000_000.0
    token, _ = auth.create_session(conn, 1, at=t0)
    touched = t0 + auth.IDLE_SECONDS - 60
    assert auth.session(conn, token, at=touched)                                           # still alive; touched now
    assert auth.session(conn, token, at=touched + auth.IDLE_SECONDS - 1)
    assert auth.session(conn, token, at=touched + 2 * auth.IDLE_SECONDS) is None           # idle too long
    token, _ = auth.create_session(conn, 1, at=t0)
    t = t0
    while t < t0 + auth.ABSOLUTE_SECONDS - auth.IDLE_SECONDS:                              # active all week
        t += auth.IDLE_SECONDS / 2
        assert auth.session(conn, token, at=t)
    assert auth.session(conn, token, at=t0 + auth.ABSOLUTE_SECONDS + 1) is None            # absolute expiry
    assert conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0          # expired rows removed


def test_sign_out_and_sign_out_everywhere(app, conn):
    secret, _, ck = enroll(app)
    a = {**JSONH, **cookie_of(ck)}
    s, r, extra = sign_in(app, secret)
    b = {**PUBLIC, **cookie_of(extra["Set-Cookie"])}
    assert call(app, "GET", "/api/overview", a)[0] == 200 and call(app, "GET", "/api/overview", b)[0] == 200
    s, _, out, extra = call(app, "POST", "/api/auth/logout-all", {**a, "X-CSRF-Token": csrf_of(app, a)}, {})
    assert s == 200 and out == {"ended": 2} and "Max-Age=0" in extra["Set-Cookie"]
    assert call(app, "GET", "/api/overview", a)[0] == 401 and call(app, "GET", "/api/overview", b)[0] == 401


def test_change_password_needs_the_current_one_and_signs_the_others_out(app, conn):
    secret, _, ck = enroll(app)
    a = {**JSONH, **cookie_of(ck)}
    s, r, extra = sign_in(app, secret)
    b = {**PUBLIC, **cookie_of(extra["Set-Cookie"])}
    token = csrf_of(app, a)
    s, _, e, _ = call(app, "POST", "/api/auth/password", {**a, "X-CSRF-Token": token}, {"current": "nope nope nope", "new": "another long phrase here"})
    assert s == 401 and e["field"] == "current"
    s, _, _, _ = call(app, "POST", "/api/auth/password", {**a, "X-CSRF-Token": token}, {"current": PASSWORD, "new": "another long phrase here"})
    assert s == 200
    assert call(app, "GET", "/api/overview", a)[0] == 200 and call(app, "GET", "/api/overview", b)[0] == 401
    assert auth.verify_secret(auth.user(conn)["password_hash"], "another long phrase here")


def test_mcp_needs_a_bearer_token_with_the_built_in_login(app, conn):
    enroll(app)
    s, _, e, _ = call(app, "POST", "/mcp", {**LOCAL, "Content-Type": "application/json"}, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert s == 401 and "bearer token" in e["error"]


def test_login_page_is_self_contained(app, conn):
    enroll(app)
    s, ctype, html, _ = call(app, "GET", "/", {"Host": "money.example.com", "X-Forwarded-Proto": "https"})
    assert ctype.startswith("text/html") and re.findall(r"https?://[^\s\"'<>]+", html) == []   # no Plaid script, nothing external
    assert "csrf-token" not in html and "data-plain-http" not in html
    s, _, html, _ = call(app, "GET", "/", {"Host": "10.0.0.5:8765"})   # plain http, not this computer: say HTTPS is needed
    assert "data-plain-http" in html

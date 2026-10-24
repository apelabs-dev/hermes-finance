"""Dashboard JSON API, static route, security headers, and the front-end safety rules."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import threading
from datetime import date
from pathlib import Path

import pytest

from factory import acct, bank, cap, snapshot, tx
from hermes_finance import classify, config, prefs, web
from hermes_finance.web import App, make_server

LOCAL = {"Host": "127.0.0.1:8765"}
POST = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
STATIC = Path(web.__file__).parent / "static"
LOGO = "https://plaid-merchant-logos.plaid.com/amazon_1.png"
ICON = "https://plaid-category-icons.plaid.com/PFC_GENERAL_MERCHANDISE.png"


@pytest.fixture
def dash(conn, client):
    bank(conn, "item_a", "Nova", institution_id="ins_nova")
    bank(conn, "item_b", "Wells Fargo", institution_id="ins_wf", status="login_required", error_code="ITEM_LOGIN_REQUIRED")
    bank(conn, "item_old", "Old Bank", institution_id="ins_old", removed=True)
    acct(conn, "chk", "item_a", current=2500.0, available=2400.0, mask="7385")
    acct(conn, "cc", "item_b", name="Visa", type_="credit", subtype="credit card", current=400.0, limit=2000.0, mask="7266")
    tx(conn, "2026-09-02", 71.01, "Amazon", cat="Other Shopping", account_id="chk", logo=LOGO, icon=ICON, tid="t_amz1")
    tx(conn, "2026-09-10", 23.48, "Amazon", cat="Other Shopping", account_id="chk", logo="https://evil.example/x.png",
       icon="http://plaid-category-icons.plaid.com/x.png", tid="t_amz2")
    tx(conn, "2026-09-11", 12.0, "Amazon", cat="Other Shopping", account_id="chk", tid="t_amz_ai")
    tx(conn, "2026-09-12", 8.0, "Amazon", cat="Other Shopping", account_id="chk", tid="t_amz_user")
    tx(conn, "2026-09-05", 55.0, "Chipotle", cat="Eating Out", account_id="cc", item_id="item_b", pending=True, tid="t_chip")
    tx(conn, "2026-09-06", 9.0, None, name="246810 ORBITEX L", cat="Other", account_id="chk", tid="t_other")
    tx(conn, "2026-09-07", 500.0, "To Savings - 7731", cat="Transfers", account_id="chk", tid="t_xfer")
    tx(conn, "2026-09-15", -2353.10, None, name="Electronic Deposit Orbitex", cat="Income", account_id="chk", tid="t_pay")
    tx(conn, "2026-09-16", 4.0, "100% Juice_Bar", cat="Eating Out", account_id="chk", tid="t_juice")
    tx(conn, "2026-08-20", 300.0, "Amazon", cat="Other Shopping", account_id="chk", tid="t_aug")
    for d in ("2026-06-12", "2026-07-12", "2026-08-12", "2026-09-12"):
        tx(conn, d, 15.49, "Netflix", cat="Entertainment", account_id="chk")
    cap(conn, "Other Shopping", 90)
    snapshot(conn, "chk", "2026-09-24", 2400.0)
    snapshot(conn, "chk", "2026-09-25", 2500.0)
    classify.set_category(conn, "t_amz_ai", "Groceries", "ai", 0.9)
    classify.set_category(conn, "t_amz_user", "Other Health", "user")
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    app.today = lambda: date(2026, 9, 25)
    return app


def get(app, path):
    status, ctype, body = app.handle("GET", path, LOCAL, b"")
    return status, ctype, json.loads(body) if ctype == "application/json" else body


def post(app, path, payload, headers=POST, raw=None):
    status, _, body = app.handle("POST", path, headers, raw if raw is not None else json.dumps(payload).encode())
    return status, json.loads(body)


# --- GET endpoints ---------------------------------------------------------------------------------

def test_overview_shape(dash):
    status, ctype, d = get(dash, "/api/overview?month=2026-09")
    assert status == 200 and ctype == "application/json"
    assert set(d) >= {"month", "today", "summary", "pace", "budgets", "categories", "alerts", "connections", "bills",
                      "upcoming", "cashflow", "net_worth", "recent", "last_sync"}
    assert d["month"] == "2026-09" and d["summary"]["day"] == 25
    assert d["summary"]["mtd"] == round(71.01 + 23.48 + 12 + 8 + 55 + 9 + 4 + 15.49, 2)
    assert d["budgets"]["count"] == 1 and d["budgets"]["categories"][0]["category"] == "Other Shopping"
    assert {a["key"] for a in d["alerts"]} == {"cap:Other Shopping:2026-09:over", "item:item_b:ITEM_LOGIN_REQUIRED"}
    assert d["connections"] == {"total": 2, "banks": 2, "broken": 1}   # (TASK-22f: banks = the Plaid part; no Alpaca here)
    # TASK-15: plus the Custom block the Net worth tile shows in that mode (here the first selection, "Cash & debts",
    # leaves nothing out: only bank accounts and cards)
    assert set(d["net_worth"]) == {"assets", "debts", "net", "custom"} and d["net_worth"]["net"] == 2100.0
    assert d["net_worth"]["custom"] == {"assets": d["net_worth"]["assets"], "debts": d["net_worth"]["debts"], "net": 2100.0,
                                        "excluded": []}
    assert len(d["cashflow"]["months"]) == 6 and d["cashflow"]["months"][-1]["month"] == "2026-09"
    assert [r["merchant"] for r in d["upcoming"]] == ["Netflix"]
    assert len(d["recent"]) <= 10 and all(not r["is_transfer"] for r in d["recent"])
    assert d["last_sync"] == "2026-09-25T15:11:00Z"
    assert get(dash, "/api/overview")[2]["month"] == "2026-09"          # defaults to the current month


def test_counts_budgets_recurring_categories_shapes(dash):
    status, _, c = get(dash, "/api/counts?month=2026-09")
    assert status == 200 and c == {"month": "2026-09", "transactions": 10, "review": 1, "over": 1, "warn": 0,
                                   "recurring": 1}
    status, _, b = get(dash, "/api/budgets?month=2026-09")
    assert status == 200 and set(b) >= {"categories", "budgets", "suggestions", "history"}
    assert b["history"]["months"] == ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
    assert b["history"]["series"]["Other Shopping"][-2:] == [300.0, round(71.01 + 23.48, 2)]
    assert len(b["history"]["coverage"]) == 6 and b["suggestions"]["months"] == ["2026-06", "2026-07", "2026-08"]
    status, _, r = get(dash, "/api/recurring")
    assert status == 200 and r["count"] == 1 and set(r["monthly_totals"]) == {"bill", "subscription"}
    status, _, cats = get(dash, "/api/categories")
    assert status == 200 and {"name": "Income", "is_income": True, "is_transfer": False, "spending": False,
                              "monthly_cap": None, "group": None, "icon": "arrow-down-left", "rgb": None} in cats
    assert next(x for x in cats if x["name"] == "Other Shopping")["monthly_cap"] == 90


def test_accounts_shape_and_no_tokens(dash):
    status, _, a = get(dash, "/api/accounts")
    assert status == 200
    assert (a["slots_used"], a["slot_limit"]) == (3, 10)                # removed Items still count
    assert a["connected_institution_ids"] == ["ins_nova", "ins_wf"]     # not the removed one
    assert {it["item_id"]: it["status"] for it in a["items"]} == {"item_a": "active", "item_b": "login_required",
                                                                  "item_old": "active"}
    assert all("access_token" not in it for it in a["items"])
    assert "access-production" not in json.dumps(a)
    nw = a["net_worth"]
    assert [p["date"] for p in nw["history"]] == ["2026-09-24", "2026-09-25"]
    assert nw["tracking_since"] == "2026-09-24"


@pytest.mark.parametrize("path", ["/api/overview?month=2026-13", "/api/budgets?month=nope", "/api/counts?month=2026-9",
                                  "/api/transactions?kind=bogus", "/api/transactions?limit=0",
                                  "/api/transactions?limit=abc", "/api/transactions?offset=-1",
                                  "/api/transactions?q=" + "x" * 201])
def test_bad_parameters_are_400(dash, path):
    status, _, d = get(dash, path)
    assert status == 400 and d["error"]


def test_unknown_api_path_is_404(dash):
    assert get(dash, "/api/nope")[0] == 404


# --- transactions ----------------------------------------------------------------------------------

def ids(d):
    return [r["transaction_id"] for r in d["rows"]]


def test_transactions_filters(dash):
    _, _, d = get(dash, "/api/transactions?month=2026-09")
    assert d["total"] == 10 and d["counts"] == {"all": 10, "review": 1, "spend": 8, "income": 1, "transfers": 1}
    assert ids(d)[0] == "t_juice"                                     # newest first
    _, _, d = get(dash, "/api/transactions?month=2026-09&kind=review")
    assert ids(d) == ["t_other"] and d["rows"][0]["review"] and d["rows"][0]["merchant"] == "246810 ORBITEX L"
    _, _, d = get(dash, "/api/transactions?month=2026-09&kind=income")
    assert ids(d) == ["t_pay"] and d["rows"][0]["is_income"]
    _, _, d = get(dash, "/api/transactions?month=2026-09&kind=transfers")
    assert ids(d) == ["t_xfer"] and d["rows"][0]["is_transfer"]
    _, _, d = get(dash, "/api/transactions?month=2026-09&category=Other%20Shopping")
    assert set(ids(d)) == {"t_amz1", "t_amz2"}
    _, _, d = get(dash, "/api/transactions?month=2026-09&bank=Wells%20Fargo")
    assert ids(d) == ["t_chip"] and d["rows"][0]["pending"] is True
    _, _, d = get(dash, "/api/transactions?month=2026-09&q=amaz")
    assert set(ids(d)) == {"t_amz1", "t_amz2", "t_amz_ai", "t_amz_user"}
    _, _, d = get(dash, "/api/transactions?month=2026-09&q=orbitex&kind=income")   # q matches name too
    assert ids(d) == ["t_pay"] and d["counts"]["all"] == 2
    _, _, d = get(dash, "/api/transactions?q=amazon")                              # no month = all months
    assert "t_aug" in ids(d)


def test_transactions_pagination(dash):
    pages = [get(dash, f"/api/transactions?month=2026-09&limit=4&offset={o}")[2] for o in (0, 4, 8, 12)]
    assert [len(p["rows"]) for p in pages] == [4, 4, 2, 0]
    assert all(p["total"] == 10 for p in pages)
    seen = [i for p in pages for i in ids(p)]
    assert len(seen) == len(set(seen)) == 10


def test_transaction_rows_carry_safe_avatar_data(dash):
    rows = {r["transaction_id"]: r for r in get(dash, "/api/transactions?month=2026-09&limit=100")[2]["rows"]}
    assert rows["t_amz1"]["logo_url"] == LOGO and rows["t_amz1"]["icon_url"] == ICON
    assert rows["t_amz2"]["logo_url"] is None and rows["t_amz2"]["icon_url"] is None   # foreign host / http
    assert rows["t_other"]["seed"] == "246810 ORBITEX L" and rows["t_amz1"]["seed"] == "Amazon"
    assert rows["t_amz1"]["account_mask"] == "7385" and rows["t_amz1"]["institution_name"] == "Nova"


@pytest.mark.parametrize("q", ["' OR 1=1 --", "'; DROP TABLE transactions; --", "\" OR \"\"=\"", "%%%", "__%__",
                               "\\", "x') UNION SELECT access_token FROM items --"])
def test_sql_injection_looking_q_is_harmless(dash, conn, q):
    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    status, _, d = get(dash, "/api/transactions?" + __import__("urllib.parse").parse.urlencode({"q": q}))
    assert status == 200 and d["total"] == 0 and d["rows"] == []
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before
    assert "access-production" not in json.dumps(d)


def test_like_wildcards_are_literal(dash):
    assert ids(get(dash, "/api/transactions?q=%25")[2]) == ["t_juice"]           # not "everything"
    assert ids(get(dash, "/api/transactions?q=_")[2]) == ["t_juice"]
    assert ids(get(dash, "/api/transactions?q=100%25%20J")[2]) == ["t_juice"]
    assert ids(get(dash, "/api/transactions?q=e_B")[2]) == ["t_juice"]


# --- POST /api/transactions/category ---------------------------------------------------------------

def cat_of(conn, tid):
    return tuple(conn.execute("SELECT category, category_source FROM v_transactions WHERE transaction_id=?",
                              (tid,)).fetchone())


def test_set_category_just_once(dash, conn):
    status, d = post(dash, "/api/transactions/category", {"transaction_id": "t_other", "category": "Fees"})
    assert status == 200 and d["changed"] == 1 and d["rule_id"] is None
    assert (d["category"], d["category_source"]) == ("Fees", "user")
    assert cat_of(conn, "t_other") == ("Fees", "user")
    assert conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0


def test_set_category_with_rule_keeps_precedence(dash, conn):
    status, d = post(dash, "/api/transactions/category",
                     {"transaction_id": "t_amz1", "category": "Groceries", "make_rule": True})
    assert status == 200
    rule = conn.execute("SELECT match_field, match_type, pattern, created_from FROM rules").fetchone()
    assert tuple(rule) == ("merchant_name", "exact", "Amazon", "user")
    assert d["rule"] == {"field": "merchant_name", "pattern": "Amazon"} and d["rule_id"]
    assert cat_of(conn, "t_amz1") == ("Groceries", "user")
    assert cat_of(conn, "t_amz2") == ("Groceries", "rule")          # matching past rows follow the rule
    assert cat_of(conn, "t_aug") == ("Groceries", "rule")
    assert cat_of(conn, "t_amz_ai") == ("Groceries", "rule")        # the user's rule beats an ai guess
    assert cat_of(conn, "t_amz_user") == ("Other Health", "user")         # user row untouched by the rule
    assert d["changed"] == 1 + 3                                    # this row + three rule matches
    assert cat_of(conn, "t_chip") == ("Eating Out", "plaid")        # other merchants unaffected
    # and a future sync of another Amazon row is classified by the rule
    tx(conn, "2026-09-20", 5.0, "Amazon", cat="Other Shopping", account_id="chk", tid="t_future")
    classify.apply(conn)
    assert cat_of(conn, "t_future") == ("Groceries", "rule")


def test_rule_from_name_when_no_merchant(dash, conn):
    post(dash, "/api/transactions/category", {"transaction_id": "t_other", "category": "Income", "make_rule": True})
    assert tuple(conn.execute("SELECT match_field, pattern FROM rules").fetchone()) == ("name", "246810 ORBITEX L")


@pytest.mark.parametrize("payload,status", [
    ({"transaction_id": "nope", "category": "Fees"}, 404),
    ({"transaction_id": "t_other", "category": "NotACategory"}, 400),
    ({"transaction_id": "t_other"}, 400),
    ({"category": "Fees"}, 400),
    ({"transaction_id": 5, "category": "Fees"}, 400),
    ({"transaction_id": "t_other", "category": "Fees", "make_rule": "yes"}, 400),
])
def test_set_category_validation(dash, conn, payload, status):
    got, d = post(dash, "/api/transactions/category", payload)
    assert got == status and d["error"]
    assert cat_of(conn, "t_other") == ("Other", "plaid")


# --- POST /api/budgets/cap -------------------------------------------------------------------------

def cap_of(conn, name):
    return conn.execute("SELECT monthly_cap FROM categories WHERE name=?", (name,)).fetchone()[0]


@pytest.mark.parametrize("raw", [
    b'{"category": "Nope", "monthly_cap": 100}',            # unknown category
    b'{"category": "Income", "monthly_cap": 100}',          # not a spending category
    b'{"category": "Transfers", "monthly_cap": 100}',
    b'{"category": "Groceries", "monthly_cap": -1}',
    b'{"category": "Groceries", "monthly_cap": NaN}',
    b'{"category": "Groceries", "monthly_cap": Infinity}',
    b'{"category": "Groceries", "monthly_cap": -Infinity}',
    b'{"category": "Groceries", "monthly_cap": 100000.01}',
    b'{"category": "Groceries", "monthly_cap": 1e400}',
    b'{"category": "Groceries", "monthly_cap": 99999999999999999999999}',
    b'{"category": "Groceries", "monthly_cap": 1' + b'0' * 400 + b'}',        # too big for a float
    b'{"category": "Groceries", "monthly_cap": "250"}',
    b'{"category": "Groceries", "monthly_cap": true}',
    b'{"category": "Groceries", "monthly_cap": [250]}',
    b'{"category": "Groceries"}',
    b'{"monthly_cap": 250}',
    b'{"category": "", "monthly_cap": 250}',
])
def test_cap_validation(dash, conn, raw):
    status, d = post(dash, "/api/budgets/cap", None, raw=raw)
    assert status == 400 and d["error"]
    assert cap_of(conn, "Groceries") is None and cap_of(conn, "Income") is None


def test_cap_set_edges_and_clear(dash, conn):
    for value in (0, 250.555, 100000):
        status, d = post(dash, "/api/budgets/cap", {"category": "groceries", "monthly_cap": value})
        assert status == 200 and d["category"] == "Groceries"
        assert cap_of(conn, "Groceries") == round(value, 2) == d["monthly_cap"]
    status, d = post(dash, "/api/budgets/cap", {"category": "Groceries", "monthly_cap": None})
    assert status == 200 and d == {"category": "Groceries", "monthly_cap": None}
    assert cap_of(conn, "Groceries") is None


def test_budgets_not_budgeted_reasons_and_a_manual_cap(dash, conn):
    b = get(dash, "/api/budgets?month=2026-09")[2]
    nb = {x["category"]: x for x in b["not_budgeted"]}
    assert {k: nb[k]["reason"] for k in ("Rent & Utilities", "Other", "Fees")} == {
        "Rent & Utilities": "Fixed bills. Tracked on Recurring",
        "Other": "Needs review. Categorize these first",
        "Fees": "Rare. No cap needed"}
    assert nb["Other"]["spend"] == 9.0
    assert nb["Groceries"]["reason"] == "No spending in the last 3 full months"
    assert "Other Shopping" not in nb                                          # capped
    assert not {s["category"] for s in b["suggestions"]["categories"]} & {"Rent & Utilities", "Other", "Fees"}

    status, d = post(dash, "/api/budgets/cap", {"category": "Other", "monthly_cap": 150})
    assert status == 200 and d == {"category": "Other", "monthly_cap": 150.0} and cap_of(conn, "Other") == 150.0
    b = get(dash, "/api/budgets?month=2026-09")[2]
    assert "Other" not in {x["category"] for x in b["not_budgeted"]}
    assert next(x for x in b["budgets"]["categories"] if x["category"] == "Other")["cap"] == 150.0


def test_writes_keep_the_csrf_gate(dash, conn):
    evil = {**POST, "Origin": "https://evil.example"}
    assert post(dash, "/api/budgets/cap", {"category": "Groceries", "monthly_cap": 5}, headers=evil)[0] == 403
    assert post(dash, "/api/transactions/category", {"transaction_id": "t_other", "category": "Fees"}, headers=evil)[0] == 403
    plain = {**POST, "Content-Type": "text/plain"}
    assert post(dash, "/api/budgets/cap", {"category": "Groceries", "monthly_cap": 5}, headers=plain)[0] == 415
    assert cap_of(conn, "Groceries") is None and cat_of(conn, "t_other") == ("Other", "plaid")


# --- static route ----------------------------------------------------------------------------------

@pytest.mark.parametrize("name,ctype", [("app.js", "text/javascript; charset=utf-8"),
                                        ("app.css", "text/css; charset=utf-8"),
                                        ("vendor/chart.umd.min.js", "text/javascript; charset=utf-8"),
                                        ("fonts/InterVariable.woff2", "font/woff2"),
                                        ("fonts/Inter-LICENSE.txt", "text/plain; charset=utf-8"),
                                        ("manifest.webmanifest", "application/manifest+json"),
                                        ("icons/icon.svg", "image/svg+xml"),
                                        ("icons/favicon.svg", "image/svg+xml"),
                                        ("icons/favicon.ico", "image/x-icon"),
                                        ("icons/favicon-16.png", "image/png"),
                                        ("icons/favicon-32.png", "image/png"),
                                        ("icons/apple-touch-icon.png", "image/png"),
                                        ("icons/icon-192.png", "image/png"),
                                        ("icons/icon-512.png", "image/png"),
                                        ("icons/icon-maskable-512.png", "image/png")])
def test_static_allowlist(dash, name, ctype):
    status, got_type, body = dash.handle("GET", f"/static/{name}", LOCAL, b"")
    assert status == 200 and got_type == ctype
    assert body == (STATIC / name).read_bytes()


def test_every_static_file_is_on_the_list_and_nothing_else_is_served():
    on_disk = {str(f.relative_to(STATIC)) for f in STATIC.rglob("*") if f.is_file()}
    assert set(web.STATIC_FILES) <= on_disk
    assert on_disk - set(web.STATIC_FILES) == {"vendor/chart.js.LICENSE.md"}   # the one file left off on purpose


def png_size(blob):
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    return int.from_bytes(blob[16:20], "big"), int.from_bytes(blob[20:24], "big")


def test_icon_files_are_what_they_say():
    for name, size in (("favicon-16.png", 16), ("favicon-32.png", 32), ("apple-touch-icon.png", 180),
                       ("icon-192.png", 192), ("icon-512.png", 512), ("icon-maskable-512.png", 512)):
        assert png_size((STATIC / "icons" / name).read_bytes()) == (size, size), name
    ico = (STATIC / "icons" / "favicon.ico").read_bytes()
    assert ico[:4] == b"\x00\x00\x01\x00" and int.from_bytes(ico[4:6], "little") == 2       # 16 + 32
    assert {ico[6], ico[22]} == {16, 32}
    for name in ("icon.svg", "favicon.svg"):
        svg = (STATIC / "icons" / name).read_text()
        assert svg.startswith("<svg") and "Gradient" not in svg and "<text" not in svg and "href" not in svg
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text())
    assert (manifest["name"], manifest["short_name"], manifest["display"]) == ("Hermes Finances", "Finances", "standalone")
    assert manifest["theme_color"] == manifest["background_color"] == "#0c0e13"
    for icon in manifest["icons"]:
        assert icon["src"].startswith("/static/icons/") and icon["src"][len("/static/"):] in web.STATIC_FILES
    assert {"maskable"} == {i["purpose"] for i in manifest["icons"] if "maskable" in i["src"]}
    assert (STATIC / "fonts" / "InterVariable.woff2").read_bytes()[:4] == b"wOF2"
    assert "SIL Open Font License" in (STATIC / "fonts" / "Inter-LICENSE.txt").read_text()


def test_favicon_and_touch_icon_at_the_root(dash):
    status, ctype, body = dash.handle("GET", "/favicon.ico", LOCAL, b"")
    assert status == 200 and ctype == "image/x-icon" and body == (STATIC / "icons" / "favicon.ico").read_bytes()
    status, ctype, body = dash.handle("GET", "/apple-touch-icon.png", LOCAL, b"")
    assert status == 200 and ctype == "image/png" and png_size(body) == (180, 180)
    for path in ("/favicon.png", "/manifest.webmanifest", "/icon.svg", "/favicon.ico/", "/favicon.ico/../web.py"):
        assert dash.handle("GET", path, LOCAL, b"")[0] == 404


@pytest.mark.parametrize("path", ["/static/../web.py", "/static/../../pyproject.toml", "/static/%2e%2e/web.py",
                                  "/static/..%2fweb.py", "/static/vendor/../app.js", "/static/./app.js",
                                  "/static//app.js", "/static/APP.JS", "/static/app.js/", "/static/",
                                  "/static/unknown.js", "/static/vendor/chart.js.LICENSE.md",
                                  "/static/vendor/chart.umd.min.js.map", "/static/app.js%00.png",
                                  "/static/%2Fetc%2Fpasswd", "/static/\\..\\web.py",
                                  "/static/fonts/", "/static/fonts/../app.js", "/static/fonts/InterVariable.woff",
                                  "/static/fonts/../../web.py", "/static/icons/", "/static/icons/nope.png",
                                  "/static/icons/../manifest.webmanifest", "/static/ICONS/icon.svg",
                                  "/static/icons/icon.svg/", "/static/icons//icon.svg", "/static/icons/%2e%2e/app.js",
                                  "/static/manifest.json", "/static/fonts/InterVariable.woff2%00"])
def test_static_traversal_and_unknown_are_404(dash, path):
    status, ctype, _ = dash.handle("GET", path, LOCAL, b"")
    assert status == 404 and ctype == "application/json"


def test_static_goes_through_the_gate(conn, client):
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765, ["https://box.tail.ts.net:8765"], ["owner@example.com"])
    assert app.handle("GET", "/static/app.js", {"Host": "evil.com"}, b"")[0] == 403
    assert app.handle("GET", "/static/app.js", {"Host": "box.tail.ts.net:8765"}, b"")[0] == 403   # no identity
    assert app.handle("GET", "/static/app.js", {**LOCAL, "X-Forwarded-For": "100.64.0.1"}, b"")[0] == 403
    ok = {"Host": "box.tail.ts.net:8765", "Tailscale-User-Login": "owner@example.com"}
    assert app.handle("GET", "/static/app.js", ok, b"")[0] == 200
    for path in ("/api/overview", "/api/transactions", "/api/accounts", "/api/budgets"):
        assert app.handle("GET", path, {"Host": "evil.com"}, b"")[0] == 403
        assert app.handle("GET", path, {"Host": "box.tail.ts.net:8765"}, b"")[0] == 403


# --- headers over a real socket --------------------------------------------------------------------

@pytest.fixture
def server(dash):
    srv = make_server(dash, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


@pytest.mark.parametrize("method,path,host,ctype", [
    ("GET", "/", "ok", "text/html; charset=utf-8"),
    ("GET", "/api/overview", "ok", "application/json"),
    ("GET", "/api/status", "ok", "application/json"),
    ("GET", "/static/app.js", "ok", "text/javascript; charset=utf-8"),
    ("GET", "/static/app.css", "ok", "text/css; charset=utf-8"),
    ("GET", "/static/vendor/chart.umd.min.js", "ok", "text/javascript; charset=utf-8"),
    ("GET", "/static/../web.py", "ok", "application/json"),
    ("GET", "/", "evil.example", "application/json"),
    ("POST", "/api/budgets/cap", "ok", "application/json"),
])
def test_security_headers_on_every_response(server, method, path, host, ctype):
    c = http.client.HTTPConnection("127.0.0.1", server, timeout=10)
    headers = {"Host": f"127.0.0.1:{server}" if host == "ok" else host}
    body = None
    if method == "POST":
        headers["Content-Type"] = "application/json"
        body = json.dumps({"category": "Groceries", "monthly_cap": 10})
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    r.read()
    assert r.getheader("Content-Type") == ctype
    assert r.getheader("Content-Security-Policy") == web.CSP
    assert r.getheader("Referrer-Policy") == "no-referrer"
    assert r.getheader("X-Content-Type-Options") == "nosniff"


def test_csp_directives():
    d = dict(part.strip().split(" ", 1) for part in web.CSP.split(";"))
    assert d["default-src"] == "'self' https://cdn.plaid.com"
    assert d["script-src"] == "'self' https://cdn.plaid.com"                   # no 'unsafe-inline' / eval
    assert d["frame-src"] == "https://cdn.plaid.com"
    assert d["connect-src"] == "'self' https://*.plaid.com"
    assert d["img-src"] == "'self' data: https://plaid-merchant-logos.plaid.com https://plaid-category-icons.plaid.com"
    assert d["style-src"] == "'self' 'unsafe-inline'"
    assert d["font-src"] == "'self'" and d["manifest-src"] == "'self'"           # Inter is self-hosted
    assert d["base-uri"] == "'none'" and d["frame-ancestors"] == "'none'"
    hosts = {t for v in d.values() for t in v.split() if "." in t}
    assert hosts == {"https://cdn.plaid.com", "https://*.plaid.com", "https://plaid-merchant-logos.plaid.com",
                     "https://plaid-category-icons.plaid.com"}                  # no font CDN, nothing new


# --- the page and its script -----------------------------------------------------------------------

def test_shell_has_no_inline_script_or_handlers(dash):
    html = dash.handle("GET", "/", LOCAL, b"")[2].decode()
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.S)
    assert scripts and all('src="' in attrs and not body.strip() for attrs, body in scripts)
    assert not re.search(r"\son[a-z]+\s*=", html)           # no onclick= etc. (the CSP would block them)
    assert "javascript:" not in html
    for key in ("overview", "transactions", "budgets", "recurring", "accounts", "investments", "debt"):
        assert f'href="#/{key}"' in html
    assert 'id="slots">slots used: <b>3 / 10</b>' in html and "Add account" in html


def test_shell_nav_tabs_icons_and_add_account(dash):
    html = dash.handle("GET", "/", LOCAL, b"")[2].decode()
    nav = re.search(r'<nav class="snav".*?</nav>', html, re.S).group(0)
    assert re.findall(r'data-pg="(\w+)"', nav) == ["overview", "transactions", "budgets", "goals", "recurring", "accounts",
                                                   "investments", "debt"]  # TASK-19: Goals, under Budgets
    assert "Loans &amp; debt" in nav
    tabs = re.search(r'<nav class="tabbar".*?</nav>', html, re.S).group(0)
    # TASK-18 addendum: four tabs on a phone, HOTFIX-1: and Settings (the gear) the fifth; Recurring, Investments, Loans &
    # debt and Goals are in the drawer
    assert web.TABS == ("overview", "transactions", "budgets", "accounts", "settings")
    assert re.findall(r'data-pg="(\w+)"', tabs) == ["overview", "transactions", "budgets", "accounts", "settings"]
    assert re.findall(r"<span>([^<]+)</span></a>", tabs) == ["Overview", "Transactions", "Budgets", "Accounts", "Settings"]
    assert tabs.count('<svg') == 5 and 'data-pg="settings"><svg' in tabs
    # Settings: the gear at the bottom of the sidebar (in the phone drawer)
    foot = re.search(r'<nav class="snav sfoot".*?</nav>', html, re.S).group(0)
    assert re.findall(r'data-pg="(\w+)"', foot) == ["settings"] and ">Settings<" in foot
    assert 'data-add="home"' in html and "Connect a bank" not in html
    for tag in ('<link rel="icon" href="/static/icons/favicon.svg" type="image/svg+xml">',
                '<link rel="icon" href="/static/icons/favicon-32.png" sizes="32x32" type="image/png">',
                '<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">',
                '<link rel="manifest" href="/static/manifest.webmanifest">',
                '<meta name="theme-color" content="#0c0e13">',
                '<meta name="apple-mobile-web-app-title" content="Finances">',
                '<link rel="preload" href="/static/fonts/InterVariable.woff2" as="font" type="font/woff2" crossorigin>'):
        assert tag in html, tag
    for href in re.findall(r'<link [^>]*href="(/static/[^"]+)"', html):
        assert href[len("/static/"):] in web.STATIC_FILES, href


def served_text(app):
    html = app.handle("GET", "/", LOCAL, b"")[2].decode()
    return html + "".join(app.handle("GET", f"/static/{n}", LOCAL, b"")[2].decode()
                          for n in ("app.js", "app.css", "manifest.webmanifest"))


def test_no_zenith_apple_card_or_coming_hints_anywhere(dash):
    text = served_text(dash)
    assert "Add account" in text and "Loans &amp; debt" in text                     # non-vacuous: this is the page
    for needle in ("Zenith", "Apple Card", "Wallet CSV", "CSV import", "not connected", "Not connected",
                   "needs loan mode"):
        assert needle not in text, needle
    assert not re.search(r"\bcoming\b(?! up)", text, re.I)          # "Coming up" (the schedule cards) is fine
    assert "soon" not in re.findall(r'class="([^"]*)"', text)


def test_no_new_chips_and_no_glow(dash):
    text = served_text(dash)
    assert not re.search(r"""['">]New['"<]""", text)                # no "New" pill or nav counter
    css = (STATIC / "app.css").read_text()
    js = (STATIC / "app.js").read_text()
    assert ".glow" not in css and "radial-gradient" not in css and "createRadialGradient" not in js
    assert "C.defaults.animation = false" in js and ":hover { transform" not in css.replace("\n", " ")


def test_budgets_ui_keeps_suggestions_quiet():
    """The page's side of the contract: the hero and "Use all" only when the payload says so (no caps), the summary
    shows YOUR budgets otherwise, and suggestion buttons are never the highlighted kind.

    2026-10-02: the small "See suggestions" link is GONE (the owner's call). It revealed a `Suggested $X · Use` line that
    moved into each no-budget row's own Details panel, so it had nothing left to reveal. The quietness this test protects
    is unchanged: a capped card still never shows a suggestion, and the suggestion still lives in the editor.
    """
    js = (STATIC / "app.js").read_text()
    body = re.search(r"\n  function drawBudgets\(.*?\n  }\n", js, re.S).group(0)
    hero, rest = body.split("} else {", 1)
    assert "if (d.suggestion_hero) {" in hero and "Use all" in hero and "Suggested budgets" in hero
    assert "Use all" not in rest and "Suggested budgets" not in rest and "Your budgets" in rest
    # the disclosure and its `.sg` line are both gone from the source; the suggestion is per-row now.
    # Match the CODE, not the prose: the comment above `notBudgetedRow` still names the button, so a bare substring
    # search over the function body would pass on a comment and fail on a real regression.
    code = re.sub(r"//[^\n]*|/\*.*?\*/", "", rest, flags=re.S)
    assert "See suggestions" not in code, "the dead 'See suggestions' link must not come back"
    assert "withSug" not in code, "...nor the branch that built it"
    bjs = (STATIC / "budgets.js").read_text()   # P2A: the rows moved from app.js's budgetCard / notBudgetedRow to budgets.js
    # no suggestion button is the highlighted kind (a `td.p` is the Planned column here, so look at the buttons)
    assert not re.search(r"h\('button', \{[^}]*class: '(p|cbtn|pill)\b", bjs)
    row = re.search(r"\n  function noBudgetRow\(.*?\n  }\n", bjs, re.S).group(0)
    row_code = re.sub(r"//[^\n]*|/\*.*?\*/", "", row, flags=re.S)   # the comments name `.sg`; the code must not contain it
    assert "class: 'sg'" not in row_code, "the inline suggestion line is gone; it lives in the row's Details panel"
    assert "budget-suggestion" in row, "...and the row's Details panel carries it instead"


def test_app_js_never_writes_html():
    js = (STATIC / "app.js").read_text()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert forbidden not in js, forbidden
    assert "textContent" in js and "createElement" in js


def test_app_js_only_accepts_allowed_image_hosts():
    js = (STATIC / "app.js").read_text()
    assert "url.protocol === 'https:'" in js
    assert "new Set(['plaid-merchant-logos.plaid.com', 'plaid-category-icons.plaid.com'])" in js
    assert "addEventListener('error'" in js and "onerror" not in js


def test_vendored_chart_js_matches_readme():
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    version, sha = re.search(r"Chart\.js v(\d+\.\d+\.\d+).*?sha256 `([0-9a-f]{64})`", readme, re.S).groups()
    blob = (STATIC / "vendor" / "chart.umd.min.js").read_bytes()
    assert hashlib.sha256(blob).hexdigest() == sha
    assert blob.startswith(f"/*!\n * Chart.js v{version}\n".encode())
    assert (STATIC / "vendor" / "chart.js.LICENSE.md").read_text().startswith("The MIT License")


# --- suggestions stay quiet once a cap exists ---------------------------------------------------------

def test_capped_categories_carry_no_suggestion_and_no_hero(dash, conn):
    b = get(dash, "/api/budgets?month=2026-09")[2]
    assert b["budgets"]["count"] == 1 and b["suggestion_hero"] is False       # Other Shopping has a cap
    sug = {s["category"]: s for s in b["suggestions"]["categories"]}
    assert "Other Shopping" not in sug and all(s["cap"] is None for s in sug.values())
    assert sug["Entertainment"]["suggested"] == 25.0                          # uncapped: still offered (quietly)
    assert b["suggestions"]["count"] == 1 and b["suggestions"]["total"] == 25.0
    post(dash, "/api/budgets/cap", {"category": "Entertainment", "monthly_cap": 20})
    b = get(dash, "/api/budgets?month=2026-09")[2]
    assert "Entertainment" not in {s["category"] for s in b["suggestions"]["categories"]}
    assert b["suggestion_hero"] is False and b["suggestions"]["count"] == 0


def test_the_suggestion_hero_only_without_any_cap(dash, conn):
    post(dash, "/api/budgets/cap", {"category": "Other Shopping", "monthly_cap": None})
    b = get(dash, "/api/budgets?month=2026-09")[2]
    assert b["budgets"]["count"] == 0 and b["suggestion_hero"] is True
    assert [s["category"] for s in b["suggestions"]["categories"] if s["suggested"]] == ["Entertainment"]
    post(dash, "/api/budgets/cap", {"category": "Other Health", "monthly_cap": 50})   # any one cap ends the hero
    assert get(dash, "/api/budgets?month=2026-09")[2]["suggestion_hero"] is False


def test_edit_form_sends_only_changed_fields_and_refetches():
    """A stale balance on the page must never be posted back: an edit sends only the fields the user
    changed, prefills from a fresh /api/manual, and a write drops the cached debt data."""
    js = (STATIC / "app.js").read_text()
    form = re.search(r"\n  function addForm\(.*?\n  }\n", js, re.S).group(0)
    assert "else if (!edit || !sameValue(f, value, existing[f.key])) {" in form
    assert "await api('/api/manual')" in re.search(r"\n  async function editManual\(.*?\n  }\n", js, re.S).group(0)
    assert "S.debt = null;" in re.search(r"\n  function refresh\(.*?\n  }\n", js, re.S).group(0)


# --- TASK-10: the browser note is gone; the editor gets a suggestion for capped categories too ---------

def test_the_browser_note_is_gone_everywhere(dash):
    """"Open this page in Safari or Chrome, not inside Discord..." (and its shorter copies in the Plaid step and
    the Bank connections card) is removed from every served page and script; the Plaid pop-up handling stays."""
    tailnet = App(config.db_path(), dash.client_factory, "127.0.0.1", 8765, ["https://box.tail.ts.net:8765"],
                  ["owner@example.com"])
    ts = {"Host": "box.tail.ts.net:8765", "Tailscale-User-Login": "owner@example.com"}
    texts = [served_text(dash), tailnet.handle("GET", "/", ts, b"")[2].decode()]
    assert "Add account" in texts[1]                                          # non-vacuous: the tailnet page
    for text in texts:
        for needle in ("Safari", "Discord", "built-in browser", "allow pop-ups", "other apps", "tailnet-note",
                       "Open this page", "Open this in"):
            assert needle not in text, needle
    assert not hasattr(web, "TAILNET_NOTE")
    js = (STATIC / "app.js").read_text()
    assert "Plaid.create({" in js and "handler.open();" in js and "onExit:" in js   # Link itself is unchanged


def test_budgets_give_the_editor_a_suggestion_for_capped_categories_too(dash, conn):
    """categories[].suggested is the editor's "Suggested $X · Use" value: set for capped and uncapped categories
    alike, null when there's no usable suggestion. The visible suggestion list stays uncapped-only."""
    tx(conn, "2026-06-15", 80.0, "Target", cat="Other Shopping", account_id="chk")
    tx(conn, "2026-07-15", 120.0, "Target", cat="Other Shopping", account_id="chk")  # Other Shopping: [80, 120, 300]
    b = get(dash, "/api/budgets?month=2026-09")[2]
    cats = {c["category"]: c for c in b["categories"]}
    other_shop = cats["Other Shopping"]
    assert other_shop["cap"] == 90.0 and other_shop["suggested"] == 125.0     # median 120, rounded up
    assert cats["Entertainment"]["cap"] is None and cats["Entertainment"]["suggested"] == 25.0
    sug = {s["category"]: s for s in b["suggestions"]["categories"]}
    assert "Other Shopping" not in sug and sug["Entertainment"]["suggested"] == 25.0  # the quiet list: uncapped only
    assert b["suggestions"]["count"] == 1 and b["suggestion_hero"] is False
    for name in ("Rent & Utilities", "Other", "Fees", "Groceries", "Other Health", "Travel"):  # excluded or unspent
        assert cats[name]["suggested"] is None, name
    post(dash, "/api/budgets/cap", {"category": "Other", "monthly_cap": 150})
    post(dash, "/api/budgets/cap", {"category": "Entertainment", "monthly_cap": 20})
    cats = {c["category"]: c for c in get(dash, "/api/budgets?month=2026-09")[2]["categories"]}
    assert cats["Other"]["cap"] == 150.0 and cats["Other"]["suggested"] is None          # capped, still excluded
    assert cats["Entertainment"]["cap"] == 20.0 and cats["Entertainment"]["suggested"] == 25.0
    overview = get(dash, "/api/overview?month=2026-09")[2]
    assert all("suggested" not in c for c in overview["categories"])                     # budgets page only


def test_cap_editor_offers_the_suggestion_quietly():
    """The editor shows "Suggested $X · Use" as plain text and a plain button (no accent class), and a capped
    card still shows no suggestion row until the editor is open."""
    js = (STATIC / "app.js").read_text()
    editor = re.search(r"\n  function capEditor\(.*?\n  }\n", js, re.S).group(0)
    assert "'Suggested '" in editor and "'Use'" in editor
    use_btn = re.search(r"h\('button', \{[^}]*\}, 'Use'\)", editor).group(0)
    assert "class: 'p'" not in use_btn and "cbtn" not in use_btn and "pill" not in use_btn
    # P2A: the row is budgets.js's categoryRow; Planned is the edit button and the editor opens in a row under it
    bjs = (STATIC / "budgets.js").read_text()
    row = re.search(r"\n  function categoryRow\(.*?\n  }\n", bjs, re.S).group(0)
    opener = re.search(r"\n  function openEditor\(.*?\n  }\n", bjs, re.S).group(0)
    assert "HOOKS.budgetLabel(c)" in row            # the rollover label is still read (for the button's tooltip)
    assert "'Bills due details'" not in row         # the disclosure is just `Details` now
    assert "details(c, d, f)" in row                # the rollover and the bills go to the Details panel
    assert "c.suggested" not in row                 # capped rows: no suggestion line
    assert "c.suggested" in opener                  # ... it goes to the editor instead


# --- TASK-11 (f): categories hidden from the "Where it went" chart (server-side, shared by every device) -------------

def test_chart_exclusions_round_trip_and_persist(dash, conn):
    assert get(dash, "/api/settings")[2] == {"chart_excluded_categories": [],
                                             "fixed_categories": ["Rent & Utilities", "Subscriptions", "Internet & Phone"],
                                             "networth_excluded": [],   # TASK-15: Custom leaves nothing out here
                                             "mcp": {"allow_writes": False, "last_used": None, "pending": 0},  # TASK-17
                                             "prefs": prefs.public(conn)}   # TASK-18: the Settings page's values
    status, d = post(dash, "/api/settings", {"chart_excluded_categories": ["rent & utilities", "Travel", "Travel"]})
    assert status == 200 and d["chart_excluded_categories"] == ["Rent & Utilities", "Travel"]   # canonical, once each
    # persisted server-side: a new App on the same DB (another device, a restart) reads the same list
    again = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    again.today = lambda: date(2026, 9, 25)
    assert get(again, "/api/settings")[2]["chart_excluded_categories"] == ["Rent & Utilities", "Travel"]
    ov = get(again, "/api/overview?month=2026-09")[2]
    assert ov["chart"] == {"excluded": ["Rent & Utilities", "Travel"],
                           "fixed_categories": ["Rent & Utilities", "Subscriptions", "Internet & Phone"]}
    # Travel has no spending this month: it stays hidden, this month and next
    assert get(again, "/api/overview?month=2026-10")[2]["chart"]["excluded"] == ["Rent & Utilities", "Travel"]
    assert post(dash, "/api/settings", {"chart_excluded_categories": []})[1]["chart_excluded_categories"] == []
    assert json.loads(conn.execute("SELECT value FROM settings WHERE key = 'chart_excluded_categories'").fetchone()[0]) == []


@pytest.mark.parametrize("payload,why", [
    ({}, "required"), ({"chart_excluded_categories": "Other Shopping"}, "list"),
    ({"chart_excluded_categories": [3]}, "list"),
    ({"chart_excluded_categories": ["Income"]}, "not a spending category"),
    ({"chart_excluded_categories": ["Transfers"]}, "not a spending category"),
    ({"chart_excluded_categories": ["Nope"]}, "not a spending category"),
    ({"chart_excluded_categories": ["Other Shopping"] * 51}, "at most"),
    ({"chart_excluded_categories": [], "theme": "light"}, "unknown setting")])
def test_chart_exclusions_are_validated(dash, conn, payload, why):
    status, d = post(dash, "/api/settings", payload)
    assert status == 400 and why in d["error"], d
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


def test_chart_exclusions_keep_the_csrf_gate(dash, conn):
    evil = {**POST, "Origin": "https://evil.example"}
    assert post(dash, "/api/settings", {"chart_excluded_categories": ["Other Shopping"]}, headers=evil)[0] == 403
    plain = {**POST, "Content-Type": "text/plain"}
    assert post(dash, "/api/settings", {"chart_excluded_categories": ["Other Shopping"]}, headers=plain)[0] == 415
    tail = {"Host": "evil.example:8765", "Content-Type": "application/json"}
    assert post(dash, "/api/settings", {"chart_excluded_categories": ["Other Shopping"]}, headers=tail)[0] == 403
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


def test_chart_exclusions_change_no_total(dash, conn, capsys, monkeypatch):
    """Only the chart reads the setting: the Overview total, categories, budgets, alerts, the pace, cash flow and the
    read commands (finance spend / budget-status) are the same with categories hidden."""
    from hermes_finance import cli
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 9, 25))

    def snapshot_all():
        ov = get(dash, "/api/overview?month=2026-09")[2]
        ov.pop("chart")
        code = cli.main(["spend", "--month", "2026-09", "--json"])
        spend = json.loads(capsys.readouterr().out)
        cli.main(["budget-status", "--json"])
        status = json.loads(capsys.readouterr().out)
        return code, ov, spend, status, get(dash, "/api/budgets?month=2026-09")[2], get(dash, "/api/counts")[2]

    before = snapshot_all()
    saved = {"chart_excluded_categories": ["Other Shopping", "Eating Out", "Other"]}
    assert post(dash, "/api/settings", saved)[0] == 200
    after = snapshot_all()
    assert after == before
    assert before[2]["total"] == before[1]["summary"]["mtd"]

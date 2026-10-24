"""TASK-18: importing a file into a manual account (importer.py + /api/import/*): new accounts or existing manual ones
(never a Plaid account), duplicates skipped by FITID or by (date, amount, normalized description) one for one, rows
with source 'import' going through the same rules and classification, the size limit, and no total moving until
rows are imported."""

from __future__ import annotations

import base64
import json
from datetime import date
from pathlib import Path

import pytest

from hermes_finance import api, classify, config, importer, manual, web
from hermes_finance.web import App

import demo_data

FIX = Path(__file__).parent / "fixtures" / "import"
TODAY = date(2026, 9, 28)
H = {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}


@pytest.fixture
def app(conn):
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


def post(app, path, body):
    s, _, out = app.handle("POST", path, H, json.dumps(body).encode())
    return s, json.loads(out)


def file(name):
    raw = (FIX / name).read_bytes()
    return raw, base64.b64encode(raw).decode()


def test_preview_changes_nothing_and_commit_imports_every_row(app, conn):
    raw, data = file("everyday-export.csv")
    before = conn.total_changes
    s, p = post(app, "/api/import/preview", {"filename": "everyday-export.csv", "data": data})
    assert s == 200 and p["kind"] == "csv" and p["rows"] == 37 and conn.total_changes == before
    assert p["headers"] == ["Posted Date", "Description", "Amount", "Account"] and len(p["sample"]) == 5
    assert p["date_format"] == "%m/%d/%Y" and p["sign"] == "minus_out"
    assert {g["label"]: (g["rows"], g["new"], g["skipped"]) for g in p["groups"]} == {"Everyday Checking": (20, 20, 0), "Rewards Card": (17, 17, 0)}
    targets = {"Everyday Checking": {"new": {"name": "Everyday Checking", "kind": "bank"}},
               "Rewards Card": {"new": {"name": "Rewards Card", "kind": "credit_card", "credit_limit": 5000}}}
    s, r = post(app, "/api/import/commit", {"filename": "everyday-export.csv", "data": data, "targets": targets})
    assert s == 200 and (r["new"], r["skipped"]) == (37, 0)
    ids = {a["label"]: a["account"]["id"] for a in r["accounts"]}
    kinds = {m["name"]: m["kind"] for m in manual.summary(conn, TODAY)["accounts"]}
    assert kinds == {"Everyday Checking": "bank", "Rewards Card": "credit_card"}
    rows = conn.execute("SELECT account_id, item_id, source, import_id, fitid FROM transactions").fetchall()
    assert len(rows) == 37 and all(x["item_id"] == "import" and x["source"] == "import" and x["import_id"] for x in rows)
    assert {x["account_id"] for x in rows} == {f"manual:{i}" for i in ids.values()}
    # no Plaid slot: no items row
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    # the rows show under their manual account's name, in the app's sign
    v = conn.execute("SELECT account_name, amount FROM v_transactions WHERE name LIKE 'PAYROLL%'").fetchone()
    assert v["account_name"] == "Everyday Checking" and v["amount"] < 0
    imp = conn.execute("SELECT * FROM imports ORDER BY id").fetchall()
    assert [(x["kind"], x["rows_new"], x["rows_skipped"]) for x in imp] == [("csv", 20, 0), ("csv", 17, 0)]


def test_the_same_file_again_imports_nothing(app, conn):
    for name in ("everyday-export.csv", "checking.ofx"):
        raw, data = file(name)
        s, p = post(app, "/api/import/preview", {"filename": name, "data": data})
        targets = {g["label"]: g["target"] for g in p["groups"]}
        assert post(app, "/api/import/commit", {"filename": name, "data": data, "targets": targets})[0] == 200
        s, again = post(app, "/api/import/preview", {"filename": name, "data": data})
        assert s == 200 and again["new"] == 0 and again["skipped"] == again["rows"]
        # it goes to the same account by name, and committing adds nothing
        s, r = post(app, "/api/import/commit", {"filename": name, "data": data})
        assert s == 200 and r["new"] == 0


def test_duplicates_match_one_for_one_and_by_fitid(conn):
    m = manual.create(conn, {"kind": "bank", "name": "Cash", "institution": "Mine", "subtype": "checking", "balance": 0}, TODAY)
    Row = importer.importfile.Row
    two = [Row("2026-09-03", "BLUE BOTTLE", 4.5, None, None, 1), Row("2026-09-03", "Blue  Bottle!", 4.5, None, None, 2)]
    new, skipped = importer._split(conn, m["id"], two)
    assert (len(new), skipped) == (2, 0)                                   # two coffees in the file: both new
    conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, first_seen_at, updated_at, "
                 "source) VALUES ('imp_x', ?, 'import', '2026-09-03', 'blue bottle', 4.5, 'x', 'x', 'import')", (f"manual:{m['id']}",))
    new, skipped = importer._split(conn, m["id"], two)
    assert (len(new), skipped) == (1, 1)                                   # one already saved: one of the two is new
    conn.execute("UPDATE transactions SET fitid = 'F1' WHERE transaction_id = 'imp_x'")
    renamed = [Row("2026-09-04", "COFFEE SHOP", 5.0, None, "F1", 1), Row("2026-09-03", "BLUE BOTTLE", 4.5, None, None, 2)]
    new, skipped = importer._split(conn, m["id"], renamed)
    assert (len(new), skipped) == (0, 2)                                   # the same FITID is the same row


def test_imports_never_touch_plaid_accounts(app, conn):
    demo_data.build(conn, TODAY)
    plaid_before = conn.execute("SELECT COUNT(*), SUM(amount) FROM transactions WHERE source = 'plaid'").fetchone()[:]
    raw, data = file("checking.ofx")
    # there's no way to name a Plaid account as the target: only manual ids are accepted
    plaid_id = conn.execute("SELECT account_id FROM accounts LIMIT 1").fetchone()[0]
    for bad in ({"account_id": plaid_id}, {"account_id": 999}, {"new": {"name": "x", "kind": "loan"}}, "x"):
        s, e = post(app, "/api/import/commit", {"filename": "c.ofx", "data": data, "targets": {"Checking ••1234": bad}})
        assert s == 400 and e["field"] == "targets", bad
    assert post(app, "/api/import/commit", {"filename": "c.ofx", "data": data})[0] == 200
    assert conn.execute("SELECT COUNT(*), SUM(amount) FROM transactions WHERE source = 'plaid'").fetchone()[:] == plaid_before


def test_imported_rows_go_through_the_rules(app, conn):
    raw, data = file("everyday-export.csv")
    classify.add_rule(conn, "name", "NETFLIX.COM", "Subscriptions", "exact", created_from="user")
    assert post(app, "/api/import/commit", {"filename": "e.csv", "data": data, "targets": {
        "Rewards Card": {"new": {"name": "Rewards Card", "kind": "credit_card", "credit_limit": 1000}}}})[0] == 200
    r = conn.execute("SELECT category, category_source FROM v_transactions WHERE name = 'NETFLIX.COM'").fetchone()
    assert (r["category"], r["category_source"]) == ("Subscriptions", "rule")
    # what no rule files waits in Needs review and the AI queue, like a synced row with no Plaid category
    other = conn.execute(f"SELECT COUNT(*) FROM v_transactions v WHERE v.source = 'import' AND {api.REVIEW_SQL}").fetchone()[0]
    assert other > 0


def test_no_total_moves_until_rows_are_imported_and_then_only_spending(app, conn):
    demo_data.build(conn, TODAY)
    before = {k: api.GET_ROUTES[k](conn, {"month": "2026-09"}, TODAY) for k in ("/api/overview", "/api/accounts")}
    raw, data = file("split-columns.csv")
    s, p = post(app, "/api/import/preview", {"filename": "s.csv", "data": data})
    after = {k: api.GET_ROUTES[k](conn, {"month": "2026-09"}, TODAY) for k in ("/api/overview", "/api/accounts")}
    assert after["/api/overview"]["summary"] == before["/api/overview"]["summary"]
    assert after["/api/accounts"]["net_worth"]["net"] == before["/api/accounts"]["net_worth"]["net"]
    # a new account with no balance given: net worth stays; imported spending shows up in the month's spending
    assert post(app, "/api/import/commit", {"filename": "s.csv", "data": data, "targets": {
        g["label"]: {"new": {"name": g["label"], "kind": "bank", "balance": 0}} for g in p["groups"]}})[0] == 200
    later = api.accounts(conn, {}, TODAY)
    assert later["net_worth"]["net"] == before["/api/accounts"]["net_worth"]["net"]


def test_a_card_needs_its_limit_and_errors_are_plain(app, conn):
    raw, data = file("card.qfx")
    s, p = post(app, "/api/import/preview", {"filename": "card.qfx", "data": data})
    label = p["groups"][0]["label"]
    assert p["groups"][0]["kind"] == "credit_card" and p["groups"][0]["balance"] > 0
    s, e = post(app, "/api/import/commit", {"filename": "card.qfx", "data": data})
    assert s == 400 and e["field"] == "credit_limit" and conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    s, r = post(app, "/api/import/commit", {"filename": "card.qfx", "data": data, "targets": {label: {"new": {
        "name": "Travel card", "kind": "credit_card", "credit_limit": 3000}}}})
    assert s == 200
    card = next(m for m in manual.summary(conn, TODAY)["accounts"] if m["name"] == "Travel card")
    assert card["balance"] == p["groups"][0]["balance"]                   # the file's ledger balance, owed
    for body, field in (({"filename": "x.pdf", "data": base64.b64encode(b"%PDF-1.4 stuff").decode()}, "file"),
                        ({"filename": "x.csv", "data": "@@not base64@@"}, "file"),
                        ({"filename": "x.csv", "data": ""}, "file")):
        s, e = post(app, "/api/import/preview", body)
        assert s == 400 and e["field"] == field and "Traceback" not in e["error"]


def test_the_http_size_limit_for_imports(conn):
    import http.client
    import threading
    app = App(config.db_path(), lambda: None, "127.0.0.1", 0)
    srv = web.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def send(path, size):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
            c.putrequest("POST", path)
            c.putheader("Host", f"127.0.0.1:{srv.server_address[1]}")
            c.putheader("Content-Type", "application/json")
            c.putheader("Content-Length", str(size))
            c.endheaders()
            limit = web.MAX_IMPORT_BODY if path.startswith("/api/import/") else web.MAX_BODY
            if size <= limit:  # over the limit the server answers before the body (it never reads one that big)
                c.send(json.dumps({"filename": "a.csv", "data": ""}).encode().ljust(size, b" "))
            return c.getresponse().status
        assert send("/api/import/preview", web.MAX_IMPORT_BODY + 1) == 413
        assert send("/api/prefs", web.MAX_BODY + 1) == 413
        assert send("/api/import/preview", 2_000_000) == 400                      # big bodies are fine for imports
    finally:
        srv.shutdown()
        srv.server_close()

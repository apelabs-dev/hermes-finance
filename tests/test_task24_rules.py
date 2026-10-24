"""TASK-24: the rules manager (rulebook.py + classify.edit_rule/keep_rule/delete_rule/restore_rule): list, edit, keep,
delete (+ undo), suggestions (+ accept, dismiss), the routes behind CSRF, `finance rules`, and finance_rules."""

from __future__ import annotations

import json
from datetime import date

import pytest

import rules_scene
from agent_tools import plugin_ctx, run_cli, table_hashes
from hermes_finance import auth, classify, cli, config, db, features, prefs, rulebook, tooldefs
from hermes_finance.web import App

TODAY = date(2026, 9, 28)
LOCAL = {"Host": "127.0.0.1:8765"}
JSONH = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
RULE_KEYS = {"id", "field", "type", "pattern", "label", "category", "source", "matches", "created_at"}


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    monkeypatch.setattr(tooldefs, "today", lambda: TODAY)


@pytest.fixture
def scene(conn):
    return rules_scene.build(conn)


def filed(conn, tid):
    return tuple(conn.execute("SELECT category, category_source FROM v_transactions WHERE transaction_id = ?", (tid,)).fetchone())


def rid(conn, pattern):
    return conn.execute("SELECT id FROM rules WHERE pattern = ?", (pattern,)).fetchone()[0]


def call(app, method, path, headers, body=None):
    status, ctype, out, _ = app.respond(method, path, headers, json.dumps(body).encode() if body is not None else b"")
    return status, json.loads(out)


@pytest.fixture
def app(scene):
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


# --- list ---------------------------------------------------------------------------------------------------------

def test_the_list_is_every_rule_newest_first_in_plain_words(conn, scene):
    p = rulebook.payload(conn)
    assert p["counts"] == {"total": 5, "user": 2, "ai": 3}
    assert [r["pattern"] for r in p["rules"]] == ["Blue Bottle", "Netflix", "Equinox", "OPENROUTER", "Xai"]
    assert all(set(r) == RULE_KEYS for r in p["rules"])
    by = {r["pattern"]: r for r in p["rules"]}
    assert by["Xai"]["label"] == "Merchant is Xai" and by["OPENROUTER"]["label"] == "Name contains OPENROUTER"
    assert by["Xai"]["source"] == "ai" and by["Netflix"]["source"] == "user"
    assert by["Xai"]["matches"] == 3 and by["OPENROUTER"]["matches"] == 2 and by["Xai"]["category"] == "Subscriptions"


# --- edit / keep ----------------------------------------------------------------------------------------------------

def test_edit_refiles_matching_rows_but_never_a_user_set_one(conn, scene):
    x = scene["xai"]
    assert filed(conn, x[0]) == ("Other Shopping", "user") and filed(conn, x[1]) == ("Subscriptions", "rule")
    out = rulebook.update(conn, rid(conn, "Xai"), "Entertainment")
    assert out["refiled"] == 2 and out["rule"]["category"] == "Entertainment" and out["rule"]["source"] == "user"
    assert filed(conn, x[1]) == filed(conn, x[2]) == ("Entertainment", "rule")
    assert filed(conn, x[0]) == ("Other Shopping", "user")                         # the user's own decision stays
    assert classify.apply(conn)["rule"] == 0                                 # the next apply agrees


def test_pattern_edit_only_for_user_rules_and_it_releases_rows_it_no_longer_matches(conn, scene):
    with pytest.raises(rulebook.RuleError, match="only your own"):
        rulebook.update(conn, rid(conn, "Xai"), "Subscriptions", "Xai Corp")
    n = scene["netflix"]
    out = rulebook.update(conn, rid(conn, "Netflix"), "Entertainment", "Netflix.com")
    assert out["rule"]["pattern"] == "Netflix.com" and out["rule"]["matches"] == 0 and out["refiled"] == 3
    assert all(filed(conn, t)[1] == "plaid" for t in n)
    with pytest.raises(rulebook.RuleError, match="already has that pattern"):
        rulebook.update(conn, rid(conn, "Netflix.com"), "Entertainment", "Blue Bottle")
    with pytest.raises(rulebook.RuleError) as e:
        rulebook.update(conn, 999, "Other Health")
    assert e.value.status == 404


def test_keep_makes_an_ai_rule_yours(conn, scene):
    r = rulebook.keep(conn, rid(conn, "OPENROUTER"))["rule"]
    assert r["source"] == "user"
    row = conn.execute("SELECT created_from, priority FROM rules WHERE id = ?", (r["id"],)).fetchone()
    assert tuple(row) == ("user", 200)
    # an AI save on the same key can no longer change it
    assert classify.add_rule(conn, "name", "OPENROUTER", "Other Shopping", "regex", created_from="ai").kept_user_rule


# --- delete / restore -------------------------------------------------------------------------------------------------

def test_a_deleted_rule_stops_applying_and_its_rows_stay(conn, scene):
    x = scene["xai"]
    out = rulebook.delete(conn, rid(conn, "Xai"))
    assert out["rule"]["pattern"] == "Xai" and out["filed"] == sorted(x[1:]) and set(out["undo"]) == {"receipt"}
    assert filed(conn, x[1]) == ("Subscriptions", "rule")                   # already-filed rows stay as they are
    classify.apply(conn)
    assert filed(conn, x[1]) == ("Subscriptions", "rule")                   # and apply leaves them alone
    assert all(r["pattern"] != "Xai" for r in rulebook.payload(conn)["rules"])
    new = classify.add_rule(conn, "merchant_name", "Xai", "Travel", created_from="ai")  # a later AI rule, maybe same id
    classify.apply(conn)
    assert filed(conn, x[1]) == ("Subscriptions", "rule") and new.id
    # a new transaction from the merchant gets no rule from the deleted one
    from factory import tx
    classify.delete_rule(conn, new.id)
    t = tx(conn, "2026-09-27", 20.0, "Xai", cat="Other Shopping")
    classify.apply(conn)
    assert filed(conn, t) == ("Other Shopping", "plaid")


def test_restore_undoes_a_delete(conn, scene):
    x = scene["xai"]
    old = rid(conn, "Xai")
    undo = rulebook.delete(conn, old)["undo"]
    r = rulebook.restore(conn, json.loads(json.dumps(undo)))["rule"]
    assert r["id"] == old and r["pattern"] == "Xai" and r["source"] == "ai"
    assert conn.execute("SELECT rule_id FROM classifications WHERE transaction_id = ?", (x[1],)).fetchone()[0] == old
    with pytest.raises(rulebook.RuleError, match="already undone"):     # TASK-24 fix: a receipt restores once
        rulebook.restore(conn, undo)
    undo = rulebook.delete(conn, old)["undo"]
    classify.add_rule(conn, "merchant_name", "Xai", "Travel", created_from="ai")
    with pytest.raises(rulebook.RuleError, match="exists again"):
        rulebook.restore(conn, undo)
    with pytest.raises(rulebook.RuleError):
        rulebook.restore(conn, {"match_field": "nope"})


# --- suggestions --------------------------------------------------------------------------------------------------

def test_suggestions_hand_filings_and_ai_overrides(conn, scene):
    s = {x["merchant"]: x for x in rulebook.suggestions(conn)}
    assert set(s) == {"Trader Joe's", "Equinox"}
    assert s["Trader Joe's"]["text"] == "You filed Trader Joe's under Groceries 2 times. Always do that?"
    assert s["Equinox"]["category"] == "Other Health" and s["Equinox"]["times"] == 2 and "AI" in s["Equinox"]["text"]
    assert set(s["Equinox"]) == {"key", "merchant", "category", "times", "text"}
    # one hand filing is not enough (Xai: 1 user row)
    assert "Xai" not in s


def test_a_suggestion_disappears_once_a_rule_covers_it(conn, scene):
    classify.add_rule(conn, "merchant_name", "Trader Joe's", "Groceries", created_from="user")
    assert {x["merchant"] for x in rulebook.suggestions(conn)} == {"Equinox"}
    # an AI rule filing them the same way
    classify.add_rule(conn, "merchant_name", "Equinox", "Other Health", created_from="ai")
    assert rulebook.suggestions(conn) == []


def test_accept_adds_a_user_rule_and_dismiss_is_remembered(conn, scene):
    s = {x["merchant"]: x for x in rulebook.suggestions(conn)}
    out = rulebook.accept(conn, s["Equinox"]["key"])
    assert out["rule"]["source"] == "user" and out["rule"]["label"] == "Merchant is Equinox"
    assert out["rule"]["category"] == "Other Health" and out["refiled"] == 1                   # the third Equinox row
    assert all(filed(conn, t)[0] == "Other Health" for t in scene["gym"])
    assert rulebook.dismiss(conn, s["Trader Joe's"]["key"]) == {"dismissed": True}
    assert rulebook.suggestions(conn) == []
    assert rulebook.dismiss(conn, s["Trader Joe's"]["key"]) == {"dismissed": True}       # twice: still one key
    assert rulebook.dismissed(conn) == [s["Trader Joe's"]["key"]]
    with pytest.raises(rulebook.RuleError) as e:
        rulebook.accept(conn, "merchant_name|nope|1")
    assert e.value.status == 404


# --- routes ---------------------------------------------------------------------------------------------------------

def test_routes_match_the_contract(app, conn, scene):
    status, got = call(app, "GET", "/api/rules", LOCAL)
    assert status == 200 and set(got) == {"rules", "counts", "suggestions"} and got["counts"]["total"] == 5
    xai = rid(conn, "Xai")
    status, out = call(app, "POST", "/api/rules/update", JSONH, {"id": xai, "category": "Travel"})
    assert status == 200 and set(out) == {"rule", "refiled"} and out["refiled"] == 2 and set(out["rule"]) == RULE_KEYS
    assert call(app, "POST", "/api/rules/update", JSONH, {"id": rid(conn, "OPENROUTER"), "category": "Travel",
                                                          "pattern": "X"})[0] == 400
    assert call(app, "POST", "/api/rules/update", JSONH, {"id": 999, "category": "Travel"}) == (404, {"error": "unknown rule 999"})
    assert call(app, "POST", "/api/rules/update", JSONH, {"id": "1", "category": "Travel"})[0] == 400
    assert call(app, "POST", "/api/rules/update", JSONH, {"id": xai, "category": "Nope"})[0] == 400
    status, out = call(app, "POST", "/api/rules/keep", JSONH, {"id": rid(conn, "OPENROUTER")})
    assert status == 200 and out["rule"]["source"] == "user"
    status, out = call(app, "POST", "/api/rules/delete", JSONH, {"id": xai})
    assert status == 200 and out["deleted"] == xai and out["rule"]["pattern"] == "Xai" and set(out["undo"]) == {"receipt"}
    status, back = call(app, "POST", "/api/rules/restore", JSONH, {"undo": out["undo"]})
    assert status == 200 and back["rule"]["id"] == xai and back["rule"]["category"] == "Travel"
    key = got["suggestions"][0]["key"]
    status, acc = call(app, "POST", "/api/rules/suggestions/accept", JSONH, {"key": key})
    assert status == 200 and set(acc) == {"rule", "refiled"}
    k2 = got["suggestions"][1]["key"]
    assert call(app, "POST", "/api/rules/suggestions/dismiss", JSONH, {"key": k2}) == (200, {"dismissed": True})
    assert call(app, "GET", "/api/rules", LOCAL)[1]["suggestions"] == []
    assert call(app, "POST", "/api/rules/suggestions/dismiss", JSONH, {})[0] == 400


@pytest.fixture
def builtin(conn, scene):
    from argon2 import PasswordHasher
    auth._hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    with conn:
        conn.execute("INSERT INTO auth_users (username, password_hash, totp_secret, created_at, password_changed_at) "
                     "VALUES ('owner', ?, ?, ?, ?)", (auth.hash_secret("pw"), auth.new_totp_secret(), db.now(), db.now()))
    prefs.save(conn, {"access.mode": "builtin"}, allowed=["access.mode"])
    token, csrf = auth.create_session(conn, 1)
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    return a, {"Cookie": f"{auth.COOKIE}={token}"}, csrf


WRITE_PATHS = ["/api/rules/update", "/api/rules/keep", "/api/rules/delete", "/api/rules/restore",
               "/api/rules/suggestions/accept", "/api/rules/suggestions/dismiss"]


def test_every_rules_write_is_a_features_route():
    assert set(WRITE_PATHS) <= set(features.POST_ROUTES) and "/api/rules" in features.GET_ROUTES


@pytest.mark.parametrize("path", WRITE_PATHS)
def test_every_rules_write_needs_the_session_and_its_csrf_token(builtin, conn, path):
    app, cookie, csrf = builtin
    undo = rulebook.delete(conn, rid(conn, "Equinox"))["undo"]
    key = rulebook.suggestions(conn)[0]["key"]
    body = {"id": rid(conn, "Xai"), "category": "Travel", "undo": undo, "key": key}
    before = table_hashes(config.db_path())
    assert call(app, "POST", path, JSONH, body)[0] == 401
    for headers in ({**JSONH, **cookie}, {**JSONH, **cookie, "X-CSRF-Token": "wrong"}):
        status, out = call(app, "POST", path, headers, body)
        assert status == 403 and "CSRF" in out["error"]
    assert table_hashes(config.db_path()) == before
    status, out = call(app, "POST", path, {**JSONH, **cookie, "X-CSRF-Token": csrf}, body)
    assert status == 200, out


# --- CLI + tool --------------------------------------------------------------------------------------------------------

def test_cli_list_suggestions_delete_and_the_tool_agree(conn, scene, capsys):
    code, out, _ = run_cli(["rules", "--json"], capsys)
    listed = json.loads(out)
    assert code == 0 and listed == rulebook.payload(conn)
    assert json.loads(run_cli(["rules", "list", "--json"], capsys)[1]) == listed
    assert tooldefs.run_read("finance_rules", {}, config.db_path(), TODAY) == listed
    assert plugin_ctx().call("finance_rules", {}) == listed
    code, out, _ = run_cli(["rules", "suggestions", "--json"], capsys)
    assert code == 0 and json.loads(out) == listed["suggestions"]
    code, out, _ = run_cli(["rules"], capsys)
    assert code == 0 and out.startswith("5 rules · 2 yours · 2 suggestions") and "Merchant is Xai" in out
    code, out, _ = run_cli(["rules", "delete", str(rid(conn, "Xai"))], capsys)
    assert code == 0 and "deleted" in out and "stay as they are" in out
    assert run_cli(["rules", "delete", "999"], capsys)[0] == 2
    assert "finance_rules" in tooldefs.READS and not any("delete" in n for n in tooldefs.TOOLS if "rule" in n)


def test_the_tool_changes_nothing(conn, scene):
    before = table_hashes(config.db_path())
    tooldefs.run_read("finance_rules", {}, config.db_path(), TODAY)
    assert table_hashes(config.db_path()) == before
    with pytest.raises(tooldefs.ToolError, match="unknown argument"):
        tooldefs.run_read("finance_rules", {"delete": 1}, config.db_path(), TODAY)


@pytest.mark.parametrize("pattern", ["Xai", "Changed"])
def test_ai_pattern_is_not_editable_even_if_unchanged(conn, scene, pattern):
    with pytest.raises(rulebook.RuleError, match="only your own"):
        rulebook.update(conn, rid(conn, "Xai"), "Travel", pattern)


@pytest.mark.parametrize("filed", [123, "txid", {"key": "value"}])
def test_restore_malformed_filed_is_a_400(app, conn, filed):
    undo = rulebook.delete(conn, rid(conn, "Xai"))["undo"]
    undo["filed"] = filed
    status, out = call(app, "POST", "/api/rules/restore", JSONH, {"undo": undo})
    assert status == 400 and "error" in out

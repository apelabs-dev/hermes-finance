"""TASK-24 fix (independent review, rules-review.md): the three defects the owner reproduced on a synthetic DB, each through
its real caller, plus the guards that keep the legitimate behavior.

A: what a deleted *user* rule filed stays protected from the AI (a stale queue's 'set', classify.set_category, "Needs
   review"), while a deleted AI rule's rows stay open to it; Undo links them again.
B: accepting a suggestion writes only that merchant's rule and re-files only its rows (no global classify-apply), counts
   only those, and is all or nothing through a direct caller, the web route and `finance rules delete`'s path alike.
C: Undo takes only the server-held receipt: forged fields, an Undo with no delete, a replay, unrelated rows and a row
   changed away and back are all refused or left alone; a legitimate Undo works, also after a restart.

Tests named in scripts/task24_fix_failfirst.py fail on the pre-fix code (6b89dd5) with the defect's own symptom."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

import rules_scene
from agent_tools import run_cli, table_hashes
from factory import basic, tx
from hermes_finance import ai, auth, classify, config, db, prefs, rulebook
from hermes_finance.web import App

TODAY = date(2026, 9, 28)
LOCAL = {"Host": "127.0.0.1:8765"}
JSONH = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}


@pytest.fixture
def scene(conn):
    return rules_scene.build(conn)


def filed(conn, tid):
    return tuple(conn.execute("SELECT category, category_source FROM v_transactions WHERE transaction_id = ?", (tid,)).fetchone())


def link(conn, tid):
    return conn.execute("SELECT rule_id FROM classifications WHERE transaction_id = ?", (tid,)).fetchone()[0]


def rid(conn, pattern):
    return conn.execute("SELECT id FROM rules WHERE pattern = ?", (pattern,)).fetchone()[0]


def classifications(conn):
    return {r[0]: tuple(r)[1:] for r in conn.execute("SELECT transaction_id, category_id, source, rule_id FROM classifications")}


def call(app, method, path, headers, body=None):
    status, _, out, _ = app.respond(method, path, headers, json.dumps(body).encode() if body is not None else b"")
    return status, json.loads(out)


def new_app():
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


# --- A: a deleted user rule's filings keep their protection ------------------------------------------------------------

@pytest.fixture
def stale(conn):
    """A queue made before the user saved a rule for the merchant: the rule re-files its rows, then the user deletes it."""
    basic(conn)
    tids = [tx(conn, f"2026-09-0{d}", 30.0, "Stale Synthetic Co", cat="Other Shopping", tid=f"st{d}")
            for d in (1, 2, 3)]
    q = ai.queue(conn)
    assert [g["merchant"] for g in q["groups"]] == ["Stale Synthetic Co"]
    rule = classify.add_rule(conn, "merchant_name", "Stale Synthetic Co", "Groceries")            # the user's
    assert rule.refiled == 3 and all(filed(conn, t) == ("Groceries", "rule") for t in tids)
    undo = rulebook.delete(conn, rule.id)["undo"]
    return q, tids, rule.id, undo


def test_stale_ai_queue_never_overwrites_a_deleted_user_rules_filings(conn, stale):
    q, tids, _, _ = stale
    out = ai.apply_decisions(conn, {"run_id": q["run_id"], "decisions": [
        {"group_id": "g1", "action": "set", "category": "Travel", "confidence": 0.95, "note": "stale guess"}]})
    assert [filed(conn, t) for t in tids] == [("Groceries", "rule")] * 3, "the AI overwrote a deleted user rule's filings"
    assert out["txns_changed"] == 0 and out["changes"] == []
    assert out["kept_user_rule"] == [{"group_id": "g1", "merchant": "Stale Synthetic Co", "rule_id": None,
                                      "category": "Groceries", "n": 3}]


def test_stale_ai_split_item_never_overwrites_a_deleted_user_rules_filing(conn, stale):
    q, tids, _, _ = stale
    ai.apply_decisions(conn, {"run_id": q["run_id"], "decisions": [
        {"group_id": "g1", "action": "split",
         "items": [{"transaction_id": tids[0], "category": "Other Health", "confidence": 0.9}], "rest": "confirm"}]})
    assert filed(conn, tids[0]) == ("Groceries", "rule"), "the AI overwrote a deleted user rule's filing"


def test_set_category_ai_keeps_a_deleted_user_rules_filing(conn, scene):
    n = scene["netflix"][0]
    rulebook.delete(conn, rid(conn, "Netflix"))
    assert filed(conn, n) == ("Entertainment", "rule")                        # the delete keeps category and source
    assert classify.set_category(conn, n, "Travel", source="ai") == "kept-user-rule", "AI overwrite allowed after delete"
    assert filed(conn, n) == ("Entertainment", "rule")
    assert classify.set_category(conn, n, "Travel") == "set" and filed(conn, n) == ("Travel", "user")   # the user still can


def test_a_deleted_ai_rules_filings_stay_open_to_the_ai(conn, scene):
    """Only a user rule's delete keeps the protection: a former AI rule's rows are not made the user's."""
    x = scene["xai"][1]
    rulebook.delete(conn, rid(conn, "Xai"))
    assert classify.set_category(conn, x, "Travel", source="ai") == "set" and filed(conn, x) == ("Travel", "ai")


def test_rows_filed_before_the_upgrade_with_no_receipt_get_no_user_protection(conn, scene):
    """A 'rule' row with no rule and no receipt (a delete before v14): no provenance, so not treated as the user's."""
    n = scene["netflix"][0]
    with conn:
        conn.execute("UPDATE classifications SET rule_id = NULL WHERE transaction_id = ?", (n,))
    assert classify.set_category(conn, n, "Travel", source="ai") == "set"


def test_needs_review_stays_cleared_after_the_user_rule_is_deleted(conn):
    """A row the AI flagged, then a user rule filed (the user decided): deleting the rule doesn't put it back in Needs
    review (v_transactions.ai_flagged)."""
    basic(conn)
    t = tx(conn, "2026-09-02", 12.0, "Flagged Synthetic Co", cat="Other Shopping", tid="fl1")
    with conn:
        conn.execute("INSERT INTO ai_runs (run_id, created_at, applied_at) VALUES ('ai-x', '2026-09-01T00:00:00Z', "
                     "'2026-09-01T00:00:00Z')")
        conn.execute("INSERT INTO ai_reviews (transaction_id, run_id, reviewed_at, verdict, note) VALUES (?, 'ai-x', "
                     "'2026-09-01T00:00:00Z', 'flagged', 'which?')", (t,))
    flagged = lambda: conn.execute("SELECT ai_flagged FROM v_transactions WHERE transaction_id = ?", (t,)).fetchone()[0]  # noqa: E731
    assert flagged() == 1
    r = classify.add_rule(conn, "merchant_name", "Flagged Synthetic Co", "Groceries")
    assert flagged() == 0
    rulebook.delete(conn, r.id)
    assert flagged() == 0, "deleting the user rule put the row back in Needs review"


def test_undo_restores_the_link_and_the_protection(conn, stale):
    q, tids, old, undo = stale
    assert all(link(conn, t) is None for t in tids)
    back = rulebook.restore(conn, undo)
    assert back["rule"]["id"] == old and back["relinked"] == 3 and all(link(conn, t) == old for t in tids)
    assert back["rule"]["source"] == "user" and back["rule"]["category"] == "Groceries"
    assert classify.set_category(conn, tids[0], "Travel", source="ai") == "kept-user-rule"
    assert classify.apply(conn)["rule"] == 0                                  # the next apply agrees


# --- B: accepting a suggestion stays in its scope and is atomic ----------------------------------------------------------

@pytest.fixture
def pending_ai_rule(conn, scene):
    """An unrelated merchant with an AI rule saved but not applied yet (the owner's repro B)."""
    t = tx(conn, "2026-09-27", 12.0, "Review Synthetic Merchant", cat="Other Shopping", tid="unrelated1")
    classify.apply(conn)
    classify.add_rule(conn, "merchant_name", "Review Synthetic Merchant", "Travel", created_from="ai")
    assert filed(conn, t) == ("Other Shopping", "plaid")
    return t


def _suggestion(conn, merchant):
    return next(s for s in rulebook.suggestions(conn) if s["merchant"] == merchant)


def test_accept_changes_only_the_approved_merchant(conn, pending_ai_rule):
    before = classifications(conn)
    out = rulebook.accept(conn, _suggestion(conn, "Trader Joe's")["key"])
    assert filed(conn, pending_ai_rule) == ("Other Shopping", "plaid"), \
        "accepting Trader Joe's changed an unrelated merchant"
    after = classifications(conn)
    changed = {t for t in after if after[t] != before.get(t)}
    trader = {r[0] for r in conn.execute("SELECT transaction_id FROM transactions WHERE merchant_name = ?", ("Trader Joe's",))}
    assert changed <= trader
    assert out["refiled"] == len(changed) == 0 and out["rule"]["label"] == "Merchant is Trader Joe's"


def test_accept_refiles_and_counts_the_approved_merchants_rows_only(conn, pending_ai_rule):
    before = classifications(conn)
    out = rulebook.accept(conn, _suggestion(conn, "Equinox")["key"])
    after = classifications(conn)
    changed = {t for t in after if after[t] != before.get(t)}
    gym = {r[0] for r in conn.execute("SELECT transaction_id FROM transactions WHERE merchant_name = 'Equinox'")}
    assert changed and changed <= gym and out["refiled"] == len(changed) == 1, "refiled counts rows outside the merchant"
    assert filed(conn, pending_ai_rule) == ("Other Shopping", "plaid")


class Injected(sqlite3.OperationalError):
    pass


def _fail_after_the_writes(monkeypatch):
    """The last step of accept (reading the saved rule back) fails: every write before it must be undone."""
    def boom(conn, rule_id):
        raise Injected("disk I/O error (injected)")
    monkeypatch.setattr(rulebook, "rule", boom)


def _fail_mid_refile(monkeypatch):
    """The second row the new rule re-files fails to write."""
    real, calls = classify._file, []

    def flaky(conn, *a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise Injected("disk I/O error (injected)")
        return real(conn, *a, **k)
    monkeypatch.setattr(classify, "_file", flaky)


@pytest.fixture
def three_to_refile(conn, scene):
    """Equinox's suggestion with 3 rows the new rule would re-file (so a failure can land half way)."""
    for d in (4, 5):
        tx(conn, f"2026-09-0{d}", 40.0, "Equinox", cat="Other Shopping", tid=f"gymx{d}")
    classify.apply(conn)
    return _suggestion(conn, "Equinox")["key"]


@pytest.mark.parametrize("inject", [_fail_after_the_writes, _fail_mid_refile], ids=["after-writes", "mid-refile"])
def test_accept_is_all_or_nothing_for_a_direct_caller(conn, three_to_refile, monkeypatch, inject):
    before = table_hashes(config.db_path())
    inject(monkeypatch)
    plain = db.connect()                                  # like `finance` / a script: no transaction of its own
    try:
        with pytest.raises(Injected):
            rulebook.accept(plain, three_to_refile)
    finally:
        plain.close()
    assert table_hashes(config.db_path()) == before, "a failed accept left a rule or re-filed rows behind"


@pytest.mark.parametrize("inject", [_fail_after_the_writes, _fail_mid_refile], ids=["after-writes", "mid-refile"])
def test_accept_is_all_or_nothing_through_the_web_route(conn, three_to_refile, monkeypatch, inject):
    before = table_hashes(config.db_path())
    inject(monkeypatch)
    status, out = call(new_app(), "POST", "/api/rules/suggestions/accept", JSONH, {"key": three_to_refile})
    assert status == 500 and out == {"error": "internal error (Injected)"}
    assert table_hashes(config.db_path()) == before, "a failed accept left a rule or re-filed rows behind"


def test_accept_inside_a_callers_transaction_undoes_only_itself(conn, three_to_refile, monkeypatch):
    """Joined to an open transaction: a savepoint, so the caller's own earlier write survives and can commit."""
    _fail_after_the_writes(monkeypatch)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO settings (key, value, updated_at) VALUES ('caller.mark', '1', 'now')")
    with pytest.raises(Injected):
        rulebook.accept(conn, three_to_refile)
    conn.commit()
    assert conn.execute("SELECT value FROM settings WHERE key = 'caller.mark'").fetchone()[0] == "1"
    assert not conn.execute("SELECT 1 FROM rules WHERE pattern = 'Equinox' AND created_from = 'user'").fetchone()


def test_atomic_keeps_the_first_error_when_sqlite_already_rolled_back(conn):
    """Inside a caller's transaction that SQLite itself rolled back (disk full, I/O error), the savepoint is gone: the
    error that did it is the one raised, not a "no such savepoint" from the cleanup."""
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(Injected):
        with classify.atomic(conn):
            conn.rollback()
            raise Injected("disk I/O error (injected)")
    assert not conn.in_transaction


def test_a_failed_delete_leaves_nothing_behind_through_the_cli(conn, scene, monkeypatch, capsys):
    """`finance rules delete`: the receipt, the detached rows and the rule's DELETE are written, then it fails: none of it
    stays."""
    before = table_hashes(config.db_path())
    real_delete = classify.delete_rule

    def delete_then_fail(c, rule_id):
        real_delete(c, rule_id)
        raise Injected("disk I/O error (injected)")
    monkeypatch.setattr(classify, "delete_rule", delete_then_fail)
    with pytest.raises(Injected):
        run_cli(["rules", "delete", str(rid(conn, "Netflix"))], capsys)
    assert table_hashes(config.db_path()) == before, "a failed delete left its writes behind"


# --- C: Undo takes only the server-held receipt --------------------------------------------------------------------------

def _restore_status(conn, undo) -> int:
    """restore(), as a status: 200, or the RuleError's."""
    try:
        rulebook.restore(conn, undo)
    except rulebook.RuleError as e:
        return e.status
    return 200


def test_forged_undo_fields_are_refused_and_change_nothing(conn, scene):
    """The owner's repro C: the delete's undo edited to another pattern, owner and priority."""
    x = scene["xai"]
    u = rulebook.delete(conn, rid(conn, "Xai"))["undo"]
    before = table_hashes(config.db_path())
    forged = {**u, "pattern": "Unrelated Synthetic Pattern", "created_from": "user", "priority": 999999}
    status = _restore_status(conn, forged)
    assert not conn.execute("SELECT 1 FROM rules WHERE pattern = 'Unrelated Synthetic Pattern'").fetchone(), \
        "forged nonmatching user-rule provenance accepted"
    assert status == 400 and table_hashes(config.db_path()) == before
    assert classify.set_category(conn, x[1], "Travel", source="ai") == "set"            # no user protection made up
    # the receipt itself still works, and restores the rule as it really was
    classify.set_category(conn, x[1], "Subscriptions", source="ai")
    back = rulebook.restore(conn, u)["rule"]
    assert (back["pattern"], back["source"]) == ("Xai", "ai")
    assert conn.execute("SELECT priority FROM rules WHERE id = ?", (back["id"],)).fetchone()[0] == 100


def test_an_undo_with_no_delete_is_refused(conn, scene):
    """A made-up undo in the pre-fix shape (every field a delete used to return): nothing was deleted, nothing restores."""
    unrelated = scene["trader"] + scene["xai"][1:]
    made_up = {"id": 777, "match_field": "merchant_name", "match_type": "exact", "pattern": "Forged Synthetic Co",
               "category_id": classify.category_id(conn, "Groceries"), "category": "Groceries", "priority": 999999,
               "created_from": "user", "created_at": "2026-09-01T00:00:00Z", "filed": unrelated}
    before = table_hashes(config.db_path())
    status = _restore_status(conn, made_up)
    assert not conn.execute("SELECT 1 FROM rules WHERE pattern = 'Forged Synthetic Co'").fetchone(), \
        "an Undo with no delete restored a rule"
    assert status == 400 and table_hashes(config.db_path()) == before


def test_a_forged_filed_list_never_links_unrelated_rows(conn, scene):
    """Unrelated rows a deleted rule left (Blue Bottle's, same shape) added to Netflix's undo: never linked."""
    bb = scene["bluebottle"]
    rulebook.delete(conn, rid(conn, "Blue Bottle"))
    u = rulebook.delete(conn, rid(conn, "Netflix"))["undo"]
    with conn:  # same category as Netflix's rows, so only the receipt can tell them apart
        conn.executemany("UPDATE classifications SET category_id = ? WHERE transaction_id = ?",
                         [(classify.category_id(conn, "Entertainment"), t) for t in bb])
    forged = {**u, "filed": sorted(set(u.get("filed", []) + bb))}
    status = _restore_status(conn, forged)
    assert all(link(conn, t) is None for t in bb), "Undo linked rows the rule never filed"
    assert status == 400


def test_replaying_an_old_undo_after_a_second_delete_is_refused(conn, scene):
    x = scene["xai"]
    first = rulebook.delete(conn, rid(conn, "Xai"))["undo"]
    rulebook.restore(conn, first)
    rulebook.delete(conn, rid(conn, "Xai"))
    before = table_hashes(config.db_path())
    status = _restore_status(conn, first)
    assert not conn.execute("SELECT 1 FROM rules WHERE pattern = 'Xai'").fetchone(), "a replayed Undo restored the rule again"
    assert status == 409 and table_hashes(config.db_path()) == before and link(conn, x[1]) is None


def test_undo_leaves_a_row_changed_away_and_back(conn, scene):
    """Netflix's user rule deleted; a later user rule on the same key re-files its rows, then it's deleted too: the rows
    look exactly as the first delete left them, but they changed in between, so the first Undo leaves them alone."""
    n = scene["netflix"]
    old = rid(conn, "Netflix")
    u = rulebook.delete(conn, old)["undo"]
    later = classify.add_rule(conn, "merchant_name", "Netflix", "Entertainment")      # changed away ...
    assert later.refiled == 3 and all(link(conn, t) == later.id for t in n)
    rulebook.delete(conn, later.id)                                                   # ... and back
    assert all(filed(conn, t) == ("Entertainment", "rule") and link(conn, t) is None for t in n)
    back = rulebook.restore(conn, u)
    assert all(link(conn, t) is None for t in n), "Undo linked a row that changed after the delete"
    assert back["relinked"] == 0 and back["rule"]["pattern"] == "Netflix"
    assert classify.set_category(conn, n[0], "Travel", source="ai") == "kept-user-rule"   # still the user's, by the later rule


def test_undo_leaves_a_row_the_ai_changed_since(conn, scene):
    """A deleted AI rule's row the AI then changed (and changed back to the same category): not linked again."""
    x = scene["xai"]
    u = rulebook.delete(conn, rid(conn, "Xai"))["undo"]
    classify.set_category(conn, x[1], "Travel", source="ai")
    classify.set_category(conn, x[1], "Subscriptions", source="ai")
    back = rulebook.restore(conn, u)
    assert link(conn, x[1]) is None and link(conn, x[2]) == back["rule"]["id"] and back["relinked"] == 1


def test_undo_of_one_delete_never_links_another_deletes_rows(conn, scene):
    rulebook.delete(conn, rid(conn, "OPENROUTER"))                       # same category (Subscriptions) as Xai's rows
    u = rulebook.delete(conn, rid(conn, "Xai"))["undo"]
    back = rulebook.restore(conn, u)
    assert back["relinked"] == 2 and all(link(conn, t) is None for t in scene["openrouter"])


def test_undo_twice_is_a_409_and_an_unknown_receipt_a_404(conn, scene):
    u = rulebook.delete(conn, rid(conn, "Xai"))["undo"]
    assert set(u) == {"receipt"} and len(u["receipt"]) >= 32
    rulebook.restore(conn, u)
    with pytest.raises(rulebook.RuleError) as e:
        rulebook.restore(conn, u)
    assert e.value.status == 409
    with pytest.raises(rulebook.RuleError) as e:
        rulebook.restore(conn, {"receipt": "not-a-receipt-" + "x" * 20})
    assert e.value.status == 404


def test_undo_works_after_a_restart(conn, scene):
    """The receipt is kept by the server (rule_deletions), not by the page or the process: a new server process (a new App
    and connection) undoes a delete the old one made."""
    x = scene["xai"]
    status, out = call(new_app(), "POST", "/api/rules/delete", JSONH, {"id": rid(conn, "Xai")})
    assert status == 200 and set(out) == {"deleted", "rule", "filed", "undo"} and out["rule"]["pattern"] == "Xai"
    status, back = call(new_app(), "POST", "/api/rules/restore", JSONH, {"undo": json.loads(json.dumps(out["undo"]))})
    assert status == 200 and back["rule"]["id"] == out["deleted"] and back["relinked"] == 2
    assert link(conn, x[1]) == out["deleted"]


def test_the_receipt_is_stored_only_as_a_hash_and_hidden_from_reads(conn, scene):
    u = rulebook.delete(conn, rid(conn, "Xai"))["undo"]
    stored = conn.execute("SELECT receipt_hash FROM rule_deletions").fetchone()[0]
    assert stored == classify.receipt_hash(u["receipt"]) and u["receipt"] not in stored
    ro = db.connect_readonly(config.db_path())
    try:
        assert ro.execute("SELECT receipt_hash FROM rule_deletions").fetchone()[0] is None
    finally:
        ro.close()


# --- the web harness: built-in login + CSRF on Undo, malformed and replayed ----------------------------------------------

@pytest.fixture
def builtin(conn, scene):
    from argon2 import PasswordHasher
    auth._hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    with conn:
        conn.execute("INSERT INTO auth_users (username, password_hash, totp_secret, created_at, password_changed_at) "
                     "VALUES ('owner', ?, ?, ?, ?)", (auth.hash_secret("pw"), auth.new_totp_secret(), db.now(), db.now()))
    prefs.save(conn, {"access.mode": "builtin"}, allowed=["access.mode"])
    token, csrf = auth.create_session(conn, 1)
    return new_app(), {**JSONH, "Cookie": f"{auth.COOKIE}={token}"}, csrf


def test_undo_needs_the_session_and_csrf_and_refuses_replays(builtin, conn):
    app, cookie, csrf = builtin
    ok = {**cookie, "X-CSRF-Token": csrf}
    status, out = call(app, "POST", "/api/rules/delete", ok, {"id": rid(conn, "Xai")})
    assert status == 200
    body = {"undo": out["undo"]}
    before = table_hashes(config.db_path())
    assert call(app, "POST", "/api/rules/restore", JSONH, body)[0] == 401
    for headers in (cookie, {**cookie, "X-CSRF-Token": "wrong"}):
        status, err = call(app, "POST", "/api/rules/restore", headers, body)
        assert status == 403 and "CSRF" in err["error"]
    assert table_hashes(config.db_path()) == before
    assert call(app, "POST", "/api/rules/restore", ok, body)[0] == 200
    after = table_hashes(config.db_path())
    status, err = call(app, "POST", "/api/rules/restore", ok, body)                 # the same receipt again
    assert status == 409 and "already undone" in err["error"] and table_hashes(config.db_path()) == after


@pytest.mark.parametrize("undo", [
    None, "a-receipt", ["receipt"], {}, {"receipt": 123}, {"receipt": ""}, {"receipt": "x" * 201},
    {"receipt": "RECEIPT", "pattern": "Unrelated"},                    # a real receipt with a forged field
    {"match_field": "merchant_name", "match_type": "exact", "pattern": "Xai", "category": "Travel", "filed": []},  # pre-fix
])
def test_malformed_undo_is_a_400_and_changes_nothing(builtin, conn, undo):
    app, cookie, csrf = builtin
    ok = {**cookie, "X-CSRF-Token": csrf}
    receipt = call(app, "POST", "/api/rules/delete", ok, {"id": rid(conn, "Xai")})[1]["undo"]["receipt"]
    if isinstance(undo, dict) and undo.get("receipt") == "RECEIPT":
        undo = {**undo, "receipt": receipt}
    before = table_hashes(config.db_path())
    status, err = call(app, "POST", "/api/rules/restore", ok, {"undo": undo})
    assert status == 400 and "error" in err
    assert table_hashes(config.db_path()) == before
    assert call(app, "POST", "/api/rules/restore", ok, {"undo": {"receipt": receipt}})[0] == 200   # still usable after


def test_unknown_receipt_is_a_404_and_a_rules_delete_verb_is_not_routed(builtin, conn):
    app, cookie, csrf = builtin
    ok = {**cookie, "X-CSRF-Token": csrf}
    before = table_hashes(config.db_path())
    assert call(app, "POST", "/api/rules/restore", ok, {"undo": {"receipt": "no-such-receipt-" + "y" * 20}})[0] == 404
    for path in ("/api/rules/delete", "/api/rules/restore", "/api/rules"):             # rules writes are POSTs only
        assert call(app, "DELETE", path, ok, {"id": rid(conn, "Xai")})[0] in (403, 404, 405)
        assert call(app, "DELETE", path, cookie, {"id": rid(conn, "Xai")})[0] in (401, 403, 404, 405)
    assert table_hashes(config.db_path()) == before

"""The daily AI classification loop: queue export, validated atomic apply, review records, user-rule
precedence, and AI flags in "Needs review"."""

from __future__ import annotations

import json
from datetime import date

import pytest

from factory import basic, tx
from hermes_finance import ai, classify, cli, config
from hermes_finance.web import App

LOCAL = {"Host": "127.0.0.1:8765"}
POST = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
AI_TABLES = ("classifications", "rules", "ai_reviews", "ai_runs", "ai_run_groups")


@pytest.fixture
def ledger(conn):
    basic(conn)
    tx(conn, "2026-06-01", 20.0, "OpenAI", name="OPENAI *CHATGPT", tid="o1")
    tx(conn, "2026-07-01", 20.0, "OpenAI", name="OPENAI *CHATGPT SUBSCR", tid="o2")
    tx(conn, "2026-07-02", 5.0, None, name="openai", tid="o3")           # no merchant_name: same group, other field
    tx(conn, "2026-08-05", 0.12, None, name="ACH VERIFY", cat="Other", tid="m1")
    tx(conn, "2026-08-06", 55.0, "Sesame", account_id="acc_cc", tid="s1")
    tx(conn, "2026-08-20", -55.0, "Sesame", account_id="acc_cc", tid="s2")
    tx(conn, "2026-09-01", 12.0, "Blue Bottle", cat="Eating Out", tid="b1")
    # never queued: decided by user / rule / ai, pending, removed, or already reviewed
    tx(conn, "2026-05-01", 9.0, "Blue Bottle", cat="Eating Out", tid="x_user")
    tx(conn, "2026-05-02", 15.49, "Netflix", cat="Entertainment", tid="x_rule")
    tx(conn, "2026-05-03", 11.99, "Hulu", cat="Entertainment", tid="x_ai")
    tx(conn, "2026-05-04", 3.0, "Blue Bottle", cat="Eating Out", tid="x_pending", pending=True)
    tx(conn, "2026-05-05", 3.0, "Blue Bottle", cat="Eating Out", tid="x_removed")
    tx(conn, "2026-05-06", 4.0, "Spotify", cat="Entertainment", tid="x_reviewed")
    classify.add_rule(conn, "merchant_name", "Netflix", "Subscriptions", created_from="ai")
    classify.apply(conn)
    classify.set_category(conn, "x_user", "Groceries", "user")
    classify.set_category(conn, "x_ai", "Subscriptions", "ai", 0.9)
    with conn:
        conn.execute("UPDATE transactions SET removed = 1 WHERE transaction_id = 'x_removed'")
        conn.execute("INSERT INTO ai_runs (run_id, created_at, applied_at) VALUES ('ai-old', ?, ?)",
                     ("2026-09-01T06:30:00Z", "2026-09-01T06:31:00Z"))
        conn.execute("INSERT INTO ai_reviews (transaction_id, run_id, reviewed_at, verdict) "
                     "VALUES ('x_reviewed', 'ai-old', '2026-09-01T06:31:00Z', 'confirmed')")
    return conn


def cat_of(conn, tid):
    return tuple(conn.execute("SELECT category, category_source FROM v_transactions WHERE transaction_id=?",
                              (tid,)).fetchone())


def reviews(conn):
    return {r["transaction_id"]: r["verdict"] for r in conn.execute("SELECT * FROM ai_reviews WHERE run_id <> 'ai-old'")}


def snapshot(conn):
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in AI_TABLES}


def by_merchant(q):
    return {g["merchant"]: g for g in q["groups"]}


def apply(conn, q, *decisions, dry_run=False):
    return ai.apply_decisions(conn, {"run_id": q["run_id"], "decisions": list(decisions)}, dry_run)


def cli_apply(tmp_path, payload, *flags):
    f = tmp_path / "decisions.json"
    f.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return cli.main(["ai-apply", str(f), *flags])


EVERY_ACTION = [
    {"group_id": "g1", "action": "rule", "category": "Subscriptions", "confidence": 0.95, "note": "AI tool subscription"},
    {"group_id": "g2", "action": "set", "category": "Transfers", "confidence": 0.95, "note": "micro-deposit verification"},
    {"group_id": "g3", "action": "split",
     "items": [{"transaction_id": "s2", "category": "Other Health", "confidence": 0.9, "note": "refund of Sesame Care"}],
     "rest": "flag", "note": "which visit?"},
    {"group_id": "g4", "action": "confirm", "confidence": 0.9},
]


# --- queue -------------------------------------------------------------------------------------------

def test_queue_groups_only_undecided_posted_rows(ledger, capsys):
    assert cli.main(["ai-queue"]) == 0
    out = capsys.readouterr().out
    assert ": " not in out and ", " not in out.split('"groups"')[0]      # compact JSON
    q = json.loads(out)
    assert q["run_id"].startswith("ai-") and len(q["run_id"]) == len("ai-20260926-063000")
    assert [(g["group_id"], g["merchant"]) for g in q["groups"]] == [
        ("g1", "OpenAI"), ("g2", "ACH VERIFY"), ("g3", "Sesame"), ("g4", "Blue Bottle")]  # oldest first
    queued = {s["transaction_id"] for g in q["groups"] for s in g["samples"]}
    assert queued == {"o1", "o2", "o3", "m1", "s1", "s2", "b1"}                        # no x_* rows
    assert (q["queued_txns"], q["truncated"]) == (7, False)
    assert {"name": "Transfers", "is_income": False, "is_transfer": True} in q["categories"]
    run = ledger.execute("SELECT * FROM ai_runs WHERE run_id = ?", (q["run_id"],)).fetchone()
    assert (run["created_at"], run["applied_at"], run["queued_groups"], run["queued_txns"]) == (q["created_at"], None, 4, 7)
    saved = {r["group_id"]: (r["merchant"], r["rule_field"], r["rule_pattern"], json.loads(r["transaction_ids_json"]))
             for r in ledger.execute("SELECT * FROM ai_run_groups WHERE run_id = ?", (q["run_id"],))}
    assert saved == {"g1": ("OpenAI", "merchant_name", "OpenAI", ["o1", "o2", "o3"]),
                     "g2": ("ACH VERIFY", "name", "ACH VERIFY", ["m1"]),
                     "g3": ("Sesame", "merchant_name", "Sesame", ["s1", "s2"]),
                     "g4": ("Blue Bottle", "merchant_name", "Blue Bottle", ["b1"])}


def test_queue_group_shape_and_history(ledger):
    classify.add_rule(ledger, "merchant_name", "SESAME ", "Other Health", created_from="ai")  # saved, not applied yet
    g = by_merchant(ai.queue(ledger))
    openai = g["OpenAI"]
    assert (openai["rule_field"], openai["rule_pattern"]) == ("merchant_name", "OpenAI")  # most common key wins
    assert openai["names"] == ["OPENAI *CHATGPT", "OPENAI *CHATGPT SUBSCR", "openai"]
    assert (openai["count"], openai["spend"]) == (3, {"min": 5.0, "max": 20.0, "sum": 45.0})
    assert openai["current"] == {"Other Shopping": 3}
    assert openai["pfc"] == {"GENERAL_MERCHANDISE_ONLINE_MARKETPLACES": 3}
    assert openai["accounts"] == ["Chase / Checking"] and openai["dates"] == ["2026-06-01", "2026-07-02"]
    assert [s["transaction_id"] for s in openai["samples"]] == ["o3", "o2", "o1"]         # newest first
    assert openai["samples"][0] == {"transaction_id": "o3", "date": "2026-07-02", "name": "openai", "spend": 5.0,
                                    "account": "Chase / Checking",
                                    "pfc_detailed": "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES",
                                    "category": "Other Shopping"}
    assert openai["existing_rule"] is None and openai["history"] == {} and openai["history_sources"] == {}
    assert g["Sesame"]["spend"] == {"min": -55.0, "max": 55.0, "sum": 0.0}
    assert g["Sesame"]["accounts"] == ["Chase / Card"]
    assert g["Sesame"]["existing_rule"] == {"category": "Other Health", "created_from": "ai"}  # case/space-insensitive
    # already-decided rows of the same merchant are context: x_user (user); pending/removed ones aren't decided
    assert g["Blue Bottle"]["history"] == {"Groceries": 1}
    assert g["Blue Bottle"]["history_sources"] == {"user": 1}


def test_samples_keep_outliers_of_a_big_group(conn):
    basic(conn)
    for day in range(1, 21):
        tx(conn, f"2026-08-{day:02d}", 10.0, "Uber", name="UBER TRIP", cat="Other Transport", tid=f"u{day:02d}")
    tx(conn, "2026-07-01", -10.0, "Uber", name="UBER REFUND", cat="Other Transport", tid="u_refund")
    tx(conn, "2026-07-02", 90.0, "Uber", name="UBER EATS", cat="Other Transport", tid="u_eats")
    (g,) = ai.queue(conn)["groups"]
    ids = [s["transaction_id"] for s in g["samples"]]
    assert len(ids) == 8 and {"u_refund", "u_eats", "u20"} <= set(ids)
    assert g["names"] == ["UBER TRIP", "UBER EATS", "UBER REFUND"] and g["count"] == 22


def test_truncated_queue_drains_oldest_first(ledger, capsys):
    assert cli.main(["ai-queue", "--max-groups", "2"]) == 0
    q = json.loads(capsys.readouterr().out)
    assert [g["merchant"] for g in q["groups"]] == ["OpenAI", "ACH VERIFY"] and q["truncated"] is True
    assert q["queued_txns"] == 4
    assert ledger.execute("SELECT COUNT(*) FROM ai_run_groups WHERE run_id = ?", (q["run_id"],)).fetchone()[0] == 2
    apply(ledger, q, {"group_id": "g1", "action": "confirm"}, {"group_id": "g2", "action": "confirm"})
    nxt = ai.queue(ledger, 2)
    assert [(g["group_id"], g["merchant"]) for g in nxt["groups"]] == [("g1", "Sesame"), ("g2", "Blue Bottle")]
    assert nxt["truncated"] is False and nxt["run_id"] != q["run_id"]
    assert cli.main(["ai-queue", "--max-groups", "0"]) == 2


def test_empty_queue_still_creates_a_run(ledger):
    q = ai.queue(ledger)
    apply(ledger, q, *({"group_id": g["group_id"], "action": "confirm"} for g in q["groups"]))
    empty = ai.queue(ledger)
    assert (empty["groups"], empty["queued_txns"], empty["truncated"]) == ([], 0, False)
    run = ledger.execute("SELECT queued_groups, queued_txns FROM ai_runs WHERE run_id = ?", (empty["run_id"],)).fetchone()
    assert tuple(run) == (0, 0)
    assert apply(ledger, empty)["groups_decided"] == 0      # and it can be applied (nothing to do)


# --- apply -------------------------------------------------------------------------------------------

def test_apply_every_action(ledger, tmp_path, capsys):
    q = ai.queue(ledger)
    assert cli_apply(tmp_path, {"run_id": q["run_id"], "decisions": EVERY_ACTION}) == 0
    out = json.loads(capsys.readouterr().out)
    # rule: an exact ai rule (priority 100) files o1/o2; o3 has no merchant_name, so it gets a per-row ai set
    rule = ledger.execute("SELECT match_field, match_type, pattern, priority, created_from, category_id FROM rules "
                          "WHERE pattern = 'OpenAI'").fetchone()
    assert tuple(rule)[:5] == ("merchant_name", "exact", "OpenAI", 100, "ai")
    assert [cat_of(ledger, t) for t in ("o1", "o2", "o3")] == [("Subscriptions", "rule")] * 2 + [("Subscriptions", "ai")]
    # set
    row = ledger.execute("SELECT category, category_source, category_confidence, category_note FROM v_transactions "
                         "WHERE transaction_id = 'm1'").fetchone()
    assert tuple(row) == ("Transfers", "ai", 0.95, "micro-deposit verification")
    # split: the refund moves, the rest is flagged
    assert cat_of(ledger, "s2") == ("Other Health", "ai") and cat_of(ledger, "s1") == ("Other Shopping", "plaid")
    # confirm
    assert cat_of(ledger, "b1") == ("Eating Out", "plaid")
    assert reviews(ledger) == {"o1": "changed", "o2": "changed", "o3": "changed", "m1": "changed",
                               "s2": "changed", "s1": "flagged", "b1": "confirmed"}
    s1 = ledger.execute("SELECT run_id, category_id, note FROM ai_reviews WHERE transaction_id = 's1'").fetchone()
    assert (s1["run_id"], s1["note"]) == (q["run_id"], "which visit?")
    assert s1["category_id"] == classify.category_id(ledger, "Other Shopping")

    assert out["run_id"] == q["run_id"] and out["dry_run"] is False
    assert (out["groups_decided"], out["txns_changed"]) == (4, 5)
    assert out["by_action"] == {"rule": 1, "set": 1, "split": 1, "confirm": 1, "flag": 0}
    assert out["changes"] == [
        {"group_id": "g1", "merchant": "OpenAI", "from": {"Other Shopping": 3}, "to": "Subscriptions", "n": 3, "action": "rule"},
        {"group_id": "g2", "merchant": "ACH VERIFY", "from": {"Other": 1}, "to": "Transfers", "n": 1, "action": "set"},
        {"group_id": "g3", "merchant": "Sesame", "from": {"Other Shopping": 1}, "to": "Other Health", "n": 1,
         "action": "split"}]
    assert out["flagged"] == [{"group_id": "g3", "merchant": "Sesame", "n": 1, "note": "which visit?"}]
    assert (out["kept_user"], out["kept_user_rule"], out["downgraded"], out["errors"], out["skipped"],
            out["unreviewed_groups"]) == (0, [], [], [], [], [])
    run = ledger.execute("SELECT applied_at, summary_json FROM ai_runs WHERE run_id = ?", (q["run_id"],)).fetchone()
    assert run["applied_at"] and json.loads(run["summary_json"]) == out
    # every reviewed row is looked at once: none of them come back
    assert ai.queue(ledger)["groups"] == []


def test_flag_keeps_the_category_and_records_the_note(ledger):
    q = ai.queue(ledger)
    out = apply(ledger, q, {"group_id": "g3", "action": "flag", "note": "can't tell what this is"})
    assert [cat_of(ledger, t) for t in ("s1", "s2")] == [("Other Shopping", "plaid")] * 2
    assert reviews(ledger) == {"s1": "flagged", "s2": "flagged"}
    assert out["flagged"] == [{"group_id": "g3", "merchant": "Sesame", "n": 2, "note": "can't tell what this is"}]
    assert out["by_action"]["flag"] == 1 and out["txns_changed"] == 0


def test_low_confidence_is_downgraded_to_flag(ledger):
    q = ai.queue(ledger)
    out = apply(ledger, q,
                {"group_id": "g1", "action": "rule", "category": "Subscriptions", "confidence": 0.5, "note": "maybe"},
                {"group_id": "g2", "action": "set", "category": "Transfers", "confidence": 0.69},
                {"group_id": "g3", "action": "split", "items": [
                    {"transaction_id": "s1", "category": "Other Health", "confidence": 0.6},
                    {"transaction_id": "s2", "category": "Other Health", "confidence": 0.7}]})
    assert ledger.execute("SELECT COUNT(*) FROM rules WHERE created_from = 'ai' AND pattern = 'OpenAI'").fetchone()[0] == 0
    assert cat_of(ledger, "o1") == ("Other Shopping", "plaid") and cat_of(ledger, "m1") == ("Other", "plaid")
    assert cat_of(ledger, "s1") == ("Other Shopping", "plaid")
    assert cat_of(ledger, "s2") == ("Other Health", "ai")                                    # 0.7 is enough
    assert reviews(ledger) == {"o1": "flagged", "o2": "flagged", "o3": "flagged", "m1": "flagged",
                               "s1": "flagged", "s2": "changed"}
    assert out["downgraded"] == [
        {"group_id": "g1", "action": "rule", "category": "Subscriptions", "confidence": 0.5, "transaction_id": None},
        {"group_id": "g2", "action": "set", "category": "Transfers", "confidence": 0.69, "transaction_id": None},
        {"group_id": "g3", "action": "split", "category": "Other Health", "confidence": 0.6, "transaction_id": "s1"}]
    assert out["by_action"] == {"rule": 0, "set": 0, "split": 1, "confirm": 0, "flag": 2}
    notes = {f["group_id"]: f["note"] for f in out["flagged"]}
    assert notes == {"g1": "low confidence (0.50) for Subscriptions: maybe",
                     "g2": "low confidence (0.69) for Transfers", "g3": "low confidence (0.60) for Other Health"}


def test_user_rows_are_never_changed(ledger):
    q = ai.queue(ledger)
    classify.set_category(ledger, "o1", "Other Health", "user")         # the user decides between queue and apply
    out = apply(ledger, q, {"group_id": "g1", "action": "set", "category": "Subscriptions", "confidence": 0.9})
    assert cat_of(ledger, "o1") == ("Other Health", "user")
    assert cat_of(ledger, "o2") == ("Subscriptions", "ai")
    assert out["kept_user"] == 1 and reviews(ledger)["o1"] == "confirmed"


def test_user_row_blocked_by_the_trigger_counts_as_kept_user(ledger, monkeypatch):
    q = ai.queue(ledger)
    classify.set_category(ledger, "o1", "Other Health", "user")

    def blind_set(conn, tid, category, source, confidence, note):  # skips the check: only the trigger is left
        conn.execute("UPDATE classifications SET source = 'ai', category_id = ? WHERE transaction_id = ?",
                     (classify.category_id(conn, category), tid))
        return "set"

    monkeypatch.setattr(ai, "_held", lambda st: None)
    monkeypatch.setattr(classify, "set_category", blind_set)
    out = apply(ledger, q, {"group_id": "g1", "action": "set", "category": "Subscriptions", "confidence": 0.9})
    assert out["kept_user"] == 1 and out["txns_changed"] == 2
    assert cat_of(ledger, "o1") == ("Other Health", "user") and cat_of(ledger, "o2") == ("Subscriptions", "ai")


def test_user_rule_is_never_overwritten(ledger):
    q = ai.queue(ledger)
    user_rule = classify.add_rule(ledger, "merchant_name", "openai", "Travel")    # between queue and apply
    out = apply(ledger, q, {"group_id": "g1", "action": "rule", "category": "Subscriptions", "confidence": 0.95},
                {"group_id": "g3", "action": "set", "category": "Other Health", "confidence": 0.9})
    rules = [tuple(r) for r in ledger.execute("SELECT pattern, created_from, priority FROM rules WHERE pattern LIKE 'openai'")]
    assert rules == [("openai", "user", 200)]                                        # no ai rule next to it
    assert cat_of(ledger, "o1") == ("Travel", "rule") and cat_of(ledger, "o3") == ("Other Shopping", "plaid")  # nothing changed
    assert {t: v for t, v in reviews(ledger).items() if t.startswith("o")} == dict.fromkeys(("o1", "o2", "o3"), "confirmed")
    assert out["kept_user_rule"] == [{"group_id": "g1", "merchant": "OpenAI", "rule_id": user_rule.id,
                                      "category": "Travel", "n": 3}]
    assert out["changes"] == [{"group_id": "g3", "merchant": "Sesame", "from": {"Other Shopping": 2},
                               "to": "Other Health", "n": 2, "action": "set"}]


def test_rows_filed_by_a_user_rule_keep_it(ledger):
    q = ai.queue(ledger)
    user_rule = classify.add_rule(ledger, "merchant_name", "Sesame", "Other Health")
    out = apply(ledger, q, {"group_id": "g3", "action": "set", "category": "Other Shopping", "confidence": 0.9})
    assert [cat_of(ledger, t) for t in ("s1", "s2")] == [("Other Health", "rule")] * 2
    assert out["kept_user_rule"] == [{"group_id": "g3", "merchant": "Sesame", "rule_id": user_rule.id,
                                      "category": "Other Health", "n": 2}]
    assert out["txns_changed"] == 0 and reviews(ledger) == {"s1": "confirmed", "s2": "confirmed"}


def test_out_of_queue_transactions_are_refused(ledger):
    q = ai.queue(ledger)
    out = apply(ledger, q,
                {"group_id": "g3", "action": "split", "items": [
                    {"transaction_id": "b1", "category": "Other Health", "confidence": 0.9},       # another group's row
                    {"transaction_id": "x_user", "category": "Other Health", "confidence": 0.9},   # never queued
                    {"transaction_id": "s2", "category": "Other Health", "confidence": 0.9}]},
                {"group_id": "g99", "action": "confirm"})
    assert cat_of(ledger, "b1") == ("Eating Out", "plaid") and cat_of(ledger, "x_user") == ("Groceries", "user")
    assert cat_of(ledger, "s2") == ("Other Health", "ai")
    assert [(e["group_id"], e.get("transaction_id"), e["error"]) for e in out["errors"]] == [
        ("g3", "b1", "transaction 'b1' is not in group g3"),
        ("g3", "x_user", "transaction 'x_user' is not in group g3"),
        ("g99", None, "unknown group_id 'g99' for this run")]
    assert reviews(ledger) == {"s1": "confirmed", "s2": "changed"}


def test_double_apply_and_unknown_run_are_refused(ledger, tmp_path, capsys):
    q = ai.queue(ledger)
    payload = {"run_id": q["run_id"], "decisions": [{"group_id": "g2", "action": "flag", "note": "?"}]}
    assert cli_apply(tmp_path, payload) == 0
    capsys.readouterr()
    before = snapshot(ledger)
    assert cli_apply(tmp_path, payload) == 2
    assert "already applied" in capsys.readouterr().err
    assert cli_apply(tmp_path, {"run_id": "ai-20000101-000000", "decisions": []}) == 2
    assert "unknown run_id" in capsys.readouterr().err
    assert snapshot(ledger) == before


def test_dry_run_changes_nothing(ledger, tmp_path, capsys):
    q = ai.queue(ledger)
    payload = {"run_id": q["run_id"], "decisions": EVERY_ACTION}
    before = snapshot(ledger)
    assert cli_apply(tmp_path, payload, "--dry-run") == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True and dry["txns_changed"] == 5
    assert snapshot(ledger) == before                                    # no rule, filing, review, or applied_at
    assert cli_apply(tmp_path, payload) == 0                              # the run is still open
    assert json.loads(capsys.readouterr().out) == {**dry, "dry_run": False}


def test_failure_mid_apply_rolls_everything_back(ledger, tmp_path, monkeypatch, capsys):
    q = ai.queue(ledger)
    payload = {"run_id": q["run_id"], "decisions": EVERY_ACTION}
    before = snapshot(ledger)
    real, calls = classify.set_category, []

    def flaky(conn, tid, *args):
        calls.append(tid)
        if tid == "m1":
            raise RuntimeError("disk on fire")
        return real(conn, tid, *args)

    monkeypatch.setattr(classify, "set_category", flaky)
    assert cli_apply(tmp_path, payload) == 1
    assert "rolled back" in capsys.readouterr().err
    assert calls == ["o3", "m1"]               # the rule was saved and applied and o3 was set before the failure...
    assert snapshot(ledger) == before          # ...and none of it persisted
    monkeypatch.setattr(classify, "set_category", real)  # not undo(): that would also drop FINANCE_DATA_DIR
    assert cli_apply(tmp_path, payload) == 0


@pytest.mark.parametrize("content", ["{not json", "[]", '{"run_id": "RUN"}', '{"run_id": "RUN", "decisions": {}}',
                                     '{"decisions": []}'])
def test_malformed_file_exits_2(ledger, tmp_path, capsys, content):
    q = ai.queue(ledger)
    before = snapshot(ledger)
    assert cli_apply(tmp_path, content.replace("RUN", q["run_id"])) == 2
    assert "error:" in capsys.readouterr().err
    assert snapshot(ledger) == before
    assert cli.main(["ai-apply", str(tmp_path / "missing.json")]) == 2


def test_reviews_only_for_decided_groups(ledger):
    q = ai.queue(ledger)
    out = apply(ledger, q, {"group_id": "g1", "action": "confirm"}, {"group_id": "g3", "action": "confirm"})
    assert set(reviews(ledger)) == {"o1", "o2", "o3", "s1", "s2"}
    assert out["unreviewed_groups"] == ["g2", "g4"] and out["groups_decided"] == 2
    assert [g["merchant"] for g in ai.queue(ledger)["groups"]] == ["ACH VERIFY", "Blue Bottle"]


def test_invalid_decisions_are_reported_and_valid_ones_apply(ledger):
    q = ai.queue(ledger)
    out = apply(ledger, q,
                {"group_id": "g1", "action": "delete"},
                {"group_id": "g2", "action": "set", "category": "Nope", "confidence": 0.9},
                {"group_id": "g3", "action": "set", "category": "Other Health"},
                {"group_id": "g3", "action": "set", "category": "Other Health", "confidence": 0.9},   # second for g3
                {"group_id": "g4", "action": "confirm", "confidence": 1.5},
                "not an object",
                {"group_id": "g2", "action": "split", "items": []})
    assert [e["error"] for e in out["errors"]] == [
        "action must be one of: rule, set, split, confirm, flag", "unknown category 'Nope'", "confidence is required",
        "more than one decision for g3", "confidence must be a number from 0 to 1", "decision must be an object",
        "more than one decision for g2"]
    assert out["groups_decided"] == 0 and reviews(ledger) == {}
    for bad in (True, "high", float("nan"), -0.1):
        q = ai.queue(ledger)
        res = apply(ledger, q, {"group_id": "g2", "action": "set", "category": "Fees", "confidence": bad},
                    {"group_id": "g1", "action": "rule", "category": "subscriptions", "confidence": 1})
        assert [e["group_id"] for e in res["errors"]] == ["g2"] and res["groups_decided"] == 1
    assert cat_of(ledger, "o1") == ("Subscriptions", "rule")             # category names are case-insensitive


def test_invalid_split_item_leaves_its_row_for_the_next_queue(ledger):
    q = ai.queue(ledger)
    out = apply(ledger, q, {"group_id": "g3", "action": "split", "rest": "confirm", "items": [
        {"transaction_id": "s2", "category": "Nope", "confidence": 0.9},
        {"transaction_id": "s2", "category": "Other Health", "confidence": 0.9}]})
    assert [e["error"] for e in out["errors"]] == ["unknown category 'Nope'", "duplicate split item for s2"]
    assert reviews(ledger) == {"s1": "confirmed"}
    (g,) = [g for g in ai.queue(ledger)["groups"] if g["merchant"] == "Sesame"]
    assert g["count"] == 1 and g["samples"][0]["transaction_id"] == "s2"


def test_rows_removed_or_reviewed_since_the_queue_are_skipped(ledger):
    first, second = ai.queue(ledger), ai.queue(ledger)
    apply(ledger, first, {"group_id": "g3", "action": "set", "category": "Other Health", "confidence": 0.9})
    with ledger:
        ledger.execute("UPDATE transactions SET removed = 1 WHERE transaction_id = 'm1'")
    out = apply(ledger, second, {"group_id": "g3", "action": "set", "category": "Travel", "confidence": 0.9},
                {"group_id": "g2", "action": "confirm"})
    assert cat_of(ledger, "s1") == ("Other Health", "ai")                          # the first run's decision stands
    assert out["skipped"] == [
        {"group_id": "g3", "transaction_id": "s1", "reason": f"already reviewed by {first['run_id']}"},
        {"group_id": "g3", "transaction_id": "s2", "reason": f"already reviewed by {first['run_id']}"},
        {"group_id": "g2", "transaction_id": "m1", "reason": "removed"}]


# --- precedence --------------------------------------------------------------------------------------

def test_ai_rows_follow_a_later_user_rule_but_user_rows_dont(ledger):
    q = ai.queue(ledger)
    apply(ledger, q, {"group_id": "g4", "action": "set", "category": "Other Shopping", "confidence": 0.8})
    assert cat_of(ledger, "b1") == ("Other Shopping", "ai")
    saved = classify.add_rule(ledger, "merchant_name", "blue bottle", "Eating Out")   # created_from defaults to user
    assert (saved.kept_user_rule, saved.refiled) == (False, 2)                        # b1 (ai) + x_pending (plaid)
    rule = ledger.execute("SELECT priority, created_from FROM rules WHERE id = ?", (saved.id,)).fetchone()
    assert tuple(rule) == (classify.USER_RULE_PRIORITY, "user") == (200, "user")
    assert cat_of(ledger, "b1") == ("Eating Out", "rule")
    assert ledger.execute("SELECT rule_id FROM classifications WHERE transaction_id = 'b1'").fetchone()[0] == saved.id
    assert cat_of(ledger, "x_user") == ("Groceries", "user")                          # user row untouched
    assert cat_of(ledger, "x_ai") == ("Subscriptions", "ai")                          # other merchants untouched
    assert classify.apply(ledger)["rule"] == 0                                         # apply agrees


def test_user_rule_beats_ai_rule_by_priority(ledger):
    ai_rule = classify.add_rule(ledger, "name", "bottle", "Travel", "regex", created_from="ai")
    user_rule = classify.add_rule(ledger, "merchant_name", "Blue Bottle", "Eating Out")
    assert ai_rule.id < user_rule.id                     # ties would go to the older rule; priority decides
    classify.apply(ledger)
    assert cat_of(ledger, "b1") == ("Eating Out", "rule")
    tx(ledger, "2026-09-20", 6.0, "Blue Bottle", cat="Eating Out", tid="b_new")      # a later sync
    classify.apply(ledger)
    assert ledger.execute("SELECT rule_id FROM classifications WHERE transaction_id = 'b_new'").fetchone()[0] == user_rule.id


def test_ai_rule_on_a_user_rules_key_is_a_no_op(ledger, capsys):
    user = classify.add_rule(ledger, "merchant_name", "Sesame", "Other Health")
    again = classify.add_rule(ledger, "merchant_name", "Sesame", "Other Shopping", created_from="ai")
    assert again == (user.id, True, 0)
    rule = ledger.execute("SELECT category_id, created_from, priority FROM rules WHERE id = ?", (user.id,)).fetchone()
    assert tuple(rule) == (classify.category_id(ledger, "Other Health"), "user", 200)
    assert cli.main(["add-rule", "--pattern", "Sesame", "--category", "Travel", "--created-from", "ai"]) == 1
    assert "is a user rule" in capsys.readouterr().out
    assert cat_of(ledger, "s1") == ("Other Health", "rule")
    # a per-transaction ai decision doesn't override the user's rule either
    assert classify.set_category(ledger, "s1", "Travel", "ai", 0.9) == "kept-user-rule"
    assert cli.main(["set-category", "s1", "Travel", "--source", "ai"]) == 1
    assert cat_of(ledger, "s1") == ("Other Health", "rule")
    # the user can still change their own rule
    assert classify.add_rule(ledger, "merchant_name", "Sesame", "Travel").refiled == 2
    assert cat_of(ledger, "s1") == ("Travel", "rule")


def test_cli_add_rule_refiles_ai_rows(ledger, capsys):
    classify.set_category(ledger, "b1", "Other Shopping", "ai", 0.8)
    assert cli.main(["add-rule", "--pattern", "Blue Bottle", "--category", "Groceries"]) == 0
    assert "2 re-filed to it" in capsys.readouterr().out
    assert cat_of(ledger, "b1") == ("Groceries", "rule") and cat_of(ledger, "x_user") == ("Groceries", "user")
    assert ledger.execute("SELECT priority FROM rules WHERE pattern = 'Blue Bottle'").fetchone()[0] == 200
    assert cli.main(["add-rule", "--pattern", "Hulu", "--category", "Entertainment", "--created-from", "ai"]) == 0
    assert cat_of(ledger, "x_ai") == ("Subscriptions", "ai")                          # an ai rule doesn't re-file
    assert ledger.execute("SELECT priority FROM rules WHERE pattern = 'Hulu'").fetchone()[0] == 100


# --- needs review ------------------------------------------------------------------------------------

def get(app, path):
    status, _, body = app.handle("GET", path, LOCAL, b"")
    assert status == 200, body
    return json.loads(body)


def post(app, path, payload):
    status, _, body = app.handle("POST", path, POST, json.dumps(payload).encode())
    assert status == 200, body
    return json.loads(body)


def test_review_count_includes_ai_flags_until_the_user_decides(ledger, client):
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    app.today = lambda: date(2026, 9, 25)

    def review():
        n = get(app, "/api/counts?month=2026-08")["review"]
        d = get(app, "/api/transactions?month=2026-08&kind=review")
        assert d["total"] == d["counts"]["review"] == n
        return {r["transaction_id"]: r["ai_note"] for r in d["rows"]}

    assert review() == {"m1": None}                                    # category Other
    rows = get(app, "/api/transactions?limit=100")["rows"]
    assert all("ai_note" in r and r["ai_note"] is None for r in rows)
    q = ai.queue(ledger)
    apply(ledger, q, {"group_id": "g3", "action": "flag", "note": "can't tell"},
          {"group_id": "g4", "action": "confirm", "note": "coffee"})
    assert review() == {"m1": None, "s1": "can't tell", "s2": "can't tell"}
    s2 = next(r for r in get(app, "/api/transactions?month=2026-08")["rows"] if r["transaction_id"] == "s2")
    assert s2["review"] is True and s2["category"] == "Other Shopping"
    b1 = next(r for r in get(app, "/api/transactions?month=2026-09")["rows"] if r["transaction_id"] == "b1")
    assert (b1["review"], b1["ai_note"]) == (False, "coffee")         # confirmed rows aren't review items
    post(app, "/api/transactions/category", {"transaction_id": "s1", "category": "Other Health"})
    assert review() == {"m1": None, "s2": "can't tell"}                # the user decided s1
    post(app, "/api/transactions/category", {"transaction_id": "s1", "category": "Other Health", "make_rule": True})
    assert review() == {"m1": None}                                    # "always file it here" decided s2 too
    assert get(app, "/api/counts?month=2026-08")["review"] == 1

"""TASK-19 (Batch C): split transactions, notes, tags and saved filters.

A split's lines add up to the transaction's amount to the cent; spending by category (budgets, rings, the chart,
cash flow, alerts, `finance spend` / budget-status, finance_transactions) uses the lines, and no total changes (checked
before / after splits on the gate DBs). A split is the user's decision: rules, set-category from anyone but the user,
and the daily AI run (ai-apply) never touch it. Notes and tags are searchable (`txns --search`, finance_transactions,
the page's search) and a tag filters; saved filters live on the server with a count for the month shown."""

from __future__ import annotations

import json
from datetime import date

import pytest

import factory as f
import gate_dbs
from hermes_finance import ai, api, classify, cli, config, db, features, insights, reads, tooldefs, txmeta
from hermes_finance.web import App

TODAY = date(2026, 9, 28)
LOCAL = {"Host": "127.0.0.1:8765"}
POST = {**LOCAL, "Content-Type": "application/json"}


@pytest.fixture
def small(conn):
    f.bank(conn, "item_a", "Chase")
    f.acct(conn, "acc_chk", current=1000.0)
    f.cap(conn, "Groceries", 300.0)
    f.cap(conn, "Other Shopping", 100.0)
    ids = {
        "costco": f.tx(conn, "2026-09-10", 254.58, "Costco", cat="Groceries"),
        "amazon": f.tx(conn, "2026-09-12", 40.00, "Amazon", cat="Other Shopping"),
        "refund": f.tx(conn, "2026-09-14", -30.00, "Costco", cat="Groceries"),
        "pending": f.tx(conn, "2026-09-27", 12.00, "Trader Joe's", cat="Groceries", pending=True),
        "pay": f.tx(conn, "2026-09-15", -2000.00, "Payroll", cat="Income"),
        "xfer": f.tx(conn, "2026-09-16", 500.00, "To Savings", cat="Transfers"),
        "aug": f.tx(conn, "2026-08-20", 90.00, "Target", cat="Other Shopping"),
    }
    return ids


def cats(conn, month="2026-09"):
    return {c["category"]: (c["spend"], c["count"]) for c in insights.category_spend(conn, month, TODAY) if c["spend"] or c["count"]}


def totals(conn, month="2026-09"):
    """Every total a split may not change: the month's spending (overview, daily curve, pending), income, cash flow,
    `finance spend`'s total and count, the pace projection."""
    s = insights.month_summary(conn, month, TODAY)
    flow = insights.cashflow(conn, 3, TODAY)
    sp = reads.spend(conn, reads.parse_filters(conn, TODAY, month=month, spending=True), TODAY)
    return {"mtd": s["mtd"], "cum": s["cum"], "pending": (s["pending_spend"], s["pending_count"]), "income": s["income"],
            "flow": [(m["income"], m["spending"], m["net"]) for m in flow["months"]], "spend": (sp["total"], sp["count"]),
            "pace": insights.pace_projection(conn, month, TODAY)["projection"]}


# --- splitting ------------------------------------------------------------------------------------------------------

def test_a_split_moves_spending_between_categories_and_changes_no_total(small, conn):
    before, before_cats = totals(conn), cats(conn)
    out = txmeta.split(conn, small["costco"], [{"category": "Groceries", "amount": 214.58}, {"category": "Other Shopping", "amount": 40}])
    assert out["split"] == [{"category": "Groceries", "amount": 214.58}, {"category": "Other Shopping", "amount": 40.0}]
    assert totals(conn) == before
    after = cats(conn)
    assert after["Groceries"] == (round(before_cats["Groceries"][0] - 40, 2), before_cats["Groceries"][1])
    assert after["Other Shopping"] == (round(before_cats["Other Shopping"][0] + 40, 2), before_cats["Other Shopping"][1] + 1)
    # budgets, alerts and budget-status read the lines: Other Shopping is now over its $100 cap
    b = insights.budgets(conn, "2026-09", TODAY)
    shop = next(c for c in b["categories"] if c["category"] == "Other Shopping")
    assert shop["spend"] == 80.0 and shop["status"] == "warn" and shop["left"] == 20.0
    bs = reads.budget_status(conn, "2026-09", TODAY)
    assert next(c for c in bs["caps"] if c["category"] == "Other Shopping")["spent"] == 80.0
    sp = reads.spend(conn, reads.parse_filters(conn, TODAY, month="2026-09", spending=True), TODAY, "category")
    assert {g["key"]: (g["total"], g["count"]) for g in sp["groups"]} == {"Groceries": (196.58, 3), "Other Shopping": (80.0, 2)}
    shop_only = reads.spend(conn, reads.parse_filters(conn, TODAY, month="2026-09", category="other shopping", spending=True), TODAY)
    assert (shop_only["total"], shop_only["count"]) == (80.0, 2)
    # the parent keeps its category (the transaction list, rules) and becomes the user's decision
    row = conn.execute("SELECT category, category_source FROM v_transactions WHERE transaction_id = ?", (small["costco"],)).fetchone()
    assert tuple(row) == ("Groceries", "user")


def test_a_split_never_changes_a_total_on_the_gate_dbs(data_dir):
    for kind in ("full", "nocaps"):
        path = config.data_dir() / f"{kind}.db"
        gate_dbs.build(kind, path, TODAY)
        conn = db.connect(path)
        month = "2026-09"
        before = totals(conn)
        before_cats = cats(conn)
        rows = conn.execute("SELECT transaction_id, amount, category FROM v_transactions WHERE month = ? AND is_income = 0 AND "
                            "is_transfer = 0 AND pending = 0 AND amount > 5 ORDER BY amount DESC", (month,)).fetchall()
        others = [r[0] for r in conn.execute("SELECT name FROM categories WHERE is_income = 0 AND is_transfer = 0 ORDER BY name")]
        moved: dict[str, float] = {}
        for i, r in enumerate(rows[:12]):  # a dozen splits, 2 or 3 lines each, odd cents
            other = next(o for o in others[i % 5:] + others if o != r["category"])
            third = next(o for o in others if o not in (r["category"], other))
            a = round(r["amount"] * 0.37, 2)
            lines = [{"category": r["category"], "amount": round(r["amount"] - a, 2)}, {"category": other, "amount": a}]
            if i % 3 == 0:
                lines = [{"category": r["category"], "amount": round(r["amount"] - a - 1.01, 2)}, {"category": other, "amount": a},
                         {"category": third, "amount": 1.01}]
            txmeta.split(conn, r[0], lines)
            for x in lines[1:]:
                moved[x["category"]] = moved.get(x["category"], 0) + x["amount"]
                moved[r["category"]] = moved.get(r["category"], 0) - x["amount"]
        assert totals(conn) == before, kind
        after = cats(conn)
        for name in set(before_cats) | set(after):
            assert after.get(name, (0.0, 0))[0] == round(before_cats.get(name, (0.0, 0))[0] + moved.get(name, 0), 2), (kind, name)
        conn.close()


@pytest.mark.parametrize("lines, message", [
    ([{"category": "Groceries", "amount": 254.58}], "2 to 20 lines"),
    ([{"category": "Groceries", "amount": 200}, {"category": "Other Shopping", "amount": 50}], "$4.58 left to split"),
    ([{"category": "Groceries", "amount": 200}, {"category": "Other Shopping", "amount": 60}], "$5.42 too much"),
    ([{"category": "Groceries", "amount": 254.58}, {"category": "Other Shopping", "amount": 0}], "more than $0"),
    ([{"category": "Groceries", "amount": 300}, {"category": "Other Shopping", "amount": -45.42}], "more than $0"),
    ([{"category": "Groceries", "amount": 200}, {"category": "Groceries", "amount": 54.58}], "on another line already"),
    ([{"category": "Groceries", "amount": 200}, {"category": "Income", "amount": 54.58}], "not a spending category"),
    ([{"category": "Groceries", "amount": 200}, {"category": "Transfers", "amount": 54.58}], "not a spending category"),
    ([{"category": "Groceries", "amount": 200}, {"category": "Food", "amount": 54.58}], "unknown category"),
    ([{"category": "Groceries", "amount": 200}, {"category": "Other Shopping", "amount": "54.58"}], "must be a number"),
    ([{"category": "Groceries", "amount": 200}, {"category": "Other Shopping", "amount": True}], "must be a number"),
    ([{"category": "Groceries", "amount": 200}, ["Other Shopping", 54.58]], "must be an object"),
    ("lines", "must be a list")])
def test_a_split_that_doesnt_add_up_or_isnt_spending_is_refused(small, conn, lines, message):
    with pytest.raises(txmeta.TxMetaError, match=message.replace("$", r"\$")):
        txmeta.split(conn, small["costco"], lines)
    assert not txmeta.is_split(conn, small["costco"])
    assert conn.execute("SELECT source FROM classifications WHERE transaction_id = ?", (small["costco"],)).fetchone() is None
    assert conn.execute("SELECT category_source FROM v_transactions WHERE transaction_id = ?", (small["costco"],)).fetchone()[0] == "plaid"


@pytest.mark.parametrize("key, message", [("pending", "still pending"), ("pay", "only spending"), ("xfer", "only spending")])
def test_pending_income_and_transfers_cant_be_split(small, conn, key, message):
    amount = conn.execute("SELECT amount FROM transactions WHERE transaction_id = ?", (small[key],)).fetchone()[0]
    with pytest.raises(txmeta.TxMetaError, match=message):
        txmeta.split(conn, small[key], [{"category": "Groceries", "amount": amount / 2}, {"category": "Other Shopping", "amount": amount / 2}])


def test_a_refund_splits_into_negative_lines(small, conn):
    before = totals(conn)
    txmeta.split(conn, small["refund"], [{"category": "Groceries", "amount": -20}, {"category": "Other Shopping", "amount": -10}])
    assert totals(conn) == before and cats(conn)["Other Shopping"][0] == 30.0


def test_resplit_replaces_and_unsplit_removes_the_lines(small, conn):
    before = cats(conn)
    txmeta.split(conn, small["costco"], [{"category": "Groceries", "amount": 214.58}, {"category": "Other Shopping", "amount": 40}])
    txmeta.split(conn, small["costco"], [{"category": "Other Health", "amount": 4.58}, {"category": "Groceries", "amount": 250}])
    assert [tuple(r) for r in conn.execute("SELECT line, amount FROM transaction_splits ORDER BY line")] == [(1, 4.58), (2, 250.0)]
    assert txmeta.split(conn, small["costco"], [])["split"] is None
    assert cats(conn) == before and conn.execute("SELECT COUNT(*) FROM transaction_splits").fetchone()[0] == 0


def test_rules_ai_and_other_set_category_callers_leave_a_split_alone(small, conn):
    txmeta.split(conn, small["costco"], [{"category": "Groceries", "amount": 214.58}, {"category": "Other Shopping", "amount": 40}])
    classify.add_rule(conn, "merchant_name", "Costco", "Other Health", created_from="user")        # re-files matches at once
    classify.apply(conn)
    assert conn.execute("SELECT category FROM v_transactions WHERE transaction_id = ?", (small["costco"],)).fetchone()[0] == "Groceries"
    assert conn.execute("SELECT category FROM v_transactions WHERE transaction_id = ?", (small["refund"],)).fetchone()[0] == "Other Health"
    assert classify.set_category(conn, small["costco"], "Other Health", "ai", 0.99) == "kept-user"
    with pytest.raises(classify.ClassifyError, match="split"):
        classify.set_category(conn, small["costco"], "Other Health", "user")
    assert txmeta.split_lines(conn, [small["costco"]])[small["costco"]][1]["category"] == "Other Shopping"


def test_ai_apply_never_touches_a_split_transaction(small, conn):
    run = ai.queue(conn)
    gid = next(r[0] for r in conn.execute("SELECT group_id, transaction_ids_json FROM ai_run_groups WHERE run_id = ?",
                                          (run["run_id"],)) if small["costco"] in json.loads(r[1]))
    group = {"group_id": gid}
    txmeta.split(conn, small["costco"], [{"category": "Groceries", "amount": 214.58}, {"category": "Other Shopping", "amount": 40}])
    lines = txmeta.split_lines(conn, [small["costco"]])
    summary = ai.apply_decisions(conn, {"run_id": run["run_id"], "decisions": [
        {"group_id": group["group_id"], "action": "set", "category": "Other Health", "confidence": 0.95, "note": "pharmacy"}]})
    assert {"group_id": group["group_id"], "transaction_id": small["costco"], "reason": "split by you"} in summary["skipped"]
    assert conn.execute("SELECT COUNT(*) FROM ai_reviews WHERE transaction_id = ?", (small["costco"],)).fetchone()[0] == 0
    assert txmeta.split_lines(conn, [small["costco"]]) == lines
    row = conn.execute("SELECT category, category_source, ai_verdict FROM v_transactions WHERE transaction_id = ?",
                       (small["costco"],)).fetchone()
    assert tuple(row) == ("Groceries", "user", None)
    # the refund in the same group was reviewed as asked
    assert conn.execute("SELECT category FROM v_transactions WHERE transaction_id = ?", (small["refund"],)).fetchone()[0] == "Other Health"
    later = ai.queue(conn)["run_id"]                                 # a split is the user's: never queued again
    assert all(small["costco"] not in json.loads(r[0]) for r in
               conn.execute("SELECT transaction_ids_json FROM ai_run_groups WHERE run_id = ?", (later,)))


# --- notes, tags, search ------------------------------------------------------------------------------------------

def test_notes_and_tags_are_saved_cleaned_and_shown_on_rows(small, conn):
    out = txmeta.set_meta(conn, small["costco"], {"note": "  paper towels, detergent  ", "tags": ["household", "#Trip", "trip ", "work  stuff"]})
    assert out == {"transaction_id": small["costco"], "note": "paper towels, detergent", "tags": ["household", "Trip", "work stuff"]}
    txmeta.set_meta(conn, small["amazon"], {"tags": ["TRIP"]})                 # the same tag, any case
    assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 3
    assert txmeta.all_tags(conn)[0] == {"name": "Trip", "count": 2}
    page = api.transactions(conn, {"month": "2026-09"}, TODAY)
    row = next(r for r in page["rows"] if r["transaction_id"] == small["costco"])
    assert row["note"] == "paper towels, detergent" and row["tags"] == ["household", "Trip", "work stuff"] and row["split"] is None
    txmeta.set_meta(conn, small["costco"], {"note": "", "tags": []})
    row = next(r for r in api.transactions(conn, {"month": "2026-09"}, TODAY)["rows"] if r["transaction_id"] == small["costco"])
    assert (row["note"], row["tags"]) == (None, [])


@pytest.mark.parametrize("data, message", [({}, "note or tags"), ({"note": 5}, "note must be text"), ({"note": "x" * 2001}, "up to 2000"),
                                           ({"tags": "trip"}, "list of names"), ({"tags": ["a,b"]}, "without commas"),
                                           ({"tags": [""]}, "can't be empty"), ({"tags": ["x" * 41]}, "up to 40"),
                                           ({"tags": [f"t{i}" for i in range(21)]}, "at most 20")])
def test_bad_notes_and_tags_are_refused(small, conn, data, message):
    with pytest.raises(txmeta.TxMetaError, match=message):
        txmeta.set_meta(conn, small["costco"], data)


def test_search_matches_notes_and_tags_and_a_tag_filters(small, conn, data_dir, capsys, monkeypatch):
    txmeta.set_meta(conn, small["amazon"], {"note": "birthday gift for Sam", "tags": ["gift"]})
    txmeta.set_meta(conn, small["costco"], {"tags": ["trip"]})
    assert [r["transaction_id"] for r in api.transactions(conn, {"q": "birthday"}, TODAY)["rows"]] == [small["amazon"]]
    assert [r["transaction_id"] for r in api.transactions(conn, {"q": "trip"}, TODAY)["rows"]] == [small["costco"]]
    assert [r["transaction_id"] for r in api.transactions(conn, {"tag": "GIFT"}, TODAY)["rows"]] == [small["amazon"]]
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    assert cli.main(["txns", "--search", "birthday", "--json"]) == 0
    assert [r["transaction_id"] for r in json.loads(capsys.readouterr().out)["rows"]] == [small["amazon"]]
    assert cli.main(["txns", "--tag", "trip", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert [r["transaction_id"] for r in got["rows"]] == [small["costco"]] and got["filters"]["tag"] == "trip"
    assert cli.main(["txns", "--tag", "a,b", "--json"]) == 2
    monkeypatch.setattr(tooldefs, "today", lambda: TODAY)
    tool = tooldefs.run_read("finance_transactions", {"search": "gift"}, config.db_path(), TODAY)
    assert [r["transaction_id"] for r in tool["rows"]] == [small["amazon"]] and tool["rows"][0]["tags"] == ["gift"]
    tool = tooldefs.run_read("finance_transactions", {"tag": "trip"}, config.db_path(), TODAY)
    assert [r["transaction_id"] for r in tool["rows"]] == [small["costco"]]


def test_a_category_filter_finds_a_split_by_its_lines(small, conn):
    txmeta.split(conn, small["costco"], [{"category": "Groceries", "amount": 214.58}, {"category": "Other Shopping", "amount": 40}])
    shop = [r["transaction_id"] for r in api.transactions(conn, {"category": "Other Shopping", "month": "2026-09"}, TODAY)["rows"]]
    groc = [r["transaction_id"] for r in api.transactions(conn, {"category": "Groceries", "month": "2026-09"}, TODAY)["rows"]]
    assert small["costco"] in shop and small["amazon"] in shop and small["costco"] in groc
    f_ = reads.parse_filters(conn, TODAY, category="other shopping", default_period="all")
    assert small["costco"] in [r["transaction_id"] for r in reads.txns(conn, f_, TODAY)["rows"]]
    row = next(r for r in reads.txns(conn, f_, TODAY)["rows"] if r["transaction_id"] == small["costco"])
    assert row["split"] == [{"category": "Groceries", "amount": 214.58}, {"category": "Other Shopping", "amount": 40.0}]


# --- saved filters ----------------------------------------------------------------------------------------------------

def test_saved_filters_are_kept_on_the_server_with_a_count(small, conn):
    txmeta.set_meta(conn, small["costco"], {"tags": ["trip"]})
    txmeta.set_meta(conn, small["amazon"], {"tags": ["trip"]})
    a = txmeta.save_filter(conn, "  Tagged:   trip ", {"tag": "#trip", "kind": "all", "q": ""})
    assert a == {"id": a["id"], "name": "Tagged: trip", "filters": {"tag": "trip"}}
    b = txmeta.save_filter(conn, "Costco spending", {"q": "costco", "kind": "spend", "category": "Groceries"})
    got = features.saved_filters(conn, {"month": "2026-09"}, TODAY)
    assert [(x["name"], x["count"]) for x in got["filters"]] == [("Tagged: trip", 2), ("Costco spending", 2)]
    assert [(x["name"], x["count"]) for x in features.saved_filters(conn, {"month": "2026-08"}, TODAY)["filters"]] == \
        [("Tagged: trip", 0), ("Costco spending", 0)]
    txmeta.delete_filter(conn, b["id"])
    assert [x["name"] for x in txmeta.saved_filters(conn)] == ["Tagged: trip"]


@pytest.mark.parametrize("name, filters, message", [
    ("", {"q": "x"}, "give the filter a name"), ("x" * 61, {"q": "x"}, "up to 60"), ("A", {}, "nothing to save"),
    ("A", {"kind": "all"}, "nothing to save"), ("A", {"month": "2026-09"}, "unknown filter"),
    ("A", {"kind": "everything"}, "kind must be"), ("A", {"category": "Food"}, "unknown category"), ("A", ["q"], "must be an object"),
    ("Tagged: trip", {"q": "y"}, "already")])
def test_bad_saved_filters_are_refused(small, conn, name, filters, message):
    txmeta.save_filter(conn, "Tagged: trip", {"tag": "trip"})
    with pytest.raises(txmeta.TxMetaError, match=message):
        txmeta.save_filter(conn, name, filters)


# --- the routes ---------------------------------------------------------------------------------------------------------

def test_the_routes(small, conn, client):
    app = App(config.db_path(), lambda: client)
    app.today = lambda: TODAY

    def call(method, path, body=None):
        status, _, raw = app.handle(method, path, POST if method == "POST" else LOCAL,
                                    json.dumps(body).encode() if body is not None else b"")
        return status, json.loads(raw)

    s, body = call("POST", "/api/transactions/split", {"transaction_id": small["costco"],
                                                       "lines": [{"category": "Groceries", "amount": 200}, {"category": "Other Shopping", "amount": 50}]})
    assert s == 400 and body == {"error": "the lines add up to $250, not $254.58: $4.58 left to split", "field": "lines"}
    s, body = call("POST", "/api/transactions/split", {"transaction_id": small["costco"],
                                                       "lines": [{"category": "Groceries", "amount": 200}, {"category": "Other Shopping", "amount": 54.58}]})
    assert s == 200 and body["split"][1] == {"category": "Other Shopping", "amount": 54.58}
    assert call("POST", "/api/transactions/category", {"transaction_id": small["costco"], "category": "Other Health"})[0] == 400
    assert call("POST", "/api/transactions/split", {"transaction_id": "nope", "lines": []})[0] == 404
    s, body = call("POST", "/api/transactions/meta", {"transaction_id": small["costco"], "note": "hi", "tags": ["trip"]})
    assert s == 200 and body == {"transaction_id": small["costco"], "note": "hi", "tags": ["trip"]}
    assert call("GET", "/api/tags")[1] == {"tags": [{"name": "trip", "count": 1}]}
    s, body = call("POST", "/api/saved-filters", {"name": "Trips", "filters": {"tag": "trip"}})
    assert s == 200
    assert call("GET", "/api/saved-filters?month=2026-09")[1]["filters"][0]["count"] == 1
    assert call("POST", "/api/saved-filters/delete", {"id": body["id"]})[0] == 200
    assert call("POST", "/api/saved-filters/delete", {"id": body["id"]})[0] == 404
    rows = call("GET", "/api/transactions?month=2026-09&tag=trip")[1]["rows"]
    assert [r["transaction_id"] for r in rows] == [small["costco"]] and rows[0]["split"] and rows[0]["note"] == "hi"


def test_the_spend_lines_view_counts_a_split_once(small, conn):
    txmeta.split(conn, small["costco"], [{"category": "Groceries", "amount": 214.58}, {"category": "Other Shopping", "amount": 40}])
    rows = conn.execute("SELECT transaction_id, split_line, category, spend FROM v_spend_lines WHERE transaction_id = ? "
                        "ORDER BY split_line", (small["costco"],)).fetchall()
    assert [tuple(r)[1:] for r in rows] == [(1, "Groceries", 214.58), (2, "Other Shopping", 40.0)]
    assert insights.spend_by(conn, "v.month = ?", ("2026-09",))[None] == (276.58, 4)   # costco (once), amazon, refund, pending

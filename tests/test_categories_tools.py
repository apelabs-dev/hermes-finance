"""v23 category agent tools: finance_categories (read, hidden reported separately), create / update / delete (writes,
approved before they run, and a declined card changes nothing), their plan summaries, argument validation, and the
plugin/MCP listings."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from agent_tools import ROOT, mcp_session, plugin_ctx, rows, table_hashes
from hermes_finance import catops, chat, config, mcp_state, tooldefs

TODAY = date(2026, 9, 28)
TS = "2026-08-01T00:00:00Z"
NEW = {"finance_categories", "finance_create_category", "finance_update_category", "finance_delete_category"}


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(tooldefs, "today", lambda: TODAY)


@pytest.fixture
def plugin():
    return plugin_ctx()


@pytest.fixture
def scene(conn):
    catops.create(conn, "Kids", group="Home", icon="tent")
    return config.db_path()


def test_the_four_tools_are_defined_and_the_plugin_lists_exactly_them(scene):
    assert NEW <= set(tooldefs.TOOLS)
    assert tooldefs.TOOLS["finance_categories"].write is False
    for name in ("finance_create_category", "finance_update_category", "finance_delete_category"):
        assert tooldefs.TOOLS[name].write is True
    manifest = (ROOT / "hermes_plugin" / "plugin.yaml").read_text().splitlines()
    start = manifest.index("provides_tools:")
    listed = []
    for line in manifest[start + 1:]:
        if not line.startswith("  - "):
            break
        listed.append(line[4:])
    assert listed == list(tooldefs.TOOLS)


def test_finance_categories_reads_and_changes_nothing(scene, conn):
    catops.set_hidden(conn, "Kids", True)
    before = table_hashes(config.db_path())
    p = tooldefs.run_read("finance_categories", {}, config.db_path(), TODAY)
    assert table_hashes(config.db_path()) == before
    kids = next(c for c in p["categories"] if c["name"] == "Kids")
    assert (kids["group"], kids["icon"], kids["built_in"], kids["hidden"], kids["monthly_cap"]) == ("Home", "tent", False, True, None)
    assert p["hidden"] == ["Kids"]
    assert "Kids" in {c["name"] for g in p["groups"] for c in g["categories"]}
    assert next(c for c in p["categories"] if c["name"] == "Coffee")["built_in"] is True


def test_create_is_described_in_plain_words_and_changes_nothing_until_applied(scene, conn, plugin):
    checked, summary = tooldefs.describe("finance_create_category",
                                         {"name": "Camp", "group": "leisure", "icon": "tent"}, config.db_path())
    assert checked["name"] == "Camp" and summary == "Add a spending category Camp in Leisure with the tent icon."
    assert rows(config.db_path(), "SELECT COUNT(*) FROM categories WHERE name = 'Camp'") == [(0,)]
    directive = plugin.call("finance_create_category", {"name": "Camp", "group": "leisure", "icon": "tent"})
    assert directive["directive"]["action"] == "approve"
    assert "Camp" in directive["directive"]["message"]
    assert rows(config.db_path(), "SELECT COUNT(*) FROM categories WHERE name = 'Camp'") == [(0,)]
    out = plugin.approved("finance_create_category", {"name": "Camp", "group": "leisure", "icon": "tent"})
    assert (out["name"], out["group"], out["icon"]) == ("Camp", "Leisure", "tent")


def test_create_refuses_a_duplicate_or_a_bad_argument_before_any_card(scene):
    for args in ({"name": "kids"}, {"name": "Camp", "group": "Pets"}, {"name": "Camp", "icon": "evil"}, {}):
        with pytest.raises(tooldefs.ToolError):
            tooldefs.describe("finance_create_category", args, config.db_path())


def test_update_summary_and_apply(scene, plugin):
    _, summary = tooldefs.describe("finance_update_category",
                                   {"name": "Kids", "rename": "Children", "icon": "cake", "hidden": True,
                                    "group": "Leisure"}, config.db_path())
    assert summary == ("For Kids: rename Kids to Children, change its icon to cake, hide it, move it to Leisure.")
    out = plugin.approved("finance_update_category",
                          {"name": "Kids", "rename": "Children", "icon": "cake", "hidden": True, "group": "Leisure"})
    assert (out["name"], out["icon"], out["hidden"], out["group"]) == ("Children", "cake", True, "Leisure")
    with pytest.raises(tooldefs.ToolError, match="income / transfer"):
        tooldefs.describe("finance_update_category", {"name": "Income", "hidden": True}, config.db_path())
    with pytest.raises(tooldefs.ToolError, match="nothing to change"):
        tooldefs.describe("finance_update_category", {"name": "Children"}, config.db_path())


def test_delete_summary_says_how_many_rows_in_how_many_places_move_where(scene, conn):
    catops.create(conn, "Children")
    conn.execute("INSERT INTO items (item_id, institution_name, access_token, status, created_at) "
                 "VALUES ('item_a', 'Chase', 'access-production-x', 'active', ?)", (TS,))
    conn.execute("INSERT INTO accounts (account_id, item_id, name, type, subtype, current_balance, updated_at) "
                 "VALUES ('acc_chk', 'item_a', 'Checking', 'depository', 'checking', 100, ?)", (TS,))
    conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, first_seen_at, "
                 "updated_at) VALUES ('t1', 'acc_chk', 'item_a', '2026-08-02', 'MART', 10, ?, ?)", (TS, TS))
    conn.execute("INSERT INTO classifications (transaction_id, category_id, source, updated_at) "
                 "SELECT 't1', id, 'user', ? FROM categories WHERE name = 'Kids'", (TS,))
    conn.execute("INSERT INTO rules (match_field, match_type, pattern, category_id, priority, created_from, created_at) "
                 "SELECT 'merchant_name', 'exact', 'Mart', id, 200, 'user', ? FROM categories WHERE name = 'Kids'", (TS,))
    conn.commit()
    checked, summary = tooldefs.describe("finance_delete_category", {"name": "Kids", "move_to": "Children"},
                                         config.db_path())
    assert summary == "Move 1 transaction and 1 rule from Kids to Children, then delete Kids."
    out = tooldefs.apply("finance_delete_category", checked, config.db_path(), TODAY)
    assert out["name"] == "Children" and out["deleted"] == "Kids"
    assert out["moved"]["classifications"] == 1 and out["moved"]["rules"] == 1
    assert rows(config.db_path(), "SELECT COUNT(*) FROM categories WHERE name = 'Kids'") == [(0,)]


def test_delete_summary_with_nothing_to_move_says_so_plainly(scene, conn):
    catops.create(conn, "Children")
    _, summary = tooldefs.describe("finance_delete_category", {"name": "Kids", "move_to": "Children"}, config.db_path())
    assert summary == "Delete Kids. Nothing to move."
    assert "Move nothing" not in summary


def test_delete_refuses_built_ins_and_a_missing_destination_before_any_card(scene):
    with pytest.raises(tooldefs.ToolError, match="built-in"):
        tooldefs.describe("finance_delete_category", {"name": "Coffee", "move_to": "Groceries"}, config.db_path())
    with pytest.raises(tooldefs.ToolError, match="move_to"):
        tooldefs.describe("finance_delete_category", {"name": "Kids"}, config.db_path())


def test_mcp_lists_the_read_always_and_the_writes_only_with_allow_writes(scene, conn):
    listed, _ = mcp_session(config.db_path(), [("finance_categories", {})])
    assert "finance_categories" in listed
    assert not NEW & {"finance_create_category", "finance_update_category", "finance_delete_category"} & set(listed)
    mcp_state.set_allow_writes(conn, True)
    listed, out = mcp_session(config.db_path(), [
        ("finance_create_category", {"name": "Camp", "icon": "tent"}),
        ("finance_categories", {}),
    ])
    assert {"finance_create_category", "finance_update_category", "finance_delete_category"} <= set(listed)
    pending = json.loads(out[0][1])
    assert pending["status"] == "pending_approval" and pending["applied"] is False
    assert rows(config.db_path(), "SELECT COUNT(*) FROM categories WHERE name = 'Camp'") == [(0,)]


CATEGORY_TABLES = ("categories", "classifications", "rules", "saved_filters", "settings")


@pytest.mark.parametrize("tool,args", [
    ("finance_create_category", {"name": "Camp", "group": "Leisure", "icon": "tent"}),
    ("finance_update_category", {"name": "Kids", "rename": "Children", "icon": "cake", "hidden": True}),
    ("finance_delete_category", {"name": "Kids", "move_to": "Camping"}),
])
def test_a_declined_card_says_so_and_changes_nothing(scene, conn, tool, args):
    """Deny through the app's own path (chat.decide, the Ask card's Deny; Settings' admin.pending_decide calls the same
    mcp_state.decide): the card reads "Declined, nothing changed" and no category table moved."""
    catops.create(conn, "Camping")
    conn.execute("INSERT INTO items (item_id, institution_name, access_token, status, created_at) "
                 "VALUES ('item_a', 'Chase', 'access-production-x', 'active', ?)", (TS,))
    conn.execute("INSERT INTO accounts (account_id, item_id, name, type, subtype, current_balance, updated_at) "
                 "VALUES ('acc_chk', 'item_a', 'Checking', 'depository', 'checking', 100, ?)", (TS,))
    conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, first_seen_at, "
                 "updated_at) VALUES ('t1', 'acc_chk', 'item_a', '2026-08-02', 'MART', 10, ?, ?)", (TS, TS))
    conn.execute("INSERT INTO classifications (transaction_id, category_id, source, updated_at) "
                 "SELECT 't1', id, 'user', ? FROM categories WHERE name = 'Kids'", (TS,))
    conn.execute("INSERT INTO rules (match_field, match_type, pattern, category_id, priority, created_from, created_at) "
                 "SELECT 'merchant_name', 'exact', 'Mart', id, 200, 'user', ? FROM categories WHERE name = 'Kids'", (TS,))
    conn.execute("INSERT INTO saved_filters (name, filters, created_at) VALUES ('Kids filter', ?, ?)",
                 (json.dumps({"category": "Kids"}), TS))
    conn.commit()
    catops.set_hidden(conn, "Coffee", True)
    checked, summary = tooldefs.describe(tool, args, config.db_path())
    action = mcp_state.record_pending(conn, tool, checked, summary, chat.CLIENT, origin=chat.ORIGIN)
    before = {t: h for t, h in table_hashes(config.db_path()).items() if t in CATEGORY_TABLES}
    app = type("App", (), {"db_path": config.db_path(), "today": staticmethod(lambda: TODAY)})()
    status, out = chat.decide(app, conn, {"id": action["id"], "approve": False})
    assert status == 200
    assert (out["card"]["status"], out["card"]["result_line"]) == ("rejected", "Declined, nothing changed")
    assert {t: h for t, h in table_hashes(config.db_path()).items() if t in CATEGORY_TABLES} == before
    assert rows(config.db_path(), "SELECT status FROM pending_actions WHERE id = ?", (action["id"],)) == [("rejected",)]
    # deny is final: a later Approve applies nothing
    status, out = chat.decide(app, conn, {"id": action["id"], "approve": True})
    assert status == 409 and out["card"]["status"] == "rejected"
    assert {t: h for t, h in table_hashes(config.db_path()).items() if t in CATEGORY_TABLES} == before

"""v19 (category names, design review 2026-10-21): a v18 database (tests/fixtures/schema_v18.sql) upgrades through the
app's own startup path. The catch-alls that shared their group's name are renamed by id (Transport -> Other Transport,
Shopping -> Other Shopping, Health -> Other Health), so every classification, rule, cap, split, rollover and map row
follows; the group Other is Misc and the category Other keeps its name; each group's catch-all is last; the new icons;
the saved filters and the chart's hidden categories follow the names; and Plaid's defaults are re-filed by today's map
(never a user's, a rule's or the AI's pick). A hand-made category that already has a new name stops the upgrade with a
clear error and nothing written (fail closed). Run again, or stopped half way and resumed, it changes nothing more."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_finance import classify, config, db, insights, txmeta

FIXTURES = Path(__file__).parent / "fixtures"
TS = "2026-08-01T00:00:00Z"
TAXONOMY = {group: [name for name, _ in cats] for group, _, cats in db.CATEGORY_GROUPS}

# transaction: (merchant, pfc primary, pfc detailed, v18 category, source)
ROWS = {
    "t_gas": ("Shell", "TRANSPORTATION", "TRANSPORTATION_GAS", "Transport", "plaid"),
    "t_park": ("SpotHero", "TRANSPORTATION", "TRANSPORTATION_PARKING", "Transport", "plaid"),
    "t_uber": ("Uber", "TRANSPORTATION", "TRANSPORTATION_TAXIS_AND_RIDE_SHARES", "Transport", "plaid"),
    "t_metro": ("Metro", "TRANSPORTATION", "TRANSPORTATION_PUBLIC_TRANSIT", "Transport", "plaid"),
    "t_toll": ("FasTrak", "TRANSPORTATION", "TRANSPORTATION_TOLLS", "Transport", "plaid"),   # no row of its own
    "t_shirt": ("Uniqlo", "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES", "Shopping", "plaid"),
    "t_amzn": ("Amazon", "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", "Shopping", "plaid"),
    "t_dentist": ("Smile Dental", "MEDICAL", "MEDICAL_DENTAL_CARE", "Health", "plaid"),
    "t_mystery": ("Mystery", "SOMETHING_NEW", "SOMETHING_NEW_ENTIRELY", "Travel", "plaid"),   # no map row at all
    "t_misc": ("Notary", "GENERAL_SERVICES", "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", "Other", "plaid"),
    "t_user": ("Chevron", "TRANSPORTATION", "TRANSPORTATION_GAS", "Transport", "user"),
    "t_rule": ("Arco", "TRANSPORTATION", "TRANSPORTATION_GAS", "Transport", "rule"),
    "t_ai": ("Mobil", "TRANSPORTATION", "TRANSPORTATION_GAS", "Transport", "ai"),
    "t_gone": ("Valero", "TRANSPORTATION", "TRANSPORTATION_GAS", "Transport", "detached"),   # its rule was deleted
}
# where each one is after v19
AFTER = {"t_gas": "Gas", "t_park": "Parking", "t_uber": "Rideshare", "t_metro": "Public Transit",
         "t_toll": "Other Transport", "t_shirt": "Clothing", "t_amzn": "Other Shopping", "t_dentist": "Doctor & Dentist",
         "t_mystery": "Travel", "t_misc": "Other", "t_user": "Other Transport", "t_rule": "Other Transport",
         "t_ai": "Other Transport", "t_gone": "Other Transport"}


def rows(conn, sql, *args):
    return [tuple(r) for r in conn.execute(sql, args)]


def v18_db(path: Path) -> sqlite3.Connection:
    """A v18 database with its data: the rows above, a cap and rollover on Transport, a split with a Shopping line, a
    group budget on Other, a hand-made category ("Kids", under Other), a saved filter and hidden chart categories that
    name the old names."""
    config.ensure_data_dir(path.parent)
    raw = sqlite3.connect(path)
    raw.executescript((FIXTURES / "schema_v18.sql").read_text())
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 18
    cid = lambda name: raw.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]   # noqa: E731
    raw.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                "VALUES ('item_a', 'ins_1', 'Chase', 'access-production-x', 'active', ?)", (TS,))
    raw.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, iso_currency, "
                "updated_at) VALUES ('acc_chk', 'item_a', 'Checking', '1234', 'depository', 'checking', 1000, 'USD', ?)", (TS,))
    raw.execute("INSERT INTO rules (match_field, match_type, pattern, category_id, priority, created_from, created_at) "
                "VALUES ('merchant_name', 'exact', 'Arco', ?, 200, 'user', ?)", (cid("Transport"), TS))
    rule_id = raw.execute("SELECT id FROM rules WHERE pattern = 'Arco'").fetchone()[0]
    for tid, (merchant, primary, detailed, cat, source) in ROWS.items():
        raw.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount, "
                    "pfc_primary, pfc_detailed, first_seen_at, updated_at) VALUES (?, 'acc_chk', 'item_a', '2026-08-02', ?, ?, "
                    "25, ?, ?, ?, ?)", (tid, merchant.upper(), merchant, primary, detailed, TS, TS))
        raw.execute("INSERT INTO classifications (transaction_id, category_id, source, rule_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)", (tid, cid(cat), "rule" if source == "detached" else source,
                                               rule_id if source == "rule" else None, TS))
    raw.execute("UPDATE categories SET monthly_cap = 300 WHERE name = 'Transport'")
    raw.execute("INSERT INTO category_rollover (category_id, enabled_at) VALUES (?, ?)", (cid("Transport"), TS))
    raw.execute("INSERT INTO transaction_splits (transaction_id, line, category_id, amount, created_at) "
                "VALUES ('t_user', 1, ?, 10, ?), ('t_user', 2, ?, 15, ?)", (cid("Shopping"), TS, cid("Gas"), TS))
    raw.execute("INSERT INTO group_budgets (group_id, amount, created_at, updated_at) "
                "SELECT id, 90, ?, ? FROM category_groups WHERE name = 'Other'", (TS, TS))
    raw.execute("INSERT INTO categories (name, is_income, is_transfer, group_id, sort) "
                "SELECT 'Kids', 0, 0, id, 1100 FROM category_groups WHERE name = 'Other'")
    raw.execute("INSERT INTO saved_filters (name, filters, created_at) VALUES ('Car', ?, ?), ('Plain', ?, ?)",
                (json.dumps({"category": ["Transport", "Gas"], "group": "Other", "q": "Transport"}), TS,
                 json.dumps({"q": "shell"}), TS))
    raw.execute("INSERT INTO settings (key, value, updated_at) VALUES ('chart_excluded_categories', ?, ?)",
                (json.dumps(["Shopping", "Coffee"]), TS))
    raw.commit()
    return raw


def categories_of(conn) -> dict[str, str]:
    return dict(rows(conn, "SELECT transaction_id, category FROM v_transactions ORDER BY transaction_id"))


def snapshot(conn) -> dict[str, list[tuple]]:
    return {t: rows(conn, f"SELECT * FROM {t} ORDER BY rowid") for t in
            ("categories", "category_groups", "plaid_category_map", "classifications", "rules", "transaction_splits",
             "category_rollover", "group_budgets", "saved_filters", "settings")}


def test_v18_to_v19_renames_by_id_and_refiles_only_plaid_defaults(data_dir):
    path = config.db_path()
    raw = v18_db(path)
    assert categories_of(raw) == {t: r[3] for t, r in ROWS.items()}
    ids = dict(rows(raw, "SELECT name, id FROM categories"))
    group_ids = dict(rows(raw, "SELECT name, id FROM category_groups"))
    old = {t: rows(raw, f"SELECT * FROM {t} ORDER BY rowid") for t in
           ("rules", "transaction_splits", "category_rollover", "group_budgets", "plaid_category_map")}
    not_plaid = rows(raw, "SELECT * FROM classifications WHERE source <> 'plaid' ORDER BY rowid")
    raw.close()

    conn = db.connect(path)   # the app's own startup path upgrades it
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
    # the names, each on its old id; the category Other keeps its name, in the group now called Misc
    now = dict(rows(conn, "SELECT name, id FROM categories"))
    assert {n: now[n] for n in ("Other Transport", "Other Shopping", "Other Health", "Other")} == \
        {"Other Transport": ids["Transport"], "Other Shopping": ids["Shopping"], "Other Health": ids["Health"],
         "Other": ids["Other"]}
    assert not {"Transport", "Shopping", "Health"} & set(now)
    assert dict(rows(conn, "SELECT name, id FROM category_groups"))["Misc"] == group_ids["Other"]
    assert [r[0] for r in rows(conn, "SELECT name FROM category_groups ORDER BY sort")] == list(TAXONOMY)
    assert rows(conn, "SELECT g.name FROM categories c JOIN category_groups g ON g.id = c.group_id WHERE c.name = 'Other'") \
        == [("Misc",)]
    # everything that points at a category or a group by id is as it was: the cap, rollover, the split's line, rules,
    # the group budget, the Plaid map (its catch-all rows now read the new names)
    assert old == {t: rows(conn, f"SELECT * FROM {t} ORDER BY rowid") for t in old}
    assert rows(conn, "SELECT monthly_cap FROM categories WHERE name = 'Other Transport'") == [(300,)]
    assert {k: classify.plaid_default(conn, None, k) for k in ("TRANSPORTATION", "GENERAL_MERCHANDISE", "HOME_IMPROVEMENT",
                                                              "MEDICAL", "PERSONAL_CARE", "GENERAL_SERVICES")} == \
        {k: now[v] for k, v in (("TRANSPORTATION", "Other Transport"), ("GENERAL_MERCHANDISE", "Other Shopping"),
                                ("HOME_IMPROVEMENT", "Other Shopping"), ("MEDICAL", "Other Health"),
                                ("PERSONAL_CARE", "Other Health"), ("GENERAL_SERVICES", "Other"))}
    # Plaid's defaults moved to today's map (detailed first, then primary); a user's, a rule's (a deleted one's too) and
    # the AI's picks never move, and a code the map doesn't know stays
    assert categories_of(conn) == AFTER
    assert rows(conn, "SELECT * FROM classifications WHERE source <> 'plaid' ORDER BY rowid") == not_plaid
    assert rows(conn, "SELECT transaction_id, updated_at FROM classifications WHERE updated_at = ? ORDER BY 1", TS) == \
        [(t, TS) for t in sorted({"t_toll", "t_amzn", "t_mystery", "t_misc", "t_user", "t_rule", "t_ai", "t_gone"})]
    # the order: each group's catch-all last, a hand-made one after the taxonomy's
    by_group = {}
    for g, c in rows(conn, "SELECT g.name, c.name FROM categories c JOIN category_groups g ON g.id = c.group_id "
                           "ORDER BY g.sort, c.sort, c.name"):
        by_group.setdefault(g, []).append(c)
    assert by_group == {**TAXONOMY, "Misc": TAXONOMY["Misc"] + ["Kids"]}
    assert [by_group[g][-1] for g in ("Transport", "Shopping", "Health")] == ["Other Transport", "Other Shopping",
                                                                              "Other Health"]
    # the icons: the route and the paint roller; Groceries keeps its cart
    assert dict(rows(conn, "SELECT name, icon FROM categories WHERE name IN "
                           "('Other Transport', 'Home Repair', 'Groceries', 'Other Shopping', 'Other Health', 'Other')")) == \
        {"Other Transport": "route", "Home Repair": "paint-roller", "Groceries": "shopping-cart",
         "Other Shopping": "shopping-bag", "Other Health": "heart-pulse", "Other": "ellipsis"}
    # what stores names follows them: the saved filter (its search text is the owner's words, kept) and the chart
    assert {f["name"]: f["filters"] for f in txmeta.saved_filters(conn)} == {
        "Car": {"category": ["Other Transport", "Gas"], "group": "Misc", "q": "Transport"}, "Plain": {"q": "shell"}}
    from hermes_finance import api
    assert api.chart_excluded(conn) == ["Other Shopping", "Coffee"]
    # and insights' own names still resolve
    assert insights.RECURRING_EXCLUDED <= set(now)

    done = snapshot(conn)
    db.init_schema(conn)                                          # again: nothing changes
    assert snapshot(conn) == done
    with conn:
        conn.execute("PRAGMA user_version = 18")                  # as if it stopped before v19 was written
    conn.close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 19
    assert snapshot(conn) == done
    # the next sync's classify agrees with where v19 put Plaid's defaults
    classify.apply(conn)
    assert {t: c for t, c in categories_of(conn).items() if t != "t_mystery"} == \
        {t: c for t, c in AFTER.items() if t != "t_mystery"}
    conn.close()


@pytest.mark.parametrize("taken", ["Other Transport", "other shopping"])
def test_a_hand_made_category_with_the_new_name_stops_the_upgrade_and_writes_nothing(data_dir, taken):
    """Fail closed: two categories the owner kept apart are never merged by an upgrade. The database stays at v18, every
    row as it was, and the error names both and says what to do."""
    path = config.db_path()
    raw = v18_db(path)
    raw.execute("INSERT INTO categories (name, is_income, is_transfer, monthly_cap) VALUES (?, 0, 0, 40)", (taken,))
    raw.commit()
    mine = raw.execute("SELECT id FROM categories WHERE name = ?", (taken,)).fetchone()[0]
    before = snapshot(raw)
    raw.close()
    with pytest.raises(db.SchemaError) as e:
        db.connect(path)
    old = "Transport" if taken == "Other Transport" else "Shopping"
    assert f"renames the category {old!r}" in str(e.value) and f"{taken!r} (id {mine}) already exists" in str(e.value)
    assert "nothing was changed" in str(e.value)
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 18
    assert snapshot(raw) == before
    # renamed by hand, the next start upgrades
    raw.execute("UPDATE categories SET name = 'Mine' WHERE id = ?", (mine,))
    raw.commit()
    raw.close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 19
    assert rows(conn, "SELECT name, monthly_cap FROM categories WHERE id = ?", mine) == [("Mine", 40)]
    assert categories_of(conn) == AFTER
    conn.close()


def test_a_misc_group_already_there_stops_the_upgrade(data_dir):
    path = config.db_path()
    raw = v18_db(path)
    raw.execute("INSERT INTO category_groups (name, sort, rgb) VALUES ('Misc', 9, '1,2,3')")
    raw.commit()
    raw.close()
    with pytest.raises(db.SchemaError, match="renames the category group 'Other'"):
        db.connect(path)
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 18
    assert raw.execute("SELECT COUNT(*) FROM categories WHERE name = 'Transport'").fetchone()[0] == 1
    raw.close()


def test_refile_plaid_defaults_on_a_new_database(conn):
    """The re-file on its own: a gas station filed under Other Transport by Plaid moves to Gas; a user's, a rule's and
    the AI's gas stations stay; a toll (no detailed row) stays on its primary's category."""
    conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                 "VALUES ('item_a', 'ins_1', 'Chase', 'access-production-x', 'active', ?)", (TS,))
    conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, iso_currency, "
                 "updated_at) VALUES ('acc_chk', 'item_a', 'Checking', '1234', 'depository', 'checking', 1000, 'USD', ?)", (TS,))
    transport = classify.category_id(conn, "Other Transport")
    for tid, detailed, source in (("p_gas", "TRANSPORTATION_GAS", "plaid"), ("p_toll", "TRANSPORTATION_TOLLS", "plaid"),
                                  ("u_gas", "TRANSPORTATION_GAS", "user"), ("r_gas", "TRANSPORTATION_GAS", "rule"),
                                  ("a_gas", "TRANSPORTATION_GAS", "ai")):
        conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount, "
                     "pfc_primary, pfc_detailed, first_seen_at, updated_at) VALUES (?, 'acc_chk', 'item_a', '2026-09-02', ?, ?, "
                     "40, 'TRANSPORTATION', ?, ?, ?)", (tid, tid.upper(), tid, detailed, TS, TS))
        conn.execute("INSERT INTO classifications (transaction_id, category_id, source, updated_at) VALUES (?, ?, ?, ?)",
                     (tid, transport, source, TS))
    conn.commit()
    with conn:
        assert db.refile_plaid_defaults(conn) == 1
    assert categories_of(conn) == {"p_gas": "Gas", "p_toll": "Other Transport", "u_gas": "Other Transport",
                                   "r_gas": "Other Transport", "a_gas": "Other Transport"}
    with conn:
        assert db.refile_plaid_defaults(conn) == 0

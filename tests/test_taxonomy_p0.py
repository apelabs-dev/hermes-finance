"""Polish P0 (schema v16): category groups and the 34-category taxonomy. A v15 database (tests/fixtures/schema_v15.sql)
upgrades to v16, twice, with every transaction in the category it had, a hand-made "Fun" filed under Leisure (never
dropped); the names insights.py and api.py hard-code still exist; Plaid's detailed codes that name a new category
outright file there on the next classify (never over a user's, a rule's or the AI's pick); /api/category-groups gives
the tree; and static/app.js, the icon template and the picker agree with db.py. Since v19 (test_category_names_v19) the
upgrade goes on to rename the catch-alls and re-file Plaid's defaults, and the taxonomy here is v19's."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path

from hermes_finance import api, classify, config, db, insights, web
from hermes_finance.web import App

FIXTURES = Path(__file__).parent / "fixtures"
SRC = Path(web.__file__).parent
ORIGINAL = ["Rent & Utilities", "Groceries", "Eating Out", "Transport", "Shopping", "Subscriptions", "Entertainment",
            "Health", "Travel", "Income", "Transfers", "Fees", "Other"]   # v1's 13, in their order (ids 1..13)
ORIGINAL_NOW = [db.V19_RENAMES.get(n, n) for n in ORIGINAL]   # v19: the three catch-alls renamed, ids kept
TAXONOMY = {group: [name for name, _ in cats] for group, _, cats in db.CATEGORY_GROUPS}
SPENDING = [name for cats in TAXONOMY.values() for name in cats]
ICONS = {name: icon for _, _, cats in db.CATEGORY_GROUPS for name, icon in cats}
TS = "2026-08-01T00:00:00Z"


def rows(conn, sql, *args):
    return [tuple(r) for r in conn.execute(sql, args)]


def snapshot(conn) -> dict[str, list[tuple]]:
    """Everything v16 writes or reads, in full."""
    return {t: rows(conn, f"SELECT * FROM {t} ORDER BY rowid") for t in
            ("categories", "category_groups", "plaid_category_map", "classifications", "rules")}


def v15_db(path: Path) -> sqlite3.Connection:
    """A v15 database as a sync leaves it: every row filed (classify.apply at v15) or decided. A Plaid default on a gas
    station (Transport at v15), a user's pick, a rule's, the AI's, a hand-made "Fun" category with a cap and a user row
    in it, and another hand-made one ("Kids")."""
    config.ensure_data_dir(path.parent)
    raw = sqlite3.connect(path)
    raw.executescript((FIXTURES / "schema_v15.sql").read_text())
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 15
    assert rows(raw, "SELECT name FROM categories ORDER BY id") == [(n,) for n in ORIGINAL]
    raw.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                "VALUES ('item_a', 'ins_1', 'Chase', 'access-production-x', 'active', ?)", (TS,))
    raw.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, iso_currency, "
                "updated_at) VALUES ('acc_chk', 'item_a', 'Checking', '1234', 'depository', 'checking', 1000, 'USD', ?)", (TS,))
    raw.execute("INSERT INTO categories (name, is_income, is_transfer, monthly_cap) VALUES ('Fun', 0, 0, 120)")
    raw.execute("INSERT INTO categories (name, is_income, is_transfer) VALUES ('Kids', 0, 0)")
    for tid, merchant, amount, primary, detailed in (
            ("t_gas", "Shell", 48.1, "TRANSPORTATION", "TRANSPORTATION_GAS"),
            ("t_coffee", "Blue Bottle", 6.5, "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE"),
            ("t_uber", "Uber", 21.0, "TRANSPORTATION", "TRANSPORTATION_TAXIS_AND_RIDE_SHARES"),
            ("t_car", "Zenith", 412.0, "LOAN_PAYMENTS", "LOAN_PAYMENTS_CAR_PAYMENT"),
            ("t_park", "SpotHero", 18.0, "TRANSPORTATION", "TRANSPORTATION_PARKING"),
            ("t_fun", "Bowlero", 40.0, "ENTERTAINMENT", "ENTERTAINMENT_OTHER_ENTERTAINMENT"),
            ("t_kids", "Gymboree", 30.0, "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE"),
            ("t_pay", "Orbitex", -3000.0, "INCOME", "INCOME_WAGES"),
            ("t_cvs", "CVS", 12.0, "MEDICAL", "MEDICAL_PHARMACIES_AND_SUPPLEMENTS")):
        raw.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount, "
                    "pfc_primary, pfc_detailed, first_seen_at, updated_at) VALUES (?, 'acc_chk', 'item_a', '2026-08-02', ?, ?, "
                    "?, ?, ?, ?, ?)", (tid, merchant.upper(), merchant, amount, primary, detailed, TS, TS))
    # a rule files Uber under Travel; the user filed SpotHero under Shopping, the Bowlero night under Fun and the Gymboree
    # one under Kids; the AI filed CVS under Health
    raw.execute("INSERT INTO rules (match_field, match_type, pattern, category_id, priority, created_from, created_at) "
                "SELECT 'merchant_name', 'exact', 'Uber', id, 100, 'user', ? FROM categories WHERE name = 'Travel'", (TS,))
    raw.execute("INSERT INTO classifications (transaction_id, category_id, source, rule_id, note, updated_at) "
                "SELECT 't_uber', category_id, 'rule', id, 'rule #' || id, ? FROM rules WHERE pattern = 'Uber'", (TS,))
    for tid, cat, source in (("t_park", "Shopping", "user"), ("t_fun", "Fun", "user"), ("t_kids", "Kids", "user"),
                             ("t_cvs", "Health", "ai")):
        raw.execute("INSERT INTO classifications (transaction_id, category_id, source, updated_at) "
                    "SELECT ?, id, ?, ? FROM categories WHERE name = ?", (tid, source, TS, cat))
    # the rest as v15's classify.apply filed them: Plaid's default, read through the v15 view
    raw.execute("INSERT INTO classifications (transaction_id, category_id, source, updated_at) "
                "SELECT transaction_id, category_id, 'plaid', ? FROM v_transactions "
                "WHERE transaction_id NOT IN (SELECT transaction_id FROM classifications)", (TS,))
    raw.commit()
    return raw


def categories_of(conn) -> dict[str, str]:
    return dict(rows(conn, "SELECT transaction_id, category FROM v_transactions ORDER BY transaction_id"))


def test_v15_to_v16_twice_keeps_every_transaction_in_its_category(data_dir):
    path = config.db_path()
    raw = v15_db(path)
    before = categories_of(raw)
    assert before == {"t_car": "Transport", "t_coffee": "Eating Out", "t_cvs": "Health", "t_fun": "Fun", "t_gas": "Transport",
                      "t_kids": "Kids", "t_park": "Shopping", "t_pay": "Income", "t_uber": "Travel"}
    old_categories = rows(raw, "SELECT id, name, is_income, is_transfer, monthly_cap FROM categories ORDER BY id")
    old_classifications = rows(raw, "SELECT * FROM classifications ORDER BY rowid")
    old_map = dict(rows(raw, "SELECT pfc_code, category_id FROM plaid_category_map"))
    raw.close()

    conn = db.connect(path)   # the app's own startup path upgrades it
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
    # every transaction keeps its category (v19's renames keep the ids), but for Plaid's defaults: v19 re-files them by
    # today's map, so the gas station, the coffee and the car payment land where a new sync would put them
    after = {**before, "t_cvs": "Other Health", "t_park": "Other Shopping",
             "t_gas": "Gas", "t_coffee": "Coffee", "t_car": "Car Payment"}
    assert categories_of(conn) == after
    plaid = {"t_gas", "t_coffee", "t_car", "t_pay"}
    got = rows(conn, "SELECT * FROM classifications ORDER BY rowid")
    assert [r for r in got if r[0] not in plaid] == [r for r in old_classifications if r[0] not in plaid]
    # every old category is there with every value it had (ids too, the three catch-alls renamed); the 23 new ones after
    assert rows(conn, "SELECT id, name, is_income, is_transfer, monthly_cap FROM categories WHERE id <= 15 ORDER BY id") \
        == [(i, db.V19_RENAMES.get(n, n), *rest) for i, n, *rest in old_categories]
    assert {r[0] for r in rows(conn, "SELECT name FROM categories WHERE id > 15")} == set(SPENDING) - set(ORIGINAL_NOW)
    # "Fun" is mapped to the Leisure group, never dropped; any other hand-made one files under Misc
    group = dict(rows(conn, "SELECT c.name, g.name FROM categories c LEFT JOIN category_groups g ON g.id = c.group_id"))
    assert group["Fun"] == "Leisure" and group["Kids"] == "Misc" and group["Income"] is None and group["Transfers"] is None
    assert all(group[n] == g for g, names in TAXONOMY.items() for n in names)
    assert rows(conn, "SELECT monthly_cap FROM categories WHERE name = 'Fun'") == [(120,)]
    # the map: v15's rows kept, but for the car payment's (still at its v15 default, so it moves to Car Payment)
    new_map = dict(rows(conn, "SELECT pfc_code, category_id FROM plaid_category_map"))
    assert {k: v for k, v in new_map.items() if k in old_map and k != "LOAN_PAYMENTS_CAR_PAYMENT"} == \
        {k: v for k, v in old_map.items() if k != "LOAN_PAYMENTS_CAR_PAYMENT"}
    assert classify.plaid_default(conn, "LOAN_PAYMENTS_CAR_PAYMENT", "LOAN_PAYMENTS") == classify.category_id(conn, "Car Payment")

    done = snapshot(conn)
    db.init_schema(conn)                                                          # again: nothing changes
    assert snapshot(conn) == done
    with conn:
        conn.execute("PRAGMA user_version = 15")                                  # as if it stopped before v16 was written
    conn.close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
    assert snapshot(conn) == done and categories_of(conn) == after

    # the next sync's classify finds Plaid's defaults already where it would put them; the user's, the rule's and the
    # AI's picks stay, and so does Income
    classify.apply(conn)
    assert categories_of(conn) == after
    conn.close()


def test_upgrade_keeps_a_car_payment_mapping_someone_changed(data_dir):
    path = config.db_path()
    raw = v15_db(path)
    raw.execute("UPDATE plaid_category_map SET category_id = (SELECT id FROM categories WHERE name = 'Transfers') "
                "WHERE pfc_code = 'LOAN_PAYMENTS_CAR_PAYMENT'")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    assert classify.plaid_default(conn, "LOAN_PAYMENTS_CAR_PAYMENT", "LOAN_PAYMENTS") == classify.category_id(conn, "Transfers")
    conn.close()


def test_a_category_given_a_group_or_icon_by_hand_keeps_it(data_dir):
    """The migration only fills what's empty: run again after a change, it changes nothing back."""
    conn = db.connect(config.db_path())
    with conn:
        conn.execute("UPDATE categories SET group_id = (SELECT id FROM category_groups WHERE name = 'Leisure'), icon = 'gift', "
                     "sort = 9 WHERE name = 'Education'")
    db.init_schema(conn)
    assert rows(conn, "SELECT g.name, c.icon, c.sort FROM categories c JOIN category_groups g ON g.id = c.group_id "
                      "WHERE c.name = 'Education'") == [("Leisure", "gift", 9)]
    conn.close()


def test_a_new_database_has_the_approved_taxonomy(conn):
    assert rows(conn, "SELECT name, sort, rgb FROM category_groups ORDER BY sort") == [
        ("Home", 1, "142,168,255"), ("Food", 2, "245,180,84"), ("Transport", 3, "95,198,232"), ("Shopping", 4, "245,158,139"),
        ("Bills", 5, "163,168,255"), ("Health", 6, "199,146,234"), ("Leisure", 7, "255,143,177"), ("Misc", 8, "155,162,182")]
    # v19: each group's catch-all is the last of it, and none shares its group's name
    assert TAXONOMY == {
        "Home": ["Rent & Utilities", "Internet & Phone", "Home Goods", "Home Repair"],
        "Food": ["Groceries", "Eating Out", "Coffee", "Alcohol & Bars"],
        "Transport": ["Gas", "Parking", "Rideshare", "Car Payment", "Car Maintenance", "Public Transit", "Other Transport"],
        "Shopping": ["Clothing", "Electronics", "Other Shopping"],
        "Bills": ["Subscriptions", "Insurance", "Fees"],
        "Health": ["Personal Care", "Gym", "Pharmacy", "Doctor & Dentist", "Other Health"],
        "Leisure": ["Entertainment", "Travel", "Hobbies", "Hotels"],
        "Misc": ["Gifts & Charity", "Pets", "Education", "Other"]}
    assert not set(TAXONOMY) & set(SPENDING)
    assert len(SPENDING) == len(set(SPENDING)) == 34 and len(set(ICONS.values())) == 34
    assert (ICONS["Other Transport"], ICONS["Home Repair"], ICONS["Groceries"]) == ("route", "paint-roller", "shopping-cart")
    # the 13 original names (v19's three renamed), with their ids; 23 new (34 in groups, Income and Transfers outside)
    assert rows(conn, "SELECT name FROM categories WHERE id <= 13 ORDER BY id") == [(n,) for n in ORIGINAL_NOW]
    assert len(set(SPENDING) - set(ORIGINAL_NOW)) == 23
    got = {r[0]: r[1:] for r in rows(conn, "SELECT c.name, g.name, c.icon, c.sort, c.is_income, c.is_transfer FROM categories c "
                                           "LEFT JOIN category_groups g ON g.id = c.group_id")}
    assert got == {**{n: (g, ICONS[n], i, 0, 0) for g, names in TAXONOMY.items() for i, n in enumerate(names, 1)},
                   "Income": (None, "arrow-down-left", 1010, 1, 0), "Transfers": (None, "arrow-left-right", 1011, 0, 1)}


def test_the_names_insights_and_api_hard_code_still_resolve(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM categories")}
    used = (set(insights.FIXED_CATEGORIES) | insights.RECURRING_EXCLUDED | insights.NO_SUGGEST
            | set(insights.NO_SUGGEST_REASONS) | {"Other"})
    # and every category name compared in their SQL or code (category = 'X', category == "X")
    for mod in ("insights.py", "api.py", "classify.py"):
        used |= set(re.findall(r"""category\s*==?\s*['"]([^'"]+)['"]""", (SRC / mod).read_text()))
    used |= set(re.findall(r"""category_id\(conn, ["']([^"']+)["']\)""", (SRC / "classify.py").read_text()))
    assert {"Rent & Utilities", "Subscriptions", "Groceries", "Eating Out", "Other Shopping", "Income", "Transfers", "Other",
            "Fees"} <= used
    assert used <= names, used - names
    assert all(classify.category_id(conn, n) for n in used)
    assert set(insights.NO_SUGGEST) == set(insights.NO_SUGGEST_REASONS)


# Plaid detailed code -> where a new database files it (primary in brackets: what a code with no row of its own reads)
PFC = {
    ("TRANSPORTATION_GAS", "TRANSPORTATION"): "Gas",
    ("TRANSPORTATION_PARKING", "TRANSPORTATION"): "Parking",
    ("TRANSPORTATION_TAXIS_AND_RIDE_SHARES", "TRANSPORTATION"): "Rideshare",
    ("TRANSPORTATION_PUBLIC_TRANSIT", "TRANSPORTATION"): "Public Transit",
    ("FOOD_AND_DRINK_COFFEE", "FOOD_AND_DRINK"): "Coffee",
    ("FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR", "FOOD_AND_DRINK"): "Alcohol & Bars",
    ("LOAN_PAYMENTS_CAR_PAYMENT", "LOAN_PAYMENTS"): "Car Payment",
    ("GENERAL_SERVICES_AUTOMOTIVE", "GENERAL_SERVICES"): "Car Maintenance",
    ("GENERAL_SERVICES_INSURANCE", "GENERAL_SERVICES"): "Insurance",
    ("GENERAL_SERVICES_EDUCATION", "GENERAL_SERVICES"): "Education",
    ("GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES", "GENERAL_MERCHANDISE"): "Clothing",
    ("GENERAL_MERCHANDISE_ELECTRONICS", "GENERAL_MERCHANDISE"): "Electronics",
    ("GENERAL_MERCHANDISE_PET_SUPPLIES", "GENERAL_MERCHANDISE"): "Pets",
    ("GENERAL_MERCHANDISE_GIFTS_AND_NOVELTIES", "GENERAL_MERCHANDISE"): "Gifts & Charity",
    ("GENERAL_MERCHANDISE_PHARMACIES_AND_SUPPLEMENTS", "GENERAL_MERCHANDISE"): "Pharmacy",
    ("MEDICAL_PHARMACIES_AND_SUPPLEMENTS", "MEDICAL"): "Pharmacy",
    ("MEDICAL_DENTAL_CARE", "MEDICAL"): "Doctor & Dentist",
    ("MEDICAL_PRIMARY_CARE", "MEDICAL"): "Doctor & Dentist",
    ("MEDICAL_VETERINARY_SERVICES", "MEDICAL"): "Pets",
    ("PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS", "PERSONAL_CARE"): "Gym",
    ("PERSONAL_CARE_HAIR_AND_BEAUTY", "PERSONAL_CARE"): "Personal Care",
    ("PERSONAL_CARE_LAUNDRY_AND_DRY_CLEANING", "PERSONAL_CARE"): "Personal Care",
    ("HOME_IMPROVEMENT_FURNITURE", "HOME_IMPROVEMENT"): "Home Goods",
    ("HOME_IMPROVEMENT_REPAIR_AND_MAINTENANCE", "HOME_IMPROVEMENT"): "Home Repair",
    ("RENT_AND_UTILITIES_INTERNET_AND_CABLE", "RENT_AND_UTILITIES"): "Internet & Phone",
    ("RENT_AND_UTILITIES_TELEPHONE", "RENT_AND_UTILITIES"): "Internet & Phone",
    ("TRAVEL_LODGING", "TRAVEL"): "Hotels",
    ("GOVERNMENT_AND_NON_PROFIT_DONATIONS", "GOVERNMENT_AND_NON_PROFIT"): "Gifts & Charity",
    # codes that could mean two categories keep their primary's (and the rest as before)
    ("HOME_IMPROVEMENT_HARDWARE", "HOME_IMPROVEMENT"): "Other Shopping",
    ("TRANSPORTATION_TOLLS", "TRANSPORTATION"): "Other Transport",
    ("TRANSPORTATION_BIKES_AND_SCOOTERS", "TRANSPORTATION"): "Other Transport",
    ("MEDICAL_OTHER_MEDICAL", "MEDICAL"): "Other Health",
    ("FOOD_AND_DRINK_RESTAURANT", "FOOD_AND_DRINK"): "Eating Out",
    ("FOOD_AND_DRINK_GROCERIES", "FOOD_AND_DRINK"): "Groceries",
    ("RENT_AND_UTILITIES_GAS_AND_ELECTRICITY", "RENT_AND_UTILITIES"): "Rent & Utilities",
    ("LOAN_PAYMENTS_MORTGAGE_PAYMENT", "LOAN_PAYMENTS"): "Rent & Utilities",
    ("TRAVEL_FLIGHTS", "TRAVEL"): "Travel",
}


def test_plaid_detailed_codes_file_under_the_new_categories(conn):
    name = dict(rows(conn, "SELECT id, name FROM categories"))
    got = {k: name[classify.plaid_default(conn, *k)] for k in PFC}
    assert got == PFC
    # every default the map names is a category, and nothing maps to a group's name that isn't one
    assert set(db.DEFAULT_PFC_MAP.values()) <= set(name.values())
    assert rows(conn, "SELECT COUNT(*) FROM plaid_category_map") == [(len(db.DEFAULT_PFC_MAP),)]


def test_apply_moves_only_plaid_defaults_never_a_users_rules_or_the_ais(conn):
    conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                 "VALUES ('item_a', 'ins_1', 'Chase', 'access-production-x', 'active', ?)", (TS,))
    conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, iso_currency, "
                 "updated_at) VALUES ('acc_chk', 'item_a', 'Checking', '1234', 'depository', 'checking', 1000, 'USD', ?)", (TS,))
    for tid, merchant in (("g1", "Shell"), ("g2", "Chevron"), ("g3", "Arco"), ("g4", "Mobil")):
        conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount, "
                     "pfc_primary, pfc_detailed, first_seen_at, updated_at) VALUES (?, 'acc_chk', 'item_a', '2026-09-02', ?, ?, "
                     "40, 'TRANSPORTATION', 'TRANSPORTATION_GAS', ?, ?)", (tid, merchant.upper(), merchant, TS, TS))
    conn.commit()
    classify.apply(conn)
    classify.set_category(conn, "g2", "Travel", "user")
    classify.set_category(conn, "g3", "Other Health", "ai", 0.9)
    with conn:
        conn.execute("INSERT INTO rules (match_field, match_type, pattern, category_id, priority, created_from, created_at) "
                     "SELECT 'merchant_name', 'exact', 'Mobil', id, 100, 'user', ? FROM categories WHERE name = 'Other Transport'", (TS,))
    classify.apply(conn)
    assert categories_of(conn) == {"g1": "Gas", "g2": "Travel", "g3": "Other Health", "g4": "Other Transport"}


def http_get(app, path):
    status, ctype, body = app.handle("GET", path, {"Host": "127.0.0.1:8765"}, b"")
    assert status == 200 and ctype == "application/json"
    return json.loads(body)


def test_the_api_gives_the_groups_and_each_categorys_group(conn, client):
    with conn:
        conn.execute("INSERT INTO categories (name, is_income, is_transfer, group_id, icon, sort) VALUES ('Kids', 0, 0, "
                     "(SELECT id FROM category_groups WHERE name = 'Misc'), NULL, 1100)")
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    app.today = lambda: date(2026, 9, 25)
    tree = http_get(app, "/api/category-groups")
    assert [(g["name"], g["rgb"]) for g in tree["groups"]] == [(g, rgb) for g, rgb, _ in db.CATEGORY_GROUPS]
    assert {g["name"]: [c["name"] for c in g["categories"]] for g in tree["groups"]} == \
        {**TAXONOMY, "Misc": TAXONOMY["Misc"] + ["Kids"]}
    assert [c["name"] for c in tree["ungrouped"]] == ["Income", "Transfers"]
    coffee = tree["groups"][1]["categories"][2]
    assert coffee == {"name": "Coffee", "is_income": False, "is_transfer": False, "spending": True, "monthly_cap": None,
                      "group": "Food", "icon": "coffee", "rgb": "245,180,84"}
    assert tree["ungrouped"][0] == {"name": "Income", "is_income": True, "is_transfer": False, "spending": False,
                                    "monthly_cap": None, "group": None, "icon": "arrow-down-left", "rgb": None}
    flat = http_get(app, "/api/categories")
    assert [c["name"] for c in flat] == sorted(SPENDING + ["Income", "Transfers", "Kids"])
    assert {c["name"]: c for c in flat}["Coffee"] == coffee
    assert {c["name"]: c["group"] for c in flat}["Kids"] == "Misc"
    assert api.GET_ROUTES["/api/category-groups"] is api.category_groups


def js_groups() -> list[tuple[str, str, list[tuple[str, str]]]]:
    """static/app.js's CAT_GROUPS, read from its source."""
    src = (SRC / "static" / "app.js").read_text()
    block = re.search(r"const CAT_GROUPS = \[\n(.*?)\n  \];", src, re.S).group(1)
    out = []
    for m in re.finditer(r"\['([^']+)', '([\d,]+)', \{(.*?)\}\]", block, re.S):
        out.append((m.group(1), m.group(2), re.findall(r"'([^']+)': '([^']+)'", m.group(3))))
    return out


def test_app_js_carries_the_same_groups_colours_and_icons():
    assert js_groups() == [(g, rgb, list(cats)) for g, rgb, cats in db.CATEGORY_GROUPS]


def test_every_category_icon_is_a_real_lucide_icon_in_the_template(conn, client):
    used = list(ICONS.values()) + list(db.UNGROUPED_ICONS.values()) + ["funnel", "house"]
    assert all(n in web.ICONS for n in used), [n for n in used if n not in web.ICONS]
    bodies = [web.ICONS[n] for n in ICONS.values()]
    assert len(set(bodies)) == 34                                   # 34 different drawings, not just names
    assert "home" not in web.ICONS                                  # the hand-drawn one is gone: Lucide's house instead
    assert web.ICONS["funnel"].startswith('<path d="M10 20a1 1 0 0 0 .553.895l2 1A1 1 0 0 0 14 21v-7')
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    status, _, html = app.handle("GET", "/", {"Host": "127.0.0.1:8765"}, b"")
    html = html.decode() if isinstance(html, bytes) else html
    assert all(f'data-icon="{n}"' in html for n in used)
    assert '<script src="/static/catpick.js" defer></script>' in html and 'href="/static/catpick.css"' in html
    # nothing on the page asks for the old icon
    for f in ("app.js", "txmeta.js", "catpick.js", "goals.js", "bills.js", "billcal.js", "settings.js", "setup.js"):
        src = (SRC / "static" / f).read_text()
        assert not re.search(r"""(icon|glyph|ibadge|opt)\(\s*'home'""", src), f

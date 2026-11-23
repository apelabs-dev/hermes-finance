"""v24: categories.colour. NULL = the icon follows its group; otherwise one of the eight swatches the goal picker uses
(goals.COLOURS). Checked on the server at create and update, returned (with the effective colour) by every category
read, and no number moves."""

from __future__ import annotations

import json
from datetime import date

import pytest

from hermes_finance import catops, config, goals, insights
from hermes_finance.web import App

TODAY = date(2026, 9, 28)
LOCAL = {"Host": "127.0.0.1:8765"}
JSONH = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
SWATCHES = ("#4fd1a5", "#8ea8ff", "#5fc6e8", "#c792ea", "#f5b454", "#ff8fb1", "#ff7b7b", "#9ba2b6")
PLAIN = "Pick a colour from the list."


def call(app, method, path, body=None):
    headers = JSONH if method == "POST" else LOCAL
    status, _ctype, out, _ = app.respond(method, path, headers, json.dumps(body).encode() if body is not None else b"")
    return status, json.loads(out)


@pytest.fixture
def app(conn):
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


def stored(conn, name):
    return conn.execute("SELECT colour FROM categories WHERE name = ?", (name,)).fetchone()[0]


def test_the_swatches_are_the_goal_pickers_eight_unchanged():
    assert goals.COLOURS == SWATCHES and catops.COLOURS is goals.COLOURS


def test_create_with_a_colour_stores_it_and_without_stores_null(conn):
    made = catops.create(conn, "Kids", colour="#c792ea")
    assert made["colour"] == "#c792ea" and made["effective_colour"] == "#c792ea" and stored(conn, "Kids") == "#c792ea"
    plain = catops.create(conn, "Games")
    assert plain["colour"] is None and stored(conn, "Games") is None
    assert plain["effective_colour"].startswith("#") and len(plain["effective_colour"]) == 7     # its group's (Misc)


def test_the_effective_colour_is_the_override_else_the_groups(conn):
    groups = {g["name"]: g["rgb"] for g in catops.payload(conn, TODAY)["groups"] if g["rgb"]}
    first = next(c for c in catops.payload(conn, TODAY)["categories"] if c["group"] and not c["colour"])
    r, g, b = (int(x) for x in groups[first["group"]].split(","))
    assert first["effective_colour"] == f"#{r:02x}{g:02x}{b:02x}"
    catops.set_colour(conn, first["name"], "#ff7b7b")
    again = next(c for c in catops.payload(conn, TODAY)["categories"] if c["name"] == first["name"])
    assert again["colour"] == "#ff7b7b" and again["effective_colour"] == "#ff7b7b"
    income = next(c for c in catops.payload(conn, TODAY)["categories"] if c["is_income"])
    assert income["colour"] is None and income["effective_colour"] is None      # no group, no override: nothing to say


@pytest.mark.parametrize("bad", ["#123456", "red", "", "#4FD1A5 ", 5, ["#4fd1a5"], "4fd1a5", {"a": 1}])
def test_an_invalid_colour_is_refused_with_plain_words_and_writes_nothing(conn, bad):
    with pytest.raises(catops.CategoryError) as e:
        catops.create(conn, "Kids", colour=bad)
    assert str(e.value) == PLAIN and e.value.field == "colour" and e.value.status == 400
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE name = 'Kids'").fetchone()[0] == 0


def test_a_colour_is_matched_case_insensitively_and_stored_as_the_swatch(conn):
    assert catops.create(conn, "Kids", colour="#C792EA")["colour"] == "#c792ea"
    assert stored(conn, "Kids") == "#c792ea"


def test_update_sets_changes_and_clears_a_colour_and_leaves_it_alone_otherwise(conn):
    catops.create(conn, "Kids")
    assert catops.update(conn, "Kids", colour="#5fc6e8")["colour"] == "#5fc6e8"
    assert catops.update(conn, "Kids", icon="tent")["colour"] == "#5fc6e8"             # not given: unchanged
    assert catops.update(conn, "Kids", rename_to="Little ones")["colour"] == "#5fc6e8"
    assert catops.update(conn, "Little ones", colour="#f5b454")["colour"] == "#f5b454"
    cleared = catops.update(conn, "Little ones", colour=None)                              # given as None: follow the group
    assert cleared["colour"] is None and stored(conn, "Little ones") is None
    assert catops.update(conn, "Little ones", icon="tent")["colour"] is None
    built_in = catops.update(conn, "Coffee", colour="#ff8fb1")                             # a built-in can have one too
    assert built_in["colour"] == "#ff8fb1" and built_in["built_in"] is True
    with pytest.raises(catops.CategoryError):
        catops.update(conn, "Coffee", colour="#000000")
    assert stored(conn, "Coffee") == "#ff8fb1"


def test_a_failed_update_changes_nothing_at_all(conn):
    catops.create(conn, "Kids")
    with pytest.raises(catops.CategoryError):
        catops.update(conn, "Kids", icon="tent", colour="nope")
    row = conn.execute("SELECT icon, colour FROM categories WHERE name = 'Kids'").fetchone()
    assert row["colour"] is None and row["icon"] != "tent"


def test_the_create_route_takes_a_colour_and_refuses_a_bad_one_by_field(app, conn):
    status, made = call(app, "POST", "/api/categories/create", {"name": "Kids", "colour": "#8ea8ff"})
    assert status == 200 and made["colour"] == "#8ea8ff" and made["effective_colour"] == "#8ea8ff"
    status, made = call(app, "POST", "/api/categories/create", {"name": "Games"})
    assert status == 200 and made["colour"] is None
    status, body = call(app, "POST", "/api/categories/create", {"name": "Toys", "colour": "#010203"})
    assert status == 400 and body["error"] == PLAIN and body["field"] == "colour"
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE name = 'Toys'").fetchone()[0] == 0


def test_the_update_route_sets_clears_and_refuses(app):
    call(app, "POST", "/api/categories/create", {"name": "Kids"})
    status, body = call(app, "POST", "/api/categories/update", {"name": "Kids", "colour": "#ff7b7b"})
    assert status == 200 and body["colour"] == "#ff7b7b"
    status, body = call(app, "POST", "/api/categories/update", {"name": "Kids", "hidden": True})
    assert status == 200 and body["colour"] == "#ff7b7b"                                 # absent key: untouched
    status, body = call(app, "POST", "/api/categories/update", {"name": "Kids", "colour": None})
    assert status == 200 and body["colour"] is None                                       # present as null: cleared
    status, body = call(app, "POST", "/api/categories/update", {"name": "Kids", "colour": "#abcdef"})
    assert status == 400 and body["error"] == PLAIN and body["field"] == "colour"


def test_every_category_read_carries_colour_and_effective_colour(app, conn):
    catops.create(conn, "Kids", group="Home", colour="#c792ea")
    status, cats = call(app, "GET", "/api/categories")
    kids = next(c for c in cats if c["name"] == "Kids")
    assert (kids["colour"], kids["effective_colour"]) == ("#c792ea", "#c792ea")
    assert all({"colour", "effective_colour"} <= set(c) for c in cats)
    status, groups = call(app, "GET", "/api/category-groups")
    every = [c for g in groups["groups"] for c in g["categories"]] + groups["ungrouped"]
    assert all({"colour", "effective_colour"} <= set(c) for c in every)
    assert next(c for c in every if c["name"] == "Kids")["colour"] == "#c792ea"
    status, page = call(app, "GET", "/api/categories/page")
    assert page["colours"] == list(SWATCHES)
    assert all({"colour", "effective_colour"} <= set(c) for c in page["categories"])
    assert next(c for c in page["categories"] if c["name"] == "Kids")["effective_colour"] == "#c792ea"
    assert catops.text(page)                                                              # the CLI's lines still render


def test_a_colour_moves_no_number(app, conn):
    catops.create(conn, "Kids")
    conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                 "VALUES ('i', 'x', 'Bank', 't', 'active', '2026-09-01T00:00:00Z')")
    conn.execute("INSERT INTO accounts (account_id, item_id, name, type, subtype, current_balance, iso_currency, "
                 "updated_at) VALUES ('a', 'i', 'Checking', 'depository', 'checking', 500, 'USD', '2026-09-01T00:00:00Z')")
    conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, first_seen_at, "
                 "updated_at) VALUES ('t', 'a', 'i', '2026-09-10', 'Toys', 42.5, '2026-09-10T00:00:00Z', "
                 "'2026-09-10T00:00:00Z')")
    conn.execute("INSERT INTO classifications (transaction_id, category_id, source, updated_at) "
                 "SELECT 't', id, 'user', '2026-09-10T00:00:00Z' FROM categories WHERE name = 'Kids'")
    conn.commit()

    def numbers():
        page = catops.payload(conn, TODAY)
        strip = lambda c: {k: v for k, v in c.items() if k not in ("colour", "effective_colour")}   # noqa: E731
        return ([strip(c) for c in page["categories"]], insights.net_worth(conn, today=TODAY)["net"],
                insights.spend_by(conn, "v.month = ?", ("2026-09",), "v.category_id"))

    before = numbers()
    catops.set_colour(conn, "Kids", "#ff8fb1")
    assert numbers() == before
    assert next(c for c in catops.payload(conn, TODAY)["categories"] if c["name"] == "Kids")["spend"] == 42.5


def test_deleting_a_category_takes_its_colour_with_it(conn):
    catops.create(conn, "Kids", colour="#c792ea")
    catops.delete(conn, "Kids", "Coffee")
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE name = 'Kids'").fetchone()[0] == 0
    assert stored(conn, "Coffee") is None

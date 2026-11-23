"""v23 category API: the one icons GET, the Settings page read, the three POST routes and their 200/400 shapes with the
offending field named (api.ApiError.field, so the sheet can show it inline)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from hermes_finance import api, catops, config, features
from hermes_finance.web import App

TODAY = date(2026, 9, 28)
LOCAL = {"Host": "127.0.0.1:8765"}
JSONH = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}


def call(app, method, path, body=None, headers=LOCAL):
    status, _ctype, out, _ = app.respond(method, path, headers, json.dumps(body).encode() if body is not None else b"")
    return status, json.loads(out)


@pytest.fixture
def app(conn):
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


def test_the_routes_are_registered_once():
    assert "/api/categories/icons" in api.GET_ROUTES
    assert "/api/categories/page" in api.GET_ROUTES
    for route in ("/api/categories/create", "/api/categories/update", "/api/categories/delete"):
        assert route in features.POST_ROUTES


def test_the_icons_read_is_the_shared_set(app):
    status, p = call(app, "GET", "/api/categories/icons")
    assert status == 200
    assert p["count"] == 100 and len(p["groups"]) == 8
    assert [g["name"] for g in p["groups"]][:2] == ["Money", "Home"]
    assert p["common"]["categories"][0] == "utensils" and len(p["common"]["goals"]) == 16


def test_categories_keep_returning_every_row_with_the_flags(app, conn):
    catops.create(conn, "Kids")
    catops.set_hidden(conn, "Kids", True)
    status, cats = call(app, "GET", "/api/categories")
    assert status == 200
    kids = next(c for c in cats if c["name"] == "Kids")
    assert (kids["built_in"], kids["hidden"], kids["group"], kids["spending"]) == (False, True, "Misc", True)
    assert next(c for c in cats if c["name"] == "Coffee")["built_in"] is True
    status, groups = call(app, "GET", "/api/category-groups")
    assert status == 200
    assert any(c["name"] == "Kids" and c["hidden"] for g in groups["groups"] for c in g["categories"])


def test_the_settings_page_read_has_groups_spend_and_counts(app, conn):
    catops.create(conn, "Kids", group="Home", icon="tent")
    status, p = call(app, "GET", "/api/categories/page")
    assert status == 200
    assert p["month"] == "2026-09"
    assert p["counts"] == {"total": 37, "builtin": 36, "user": 1, "hidden": 0, "spending": 35}
    kids = next(c for c in p["categories"] if c["name"] == "Kids")
    assert (kids["group"], kids["icon"], kids["spend"]) == ("Home", "tent", 0.0)
    assert kids["transactions"] == 0
    assert all(isinstance(c["transactions"], int) for c in p["categories"])
    assert [g["name"] for g in p["groups"]][-1] == "Income & transfers"


def test_create_returns_the_category_and_a_bad_field_names_itself(app):
    status, created = call(app, "POST", "/api/categories/create",
                           {"name": "Kids", "group": "home", "icon": "tent"}, JSONH)
    assert status == 200
    assert (created["name"], created["group"], created["icon"], created["built_in"]) == ("Kids", "Home", "tent", False)
    status, err = call(app, "POST", "/api/categories/create", {"name": "kids"}, JSONH)
    assert status == 400 and err["field"] == "name" and "already" in err["error"]
    status, err = call(app, "POST", "/api/categories/create", {"name": "Camp", "group": "Pets"}, JSONH)
    assert status == 400 and err["field"] == "group"
    status, err = call(app, "POST", "/api/categories/create", {"name": "Camp", "icon": "evil"}, JSONH)
    assert status == 400 and err["field"] == "icon"
    status, err = call(app, "POST", "/api/categories/create", {}, JSONH)
    assert status == 400 and err["field"] == "name"


def test_update_changes_only_what_is_given(app):
    call(app, "POST", "/api/categories/create", {"name": "Kids"}, JSONH)
    status, out = call(app, "POST", "/api/categories/update", {"name": "Kids", "rename": "Children"}, JSONH)
    assert status == 200 and out["name"] == "Children"
    status, out = call(app, "POST", "/api/categories/update", {"name": "Children", "icon": "cake", "hidden": True}, JSONH)
    assert status == 200 and (out["icon"], out["hidden"], out["group"]) == ("cake", True, "Misc")
    status, out = call(app, "POST", "/api/categories/update", {"name": "Children", "group": "Leisure"}, JSONH)
    assert status == 200 and out["group"] == "Leisure"
    status, err = call(app, "POST", "/api/categories/update", {"name": "Coffee", "rename": "Espresso"}, JSONH)
    assert status == 400 and "built-in" in err["error"]
    status, err = call(app, "POST", "/api/categories/update", {"name": "Nope", "icon": "cake"}, JSONH)
    assert status == 400 and "unknown category" in err["error"]


def test_delete_needs_a_destination_and_returns_what_moved(app, conn):
    for name in ("Kids", "Children"):
        call(app, "POST", "/api/categories/create", {"name": name}, JSONH)
    status, err = call(app, "POST", "/api/categories/delete", {"name": "Kids"}, JSONH)
    assert status == 400 and err["field"] == "move_to"
    status, err = call(app, "POST", "/api/categories/delete", {"name": "Kids", "move_to": "Kids"}, JSONH)
    assert status == 400 and err["field"] == "move_to"
    status, out = call(app, "POST", "/api/categories/delete", {"name": "Kids", "move_to": "Children"}, JSONH)
    assert status == 200 and out["deleted"] == "Kids" and out["name"] == "Children"
    assert set(out["moved"]) == {"classifications", "rules", "plaid_category_map", "ai_reviews", "transaction_splits",
                                 "category_rollover", "rule_deletions", "pending_actions"}
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE name = 'Kids'").fetchone()[0] == 0

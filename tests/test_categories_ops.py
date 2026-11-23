"""v23 category operations (catops.py): create / rename / icon / hide / unhide / move, their validation and the
built-in / income / transfer limits (the layer enforces them, not its callers)."""

from __future__ import annotations

import pytest

from hermes_finance import catops, caticons


def rows(conn, sql, *args):
    return [tuple(r) for r in conn.execute(sql, args)]


def test_create_defaults_to_misc_and_the_group_fallback_icon(conn):
    out = catops.create(conn, "Kids")
    assert out["name"] == "Kids"
    assert out["group"] == "Misc" and out["icon"] == catops.GROUP_ICON["Misc"]
    assert out["built_in"] is False and out["hidden"] is False
    assert out["spending"] is True
    assert rows(conn, "SELECT is_income, is_transfer FROM categories WHERE name = 'Kids'") == [(0, 0)]
    assert rows(conn, "SELECT sort FROM categories WHERE name = 'Kids'")[0][0] == 1000 + rows(
        conn, "SELECT id FROM categories WHERE name = 'Kids'")[0][0]


def test_create_accepts_a_group_in_any_case_and_an_explicit_icon(conn):
    food = catops.create(conn, "Meal prep", group="food")
    assert food["group"] == "Food" and food["icon"] == catops.GROUP_ICON["Food"]
    tent = catops.create(conn, "Camping", group="Leisure", icon="tent")
    assert (tent["group"], tent["icon"]) == ("Leisure", "tent")
    assert tent["icon"] in caticons.ALL


def test_create_normalises_whitespace_and_bounds_the_name(conn):
    assert catops.create(conn, "  Meal   prep  ")["name"] == "Meal prep"
    assert catops.create(conn, "x" * 60)["name"] == "x" * 60
    with pytest.raises(catops.CategoryError) as e:
        catops.create(conn, "x" * 61)
    assert e.value.field == "name"
    for bad in ("", "   ", "a\x00b", "a\u0007b"):
        with pytest.raises(catops.CategoryError):
            catops.create(conn, bad)
    assert catops.create(conn, "a\tb")["name"] == "a b"   # whitespace is normalised, not kept


def test_create_refuses_an_unknown_group_and_an_unknown_icon(conn):
    with pytest.raises(catops.CategoryError) as e:
        catops.create(conn, "Kids", group="Pets")
    assert e.value.field == "group"
    with pytest.raises(catops.CategoryError) as e:
        catops.create(conn, "Kids", icon="evil")
    assert e.value.field == "icon"
    assert rows(conn, "SELECT COUNT(*) FROM categories WHERE name = 'Kids'") == [(0,)]


@pytest.mark.parametrize("first,second", [("Kids", "kids"), ("kids", "KIDS")])
def test_uniqueness_is_case_insensitive_on_create_and_rename(conn, first, second):
    catops.create(conn, first)
    with pytest.raises(catops.CategoryError) as e:
        catops.create(conn, second)
    assert e.value.field == "name"
    other = catops.create(conn, "Children")
    with pytest.raises(catops.CategoryError) as e:
        catops.rename(conn, other["name"], first.upper())
    assert e.value.field == "name"
    assert catops.rename(conn, other["name"], "children")["name"] == "children"


def test_a_user_category_is_always_spending_only(conn):
    out = catops.create(conn, "Kids")
    assert (out["is_income"], out["is_transfer"], out["spending"]) == (False, False, True)
    assert rows(conn, "SELECT is_income, is_transfer FROM categories WHERE name = 'Kids'") == [(0, 0)]


def test_built_ins_can_be_re_iconed_and_hidden_but_not_renamed_deleted_or_moved(conn):
    before = rows(conn, "SELECT id, name, group_id FROM categories WHERE name = 'Coffee'")[0]
    iconed = catops.set_icon(conn, "Coffee", "cooking-pot")
    assert iconed["icon"] == "cooking-pot" and iconed["built_in"] is True
    hidden = catops.set_hidden(conn, "Coffee", True)
    assert hidden["hidden"] is True
    for verb, call in (
            ("rename", lambda: catops.rename(conn, "Coffee", "Espresso")),
            ("delete", lambda: catops.delete(conn, "Coffee", "Groceries")),
            ("move", lambda: catops.move(conn, "Coffee", "Misc"))):
        with pytest.raises(catops.CategoryError) as e:
            call()
        assert "is a built-in category" in str(e.value)
        assert verb in str(e.value)
    assert rows(conn, "SELECT id, name, group_id FROM categories WHERE name = 'Coffee'")[0] == before
    assert catops.set_hidden(conn, "Coffee", False)["hidden"] is False


def test_income_and_transfers_refuse_hiding_but_accept_an_icon(conn):
    for name in ("Income", "Transfers"):
        with pytest.raises(catops.CategoryError) as e:
            catops.set_hidden(conn, name, True)
        assert "income" in str(e.value).lower() or "transfer" in str(e.value).lower()
        assert rows(conn, "SELECT is_hidden FROM categories WHERE name = ?", name) == [(0,)]
        assert catops.set_icon(conn, name, "wallet")["icon"] == "wallet"
    assert rows(conn, "SELECT COUNT(*) FROM categories WHERE is_hidden = 1") == [(0,)]


def test_hidden_round_trips_and_keeps_the_chart_setting_in_step(conn):
    catops.create(conn, "Kids")
    catops.set_hidden(conn, "Kids", True)
    assert catops.set_hidden(conn, "Kids", True)["hidden"] is True
    assert rows(conn, "SELECT value FROM settings WHERE key = 'chart_excluded_categories'") == [('["Kids"]',)]
    assert catops.set_hidden(conn, "Kids", False)["hidden"] is False
    assert rows(conn, "SELECT value FROM settings WHERE key = 'chart_excluded_categories'") == [("[]",)]


def test_move_only_touches_the_group_and_only_for_user_categories(conn):
    catops.create(conn, "Kids")
    moved = catops.move(conn, "Kids", "home")
    assert (moved["group"], moved["icon"]) == ("Home", catops.GROUP_ICON["Misc"])
    with pytest.raises(catops.CategoryError):
        catops.move(conn, "Kids", "Pets")
    with pytest.raises(catops.CategoryError) as e:
        catops.move(conn, "Groceries", "Misc")
    assert "built-in" in str(e.value)


def test_update_changes_only_what_is_given_and_in_one_transaction(conn):
    catops.create(conn, "Kids")
    out = catops.update(conn, "Kids", rename_to="Children", hidden=True)
    assert (out["name"], out["hidden"], out["icon"]) == ("Children", True, catops.GROUP_ICON["Misc"])
    out = catops.update(conn, "Children", group="Home", icon="tent")
    assert (out["name"], out["group"], out["icon"], out["hidden"]) == ("Children", "Home", "tent", True)
    with pytest.raises(catops.CategoryError):
        catops.update(conn, "Children", group="Pets")
    assert rows(conn, "SELECT name, is_hidden FROM categories WHERE name = 'Children'") == [("Children", 1)]


def test_payload_groups_every_category_with_its_month_spend_and_counts(conn):
    catops.create(conn, "Kids", group="Home", icon="tent")
    p = catops.payload(conn)
    assert [g["name"] for g in p["groups"]][-1] == "Income & transfers"
    names = [c["name"] for g in p["groups"] for c in g["categories"]]
    assert len(names) == 37 and "Kids" in names and "Income" in names
    kids = next(c for c in p["categories"] if c["name"] == "Kids")
    assert (kids["group"], kids["icon"], kids["spend"], kids["built_in"], kids["hidden"]) == ("Home", "tent", 0.0, False, False)
    assert p["counts"] == {"total": 37, "builtin": 36, "user": 1, "hidden": 0, "spending": 35}
    assert p["hidden"] == []


def test_text_lines_name_group_icon_tags_and_spend(conn):
    catops.create(conn, "Kids", group="Home")
    lines = catops.text(catops.payload(conn)).splitlines()
    line = next(x for x in lines if "Kids" in x)
    assert "Home" in line and "yours" in line and "spend 0.00" in line

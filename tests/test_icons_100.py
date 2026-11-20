"""The 100-icon set (v23, 2026-11-15): eight groups, genuine Lucide names, one shared list for categories and goals,
every name drawable from the app's own sprite, and the one GET payload the browser picker reads."""

from __future__ import annotations

from hermes_finance import caticons, goals, web


def test_eight_groups_and_one_hundred_unique_icons():
    assert len(caticons.GROUPS) == 8
    assert len(caticons.ALL) == 100
    assert len(set(caticons.ALL)) == 100
    assert caticons.ALL == [name for _, names in caticons.GROUPS for name in names]
    assert all(names and len(names) == len(set(names)) for _, names in caticons.GROUPS)


def test_the_two_common_sets_are_sixteen_icons_each_and_subsets():
    assert len(caticons.QUICK_CAT) == len(caticons.QUICK_GOAL) == 16
    assert set(caticons.QUICK_CAT) <= set(caticons.ALL)
    assert set(caticons.QUICK_GOAL) <= set(caticons.ALL)
    assert caticons.PHONE_ROWS == 12
    assert len(caticons.QUICK_CAT[:caticons.PHONE_ROWS]) == 12


def test_every_name_is_drawn_by_the_app_sprite_and_goals_share_the_set():
    missing = [name for name in caticons.ALL if name not in web.ICONS or not web.ICONS[name].strip()]
    assert missing == []
    assert goals.ICONS == tuple(caticons.ALL)   # one allow-list, one picker
    assert set(goals.LEGACY_ICONS.values()) <= set(caticons.ALL)   # old goal looks open with a valid selection
    assert tuple(goals.COLOURS) == ("#4fd1a5", "#8ea8ff", "#5fc6e8", "#c792ea", "#f5b454", "#ff8fb1", "#ff7b7b", "#9ba2b6")


def test_the_icons_payload_is_the_one_read_the_picker_uses():
    p = caticons.payload()
    assert p["count"] == 100
    assert [g["name"] for g in p["groups"]] == [name for name, _ in caticons.GROUPS]
    assert [n for g in p["groups"] for n in g["icons"]] == caticons.ALL
    assert p["common"] == {"categories": caticons.QUICK_CAT, "goals": caticons.QUICK_GOAL}

"""v23 `finance categories`: list (text and --json), add, rename, icon, hide, unhide, move and delete --to, its exit
codes, and the built-in limits (catops enforces them, so the CLI does too)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from agent_tools import run_cli
from hermes_finance import catops, cli, config


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 9, 28))


def rows(conn, sql, *args):
    return [tuple(r) for r in conn.execute(sql, args)]


def test_list_text_and_json(conn, capsys):
    catops.create(conn, "Kids", group="Home", icon="tent")
    code, out, err = run_cli(["categories"], capsys)
    assert (code, err) == (0, "")
    line = next(x for x in out.splitlines() if "Kids" in x)
    assert "Home" in line and "tent" in line and "yours" in line and "spend 0.00" in line
    built = next(x for x in out.splitlines() if "Coffee" in x)
    assert "built-in" in built
    code, out, err = run_cli(["categories", "--json"], capsys)
    assert (code, err) == (0, "")
    p = json.loads(out)
    assert p["counts"] == {"total": 37, "builtin": 36, "user": 1, "hidden": 0, "spending": 35}
    assert next(c for c in p["categories"] if c["name"] == "Kids")["icon"] == "tent"


def test_add_rename_icon_move_and_the_json_shape(conn, capsys):
    code, out, err = run_cli(["categories", "add", "Kids", "--group", "home", "--icon", "tent"], capsys)
    assert (code, err) == (0, "")
    assert "Added category Kids (Home, icon tent)." in out
    code, out, err = run_cli(["categories", "add", "Camp", "--json"], capsys)
    assert code == 0 and json.loads(out)["group"] == "Misc"
    code, out, err = run_cli(["categories", "rename", "Kids", "Children"], capsys)
    assert (code, out.strip()) == (0, "Renamed Kids to Children.")
    code, out, err = run_cli(["categories", "icon", "Children", "cake"], capsys)
    assert (code, out.strip()) == (0, "Children now uses the cake icon.")
    code, out, err = run_cli(["categories", "move", "Children", "--group", "Leisure"], capsys)
    assert (code, out.strip()) == (0, "Children moved to Leisure.")
    assert rows(conn, "SELECT group_id FROM categories WHERE name = 'Children'") == \
        [(rows(conn, "SELECT id FROM category_groups WHERE name = 'Leisure'")[0][0],)]


def test_hide_and_unhide_text_and_the_hidden_list(conn, capsys):
    catops.create(conn, "Kids")
    code, out, err = run_cli(["categories", "hide", "Kids"], capsys)
    assert code == 0 and "Kids is hidden" in out
    assert rows(conn, "SELECT is_hidden FROM categories WHERE name = 'Kids'") == [(1,)]
    code, out, err = run_cli(["categories", "unhide", "Kids"], capsys)
    assert code == 0 and "Kids is visible again" in out
    assert rows(conn, "SELECT is_hidden FROM categories WHERE name = 'Kids'") == [(0,)]


def test_delete_moves_rows_and_needs_its_destination(conn, capsys):
    catops.create(conn, "Kids")
    catops.create(conn, "Children")
    code, out, err = run_cli(["categories", "delete", "Kids"], capsys)
    assert code == 2 and "--to" in err
    code, out, err = run_cli(["categories", "delete", "Kids", "--to", "Children"], capsys)
    assert (code, err) == (0, "")
    assert out.strip() == "Moved nothing from Kids to Children, then deleted Kids."
    assert rows(conn, "SELECT COUNT(*) FROM categories WHERE name = 'Kids'") == [(0,)]


def test_built_in_refusals_exit_2_with_one_clear_sentence(conn, capsys):
    for argv, word in ((["categories", "rename", "Coffee", "Espresso"], "rename"),
                       (["categories", "delete", "Coffee", "--to", "Groceries"], "delete"),
                       (["categories", "move", "Coffee", "--group", "Misc"], "move")):
        code, out, err = run_cli(argv, capsys)
        assert code == 2 and out == ""
        assert "is a built-in category" in err and word in err
    code, out, err = run_cli(["categories", "hide", "Income"], capsys)
    assert code == 2 and ("income" in err.lower() or "transfer" in err.lower())
    code, out, err = run_cli(["categories", "add", "Kids", "--group", "Pets"], capsys)
    assert code == 2 and "Pick a group" in err


def test_an_unknown_category_writes_nothing_and_exits_2(conn, capsys):
    before = rows(conn, "SELECT COUNT(*) FROM categories")
    code, out, err = run_cli(["categories", "icon", "Nope", "cake"], capsys)
    assert code == 2 and "unknown category" in err
    assert rows(conn, "SELECT COUNT(*) FROM categories") == before


def test_help_names_the_eight_groups_and_every_command(capsys):
    with pytest.raises(SystemExit):
        cli.main(["categories", "--help"])
    out, err = capsys.readouterr()
    for word in ("Home", "Food", "Transport", "Shopping", "Bills", "Health", "Leisure", "Misc",
                 "add", "rename", "icon", "hide", "unhide", "move", "delete"):
        assert word in out

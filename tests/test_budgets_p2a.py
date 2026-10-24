"""Polish P2A: the Budgets page as a table grouped by category group (Planned / Spent / Left).

API side (no browser), on the synthetic `features` DB with a few caps added (rollover is on for Eating Out and Other Transport):
  - a group's Planned / Spent / Left are the sums of its capped categories, and the groups add up to the page's totals;
  - the page totals (`budgets.*`) are what insights.budgets gives over the raw category rows: the grouping changed nothing;
  - a rollover category's Left is still (cap in force - spent - bills due), the cap in force being base + rolled over;
  - an uncapped category with spending this month and no history is `new`, and carries a `starter` suggestion while the
    history-based `suggested` and the page-wide suggestions stay exactly as they were.

Browser side (tests/cdp.py, a throwaway loopback server on the same kind of DB):
  - a group folds and unfolds from the keyboard (Enter and Space on its button, aria-expanded follows) and by touch, and its
    button is at least 44px tall on a phone;
  - the page never scrolls sideways at 390 (nor at 1280), a row reads name + Left on its first line and Planned / Spent
    on its second;
  - the old "Categories got finer" note is gone (removed 2026-10-21) and never comes back, even on a fresh device;
  - the progress line and the Left pill agree: red when over, amber from 80%, green below, gray at exactly $0.
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome
from factory import cap, tx
from hermes_finance import api, classify, db, insights

TODAY = date(2026, 10, 20)
MONTH = "2026-10"
STATIC = Path(__file__).parents[1] / "src" / "hermes_finance" / "static"


def build(path: Path, today: date) -> Path:
    """The features DB plus: Coffee capped at exactly what it has spent ($0 left), a few more caps, and a new category
    (Hobbies) that has spending this month and no history."""
    gate_dbs.build("features", path, today)
    conn = db.connect(path)
    month = today.isoformat()[:7]
    spent = {r[0]: r[1] for r in conn.execute(
        "SELECT c.name, SUM(v.spend) FROM v_transactions v JOIN categories c ON c.id = v.category_id WHERE v.month = ? GROUP BY c.name",
        (month,))}
    cap(conn, "Coffee", round(spent.get("Coffee", 18.48), 2))
    cap(conn, "Gas", 90)
    cap(conn, "Rent & Utilities", 2300)
    cap(conn, "Groceries", 500)
    cap(conn, "Travel", 400)
    tid = tx(conn, f"{month}-05", 64.20, "Michaels", cat="Other Shopping")
    classify.set_category(conn, tid, "Hobbies")
    conn.commit()
    conn.close()
    return path


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    path = build(tmp_path_factory.mktemp("p2a") / "finance.db", TODAY)
    conn = db.connect(path)
    try:
        yield conn, api.budgets_page(conn, {"month": MONTH}, TODAY)
    finally:
        conn.close()


def cents(x: float) -> float:
    return round(x, 2)


def test_group_totals_are_the_sums_of_their_categories(page):
    conn, d = page
    cats = {c["category"]: c for c in d["categories"]}
    assert d["groups"], "the page carries its groups"
    seen: list[str] = []
    for g in d["groups"]:
        capped = [cats[n] for n in g["categories"] if cats[n]["cap"] is not None]
        assert g["count"] == len(capped), g["name"]
        # group roll-up (2026-10-22): a group with caps counts EVERY category in it; one with none stays at zeros
        every = [cats[n] for n in g["categories"]] if capped else []
        loose = [c for c in every if c["cap"] is None]
        assert g["planned"] == cents(sum(c["cap"] for c in capped)), g["name"]
        assert g["spent"] == cents(sum(c["spend"] for c in every)), g["name"]
        assert g["due"] == cents(sum(c["due"] for c in every)), g["name"]
        assert g["left"] == cents(sum(c["left"] for c in capped) - sum(c["spend"] + c["due"] for c in loose)) \
            == cents(g["planned"] - g["spent"] - g["due"]), g["name"]
        assert g["over"] == sum(1 for c in capped if c["status"] == "over"), g["name"]
        assert all(cats[n]["group"] == g["name"] for n in g["categories"]), g["name"]
        seen += g["categories"]
    assert sorted(seen) == sorted(cats), "every spending category is in exactly one group"
    assert [g["name"] for g in d["groups"]][:3] == ["Home", "Food", "Transport"]   # the taxonomy's order (db.CATEGORY_GROUPS)
    assert any(g["count"] for g in d["groups"]) and any(len(g["categories"]) > g["count"] for g in d["groups"])


def test_the_page_totals_are_what_they_were(page):
    """Grouping added fields. The totals equal insights.budgets over the same rows and the groups add up to them; since
    the group roll-up (2026-10-22) they also take the uncapped categories in a group with caps."""
    conn, d = page
    raw = insights.category_spend(conn, MONTH, TODAY)
    want = insights.budgets(conn, MONTH, TODAY, raw)
    for key in ("total_cap", "total_spend", "total_due", "left", "count", "over", "under", "under_count", "over_amount", "per_day"):
        assert d["budgets"].get(key) == want.get(key), key
    capped = [c for c in raw if c["cap"] is not None]
    # group roll-up: the uncapped categories in a group with caps count too
    groups_with_caps = {c["group"] for c in d["categories"] if c["cap"] is not None}
    loose = [c for c in d["categories"] if c["cap"] is None and c["group"] in groups_with_caps]
    assert loose and any(c["spend"] for c in loose), "the fixture has uncapped spending in a group with caps"
    assert d["budgets"]["total_cap"] == cents(sum(c["cap"] for c in capped))
    assert d["budgets"]["total_spend"] == cents(sum(c["spend"] for c in capped) + sum(c["spend"] for c in loose))
    assert d["budgets"]["total_due"] == cents(sum(c["due"] for c in capped) + sum(c["due"] for c in loose))
    assert d["budgets"]["left"] == cents(sum(c["cap"] - c["spend"] - c["due"] for c in capped)
                                         - sum(c["spend"] + c["due"] for c in loose))
    assert cents(d["budgets"]["under"] - d["budgets"]["over_amount"]) == d["budgets"]["left"]
    assert cents(sum(g["planned"] for g in d["groups"])) == d["budgets"]["total_cap"]
    assert cents(sum(g["spent"] for g in d["groups"])) == d["budgets"]["total_spend"]
    assert cents(sum(g["left"] for g in d["groups"])) == d["budgets"]["left"]
    # and each category's own numbers are the raw ones, field for field
    by_name = {c["category"]: c for c in d["categories"]}
    for c in raw:
        for key in ("cap", "spend", "due", "left", "status", "base_cap", "rollover", "last_month", "avg3"):
            assert by_name[c["category"]].get(key) == c.get(key), (c["category"], key)


def test_a_rollover_categorys_left_is_unchanged(page):
    conn, d = page
    rolled = [c for c in d["categories"] if c.get("rollover") and c["rollover"]["amount"] > 0]
    assert rolled, "the features DB has a category with something rolled over (Eating Out)"
    for c in rolled:
        assert c["cap"] == cents(c["base_cap"] + c["rollover"]["amount"]), c["category"]       # the cap in force
        assert c["left"] == cents(c["cap"] - c["spend"] - c["due"]), c["category"]             # Left: the same net as ever
        assert c["left"] != cents(c["base_cap"] - c["spend"] - c["due"])                       # ... and it counts the rollover
    raw = {c["category"]: c for c in insights.category_spend(conn, MONTH, TODAY)}
    assert all(raw[c["category"]]["left"] == c["left"] for c in rolled)


def test_a_new_category_with_spending_is_flagged_and_gets_a_starter_suggestion(page):
    conn, d = page
    cats = {c["category"]: c for c in d["categories"]}
    hobbies = cats["Hobbies"]
    assert hobbies["cap"] is None and hobbies["spend"] == 64.2
    assert hobbies["new"] is True and hobbies["suggested"] is None, hobbies       # no history to suggest from
    assert hobbies["starter"] == insights.round_cap(64.2) == 75.0                  # what it has taken so far, rounded up
    for name, c in cats.items():
        if name != "Hobbies":
            assert not c["new"] or (c["cap"] is None and c["spend"] > 0 and c["last_month"] == 0 and c["avg3"] == 0), name
        if c["cap"] is not None:
            assert c["starter"] is None, name                                      # a capped row needs no starter
        if c["suggested"] is not None:
            assert c["starter"] is None, name                                      # history wins over the starter
    # the page-wide suggestions (the hero's total, "Use all", not_budgeted) are the history-based ones, untouched
    listed = {s["category"]: s for s in d["suggestions"]["categories"]}
    assert listed["Hobbies"]["suggested"] is None, "the history-based list still has nothing to say about it"
    assert all(s["suggested"] is None or s["suggested"] == cats[n]["suggested"] for n, s in listed.items())


def test_the_finer_note_is_gone_from_the_code():
    js = (STATIC / "budgets.js").read_text()
    assert "Categories got finer" not in js and "hf.budgets.finer" not in js and "finerNote" not in js
    assert "'hf.budgets.shut'" in js, "the folded groups are still remembered per device"


# --- the browser --------------------------------------------------------------------------------------------------

pytestmark_browser = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
KEYS = {"Enter": 13, " ": 32}


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    today = date.today()
    srv = Server(build(tmp_path_factory.mktemp("p2ab") / "finance.db", today), today=today)
    try:
        yield srv
    finally:
        srv.close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


def go(b, site, width, fresh=True):
    b.size(width, 900)
    if fresh:
        b.goto(f"{site.url}/?p2a={time.time()}#/budgets")
        b.js("localStorage.removeItem('hf.budgets.shut'); localStorage.removeItem('hf.budgets.finer')")
        b.goto(f"{site.url}/?p2a={time.time()}#/budgets")
    else:
        b.goto(f"{site.url}/?p2a={time.time()}#/budgets")
    b.settle()
    b.js("new Promise((r) => setTimeout(r, 200))")


def press(b, value):
    text = {"Enter": "\r", " ": " "}[value]
    b.call("Input.dispatchKeyEvent", type="keyDown", key=value, code="Space" if value == " " else value,
           windowsVirtualKeyCode=KEYS[value], text=text)
    b.call("Input.dispatchKeyEvent", type="keyUp", key=value, code="Space" if value == " " else value, windowsVirtualKeyCode=KEYS[value])
    b.js("new Promise((r) => setTimeout(r, 200))")


def folded(b, group):
    return b.js(f"""(() => {{ const t = document.querySelector('tbody[data-group="{group}"]');
      const btn = t.querySelector('.gbtn'), rows = [...t.querySelectorAll('tr:not(.g)')];
      return {{expanded: btn.getAttribute('aria-expanded'), shut: t.classList.contains('shut'),
        hidden: rows.length > 0 && rows.every((r) => r.hidden), visible: rows.some((r) => r.getClientRects().length > 0)}}; }})()""")


@pytestmark_browser
def test_a_group_folds_and_unfolds_from_the_keyboard(chrome, site):
    b = chrome
    go(b, site, 1280)
    assert folded(b, "Food") == {"expanded": "true", "shut": False, "hidden": False, "visible": True}
    btn = b.js("""(() => { const e = document.querySelector('tbody[data-group="Food"] .gbtn');
      return {tag: e.tagName, type: e.type, label: e.getAttribute('aria-label'), controls: e.getAttribute('aria-controls').split(' ').length}; })()""")
    assert btn["tag"] == "BUTTON" and btn["type"] == "button" and btn["label"].startswith("Food, ") and btn["controls"] >= 2, btn
    b.js("document.querySelector('tbody[data-group=\"Food\"] .gbtn').focus()")
    press(b, "Enter")
    assert folded(b, "Food") == {"expanded": "false", "shut": True, "hidden": True, "visible": False}
    assert b.js("document.activeElement.classList.contains('gbtn')"), "focus stays on the button"
    press(b, " ")
    assert folded(b, "Food") == {"expanded": "true", "shut": False, "hidden": False, "visible": True}
    # the fold is remembered on this device, and the group's own totals stay in view while it is folded
    press(b, "Enter")
    assert b.js("JSON.parse(localStorage.getItem('hf.budgets.shut'))") == ["Food"]
    assert b.js("""document.querySelector('tbody[data-group="Food"] tr.g').getClientRects().length > 0""")
    go(b, site, 1280, fresh=False)
    assert folded(b, "Food")["shut"] is True
    assert b.problems() == []


@pytestmark_browser
def test_a_group_folds_by_touch_and_its_target_is_44px_on_a_phone(chrome, site):
    b = chrome
    b.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
    try:
        go(b, site, 390)
        assert b.js("matchMedia('(pointer: coarse)').matches")
        box = b.js("""(() => { const e = document.querySelector('tbody[data-group="Transport"] .gbtn');
          e.scrollIntoView({block: 'center'}); const r = e.getBoundingClientRect();
          const y = r.y + r.height / 2, hit = [];   // what a finger actually hits: the 24px chevron plus its ::after
          for (let x = Math.floor(r.left) - 12; x <= Math.ceil(r.right) + 12; x++) {
            const t = document.elementFromPoint(x, y); if (t && t.closest('.gbtn') === e) hit.push(x); }
          return {x: r.x + 24, y, h: r.height, w: r.width, hit: hit.length ? hit[hit.length - 1] - hit[0] + 1 : 0,
                  gap: hit.length === hit[hit.length - 1] - hit[0] + 1}; })()""")
        # the chevron draws 24px wide (one icon column with the categories, 2026-10-22); its tap area stays 44px wide
        assert box["h"] >= 44 and box["hit"] >= 44 and box["gap"], box
        for _ in range(2):          # fold, then unfold
            before = folded(b, "Transport")["shut"]
            b.call("Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": box["x"], "y": box["y"]}])
            b.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
            b.js("new Promise((r) => setTimeout(r, 250))")
            assert folded(b, "Transport")["shut"] is (not before)
        assert b.problems() == []
    finally:
        b.call("Emulation.setTouchEmulationEnabled", enabled=False)


@pytestmark_browser
@pytest.mark.parametrize("width", [390, 1280])
def test_the_page_never_scrolls_sideways(chrome, site, width):
    b = chrome
    go(b, site, width)
    assert b.js("document.documentElement.scrollWidth") == width
    # fold and open an editor and a Details panel too: none of them may push the page wider
    b.js("document.querySelector('tbody[data-group=\"Transport\"] .gbtn').click()")
    b.js("document.querySelector('tr.c[data-category=\"Eating Out\"] .plan').click()")
    b.js("new Promise((r) => setTimeout(r, 200))")
    assert b.js("document.documentElement.scrollWidth") == width
    b.js("document.querySelector('.help-trigger[data-help=\"budget-bills\"]').click()")
    b.js("new Promise((r) => setTimeout(r, 250))")
    assert b.js("document.documentElement.scrollWidth") == width
    assert b.problems() == []


@pytestmark_browser
def test_on_a_phone_a_row_reads_name_and_left_then_planned_and_spent(chrome, site):
    b = chrome
    go(b, site, 390)
    rows = b.js("""(() => [...document.querySelectorAll('#page tr.c')].map((c) => {
      const q = (s) => c.querySelector(s).getBoundingClientRect();
      const nm = q('.nml'), left = q('.rp'), plan = q('.plan'), spent = q('.act');
      return {name: c.dataset.category, nameTop: nm.top, leftTop: left.top, planTop: plan.top, spentTop: spent.top,
        // the text itself, not scrollWidth: the link's 44px phone hit box (an ::after) counts towards scrollWidth
        nameCut: (() => { const e = c.querySelector('.nml'), g = document.createRange(); g.selectNodeContents(e);
          return g.getBoundingClientRect().width > e.clientWidth + 1; })(),
        planLeft: plan.left, spentLeft: spent.left, leftRight: left.right, rowRight: c.getBoundingClientRect().right}; }))()""")
    assert len(rows) >= 8
    for r in rows:
        assert abs(r["nameTop"] - r["leftTop"]) <= 20, r                  # line 1: the name and Left
        assert r["planTop"] > r["nameTop"] + 8 and abs(r["planTop"] - r["spentTop"]) <= 6, r     # line 2: Planned · Spent
        assert r["planLeft"] < r["spentLeft"] and r["leftRight"] <= r["rowRight"] + 1, r
        assert not r["nameCut"], r
    text = b.js("document.querySelector('tr.c td.p').textContent + '|' + getComputedStyle(document.querySelector('tr.c td.p'), '::before').content")
    assert "Planned" in text
    assert b.problems() == []


@pytestmark_browser
def test_the_line_and_the_left_pill_agree_and_gray_is_exactly_zero(chrome, site):
    b = chrome
    go(b, site, 1280)
    rows = b.js("""(() => [...document.querySelectorAll('#page tr.c')].map((c) => {
      const hair = c.querySelector('.hair'), pill = c.querySelector('.rp');
      return {name: c.dataset.category, status: hair.dataset.ringStatus, pct: Number(hair.dataset.ringPct),
        pill: [...pill.classList].filter((x) => x !== 'rp')[0] || '', left: Number(c.dataset.left), text: pill.textContent,
        bar: getComputedStyle(hair.querySelector('i:not(.d)')).backgroundColor}; }))()""")
    tone = {"ok": "", "warn": "w", "over": "b"}
    colour = {"ok": "rgb(79, 209, 165)", "warn": "rgb(245, 180, 84)", "over": "rgb(255, 123, 123)"}
    for r in rows:
        assert r["bar"] == colour[r["status"]], r
        if r["left"] == 0:
            assert r["pill"] == "z", r                                       # gray at exactly $0, whatever the line says
        else:
            assert r["pill"] == tone[r["status"]], r
        assert (r["status"] == "over") == (r["pct"] >= 100), r
        assert (r["status"] == "warn") == (80 <= r["pct"] < 100), r
    kinds = {r["pill"] for r in rows}
    assert {"", "w", "b", "z"} <= kinds, (kinds, "the fixture shows a green, an amber, a red and a $0 row")
    assert next(r for r in rows if r["name"] == "Coffee")["text"] == "$0"
    assert b.problems() == []


@pytestmark_browser
def test_the_finer_note_never_shows_even_on_a_fresh_device(chrome, site):
    b = chrome
    go(b, site, 1280)   # fresh: localStorage cleared, as on a new phone
    assert b.js("document.querySelector('[data-note=finer], .finer')") is None
    assert "Categories got finer" not in b.js("document.getElementById('page').textContent")
    assert b.js("document.querySelectorAll('tr.c').length") > 5
    assert b.problems() == []


@pytestmark_browser
def test_a_new_uncapped_category_shows_with_a_suggestion_and_use_sets_the_cap(chrome, site):
    """Group roll-up (2026-10-22): Hobbies (new this month, no history) is in Leisure, which has a cap, so its spending
    counts in Leisure: it sits under the group with Set (not in No budget yet), and Set offers its starter as Use."""
    b = chrome
    go(b, site, 1280)
    row = b.js("""(() => { const r = document.querySelector('tbody.bg tr.u[data-category="Hobbies"]');
      return r && {group: r.closest('tbody').dataset.group, spent: r.dataset.spent, set: (r.querySelector('.plan.set') || {}).textContent}; })()""")
    assert row and row["set"] == "Set" and float(row["spent"]) > 0, row
    assert b.js("document.querySelector('.nbrow[data-category=\"Hobbies\"]')") is None, "counted in its group, not No budget yet"
    # on a phone its amount sits in a fixed column, so every row's Set lines up whatever the amount's width
    go(b, site, 390)
    got = b.js("(() => { const r = document.querySelector('tr.u[data-category=\"Hobbies\"]'); return [r.querySelector('td.a').getBoundingClientRect().width, "
               "r.getBoundingClientRect().right - r.querySelector('.set').getBoundingClientRect().right]; })()")
    assert got[0] >= 63.5 and got[1] > 60, got
    go(b, site, 1280)
    b.js("document.querySelector('tr.u[data-category=\"Hobbies\"] .plan.set').click()")
    b.js("new Promise((r) => setTimeout(r, 400))")
    sug = b.js("(document.querySelector('#sheet .bx-sug') || {}).textContent")
    assert sug and "Suggested $75" in sug, sug
    b.requests()
    b.js("[...document.querySelectorAll('#sheet .bx-sug button')].find((x) => x.textContent === 'Use').click()")
    b.js("new Promise((r) => setTimeout(r, 700))")
    assert [u for m, u in b.requests() if m == "POST"] == [f"{site.url}/api/budgets/cap"]
    assert b.js("document.querySelector('tr.c[data-category=\"Hobbies\"] .plan').textContent.trim()") == "$75"
    assert b.js("document.querySelector('tr.u[data-category=\"Hobbies\"]')") is None
    assert b.problems() == []


@pytest.mark.parametrize("width", [360, 375, 390, 430, 768, 1280])
def test_the_editors_remove_sits_at_the_footers_far_end_and_never_wraps(chrome, site, width):
    """The sheet's footer: Save, Cancel, then "Remove budget" (trash icon, one line) at the far end of the footer; on a
    phone it is a full-width row below the Save/Cancel pair. Only a capped category has it, and it never leaves the sheet."""
    b = chrome
    go(b, site, width)
    probe = """(() => { const sh = document.querySelector('#sheet'), foot = sh.querySelector('.bx-foot'), rm = foot.querySelector('button.rm'),
        r = rm.getBoundingClientRect(), f = foot.getBoundingClientRect(), s = sh.getBoundingClientRect(),
        ps = getComputedStyle(foot),
        save = foot.querySelector('button.p').getBoundingClientRect(), cancel = [...foot.querySelectorAll('button')].find(x => x.textContent === 'Cancel').getBoundingClientRect();
        return {text: rm.textContent.trim(), label: rm.getAttribute('aria-label'), icon: !!rm.querySelector('svg[data-icon=trash]'),
                h: r.height, sh: save.height, lines: rm.querySelector('span').getClientRects().length,
                toEnd: f.right - parseFloat(ps.paddingRight) - r.right,   // the footer's content box: on a phone its box runs edge to edge (sticky)
                 inside: r.left >= s.left && r.right <= s.right && r.bottom <= s.bottom,
                after: r.left >= cancel.right - 1 || r.top >= cancel.bottom - 1, order: save.left < cancel.left,
                sug: !!sh.querySelector('.bx-sug')}; })()"""
    for cat, has_sug in (("Groceries", False), ("Eating Out", True)):
        b.js("(() => { const p = document.querySelector('tr.c[data-category=\"%s\"] .plan'); p.scrollIntoView({block: 'center'}); p.click(); })()" % cat)
        b.js("new Promise((r) => setTimeout(r, 450))")
        got = b.js(probe)
        assert got["text"] == "Remove budget" and got["label"] == f"Remove the {cat} budget" and got["icon"], got
        assert got["lines"] == 1 and got["inside"], (cat, got)
        assert abs(got["h"] - got["sh"]) <= (6 if width <= 600 else 1), (cat, got)   # Save's height; on a phone Save is 46, Remove 40 (+44px hit area)
        assert got["sug"] == has_sug and got["after"] and got["order"], (cat, got)
        assert got["toEnd"] <= 1, (width, cat, got)                                     # at the footer's far end
        b.js("document.querySelector('#sheet .bx-foot button.p + button').click()")
        b.js("new Promise((r) => setTimeout(r, 300))")
    assert b.problems() == []


@pytest.mark.parametrize("width", [360, 390, 430])
def test_a_phones_set_button_sits_right_after_the_category_name(chrome, site, width):
    """The owner, 2026-10-22: on a phone Set hung in a column by the amounts, far from the name it sets a budget for. It
    sits 10px after the name (the gap a capped row's (i) has), on the name's line; the amount stays flush right."""
    b = chrome
    go(b, site, width)
    rows = b.js("""(() => [...document.querySelectorAll('.bt tr.u')].filter(r => r.querySelector('.plan.set')).map(r => {
        const n = r.querySelector('.nml').getBoundingClientRect(), s = r.querySelector('.plan.set').getBoundingClientRect(),
          a = r.querySelector('td.a .act').getBoundingClientRect(), row = r.getBoundingClientRect(), mid = (x) => x.top + x.height / 2;
        return {cat: r.dataset.category, gap: s.left - n.right, dy: Math.abs(mid(s) - mid(n)), end: row.right - a.right,
                lines: r.querySelector('.nml').getClientRects().length}; }))()""")
    assert rows, "no row with Set"
    for r in rows:
        assert 9 <= r["gap"] <= 11 and r["dy"] <= 2 and r["lines"] == 1, (width, r)
    assert len({round(r["end"]) for r in rows}) == 1, (width, rows)                # amounts share one right edge
    assert b.problems() == []


@pytest.mark.parametrize("width", [360, 390, 430, 768, 1280])
def test_budget_rows_share_one_icon_column_and_one_right_edge_and_the_sheet_is_symmetric(chrome, site, width):
    """The owner, 2026-10-22 ("all buttons are still misaligned", phone screenshot of Eating Out's editor): a group's icon sat
    18px (desktop) / 30px (phone) right of its categories' icons, the editor started 30px left of them, and on a phone a
    category's Left pill ended 8px past its group's total. Now every row's icon is in one column, the editor's amount
    field starts in it and Remove ends where every row's Left ends; the fold chevron keeps a 44px tap area on a phone."""
    b = chrome
    go(b, site, width)
    rows = b.js("""(() => { const card = document.querySelector('.bt').getBoundingClientRect();
      return [...document.querySelectorAll('.bt tr.g, .bt tr.c, .bt tr.u')].filter(r => r.getBoundingClientRect().height).map(r => {
        const ic = r.querySelector('.cb'), u = r.classList.contains('u'),
          end = u ? r.querySelector('td.a') : (r.querySelector('td.r .rp') || r.querySelector('td.r .gtot') || r.querySelector('td.r')),
          ink = end && (end.querySelector('.rp, .gtot, span') || end), e = ink && ink.getBoundingClientRect();
        return {k: r.className.split(' ')[0], icon: ic ? ic.getBoundingClientRect().left - card.left : null,
                end: e && e.width ? card.right - e.right : null}; }); })()""")
    icons = sorted({round(r["icon"]) for r in rows if r["icon"] is not None})
    assert {r["k"] for r in rows} >= {"g", "c"} and len(icons) == 1, (width, icons)
    ends = sorted({round(r["end"]) for r in rows if r["end"] is not None and (width <= 600 or r["k"] != "u")})
    assert len(ends) == 1, (width, ends)                       # one right edge for Left (a desktop "u" row has no Left)
    if width <= 600:
        tap = b.js("(() => { const g = document.querySelector('.bt tr.g .gbtn'), a = getComputedStyle(g, '::after'); "
                   "const r = g.getBoundingClientRect(); return [r.width - parseFloat(a.left) - parseFloat(a.right), r.height]; })()")
        assert tap[0] >= 44 and tap[1] >= 44, (width, tap)
    for sel in ('tr.c[data-category="Eating Out"] .plan', "tr.g .plan"):
        b.js("(() => { const p = document.querySelector(%s); p.scrollIntoView({block: 'center'}); p.click(); })()" % json.dumps(sel))
        b.js("new Promise((r) => setTimeout(r, 450))")
        ed = b.js("""(() => { const sh = document.querySelector('#sheet').getBoundingClientRect(), bx = document.querySelector('#sheet .bx'),
          i = bx.querySelector('.bx-in').getBoundingClientRect(), foot = [...bx.querySelectorAll('.bx-foot button')].map(x => x.getBoundingClientRect());
          return {left: i.left - sh.left, right: sh.right - i.right, over: Math.max(0, ...foot.map(x => x.right - sh.right), ...foot.map(x => sh.left - x.left))}; })()""")
        # in the sheet the amount field and the footer share the sheet's two padding edges, and nothing pokes out of it
        assert abs(ed["left"] - ed["right"]) <= 1 and ed["over"] == 0, (width, sel, ed)
        b.js("document.querySelector('#sheet .bx-foot button.p + button').click()")
        b.js("new Promise((r) => setTimeout(r, 300))")
    assert b.js("document.documentElement.scrollWidth - innerWidth") <= 0
    assert b.problems() == []

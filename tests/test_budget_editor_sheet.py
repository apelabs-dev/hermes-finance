"""The Budgets cap editor as the app's modal sheet (a bottom sheet below 1024px, a centred dialog from 1024px up).

Approved mock (two review rounds): the category's badge and name, "$59 spent of $150 in October · $91 left", a labelled
"Monthly budget" field with "Suggested $X · Use", a "Roll over what's left" settings row (label and state on the left, the
switch on the right) and the footer Save · Cancel ... Remove budget (red, trash icon, one tap, no confirm). On a phone
(<=600px) Save and Cancel share a row at equal width and Remove sits full width below.

Behaviour is the old inline editor's: same API calls, same validation, same three entry points (a capped category's Planned,
a group's Planned, the "No budget yet" Set buttons). Chrome (tests/cdp.py) on the synthetic `features` DB (test_budgets_p2a.build:
Eating Out has $250 + $124 rolled over from last month, Other Health has its switch off, Groceries spent more than its cap),
plus a $700 Transport group budget.
"""
from __future__ import annotations

import calendar
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

from cdp import Chrome, Server, find_chrome
from hermes_finance import db, groupbudget
from test_budgets_p2a import build as build_features

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()
CUR = calendar.month_name[TODAY.month]
PREV = calendar.month_name[(TODAY.month - 2) % 12 + 1]
PHONE, DESKTOP = 390, 1280
VK = {"Enter": 13, "Escape": 27, "Backspace": 8, "Tab": 9}

CAT_PLAN = 'tr.c[data-category="Eating Out"] .plan'
GROUP_PLAN = 'tr.g[data-group="Food"] .gplan'          # no budget of its own: Planned is its caps' sum
OWN_GROUP_PLAN = 'tr.g[data-group="Transport"] .gplan'  # its own $700 budget (Remove shows)
SPEND_SET = 'tr.u[data-category="Hobbies"] .plan.set'   # an uncapped category inside Leisure (group roll-up)


def build(path: Path) -> Path:
    build_features(path, TODAY)
    conn = db.connect(path)
    try:
        groupbudget.set_budget(conn, {"group": "Transport", "monthly_budget": 700})
    finally:
        conn.close()
    return path


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    srv = Server(build(tmp_path_factory.mktemp("edsheet") / "finance.db"), today=TODAY)
    try:
        yield srv
    finally:
        srv.close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


@contextmanager
def own_site(tmp_path, b, width):
    """A site of this test's own (the test writes to it) and the page opened at `width`."""
    tmp_path.mkdir(exist_ok=True)
    path = build(tmp_path / "finance.db")
    srv = Server(path, today=TODAY)
    try:
        go(b, srv, width)
        yield srv, path
    finally:
        srv.close()


def go(b, site, width, theme="dark"):
    b.size(width, 900)
    b.goto(f"{site.url}/?edsheet={time.time()}#/budgets")
    b.js(f"localStorage.removeItem('hf.budgets.shut'); document.documentElement.dataset.theme = {json.dumps(theme)}")
    b.settle()
    b.js("new Promise((r) => setTimeout(r, 200))")
    b.requests()


def q(b, selector, expr="el.textContent.trim()"):
    return b.js(f"(() => {{ const el = document.querySelector({json.dumps(selector)}); return el ? {expr} : null; }})()")


def click(b, selector):
    b.js(f"(() => {{ const el = document.querySelector({json.dumps(selector)}); el.scrollIntoView({{block: 'center'}}); el.click(); }})()")
    b.settle()
    b.js("Promise.all(document.getAnimations().filter((a) => a.effect?.getComputedTiming().iterations !== Infinity).map((a) => a.finished.catch(() => 0))).then(() => 1)")   # the sheet has slid in


def pause(b, ms=500):
    b.js(f"new Promise((r) => setTimeout(r, {ms}))")


def key(b, name):
    text = {"Enter": "\r"}.get(name)
    b.call("Input.dispatchKeyEvent", type="keyDown", key=name, code=name, windowsVirtualKeyCode=VK[name], **({"text": text} if text else {}))
    b.call("Input.dispatchKeyEvent", type="keyUp", key=name, code=name, windowsVirtualKeyCode=VK[name])
    b.settle()


def typed(b, text):
    b.js("document.activeElement.select()")
    b.call("Input.insertText", text=text)
    b.settle()


def sheet_open(b):
    return b.js("document.getElementById('modal').classList.contains('on')")


def rect(b, selector):
    return b.js(f"""(() => {{ const el = document.querySelector({json.dumps(selector)}); if (!el) return null;
      const r = el.getBoundingClientRect(); return {{left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height}}; }})()""")


def stored_cap(path: Path, category: str):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT monthly_cap FROM categories WHERE name = ?", (category,)).fetchone()[0]
    finally:
        conn.close()


def rollovers(path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return sorted(r[0] for r in conn.execute(
            "SELECT c.name FROM category_rollover r JOIN categories c ON c.id = r.category_id"))
    finally:
        conn.close()


def group_budgets(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return groupbudget.amounts(conn)
    finally:
        conn.close()


def reveal_nb(b):
    """The "No budget yet" rows with nothing spent wait behind one button."""
    b.js("(() => { const m = document.querySelector('#page .nb button.more'); if (m && m.getAttribute('aria-expanded') === 'false') m.click(); })()")


def entry(b, name):
    """Open the editor from one of its entry points; returns the opener's selector."""
    sel = {
        "category": CAT_PLAN,
        "group": GROUP_PLAN,
        "group-own": OWN_GROUP_PLAN,
        "spend-row": SPEND_SET,
        "nb-category": '#page .nb .nbrow:not(.nbgrp)[data-category="Subscriptions"] .setc',
        "nb-group": '#page .nb .nbgrp[data-group="Bills"] .setc',
    }[name]
    reveal_nb(b)
    click(b, sel)
    return sel


ENTRIES = ["category", "group", "group-own", "spend-row", "nb-category", "nb-group"]
NAMES = {"category": "Eating Out", "group": "Food", "group-own": "Transport", "spend-row": "Hobbies",
         "nb-category": "Subscriptions", "nb-group": "Bills"}


# --- it is the app's modal sheet / dialog ---------------------------------------------------------------------------

@pytest.mark.parametrize("width", [PHONE, DESKTOP])
@pytest.mark.parametrize("name", ENTRIES)
def test_the_editor_opens_as_a_sheet_or_dialog_from_every_entry_point(chrome, site, width, name):
    b = chrome
    go(b, site, width)
    entry(b, name)
    assert sheet_open(b), "the editor opens the modal sheet"
    assert q(b, "#sheet", "[el.getAttribute('role'), el.getAttribute('aria-modal'), el.getAttribute('aria-labelledby')]") == \
        ["dialog", "true", "sheet-title"]
    assert q(b, "#sheet #sheet-title") == NAMES[name]
    assert q(b, "#page tr.ed-row, #page .ed") is None, "no inline editor in the page"
    # the field is focused and selected when it opens
    assert b.js("(() => { const i = document.activeElement; return i.tagName === 'INPUT' && i.closest('#sheet') !== null "
                "&& i.selectionStart === 0 && i.selectionEnd === i.value.length; })()")
    s = rect(b, "#sheet")
    if width == PHONE:   # a bottom sheet: full width, on the bottom edge, with its grab handle
        assert s["left"] == 0 and abs(s["right"] - width) <= 1 and abs(s["bottom"] - 900) <= 1, s
        grab = rect(b, "#sheet .bx-grab")
        assert grab and grab["width"] > 0 and q(b, "#sheet .bx-grab", "el.getAttribute('aria-hidden')") == "true"
    else:                # a centred dialog about 460px wide
        assert abs((s["left"] + s["right"]) / 2 - width / 2) <= 1 and abs((s["top"] + s["bottom"]) / 2 - 450) <= 40, s
        assert 440 <= s["width"] <= 470 and s["bottom"] < 900 and s["top"] > 0, s
        assert q(b, "#sheet .bx-grab", "el.getBoundingClientRect().width") in (None, 0), "no grab handle on the dialog"
    assert q(b, "#sheet .shd button, #sheet .shd .ib") is None, "no close button: Cancel, Escape and the backdrop close it"
    assert b.problems() == []


@pytest.mark.parametrize("name", ["category", "group-own", "nb-group", "spend-row"])
def test_the_header_has_the_rows_badge_the_name_and_the_figures(chrome, site, name):
    b = chrome
    go(b, site, DESKTOP)
    reveal_nb(b)
    row = {"category": 'tr.c[data-category="Eating Out"]', "group-own": 'tr.g[data-group="Transport"]',
           "nb-group": '#page .nbgrp[data-group="Bills"]', "spend-row": 'tr.u[data-category="Hobbies"]'}[name]
    want = b.js(f"""(() => {{ const c = document.querySelector({json.dumps(row + ' .cb')}); const s = c.querySelector('svg');
      return {{icon: s.dataset.icon, rgb: c.style.getPropertyValue('--rgb')}}; }})()""")
    figures = b.js(f"""(() => {{ const r = document.querySelector({json.dumps(row)}); const t = (s) => (r.querySelector(s) || {{}}).textContent;
      return {{plan: t('.plan'), spent: t('td.a'), left: t('td.r')}}; }})()""")
    entry(b, name)
    got = b.js("""(() => { const c = document.querySelector('#sheet .shd .cb'); const s = c.querySelector('svg');
      return {icon: s.dataset.icon, rgb: c.style.getPropertyValue('--rgb'), hidden: c.getAttribute('aria-hidden'),
              h2: document.querySelector('#sheet .shd h2').id}; })()""")
    assert got == {**want, "hidden": "true", "h2": "sheet-title"}, (got, want)
    sub = q(b, "#sheet .bx-sub")
    assert sub and f"in {CUR}" in sub, sub
    if name == "category":   # "$132 spent of $374 in October · $242 left": the cap in force, spent and left, as the row shows them
        assert sub == f"{figures['spent']} spent of {figures['plan']} in {CUR} · {figures['left']} left", (sub, figures)
    if name == "group-own":
        assert sub.startswith(f"{figures['spent']} spent of {figures['plan']} in {CUR}"), (sub, figures)
    if name in ("nb-group", "spend-row"):
        assert sub.endswith("no budget yet") and " spent in " in sub, sub


def test_the_field_is_labelled_and_the_suggestion_is_offered_only_when_it_differs(chrome, site):
    b = chrome
    go(b, site, DESKTOP)
    entry(b, "category")
    assert q(b, "#sheet label.bx-field .bx-lab") == "Monthly budget"
    assert q(b, "#sheet label.bx-field input", "el.getAttribute('aria-label')") == "Monthly budget for Eating Out"
    assert q(b, "#sheet .bx-sug") == "Suggested $150 · Use"           # last month's habit, differs from the $250 set
    assert q(b, "#sheet label.bx-field input", "el.value") == "250"     # the amount you set, not the cap in force
    key(b, "Escape")
    click(b, 'tr.c[data-category="Groceries"] .plan')                    # its suggestion equals its cap: nothing offered
    assert q(b, "#sheet .bx-sug") is None
    key(b, "Escape")
    click(b, GROUP_PLAN)
    assert q(b, "#sheet label.bx-field input", "el.getAttribute('aria-label')") == "Monthly budget for the Food group"
    assert q(b, "#sheet .bx-lab") == "Monthly budget" and q(b, "#sheet .bx-sug") is None
    assert q(b, "#sheet input", "el.placeholder") == "904.7"
    assert q(b, "#sheet .bx-roll") is None, "a group budget has no roll-over row"


# --- the footer: Save, Cancel, then Remove at the far end -----------------------------------------------------------

FOOT = """(() => { const f = document.querySelector('#sheet .bx-foot'), fr = f.getBoundingClientRect(), bs = [...f.querySelectorAll('button')];
  const r = (e) => { const x = e.getBoundingClientRect(); return {left: x.left, right: x.right, top: x.top, bottom: x.bottom, width: x.width, height: x.height}; };
  const rm = f.querySelector('button.rm'), cs = rm && getComputedStyle(rm);
  // the footer's box runs edge to edge on a phone (it is sticky, on the sheet's background): its controls' edges are the content box's
  const cb = r(f), ps = getComputedStyle(f); cb.left += parseFloat(ps.paddingLeft); cb.right -= parseFloat(ps.paddingRight); cb.width = cb.right - cb.left;
  return {foot: cb, texts: bs.map((x) => x.textContent.trim()), save: r(bs[0]), cancel: r(bs[1]), rm: rm && r(rm),
          rmLabel: rm && rm.getAttribute('aria-label'), rmIcon: rm && (rm.querySelector('svg.i') || {dataset: {}}).dataset.icon,
          rmColor: cs && cs.color, rmText: rm && rm.querySelector('span') && rm.querySelector('span').textContent, rmWrap: rm && rm.getClientRects().length}; })()"""


@pytest.mark.parametrize("width", [360, PHONE, 430, 768, DESKTOP])
def test_the_footer_order_and_geometry(chrome, site, width):
    b = chrome
    go(b, site, width)
    entry(b, "category")
    f = b.js(FOOT)
    assert f["texts"] == ["Save", "Cancel", "Remove budget"], f
    assert f["rmLabel"] == "Remove the Eating Out budget" and f["rmText"] == "Remove budget" and f["rmIcon"] == "trash", f
    assert f["rmColor"] == "rgb(255, 163, 163)", f
    assert f["rm"]["height"] > 0 and f["rm"]["right"] <= f["foot"]["right"] + 0.5, f
    if width <= 600:   # Save and Cancel share a row at equal width; Remove is full width on its own line below
        assert abs(f["save"]["top"] - f["cancel"]["top"]) <= 1 and abs(f["save"]["width"] - f["cancel"]["width"]) <= 1, f
        assert f["save"]["right"] <= f["cancel"]["left"], f
        assert abs(f["save"]["left"] - f["foot"]["left"]) <= 1 and abs(f["cancel"]["right"] - f["foot"]["right"]) <= 1, f
        assert f["rm"]["top"] >= f["save"]["bottom"] + 4 and abs(f["rm"]["width"] - f["foot"]["width"]) <= 1, f
    else:              # one row: Save, Cancel, a gap, Remove with its right edge on the footer's
        mid = lambda x: (x["top"] + x["bottom"]) / 2                                   # noqa: E731
        assert abs(mid(f["save"]) - mid(f["rm"])) <= 2 and abs(mid(f["cancel"]) - mid(f["rm"])) <= 2, f
        assert f["save"]["right"] <= f["cancel"]["left"] and f["cancel"]["right"] + 24 <= f["rm"]["left"], f
        assert abs(f["rm"]["right"] - f["foot"]["right"]) <= 1 and abs(f["save"]["left"] - f["foot"]["left"]) <= 1, f
    assert b.problems() == []


def test_a_category_without_a_budget_has_no_remove(chrome, site):
    b = chrome
    go(b, site, DESKTOP)
    entry(b, "nb-category")
    assert b.js(FOOT)["texts"] == ["Save", "Cancel"]
    assert q(b, "#sheet button.rm") is None
    assert q(b, "#sheet .bx-roll .grow span") == "Set a budget first; then what’s left each month can carry into the next."
    assert q(b, "#sheet .bx-roll .tgl", "el.disabled") is True


@pytest.mark.parametrize("width", [360, PHONE, 430])
def test_every_phone_control_has_a_44px_hit_area_and_the_field_is_16px(chrome, site, width):
    b = chrome
    go(b, site, width)
    entry(b, "category")
    got = b.js(r"""(() => { const px = (v) => parseFloat(v) || 0, out = [];
      for (const el of document.querySelectorAll('#sheet button, #sheet input, #sheet [role=switch]')) {
        const r = el.getBoundingClientRect(); if (!r.width) continue;
        let [L, T, R, B] = [r.left, r.top, r.right, r.bottom];
        const cs = getComputedStyle(el, '::after');
        if (!['none', 'normal'].includes(cs.content) && cs.position === 'absolute') {
          const x0 = r.left + px(getComputedStyle(el).borderLeftWidth) + px(cs.left), y0 = r.top + px(getComputedStyle(el).borderTopWidth) + px(cs.top);
          [L, T, R, B] = [Math.min(L, x0), Math.min(T, y0), Math.max(R, x0 + px(cs.width)), Math.max(B, y0 + px(cs.height))];
        }
        if (el.tagName === 'INPUT') { const l = el.closest('label').getBoundingClientRect();
          [L, T, R, B] = [Math.min(L, l.left), Math.min(T, l.top), Math.max(R, l.right), Math.max(B, l.bottom)]; }
        const cx = (L + R) / 2, cy = (T + B) / 2, hit = document.elementFromPoint(cx, cy);
        out.push({el: (el.getAttribute('aria-label') || el.textContent || el.tagName).trim().slice(0, 28), w: R - L, h: B - T,
                  hit: !!hit && (hit === el || el.contains(hit) || (el.tagName === 'INPUT' && el.closest('label').contains(hit)))});
      }
      return {out, font: px(getComputedStyle(document.querySelector('#sheet input')).fontSize),
              small: [...document.querySelectorAll('#sheet *')].filter((e) => e.childNodes.length && [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())
                && e.getClientRects().length && px(getComputedStyle(e).fontSize) < 13).map((e) => e.className)}; })()""")
    assert len(got["out"]) >= 6, got      # field, Use, switch, Save, Cancel, Remove
    for c in got["out"]:
        assert c["w"] >= 43.5 and c["h"] >= 43.5 and c["hit"], (width, c)
    assert got["font"] >= 16 and got["small"] == [], got


# --- the roll-over settings row ---------------------------------------------------------------------------------------

ROLL = """(() => { const r = document.querySelector('#sheet .bx-roll'), sw = r.querySelector('[role=switch]'), g = r.querySelector('.grow');
  const rc = (e) => { const x = e.getBoundingClientRect(); return {left: x.left, right: x.right, top: x.top, bottom: x.bottom}; };
  return {label: g.querySelector('b').textContent, state: g.querySelector('span').textContent, checked: sw.getAttribute('aria-checked'),
          disabled: sw.disabled, role: sw.getAttribute('role'), aria: sw.getAttribute('aria-label'), sw: rc(sw), grow: rc(g), row: rc(r),
          rule: getComputedStyle(r).borderTopStyle}; })()"""


@pytest.mark.parametrize("category,checked,state", [
    ("Eating Out", "true", f"Adding {PREV}’s $124 to {CUR}"),
    ("Other Transport", "true", f"On · nothing was left in {PREV}"),
    ("Other Health", "false", f"Off · turn on to add {PREV}’s $185.91 to {CUR}"),
    ("Groceries", "false", f"Off · {CUR} starts fresh"),
])
@pytest.mark.parametrize("width", [PHONE, DESKTOP])
def test_the_rollover_is_a_settings_row_with_the_switch_on_the_right(chrome, site, width, category, checked, state):
    b = chrome
    go(b, site, width)
    click(b, f'tr.c[data-category="{category}"] .plan')
    r = b.js(ROLL)
    assert r["label"] == "Roll over what’s left" and r["state"] == state, r
    assert r["role"] == "switch" and r["checked"] == checked and not r["disabled"], r
    assert r["aria"] == f"Roll over what's left in {category}", r
    assert r["sw"]["left"] >= r["grow"]["right"] - 0.5, r                       # the switch is to the right of the label
    assert abs((r["sw"]["top"] + r["sw"]["bottom"]) / 2 - (r["grow"]["top"] + r["grow"]["bottom"]) / 2) <= 3, r
    assert abs(r["sw"]["right"] - r["row"]["right"]) <= 1 and r["rule"] == "solid", r      # a thin solid divider, not a dashed one
    assert q(b, "#sheet .bx-roll", "el.parentElement.className").split()[0] == "bx"
    assert b.problems() == []


def test_a_category_without_a_budget_says_to_set_one_first(chrome, site):
    b = chrome
    go(b, site, DESKTOP)
    entry(b, "spend-row")
    r = b.js(ROLL)
    assert r["state"] == "Set a budget first; then what’s left each month can carry into the next." and r["disabled"], r


def test_the_switch_saves_at_once_and_the_sheet_stays_open_with_the_new_state(chrome, tmp_path):
    b = chrome
    with own_site(tmp_path, b, DESKTOP) as (srv, path):
        click(b, 'tr.c[data-category="Other Health"] .plan')
        sub_before = q(b, "#sheet .bx-sub")
        assert "Off ·" in b.js(ROLL)["state"]
        b.requests()
        click(b, "#sheet .bx-roll [role=switch]")
        pause(b, 800)
        assert [u for m, u in b.requests() if m == "POST"] == [f"{srv.url}/api/budgets/rollover"]
        assert rollovers(path) == ["Eating Out", "Other Health", "Other Transport"]
        assert sheet_open(b) and q(b, "#sheet #sheet-title") == "Other Health", "the sheet stays open"
        r = b.js(ROLL)
        assert r["checked"] == "true" and r["state"] == f"Adding {PREV}’s $185.91 to {CUR}" and not r["disabled"], r
        assert b.js("document.activeElement.getAttribute('role')") == "switch", "focus stays on the switch"
        # the header's figures follow (the cap in force grew by what rolled in), as does the page behind
        plan = q(b, 'tr.c[data-category="Other Health"] .plan')
        assert plan == "$411" and f"of {plan} in {CUR}" in q(b, "#sheet .bx-sub") != sub_before, (plan, q(b, "#sheet .bx-sub"))
        assert q(b, 'tr.c[data-category="Other Health"] .plan .roll svg', "true") is True
        assert "Rollover on for" in q(b, "#toast")
        # and off again
        click(b, "#sheet .bx-roll [role=switch]")
        pause(b, 800)
        assert rollovers(path) == ["Eating Out", "Other Transport"]
        r = b.js(ROLL)
        assert sheet_open(b) and r["checked"] == "false" and r["state"] == f"Off · turn on to add {PREV}’s $185.91 to {CUR}", r
        assert b.problems() == []


def test_a_failed_toggle_reverts_and_says_so(chrome, tmp_path):
    b = chrome
    with own_site(tmp_path, b, DESKTOP):
        click(b, 'tr.c[data-category="Other Health"] .plan')
        b.js("window.__fetch = window.fetch; window.fetch = (u, o) => String(u).includes('/rollover') ? "
             "Promise.resolve(new Response(JSON.stringify({error: 'nope'}), {status: 400, headers: {'content-type': 'application/json'}})) : window.__fetch(u, o)")
        click(b, "#sheet .bx-roll [role=switch]")
        pause(b, 500)
        r = b.js(ROLL)
        assert sheet_open(b) and r["checked"] == "false" and not r["disabled"], r
        assert "Could not save" in q(b, "#toast")
        b.js("window.fetch = window.__fetch")
        b.problems()


# --- saving, using the suggestion, removing -------------------------------------------------------------------------

def test_remove_is_one_tap_with_no_confirmation(chrome, tmp_path):
    b = chrome
    with own_site(tmp_path, b, DESKTOP) as (srv, path):
        click(b, CAT_PLAN)
        assert stored_cap(path, "Eating Out") == 250
        b.requests()
        click(b, "#sheet button.rm")
        pause(b, 700)
        assert [(m, u) for m, u in b.requests() if m == "POST"] == [("POST", f"{srv.url}/api/budgets/cap")]
        assert stored_cap(path, "Eating Out") is None, "one tap removed the budget"
        assert not sheet_open(b), "no confirmation sheet, no undo: it closed"
        assert q(b, 'tr.c[data-category="Eating Out"]') is None
        assert "Budget removed from" in q(b, "#toast")
        assert b.problems() == []


def test_removing_a_group_budget_is_one_tap_too(chrome, tmp_path):
    b = chrome
    with own_site(tmp_path, b, PHONE) as (srv, path):
        click(b, OWN_GROUP_PLAN)
        assert group_budgets(path) == {"Transport": 700.0}
        assert q(b, "#sheet button.rm", "el.getAttribute('aria-label')") == "Remove the Transport budget"
        click(b, "#sheet button.rm")
        pause(b, 700)
        assert group_budgets(path) == {} and not sheet_open(b)
        assert "Group budget removed from" in q(b, "#toast")


def test_save_by_button_and_by_enter_saves_the_typed_amount_and_closes(chrome, tmp_path):
    b = chrome
    with own_site(tmp_path, b, DESKTOP) as (srv, path):
        click(b, 'tr.c[data-category="Groceries"] .plan')
        typed(b, "480")
        click(b, "#sheet .bx-foot button.p")
        pause(b, 700)
        assert stored_cap(path, "Groceries") == 480 and not sheet_open(b)
        click(b, 'tr.c[data-category="Gas"] .plan')
        typed(b, "95.5")
        key(b, "Enter")
        pause(b, 700)
        assert stored_cap(path, "Gas") == 95.5 and not sheet_open(b)
        assert q(b, 'tr.c[data-category="Gas"] .plan') == "$96"
        assert b.problems() == []


def test_use_saves_the_suggestion_in_one_click(chrome, tmp_path):
    b = chrome
    with own_site(tmp_path, b, DESKTOP) as (srv, path):
        click(b, CAT_PLAN)
        b.requests()
        click(b, "#sheet .bx-sug button")
        pause(b, 700)
        assert [u for m, u in b.requests() if m == "POST"] == [f"{srv.url}/api/budgets/cap"]
        assert stored_cap(path, "Eating Out") == 150 and not sheet_open(b)


def test_a_group_budget_is_set_and_an_empty_field_clears_it(chrome, tmp_path):
    b = chrome
    with own_site(tmp_path, b, DESKTOP) as (srv, path):
        click(b, GROUP_PLAN)
        assert sheet_open(b)
        typed(b, "800")
        key(b, "Enter")
        pause(b, 700)
        assert group_budgets(path) == {"Food": 800.0, "Transport": 700.0} and not sheet_open(b)
        click(b, OWN_GROUP_PLAN)
        assert sheet_open(b)
        b.js("document.activeElement.select()")
        key(b, "Backspace")
        key(b, "Enter")
        pause(b, 700)
        assert group_budgets(path) == {"Food": 800.0}


def test_an_invalid_amount_shows_the_alert_and_keeps_the_sheet_open(chrome, site):
    b = chrome
    go(b, site, PHONE)
    entry(b, "category")
    b.requests()
    typed(b, "lots")
    key(b, "Enter")
    assert q(b, "#sheet [role=alert]") == "Enter an amount from $0 to $100,000."
    assert q(b, "#sheet input", "el.getAttribute('aria-invalid')") == "true"
    assert q(b, "#sheet input", "el.getAttribute('aria-describedby') === document.querySelector('#sheet [role=alert]').id") is True
    assert sheet_open(b) and not [1 for m, _ in b.requests() if m == "POST"]
    assert b.js("document.activeElement === document.querySelector('#sheet input')")
    typed(b, "12")        # typing clears the error
    assert q(b, "#sheet [role=alert]") == "" and q(b, "#sheet input", "el.hasAttribute('aria-invalid')") is False
    key(b, "Escape")


# --- closing: Escape, Cancel, the backdrop; focus goes back to the opener ------------------------------------------

@pytest.mark.parametrize("how", ["escape", "cancel", "backdrop"])
@pytest.mark.parametrize("name", ENTRIES)
def test_it_closes_and_focus_returns_to_the_opener(chrome, site, how, name):
    b = chrome
    go(b, site, PHONE if how == "backdrop" else DESKTOP)
    sel = entry(b, name)
    b.js(f"window.__opener = document.querySelector({json.dumps(sel)})")
    b.requests()
    typed(b, "777")
    if how == "escape":
        key(b, "Escape")
    elif how == "cancel":
        click(b, "#sheet .bx-foot button:nth-child(2)")
    else:
        b.call("Input.dispatchMouseEvent", type="mousePressed", button="left", clickCount=1, x=20, y=20)
        b.call("Input.dispatchMouseEvent", type="mouseReleased", button="left", clickCount=1, x=20, y=20)
        b.settle()
    assert not sheet_open(b)
    assert b.js("document.activeElement === window.__opener"), b.js("document.activeElement.outerHTML.slice(0, 120)")
    assert not [1 for m, _ in b.requests() if m == "POST"], "nothing was saved"
    assert b.problems() == []


def test_escape_after_the_page_redrew_still_returns_focus_to_the_redrawn_opener(chrome, tmp_path):
    b = chrome
    with own_site(tmp_path, b, DESKTOP):
        click(b, 'tr.c[data-category="Other Health"] .plan')
        b.js("window.__opener = document.querySelector('tr.c[data-category=\"Other Health\"] .plan')")
        click(b, "#sheet .bx-roll [role=switch]")        # saves; reloadBudgets redraws the page and the opener with it
        pause(b, 800)
        assert b.js("!window.__opener.isConnected")
        key(b, "Escape")
        assert not sheet_open(b)
        assert b.js("document.activeElement === document.querySelector('tr.c[data-category=\"Other Health\"] .plan')")


def test_save_closes_and_focuses_the_redrawn_opener(chrome, tmp_path):
    b = chrome
    with own_site(tmp_path, b, DESKTOP):
        click(b, CAT_PLAN)
        b.js("window.__opener = document.querySelector('tr.c[data-category=\"Eating Out\"] .plan')")
        typed(b, "300")
        key(b, "Enter")
        pause(b, 700)
        assert not sheet_open(b) and b.js("!window.__opener.isConnected")
        assert b.js("document.activeElement === document.querySelector('tr.c[data-category=\"Eating Out\"] .plan')")
    with own_site(tmp_path / "phone", b, PHONE):   # a "Set" on a No budget yet row becomes a capped row's Planned
        reveal_nb(b)
        click(b, '#page .nb .nbrow:not(.nbgrp)[data-category="Subscriptions"] .setc')
        typed(b, "30")
        key(b, "Enter")
        pause(b, 700)
        assert not sheet_open(b)
        assert b.js("document.activeElement === document.querySelector('tr.c[data-category=\"Subscriptions\"] .plan')")


def test_tab_is_trapped_inside_the_sheet(chrome, site):
    b = chrome
    go(b, site, DESKTOP)
    entry(b, "category")
    seen = set()
    for _ in range(9):
        key(b, "Tab")
        seen.add(b.js("document.activeElement.closest('#sheet') ? 'in' : 'out'"))
    assert seen == {"in"}
    key(b, "Escape")


# --- fits every width, both themes ---------------------------------------------------------------------------------

@pytest.mark.parametrize("width", [360, PHONE, 430, 768, DESKTOP])
@pytest.mark.parametrize("name", ["category", "group-own"])
def test_nothing_overflows_with_the_editor_open(chrome, site, width, name):
    b = chrome
    go(b, site, width)
    entry(b, name)
    assert sheet_open(b)
    got = b.js("""(() => { const s = document.getElementById('sheet'), r = s.getBoundingClientRect(), d = document.documentElement;
      return {doc: d.scrollWidth - innerWidth, sheet: s.scrollWidth - s.clientWidth, left: r.left, right: r.right - innerWidth,
              top: r.top, bottom: r.bottom - innerHeight, wide: [...s.querySelectorAll('*')].filter((e) => e.getBoundingClientRect().right > r.right + 0.5).length}; })()""")
    assert got["doc"] <= 0 and got["sheet"] <= 0 and got["left"] >= 0 and got["right"] <= 0.5 and got["top"] >= 0 and got["bottom"] <= 0.5 \
        and got["wide"] == 0, (width, got)
    assert b.problems() == []


def lum(rgb):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


COLOURS = """(() => { const rgb = (s) => (s.match(/[\\d.]+/g) || []).slice(0, 4).map(Number), cs = (e) => getComputedStyle(e);
  const sheet = document.getElementById('sheet'), save = sheet.querySelector('.bx-foot button.p'), rm = sheet.querySelector('button.rm'),
        field = sheet.querySelector('.edin'), sub = sheet.querySelector('.bx-sub'), err = sheet.querySelector('[role=alert]');
  return {sheet: rgb(cs(sheet).backgroundColor), save: rgb(cs(save).color), saveBg: rgb(cs(save).backgroundColor), rm: rgb(cs(rm).color),
          field: rgb(cs(field).backgroundColor), input: rgb(cs(field.querySelector('input')).color), sub: rgb(cs(sub).color),
          err: rgb(cs(err).color), state: rgb(cs(sheet.querySelector('.bx-roll .grow span')).color)}; })()"""


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_editor_is_readable_in_both_themes(chrome, site, theme):
    b = chrome
    go(b, site, DESKTOP, theme=theme)
    entry(b, "category")
    typed(b, "lots")
    key(b, "Enter")          # so the error is painted too
    c = b.js(COLOURS)
    assert contrast(c["save"][:3], c["saveBg"][:3]) >= 4.5, c
    assert contrast(c["rm"][:3], c["sheet"][:3]) >= 4.5, c
    assert contrast(c["err"][:3], c["sheet"][:3]) >= 4.5, c
    assert contrast(c["input"][:3], c["field"][:3]) >= 4.5 and contrast(c["sub"][:3], c["sheet"][:3]) >= 4.5, c
    assert contrast(c["state"][:3], c["sheet"][:3]) >= 4.5, c
    if theme == "light":
        assert c["rm"][:3] == [196, 44, 44] and c["save"][:3] == [255, 255, 255], c
        assert c["field"][:3] == [255, 255, 255] and lum(c["saveBg"][:3]) < 0.1 and lum(c["sheet"][:3]) > 0.9, c
        assert lum(c["field"][:3]) > 0.5, "no dark fill on the field in the light theme"
    else:
        assert c["rm"][:3] == [255, 163, 163], c
    assert b.problems() == []


def test_the_old_inline_editor_styles_are_gone():
    static = Path(__file__).parents[1] / "src" / "hermes_finance" / "static"
    stale = ("ed-row", "edrow", "edmore", "rollrow", "bedit", "edsug")
    hits = [f"{p.name}: {w}" for p in sorted(static.glob("*.css")) + sorted(static.glob("*.js")) for w in stale if w in p.read_text()]
    assert hits == []


# --- the phone keyboard: the field, Save and Cancel stay on screen ---------------------------------------------------
#
# iOS's decimal pad has no return key (no Enter to save) and a tap outside the sheet closes it (the typed amount is lost),
# so with the keyboard up Save and Cancel must be on screen without scrolling. Below 1024px the footer is sticky at the
# bottom of the scrolling sheet, and the sheet fits the VISUAL viewport (the keyboard shrinks window.visualViewport, not the
# layout viewport). The 1024px+ dialog is unchanged.

KEYBOARD_ENTRIES = ["category", "group", "nb-category"]
FIT_STUB = """(() => {
  const vv = new EventTarget();
  Object.assign(vv, {height: innerHeight, width: innerWidth, offsetTop: 0, offsetLeft: 0, pageTop: 0, pageLeft: 0, scale: 1});
  Object.defineProperty(window, 'visualViewport', {configurable: true, value: vv});
  window.__vv = vv;
  window.__shrink = (height, offsetTop = 0) => { vv.height = height; vv.offsetTop = offsetTop; vv.dispatchEvent(new Event('resize')); };
  return true;
})()"""


def inside(r, vh, vw=PHONE):
    return r and r["top"] >= -0.5 and r["bottom"] <= vh + 0.5 and r["left"] >= -0.5 and r["right"] <= vw + 0.5


@pytest.mark.parametrize("name", KEYBOARD_ENTRIES)
@pytest.mark.parametrize("vh", [420, 360])
def test_with_the_keyboard_up_the_field_and_save_and_cancel_are_on_screen_without_scrolling(chrome, site, vh, name):
    b = chrome
    go(b, site, PHONE)
    b.size(PHONE, vh)            # what is left of a phone's screen above the on-screen keyboard
    pause(b, 250)
    entry(b, name)
    assert sheet_open(b), "the editor opens the modal sheet"
    assert b.js("document.activeElement.tagName === 'INPUT' && document.activeElement.closest('#sheet') !== null")
    pause(b, 250)
    sheet = rect(b, "#sheet")
    assert sheet["top"] >= -0.5 and sheet["bottom"] <= vh + 0.5 and sheet["height"] <= vh + 0.5, (vh, sheet)
    got = {k: rect(b, sel) for k, sel in (("field", "#sheet .bx-in"), ("save", "#sheet .bx-foot button.p"),
                                          ("cancel", "#sheet .bx-foot button:nth-child(2)"), ("remove", "#sheet .bx-foot button.rm"))}
    for k in ("field", "save", "cancel"):
        assert inside(got[k], vh), (vh, name, k, got[k])
    if name == "category":
        assert inside(got["remove"], vh), (vh, got["remove"])     # the phone's full-width Remove row is in the footer too
    # nothing covers the field: the point in the middle of it is the field itself, not the footer on top of it
    assert b.js("""(() => { const f = document.querySelector('#sheet .bx-in').getBoundingClientRect();
      const el = document.elementFromPoint(f.left + f.width / 2, f.top + f.height / 2); return !!el && el.closest('.bx-in') !== null; })()""")
    assert got["field"]["bottom"] <= got["save"]["top"] + 0.5, got      # in the page's order, the field above the footer
    assert b.problems() == []


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_below_1024_the_footer_sticks_to_the_sheets_bottom_on_the_sheets_own_background(chrome, site, theme):
    b = chrome
    go(b, site, PHONE, theme)
    b.size(PHONE, 360)
    pause(b, 250)
    entry(b, "category")
    probe = """(() => { const s = document.getElementById('sheet'), f = s.querySelector('.bx-foot'), r = f.getBoundingClientRect(),
      cs = getComputedStyle(f), ss = getComputedStyle(s);
      return {pos: cs.position, bg: cs.backgroundColor, sheetBg: ss.backgroundColor, scrollable: s.scrollHeight > s.clientHeight + 1,
              top: r.top, bottom: r.bottom, sheetBottom: s.getBoundingClientRect().bottom, scrollTop: s.scrollTop,
              max: s.scrollHeight - s.clientHeight}; })()"""
    first = b.js(probe)
    assert first["pos"] == "sticky" and first["scrollable"], first
    assert first["bg"] == first["sheetBg"] and first["bg"] not in ("rgba(0, 0, 0, 0)", "transparent"), first   # nothing shows through
    b.js("(() => { const s = document.getElementById('sheet'); s.scrollTop = 0; })()")
    top = b.js(probe)
    b.js("(() => { const s = document.getElementById('sheet'); s.scrollTop = s.scrollHeight; })()")
    end = b.js(probe)
    assert end["scrollTop"] > top["scrollTop"], (top, end)
    assert abs(top["bottom"] - end["bottom"]) <= 1 and abs(top["top"] - end["top"]) <= 1, (top, end)    # it does not move while the sheet scrolls
    assert end["bottom"] <= end["sheetBottom"] + 0.5, end
    assert b.problems() == []


def test_the_desktop_dialogs_footer_is_not_sticky_and_the_dialog_is_unchanged(chrome, site):
    b = chrome
    go(b, site, DESKTOP)
    b.js(FIT_STUB)
    entry(b, "category")
    assert q(b, "#sheet .bx-foot", "getComputedStyle(el).position") == "static"
    s = rect(b, "#sheet")
    assert 440 <= s["width"] <= 470 and s["bottom"] < 900 and s["top"] > 0, s
    assert b.js("document.getElementById('sheet').scrollHeight <= document.getElementById('sheet').clientHeight + 1")
    # a shrinking visual viewport (a tablet's keyboard) leaves the dialog alone
    b.js("window.__shrink(500)")
    pause(b, 100)
    assert q(b, "#sheet", "[el.style.bottom, el.style.maxHeight]") == ["", ""]
    assert rect(b, "#sheet") == s


def style_of_sheet(b):
    return b.js("(() => { const s = document.getElementById('sheet'); return {bottom: s.style.bottom, maxHeight: s.style.maxHeight, "
                "attr: s.getAttribute('style')}; })()")


def open_with_stub(b, site):
    go(b, site, PHONE)
    b.js(FIT_STUB)
    entry(b, "category")
    assert sheet_open(b)


def test_the_sheet_follows_the_visual_viewport_and_lifts_by_the_keyboards_height(chrome, site):
    b = chrome
    open_with_stub(b, site)
    assert style_of_sheet(b)["bottom"] == "" and style_of_sheet(b)["maxHeight"] == "", "no keyboard: the app's own sheet"
    b.js("window.__shrink(500)")                                  # the keyboard takes 400px of the 900px window
    pause(b, 150)
    st = style_of_sheet(b)
    assert st["bottom"] == "400px", st
    assert 380 <= float(st["maxHeight"].removesuffix("px")) <= 500, st                       # capped to the visual viewport, a small gap above
    s = rect(b, "#sheet")
    assert abs(s["bottom"] - 500) <= 1 and s["top"] >= 0 and s["height"] <= 500, s            # sits on the keyboard
    for sel in ("#sheet .bx-in", "#sheet .bx-foot button.p", "#sheet .bx-foot button:nth-child(2)"):
        r = rect(b, sel)
        assert r["top"] >= 0 and r["bottom"] <= 500.5, (sel, r)
    # iOS pans the visual viewport when the focused field is near the bottom: its top moves, the lift follows
    b.js("window.__shrink(500, 100)")
    pause(b, 150)
    st = style_of_sheet(b)
    assert st["bottom"] == "300px", st
    s = rect(b, "#sheet")
    assert abs(s["bottom"] - 600) <= 1 and s["top"] >= 100 - 0.5, s
    # the keyboard goes away: the app's own sheet again (no inline styles left)
    b.js("window.__shrink(900, 0)")
    pause(b, 150)
    st = style_of_sheet(b)
    assert st["bottom"] == "" and st["maxHeight"] == "", st
    assert b.problems() == []


@pytest.mark.parametrize("how", ["cancel", "escape", "backdrop", "save"])
def test_every_way_of_closing_removes_the_inline_styles_and_the_listeners(chrome, site, how):
    b = chrome
    open_with_stub(b, site)
    b.js("window.__shrink(500)")
    pause(b, 150)
    assert style_of_sheet(b)["bottom"] == "400px"
    if how == "cancel":
        click(b, "#sheet .bx-foot button:nth-child(2)")
    elif how == "escape":
        key(b, "Escape")
    elif how == "backdrop":
        b.call("Input.dispatchMouseEvent", type="mousePressed", button="left", clickCount=1, x=20, y=20)
        b.call("Input.dispatchMouseEvent", type="mouseReleased", button="left", clickCount=1, x=20, y=20)
        b.settle()
    else:
        click(b, "#sheet .bx-foot button.p")                     # the unchanged amount: saves what is there
        pause(b, 600)
    assert not sheet_open(b)
    st = style_of_sheet(b)
    assert st["bottom"] == "" and st["maxHeight"] == "" and not st["attr"], st
    # the listeners are gone: a later shrink does not touch the (closed) sheet, nor the next one that opens
    b.js("window.__shrink(300)")
    pause(b, 100)
    st = style_of_sheet(b)
    assert st["bottom"] == "" and st["maxHeight"] == "" and not st["attr"], st
    assert b.problems() == []

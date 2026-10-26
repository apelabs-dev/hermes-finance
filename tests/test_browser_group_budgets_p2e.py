"""Polish P2e in Chrome (tests/cdp.py), at 1280 and at 390: group budgets on the Budgets page and the one-time move.

On demo data with the owner's 7 caps (Groceries 400, Eating Out 150, Other Transport 800, Other Health 300,
Other Shopping 200, Entertainment 60, Subscriptions 50):
  1. a group's Planned is edited inline like a category's (set it, Esc cancels and gives the focus back, clear it and the
     group is the caps' sum again; on a phone the field is 44 px to hit);
  2. the banner's Review: Transport on Group budget with its amount changed, Food on Keep (with the keyboard), Apply: the
     Transport group budget, Food's two caps still there, and the banner counts only what is still eligible;
  3. Not now hides the banner for the rest of the month, a reload too;
  4. "categories add up to $550" next to a Food group budget of $500, and the Overview card's single Food row.
Each one checks that no text is under 13 px and no control under 24 px to tap. P2E_SHOTS=<dir> saves the screenshots.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

import demo_data
from cdp import Chrome, Server, find_chrome
from factory import tx
from hermes_finance import db, groupbudget, insights

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
CAPS = {"Groceries": 400, "Eating Out": 150, "Other Transport": 800, "Other Health": 300, "Other Shopping": 200,
        "Entertainment": 60, "Subscriptions": 50}
SHOTS = Path(os.environ["P2E_SHOTS"]) if os.environ.get("P2E_SHOTS") else None
WIDTHS = pytest.mark.parametrize("width", [1280, 390])

# Every text and every control on the page (and in an open sheet), or in the boxes `sel` picks, that is on screen: text
# under 13 px, and a control whose hit area (its box, grown by an absolutely placed ::before / ::after, or the <label>
# round an input) is under 24 px
SIZES = r"""((sel) => {
  const scopes = sel ? [...document.querySelectorAll(sel)] : [document.getElementById('page')];
  if (!sel && document.getElementById('modal').classList.contains('on')) scopes.push(document.getElementById('sheet'));
  const shown = (el) => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  const px = (v) => parseFloat(v) || 0;
  const small = [], tiny = [];
  for (const scope of scopes) {
    const walk = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      const el = n.parentElement;
      if (!n.textContent.trim() || !el || !shown(el) || el.closest('svg')) continue;
      const fs = px(getComputedStyle(el).fontSize);
      if (fs < 13 - 0.01) small.push(`${el.className || el.tagName}: ${fs}px "${n.textContent.trim().slice(0, 30)}"`);
    }
    for (const el of scope.querySelectorAll('input:not([type=hidden]), select, textarea')) {
      if (shown(el) && px(getComputedStyle(el).fontSize) < 13 - 0.01) small.push(`${el.tagName}: ${getComputedStyle(el).fontSize}`);
    }
    const TAP = 'button, a[href], input:not([type=hidden]), select, textarea, [role=button], [role=switch], [role=radio]';
    for (const el of [scope, ...scope.querySelectorAll(TAP)].filter((e) => e.matches(TAP))) {
      if (!shown(el) || el.closest('svg')) continue;
      if (el.tagName === 'A' && getComputedStyle(el).display === 'inline' && el.parentElement.textContent.trim() !== el.textContent.trim()) continue;
      const r = el.getBoundingClientRect();
      let [L, T, R, B] = [r.left, r.top, r.right, r.bottom];
      const es = getComputedStyle(el);
      if (es.position !== 'static') {
        for (const pe of ['::before', '::after']) {
          const cs = getComputedStyle(el, pe);
          if (['none', 'normal'].includes(cs.content) || cs.display === 'none' || cs.position !== 'absolute') continue;
          const x0 = r.left + px(es.borderLeftWidth) + px(cs.left), y0 = r.top + px(es.borderTopWidth) + px(cs.top);
          [L, T, R, B] = [Math.min(L, x0), Math.min(T, y0), Math.max(R, x0 + px(cs.width)), Math.max(B, y0 + px(cs.height))];
        }
      }
      const lab = el.matches('input, select, textarea') ? el.closest('label') : null;
      if (lab) { const q = lab.getBoundingClientRect(); [L, T, R, B] = [Math.min(L, q.left), Math.min(T, q.top), Math.max(R, q.right), Math.max(B, q.bottom)]; }
      if (Math.min(R - L, B - T) < 24 - 0.5)
        tiny.push(`${el.tagName}.${el.className} "${(el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 30)}" ${Math.round(R - L)}x${Math.round(B - T)}`);
    }
  }
  return {small, tiny, seen: scopes.length};
})"""

# The hit area of one control, as above (for the 44 px phone check of the inline edit)
HIT = r"""((sel) => {
  const el = document.querySelector(sel), lab = el.closest('label') || el;
  const a = el.getBoundingClientRect(), b = lab.getBoundingClientRect();
  return [Math.max(a.right, b.right) - Math.min(a.left, b.left), Math.max(a.bottom, b.bottom) - Math.min(a.top, b.top)];
})"""


def build(path: Path, today: date, food: float | None = None) -> Path:
    """Demo data with exactly the owner's 7 caps (no rollover); `food`: a Food group budget, and a big grocery run today."""
    conn = db.connect(path)
    try:
        demo_data.build(conn, today=today)
        with conn:
            conn.execute("UPDATE categories SET monthly_cap = NULL")
            conn.execute("DELETE FROM category_rollover")
            for name, value in CAPS.items():
                conn.execute("UPDATE categories SET monthly_cap = ? WHERE name = ?", (value, name))
        if food is not None:
            tx(conn, today.isoformat(), 420.0, "Whole Foods", cat="Groceries", account_id="acc_chase_cc", item_id="demo_chase")
            groupbudget.set_budget(conn, {"group": "Food", "monthly_budget": food})
    finally:
        conn.close()
    return path


@contextmanager
def site(path: Path, width: int):
    today = date.today()
    server = Server(path, today=today)
    try:
        with Chrome() as b:
            b.size(width, 900 if width == 1280 else 844, scale=1 if width == 1280 else 2)
            b.goto(f"{server.url}/#/budgets")
            b.settle()
            yield b, server.url
    finally:
        server.close()


def tap(b, selector):
    box = b.js(f"""(() => {{const el=document.querySelector({json.dumps(selector)});
        el.scrollIntoView({{block:'center'}}); const r=el.getBoundingClientRect();
        return {{x:r.x+r.width/2,y:r.y+r.height/2}};}})()""")
    b.call("Input.dispatchMouseEvent", type="mousePressed", button="left", clickCount=1, **box)
    b.call("Input.dispatchMouseEvent", type="mouseReleased", button="left", clickCount=1, **box)
    b.settled()   # (not settle(): a fixed two frames; a loaded CI runner re-rendered after it)


VK = {"Enter": 13, "Escape": 27, " ": 32, "Backspace": 8}


def key(b, name):
    text = {"Enter": "\r", " ": " "}.get(name)
    code = {" ": "Space"}.get(name, name)
    b.call("Input.dispatchKeyEvent", type="keyDown", key=name, code=code, windowsVirtualKeyCode=VK[name],
           **({"text": text} if text else {}))
    b.call("Input.dispatchKeyEvent", type="keyUp", key=name, code=code, windowsVirtualKeyCode=VK[name])
    b.settled()   # (not settle(): a fixed two frames; a loaded CI runner re-rendered after it)


def typed(b, text):
    """Replace what the focused field holds (its value selected first) with `text`, as typed."""
    b.js("document.activeElement.select()")
    b.call("Input.insertText", text=text)
    b.settled()   # (not settle(): a fixed two frames; a loaded CI runner re-rendered after it)


def q(b, selector, expr="el.textContent.trim()"):
    return b.js(f"(() => {{ const el = document.querySelector({json.dumps(selector)}); return el ? {expr} : null; }})()")


def sizes_ok(b, sel=None):
    got = b.js(f"{SIZES}({json.dumps(sel)})")
    assert got["seen"] and (got["small"], got["tiny"]) == ([], []), got


def shot(b, name, width, full=False):
    if SHOTS:
        SHOTS.mkdir(parents=True, exist_ok=True)
        b.screenshot(SHOTS / f"{name}-{width}.png", full=full)


def stored(path: Path):
    """(group budgets, kept groups, caps) as the database has them now (read-only)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return (groupbudget.amounts(conn), groupbudget.kept(conn),
                {r[0]: r[1] for r in conn.execute("SELECT name, monthly_cap FROM categories WHERE monthly_cap IS NOT NULL")})
    finally:
        conn.close()


FOOD = 'tr.g[data-group="Food"]'
PLAN = f"{FOOD} .gplan"
# addendum 3: the "sum" marker of a group's Planned when it has no budget of its own (null when there's none): its text,
# whether it's painted, its font size, and whether it sits just before the figure, on its line
MARK = r"""((sel) => {
  const s = document.querySelector(sel + ' .gsum'), p = document.querySelector(sel + ' .gplan');
  if (!s) return null;
  const r = s.getBoundingClientRect(), f = p.getBoundingClientRect(), cs = getComputedStyle(s);
  return {text: s.textContent, shown: r.width > 0 && r.height > 0 && cs.visibility === 'visible' && Number(cs.opacity) > 0,
          size: parseFloat(cs.fontSize), before: r.right <= f.left + 0.5 && Math.abs((r.top + r.bottom) - (f.top + f.bottom)) < 12};
})"""


def marked(b, group):
    m = b.js(f"{MARK}({json.dumps(f'tr.g[data-group={json.dumps(group)}]')})")
    return m is not None and m["text"] == "sum" and m["shown"] and m["size"] >= 13 and m["before"] or m


@WIDTHS
def test_a_groups_planned_is_edited_inline(tmp_path, width):
    path = build(tmp_path / "synthetic.sqlite", date.today())
    with site(path, width) as (b, url):
        assert q(b, PLAN) == "$550" and q(b, FOOD, "el.dataset.budget") == ""
        # no budget of its own: Planned is the caps' sum. Group roll-up (2026-10-22): Coffee (uncapped, $ spent) counts
        # in Food, so it sits under the group with Set, not in No budget yet
        assert marked(b, "Food") is None and marked(b, "Transport") is None   # The owner, 2026-10-21: no visible "sum" marker
        coffee_in_food = "tbody.bg[data-group='Food'] tr[data-category='Coffee']"
        assert q(b, coffee_in_food) is not None and q(b, ".nbrow[data-category='Coffee']") is None
        assert q(b, f"{coffee_in_food} .plan.set") == "Set"
        sizes_ok(b)
        # Esc: nothing saved, the focus back on Planned (the editor is the app's sheet, not a row in the table)
        tap(b, PLAN)
        assert q(b, "#modal.on #sheet .bx input", "el === document.activeElement && el.getAttribute('aria-label')") == \
            "Monthly budget for the Food group"
        assert q(b, "#sheet .bx input", "el.placeholder") == "550"
        if width == 390:
            w, h = b.js(f"{HIT}('#sheet .bx input')")
            assert min(w, h) >= 44 - 0.5, (w, h)
        sizes_ok(b)
        typed(b, "999")
        key(b, "Escape")
        assert q(b, "#modal.on") is None
        assert b.js(f"document.activeElement === document.querySelector({json.dumps(PLAN)})")
        assert stored(path)[0] == {}
        # set: $500, Planned is the group budget
        tap(b, PLAN)
        typed(b, "500")
        key(b, "Enter")
        assert stored(path)[0] == {"Food": 500.0}
        assert q(b, PLAN) == "$500" and q(b, FOOD, "el.dataset.budget") == "500"
        assert q(b, PLAN, "el.getAttribute('aria-label')") == "Food group budget: $500. Edit"
        assert q(b, f"{FOOD} .gover") == "categories add up to $550"
        assert marked(b, "Food") is None and marked(b, "Transport") is None
        assert q(b, coffee_in_food) is not None and q(b, ".nbrow[data-category='Coffee']") is None   # still counted in Food
        assert q(b, f"{coffee_in_food} .plan.set") == "Set"   # a group budget doesn't lock it out of one of its own (2026-10-24)
        assert b.js("document.querySelectorAll('tbody.bg[data-group=\"Food\"] tr:not(.g) .plan:not(.set)').length") == 2   # the caps stay
        assert "$1,910" in b.js("document.getElementById('page').textContent")   # 500 + the 5 caps outside Food
        sizes_ok(b)
        # clear: the group is the caps' sum again
        tap(b, PLAN)
        assert q(b, "#sheet .bx input", "el.value") == "500"
        b.js("document.activeElement.select()")
        key(b, "Backspace")
        key(b, "Enter")
        assert stored(path)[0] == {}
        assert q(b, PLAN) == "$550" and q(b, FOOD, "el.dataset.budget") == "" and q(b, f"{FOOD} .gover") is None
        assert marked(b, "Food") is None and q(b, f"{coffee_in_food} .plan.set") == "Set"
        assert "$1,960" in b.js("document.getElementById('page').textContent")
        sizes_ok(b)
        assert b.problems() == []


@WIDTHS
def test_the_review_moves_transport_and_keeps_food(tmp_path, width):
    path = build(tmp_path / "synthetic.sqlite", date.today())
    with site(path, width) as (b, url):
        assert q(b, ".mvb p") == "Move 7 budgets to groups"
        assert b.js("[...document.querySelectorAll('.mvb button')].map((x) => x.textContent)") == ["Review", "Not now"]
        b.js("scrollTo(0, 0)")
        shot(b, "banner", width)
        sizes_ok(b)
        tap(b, ".mvb .got")
        rows = '#sheet .mvrow'
        assert b.js(f"[...document.querySelectorAll('{rows}')].map((r) => r.dataset.group)") == \
            ["Food", "Transport", "Shopping", "Bills", "Health", "Leisure"]
        apply = "#sheet .row .btn:not(.ghost)"
        assert q(b, apply) == "Move 7"
        # Apply is in view, uncovered, before the sheet is scrolled (its row sticks to the sheet's bottom)
        assert q(b, apply, "(() => { const r = el.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight && "
                 "document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2).closest('button') === el; })()") is True
        assert q(b, f'{rows}[data-group="Food"] .mvcaps') == "Groceries $400 + Eating Out $150"
        assert q(b, f'{rows}[data-group="Food"] .cb', "Boolean(el)")
        assert q(b, f'{rows}[data-group="Food"] .mvsp', "el.textContent.endsWith(' spent')")
        assert q(b, f'{rows}[data-group="Transport"] input', "el.value") == "800"
        assert q(b, f'{rows}[data-group="Food"] .mvseg', "el.getAttribute('aria-label')") == \
            "Food: group budget or keep its category budgets"
        assert b.js(f"[...document.querySelectorAll('{rows}[data-group=\"Food\"] .mvseg button')].map((x) => "
                    "[x.textContent, x.getAttribute('aria-pressed')])") == [["Group budget", "true"], ["Keep category budgets", "false"]]
        # Transport's amount changed, Food on Keep with the keyboard
        b.js(f"document.querySelector('{rows}[data-group=\"Transport\"] input').focus()")
        typed(b, "850")
        b.js(f"document.querySelector('{rows}[data-group=\"Food\"] [data-choice=keep]').focus()")
        key(b, "Enter")
        assert b.js(f"[...document.querySelectorAll('{rows}[data-group=\"Food\"] .mvseg button')].map((x) => "
                    "x.getAttribute('aria-pressed'))") == ["false", "true"]
        assert q(b, f'{rows}[data-group="Food"] input', "el.disabled") is True
        assert q(b, apply) == "Move 5"
        b.js("document.getElementById('sheet').scrollTop = 0")
        b.settle()
        shot(b, "review-keep", width)
        sizes_ok(b)
        tap(b, apply)
        assert not b.js("document.getElementById('modal').classList.contains('on')")
        amounts, kept, caps = stored(path)
        assert amounts == {"Transport": 850.0, "Shopping": 200.0, "Bills": 50.0, "Health": 300.0, "Leisure": 60.0}
        assert kept == {"Food"} and caps == {"Groceries": 400.0, "Eating Out": 150.0}
        assert q(b, 'tr.g[data-group="Transport"]', "el.dataset.budget") == "850"
        assert q(b, 'tr.g[data-group="Transport"] .gplan') == "$850"
        assert q(b, FOOD, "el.dataset.budget") == "" and q(b, PLAN) == "$550"
        assert b.js("[...document.querySelectorAll('tbody.bg[data-group=\"Food\"] tr:not(.g) .plan:not(.set)')].map((x) => x.textContent)") == \
            ["$400", "$150"]
        assert q(b, ".mvb") is None                                     # everything listed was decided
        shot(b, "after-apply", width, full=True)
        sizes_ok(b)
        # a cap in a group nobody reviewed brings the banner back for that one only; Food (kept) never comes back
        conn = sqlite3.connect(path)
        with conn:
            conn.execute("UPDATE categories SET monthly_cap = 30 WHERE name = 'Pets'")
            conn.execute("UPDATE categories SET monthly_cap = 40 WHERE name = 'Coffee'")
        conn.close()
        b.goto(f"{url}/?again=1#/budgets")
        b.settle()
        assert q(b, ".mvb p") == "Move 1 budget to groups"
        tap(b, ".mvb .got")
        assert b.js(f"[...document.querySelectorAll('{rows}')].map((r) => r.dataset.group)") == ["Misc"]
        assert q(b, apply) == "Move 1"
        assert b.problems() == []


@WIDTHS
def test_not_now_hides_the_banner_for_the_month(tmp_path, width):
    path = build(tmp_path / "synthetic.sqlite", date.today())
    month = date.today().strftime("%Y-%m")
    with site(path, width) as (b, url):
        assert q(b, ".mvb p") == "Move 7 budgets to groups"
        sizes_ok(b)
        tap(b, ".mvb .later")
        assert q(b, ".mvb") is None
        assert b.js("localStorage.getItem('hf.budgets.move')") == month
        assert b.js("document.activeElement && document.activeElement !== document.body")   # the focus wasn't dropped
        b.goto(f"{url}/?again=1#/budgets")
        b.settle()
        assert q(b, ".mvb") is None and q(b, "table.bt", "Boolean(el)")
        b.goto(f"{url}/?again=1#/overview")
        b.goto(f"{url}/?again=1#/budgets")
        b.settle()
        assert q(b, ".mvb") is None
        sizes_ok(b)
        # a month later it's offered again (nothing was written to the database)
        b.js(f"localStorage.setItem('hf.budgets.move', {json.dumps(insights.add_months(month, -1))})")
        b.goto(f"{url}/?again=2#/budgets")
        b.settle()
        assert q(b, ".mvb p") == "Move 7 budgets to groups"
        assert stored(path)[:2] == ({}, set())
        assert b.problems() == []


@WIDTHS
def test_the_over_note_and_the_overview_card(tmp_path, width):
    path = build(tmp_path / "synthetic.sqlite", date.today(), food=500)
    with site(path, width) as (b, url):
        assert q(b, FOOD, "el.dataset.budget") == "500" and q(b, PLAN) == "$500"
        note = f"{FOOD} .gover"
        assert q(b, note) == "categories add up to $550"
        assert q(b, note, "el.getAttribute('role')") == "note"
        assert q(b, note, "getComputedStyle(el).color") == "rgb(255, 201, 119)"   # the warn tone
        assert q(b, note, "parseFloat(getComputedStyle(el).fontSize)") >= 13
        assert q(b, 'tr.g[data-group="Transport"] .gover') is None
        assert q(b, ".mvb p") == "Move 5 budgets to groups"            # Food has a budget: not offered
        assert "$1,910" in b.js("document.getElementById('page').textContent") and \
            "6 budgets" in b.js("document.getElementById('page').textContent")
        shot(b, "budgets-mixed", width, full=True)
        b.js(f"document.querySelector({json.dumps(FOOD)}).scrollIntoView({{block: 'center'}})")
        b.settle()
        shot(b, "over-note", width)
        sizes_ok(b)
        b.goto(f"{url}/#/overview")
        b.settle()
        rows = b.js("[...document.querySelectorAll('a.brow')].map((r) => [r.dataset.group || null, r.querySelector('.bl1 > b').textContent])")
        assert [g for g, name in rows if name == "Food"] == ["Food"], rows   # one row, the group's
        assert not {"Groceries", "Eating Out", "Coffee", "Alcohol & Bars"} & {name for _, name in rows}, rows
        food = 'a.brow[data-group="Food"]'
        assert q(b, food, "el.querySelector('.cb') !== null") and q(b, f"{food} .amt").endswith("of $500")
        b.js(f"document.querySelector({json.dumps(food)}).closest('section').scrollIntoView({{block: 'start'}})")
        b.settle()
        shot(b, "overview-budgets", width)
        sizes_ok(b, "a.brow, .bfoot")   # the Budgets card's rows and counts (its header link is every card's)
        assert b.problems() == []


@WIDTHS
def test_a_group_with_no_budget_and_no_caps_gets_one_from_no_budget_yet(tmp_path, width):
    """(review of P2e) Home has no group budget and no capped category, so it isn't in the table: its row in
    No budget yet sets a budget on the whole group, and the group then joins the table with that budget."""
    path = build(tmp_path / "synthetic.sqlite", date.today())
    with site(path, width) as (b, url):
        home = '.nb .nbgrp[data-group="Home"]'
        assert q(b, 'tr.g[data-group="Home"]') is None
        assert q(b, home) is not None and "Whole group" in q(b, f"{home} .grow > span"), q(b, home)
        sizes_ok(b)
        tap(b, f"{home} .setc")
        assert q(b, "#modal.on #sheet .bx input", "el === document.activeElement && el.getAttribute('aria-label')") == \
            "Monthly budget for the Home group"
        if width == 390:
            w, h = b.js(f"{HIT}('#sheet .bx input')")
            assert min(w, h) >= 44 - 0.5, (w, h)
        typed(b, "2500")
        key(b, "Enter")
        b.js("new Promise((r) => setTimeout(r, 400))")
        assert stored(path)[0] == {"Home": 2500}
        assert q(b, 'tr.g[data-group="Home"]', "el.dataset.budget") == "2500", q(b, '#page .bt')
        assert q(b, home) is None
        sizes_ok(b)
        assert b.problems() == []


@WIDTHS
def test_a_group_budget_leaves_every_category_a_set(tmp_path, width):
    """The owner, 2026-10-24: under a group budget, its uncapped categories had no Set anywhere: the ones with spending got a
    Spent row without one, the ones with nothing spent had no row at all. Every uncapped category of the group now has a
    Set: under the group when it has spending, else in No budget yet, saying the group already counts it. Setting one
    gives that category a budget of its own and leaves the group's."""
    path = build(tmp_path / "synthetic.sqlite", date.today(), food=500)
    with site(path, width) as (b, url):
        rows = b.js("""(() => { const out = {};
          for (const r of document.querySelectorAll('tbody.bg[data-group="Food"] tr.u')) out[r.dataset.category] = ['table', (r.querySelector('.plan.set') || {}).textContent];
          for (const r of document.querySelectorAll('#page .nb .nbrow[data-category]')) out[r.dataset.category] = ['nb', (r.querySelector('.setc') || {}).textContent, r.querySelector('.grow > span[title]').title];
          return out; })()""")
        caps = stored(path)[2]
        uncapped = [n for n in ("Groceries", "Eating Out", "Coffee", "Alcohol & Bars") if n not in caps]
        assert uncapped, caps
        for n in uncapped:
            assert n in rows and rows[n][1] == "Set", (n, rows)
            if rows[n][0] == "nb":
                assert rows[n][2] == "Counted in Food’s $500", rows[n]
        assert rows["Coffee"][0] == "table"
        # set Coffee from its row: its own budget, the group's untouched
        tap(b, "tbody.bg[data-group='Food'] tr.u[data-category='Coffee'] .plan.set")
        typed(b, "40")
        key(b, "Enter")
        groups, _, caps = stored(path)
        assert caps.get("Coffee") == 40.0 and groups == {"Food": 500.0}
        assert q(b, PLAN) == "$500"
        assert q(b, "tbody.bg[data-group='Food'] tr.c[data-category='Coffee'] .plan") == "$40"
        sizes_ok(b)
        assert b.problems() == []

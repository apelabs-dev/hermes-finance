"""Group (i) Details and the group name as a link to the group's transactions (tests/cdp.py), at 1280 and 390.

On demo data with the owner's 7 caps (Groceries 400, Eating Out 150, Other Transport 800, Other Health 300,
Other Shopping 200, Entertainment 60, Subscriptions 50):
  1. Food's Details popover: six group bars, the dashed line at the caps' sum ($550) or, with a Food group budget of
     $500, at that budget; last month and the 3-month average are the sums from /api/budgets; any group with bills
     lists them merged under "Still due"; this month's breakdown is biggest-first, its amounts the categories' spend and
     its names links to their transactions. Text >= 13px, controls >= 24px, in the page and in the panel.
  2. Food's name is a link to #/transactions?group=Food: clicking it shows the "Food · all" chip and exactly the
     transactions /api/transactions gives for the group this month.
  3. The chevron still folds the group, writes hf.budgets.shut, and the fold survives a reload.
  4. On a phone the chevron and the name link each have a >= 44x44 hit box, do not overlap, and the name stays on one line.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import pytest

import demo_data
from cdp import Chrome, Server, find_chrome
from factory import tx
from hermes_finance import db, groupbudget

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
CAPS = {"Groceries": 400, "Eating Out": 150, "Other Transport": 800, "Other Health": 300, "Other Shopping": 200,
        "Entertainment": 60, "Subscriptions": 50}
SHOTS = Path(os.environ["GROUP_DETAILS_SHOTS"]) if os.environ.get("GROUP_DETAILS_SHOTS") else None
WIDTHS = pytest.mark.parametrize("width", [1280, 390])

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
      if (Math.min(R - L, B - T) < 24 - 0.5)
        tiny.push(`${el.tagName}.${el.className} "${(el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 30)}" ${Math.round(R - L)}x${Math.round(B - T)}`);
    }
  }
  return {small, tiny, seen: scopes.length};
})"""

HIT = r"""((sel) => {
  const el = document.querySelector(sel), r = el.getBoundingClientRect(), es = getComputedStyle(el);
  let [L, T, R, B] = [r.left, r.top, r.right, r.bottom];
  if (es.position !== 'static') for (const pe of ['::before', '::after']) {
    const cs = getComputedStyle(el, pe), px = (v) => parseFloat(v) || 0;
    if (['none', 'normal'].includes(cs.content) || cs.display === 'none' || cs.position !== 'absolute') continue;
    const x0 = r.left + px(es.borderLeftWidth) + px(cs.left), y0 = r.top + px(es.borderTopWidth) + px(cs.top);
    [L, T, R, B] = [Math.min(L, x0), Math.min(T, y0), Math.max(R, x0 + px(cs.width)), Math.max(B, y0 + px(cs.height))];
  }
  return {l: L, t: T, r: R, b: B, w: R - L, h: B - T};
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


def get_json(b, path: str):
    """The API's answer, fetched by the page itself (the test process has no network; the page has its server)."""
    return b.js(f"fetch({json.dumps(path)}).then((r) => r.json())")


def tap(b, selector):
    box = b.js(f"""(() => {{const el=document.querySelector({json.dumps(selector)});
        el.scrollIntoView({{block:'center'}}); const r=el.getBoundingClientRect();
        return {{x:r.x+r.width/2,y:r.y+r.height/2}};}})()""")
    b.call("Input.dispatchMouseEvent", type="mousePressed", button="left", clickCount=1, **box)
    b.call("Input.dispatchMouseEvent", type="mouseReleased", button="left", clickCount=1, **box)
    b.settle()


def key(b, name):
    vk = {"Escape": 27, "Enter": 13}
    b.call("Input.dispatchKeyEvent", type="keyDown", key=name, code=name, windowsVirtualKeyCode=vk[name])
    b.call("Input.dispatchKeyEvent", type="keyUp", key=name, code=name, windowsVirtualKeyCode=vk[name])
    b.settle()


def q(b, selector, expr="el.textContent.trim()"):
    return b.js(f"(() => {{ const el = document.querySelector({json.dumps(selector)}); return el ? {expr} : null; }})()")


def money(x: float) -> str:
    return f"${x:,.0f}"


def sizes_ok(b, sel=None):
    got = b.js(f"{SIZES}({json.dumps(sel)})")
    assert got["seen"] and (got["small"], got["tiny"]) == ([], []), got


def shot(b, name, width):
    if SHOTS:
        SHOTS.mkdir(parents=True, exist_ok=True)
        b.screenshot(SHOTS / f"{name}-{width}.png")


def by_name(d):
    return {c["category"]: c for c in d["categories"]}


def group(d, name):
    return next(g for g in d["groups"] if g["name"] == name)


def expected_breakdown(d, g):
    cats, order = by_name(d), {n: i for i, n in enumerate(g["categories"])}
    rows = [cats[n] for n in g["categories"] if n in cats and cats[n]["spend"] != 0]
    rows.sort(key=lambda c: (-c["spend"], order[c["category"]]))
    return [(c["category"], f"${c['spend']:,.0f}") for c in rows]


FOOD = 'tr.g[data-group="Food"]'
PANEL = '.help-panel.detail'


@WIDTHS
@pytest.mark.parametrize("food", [None, 500], ids=["caps-sum", "group-budget"])
def test_a_groups_details_popover(tmp_path, width, food):
    path = build(tmp_path / "synthetic.sqlite", date.today(), food=food)
    with site(path, width) as (b, url):
        d = get_json(b, "/api/budgets")
        g = group(d, "Food")
        sizes_ok(b)
        tap(b, f'{FOOD} .help-trigger[data-help="budget-group"]')
        assert q(b, PANEL, "el.getAttribute('aria-label')") == "Details for Food"
        # 1. the group's six bars, the dashed line at the group budget or the caps' sum
        assert b.js(f"document.querySelectorAll({json.dumps(PANEL + ' .hist .hc')}).length") == len(d["history"]["months"]) == 6
        # ...and every bar is that month's sum over the group's categories: its label carries the figure
        series = d["history"]["series"]
        sums = [round(sum(((series.get(n) or [])[k] if k < len(series.get(n) or []) else 0)
                          for n in g["categories"]), 2) for k in range(len(d["history"]["months"]))]
        labels = b.js(f"[...document.querySelectorAll({json.dumps(PANEL + ' .hist .hc')})].map((e) => e.getAttribute('aria-label'))")
        got_sums = []
        for label in labels:
            m = re.search(r"(-?[\d,]+\.\d\d)", label)
            assert m, label
            got_sums.append(round(-float(m.group(1).replace(",", "")) if "net refund" in label
                                  else float(m.group(1).replace(",", "")), 2))
        assert got_sums == sums, (got_sums, sums)
        cap = 500 if food is not None else 550
        assert q(b, PANEL + " .hist .cap", "el.title") == (f"Group budget ${cap}" if food is not None
                                                           else f"Category budgets added up ${cap}")
        note = q(b, PANEL + " .hist small")
        assert money(cap) in note and ("group budget" if food is not None else "category budgets added up") in note, note
        assert b.js(f"document.querySelector({json.dumps(PANEL + ' .hist .cap')}).getBoundingClientRect().width > 0")
        # 2. last month and the 3-month average: sums over the group's categories
        cats = by_name(d)
        last = sum((cats.get(n) or {}).get("last_month", 0) for n in g["categories"])
        avg = sum((cats.get(n) or {}).get("avg3", 0) for n in g["categories"])
        assert b.js(f"[...document.querySelectorAll({json.dumps(PANEL + ' .row3 .v')})].map((e) => e.textContent)") == \
            [money(last), money(avg)]
        # 4. the breakdown: biggest first, its spend, each name a link to that category's transactions
        got = b.js(f"[...document.querySelectorAll({json.dumps(PANEL + ' .bd .bdr')})].map((r) => "
                   "[r.dataset.category, r.querySelector('.v').textContent, r.querySelector('a').getAttribute('href')])")
        want = expected_breakdown(d, g)
        assert [(name, amount) for name, amount, _ in got] == want, got
        for name, _, href in got:
            assert href == "#/transactions?" + urlencode({"category": name}), href
        assert b.js(f"document.querySelectorAll({json.dumps(PANEL + ' .bd .bdr')}).length") == len(got) > 0
        sizes_ok(b)
        sizes_ok(b, PANEL)
        shot(b, f"popover-{'budget' if food is not None else 'sum'}", width)
        key(b, "Escape")
        assert q(b, PANEL) is None

        # any group with bills lists them merged, in the group's taxonomy order
        billed = next(g for g in d["groups"] if any((cats.get(n) or {}).get("bills_due") for n in g["categories"]))
        merged = [bill for n in billed["categories"] for bill in (cats.get(n) or {}).get("bills_due", [])]
        sel = f'tr.g[data-group={json.dumps(billed["name"])}] .help-trigger[data-help="budget-group"]'
        tap(b, sel)
        heads = b.js(f"[...document.querySelectorAll({json.dumps(PANEL + ' h4')})].map((e) => e.textContent)")
        assert "Still due" in heads and "This month" in heads, heads
        assert b.js(f"document.querySelectorAll({json.dumps(PANEL + ' .help-bill')}).length") == len(merged) > 0
        assert b.js(f"[...document.querySelectorAll({json.dumps(PANEL + ' .help-bill > span')})].map((e) => e.childNodes[0].textContent)") == \
            [bill["merchant"] for bill in merged]
        sizes_ok(b, PANEL)
        key(b, "Escape")
        assert b.problems() == []


@WIDTHS
def test_the_group_name_opens_its_transactions(tmp_path, width):
    path = build(tmp_path / "synthetic.sqlite", date.today())
    month = date.today().strftime("%Y-%m")
    with site(path, width) as (b, url):
        link = f'{FOOD} a.nml'
        assert q(b, link, "el.getAttribute('href')") == "#/transactions?group=Food"
        assert q(b, link) == "Food"
        tap(b, link)
        assert b.js("location.hash") == "#/transactions?group=Food"
        assert b.until("Boolean(document.querySelector('.txchips .tfchip'))")   # (the page's chips are drawn)
        assert q(b, ".txchips .tfchip") == "Food · all"
        want = get_json(b, "/api/transactions?" + urlencode({"group": "Food", "month": month, "limit": 50}))["rows"]
        assert b.js("[...document.querySelectorAll('#page tr[data-tid]')].map((r) => r.dataset.tid)") == \
            [r["transaction_id"] for r in want] != []
        assert b.problems() == []
        shot(b, "transactions-food", width)


@WIDTHS
def test_the_chevron_still_folds_and_is_remembered(tmp_path, width):
    path = build(tmp_path / "synthetic.sqlite", date.today())
    with site(path, width) as (b, url):
        def folded():
            return b.js(f"""(() => {{ const t = document.querySelector('tbody[data-group="Food"]');
              const btn = t.querySelector('.gbtn'), rows = [...t.querySelectorAll('tr:not(.g)')];
              return {{expanded: btn.getAttribute('aria-expanded'), shut: t.classList.contains('shut'),
                hidden: rows.length > 0 && rows.every((r) => r.hidden)}}; }})()""")
        assert folded() == {"expanded": "true", "shut": False, "hidden": False}
        tap(b, f"{FOOD} .gbtn")
        assert folded() == {"expanded": "false", "shut": True, "hidden": True}
        assert b.js("JSON.parse(localStorage.getItem('hf.budgets.shut'))") == ["Food"]
        b.goto(f"{url}/?again=1#/budgets")
        b.settle()
        assert folded()["shut"] is True
        assert q(b, f"{FOOD} a.nml", "el.closest('tbody').classList.contains('shut')") is True
        assert b.problems() == []


def test_on_a_phone_every_group_count_and_details_sit_the_same_distance_from_its_name(tmp_path):
    """A short name (Food) must not push its count and (i) further out than a long one (Transport): the 44px hit box
    comes from the link's ::after, never from a min-width that pads the link itself."""
    path = build(tmp_path / "synthetic.sqlite", date.today())
    with site(path, 390) as (b, url):
        rows = b.js("""[...document.querySelectorAll('.ghl')].filter((l) => l.offsetParent).map((l) => {
          const a = l.querySelector('a.gn'), rg = document.createRange(); rg.selectNodeContents(a);
          const text = rg.getBoundingClientRect(), box = a.getBoundingClientRect();
          const c = l.querySelector('.gc').getBoundingClientRect();
          return {name: a.textContent, pad: box.width - text.width, gap: c.left - text.right}; })""")
        assert len(rows) >= 3 and any(len(r["name"]) <= 5 for r in rows), rows
        assert all(r["pad"] <= 0.5 for r in rows), rows
        gaps = [r["gap"] for r in rows]
        assert max(gaps) - min(gaps) <= 1, rows
        name = b.js(f"{HIT}({json.dumps(FOOD + ' a.nml')})")
        assert min(name["w"], name["h"]) >= 44 - 0.5, name
        assert b.problems() == []


def test_on_a_phone_the_fold_and_the_name_do_not_overlap(tmp_path):
    path = build(tmp_path / "synthetic.sqlite", date.today())
    with site(path, 390) as (b, url):
        btn = b.js(f"{HIT}({json.dumps(FOOD + ' .gbtn')})")
        name = b.js(f"{HIT}({json.dumps(FOOD + ' a.nml')})")
        assert min(btn["w"], btn["h"]) >= 44 - 0.5, btn
        assert min(name["w"], name["h"]) >= 44 - 0.5, name
        overlap = min(btn["r"], name["r"]) - max(btn["l"], name["l"])
        overlap_v = min(btn["b"], name["b"]) - max(btn["t"], name["t"])
        assert overlap <= 0.5 or overlap_v <= 0.5, (btn, name)
        line = b.js(f"""(() => {{ const a = document.querySelector({json.dumps(FOOD + ' a.nml')});
          const rg = document.createRange(); rg.selectNodeContents(a);
          return {{rects: a.getClientRects().length, textLines: [...rg.getClientRects()].length,
            text: rg.getBoundingClientRect().width, client: a.clientWidth, wrap: getComputedStyle(a).whiteSpace}}; }})()""")
        assert line["rects"] == 1 and line["textLines"] == 1, line
        # the text itself fits the link (scrollWidth would also count the transparent ::after hit box that grows a short
        # name to 44px sideways, which is meant to stick out)
        assert line["wrap"] == "nowrap" and line["text"] <= line["client"] + 1, line
        sizes_ok(b)
        assert b.problems() == []

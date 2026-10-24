"""Option A (quiet, content-first help) + Option C (aligned budget footers) as production behaviour.

MOCK-023 was a mock injection; this pins the same two decisions in the app's own stylesheet, measured on the
synthetic features DB:
  A  the bare info trigger is a borderless 24px circle with the existing 18px glyph; a labelled trigger is
     readable 13px text with no box; labels and masks sit a consistent 10px from their control; a phone keeps a
     44 x 44 invisible hit box; the focus ring is explicit.
  C  a budget row (P2A: the Budgets page is a table) keeps the name and its Details disclosure at the left, the Planned
     button (the Edit control) in its own column, and the editor in a row of its own below. Rows with no bills keep
     Planned in the same place, and Edit -> Cancel puts the disclosure back (the restore() path rebuilds it).

Run through scripts/check.py's safe_env; no provider calls and no production writes.
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")

# The pages the coverage inventory (MOCK-023) found help triggers on, plus the two with none.
PAGES = ["overview", "budgets", "accounts", "debt", "recurring", "goals", "settings", "transactions", "investments"]
DESKTOP = [1440, 1280, 1024]
PHONE = [360, 390, 430, 768]


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("helpa") / "finance.db"
    gate_dbs.build("features", path, date.today())
    srv = Server(path, today=date.today())
    try:
        yield srv
    finally:
        srv.close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


def go(b, site, page, width=390):
    b.size(width, 900)
    b.goto(f"{site.url}/?helpa={time.time()}#/{page}")
    b.settle()
    b.js("new Promise(r => setTimeout(r, 200))")


def key(b, value):
    b.call("Input.dispatchKeyEvent", type="keyDown", key=value, code=value,
           windowsVirtualKeyCode={"Enter": 13, "Escape": 27, "Tab": 9, " ": 32}[value], text="\r" if value == "Enter" else "")
    b.call("Input.dispatchKeyEvent", type="keyUp", key=value, code=value,
           windowsVirtualKeyCode={"Enter": 13, "Escape": 27, "Tab": 9, " ": 32}[value])
    b.js("new Promise(r => setTimeout(r, 100))")


def clean(b):
    assert b.problems() == []
    assert b.js("document.documentElement.scrollWidth <= innerWidth + 1")


def _triggers(b):
    """Every visible help control on the page, with the geometry Option A specifies."""
    return b.js("""(() => [...document.querySelectorAll('#page .help-trigger')].filter((e) => e.getClientRects().length)
      .map((e) => {
        const cs = getComputedStyle(e), r = e.getBoundingClientRect();
        const svg = e.querySelector('svg');
        return {labelled: e.classList.contains('labelled'), w: Math.round(r.width), h: Math.round(r.height),
          border: [cs.borderTopWidth, cs.borderRightWidth, cs.borderBottomWidth, cs.borderLeftWidth].join(' '),
          radius: cs.borderTopLeftRadius, bg: cs.backgroundColor, colour: cs.color,
          font: parseFloat(cs.fontSize), cut: e.scrollWidth > e.clientWidth + 1,
          glyph: svg ? Math.round(svg.getBoundingClientRect().width) : null,
          glyphIn: svg ? svg.getBoundingClientRect().width <= r.width : null,
          outline: cs.outlineWidth, offset: cs.outlineOffset};
      }))()""")


# --- Option A: the bare info control is quiet and content-first -------------------------------------------

@pytest.mark.parametrize("page", PAGES)
def test_bare_help_trigger_is_a_borderless_24px_circle(chrome, site, page):
    b = chrome
    go(b, site, page, 1440)
    bare = [t for t in _triggers(b) if not t["labelled"]]
    if page == "budgets":
        # 2026-10-02: budgets NOW has bare call sites — each no-budget row's Details disclosure for its budget
        # suggestion is the standard 24px info glyph (it is the word "Details" that did not fit a 254px row). These are
        # held to exactly the same geometry as every other bare control, so they are asserted, not excused.
        assert bare, "budgets renders the per-row suggestion disclosures"
        for t in bare:
            assert (t["w"], t["h"]) == (24, 24), t
            assert t["border"] == "0px 0px 0px 0px" and t["bg"] == "rgba(0, 0, 0, 0)", t
            assert t["glyph"] == 18 and t["glyphIn"] and not t["cut"], t
        clean(b)
        return
    if page in ("transactions", "investments"):
        # The coverage inventory: these pages have no bare icon call sites.
        assert bare == [], f"{page} has no bare help call site; a trigger appeared: {bare}"
        clean(b)
        return
    assert bare, f"{page} renders no bare help control"
    for t in bare:
        assert (t["w"], t["h"]) == (24, 24), (page, t, "the bare trigger is not 24 x 24")
        assert t["border"] == "0px 0px 0px 0px", (page, t, "Option A draws no border on the info control")
        assert t["bg"] in ("rgba(0, 0, 0, 0)", "transparent"), (page, t, "the info control is not quiet")
        assert t["radius"] == "50%", (page, t, "the bare trigger is not round")
        assert t["glyph"] == 18, (page, t, "the existing 18px info glyph shrank")
        assert t["glyphIn"], (page, t, "the glyph does not fit its control")
        assert not t["cut"], (page, t, "the control clips its own content")
    clean(b)


@pytest.mark.parametrize("page", ["overview"])
def test_labelled_help_is_readable_13px_text_without_a_box(chrome, site, page):
    """The labelled call sites (the Overview "Budget breakdown"; P2A: a Budgets row's Details is now the bare icon in the
    row's name line, covered by the bare-trigger test) must be readable text with no box."""
    b = chrome
    go(b, site, page, 1440)
    labelled = [t for t in _triggers(b) if t["labelled"]]
    assert labelled, f"{page} renders no labelled disclosure"
    for t in labelled:
        assert t["font"] == 13, (page, t, "a labelled disclosure is not 13px")
        assert t["border"] == "0px 0px 0px 0px", (page, t, "a labelled disclosure is boxed")
        assert t["bg"] in ("rgba(0, 0, 0, 0)", "transparent"), (page, t, "a labelled disclosure has a background")
        assert not t["cut"], (page, t, "the label is clipped")
    clean(b)


@pytest.mark.parametrize("page", ["overview", "accounts", "budgets", "recurring"])
def test_label_and_mask_sit_a_consistent_10px_from_their_control(chrome, site, page):
    """Help remains beside its label; approved Accounts rows use a 6px gap plus 2px control margin."""
    b = chrome
    go(b, site, page, 1440)
    rows = b.js("""(() => {
      const gaps = [];
      for (const t of document.querySelectorAll('#page .help-trigger')) {
        if (!t.getClientRects().length) continue;
        const p = t.parentElement;
        const cs = getComputedStyle(p);
        if (p.classList.contains('help-trigger')) continue;          // inside a panel
        const tr = t.getBoundingClientRect();
        const sib = [...p.children].filter((c) => c !== t && c.getClientRects().length
          && c.getBoundingClientRect().width);
        if (!sib.length) continue;
        const box = p.getBoundingClientRect();
        // The horizontal distance to the nearest sibling, either side, on the same line.
        const near = sib.map((c) => c.getBoundingClientRect())
          .filter((r) => r.top < tr.bottom - 2 && r.bottom > tr.top + 2)
          .map((r) => Math.min(Math.abs(r.left - tr.right), Math.abs(tr.left - r.right)));
        gaps.push({dense: !!t.closest('.r2'), key: t.dataset.help || '', tag: p.className || p.tagName,
          colGap: parseFloat(cs.columnGap) || 0, near: near.length ? Math.min(...near) : null,
          label: (t.getAttribute('aria-label') || '').slice(0, 40), box: Math.round(box.width)});
      }
      return gaps; })()""")
    assert rows, page
    for g in rows:
        if g["near"] is None:
            continue  # a trigger that leads its row (an account row button) has nothing to the left
        assert g["near"] >= 8, (page, g, "the control is glued to the text it explains")
        assert g["colGap"] in ((6.0,) if g["dense"] else (0.0, 10.0)), (page, g, "unexpected help spacing")


@pytest.mark.parametrize("width", PHONE)
def test_a_phone_keeps_a_44px_invisible_hit_box(chrome, site, width):
    b = chrome
    go(b, site, "accounts", width)
    b.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
    go(b, site, "accounts", width)
    assert b.js("matchMedia('(max-width: 1023.98px)').matches")
    hits = b.js("""(() => [...document.querySelectorAll('#page .help-trigger')].filter((e) => e.getClientRects().length)
      .map((e) => { const r = e.getBoundingClientRect(), cs = getComputedStyle(e, '::before');
        return {own: [Math.round(r.width), Math.round(r.height)],
          before: [Math.round(parseFloat(cs.width)), Math.round(parseFloat(cs.height))],
          bg: cs.backgroundColor, pos: cs.position, label: (e.getAttribute('aria-label') || '').slice(0, 30)}; }))()""")
    assert hits, "accounts renders help controls"
    for h in hits:
        assert h["own"] == [24, 24], h
        assert h["pos"] == "absolute", h
        assert min(h["before"]) >= 44, (h, "the 44px phone hit box is not there")
        assert h["bg"] in ("rgba(0, 0, 0, 0)", "transparent"), (h, "the hit box is not invisible")
    b.call("Emulation.setTouchEmulationEnabled", enabled=False)


def test_the_focus_ring_is_explicit(chrome, site):
    b = chrome
    go(b, site, "settings", 1440)
    b.js("document.body.focus()")
    # Tab through the page until the help control has keyboard focus (it is late in the tab order).
    for _ in range(200):
        key(b, "Tab")
        if b.js("!!(document.activeElement && document.activeElement.classList.contains('help-trigger'))"):
            break
    else:
        pytest.fail("no help control was reached with the keyboard")
    ring = b.js("""(() => { const e = document.activeElement, cs = getComputedStyle(e);
      return {focusVisible: e.matches(':focus-visible'), width: parseFloat(cs.outlineWidth),
        style: cs.outlineStyle, offset: parseFloat(cs.outlineOffset)}; })()""")
    assert ring["focusVisible"], ring
    assert ring["style"] != "none" and ring["width"] >= 2 and ring["offset"] >= 2, ring
    clean(b)


# --- Option C: a budget row keeps its name, its Details and its Planned button apart ------------------------
# P2A: the Budgets page is a table (budgets.js). What the card footer guaranteed - the amount, the Details disclosure and
# Edit never overlap, nothing is cut, Cancel puts everything back - is now guaranteed per row: the name and Details on the
# left, the Planned button (the Edit control) in its own column, and the editor in a row of its own under the category.

ROWS = """(() => [...document.querySelectorAll('#page tr.c')].map((c) => {
  const box = (e) => { const r = e.getBoundingClientRect();
    return {top: Math.round(r.top), left: Math.round(r.left), right: Math.round(r.right), bottom: Math.round(r.bottom),
            w: Math.round(r.width), h: Math.round(r.height)}; };
  const nm = c.querySelector('.nml'), det = c.querySelector('[data-help="budget-bills"]'), plan = c.querySelector('.plan');
  const kids = [...c.querySelectorAll('.nm .l > *, .p > *, .a > *, .r > *')].filter((e) => e.getClientRects().length)
    .map((e) => ({txt: (e.textContent || '').trim().slice(0, 24), r: box(e)}));
  const clashes = [];
  for (let i = 0; i < kids.length; i++) for (let j = i + 1; j < kids.length; j++) {
    const a = kids[i].r, z = kids[j].r;
    if (a.left < z.right && z.left < a.right && a.top < z.bottom && z.top < a.bottom) clashes.push([kids[i].txt, kids[j].txt]);
  }
  return {name: c.dataset.category, due: Number(c.dataset.due) > 0, det: det ? box(det) : null, plan: plan ? box(plan) : null,
    row: box(c), nameBox: box(nm), nameCut: nm.scrollWidth > nm.clientWidth + 1, detCut: det ? det.scrollWidth > det.clientWidth + 1 : false,
    detLabel: det ? det.getAttribute('aria-label') || det.textContent.trim() : null,
    planText: plan ? plan.textContent.trim() : null, planLabel: plan ? plan.getAttribute('aria-label') : null,
    pills: c.querySelectorAll('.rp').length, clashes, overflow: c.scrollWidth > c.clientWidth + 1};
}))()"""


@pytest.mark.parametrize("width", DESKTOP + PHONE)
def test_a_budget_row_puts_the_name_first_and_planned_in_its_own_place(chrome, site, width):
    """Every capped category is a row with a Details disclosure and a Planned button, which never overlap each other or
    the Spent / Left cells, and no label is cut. Desktop: Planned sits to the right of the name and its Details.
    Phone: the row reflows (name and Left on the first line, "Planned" and "Spent" on the second), still no overlaps."""
    b = chrome
    go(b, site, "budgets", width)
    rows = b.js(ROWS)
    assert rows, "the features DB renders budget rows"
    assert any(r["due"] for r in rows) and not all(r["due"] for r in rows), "the fixture needs rows with and without bills due"
    for r in rows:
        assert r["det"] and r["plan"], r["name"]
        assert not r["clashes"], (r["name"], r["clashes"])
        assert not r["nameCut"] and not r["overflow"], r
        if width >= 1024:
            assert not r["detCut"], r        # (a phone's 44px invisible hit box is what makes scrollWidth > clientWidth there)
        assert r["planLabel"].startswith(f"Budget for {r['name']}: ") and r["planLabel"].endswith("Edit"), r
        assert r["pills"] == 1, (r["name"], "one Left pill per row")
        assert r["det"]["bottom"] > r["det"]["top"] and r["plan"]["bottom"] > r["plan"]["top"], r
        if width >= 768:
            assert r["det"]["right"] <= r["plan"]["left"] + 1, (r["name"], r["det"], r["plan"])    # Planned right of Details
            assert abs((r["det"]["top"] + r["det"]["bottom"]) - (r["plan"]["top"] + r["plan"]["bottom"])) <= 40, r  # one band
        assert "rolled over" not in r["planText"], r     # the rollover is a Details fact, never glued onto the number
    clean(b)


def test_edit_and_cancel_restore_the_disclosure(chrome, site):
    """The editor opens as a sheet over the page, so Cancel must close it, leave the row (Details, Planned) intact and
    give the focus back to Planned."""
    b = chrome
    go(b, site, "budgets", 1440)
    out = b.js("""(async () => {
      const wait = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
      const row = [...document.querySelectorAll('#page tr.c')].find((c) => Number(c.dataset.due) > 0);
      if (!row) return {skipped: 'no row with bills due'};
      const has = () => ({det: !!row.querySelector('[data-help="budget-bills"]'), plan: !!row.querySelector('.plan'),
        editor: !!document.querySelector('#modal.on #sheet .bx input')});
      const before = has();
      row.querySelector('.plan').click();
      await wait();
      const open = has();
      [...document.querySelectorAll('#sheet .bx-foot button')].find((x) => x.textContent === 'Cancel').click();
      await wait(); await new Promise((r) => setTimeout(r, 300));
      return {before, open, after: has(), focused: document.activeElement.className};
    })()""")
    if out.get("skipped"):
        pytest.skip(out["skipped"])
    assert out["before"]["det"] and out["before"]["plan"] and not out["before"]["editor"], out
    assert out["open"]["editor"] and out["open"]["det"] and out["open"]["plan"], out
    assert out["after"]["det"] and out["after"]["plan"] and not out["after"]["editor"], out
    assert out["focused"] == "plan", out
    clean(b)


def test_a_due_disclosure_still_opens_and_escapes(chrome, site):
    b = chrome
    go(b, site, "budgets", 1440)
    b.js("document.querySelector('#page tr.c[data-due]:not([data-due=\"0\"]) [data-help=budget-bills]').click()")
    b.js("new Promise(r => setTimeout(r, 250))")
    panel = b.js("""(() => { const p = document.querySelector('.help-panel');
      if (!p) return null; const r = p.getBoundingClientRect();
      return {role: p.getAttribute('role'), bills: p.querySelectorAll('.help-bill').length,
        text: p.textContent.slice(0, 40),
        inside: r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight}; })()""")
    assert panel, "the disclosure opens a panel"
    assert panel["role"] == "dialog" and panel["bills"] >= 1 and panel["inside"], panel
    key(b, "Escape")
    assert b.js("!document.querySelector('.help-panel')")
    assert b.js("document.activeElement.dataset.help") == "budget-bills"
    clean(b)


# ---------------------------------------------------------------- Option C: the credit-card disclosure
#
# Option C is one decision: an account row keeps its HEADLINE FACTS inline and the EXPLANATION of why its balance is
# approximate goes in the trigger's panel. The disclosure is the "last set $X on DATE, minus $Y matched payments,
# update it from a statement" tail — a reader who only wants the headline must not have to read past it.
#
# Both halves are asserted. The panel half alone would pass against the old code for the wrong reason (the old panel
# carried a shorter generic version of the same sentence), so the tests pin BOTH that the row is free of the
# disclosure AND that the panel carries the specific matched-payments explanation the old row had.

def _cc_rows(b):
    """Every APPROXIMATE-BALANCE credit-card row: its inline subtitle and the trigger that leads it.

    The row is picked by the amount's `title="Approximate: …"` marker (app.js sets it only when `m.approx`), NOT by
    the mere presence of an amount — every account row has one, so a looser selector also collects savings accounts
    and loans, whose panels carry no manual-balance disclosure.
    """
    return b.js("""(() => [...document.querySelectorAll('#page .account-entry')]
      .filter((e) => e.querySelector('.li .amt[title*="Approximate"]'))
      .map((e) => ({
        sub: (e.querySelector('.li .grow > span:not(.track)') || {}).textContent || '',
        helpKey: (e.querySelector('.help-trigger') || {}).dataset?.help || null,
      })))()""")


def test_c_a_credit_card_row_keeps_only_its_headline_facts(chrome, site):
    """The inline subtitle is the cap fact and the date — never the matched-payments disclosure."""
    b = chrome
    go(b, site, "accounts", 1440)
    rows = _cc_rows(b)
    assert rows, "the features DB has approximate-balance credit cards to check"
    for r in rows:
        low = r["sub"].lower()
        assert "matched payment" not in low, f"the disclosure leaks inline: {r['sub']!r}"
        assert "last set" not in low, f"'last set' is disclosure, not a headline fact: {r['sub']!r}"
        assert "statement" not in low, f"the nudge is disclosure, not a headline fact: {r['sub']!r}"
        # what stays: the headline facts are still there, so the row did not simply lose its subtitle
        assert low.strip(), "the row keeps a subtitle"
        assert "%" in low or "updated" in low, f"the headline facts survive: {r['sub']!r}"
    clean(b)


def test_c_the_disclosure_lives_in_the_credit_card_panel(chrome, site):
    """The panel carries the whole explanation: the entered balance, the matched payments, the nudge."""
    b = chrome
    go(b, site, "accounts", 1440)
    rows = _cc_rows(b)
    assert rows, "the features DB has approximate-balance credit cards to check"
    shown = 0
    for i, r in enumerate(rows):
        if not r["helpKey"]:
            continue
        # The card row's OWN trigger: data-help names the kind of help ('manual-account' on every manual row), not the
        # row. A page-wide [data-help=...] lookup only hit Best Buy while "Added by you" listed it first; approved
        # density mock D2 B removed that card, so the first one on the page is now another account's.
        b.js(f"""[...document.querySelectorAll('#page .account-entry')]
          .filter((e) => e.querySelector('.li .amt[title*="Approximate"]'))[{i}].querySelector('.help-trigger').click()""")
        b.js("new Promise(r => setTimeout(r, 220))")
        panel = b.js("""(() => { const p = document.querySelector('.help-panel');
          return p ? p.textContent : null; })()""")
        assert panel, "the account's disclosure opens a panel"
        low = panel.lower()
        assert "manual balance" in low, panel
        assert "matched payment" in low, f"the matched-payments explanation belongs in the panel: {panel}"
        assert "statement" in low, f"the nudge belongs in the panel: {panel}"
        shown += 1
        key(b, "Escape")
        assert b.js("!document.querySelector('.help-panel')")
    assert shown, "at least one credit-card disclosure was opened and checked"
    clean(b)


# --- Bug 1 / Bug 2 / Bug 3 (2026-10-02): one line, one chip, suggestion in the panel ---------------------
# (P2A: the "footer" is the Planned cell of a table row, the "header chip" is the row's Left pill)

@pytest.mark.parametrize("width", DESKTOP)
def test_a_budget_planned_cell_is_the_short_amount_and_never_wraps(chrome, site, width):
    """Bug 1: Planned reads `$X` (the cap in force) as one line, with Details named and nothing overflowing.

    The long `+ $97.17 rolled over` label stays out of the cell by decision (the owner, 2026-10-02) - it is the same fact
    as the rollover disclosure. What must hold: the cell is one line, nothing wraps, nothing overflows the row.
    """
    b = chrome
    go(b, site, "budgets", width)
    rows = b.js("""(() => [...document.querySelectorAll('#page tr.c')].map(c => {
      const plan = c.querySelector('.plan'), cs = getComputedStyle(plan);
      const det = c.querySelector('[data-help="budget-bills"]');
      return {name: c.dataset.category, text: plan.textContent.trim(), white: cs.whiteSpace,
        planH: Math.round(plan.getBoundingClientRect().height), rowH: Math.round(c.getBoundingClientRect().height),
        overflows: c.scrollWidth > c.clientWidth + 1, detLabel: det ? det.getAttribute('aria-label') : null};
    }))()""")
    assert rows, "budget rows render"
    for r in rows:
        assert r["text"].startswith("$") and "rolled over" not in r["text"], r
        assert r["planH"] <= 34 and r["rowH"] <= 90, r
        assert not r["overflows"], r
        assert r["detLabel"] == f"Details for {r['name']}", r     # the disclosure is named `Details`, not `Bills due details`
    clean(b)


def test_a_budget_row_carries_one_status_pill_only(chrome, site):
    """Bug 3 (option 3a): a row holds ONE conditional status thing, the Left pill, and nothing else.

    `Still due` is in the Details panel, so the row never has to break to fit a second chip.
    """
    b = chrome
    go(b, site, "budgets", 1280)
    rows = b.js("""(() => [...document.querySelectorAll('#page tr.c')].map(c => {
      const nm = c.querySelector('.nml');
      return {name: c.dataset.category, pills: [...c.querySelectorAll('.rp, .pill')].map(p => (p.textContent || '').trim()),
        nameCut: nm.scrollWidth > nm.clientWidth + 1, overflow: c.scrollWidth > c.clientWidth + 1};
    }))()""")
    assert rows, "budget rows render"
    for r in rows:
        assert len(r["pills"]) == 1, (r, "a row must carry a single status chip")
        assert not r["pills"][0].startswith("Still due"), (r, "`Still due` belongs in the Details panel")
        assert not r["overflow"] and not r["nameCut"], r
    clean(b)


def test_the_still_due_fact_is_reachable_in_the_details_panel(chrome, site):
    """The counterpart of the pill test: moving `Still due` must not delete it.

    A regression here is silent - nothing fails if the fact simply vanishes, so this asserts it is still rendered
    SOMEWHERE for a row that has bills due.
    """
    b = chrome
    go(b, site, "budgets", 1280)
    res = b.js("""(() => {
      const withDue = [...document.querySelectorAll('#page tr.c')].filter(c => Number(c.dataset.due) > 0);
      if (!withDue.length) return {checked: 0};
      withDue[0].querySelector('[data-help="budget-bills"]').click();
      return new Promise(r => setTimeout(() => {
        const panel = document.querySelector('.help-panel');
        const txt = panel ? (panel.innerText || '') : '';
        r({checked: withDue.length, opened: !!panel, hasHeading: /Still due/.test(txt),
           hasBills: /due|bill/i.test(txt), snippet: txt.slice(0, 160)});
      }, 250));
    })()""")
    assert res.get("checked"), "the fixture needs at least one row with bills due"
    assert res["opened"], res
    assert res["hasHeading"], res
    assert res["hasBills"], res
    clean(b)


@pytest.mark.parametrize("width", DESKTOP)
def test_no_budget_rows_are_one_line_and_all_the_same_height(chrome, site, width):
    """Bug 2: every visible `No budget yet` row is a single line, and no two rows differ in height.

    The rows were 65/104/85/104 at 1280 because the `Suggested $X · Use` line only rendered on some of them. That
    line is in the row's Details panel, so nothing conditional is left to stagger the rows. (P2A: a row may now carry a
    `Use $X` button next to `Set`; rows with nothing spent are folded away until asked for, so only visible ones count.)
    """
    b = chrome
    go(b, site, "budgets", width)
    res = b.js("""(() => {
      const rows = [...document.querySelectorAll('#page .nb .li')].filter(r => r.getClientRects().length);
      if (!rows.length) return {n: 0};
      const info = rows.map(r => {
        const b = r.getBoundingClientRect();
        const set = [...r.querySelectorAll('button')].find(x => x.textContent === 'Set');
        const g = r.querySelector('.grow');
        return {name: r.querySelector('b')?.textContent || '', h: Math.round(b.height),
          nameBlockH: Math.round(g.getBoundingClientRect().height),
          // ONE line, checked as containment rather than as a pixel tolerance: a WRAP puts a control outside the row's
          // line band entirely, which is what "everything shares the row's own Y range" catches.
          rowMids: [...r.querySelectorAll('.usec, .setc')].filter(c => c.getClientRects().length)
            .map(c => { const q = c.getBoundingClientRect(); return [Math.round(q.top), Math.round(q.bottom)]; }),
          // the suggestion's (i) sits on the name's line, next to it (the owner, 2026-10-21), not with Use / Set
          infoOnName: (() => { const i = r.querySelector('.nbsg'), n = r.querySelector('.grow > b'); if (!i) return true;
            const a = i.getBoundingClientRect(), c = n.getBoundingClientRect();
            return Math.abs((a.top + a.bottom) - (c.top + c.bottom)) / 2 <= 3 && a.left >= c.right && a.left - c.right <= 12; })(),
          setText: set?.textContent || null,
          overflow: r.scrollWidth > r.clientWidth + 1,
          hasInlineSuggestion: !!r.querySelector('.sg')};
      });
      return {n: info.length, info};
    })()""")
    assert res["n"] >= 2, res
    heights = {r["h"] for r in res["info"]}
    assert len(heights) == 1, (width, res["info"], "these rows must all be the same height")
    nameBlocks = {r["nameBlockH"] for r in res["info"]}
    assert len(nameBlocks) == 1, (width, res["info"], "the name+reason block must be the same height on every row")
    for r in res["info"]:
        assert r["rowMids"], r
        lo = min(pair[0] for pair in r["rowMids"])
        hi = max(pair[1] for pair in r["rowMids"])
        assert hi - lo <= 36, (width, r, "the controls are spread over more than one line")
        assert r["infoOnName"], (width, r, "the suggestion's (i) sits right after the category name, on its line")
        assert r["setText"] == "Set", (width, r, "the button is named `Set`")
        assert not r["hasInlineSuggestion"], (width, r, "the suggestion belongs in the panel, not on the row")
        assert not r["overflow"], (width, r)
    clean(b)

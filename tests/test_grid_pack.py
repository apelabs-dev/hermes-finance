"""Masonry packing of the dashboard card grids: app.js wiring + CSS, and the placement rules of packLayout()
(run in node when it is installed)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_finance import web

STATIC = Path(web.__file__).parent / "static"
JS = (STATIC / "app.js").read_text()
CSS = (STATIC / "app.css").read_text()
U, G, HOLE, CHART = 4, 16, 24, 40


def js_function(name: str) -> str:
    m = re.search(rf"\n  (?:async )?function {name}\(.*?\n  }}\n", JS, re.S)
    assert m, name
    return m.group(0)


def test_grid_js_is_present_and_wired():
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert forbidden not in JS, forbidden
    # one ResizeObserver on the cards and one MutationObserver on the grids (any change inside a card), batched in
    # requestAnimationFrame; the packer's own writes (order, .pk / .fill) don't count as content changes
    assert JS.count("new ResizeObserver(") == 1 and JS.count("new MutationObserver(") == 1
    assert "PK.frame = requestAnimationFrame(packAll)" in JS and "PK.ro.observe(el)" in JS
    assert "PK.mo.observe(grid, WATCH)" in JS and "attributeFilter: ['class', 'hidden', 'open']" in JS
    assert "!PK.grids.has(el) && !PK.grids.has(el.parentElement)" in js_function("contentChanged")
    assert "PACK = {unit: 4, gutter: 16, hole: 24, chartStretch: 40" in JS
    # which cards stretch only 40px is decided by packsLikeChart() (behaviour: test_which_cards_pack_like_a_chart_card)
    assert "chart: packsLikeChart(el)" in js_function("packAll")
    assert ".card.grow > .fillh, .card.grow .fillh-wrap > .fillh { flex: 1 0 0; height: auto; }" in CSS
    for fn, cls in (("spentCard", "'s2 grow'"), ("flowCard", "'s2 grow'")):
        assert cls in js_function(fn) and "fillh'" in js_function(fn), fn
    # every multi-column card grid is packed after it is rendered (and after its charts exist)
    # (P2A: Budgets is no longer a card grid, it is the summary, one grouped table and the No-budget card, so drawBudgets
    # packs nothing: the table is not a masonry item)
    for fn in ("renderOverview", "renderRecurring", "renderAccounts", "renderInvestments", "renderDebt"):
        body = js_function(fn)
        assert "pack(grid);" in body, fn
        if "later.forEach" in body:
            assert body.index("later.forEach") < body.index("pack(grid);"), fn
    ov = js_function("renderOverview")
    assert "spent.dataset.pin = 'start'" in ov and "stack.dataset.pin = 'end'" in ov
    # the three tiles are one full-width row; phones and wider screens read the 005 order, Recent transactions last
    assert "h('div', {class: 'tiles s3'}, worthTile(d), investTile(d), debtTile(d), HOOKS.tiles(d))" in ov  # TASK-19: + Goals
    assert "h('div', {class: 'grid'}, spent, stack, tiles, budgets, where, bills, flow, upcoming, recent)" in ov
    assert "[spent, stack, tiles, budgets, where, bills, flow, upcoming, recent].forEach" in ov
    drawn = js_function("drawBudgets")
    assert "pack(" not in drawn and "HOOKS.budgetsBody(d)" in drawn      # P2A: the body is budgets.js's table, not a packed grid
    # CSS: 4px rows without row gaps on packed grids; the column count comes from --cols (1 on phones)
    assert ".grid.pk { grid-auto-rows: 4px; row-gap: 0; }" in CSS
    # TASK-10: a stretched card is at least its area's height, never pinned to it (pinned + overflow: hidden cut
    # off an opened budget editor)
    assert ".grid.pk > .fill { align-self: start; min-height: 100%; }" in CSS and "align-self: stretch" not in CSS
    assert re.search(r"\.grid \{[^}]*--cols: 3;", CSS) and re.search(r"\.grid\.two \{[^}]*--cols: 2;", CSS)
    phone = CSS[CSS.index("@media (max-width: 1023.98px)"):]   # the approved phone layout (MOCK-017): one column below 1024px
    assert ".grid, .grid.two { grid-template-columns: minmax(0, 1fr); gap: 14px; --cols: 1; }" in phone


# --- packLayout() in node ----------------------------------------------------------------------------

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def one(h, k=1, chart=False, pin=None):
    return {"spans": [{"k": k, "h": h}], "chart": chart, "pin": pin}


def flex(*heights):
    return {"spans": [{"k": i + 1, "h": h} for i, h in enumerate(heights)], "chart": False, "pin": None}


def run_pack(cases):
    code = re.search(r"\n  const PACK = \{.*?\};\n", JS, re.S).group(0) + js_function("packLayout")
    prog = code + f"\nconst cases = {json.dumps(cases)};\n" \
        "console.log(JSON.stringify(cases.map((c) => packLayout(c.items, c.cols))));"
    out = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


def below(layout, p):
    """Tops of the cards under `p` in its columns."""
    return [q["top"] for q in layout if q is not p and q["top"] > p["top"] and q["c"] < p["c"] + p["k"] and p["c"] < q["c"] + q["k"]]


def check(items, cols, layout):
    """The rules every packed layout keeps; returns the largest gap between cards (px)."""
    assert len(layout) == len(items)
    spans = []
    for it, p in zip(items, layout):
        s = next(s for s in it["spans"] if min(s["k"], cols) == p["k"])
        visual = p["rows"] * U - G
        assert visual >= s["h"] - 0.01                      # never shorter than its content
        under = below(layout, p)
        if under:
            assert p["top"] + p["rows"] == min(under)         # TASK-11: down to the card below: the gap is the gutter
        elif it["chart"]:
            assert visual - s["h"] <= CHART                   # at the bottom edge a chart card stretches 40px at most
        if it["pin"] == "start":
            assert p["c"] == 0
        if it["pin"] == "end":
            assert p["c"] + p["k"] == cols
        spans.append((p["top"], p["top"] + p["rows"], range(p["c"], p["c"] + p["k"])))
    # reading order: never above an earlier card, same line -> left to right
    floor = 0
    for a, b in zip(layout, layout[1:]):
        floor = max(floor, a["top"])
        assert b["top"] * U >= floor * U - HOLE
        if abs(b["top"] - a["top"]) * U <= HOLE:
            assert b["c"] > a["c"]
    end = max(e for _, e, _ in spans)
    worst = 0
    for col in range(cols):
        stack = sorted((t, e) for t, e, cs in spans if col in cs)
        assert stack, f"column {col} is empty"
        worst = max(worst, stack[0][0] * U)
        for (t1, e1), (t2, e2) in zip(stack, stack[1:]):
            assert t2 >= e1                                   # no overlaps
            worst = max(worst, (t2 - e1) * U)
        worst = max(worst, (end - stack[-1][1]) * U)
    return worst


def overview(spent, stack, budgets, coming, bills, where, flow, recent=367):
    return [one(spent, 2, True, "start"), one(stack, pin="end"), one(budgets), one(coming), one(bills),
            one(where, chart=True), one(flow, 2, True), one(recent, 3)]


OVERVIEWS = {  # card heights measured at 1440px (demo DB, 13 bills, 0 caps + 1 bill, and the live numbers)
    "demo": overview(419, 416, 346, 409, 367, 450, 364),
    "many bills": overview(419, 416, 346, 409, 778, 479, 364),
    "few bills, no caps": overview(419, 370, 225, 253, 132, 479, 364),
    "live": overview(419, 416, 225, 340, 367, 479, 360),
    "1024": overview(419, 508, 367, 409, 384, 468, 381),
}


@needs_node
def test_overview_packs_without_holes_and_keeps_the_order():
    layouts = run_pack([{"items": it, "cols": 3} for it in OVERVIEWS.values()])
    for (name, items), layout in zip(OVERVIEWS.items(), layouts):
        assert check(items, 3, layout) <= HOLE, name
        spent, stack, recent = layout[0], layout[1], layout[-1]
        assert (spent["c"], spent["k"], spent["top"]) == (0, 2, 0), name        # top-left
        assert (stack["c"], stack["top"]) == (2, 0), name                       # top-right
        assert recent["k"] == 3 and recent["top"] == max(p["top"] + p["rows"] for p in layout[:-1]), name
    # the live heights: Budgets | Coming up | Bills on one line, then Where it went and Income vs spending
    live = layouts[list(OVERVIEWS).index("live")]
    assert len({p["top"] for p in live[2:5]}) == 1 and [p["c"] for p in live[2:5]] == [0, 1, 2]


def overview_005(spent, stack, tiles, budgets, where, bills, flow, coming, recent):
    """The 005 overview: the three tiles are one full-width item under Spent / Left to spend + On pace. Spent and
    Income vs spending are .grow cards (their chart takes extra height), so they may stretch; the doughnut may not."""
    return [one(spent, 2, pin="start"), one(stack, pin="end"), one(tiles, 3), one(budgets), one(where, chart=True),
            one(bills), one(flow, 2), one(coming), one(recent, 3)]


OVERVIEWS_005 = {  # natural heights measured at 1440px on the demo DB (shots/008/report.json), and variations
    "demo": overview_005(479, 480, 214, 514, 634, 460, 460, 541, 441),
    "tall stack": overview_005(479, 510, 214, 514, 634, 460, 460, 541, 441),
    "no caps, few bills": overview_005(479, 420, 214, 225, 634, 180, 460, 300, 441),
}


@needs_node
def test_005_overview_keeps_the_tiles_row_and_the_mockup_order():
    layouts = run_pack([{"items": it, "cols": 3} for it in OVERVIEWS_005.values()])
    for (name, items), layout in zip(OVERVIEWS_005.items(), layouts):
        assert check(items, 3, layout) <= HOLE, name
        spent, stack, tiles, budgets, where, bills, flow, coming, recent = layout
        assert (spent["c"], spent["top"], stack["c"], stack["top"]) == (0, 0, 2, 0), name
        assert (tiles["c"], tiles["k"]) == (0, 3), name                              # one row, full width
        assert tiles["top"] == max(spent["top"] + spent["rows"], stack["top"] + stack["rows"]), name
        assert [p["c"] for p in (budgets, where, bills)] == [0, 1, 2] and len({budgets["top"], where["top"], bills["top"]}) == 1
        assert (flow["c"], flow["k"], coming["c"]) == (0, 2, 2), name               # Income vs spending + Coming up
        assert recent["k"] == 3 and recent["top"] == max(p["top"] + p["rows"] for p in layout[:-1]), name


@needs_node
def test_budgets_not_budgeted_fills_the_last_row():
    cards = [one(326, chart=True) for _ in range(7)]
    uneven = [one(h, chart=True) for h in (388, 360, 423, 423, 423, 423, 396)]
    cases = [(cards + [flex(400, 308, 300)], 3), (cards[:6] + [flex(400, 308, 300)], 3),
             (cards + [one(326, chart=True), flex(340, 308, 300)], 3), (uneven + [flex(420, 344, 308)], 3),
             ([flex(400, 308, 300)], 3)]
    layouts = run_pack([{"items": it, "cols": c} for it, c in cases])
    for (items, cols), layout in zip(cases, layouts):
        assert check(items, cols, layout) <= HOLE
    seven, six, eight, _, alone = layouts
    assert (seven[-1]["c"], seven[-1]["k"]) == (1, 2)       # 3 + 3 + 1 cards: the last two columns
    assert (six[-1]["c"], six[-1]["k"]) == (0, 3)           # full rows: full width underneath
    assert (eight[-1]["c"], eight[-1]["k"]) == (2, 1)       # 3 + 3 + 2 cards: the last column
    assert (alone[0]["c"], alone[0]["k"]) == (0, 3)


@needs_node
def test_two_columns_line_up_and_charts_barely_stretch():
    lists = [one(403), one(576)]                             # Recurring: Bills + Subscriptions
    charts = [one(200, chart=True), one(500, chart=True)]    # nothing may absorb this difference
    layouts = run_pack([{"items": lists, "cols": 2}, {"items": charts, "cols": 2}])
    assert check(lists, 2, layouts[0]) == 0
    assert layouts[0][0]["rows"] == layouts[0][1]["rows"]    # the shorter list card stretches to match
    assert layouts[1][0]["rows"] * U - G - 200 <= CHART      # the short chart card is not blown up to 500


def run_packs_like_chart(cards):
    """packsLikeChart() from app.js, in node, on stub cards {'has': [selectors it contains], 'classes': [...]}."""
    prog = js_function("packsLikeChart") + f"""
const cards = {json.dumps(cards)};
console.log(JSON.stringify(cards.map((c) => packsLikeChart({{
  querySelector: (sel) => (sel.split(',').some((s) => c.has.includes(s.trim())) ? {{}} : null),
  classList: {{contains: (x) => c.classes.includes(x)}},
}}))));"""
    return json.loads(subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=60, check=True).stdout)


@needs_node
def test_which_cards_pack_like_a_chart_card():
    """A chart card (its canvas doesn't grow) and, since TASK-12b, a card showing an empty chart area stretch only 40px;
    a .grow card's chart and a list card may stretch as far as the layout needs. Then packLayout() with the heights
    measured at 1440 on the live Investments page (gate DB history-empty: Total investments 361px with "History builds
    from today", Allocation 487px beside it): the empty card stays at most 40px over its content instead of carrying
    a 127px taller blank box (the empty chart area was 307px, the brief asks for about 180-220)."""
    kinds = {"chart card": {"has": ["canvas"], "classes": ["card"]},
             "grow card with its chart": {"has": ["canvas"], "classes": ["card", "grow"]},
             "grow card, empty chart area": {"has": ["[data-empty-chart]"], "classes": ["card", "grow"]},
             "card, empty chart stand-in": {"has": ["[data-empty-chart]"], "classes": ["card"]},
             "list card": {"has": [], "classes": ["card"]}}
    got = dict(zip(kinds, run_packs_like_chart(list(kinds.values()))))
    assert got == {"chart card": True, "grow card with its chart": False, "grow card, empty chart area": True,
                   "card, empty chart stand-in": True, "list card": False}
    total, alloc = 361, 487
    cases = [[one(total, 2, got["grow card, empty chart area"]), one(alloc)],     # history-empty: no chart
             [one(total, 2, got["grow card with its chart"]), one(alloc)]]       # the same card with its chart
    empty, charted = run_pack([{"items": it, "cols": 3} for it in cases])
    check(cases[0], 3, empty)                                           # every rule holds (incl. the 40px chart cap); its
    assert check(cases[1], 3, charted) == 0                             # bottom edge is ragged on purpose, this one isn't
    assert empty[0]["rows"] * U - G - total <= CHART                    # capped: the card keeps (about) its own height
    assert empty[0]["rows"] < empty[1]["rows"]                          # so it ends above the taller Allocation card
    assert charted[0]["rows"] == charted[1]["rows"]                     # a real chart fills the extra height instead


@needs_node
def test_many_cards_stay_fast_and_valid():
    banks = [665, 194, 316, 148, 562, 230, 410, 180, 290, 150]
    items = [one(446, 2, True, "start"), one(305, pin="end")] + [one(h) for h in banks]
    layout = run_pack([{"items": items, "cols": 3}])[0]
    assert check(items, 3, layout) <= HOLE


# --- chart axis labels (TASK-10 sweep) -----------------------------------------------------------------

@needs_node
def test_axis_labels_keep_the_zeros_of_whole_thousands():
    """axisMoney() trimmed trailing zeros from whole numbers too, so a $40k tick read "$4k" and $100k read "$1k"
    (the net worth and portfolio charts were labelled 10x too small)."""
    # TASK-18: axisMoney writes the currency through withSym / decs (Settings > Region); here USD in en-US
    prog = ("const LOC = 'en-US'; const CUR = {digits: 2, sym: '$', before: true, gap: '', dec: '.'};"
            "const NF0 = new Intl.NumberFormat(LOC, {maximumFractionDigits: 0});"
            "const NF2 = new Intl.NumberFormat(LOC, {minimumFractionDigits: 2, maximumFractionDigits: 2});"
            "const withSym = (s) => `${CUR.sym}${s}`; const decs = (t) => t;") + js_function("moneyParts") + js_function("money") + js_function("axisMoney") + """
const t = (step) => [{value: 0}, {value: step}];
console.log(JSON.stringify([
  [10000, t(10000)], [20000, t(10000)], [40000, t(10000)], [100000, t(20000)], [50000, t(10000)],
  [1500, t(500)], [1250, t(250)], [2000, t(1000)], [800, t(200)], [0.5, t(0.25)], [-30000, t(10000)],
  [1200, t(100)], [12000, t(2000)],
].map(([v, ticks]) => axisMoney(v, 0, ticks))));"""
    out = json.loads(subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=60, check=True).stdout)
    assert out == ["$10k", "$20k", "$40k", "$100k", "$50k", "$1.5k", "$1.3k", "$2k", "$800", "$0.50", "-$30k",
                   "$1.2k", "$12k"]

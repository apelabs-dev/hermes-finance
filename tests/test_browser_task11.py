"""TASK-11 browser checks in headless Chrome (tests/cdp.py; https and plaid.com blocked), on throwaway loopback servers
over synthetic DBs: the demo DB (as test_browser.py) and the gates' `full` DB (tests/gate_dbs.py)."""

from __future__ import annotations

import math
import sqlite3
import time
from datetime import date

import pytest

import demo_data
import gate_dbs
from cdp import Chrome, Server, find_chrome
from hermes_finance import db, manual

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
WIDTHS = {"desktop": (1440, 900), "phone": (390, 844)}
AFTER_CHANGE = "await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 80))));"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """The demo DB: one Plaid brokerage (so "By account type" has a single non-zero slice) and a manual card."""
    path = tmp_path_factory.mktemp("browser11") / "finance.db"
    conn = db.connect(path)
    today = date(2026, 9, 26)  # includes rent and Shopping; independent of the wall-clock month boundary
    demo_data.build(conn, today=today)
    demo_data.extras(conn, today=today)
    manual.create(conn, {"kind": "credit_card", "name": "Sapphire", "institution": "Chase", "credit_limit": 6000.0,
                         "balance": 420.0}, today)
    conn.close()
    srv = Server(path, today=today)
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def full(tmp_path_factory):
    """The gates' `full` DB: a $0 Plaid "Automated Investing" account and a manual 401(k) as in the real Allocation."""
    path = gate_dbs.build("full", tmp_path_factory.mktemp("full11") / "finance.db")
    srv = Server(path)
    srv.db_path = path
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def browser():
    with Chrome() as b:
        b.call('Page.addScriptToEvaluateOnNewDocument', source="""
          { const NativeDate = Date, now = new NativeDate(2026, 8, 26, 12).getTime();
            window.Date = class extends NativeDate {
              constructor(...args) { super(...(args.length ? args : [now])); }
              static now() { return now; }
            };
          }
        """)
        yield b


def open_page(browser, srv, page, mode):
    browser.size(*WIDTHS[mode])
    browser.goto(f"{srv.url}/?t={mode}{time.time():.3f}#/{page}")  # a new URL: always a real load
    browser.problems()


def api(browser, path):
    return browser.js(f"fetch({path!r}).then((r) => r.json())")


def card_by_title(title: str) -> str:
    return (f"[...document.querySelectorAll('#page .card')].find((c) => (c.querySelector('h3 .ttl') || {{}}).textContent "
            f"=== {title!r})")


# --- (a) the Allocation legend must never be clipped, whichever grouping is shown ------------------------------

ALLOCATION = """(async () => {
  const card = CARD;
  const out = [];
  for (const k of ['type', 'holding', 'type', 'holding']) {
    card.querySelector(`.seg button[data-k="${k}"]`).click();
    AFTER_CHANGE
    const rows = card.querySelectorAll('.lgr');
    const last = rows[rows.length - 1].getBoundingClientRect();
    const c = card.getBoundingClientRect();
    out.push({k, rows: rows.length, sh: card.scrollHeight, ch: card.clientHeight, last: Math.round(last.bottom),
      bottom: Math.round(c.bottom), packed: card.parentElement.classList.contains('pk')});
  }
  return out;
})()""".replace("CARD", card_by_title("Allocation")).replace("AFTER_CHANGE", AFTER_CHANGE)


@pytest.mark.parametrize("mode", WIDTHS)
def test_allocation_legend_fits_in_both_groupings(browser, full, mode):
    """Toggling "By holding" / "By account type" rewrites the legend (4 rows by type: Brokerage, Robo at $0, Retirement,
    Crypto; 4 by holding: the 401(k), NVDA, Cash, Unpriced / pending); the card follows at 1440 (packed) and 390, so no
    row is cut off at the bottom edge."""
    open_page(browser, full, "investments", mode)
    rows = browser.js(ALLOCATION)
    assert [r["rows"] for r in rows] == [4, 4, 4, 4]
    if mode == "desktop":
        assert all(r["packed"] for r in rows)
    clipped = [r for r in rows if r["sh"] > r["ch"] or r["last"] > r["bottom"]]
    assert not clipped, clipped
    assert browser.problems() == []


# --- TASK-11b (1): Plaid's balance (491.62) and its holdings (491.47) disagree: the $0.15 is its own quiet row -----

RESIDUAL = """(() => {
  const card = CARD;
  const rows = [...card.querySelectorAll('.lgr')].map((r) => ({label: r.querySelector('.grow > b').textContent,
    amount: r.querySelector('.amt').textContent, pct: r.querySelector('.rt > span:last-child').textContent,
    sw: getComputedStyle(r.querySelector('.sw')).backgroundColor, ink: getComputedStyle(r.querySelector('.grow > b')).color}));
  const ch = Chart.getChart(card.querySelector('canvas'));
  const ds = ch.data.datasets[0];
  const top = TOTAL;
  return {rows, labels: ch.data.labels, data: ds.data, colors: ds.backgroundColor, total: top.querySelector('.big').textContent};
})()""".replace("CARD", card_by_title("Allocation")).replace("TOTAL", card_by_title("Total investments"))
TILE = """(() => {
  const tile = CARD;
  return {segs: [...tile.querySelectorAll('.stk i')].map((i) => [getComputedStyle(i).backgroundColor, i.style.width]),
    line: tile.querySelector('.tl2').textContent, big: tile.querySelector('.big').textContent};
})()""".replace("CARD", card_by_title("Investments"))


def dollars(text: str) -> float:
    return (-1 if text.startswith("-") else 1) * float(text.lstrip("-$").replace(",", ""))


def by_holding(browser, srv, mode):
    open_page(browser, srv, "investments", mode)
    browser.js("document.querySelector('#page .seg button[data-k=holding]').click()")
    browser.settle()
    return browser.js(RESIDUAL)


def set_balance(srv, value):
    conn = sqlite3.connect(srv.db_path)
    with conn:
        conn.execute("UPDATE accounts SET current_balance = ? WHERE account_id = 'acc_nova_inv'", (value,))
    conn.close()


@pytest.mark.parametrize("mode", WIDTHS)
def test_the_residual_is_a_quiet_grey_row_below_cash(browser, full, mode):
    d = by_holding(browser, full, mode)
    assert [r["label"] for r in d["rows"]][-2:] == ["Cash", "Unpriced / pending"]
    last = d["rows"][-1]
    assert (last["amount"], last["pct"]) == ("$0.15", "0.0%")
    assert last["sw"] == "rgb(100, 107, 128)" and last["ink"] == "rgb(182, 188, 203)"      # quiet grey, muted label
    assert (d["labels"][-1], d["data"][-1], d["colors"][-1]) == ("Unpriced / pending", 0.15, "#646b80")
    total = dollars(d["total"])
    assert total == 18741.62 and round(sum(dollars(r["amount"]) for r in d["rows"]), 2) == total
    assert round(sum(d["data"]), 2) == total                                              # the ring = the total
    open_page(browser, full, "overview", mode)
    t = browser.js(TILE)
    assert dollars(t["big"]) == total and t["segs"][-1][0] == "rgb(100, 107, 128)"          # its grey sliver on the tile
    assert "Unpriced" not in t["line"] and t["line"].startswith("Work 401(k)")              # never the "top" holding
    assert browser.problems() == []


def test_a_negative_residual_is_listed_but_not_drawn(browser, full):
    set_balance(full, 491.40)                                   # the holdings (491.47) are worth more than the balance
    try:
        d = by_holding(browser, full, "desktop")
    finally:
        set_balance(full, 491.62)
    last = d["rows"][-1]
    # TASK-22f: its share shows, signed (it was left blank, so the numbers gate read 0.0% for the live DB's −0.7%);
    # -$0.07 of $18,741.40 rounds to 0.0% (never "−0.0%")
    assert (last["label"], last["amount"], last["pct"]) == ("Unpriced / pending", "-$0.07", "0.0%")
    assert "Unpriced / pending" not in d["labels"] and all(v > 0 for v in d["data"])
    total = dollars(d["total"])
    assert total == 18741.40 and round(sum(dollars(r["amount"]) for r in d["rows"]), 2) == total
    assert browser.problems() == []


# --- (b) a doughnut with one non-zero slice is a full ring: no separator notch at 12 o'clock --------------------

DOUGHNUTS = """(() => {
  const out = [];
  for (const canvas of document.querySelectorAll('#page canvas')) {
    const ch = Chart.getChart(canvas);
    if (!ch || ch.config.type !== 'doughnut') continue;
    const ds = ch.data.datasets[0];
    const arcs = ch.getDatasetMeta(0).data;
    const arc = arcs.find((a) => a.circumference > 0);
    const r = (arc.innerRadius + arc.outerRadius) / 2;
    const dpr = ch.currentDevicePixelRatio;
    const ctx = canvas.getContext('2d');
    const top = [-1, 0, 1].map((dx) => [...ctx.getImageData(Math.round((arc.x + dx) * dpr), Math.round((arc.y - r) * dpr), 1, 1).data]);
    out.push({n: ds.data.filter((v) => v > 0).length, borderWidth: ds.borderWidth, borderRadius: ds.borderRadius, top,
      first: ds.backgroundColor[ds.data.findIndex((v) => v > 0)]});
  }
  return out;
})()"""


def rgb(hex_: str) -> tuple[int, int, int]:
    return tuple(int(hex_[i:i + 2], 16) for i in (1, 3, 5))


@pytest.mark.parametrize("mode", WIDTHS)
def test_a_single_segment_doughnut_is_a_full_ring(browser, site, mode):
    open_page(browser, site, "investments", mode)
    browser.js("document.querySelector('#page .seg button[data-k=type]').click()")
    browser.settle()
    (one,) = browser.js(DOUGHNUTS)
    assert one["n"] == 1 and (one["borderWidth"], one["borderRadius"]) == (0, 0), one
    want = rgb(one["first"])
    for px in one["top"]:  # the ring's own colour at the top, not the card showing through a separator
        assert px[3] == 255 and all(abs(a - b) <= 24 for a, b in zip(px[:3], want)), (px, want)
    browser.js("document.querySelector('#page .seg button[data-k=holding]').click()")
    browser.settle()
    (two,) = browser.js(DOUGHNUTS)
    assert two["n"] == 2 and (two["borderWidth"], two["borderRadius"]) == (3, 4)   # 2+ slices keep the separators
    assert browser.problems() == []


# --- (c, d) "Left to spend" is the net: budgeted - spent - bills still due in capped categories ------------------

LEFT = """(() => {
  const card = CARD;
  const big = card.querySelector('.big');
  return {big: big.textContent, bad: big.classList.contains('bad'), subs: [...card.querySelectorAll('.sub')].map((s) => s.textContent),
    ring: card.querySelector('.ring b').textContent};
})()""".replace("CARD", card_by_title("Left to spend"))


def money2(v: float) -> str:
    return f"{'-' if v < 0 else ''}${abs(v):,.0f}" if float(v).is_integer() else f"{'-' if v < 0 else ''}${abs(v):,.2f}"


def money0(v: float) -> str:
    return f"{'-' if round(v) < 0 else ''}${abs(v):,.0f}"


def check_left_tile(browser, b):
    t = browser.js(LEFT)
    assert t["big"] == money2(b["left"]) and t["bad"] == (b["left"] < 0), (t, b["left"])
    # TASK-14b: (spent + bills still due) / budgeted, rounded half up as the page's Math.round
    assert t["ring"] == f"{math.floor((b['total_spend'] + b['total_due']) / b['total_cap'] * 100 + 0.5)}%"
    if b["left"] < 0:
        assert t["subs"][0].startswith(f"Over budget by {money0(-b['left'])}"), t
    else:
        days = f"{b['days_left']} day" + ("" if b["days_left"] == 1 else "s")   # the page's plural(): "1 day left" on the 29th of a 30-day month
        assert t["subs"][0] == f"About {money0(b['per_day'])} a day for the {days} left", t
    if b["total_due"]:
        assert f"{money0(b['total_due'])} in bills still due" in t["subs"], t
        browser.js("document.querySelector('[data-help=left-bills]').click()")
        bills = browser.js("document.querySelector('.help-panel').textContent")
        assert all(x['merchant'] in bills for x in b["bills_due"]), (bills, b['bills_due'])
    browser.js("document.querySelector('[data-help=left-split]').click()")
    split_text = browser.js("document.querySelector('.help-panel').textContent")
    split = f"{money0(b['under'])} left in {b['under_count']} budget{'s' if b['under_count'] != 1 else ''}"
    assert split in split_text, (t, split_text)
    if b["over"]:
        assert f"{money0(b['over_amount'])} over in {b['over']}" in split_text, split_text
    browser.js("document.querySelector('[data-help=left-split]').click()")
    return t


def test_left_to_spend_is_the_net_on_overview_and_budgets(browser, site):
    open_page(browser, site, "overview", "desktop")
    b = api(browser, "/api/overview")["budgets"]
    assert b["left"] == round(b["total_cap"] - b["total_spend"] - b["total_due"], 2)
    check_left_tile(browser, b)
    # overspend a cap on purpose: the net goes negative and turns red (and the Budgets page agrees)
    browser.js("fetch('/api/budgets/cap', {method: 'POST', headers: {'Content-Type': 'application/json'}, "
               "body: JSON.stringify({category: 'Other Shopping', monthly_cap: 1})})")
    try:
        open_page(browser, site, "overview", "desktop")
        over = api(browser, "/api/overview")["budgets"]
        assert over["left"] < 0 and over["per_day"] == 0.0
        check_left_tile(browser, over)
        open_page(browser, site, "budgets", "desktop")
        summary = browser.js("document.querySelector('#page .bsum').textContent")
        assert f"{money0(-over['left'])} over budget" in summary and f"{money0(over['over_amount'])} over in" in summary
        bp = api(browser, "/api/budgets")
        # 2026-10-02 (option 3a): the amount of bills still due is not a chip in the row, it is disclosed in the
        # Details panel and published as `data-due`, so this asserts the value on every row that has bills due.
        # P2A: a category is a table row (`tr.c`) in the stable taxonomy order, no longer a card ordered fullest ring
        # down, so rows are matched to the API by NAME rather than by position.
        #
        # Read the DOM and the API back to back, with nothing in between: the page re-renders asynchronously after the
        # cap POST above, and an earlier version polled once, then read the API, then read the DOM — landing on two
        # different moments, which surfaced as a mystery float mismatch rather than "the page moved under me".
        browser.js("new Promise((r) => setTimeout(r, 400))")
        dom, bp = browser.js("""(() => [...document.querySelectorAll('#page tr.c')]
          .map((c) => ({name: c.dataset.category, due: c.dataset.due})))()"""), \
            api(browser, "/api/budgets")
        assert dom, "the budgets page rendered no rows to read"
        assert all(x["due"] is not None for x in dom), (dom, "every budget row publishes data-due")
        shown = sorted((x["name"], float(x["due"])) for x in dom if float(x["due"]) > 0)
        want = sorted((c["category"], c["due"]) for c in bp["categories"] if c["cap"] and c["due"])
        # name the rows on both sides, so a mismatch says WHICH category drifted instead of printing two float lists
        assert shown == want, ({"dom": shown, "api": want, "domAll": [(x["name"], x["due"]) for x in dom]},
                               "data-due disagrees with the API")
        # and no row holds more than that ONE status pill (the Left pill)
        counts = browser.js("[...document.querySelectorAll('#page tr.c')].map((r) => r.querySelectorAll('.rp').length)")
        assert counts and max(counts) <= 1, counts
    finally:
        browser.js("fetch('/api/budgets/cap', {method: 'POST', headers: {'Content-Type': 'application/json'}, "
                   "body: JSON.stringify({category: 'Other Shopping', monthly_cap: 850})})")
    assert browser.problems() == []


# --- (f) hide categories from the "Where it went" chart ---------------------------------------------------------

WHERE = """(() => {
  const card = CARD;
  const canvas = card.querySelector('canvas');
  const ch = canvas && Chart.getChart(canvas);
  const rows = [...card.querySelectorAll('.crow')].map((r) => {
    const t = r.querySelector('.ctog');
    t.scrollIntoView({block: 'center'});                // elementFromPoint only sees what's on screen
    const q = t.getBoundingClientRect();
    const cy = q.top + q.height / 2;
    const hit = [[q.left - 6, cy], [q.right + 6, cy], [q.left + q.width / 2, q.top - 6], [q.left + q.width / 2, q.bottom + 6]]
      .every(([x, y]) => { const e = document.elementFromPoint(x, y); return e === t || t.contains(e); });
    return {name: r.querySelector('.grow > b').textContent, sub: r.querySelector('.grow > span').textContent,
      amount: r.querySelector('.amt').textContent, off: r.classList.contains('off'), pressed: t.getAttribute('aria-pressed'),
      label: t.getAttribute('aria-label'), tag: t.tagName, link: r.querySelector('a.crowl, a.wwpayments').getAttribute('href'),
      opacity: getComputedStyle(r.querySelector('.ctog .cb')).opacity, strike: getComputedStyle(r.querySelector('.amt')).textDecorationLine,
      hit, inc: (r.querySelector('.inc') || {}).textContent || null};
  });
  return {rows, extra: card.querySelector('h3 .hx').textContent, label: canvas ? canvas.getAttribute('aria-label') : null,
    slices: ch ? ch.data.datasets[0].data.length : 0, border: ch ? ch.data.datasets[0].borderWidth : null,
    all_hidden: (card.querySelector('.allhid') || {}).textContent || null,
    hid: (card.querySelector('.wfoot .whid') || {}).textContent || null,
    head_lines: (() => { const h3 = card.querySelector(':scope > h3');   // 1 = the header fits its one-line strip
      return h3.getBoundingClientRect().height <= (parseFloat(getComputedStyle(h3).minHeight) || 0) + 2 ? 1 : 2; })(),
    preset: (card.querySelector('[data-preset]') || {}).textContent || null};
})()""".replace("CARD", card_by_title("Where it went"))
CLICK_TOGGLE = """(async () => { const b = [...document.querySelectorAll('#page .ctog')].find((x) => x.dataset.key === KEY);
  b.click(); await new Promise((r) => setTimeout(r, 400)); return document.activeElement.dataset.key; })()"""


def where(browser):
    browser.settle()
    return browser.js(WHERE)


def click_text(browser, text):
    browser.js(f"[...document.querySelectorAll('#page button')].find((b) => b.textContent === {text!r}).click()")
    browser.js("new Promise((r) => setTimeout(r, 400))")


@pytest.mark.parametrize("mode", WIDTHS)
def test_where_it_went_hides_groups(browser, site, mode):
    open_page(browser, site, "overview", mode)
    ov = api(browser, "/api/overview")
    groups = [g for g in ov["spend_groups"] if g["count"]]
    total = sum(g["spend"] for g in groups)
    w = where(browser)
    assert len(w["rows"]) == len(groups)
    assert all(r["tag"] == "BUTTON" and r["pressed"] == "false" and r["hit"] for r in w["rows"])
    assert all(r["link"].startswith("#/transactions") for r in w["rows"]), w["rows"]
    assert w["extra"] == "this month" and w["label"].endswith(f"{money0(total)} spent so far"), w
    n_all = w["slices"]
    home = next(g for g in groups if g["group"] == "Home")
    assert browser.js(CLICK_TOGGLE.replace("KEY", repr("Home"))) == "Home"        # focus stays on the toggle
    w = where(browser)
    # (reviewer, restoring task11's checks for groups) the hidden row stays, dimmed and struck through; the ring, the
    # label and every % cover only what's shown; the header keeps one line, the count + reset sit in the footer
    row = next(r for r in w["rows"] if r["name"] == "Home")
    assert row["off"] and row["pressed"] == "true" and row["sub"].startswith("hidden · "), row
    assert float(row["opacity"]) == pytest.approx(0.45) and row["strike"] == "line-through", row
    assert w["slices"] == len(groups) - 1 == n_all - 1
    assert w["extra"] == "this month" and w["hid"] == "1 hidden · Show all" and w["head_lines"] == 1, w
    shown = total - home["spend"]
    assert w["label"].endswith(f"{money0(shown)} shown of {money0(total)} spent"), w["label"]
    on = [r for r in w["rows"] if not r["off"]]
    pcts = [int(r["sub"].split("%")[0]) for r in on]
    amounts = [float(r["amount"].replace("$", "").replace(",", "")) for r in on]
    assert abs(sum(amounts) - shown) <= len(amounts) * 0.5                            # the shown rows make up the middle
    assert all(abs(p - a / shown * 100) <= 0.5 + 100 * 0.5 / shown for p, a in zip(pcts, amounts)), (pcts, amounts)
    assert set(api(browser, "/api/settings")["chart_excluded_categories"]) == {c["category"] for c in home["categories"]}
    open_page(browser, site, "overview", mode)
    assert next(r for r in where(browser)["rows"] if r["name"] == "Home")["off"]
    click_text(browser, "Show all")
    w = where(browser)
    assert api(browser, "/api/settings")["chart_excluded_categories"] == [] and w["slices"] == n_all
    assert not any(r["off"] for r in w["rows"]) and w["hid"] is None
    click_text(browser, "Hide fixed bills")
    assert where(browser)["preset"] == "Show fixed bills"
    fixed = api(browser, "/api/settings")                                # the preset hides every fixed-bill category (insights.py's list)
    assert set(fixed["chart_excluded_categories"]) == set(fixed["fixed_categories"]), fixed
    click_text(browser, "Show fixed bills")
    assert api(browser, "/api/settings")["chart_excluded_categories"] == []
    keys = [c["category"] for g in groups for c in g["categories"]]
    browser.js(f"fetch('/api/settings', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, "
               f"body: JSON.stringify({{chart_excluded_categories: {keys!r}}})}})")
    open_page(browser, site, "overview", mode)
    assert where(browser)["all_hidden"] == "All categories hidden · Show all"
    browser.js(CLICK_TOGGLE.replace("KEY", repr("Home")))
    assert where(browser)["slices"] == 1 and where(browser)["border"] == 0
    click_text(browser, "Show all")
    assert browser.problems() == []


def test_other_is_a_taxonomy_group_without_merged_categories(browser, full):
    open_page(browser, full, "overview", "desktop")
    w = where(browser)
    assert all(r["inc"] is None for r in w["rows"])
    assert all("group=" in r["link"] for r in w["rows"])


# --- card payment matching: the form's dry run and the approximate balance ------------------------------------------

@pytest.mark.parametrize("mode", WIDTHS)
def test_a_matched_card_shows_its_balance_both_ways(browser, full, mode):
    """The `full` DB's Best Buy card: set 40 days ago, matched to its Citi autopay; the connected Double Cash's own
    payment with the same label pairs and is left out."""
    open_page(browser, full, "accounts", mode)
    m = next(a for a in api(browser, "/api/manual")["accounts"] if a["name"] == "Best Buy")
    assert (m["approx"], m["entered_balance"], m["matched_total"], m["balance"], m["nudge"]) == (True, 1110.66, 35.0, 1075.66, True)
    rows = browser.js("[...document.querySelectorAll('#page [data-edit]')].filter((r) => r.textContent.includes('Best Buy'))"
                      ".map((e) => e.closest('.li')).map((r) => ({amt: r.querySelector('.amt').textContent, sub: r.querySelector('.grow > span').textContent}))")
    assert rows and all(r["amt"] == "≈ -$1,075.66" for r in rows), rows
    when = browser.js(f"new Date('{m['balance_as_of']}T12:00:00').toLocaleDateString('en-US', {{month: 'short', day: 'numeric', year: 'numeric'}})")
    # Option C (2026-10-02): the ROW keeps its headline facts only — "27% of $4,000 · updated Aug 23, 2026" — and the
    # "last set $X on …, minus $Y in matched payment … update it from a statement" disclosure lives in the trigger's
    # PANEL. This asserts the half that belongs here: the disclosure must NOT be back on the row. The panel side is
    # already covered end-to-end by test_c_the_disclosure_lives_in_the_credit_card_panel, which opens the real panel on
    # the fixture that has the control (`manual-account` is the row's Details/sheet control, not the disclosure, which
    # is why this file does not try to click one here).
    assert all("last set" not in r["sub"] and "matched payment" not in r["sub"] and
               "update it from a statement" not in r["sub"] for r in rows), (rows, "the disclosure leaked back onto the row")
    open_page(browser, full, "debt", mode)
    bals = browser.js("[...document.querySelectorAll('#page .dl .bal, #page .dg .xtra .amt')].map((e) => e.textContent)")
    assert bals.count("≈ $1,075.66") == 2, bals                               # the Credit cards group and All debts
    # the form's dry run: the autopay line counts, the Double Cash payment with the same label doesn't
    open_page(browser, full, "accounts", mode)
    box = browser.js("""(async () => { const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      [...document.querySelectorAll('#page [data-edit]')].find((b) => b.textContent.includes('Best Buy')).click();
      for (let i = 0; i < 60 && !document.querySelector('#sheet .matchbox'); i++) await sleep(50);
      return document.querySelector('#sheet .matchbox').textContent; })()""")
    assert box.startswith("This would have matched 1 payment ($35) in the last 90 days. 1 more pays a connected card "
                          "and is left out."), box
    browser.js("document.querySelector('#sheet [aria-label=Close]').click()")
    assert browser.problems() == []

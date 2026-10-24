"""Browser checks in headless Chrome (tests/cdp.py): what only a real layout can show. Skipped when Chrome
isn't installed. The page runs against a synthetic DB (tests/demo_data.py) on a throwaway loopback server;
Chrome blocks every https:// request and plaid.com, so nothing leaves the machine."""

from __future__ import annotations

from datetime import date

import pytest

import demo_data
from cdp import Chrome, Server, find_chrome
from pngtools import logo_png
from hermes_finance import db, manual

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
WIDTHS = {"desktop": (1440, 900), "phone": (390, 844)}
PAGES = ("overview", "transactions", "budgets", "recurring", "accounts", "investments", "debt")


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("browser") / "finance.db"
    conn = db.connect(path)
    # Mid-month fixture: month-start runs otherwise lack the transactions and calendar occurrences exercised below.
    today = date(2026, 9, 26)
    demo_data.build(conn, today=today)
    demo_data.extras(conn, today=today)
    seed_logos(conn)
    seed_late_bill(conn, today)
    conn.close()
    srv = Server(path, today=today)
    yield srv
    srv.close()


def seed_late_bill(conn, today=None):
    """A monthly bill whose next charge is two weeks overdue, so "Late" labels show (Recurring, Overview bills)."""
    from datetime import timedelta
    today = today or date.today()
    with conn:
        for k, back in enumerate((107, 76, 45)):
            d = (today - timedelta(days=back)).isoformat()
            conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount, "
                         "pending, pfc_primary, pfc_detailed, raw_json, first_seen_at, updated_at) VALUES "
                         "(?, 'acc_nova_chk', 'demo_nova', ?, 'COMCAST CABLE', 'Comcast', 89.99, 0, 'RENT_AND_UTILITIES', "
                         "'RENT_AND_UTILITIES_INTERNET_AND_CABLE', '{}', ?, ?)", (f"late_{k}", d, d, d))


def seed_logos(conn):
    """The three kinds of institution row (a Plaid logo, the bank's own icon, initials in its real colour), one
    cached holding logo, and a manual card whose typed institution matches a cached name exactly."""
    with conn:
        conn.executemany("INSERT INTO institutions VALUES (?,?,?,?,?,?, '2026-09-26T00:00:00Z')", [
            ("ins_demo_nova", "Nova", logo_png(64), "plaid", "#00a9ce", "https://www.novabank.example"),
            ("ins_demo_chase", "Chase", logo_png(48), "site", "#095aa6", "https://www.chase.com"),
            ("ins_demo_usb", "U.S. Bank", None, "none", "#102670", "https://www.usbank.com")])
        conn.execute("INSERT INTO security_logos VALUES ('NVDA', ?, 'fmp', '2026-09-26T00:00:00Z')", (logo_png(64),))
    manual.create(conn, {"kind": "credit_card", "name": "Sapphire", "institution": "Chase", "credit_limit": 6000.0,
                         "balance": 420.0}, date.today())


@pytest.fixture(scope="module")
def browser():
    with Chrome() as b:
        # The UI chooses its initial month from the browser clock, not Server.today. Pin both sides together.
        b.call('Page.addScriptToEvaluateOnNewDocument', source="""
          { const NativeDate = Date, now = new NativeDate(2026, 8, 26, 12).getTime();
            window.Date = class extends NativeDate {
              constructor(...args) { super(...(args.length ? args : [now])); }
              static now() { return now; }
            };
          }
        """)
        yield b


def open_page(browser, site, page, mode):
    browser.size(*WIDTHS[mode])
    browser.goto(f"{site.url}/?t={mode}#/{page}")
    browser.problems()


# --- bug: the budget editor was clipped at the card's bottom edge -------------------------------------

AFTER_CHANGE = "await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 80))));"
EDITORS = """(async () => {
  const out = [];
  const still = async () => {   // the sheet has slid in and the focus has landed
    AFTER_CHANGE
    await Promise.all(document.getAnimations().map((a) => a.finished.catch(() => 0)));
    AFTER_CHANGE
  };
  const measure = (what) => {
    const sh = document.getElementById('sheet'), s = sh.getBoundingClientRect(), input = sh.querySelector('.bx input');
    const i = input.getBoundingClientRect();
    out.push({what, sh: sh.scrollHeight, ch: sh.clientHeight, editor_bottom: Math.round(Math.max(s.bottom, i.bottom)),
      card_bottom: innerHeight, top: Math.round(s.top), on: document.getElementById('modal').classList.contains('on')});
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  };
  // The editor is the app's sheet (a bottom sheet on a phone, a dialog on desktop): it must fit the screen whole
  for (const row of document.querySelectorAll('#page tr.c')) {
    const name = row.querySelector('.nml').textContent;
    row.querySelector('.plan').click();
    await still();
    measure(name);
    await still();
  }
  const nb = document.querySelector('#page .nb');
  for (const btn of nb.querySelectorAll('.setc')) {
    btn.click();
    await still();
    measure('No budget yet: ' + btn.closest('.li').querySelector('.grow > b, .grow > a').textContent);
    await still();
  }
  return out;
})()""".replace("AFTER_CHANGE", AFTER_CHANGE)


@pytest.mark.parametrize("mode", WIDTHS)
def test_budget_editor_is_never_clipped(browser, site, mode):
    """Opening the editor on a Budgets row must not cut it off: the sheet fits the screen whole, with nothing scrolling inside it."""
    open_page(browser, site, "budgets", mode)
    rows = browser.js(EDITORS)
    assert len(rows) >= 5 + 3                                           # 5 capped rows + uncapped rows
    clipped = [r for r in rows if not r["on"] or r["sh"] > r["ch"] or r["editor_bottom"] > r["card_bottom"] or r["top"] < 0]
    assert not clipped, clipped
    assert browser.problems() == []


# --- bug: icon badges drew their glyph small in the top-left corner (the Investments "sprout") --------

BADGES = """(() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && !el.closest('[hidden], template'); };
  const ink = (svg) => { let l = 1e9, t = 1e9, r = -1e9, b = -1e9;
    for (const s of svg.querySelectorAll('path, circle, rect, line, polyline, polygon, ellipse')) {
      const q = s.getBoundingClientRect(); if (!q.width && !q.height) continue;
      l = Math.min(l, q.left); t = Math.min(t, q.top); r = Math.max(r, q.right); b = Math.max(b, q.bottom); }
    return {l, t, r, b}; };
  const out = [];
  for (const tile of document.querySelectorAll('.cb, h3 .hi, .ck, .tick')) {
    const svg = tile.querySelector(':scope > svg');
    if (!svg || !vis(tile)) continue;
    const T = tile.getBoundingClientRect(); const I = ink(svg);
    const rgb = getComputedStyle(tile).getPropertyValue('--rgb').trim();
    const cls = tile.className.baseVal === undefined ? tile.className : tile.className.baseVal;
    out.push({tile: cls, icon: svg.dataset.icon || '', where: (tile.closest('.card, .sheet, .panel, .alerts, aside') || {}).className || '',
      w: Math.round(T.width), dx: +((I.l + I.r) / 2 - (T.left + T.right) / 2).toFixed(2),
      dy: +((I.t + I.b) / 2 - (T.top + T.bottom) / 2).toFixed(2), size: +(Math.max(I.r - I.l, I.b - I.t) / T.width).toFixed(3),
      color_ok: !rgb || tile.matches('.ck, .tick') || getComputedStyle(svg).stroke.replace(/ /g, '') === `rgb(${rgb})`.replace(/ /g, '')});
  }
  for (const p of document.querySelectorAll('.pill')) {
    const svg = p.querySelector(':scope > svg');
    if (!svg || !vis(p)) continue;
    const P = p.getBoundingClientRect(); const S = svg.getBoundingClientRect();
    out.push({tile: 'pill', icon: svg.dataset.icon || '', where: p.textContent.slice(0, 30), w: Math.round(P.height), dx: 0,
      dy: +((S.top + S.bottom) / 2 - (P.top + P.bottom) / 2).toFixed(2), size: 0.55, color_ok: true});
  }
  return out;
})()"""
DIALOGS = {  # extra states with badges: the Add account chooser and its manual list, a form, the loan panel
    "chooser": "document.getElementById('addacct').click()",
    "manual-list": "[...document.querySelectorAll('#sheet .opt')].find((b) => b.textContent.includes('Add manually')).click()",
    "loan-form": "[...document.querySelectorAll('#sheet .opt')].find((b) => b.textContent.startsWith('Loan')).click()",
}


def bad_badges(rows):
    return [r for r in rows if abs(r["dx"]) > 1.0 or abs(r["dy"]) > 1.0 or not 0.48 <= r["size"] <= 0.62
            or not r["color_ok"]]


@pytest.mark.parametrize("mode", WIDTHS)
def test_every_badge_glyph_is_centered_and_sized(browser, site, mode):
    """Every icon tile (badges, card-title tiles, check circles, holding bubbles) draws its glyph centered (within
    1px) at 48-62% of the tile, in the tile's colour; every pill icon is vertically centered."""
    seen, bad = 0, {}
    for page in PAGES:
        open_page(browser, site, page, mode)
        rows = browser.js(BADGES)
        seen += len(rows)
        if bad_badges(rows):
            bad[page] = bad_badges(rows)
    open_page(browser, site, "debt", mode)
    for name, click in DIALOGS.items():
        browser.js(click)
        browser.settle()
        rows = browser.js(BADGES)
        seen += len(rows)
        if bad_badges(rows):
            bad[name] = bad_badges(rows)
    browser.js("document.querySelector('#sheet [aria-label=Close]').click()")
    browser.js("document.querySelector('#page [data-loan]').click()")
    browser.js("new Promise((r) => setTimeout(r, 400))")
    rows = browser.js(BADGES)
    seen += len(rows)
    if bad_badges(rows):
        bad["loan-panel"] = bad_badges(rows)
    assert seen > 100
    assert not bad, bad
    assert browser.problems() == []



# --- sweep: status colours on figures never showed (container rules like `.sub b` beat `.bad`) ---------

STATUS = """(() => {
  const want = {pos: 'rgb(79, 209, 165)', bad: 'rgb(255, 123, 123)', warn: 'rgb(245, 180, 84)', accc: 'rgb(142, 168, 255)'};
  // variants that use the class for their own palette (pills, count chips, alert strips...) are not text colours
  const skip = '.pill, .bc, .al, .cal, .dot, .st, .fchip, .tag, .ferr, .rc, .tip, .ck, .cb, .formerr, #msg, .toast, .btn, .ia';
  const out = [];
  let seen = 0;
  for (const [cls, rgb] of Object.entries(want)) {
    for (const el of document.querySelectorAll('.' + cls)) {
      const r = el.getBoundingClientRect();
      if (el.closest(skip) || !r.width || !el.textContent.trim()) continue;
      seen += 1;
      const got = getComputedStyle(el).color;
      if (got !== rgb) out.push({cls, got, text: el.textContent.trim().slice(0, 40), in: (el.parentElement.className || '') + ''});
    }
  }
  return {seen, wrong: out};
})()"""


@pytest.mark.parametrize("mode", WIDTHS)
def test_status_colours_show_on_every_page(browser, site, mode):
    """A figure marked .pos / .bad / .warn / .accc renders in that colour: "2 over" red, "Own $X" green, a card's
    utilization amber, a loan's "% paid off" in the accent, a liability's amount red."""
    seen, wrong = 0, {}
    for page in PAGES:
        open_page(browser, site, page, mode)
        r = browser.js(STATUS)
        seen += r["seen"]
        if r["wrong"]:
            wrong[page] = r["wrong"]
    assert seen >= 20
    assert not wrong, wrong
    open_page(browser, site, "recurring", mode)                      # non-vacuous: a late bill is on the page
    # (TASK-19: the bills calendar, in the Next 30 days card's place, marks it late: a red ring on the grid / agenda)
    assert browser.js("[...document.querySelectorAll('#page .calcard .ck.late')].filter((e) => e.getClientRects().length).length") >= 1


# --- sweep: no money figure is cut off (the holdings Cash row hid each account's amount) ---------------

FIGURES = """(() => {
  const out = [];
  let seen = 0;
  for (const el of document.querySelectorAll('#page .v, #page .amt, #page .gtot, #page .bal, #banks .v')) {
    const r = el.getBoundingClientRect();
    if (!r.width || !el.textContent.trim() || el.closest('[hidden]')) continue;
    seen += 1;
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (['auto', 'scroll'].includes(cs.overflowX)) break;         // inside a scroller (the Recurring timeline): reachable
      if (!['hidden', 'clip'].includes(cs.overflowX)) continue;
      const q = a.getBoundingClientRect();
      if (r.left < q.left - 1 || r.right > q.right + 1 || (el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).overflowX !== 'visible')) {
        out.push({text: el.textContent.trim().slice(0, 30), cut_by: a.className.toString().slice(0, 40),
          el: [Math.round(r.left), Math.round(r.right)], box: [Math.round(q.left), Math.round(q.right)]});
        break;
      }
    }
  }
  return {seen, cut: out};
})()"""


@pytest.mark.parametrize("mode", WIDTHS)
def test_no_money_figure_is_cut_off(browser, site, mode):
    seen, cut = 0, {}
    for page in PAGES:
        open_page(browser, site, page, mode)
        r = browser.js(FIGURES)
        seen += r["seen"]
        if r["cut"]:
            cut[page] = r["cut"]
    assert seen >= 100
    assert not cut, cut


# --- every in-card change re-packs (the class of bug 1, not just the cap editor) -----------------------

CHANGES = """(async () => {
  const wait = async () => { AFTER_CHANGE };
  const clip = (card) => ({sh: card.scrollHeight, ch: card.clientHeight, h: Math.round(card.getBoundingClientRect().height)});
  const out = {};
  const page = location.hash.slice(2);
  if (page === 'budgets') {
    // 2026-10-02: the "See suggestions" disclosure is gone (it revealed a `.sg` line that moved into each row's
    // Details panel), so this block no longer clicks it. What remains is the inline editor error, which still makes
    // the editor taller and must still fit its sheet rather than clip.
    document.querySelector('#page tr.c .plan').click();
    await wait();
    const sheet = document.getElementById('sheet');
    const input = sheet.querySelector('.bx input');
    input.value = 'lots';
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    await wait();
    out.error = {...clip(sheet), text: sheet.querySelector('.bx .err').textContent};
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  }
  if (page === 'debt') {
    const card = document.querySelector('#page .dg').closest('.card');
    const before = clip(card);
    document.querySelector('#page .dg .hd[aria-expanded]').click();
    await wait();
    out.cards_group = {before, after: clip(card), open: document.querySelector('#page .dg.open') !== null};
  }
  return out;
})()""".replace("AFTER_CHANGE", AFTER_CHANGE)


@pytest.mark.parametrize("mode", WIDTHS)
def test_a_packed_budget_card_re_packs_without_clipping(browser, site, mode):
    """The editor's inline error and the Credit cards group each make a packed card taller, and each re-packs so
    nothing is clipped.

    Rewritten 2026-10-02. This used to open with "See suggestions" and assert the no-budget card grew, which was a real
    re-pack check. That button is gone (the owner's call: it revealed a `Suggested $X` line that moved into each row's own
    Details panel, so it had nothing left to reveal), and every "No budget yet" row is now a fixed 65px by design.
    A dead control that grows nothing cannot be used to prove re-packing, so the case it covered is now covered by the
    two live ones below — plus the uniform-height assertion, which is the property that replaced it.
    """
    open_page(browser, site, "budgets", mode)
    b = browser.js(CHANGES)
    assert b["error"]["text"].startswith("Enter an amount") and b["error"]["sh"] <= b["error"]["ch"], b["error"]
    # the replacement invariant: the no-budget card's rows are all one height, and none of them overflows
    rows = browser.js("""(() => [...document.querySelectorAll('#page .nb .li')].map((r) => {
      const rr = r.getBoundingClientRect();
      return {name: r.querySelector('.grow > b, .grow > a').textContent, h: Math.round(rr.height),
              sh: r.scrollHeight, ch: r.clientHeight,
              nameCut: (() => { const bb = r.querySelector('.grow > b, .grow > a');   // a group row's name is a link (9c9ef94)
                return bb.scrollWidth > bb.clientWidth + 1; })()};
    }))()""")
    assert rows, "the fixture has categories without a budget"
    rows = [r for r in rows if r["h"] > 0]     # P2A: rows with nothing spent are folded away until "Show N categories..."
    assert rows, "the fixture has categories without a budget that carry spending"
    if mode == "desktop":   # P2A: on a phone a row's reason may wrap and its buttons drop under it, so heights differ there
        assert len({r['h'] for r in rows}) == 1, (mode, rows, "these rows must all be the same height")
    for r in rows:
        assert r["sh"] <= r["ch"], (mode, r)
        assert not r["nameCut"], (mode, r, "a category name must never be cut")
    open_page(browser, site, "debt", mode)
    d = browser.js(CHANGES)["cards_group"]
    assert d["open"] and d["after"]["h"] > d["before"]["h"] and d["after"]["sh"] <= d["after"]["ch"], d
    assert browser.problems() == []


# --- real bank logos and holding logos, with the fallbacks ---------------------------------------------

LOGOS = """(async () => {
  await Promise.all([...document.images].map((i) => i.complete ? 0 : new Promise((r) => { i.onload = i.onerror = r; })));
  const chip = (el) => { if (!el) return null; const img = el.querySelector('img'); const cs = getComputedStyle(el);
    return {cls: el.className, img: img ? img.getAttribute('src') : null, loaded: img ? img.naturalWidth > 0 : null,
      bg: cs.backgroundColor, round: cs.borderRadius, text: el.textContent.trim(), dot: !!el.querySelector('.st'),
      w: Math.round(el.getBoundingClientRect().width)}; };
  const q = (sel) => [...document.querySelectorAll(sel)];
  return {
    sidebar: q('#banks .bank .link').map((a) => ({name: a.querySelector('.nm').textContent, ...chip(a.querySelector('.ia'))})),
    rows: q('#page .li.acct').map((r) => ({name: r.querySelector('.grow b').textContent, ...chip(r.querySelector('.ia'))})),
    manual: q('#page [data-edit], #page [data-loan]').map((e) => e.closest('.li')).map((r) => ({name: (r.querySelector('.grow b') || r).textContent,
      ia: chip(r.querySelector('.ia')), badge: !!r.querySelector('.cb')})),
    tx: q('#page tr:not(.day) > td.c-acct').slice(0, 40).map((td) => ({text: td.textContent, ...chip(td.querySelector('.ia'))})),
    ticks: q('#page .tick').map((t) => ({row: t.closest('.li').querySelector('.grow b').textContent, ...chip(t)})),
  };
})()"""


def test_bank_and_holding_logos_with_their_fallbacks(browser, site):
    open_page(browser, site, "accounts", "desktop")
    a = browser.js(LOGOS)
    side = {s["name"]: s for s in a["sidebar"] if s["name"] != "Added by you"}    # banks (the manual row has a badge)
    assert side["Nova"]["img"].startswith("/api/institutions/ins_demo_nova/logo") and side["Nova"]["loaded"]
    assert side["Chase"]["img"].startswith("/api/institutions/ins_demo_chase/logo") and side["Chase"]["loaded"]
    assert side["U.S. Bank"]["img"] is None and side["U.S. Bank"]["bg"] == "rgb(16, 38, 112)"  # initials, real colour
    assert side["U.S. Bank"]["text"] == "U" and all(s["dot"] for s in side.values())          # the status dot stays
    assert side["Nova"]["bg"] == "rgb(255, 255, 255)" and side["Nova"]["round"] == "50%"      # a light round chip
    rows = [r for r in a["rows"] if r["img"]]
    assert {r["img"].split("?")[0] for r in rows} == {"/api/institutions/ins_demo_nova/logo",
                                                      "/api/institutions/ins_demo_chase/logo"}
    assert all(r["loaded"] for r in rows)
    man = {m["name"].split("manual")[0].strip(): m for m in a["manual"]}
    sapphire = next(v for k, v in man.items() if k.startswith("Sapphire"))
    assert sapphire["ia"] and sapphire["ia"]["img"].startswith("/api/institutions/ins_demo_chase/logo")  # exact name
    zenith = next(v for k, v in man.items() if k.startswith("Zenith"))
    assert zenith["ia"] is None and zenith["badge"]                                          # no match: the kind icon
    open_page(browser, site, "transactions", "desktop")
    tx = browser.js(LOGOS)["tx"]
    assert tx and all(t["cls"] for t in tx)
    assert {t["img"].split("?")[0] for t in tx if t["img"]} <= {"/api/institutions/ins_demo_nova/logo",
                                                                "/api/institutions/ins_demo_chase/logo"}
    assert any(t["img"] for t in tx) and all(t["loaded"] for t in tx if t["img"])
    open_page(browser, site, "investments", "desktop")
    ticks = {t["row"]: t for t in browser.js(LOGOS)["ticks"]}
    nvda = next(v for k, v in ticks.items() if k.startswith("NVDA"))
    assert nvda["img"] == "/api/securities/NVDA/logo" and nvda["loaded"] and nvda["bg"] == "rgb(255, 255, 255)"
    cash = ticks["Cash"]
    assert cash["img"] is None and cash["text"] == ""                                       # the $ badge (an icon)
    assert browser.problems() == []                                                         # no CSP report either


# --- sweep: the confirmation toast after a save was hidden at once by the page refresh ------------------

SAVE_AND_READ_TOAST = """(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  document.querySelector('#page [data-edit]').click();
  for (let i = 0; i < 40 && !document.querySelector('#sheet form #f-balance'); i++) await sleep(50);
  const input = document.getElementById('f-balance');
  input.value = String(Number(input.value.replace(/[^0-9.]/g, '')) + 1);
  input.dispatchEvent(new Event('input', {bubbles: true}));
  document.querySelector('#sheet form').requestSubmit();
  await sleep(900);
  const t = document.getElementById('toast');
  return {on: t.classList.contains('on'), text: t.textContent};
})()"""


def test_the_saved_toast_stays_visible_after_the_page_refreshes(browser, site):
    """Saving a manual account shows "Saved <name>." and keeps it up while the page reloads its data (the refresh
    used to hide it the same instant, so no save, add, archive or paid-off ever showed its confirmation)."""
    open_page(browser, site, "accounts", "desktop")
    t = browser.js(SAVE_AND_READ_TOAST)
    assert t["text"].startswith("Saved ") and t["on"], t
    assert browser.problems() == []


# --- sweep: the recategorize toast said "5 mores from them" -------------------------------------------

RECATEGORIZE = """(async () => {
  const sel = [...document.querySelectorAll('#page button.catb')].find((x) => x.value === 'Gas'
    && [...document.querySelectorAll('#page tr')].filter((tr) => tr.querySelector('.mt b .t')
      && tr.querySelector('.mt b .t').textContent === x.closest('tr').querySelector('.mt b .t').textContent).length > 2);
  sel.click();   // polish P0: the grouped category picker
  for (let i = 0; i < 50 && !document.querySelector('.cpk .cpko'); i++) await new Promise((r) => setTimeout(r, 50));
  document.querySelector('.cpk .cpko[data-cat="Travel"]').click();
  await new Promise((r) => setTimeout(r, 700));
  return document.getElementById('toast').textContent;
})()"""


def test_the_recategorize_toast_reads_right(browser, site):
    open_page(browser, site, "transactions", "desktop")
    text = browser.js(RECATEGORIZE)
    assert text.startswith("Moved ") and "more from them in this list" in text and "mores" not in text, text
    assert browser.problems() == []


# --- the editor's "Suggested $X · Use" (writes: keep this test last) ----------------------------------

EDITOR_USE = """(async () => {
  const wait = async () => { AFTER_CHANGE };
  const row = [...document.querySelectorAll('#page tr.c')].find((c) => c.querySelector('.nml').textContent === NAME);
  row.querySelector('.plan').click();
  await wait();
  await Promise.all(document.getAnimations().map((a) => a.finished.catch(() => 0)));
  const sheet = document.getElementById('sheet');
  const sug = sheet.querySelector('.bx-sug');
  const use = sug && [...sug.querySelectorAll('button')].find((b) => b.textContent === 'Use');
  const info = {text: sug ? sug.textContent : null, useClass: use ? use.className : null,
    useBg: use ? getComputedStyle(use).backgroundColor : null, sh: sheet.scrollHeight, ch: sheet.clientHeight};
  if (use) use.click();
  await new Promise((r) => setTimeout(r, 600));
  return info;
})()""".replace("AFTER_CHANGE", AFTER_CHANGE)


def test_the_editor_offers_the_suggestion_and_use_saves_it(browser, site):
    open_page(browser, site, "budgets", "desktop")
    d = browser.js(f"fetch('/api/budgets').then((r) => r.json())")
    capped = [c for c in d["categories"] if c["cap"] is not None and c["suggested"] is not None
              and c["suggested"] != c["cap"]]
    assert capped, "the demo data needs a capped category with a different suggestion"
    target = capped[0]
    before_cards = browser.js("[...document.querySelectorAll('#page tr.c')].map((s) => s.textContent)")
    assert not any("Suggested" in t for t in before_cards)                       # quiet until the editor opens
    browser.requests()
    info = browser.js(EDITOR_USE.replace("NAME", repr(target["category"])))
    assert info["text"].replace("\u00a0", " ").startswith("Suggested $") and "Use" in info["text"]
    assert info["useBg"] != "rgb(238, 240, 246)" and "p" not in info["useClass"].split()   # plain, never the accent
    assert info["sh"] <= info["ch"]
    posts = [u for m, u in browser.requests() if m == "POST"]
    assert posts == [f"{site.url}/api/budgets/cap"]
    after = browser.js("fetch('/api/budgets').then((r) => r.json())")
    cap = next(c for c in after["categories"] if c["category"] == target["category"])["cap"]
    assert cap == target["suggested"]
    browser.js(f"fetch('/api/budgets/cap', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, "
               f"body: JSON.stringify({{category: {target['category']!r}, monthly_cap: {target['cap']}}})}})")
    assert browser.problems() == []

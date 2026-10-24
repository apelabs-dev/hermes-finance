"""Gate 2 (TASK-11 final review): the layout audit.

    uv run python scripts/audit_layout.py <base_url> [--out shots/011] [--label NAME] [--widths 1440,1024,390]
                                          [--states investments,...] [--inject-css FILE]

Opens every page, and every modal / drawer / editor / panel state the click-through opens (TASK-14 / 15: also Accounts,
the Overview and the phone drawer with the net worth switch on Custom, and its Edit sheet, as opened and with a group
mixed and one folded), in headless Chrome (tests/cdp.py: temp
profile, random DevTools port, every https:// request and plaid.com blocked) at 1440, 1024 and 390 px (TASK-18: every
page, and the Settings / first-run / import / sign-in screens, at 390, 768, 1024, 1280 and 1440 in the dark and the light
theme; TASK-21: the Track as a bill sheet, a Recurring row with "~$ est." and its range, its bill sheet and the calendar's
every-2-months chip at all seven widths in both themes), and fails on:

  clipping    an element with overflow hidden/clip whose content is bigger than its box (cards, legends, rows); an
              ellipsis only where names are shortened on purpose, never on a money figure; (TASK-14b) text inside a
              ring (a %, "used", "with bills") whose glyphs, measured in its own font, reach past the ring's hole into
              the arc by more than 0.5 px, or wrap
  alignment   cards side by side whose tops miss each other by 1-12 px (a masonry offset is more; then the gaps are
              checked); a row's avatar / icon off the middle of its text by more than 2 px; the right-aligned amounts
              of one list not on one edge (1 px); a badge glyph off its tile's centre by more than 1 px
  spacing     the same kind of card with different padding (1 px); gaps between cards (column and row) not the grid's
              gap (1 px); two siblings whose boxes overlap
  charts      a one-slice doughnut with a notch (its border, or the card showing at 12 o'clock); a legend that doesn't
              fit its card; a canvas outside its box
  text        text under 13 px (TASK-18); contrast under 4.5:1 (3:1 for large text); anything behind the phone tab bar;
              (TASK-18) a phone tab label under 13 px, on two lines or cut
  overlays    a toast, modal or panel not fully on screen, or under the phone tab bar
  segments    (TASK-12b) a segmented control (.seg, or any role=group / radiogroup of toggle buttons, or 2+ aria-pressed
              buttons side by side) whose gaps differ by more than 1 px (per line); and one stretched across its width
              (wider than its buttons' natural widths + 8 px, with the buttons spread over it) whose buttons' widths
              differ by more than 1 px; (TASK-15) a switch (role=switch, or a role=checkbox button) smaller than
              36 x 20 px, or with its knob outside its track
  empty       (TASK-12b) an empty-state message (.emptyov, .empty, [data-empty]) off the horizontal centre of its chart
              area or container by more than 4 px; with no chart drawn there, off the vertical centre by more than 8 px,
              or with more than 120 px of blank space above or below it; with a chart drawn (one point), covering a point

TASK-18 ports the reviewer's independent audit (sketches/017-launch/audit_independent.py, and shoot_017.py's row check):

  overflow    the page wider than the window
  escape      an element sticking out of its card, sheet, panel or frame sideways (not inside a horizontal scroller)
  cliptext    a text cut by an overflow-hidden box: measured on the text's own rects; an ellipsis only counts where one
              can be drawn (single-line text in a block box), so a cut word on a wrapping or flex line fails
  wrapped     a button, pill, chip, tag or segment whose label wraps (its text nodes' lines; an icon isn't a line;
              choice cards, option rows and ghost rows are two lines by design)
  rows        cards side by side (tops within 2 px) whose title, hero figure, bar or first detail line (a frame's panel
              and heading) are more than 2 px apart
  orphans     a lone fragment ("29)", "it", "ago") alone on the last line of a wrapped text

HOTFIX-1, in the light theme:

  darkbg      a select, text field, textarea or field wrapper, or a surface (card, sheet, panel, sync panel, toast, sidebar,
              tab bar, dialog, menu) whose fill is dark, or which a dark gradient layer paints over a fifth of its box or
              more (Settings' weekly summary day select was slate with white triangles: its arrow drawn full size); any
              other box but a button with a near-black fill or gradient of its own (a car page's chips and notes)

TASK-20 / 22d, on a phone (under 1024px):

  tap         a control under 44 x 44 px to hit (its box, its own ::before / ::after hit box, an input's <label>)
  tap-covered something else on top at a control's centre (elementFromPoint), the page scrolled to bring each on screen

docs/ui-invariants.md rule 19:

  rowname     a list row's name that wraps onto a second line and makes its row taller than a row beside it in the same
              list, of the same shape, whose name is on one line

docs/ui-invariants.md rule 20:

  cutnum      a figure (a currency amount, a percentage, a number with thousands separators or a decimal one) that loses a
              character to an ellipsis, a line clamp or a box that clips it (each character measured); labels may truncate,
              and digits inside a word or a name ("Store 4521") aren't a figure

Hidden layers are skipped exactly ([hidden], <template>, .sr, aria-hidden, a closed <details>, anything invisible).
Every new check first runs on the Overview with a planted bug (selftest()); the run stops if one misses it.

--states limits the run to those states; --inject-css adds a stylesheet after each page loads (the gate's own
discriminating test re-introduces old CSS this way and must see these checks fail).

It writes <out>/layout-report.json (every check, per label / width / state) and one screenshot per FAIL with red boxes
around the offending elements (<out>/fail-<label>-<width>-<state>-<check>.png). It never saves anything: forms are
opened and closed, never submitted with valid data (one empty submit shows the inline errors); the only write-like step
is none. Exit status 1 on any FAIL.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from cdp import READY_NOW, Chrome  # noqa: E402

WIDTHS = (1440, 1280, 1024, 768, 430, 390, 360)  # TASK-18: every page at 1440 / 1280 / 1024 / 768 / 390 in both themes;
                                                 # TASK-20: 430 and 360 too (TAP_WIDTHS), dark
CORE_WIDTHS = (1440, 1024, 390)          # the modal / editor / panel states (TASK-11's set), dark theme
LIGHT_WIDTHS = (1440, 390)               # HOTFIX-1: and in the light theme at these (sheets, the sync panel, the drawer)
THEMES = ("dark", "light")
TAP_WIDTHS = (430, 360)                  # TASK-20: the tap check's other phones (390 is in WIDTHS): pages, drawer, dark
HEIGHT = {1440: 900, 1280: 800, 1024: 768, 768: 1024, 430: 932, 390: 844, 360: 780}
PHONE = 1024                             # below this: the one-column layout (drawer + tab bar)
DOCK = 1400                              # TASK-19: from here the transaction sheet docks beside the table
WORKERS = 4                              # TASK-20: headless Chromes per DB (the run is mostly waiting on the pages)
PAGES = ("overview", "transactions", "budgets", "recurring", "accounts", "investments", "debt", "goals")  # + TASK-19
CHECKS = ("clipping", "alignment", "spacing", "charts", "text", "overlays", "segments", "empty",
          "overflow", "escape", "cliptext", "wrapped", "rows", "orphans", "tap", "tap-covered", "darkbg", "headers",
          "rowname", "cutnum")
NEW_CHECKS = CHECKS[8:]

REPORT_LOCK = threading.Lock()
# TEST-SPEED: no fixed waits. After the page loads, after the theme is switched or a stylesheet added, and after every step,
# the run waits until the page is settled (tests/cdp.py PEND: no fetch or body in flight, no timer of 1 s or less, no
# animation frame asked for, no finite CSS animation / transition running, fonts and images loaded, and two frames without
# a DOM change). The fixed waits it replaces (2 frames + 120 ms after each step, 0.15 s + 2 frames + 60 ms before the
# checks: 1.2 s a state on average) were most of the gate's time, and a busy machine could outlast them.


def click(sel: str, text: str | None = None) -> str:
    """JS: click the first visible element matching `sel` whose text starts with `text` (throws if there is none)."""
    return (f"(() => {{ const els = [...document.querySelectorAll({json.dumps(sel)})].filter((e) => e.getClientRects().length);"
            f" const t = {json.dumps(text)}; const el = els.find((e) => t === null || e.textContent.trim().startsWith(t));"
            f" if (!el) throw new Error('nothing to click: ' + {json.dumps(sel)} + ' ' + t); el.click(); return true; }})()")


def wait_for(sel: str, ms: int = 4000) -> str:
    """JS: wait until `sel` is on the page (at most `ms`; TASK-21: a longer wait where a step waits on a server answer that a
    busy machine, running a dozen Chromes, can take several seconds to give). TEST-SPEED: it gives up ("never appeared")
    as soon as the page is settled with nothing left that could still bring it within `ms` (tests/cdp.py __appear: no
    fetch, no timer due, no interval, no animation, two frames without a DOM change), not at the end of `ms`: a state
    that isn't on a DB is a SKIP at once."""
    return f"window.__appear(() => document.querySelector({json.dumps(sel)}), {ms}, {json.dumps(sel)})"


SLOW = 15000   # ms: a step waiting on an answer from the server (see wait_for)


ADD = "#addacct"  # desktop / tablet; phones open the chooser from the page
# JS: type a value into an input the way a person would (so the form's input listeners run)
FILL = ("(() => {{ const i = document.querySelector({sel!r}); i.value = {v!r}; i.dispatchEvent(new Event('input', "
        "{{bubbles: true}})); return 1; }})()")
FORMS = ("Bank account", "Credit card", "Investment", "Loan", "Car", "Other asset", "Other liability")
# TASK-13b: a car / property row opens its page (#/asset/<id>); the page's pictures are loaded before the checks
IMAGES = ("new Promise((r) => Promise.all([...document.querySelectorAll('#page img')].map((i) => i.complete ? 1 : "
          "new Promise((x) => { i.onload = i.onerror = x; }))).then(() => r(1)))")


def asset(kind: str, name: str | None = None) -> list[str]:
    return [click(f"#page a[data-asset][data-kind={kind}]", name), wait_for("#page .vd-hero"), IMAGES]


EDIT = [click("#page .vd-actions .btn", "Edit"), wait_for("#sheet form")]

# TASK-21: the transaction sheet (docked from 1400px, the usual sheet below), and the bills calendar moved on to the month
# that shows an every-2-months bill (its chip on the grid; on a phone, its agenda row)
TXBILL = "#txpanel:not([hidden]) .billsec .btn, #sheet.txsheet .billsec .btn"
BIMONTHLY_JS = ("new Promise((res, rej) => { let n = 0; const sel = '#page .bchip[data-cadence=bimonthly], #page .agi[data-cadence=bimonthly]';"
                " const label = () => (document.querySelector('#page .calnav span') || {}).textContent;"
                " const step = () => { if ([...document.querySelectorAll(sel)].some((e) => e.getClientRects().length)) return res(n);"
                " const next = document.querySelector(\"#page .calnav button[aria-label='Next month']\");"
                " if (n++ >= 12 || !next || next.disabled) return rej(new Error('never appeared: an every-2-months bill on the calendar'));"
                " const was = label(); next.click();"   # the next month drawn (its label changes), then look again
                # (TEST-SPEED: once the page has settled, not 150 ms after the label changed; the label's change waited for
                # without polling, 8 s at most as before)
                " window.__appear(() => label() !== was, 8000, 'the next month').catch(() => 0).then(() => window.__settled()).then(step); };"
                " step(); })")

# TASK-22f: Sync now's panel (#syncp) in each of its states. POST /api/sync is answered in the page (window.fetch) with
# what the server sends for that state, built from this DB's own banks and accounts (so the logos are its own), never a
# real sync; a 429 answered this way logs nothing (a real one logs "Failed to load resource"). "busy" never answers (the
# header's Syncing…, HOTFIX-1: with its border running). HOTFIX-1: "skip" answers "Ask banks" with nothing synced again
# (asked within a minute of a sync); "lost" rejects the POST as a phone's browser does ("Load failed") and the look-up (GET
# /api/sync?rid=) says the server never got it. KEEP is a pointer on the panel, so "Nothing new" doesn't close by itself
# (4 s) mid-check.
SYNC_JS = r"""((kind) => (async () => {
  const d = await (await fetch('/api/accounts')).json();
  const banks = d.items.filter((it) => !it.removed_at);
  const accts = d.net_worth.institutions.filter((b) => b.item_id !== 'alpaca').flatMap((b) => b.accounts.map((a) =>
    ({...a, bank: b.institution_name, institution_id: b.institution_id, item_id: b.item_id})));
  const moved = accts.slice(0, 5).map((a, i) => ({account_id: a.account_id, label: `${a.bank} ••${a.mask}`, item_id: a.item_id,
    bank: a.bank, institution_id: a.institution_id, name: a.name, mask: a.mask, type: a.type, subtype: a.subtype,
    before: Math.round(((a.current || 0) + 971.13 / (i + 1)) * 100) / 100, after: a.current || 0}));
  const bankAt = (i) => banks[i % Math.max(banks.length, 1)] || {institution_name: 'Chase', institution_id: null};
  const spend = [['Zenith Financial Services', 465.1], ['Costco', 269.14], ['Whole Foods', 10.09, true], ['Xai', 5]].map(([m, amt, pend], i) =>
    ({transaction_id: `lay${i}`, name: m, merchant_name: m, amount: amt, pending: Boolean(pend), logo_url: null,
      bank: bankAt(i).institution_name, institution_id: bankAt(i).institution_id, account_name: 'Checking', mask: accts[i] ? accts[i].mask : null}));
  const rb = banks.map((b) => ({item_id: b.item_id, institution_name: b.institution_name, institution_id: b.institution_id, asked_ago: null, retry_after: 0}));
  const refresh = {allowed: true, billed: false, plan: 'trial', sandbox: false, banks: rb};
  const ok = banks.map((b) => ({item_id: b.item_id, institution_name: b.institution_name, institution_id: b.institution_id, ok: true,
    added: 0, modified: 0, removed: 0, error_code: null, error: null, extras: {}}));
  const none = {new: 0, updated: 0, removed: 0, balances: [], new_spend: [], new_more: {spend: 0, transfers: 0, money_in: 0}, text: 'Nothing new'};
  const base = {ok: true, synced: banks.length, banks: banks.length, message: '', unreachable: false, items: ok, waiting: [], errors: [],
    last_sync: new Date().toISOString(), previous_sync: d.last_sync || null, data_age: 'synced just now', refresh,
    changes: {new: 12, updated: 10, removed: 0, balances: moved, new_spend: spend, new_more: {spend: 0, transfers: 8, money_in: 0}, text: ''}};
  const nothing = {...base, changes: none};
  const k = banks.length > 1 ? 1 : 0;
  const bodies = {result: base, nothing, cooldown: {error: 'Synced a moment ago. Try again in 42 s.', retry_after: 42},
    unreachable: {...nothing, ok: false, synced: 0, unreachable: true, items: [], errors: ["Couldn't reach Plaid. Nothing changed; try again in a minute."]},
    relogin: {...base, ok: false, synced: banks.length - 1, errors: ["Couldn't sync a bank."],
      items: ok.map((it, i) => (i === k ? {...it, ok: false, error_code: 'ITEM_LOGIN_REQUIRED', error: 'ITEM_LOGIN_REQUIRED'} : it)),
      changes: {...base.changes, new: 11, balances: moved.slice(0, 4)}},
    'refresh-one': {...nothing, refresh: {...refresh, banks: rb.map((b, i) => (i ? b : {...b, asked_ago: 720, retry_after: 1080}))}},
    'refresh-all': {...nothing, refresh: {...refresh, banks: rb.map((b) => ({...b, asked_ago: 720, retry_after: 1080}))}}};
  const asked = {...nothing, refresh: {...refresh, banks: rb.map((b) => ({...b, asked_ago: 0, retry_after: 1800}))},
    refresh_requested: rb.map((b) => ({item_id: b.item_id, institution_name: b.institution_name, products: ['transactions'], ok: true})),
    refresh_failed: [], refresh_waiting: [], note: 'Banks asked for fresh data'};
  // HOTFIX-1: asked inside the minute after a sync (nothing synced again); and a POST whose answer never came, with the
  // look-up saying the server never got it ("Lost the connection")
  bodies.skip = nothing;
  const skipped = {...asked, synced: 0, items: [], waiting: rb.map((b) => ({item_id: b.item_id, retry_after: 21})),
    sync_skipped: {ago: 40, retry_after: 21}, changes: {...none, text: null}};
  const f = window.fetch;
  window.fetch = (u, o) => {
    if (kind === 'lost' && String(u).includes('/api/sync?')) return Promise.resolve(new Response(JSON.stringify(
      {rid: 'x', found: false, running: false}), {status: 200, headers: {'Content-Type': 'application/json'}}));
    if (!String(u).endsWith('/api/sync') || !o || o.method !== 'POST') return f(u, o);
    if (kind === 'busy') return new Promise(() => {});
    if (kind === 'lost') return Promise.reject(new TypeError('Load failed'));
    const again = JSON.parse(o.body || '{}').refresh;
    const body = again ? (kind === 'skip' ? skipped : asked) : bodies[kind];
    return Promise.resolve(new Response(JSON.stringify(body), {status: kind === 'cooldown' && !again ? 429 : 200,
      headers: {'Content-Type': 'application/json'}}));
  };
  return 1;
})())"""
KEEP = "(() => { document.getElementById('syncp').dispatchEvent(new Event('pointerenter')); return 1; })()"
# TASK-22g: Settings on the Paid plan (the plan's second line, the switch off), answered in the page like SYNC_JS: GET
# /api/settings/page comes back with the plan as Paid, then the page is drawn again; nothing is saved
PAID_JS = """(() => { const f = window.fetch;
  window.fetch = async (u, o) => { const r = await f(u, o); if (!String(u).endsWith('/api/settings/page')) return r;
    const d = await r.json(); d.plaid.refresh = {...d.plaid.refresh, plan: 'paid', allowed: false, set: false, billed: !d.plaid.refresh.sandbox};
    return new Response(JSON.stringify(d), {status: r.status, headers: {'Content-Type': 'application/json'}}); };
  location.hash = '#/settings?plan=paid'; return 1; })()"""


def sync_state(kind: str, ready: str) -> list[str]:
    # Pin the brief panel in the readiness microtask, before any settling/CDP round trip can outlast its 4s timer.
    pinned = f"{wait_for(f'#syncp:not([hidden]) {ready}', SLOW)}.then(() => {KEEP})"
    return [f"{SYNC_JS}({json.dumps(kind)})", click("#syncnow"), pinned]


# (name, page, steps, scope, when): steps are JS run in order after the page loads (each followed by a short wait);
# scope is where the checks look (page | modal | panel | drawer | txpanel | syncp); when: "all" or "phone" / "wide" only, or
# (TASK-19) "dock" / "nodock": 1400px and wider, where the transaction sheet docks beside the table, or narrower.
STATES: list[tuple[str, str, list[str], str, str]] = [
    *[(p, p, [], "page", "all") for p in PAGES],
    ("transactions-review", "transactions", [click(".fbar .fchip[data-kind=review]")], "page", "all"),
    # Prefer the capped category Planned control; nocaps reaches the same editor through its category Set control. The editor
    # is the app's modal sheet (bottom sheet on a phone, centred dialog from 1024px), so it is audited as one.
    ("budgets-editor", "budgets", [click("#page tr.c .plan, #page .nb .nbrow[data-category] .setc")], "modal", "all"),
    ("budgets-editor-error", "budgets", [click("#page tr.c .plan, #page .nb .nbrow[data-category] .setc"),
                                         "(() => { const i = document.querySelector('#sheet .bx input'); i.value = 'lots';"
                                         " i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); return 1; })()"],
     "modal", "all"),
    ("budgets-set-budget", "budgets", [click("#page .nb .setc")], "modal", "all"),
    # 2026-10-02: the bulk "See suggestions" link is GONE (design decision) — it revealed the `.sg` line that moved
    # into each no-budget row's own Details panel, so it had nothing left to reveal. This state now opens ONE row's
    # suggestion disclosure instead, which is where the suggestion text actually lives, and it asserts that panel's
    # money is whole. The "Set" control keeps its own state (`budgets-set-budget`).
    ("budgets-suggestions", "budgets", [click("#page .nb .nbrow .help-trigger")], "page", "all"),
    ("investments-by-type", "investments", [click("#page .seg button[data-k=type]")], "page", "all"),
    ("investments-all-time", "investments", [click("#page .seg button[data-r=all]")], "page", "all"),
    ("debt-cards-open", "debt", [click("#page .dg .hd[aria-expanded]")], "page", "all"),
    ("debt-sort-apr", "debt", [click("#page .sorttabs button[data-s=apr]")], "page", "all"),
    ("loan-panel", "debt", [click("#page [data-loan]"), wait_for("#panel:not([hidden]) canvas, #panel:not([hidden]) .msgbox")], "panel", "all"),
    ("paid-off-confirm", "debt", [click("#page [data-loan]"), wait_for("#panel:not([hidden]) .row"),
                                  click("#panel .btn", "Mark as paid off")], "modal", "all"),
    ("add-account", "accounts", [click("[data-add=home]")], "modal", "all"),
    ("add-plaid", "accounts", [click("[data-add=home]"), click("#sheet .opt", "Link a bank")], "modal", "all"),
    ("add-manual", "accounts", [click("[data-add=home]"), click("#sheet .opt", "Add manually")], "modal", "all"),
    *[(f"form-{f.lower().replace(' ', '-')}", "accounts", [click("[data-add=home]"), click("#sheet .opt", "Add manually"),
                                                           click("#sheet .opt", f)], "modal", "all") for f in FORMS],
    ("form-property", "accounts", [click("[data-add=home]"), click("#sheet .opt", "Add a property")], "modal", "all"),
    ("form-loan-errors", "accounts", [click("[data-add=home]"), click("#sheet .opt", "Add manually"),
                                      click("#sheet .opt", "Loan"), "document.querySelector('#sheet form').requestSubmit()"],
     "modal", "all"),
    ("edit-manual", "accounts", [click("#page [data-edit]"), wait_for("#sheet form")], "modal", "all"),
    # TASK-12 / 13b: the edit forms of a car following its Carfax trade-in, one with "Your value" (Use estimate again,
    # the mileage nudge), a property, a car whose check was blocked (nocaps), and the car form with a VIN (valid, then
    # a bad check digit)
    ("edit-vehicle", "accounts", asset("vehicle") + EDIT, "modal", "all"),
    ("edit-vehicle-your-value", "accounts", asset("vehicle", "2015 Honda Fit") + EDIT, "modal", "all"),
    ("edit-property", "accounts", asset("property") + EDIT, "modal", "all"),
    # TASK-13b: the car / property pages in their picture states (the full DB has all five)
    ("asset-car-photo", "accounts", asset("vehicle", "2019 Zenith Mesa"), "page", "all"),
    ("asset-car-silhouette", "accounts", asset("vehicle", "2015 Honda Fit"), "page", "all"),
    ("asset-property-photo", "accounts", asset("property", "Condo"), "page", "all"),
    ("asset-property-map", "accounts", asset("property", "Cabin"), "page", "all"),
    ("asset-property-no-estimate", "accounts", asset("property", "Studio"), "page", "all"),
    ("form-car-vin", "accounts", [click("[data-add=home]"), click("#sheet .opt", "Add a vehicle"), wait_for("#f-vin"),
                                  FILL.format(sel="#f-vin", v="1HGCM82633A004352"), FILL.format(sel="#f-mileage", v="120000"),
                                  FILL.format(sel="#f-zip_code", v="94110")], "modal", "all"),
    ("form-car-bad-vin", "accounts", [click("[data-add=home]"), click("#sheet .opt", "Add a vehicle"), wait_for("#f-vin"),
                                      FILL.format(sel="#f-vin", v="1HGCM82643A004352"),
                                      "document.querySelector('#sheet form').requestSubmit()", wait_for("#sheet .fld.bad")],
     "modal", "all"),
    ("archive-confirm", "accounts", [click("#page [data-edit]"), wait_for("#sheet form"), click("#sheet .btn.danger", "Archive")],
     "modal", "all"),
    ("toast", "accounts", [click("#page [data-edit]"), wait_for("#sheet form"), "document.querySelector('#sheet form').requestSubmit()",
                           wait_for("#toast.on")], "page", "all"),
    ("phone-drawer", "overview", [click("#menu")], "drawer", "phone"),
    # TASK-14 / 15: the net worth switch on Custom: the Accounts card (the quiet line, the groups left out dimmed or
    # counted in part, the "Not counted" tags, the chart on custom_net) and the Overview tile, flipped on the page; the
    # phone drawer's figure and tags in it
    ("accounts-custom", "accounts", [click("#page .nwseg button[data-nw=custom]")], "page", "all"),
    ("overview-custom", "overview", [click("#page .nwseg button[data-nw=custom]")], "page", "all"),
    ("phone-drawer-custom", "overview", [click("#page .nwseg button[data-nw=custom]"), click("#menu")], "drawer", "phone"),
    # TASK-15: the Custom net worth's Edit sheet as it opens, and with an account switched (its group mixed) and the last
    # group folded away
    ("networth-sheet", "accounts", [click("#page .nwseg button[data-nw=custom]"), wait_for("#sheet .nwlist")], "modal", "all"),
    ("networth-sheet-mixed", "accounts", [click("#page .nwseg button[data-nw=custom]"), wait_for("#sheet .nwlist"), click("#sheet .nwg .nwr .tgl"),
                                          click("#sheet .nwg:last-of-type .nwgt")], "modal", "all"),
    # TASK-18: Settings (its sheets and forms opened, nothing saved), first-run setup's six steps, importing a file
    ("settings", "settings", [], "page", "all"),
    ("rules-list", "settings", [click("#page .rules .rsum"), wait_for("#page .rules .rlist")], "page", "all"),
    ("rules-search", "settings", [click("#page .rules .rsum"), wait_for("#page .rules .rq"),
      FILL.format(sel="#page .rules .rq", v="Subscriptions")], "page", "all"),
    ("rules-suggestions", "settings", [click("#page .rules .rsum"), wait_for("#page .rules .rsugs")], "page", "all"),
    ("rules-edit", "settings", [click("#page .rules .rsum"), wait_for("#page .rules .rrow[data-rule]"),
      "(() => { const r = document.querySelector('#page .rules .rrow[data-rule]'); if (innerWidth < 1024) r.querySelector('.rmore').click(); return 1; })()",
      "(() => { const r = document.querySelector('#page .rules .rrow[data-rule]'); const b = innerWidth >= 1024 ? r.querySelector('.rib[aria-label=Edit]') : [...r.querySelectorAll('.rmenu button')].find(x => x.textContent.includes('Edit')); b.click(); return 1; })()",
      wait_for("#sheet.rsheet select")], "modal", "all"),
    ("rules-delete", "settings", [click("#page .rules .rsum"), wait_for("#page .rules .rrow[data-rule]"),
      "(() => { const r = document.querySelector('#page .rules .rrow[data-rule]'); if (innerWidth < 1024) r.querySelector('.rmore').click(); return 1; })()",
      "(() => { const r = document.querySelector('#page .rules .rrow[data-rule]'); const b = innerWidth >= 1024 ? r.querySelector('.rib[aria-label=Delete]') : [...r.querySelectorAll('.rmenu button')].find(x => x.textContent.includes('Delete')); b.click(); return 1; })()",
      wait_for("#page .rules .confirm")], "page", "all"),
    ("settings-replace-keys", "settings", [click("#page .sc .btn", "Replace keys")], "modal", "all"),
    ("settings-alpaca", "settings", [click("#page .sc .btn", "Connect")], "modal", "all"),
    ("settings-login-setup", "settings", [click("#page .choice", "Built-in login"), wait_for("#sheet .wiz")], "modal", "all"),
    ("settings-new-token", "settings", [click("#page .sc button.lnk", "New token")], "page", "all"),
    ("settings-carfax-how", "settings", [click("#page details.cfg summary", "How")], "page", "all"),
    ("settings-paid", "settings", [PAID_JS, wait_for("#page .seg [data-k=paid][aria-pressed=true]", SLOW)], "page", "all"),
    *[(f"setup-{k}", f"setup?step={k}", [], "page", "all") for k in range(1, 7)],
    ("import-drop", "accounts", [click("[data-add=home]"), click("#sheet .opt", "Import a file"), wait_for("#sheet .drop")], "modal", "all"),
    ("import-csv", "accounts", [click("[data-add=home]"), click("#sheet .opt", "Import a file"), wait_for("#sheet .drop"),
                                "IMPORT:everyday-export.csv", wait_for("#sheet table.map")], "modal", "all"),
    ("import-ofx", "accounts", [click("[data-add=home]"), click("#sheet .opt", "Import a file"), wait_for("#sheet .drop"),
                                "IMPORT:card.qfx", wait_for("#sheet .mapacct")], "modal", "all"),
    # TASK-19 (Batch C): the New goal sheet (each template; Save up following part of an account), a goal's own sheet
    # (and one logged by hand, with its entries), the transaction sheet (docked beside the table from 1400px, the usual
    # sheet below) with its category and with a split started, "Save this search", the bills calendar's next month,
    # the Alpaca Connect form and its Remove question
    *[(name, "goals", [click("#page .gsum [data-add=goal], #page [data-template=save]"), wait_for("#sheet .gform"), *more], "modal", "all")
      for name, more in (("goals-new", []), ("goals-new-part", [click("#sheet .choice", "Part of an account")]),
                         ("goals-new-log", [click("#sheet .choice", "I’ll log it myself")]),
                         ("goals-new-payoff", [click("#sheet .tseg button", "Pay off")]),
                         ("goals-new-monthly", [click("#sheet .tseg button", "Monthly savings")]))],
    ("goal-edit", "goals", [click("#page .card.goal[data-goal]"), wait_for("#sheet.gsheet .savenote")], "modal", "all"),
    ("goal-edit-entries", "goals", [click("#page .card.goal[data-goal]", "Trip"), wait_for("#sheet.gsheet .gentry")], "modal", "all"),
    *[(f"tx-sheet{sfx}", "transactions", [click(".fbar .fchip[data-kind=spend]"), wait_for("#page table.tx tbody tr[data-tid]"),
                                           click("#page table.tx tbody tr[data-tid]:not(:has(.pend)) td.num"),
                                           wait_for(f"{root} .tsec"), *more], scope, when)
      for sfx, more in (("", []), ("-split", [click("#txpanel .spl.one .btn, #sheet .spl.one .btn", "Split")]))
      for root, scope, when in (("#txpanel:not([hidden])", "txpanel", "dock"), ("#sheet.txsheet", "modal", "nodock"))],
    ("tx-save-search", "transactions", [FILL.format(sel="#page .fbar .search", v="coffee"), wait_for("#page .sfbar .sfadd", SLOW),
                                        click("#page .sfbar .sfadd")], "modal", "all"),
    ("recurring-next-month", "recurring", [wait_for("#page .calcard", SLOW), click("#page .calnav button[aria-label='Next month']")],
     "page", "all"),   # (TEST-SPEED: the next month's answer and redraw are waited for by settled(), not 400 ms)
    ("accounts-alpaca-details", "accounts", [click("#page .li.acct.r2.apirow[data-api=alpaca] .nmrow > .help-trigger"),
      wait_for("#sheet.apsheet")], "modal", "all"),
    ("accounts-alpaca-replace", "accounts", [click("#page .li.acct.r2.apirow[data-api=alpaca] .nmrow > .help-trigger"),
      click("#sheet .apictl .lnk", "Replace key")], "modal", "all"),
    ("accounts-alpaca-remove", "accounts", [click("#page .li.acct.r2.apirow[data-api=alpaca] .nmrow > .help-trigger"),
      click("#sheet .apictl .lnk", "Remove")], "modal", "all"),
    ("add-alpaca", "accounts", [click("[data-add=home]"), click("#sheet .opt", "Alpaca")], "modal", "all"),
    ("alpaca-remove", "investments", [click("#page .apirow .apictl .lnk", "Remove")], "modal", "all"),
    # TASK-21: bills whose amount changes and any schedule: a Recurring row with "~$ est." and its usual range, the bill
    # sheet it opens, the bills calendar's every-2-months chip (a phone's agenda row), and Track as a bill from a
    # transaction (as it opens, and with "Changes each time" and a usual amount typed: the preview line under it)
    ("recurring-bill-est", "recurring", [wait_for("#page .calcard", SLOW), wait_for("#page .li[data-est]")], "page", "all"),
    ("bill-sheet", "recurring", [wait_for("#page .calcard", SLOW), click("#page .li[data-est]"), wait_for("#sheet.billsheet .tsec", SLOW)],
     "modal", "all"),
    ("recurring-every-2-months", "recurring", [wait_for("#page .calcard", SLOW), BIMONTHLY_JS], "page", "all"),
    # TASK-22f: Sync now's header while it runs, and its panel's every state (MOCK-022)
    ("sync-busy", "overview", [f"{SYNC_JS}('busy')", click("#syncnow"), wait_for("#syncnow:disabled")], "page", "all"),
    ("sync-result", "overview", sync_state("result", ".spcols"), "syncp", "all"),
    ("sync-nothing", "overview", sync_state("nothing", ".spask"), "syncp", "all"),
    ("sync-asked", "overview", [*sync_state("nothing", ".spask"), click("#syncp .spask"),
                                wait_for("#syncp:not([hidden]) .spwait", SLOW)], "syncp", "all"),
    ("sync-asked-skip", "overview", [*sync_state("skip", ".spask"), click("#syncp .spask"),   # HOTFIX-1
                                     wait_for("#syncp:not([hidden]) .spwait", SLOW)], "syncp", "all"),
    ("sync-lost", "overview", sync_state("lost", ".spic.warn"), "syncp", "all"),                  # HOTFIX-1
    ("sync-cooldown", "overview", sync_state("cooldown", ".sphd.one"), "syncp", "all"),
    ("sync-unreachable", "overview", sync_state("unreachable", ".spic.bad"), "syncp", "all"),
    ("sync-relogin", "overview", sync_state("relogin", ".sprow.relog"), "syncp", "all"),
    ("sync-refresh-one", "overview", sync_state("refresh-one", ".spwait"), "syncp", "all"),
    ("sync-refresh-all", "overview", sync_state("refresh-all", ".spask:disabled"), "syncp", "all"),
    *[(f"track-sheet{sfx}", "transactions", [click(".fbar .fchip[data-kind=spend]"), wait_for("#page table.tx tbody tr[data-tid]"),
                                            click("#page table.tx tbody tr[data-tid]:not(:has(.pend)) td.num"),
                                            wait_for(TXBILL, SLOW), click(TXBILL),
                                            wait_for("#sheet.billsheet .trackf", SLOW), *more], "modal", "all")
      for sfx, more in (("", [wait_for("#sheet .bprev svg", SLOW)]),
                        ("-varies", [click("#sheet .bseg button", "Changes each time"), FILL.format(sel="#bt-usual", v="100-150"),
                                     wait_for("#sheet .bprev svg", SLOW)]))],   # (TEST-SPEED: the preview's 200 ms debounce,
                                                                                  # its answer and redraw: settled(), not 600 ms)
]
# TASK-23: the Ask panel (static/chat.js) closed (its button on the Overview), open, with an answer (source line, budget
# ring) and a "Suggested change" card, and that card approved. POST /api/chat and /api/chat/decide are answered in the
# page (window.fetch, like SYNC_JS) from this DB's own budgets, so no brain is needed and nothing is saved. A DB with AI
# off has no Ask button: those states SKIP there. Also the panel's two failure states: a turn that failed (its one
# friendly line, kind 'error') and a card whose Approve didn't go through (still pending: the reason above live Approve /
# Deny, kind 'retry'); error answers in the page too, so the browser logs no failed request.
CHAT_JS = r"""((kind) => (async () => {
  const d = await (await fetch('/api/budgets')).json();
  const capped = d.categories.filter((c) => c.cap != null);
  const c = capped.find((x) => x.status === 'over') || capped[0] || {category: 'Other Shopping', spend: 709.24, cap: 200, due: 0, count: 24, status: 'over'};
  const fmt = (x) => `$${Number(x).toFixed(2)}`;
  const newCap = Math.ceil((c.spend + 50) / 50) * 50;
  const card = {id: 9001, tool: 'finance_set_cap', title: `Raise ${c.category} cap to $${newCap}?`,
    summary: `Set the monthly budget cap on ${c.category} to ${fmt(newCap)} (now ${fmt(c.cap)}).`, status: 'pending', result_line: null, client: 'Ask'};
  const reply = {reply: `You've spent ${fmt(c.spend)} on ${c.category} this month. The cap is ${fmt(c.cap)}, so you're ${fmt(Math.abs(c.spend - c.cap))} ${c.spend > c.cap ? 'over' : 'under'}.\n\nMost of it went on a few larger orders. Raising the cap to $${newCap} would cover a month like this one.`,
    sources: [`from budget-status · ${d.month}`], stats: [{category: c.category, cap: c.cap, spent: c.spend, due: c.due || 0, count: c.count, status: c.status}],
    cards: [card], brain: document.body.dataset.askBrain};
  const f = window.fetch;
  const ans = (body, status = 200) => Promise.resolve(new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}}));
  window.fetch = (u, o) => {
    const url = String(u);
    if (url.endsWith('/api/chat') && o && o.method === 'POST') return kind === 'error'
      ? ans({error: "Couldn't reach the AI provider. Is it running, and is the address in Settings right?"}, 502) : ans(reply);
    if (url.endsWith('/api/chat/decide') && o && o.method === 'POST') return kind === 'retry' ? ans({error: 'internal error (OperationalError)'}, 500)
      : ans({card: {...card, status: 'applied', result_line: `Saved: ${c.category} cap is now $${newCap}`}});
    if (url.includes('/api/chat/cards')) return ans({cards: kind === 'retry' ? [card] : []});
    return f(u, o);
  };
  sessionStorage.removeItem('hf.chat.v1');
  return 1;
})())"""
CHAT_ASK = ("(() => { const i = document.querySelector('#chat .chin'); i.value = 'Why am I over on Shopping?';"
            " i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); return 1; })()")
STATES += [
    ("chat-closed", "overview", [f"{CHAT_JS}('closed')", wait_for("#askb")], "page", "all"),
    ("chat-open", "overview", [f"{CHAT_JS}('open')", click("#askb"), wait_for("#chat:not([hidden]) .chin")], "chat", "all"),
    ("chat-answer", "overview", [f"{CHAT_JS}('answer')", click("#askb"), wait_for("#chat:not([hidden]) .chin"), CHAT_ASK,
                                 wait_for("#chat .appr[data-status=pending]", SLOW)], "chat", "all"),
    ("chat-approved", "overview", [f"{CHAT_JS}('approved')", click("#askb"), wait_for("#chat:not([hidden]) .chin"), CHAT_ASK,
                                   wait_for("#chat .appr[data-status=pending]", SLOW), click("#chat .appr .btn", "Approve"),
                                   wait_for("#chat .appr[data-status=applied]", SLOW)], "chat", "all"),
    ("chat-error", "overview", [f"{CHAT_JS}('error')", click("#askb"), wait_for("#chat:not([hidden]) .chin"), CHAT_ASK,
                                wait_for("#chat .msg.bot.err", SLOW)], "chat", "all"),
    ("chat-retry", "overview", [f"{CHAT_JS}('retry')", click("#askb"), wait_for("#chat:not([hidden]) .chin"), CHAT_ASK,
                                wait_for("#chat .appr[data-status=pending]", SLOW), click("#chat .appr .btn", "Approve"),
                                wait_for("#chat .appr[data-status=pending] .apres.bad", SLOW)], "chat", "all"),
]
# TASK-21: these run at every width (the five pages' and the three phones') in both themes, with the tap check on phones
EVERY_WIDTH = {"recurring-bill-est", "bill-sheet", "recurring-every-2-months", "track-sheet", "track-sheet-varies",
               "sync-busy", "sync-result", "sync-relogin", "sync-refresh-one",   # (TASK-22f: the sync header and panel)
               "settings-paid",                                                  # (TASK-22g: the Paid plan's line)
               "chat-closed", "chat-open", "chat-answer", "chat-approved",       # (TASK-23: the Ask panel,
               "chat-error", "chat-retry",                                       #  and its two failure states)
               "rules-list", "rules-search", "rules-edit", "rules-delete", "rules-suggestions"}   # (TASK-24: the rules
                                                                                 #  page and its sheets)
# the pages every width x theme sees (the rest: CORE_WIDTHS, dark)
EVERY = {*PAGES, "settings", *(f"setup-{k}" for k in range(1, 7)), "import-drop", "import-csv", "import-ofx"}

# TASK-18: the built-in login's own page, on the gates' two sign-in DBs (tests/gate_dbs.py signin / signin-setup): the
# sign-in (password, then the code or a recovery code) and the first-time set-up's three steps (the code is worked out
# in the page from the key it shows: HMAC-SHA1 through WebCrypto). Nothing is saved: the set-up stops before its last step.
TOTP_JS = """(async () => { const key = document.querySelector('#page .qk').textContent.replace(/\\s+/g, '');
  const alpha = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'; let bits = ''; for (const c of key) bits += alpha.indexOf(c).toString(2).padStart(5, '0');
  const bytes = new Uint8Array(Math.floor(bits.length / 8)).map((_, i) => parseInt(bits.slice(i * 8, i * 8 + 8), 2));
  const step = Math.floor(Date.now() / 30000); const msg = new Uint8Array(8); let x = step;
  for (let i = 7; i >= 0; i--) { msg[i] = x & 255; x = Math.floor(x / 256); }
  const k = await crypto.subtle.importKey('raw', bytes, {name: 'HMAC', hash: 'SHA-1'}, false, ['sign']);
  const d = new Uint8Array(await crypto.subtle.sign('HMAC', k, msg)); const o = d[19] & 15;
  const v = (((d[o] & 127) << 24) | (d[o + 1] << 16) | (d[o + 2] << 8) | d[o + 3]) % 1000000;
  const code = String(v).padStart(6, '0'); const boxes = [...document.querySelectorAll('#page .codeb input')];
  boxes.forEach((b, i) => { b.value = code[i]; }); boxes[5].dispatchEvent(new Event('input', {bubbles: true})); return code; })()"""
from gate_dbs import SIGNIN_PASSWORD, SIGNIN_USER  # noqa: E402  (the sign-in DB's login)
SIGNIN_STATES: list[tuple[str, str, list[str], str, str]] = [
    ("signin", "", [], "page", "all"),
    ("signin-code", "", [FILL.format(sel="#page input[autocomplete=username]", v=SIGNIN_USER),
                         FILL.format(sel="#page input[type=password]", v=SIGNIN_PASSWORD),
                         "document.querySelector('#page form').requestSubmit()", wait_for("#page .codeb")], "page", "all"),
    ("signin-recovery", "", [FILL.format(sel="#page input[autocomplete=username]", v=SIGNIN_USER),
                             FILL.format(sel="#page input[type=password]", v=SIGNIN_PASSWORD),
                             "document.querySelector('#page form').requestSubmit()", wait_for("#page .codeb"),
                             click("#page button.lnk", "Use a recovery code"), wait_for("#page input[placeholder='xxxx-xxxx']")], "page", "all"),
]
SETUP_STATES: list[tuple[str, str, list[str], str, str]] = [
    ("login-setup-1", "", [wait_for("#page .pws")], "page", "all"),
    ("login-setup-2", "", [FILL.format(sel="#page input[autocomplete=username]", v=SIGNIN_USER),
                           FILL.format(sel="#page input[autocomplete=new-password]", v=SIGNIN_PASSWORD),
                           f"(() => {{ const i = document.querySelectorAll('#page input[autocomplete=new-password]')[1]; i.value = {json.dumps(SIGNIN_PASSWORD)}; return 1; }})()",
                           "document.querySelector('#page form').requestSubmit()", wait_for("#page img.qr[src^='data:']")], "page", "all"),
    ("login-setup-3", "", [FILL.format(sel="#page input[autocomplete=username]", v=SIGNIN_USER),
                           FILL.format(sel="#page input[autocomplete=new-password]", v=SIGNIN_PASSWORD),
                           f"(() => {{ const i = document.querySelectorAll('#page input[autocomplete=new-password]')[1]; i.value = {json.dumps(SIGNIN_PASSWORD)}; return 1; }})()",
                           "document.querySelector('#page form').requestSubmit()", wait_for("#page .codeb"), TOTP_JS,
                           wait_for("#page .rcodes")], "page", "all"),
]
AUTH_STATES = {"signin": SIGNIN_STATES, "signin-setup": SETUP_STATES}
EVERY |= {name for states in AUTH_STATES.values() for name, *_ in states}

CHECK_JS = r"""((scope) => {
const V = [];
const seen = {clipping: 0, alignment: 0, spacing: 0, charts: 0, text: 0, overlays: 0, segments: 0, empty: 0, overflow: 0,
  escape: 0, cliptext: 0, wrapped: 0, rows: 0, orphans: 0, tap: 0, 'tap-covered': 0, darkbg: 0, headers: 0, rowname: 0, cutnum: 0};
const R = (el) => { const r = el.getBoundingClientRect(); return [Math.round(r.left + scrollX), Math.round(r.top + scrollY), Math.round(r.width), Math.round(r.height)]; };
const who = (el) => {
  const t = (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 50);
  const cls = typeof el.className === 'string' ? el.className : (el.className && el.className.baseVal) || '';
  const card = el.closest('.card, .sheet, .panel, .syncp, aside');
  const ct = card ? ((card.querySelector('h3 .ttl, .hd b, h2') || {}).textContent || card.className).trim().slice(0, 30) : '';
  return `${el.tagName.toLowerCase()}${cls ? '.' + cls.trim().split(/\s+/).join('.') : ''} "${t}"${ct ? ' in ' + ct : ''}`;
};
const fail = (check, rule, els, detail) => V.push({check, rule, what: els.map(who), rects: els.map(R), detail});
const roots = {page: [document.querySelector('main'), document.getElementById('side'), document.getElementById('tabbar')],
  modal: [document.getElementById('sheet')], panel: [document.getElementById('panel')], drawer: [document.getElementById('side')],
  syncp: [document.getElementById('syncp')],
  chat: [document.getElementById('chat')],   // TASK-23: the Ask panel
  txpanel: [document.getElementById('txpanel'), document.querySelector('main')]}[scope]
  .filter(Boolean);
const shown = (el) => {
  if (!el.getClientRects().length) return false;
  const cs = getComputedStyle(el);
  if (cs.visibility !== 'visible' || cs.display === 'none') return false;
  for (let e = el; e; e = e.parentElement) if (Number(getComputedStyle(e).opacity) === 0) return false;
  const r = el.getBoundingClientRect();
  return r.width >= 1 && r.height >= 1;
};
const all = [];
for (const root of roots) if (shown(root)) for (const el of [root, ...root.querySelectorAll('*')]) if (!el.closest('svg, canvas, template, [hidden]') || el.tagName === 'svg') if (shown(el)) all.push(el);
const phone = innerWidth < 1024;   // TASK-18: the one-column layout (drawer + tab bar) up to 1024px
const MONEY = /\$\s?[0-9]/;
const ownText = (el) => [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join('').trim();

// ---------- clipping ----------
// Shortened on purpose with an ellipsis (names, titles of rows, labels): allowed while they hold no money figure.
// (.top h1.t1: a car / property page's title in the top bar; on a phone a long name ends in an ellipsis on purpose,
// the whole name being under the picture)
const NAMEY = '.grow > b, .grow > span, .nm, .ac > span:first-child, .tx .mt b .t, .tlc .tm b, .cashl .nm, .only-m, .bank .nm, .top h1.t1, .bchip .bct b, .tx .splt, .li.r2 .meta2 > .mt, .spl.one button.catb .catn';
// (TASK-19: .bchip .bct b, a merchant's name on a bills calendar chip: the whole name is its title and the day's label;
// .tx .splt, a split row's categories, "Split · Groceries + Hou…" as the approved design draws it, all of them in its title;
// .li.r2 .meta2 > .mt, the words that end an Accounts row's details line, "Estimated value · updated 9 da…": approved
// density mock D1 B draws one details line that may end in an ellipsis, the whole line in its title and the row's
// panel. Its figures (a payment on its way, "27% of $4,000", "81% paid off") sit before it, never shortened, and money
// in it would still fail here.)
// (.spl.one button.catb .catn, the transaction sheet's category button: on a 390 phone the badge + button + Split stay
// on one row and the 3 longest labels, "Transport · Car Maintenance", end in an ellipsis on purpose (the owner, 2026-10-23:
// truncate, don't wrap or restack the row). The whole name is in the button's aria-label and in the picker it opens.)
for (const el of all) {
  const cs = getComputedStyle(el);
  const hid = (v) => v === 'hidden' || v === 'clip';
  if (!hid(cs.overflowX) && !hid(cs.overflowY)) continue;
  seen.clipping += 1;
  const overX = hid(cs.overflowX) && el.scrollWidth > el.clientWidth + 1;
  const overY = hid(cs.overflowY) && el.scrollHeight > el.clientHeight + 1;
  if (!overX && !overY) continue;
  const text = (el.textContent || '').trim();
  const ell = cs.textOverflow === 'ellipsis';
  if (ell && !overY && el.matches(NAMEY) && !MONEY.test(text)) continue;   // a name, shortened on purpose
  // A text field longer than its box scrolls inside it (the browser's own behaviour, all of it reachable by the caret),
  // e.g. NHTSA's trim "CMT2L-AHXEBA / CMT2L-AHXGBA" (seen on the real DB 2026-09-27). Numbers / amounts stay checked.
  if (el.tagName === 'INPUT' && !overY && !/^[\s$0-9.,-]*$/.test(el.value || '')) continue;
  fail('clipping', ell ? (MONEY.test(text) ? 'money cut off by an ellipsis' : 'an ellipsis where nothing should be shortened')
    : 'content bigger than its overflow:hidden box', [el], {scrollWidth: el.scrollWidth, clientWidth: el.clientWidth,
    scrollHeight: el.scrollHeight, clientHeight: el.clientHeight});
}
// (TASK-14b) the text in a ring stays in its hole: each line's glyph box (a canvas measure in the element's own font and
// letter-spacing, placed on its baseline) inside the arc's inner edge. A ring isn't overflow:hidden, so nothing above
// sees "with bills" running into the arc of the 70px Overview ring.
const inkCtx = document.createElement('canvas').getContext('2d');
for (const rg of all.filter((e) => e.matches('.ring'))) {
  const svg = rg.querySelector(':scope > svg');
  const track = svg && svg.querySelector(':scope > circle');
  if (!track) continue;
  const R = rg.getBoundingClientRect(), cx = (R.left + R.right) / 2, cy = (R.top + R.bottom) / 2;
  const scale = R.width / (svg.viewBox.baseVal.width || R.width);
  const hole = (Number(track.getAttribute('r')) - Number(track.getAttribute('stroke-width')) / 2) * scale;
  for (const el of all.filter((e) => rg.contains(e) && e !== rg && e.childNodes.length && [...e.childNodes].every((n) => n.nodeType === 3))) {
    const t = el.textContent;
    if (!t.trim()) continue;
    seen.clipping += 1;
    const cs = getComputedStyle(el);
    inkCtx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    inkCtx.letterSpacing = cs.letterSpacing === 'normal' ? '0px' : cs.letterSpacing;
    const m = inkCtx.measureText(t);
    const range = document.createRange(); range.selectNodeContents(el);
    const lines = [...range.getClientRects()].filter((q) => q.width > 0);
    if (lines.length !== 1) { fail('clipping', "a ring's text wraps", [el, rg], {lines: lines.length, text: t}); continue; }
    const q = lines[0], base = q.bottom - m.fontBoundingBoxDescent;
    const xs = [q.left - m.actualBoundingBoxLeft, q.left + m.actualBoundingBoxRight];
    const ys = [base - m.actualBoundingBoxAscent, base + m.actualBoundingBoxDescent];
    const far = Math.max(...xs.flatMap((x) => ys.map((y) => Math.hypot(x - cx, y - cy))));
    if (far > hole + 0.5) fail('clipping', "a ring's text runs past its hole into the arc", [el, rg],
      {text: t, glyphs_reach: +far.toFixed(2), hole_radius: +hole.toFixed(2), font: cs.fontSize});
  }
}

// ---------- alignment ----------
const cardsOf = (grid) => [...grid.children].filter(shown);
const box = (el) => el.getBoundingClientRect();
for (const grid of document.querySelectorAll('#page .grid, #page .tiles')) {
  if (scope !== 'page' || !shown(grid)) continue;
  const cards = cardsOf(grid);
  for (let i = 0; i < cards.length; i++) for (let j = i + 1; j < cards.length; j++) {
    const A = box(cards[i]), B = box(cards[j]);
    const side = Math.min(A.right, B.right) - Math.max(A.left, B.left) < 1;   // side by side: no shared columns
    seen.alignment += 1;
    const d = Math.abs(A.top - B.top);
    if (side && d > 1 && d <= 12) fail('alignment', 'cards side by side with tops 1-12 px apart', [cards[i], cards[j]], {dy: +d.toFixed(1)});
  }
}
// a row's avatar / icon on the middle of its text block
const ROWS = '.li, .lgr, .crow, .brow, .bank .link, .conn, .opt, .ghost-row, .grp, .dl .nmc, .al, .matchbox, .prev, .note, .empty, .emptyov';
const ICON = '.av, .ia, .cb, .ck, .cal, .tick, .sw, .ring, .ctog';
for (const row of all.filter((e) => e.matches(ROWS))) {
  const kids = [...row.children].filter(shown);
  const icon = kids.find((k) => k.matches(ICON) || (k.matches('.nmc, a.crowl') ? false : k.querySelector(':scope > ' + ICON.split(', ').join(', :scope > '))));
  const grow = row.querySelector(':scope > .grow, :scope > a > .grow, :scope > .crowl > .grow, :scope > span:not(.ck):not(.cb):not(.av):not(.ia)');
  if (!icon || !grow || !shown(grow)) continue;
  const I = box(icon.matches(ICON) ? icon : icon.querySelector(ICON)), G = box(grow);
  if (I.bottom < G.top || I.top > G.bottom) continue;  // on another line (a wrapped row): not beside it
  seen.alignment += 1;
  // a list row centres its icon on its text block; a callout (align-items: flex-start) on the text's first line
  let mid = (G.top + G.bottom) / 2, what = 'text';
  if (getComputedStyle(row).alignItems === 'flex-start') {
    const rg = document.createRange(); rg.selectNodeContents(grow);
    const line = [...rg.getClientRects()].find((q) => q.height > 0);
    if (line) { mid = (line.top + line.bottom) / 2; what = 'first line'; }
  }
  const dy = (I.top + I.bottom) / 2 - mid;
  if (Math.abs(dy) > 2) fail('alignment', `a row's icon is off the middle of its ${what}`, [icon, grow], {dy: +dy.toFixed(1)});
}
// right-aligned amounts in one list share one right edge
const lists = new Map();
for (const el of all.filter((e) => e.matches('.amt, .bal, .gtot') && MONEY.test(e.textContent))) {
  const row = el.closest('.li, .lgr, .crow, .dl, .grp, .tlc');
  if (!row || !row.parentElement || row.closest('.txgrid, .tlw')) continue;
  // only the row's right-most figure, and only rows whose last thing is that figure (or its .rt column)
  const last = [...row.querySelectorAll('*')].filter(shown).reduce((a, e) => (box(e).right > box(a).right + 0.5 ? e : a), el);
  if (last !== el && !last.contains(el) && !el.contains(last) && !(el.closest('.rt') && el.closest('.rt').contains(last))) continue;
  const key = row.parentElement;
  if (!lists.has(key)) lists.set(key, []);
  lists.get(key).push(el);
}
for (const [, els] of lists) {
  if (els.length < 2) continue;
  seen.alignment += els.length;
  const edges = els.map((e) => box(e).right);
  const mx = Math.max(...edges), mn = Math.min(...edges);
  if (mx - mn > 1) fail('alignment', 'right-aligned amounts of one list not on one edge', [els[edges.indexOf(mn)], els[edges.indexOf(mx)]], {edges: edges.map((x) => +x.toFixed(1))});
}
// badge glyphs centred in their tiles
const ink = (svg) => { let l = 1e9, t = 1e9, r = -1e9, b = -1e9;
  for (const s of svg.querySelectorAll('path, circle, rect, line, polyline, polygon, ellipse')) {
    const q = s.getBoundingClientRect(); if (!q.width && !q.height) continue;
    l = Math.min(l, q.left); t = Math.min(t, q.top); r = Math.max(r, q.right); b = Math.max(b, q.bottom); }
  return {l, t, r, b}; };
for (const tile of all.filter((e) => e.matches('.cb, h3 .hi, .ck, .tick'))) {
  const svg = tile.querySelector(':scope > svg');
  if (!svg) continue;
  seen.alignment += 1;
  const T = box(tile), I = ink(svg);
  const dx = (I.l + I.r) / 2 - (T.left + T.right) / 2, dy = (I.t + I.b) / 2 - (T.top + T.bottom) / 2;
  if (Math.abs(dx) > 1 || Math.abs(dy) > 1) fail('alignment', "a badge glyph is off its tile's centre", [tile], {dx: +dx.toFixed(2), dy: +dy.toFixed(2)});
}

// ---------- spacing ----------
// the same kind of card has the same padding (and its title sits on it)
const kinds = new Map();
for (const c of all.filter((e) => e.matches('.card'))) {
  const k = [...c.classList].filter((x) => !['fill', 's2', 's3', 'grow', 'tile'].includes(x)).sort().join('.') + (c.classList.contains('tile') ? '.tile' : '');
  if (!kinds.has(k)) kinds.set(k, []);
  kinds.get(k).push(c);
}
for (const [k, cs] of kinds) {
  const pads = cs.map((c) => { const s = getComputedStyle(c); return [s.paddingTop, s.paddingRight, s.paddingBottom, s.paddingLeft].map(parseFloat); });
  seen.spacing += cs.length;
  for (let i = 1; i < cs.length; i++) if (pads[i].some((p, j) => Math.abs(p - pads[0][j]) > 1))
    fail('spacing', `two ${k || 'plain'} cards with different padding`, [cs[0], cs[i]], {a: pads[0], b: pads[i]});
  for (const c of cs) {
    const h = c.querySelector(':scope > h3, :scope > .hd, :scope > .hdrow');
    if (!h) continue;
    const s = getComputedStyle(c);
    const inset = box(h).left - box(c).left - parseFloat(s.borderLeftWidth);
    if (Math.abs(inset - parseFloat(s.paddingLeft)) > 1) fail('spacing', "a card's title off its padding", [c, h], {inset: +inset.toFixed(1), padding: parseFloat(s.paddingLeft)});
  }
}
// gaps between cards: the grid's column gap across, and down a column
for (const grid of document.querySelectorAll('#page .grid, #page .tiles, #page .stack')) {
  if (scope !== 'page' || !shown(grid)) continue;
  const cs = getComputedStyle(grid);
  const gap = parseFloat(cs.columnGap) || parseFloat(cs.rowGap) || 0;
  const cards = cardsOf(grid).map((el) => [el, box(el)]);
  // the grid's column tracks (their x centres): a card is checked down every column it spans, so an empty column
  // under a wide card (a stack pinned beside it on a narrower grid) is a gap too
  const gb = grid.getBoundingClientRect();
  const centres = [];
  if (grid.matches('.grid, .tiles')) {
    let x = gb.left + parseFloat(cs.paddingLeft || 0);
    for (const w of cs.gridTemplateColumns.split(' ').map(parseFloat).filter((v) => v > 0)) { centres.push(x + w / 2); x += w + gap; }
  }
  for (const [a, A] of cards) {
    let right = null;
    for (const [b, B] of cards) {
      if (b !== a && Math.min(A.bottom, B.bottom) - Math.max(A.top, B.top) > 1 && B.left >= A.right - 1 && (!right || B.left < right[1].left)) right = [b, B];
    }
    if (right) {
      seen.spacing += 1;
      const g = right[1].left - A.right;
      if (Math.abs(g - gap) > 1) fail('spacing', `gap between cards (across) is not the grid's ${gap}px`, [a, right[0]], {gap: +g.toFixed(1), want: gap});
    }
    const cols = centres.filter((cx) => cx > A.left && cx < A.right);
    const done = new Set();
    for (const cx of cols.length ? cols : [(A.left + A.right) / 2]) {
      let below = null;
      for (const [b, B] of cards) if (b !== a && B.left < cx && B.right > cx && B.top >= A.bottom - 1 && (!below || B.top < below[1].top)) below = [b, B];
      if (!below) {
        // Nothing under it in this column. A ragged bottom edge is masonry; but a card that starts after this one
        // ends, in another column, leaves this column empty beside it: a hole in the flow (a stack pinned beside a
        // full-width card, a lone card on its row).
        const later = cards.find(([c, C]) => c !== a && C.top >= A.bottom - 1);
        seen.spacing += 1;
        // A plain uniform grid ([data-uniform]: equal cards, never packed) ends its last row short by design; the
        // hole rule is for packed grids only (every other spacing rule still applies to it).
        if (grid.matches('.grid') && !grid.matches('[data-uniform]') && later)
          fail('spacing', 'an empty column beside a card that starts after this one ends (a hole)', [a, later[0]], {column_x: Math.round(cx)});
        continue;
      }
      if (done.has(below[0])) continue;
      done.add(below[0]);
      seen.spacing += 1;
      const g = below[1].top - A.bottom;
      if (Math.abs(g - gap) > 1) fail('spacing', `gap between cards (down) is not the grid's ${gap}px`, [a, below[0]], {gap: +g.toFixed(1), want: gap, column_x: Math.round(cx)});
    }
  }
}
// no two in-flow siblings overlap
const flowy = (e) => { const s = getComputedStyle(e); return !['absolute', 'fixed'].includes(s.position) && s.display !== 'inline' && s.display !== 'contents' && s.float === 'none'; };
for (const el of all) {
  const kids = [...el.children].filter((k) => shown(k) && flowy(k) && !k.matches('svg, canvas, br'));
  if (kids.length < 2 || el.matches('svg, .ring, .rin')) continue;
  const bx = kids.map(box);
  for (let i = 0; i < kids.length; i++) for (let j = i + 1; j < kids.length; j++) {
    seen.spacing += 1;
    const x = Math.min(bx[i].right, bx[j].right) - Math.max(bx[i].left, bx[j].left);
    const y = Math.min(bx[i].bottom, bx[j].bottom) - Math.max(bx[i].top, bx[j].top);
    if (x > 0.5 && y > 0.5) fail('spacing', 'two siblings overlap', [kids[i], kids[j]], {x: +x.toFixed(1), y: +y.toFixed(1)});
  }
}

// ---------- charts ----------
for (const cv of [...document.querySelectorAll('canvas')].filter((c) => roots.some((r) => r.contains(c)) && c.getClientRects().length)) {
  const ch = window.Chart && Chart.getChart(cv);
  const wrap = cv.parentElement, card = cv.closest('.card, .panel');
  seen.charts += 1;
  const C = box(cv), W = box(wrap);
  if (C.left < W.left - 1 || C.right > W.right + 1 || C.top < W.top - 1 || C.bottom > W.bottom + 1)
    fail('charts', 'a canvas outside its box', [cv, wrap], {canvas: [C.left, C.top, C.width, C.height].map(Math.round), box: [W.left, W.top, W.width, W.height].map(Math.round)});
  if (card) {
    const K = box(card), s = getComputedStyle(card);
    if (C.right > K.right - parseFloat(s.paddingRight) + 1 || C.left < K.left + parseFloat(s.paddingLeft) - 1)
      fail('charts', 'a canvas past its card\'s padding', [cv, card], {});
    const leg = [...card.querySelectorAll('.lgr, .crow, .leg')].filter(shown).pop();
    if (leg && box(leg).bottom > K.bottom - parseFloat(s.paddingBottom) - parseFloat(s.borderBottomWidth) + 1)
      fail('charts', 'the legend runs past its card', [leg, card], {legend_bottom: Math.round(box(leg).bottom), card_inner_bottom: Math.round(K.bottom - parseFloat(s.paddingBottom))});
  }
  if (ch && ch.config.type === 'doughnut') {
    const ds = ch.data.datasets[0];
    const nz = ds.data.filter((v) => v > 0).length;
    if (nz === 1) {
      const arc = ch.getDatasetMeta(0).data.find((a) => a.circumference > 0);
      const r = (arc.innerRadius + arc.outerRadius) / 2, dpr = ch.currentDevicePixelRatio;
      const px = [-1, 0, 1].map((dx) => [...cv.getContext('2d').getImageData(Math.round((arc.x + dx) * dpr), Math.round((arc.y - r) * dpr), 1, 1).data]);
      const want = ds.backgroundColor[ds.data.findIndex((v) => v > 0)];
      const rgb = [1, 3, 5].map((i) => parseInt(want.slice(i, i + 2), 16));
      const notch = ds.borderWidth !== 0 || px.some((p) => p[3] < 250 || p.slice(0, 3).some((v, i) => Math.abs(v - rgb[i]) > 24));
      if (notch) fail('charts', 'a one-slice doughnut with a notch at 12 o\'clock', [cv], {borderWidth: ds.borderWidth, top: px});
    }
  }
}

// ---------- text ----------
const rgba = (s) => { const m = String(s).match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(/[\s,\/]+/).filter(Boolean).map(Number);
  return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
const over = (top, under) => under.map((v, i) => v * (1 - top[3]) + top[i] * top[3]);
// The backgrounds behind an element, composited from the page up; a gradient counts as its lightest stop.
const backdrop = (el) => {
  const layers = [];
  for (let e = el; e; e = e.parentElement) {
    const s = getComputedStyle(e);
    let c = rgba(s.backgroundColor);
    if ((!c || c[3] === 0) && s.backgroundImage.includes('gradient')) {
      const stops = (s.backgroundImage.match(/rgba?\([^)]+\)/g) || []).map(rgba);
      if (stops.length) c = stops.reduce((a, b) => (lum(b) * b[3] > lum(a) * a[3] ? b : a));
    }
    if (c && c[3] > 0) { layers.push(c); if (c[3] >= 1) break; }
  }
  let out = [11, 13, 18];  // --bg
  for (const c of layers.reverse()) out = over(c, out);
  return out;
};
for (const el of all) {
  const t = ownText(el);
  if (!t || el.closest('button:disabled, [aria-disabled=true], .skel')) continue;
  const s = getComputedStyle(el);
  const size = parseFloat(s.fontSize);
  seen.text += 1;
  if (size < 13 - 0.01) fail('text', 'text under 13px', [el], {fontSize: size, text: t.slice(0, 40)});   // TASK-18: 13px
  let fg = rgba(s.color);
  let alpha = fg[3];
  for (let e = el; e; e = e.parentElement) alpha *= Number(getComputedStyle(e).opacity);
  const bg = backdrop(el);
  const c = over([fg[0], fg[1], fg[2], alpha], bg);
  const large = size >= 24 || (size >= 18.66 && Number(s.fontWeight) >= 700);
  const r = ratio(c, bg);
  if (r < (large ? 3 : 4.5) - 0.005) fail('text', `contrast under ${large ? '3' : '4.5'}:1`, [el], {ratio: +r.toFixed(2), color: s.color, text: t.slice(0, 40)});
}
const tabbarEl = document.getElementById('tabbar');
const tabShown = Boolean(tabbarEl) && getComputedStyle(tabbarEl).display !== 'none';  // not on a board page (first-run setup)
if (phone && scope === 'page' && tabShown) {
  // at the very bottom of the page, the last thing on it clears the tab bar
  const bar = document.getElementById('tabbar').getBoundingClientRect();
  const page = document.getElementById('page');
  const kids = [...page.querySelectorAll(':scope > *')].filter(shown);
  const last = kids[kids.length - 1];
  if (last) {
    const bottom = last.getBoundingClientRect().bottom + scrollY - (document.documentElement.scrollHeight - innerHeight);  // at the end of the scroll
    seen.text += 1;
    if (bottom > bar.top + 0.5) fail('text', 'the end of the page stays behind the phone tab bar', [last], {bottom: Math.round(bottom), tabbar_top: Math.round(bar.top)});
  }
}

// ---------- darkbg (HOTFIX-1): in the light theme, no field or surface painted dark ----------
// A select, text field or textarea (and the wrappers that draw a field: .inp, .edin, .seg, .chin, .choice), and every
// surface the light theme draws light (a card, sheet, panel, the sync panel, a toast, the sidebar / drawer, the tab bar, a
// dialog, menu or list box): its fill, composited over what's behind it, must be light, and no gradient layer with a dark
// colour may paint a fifth of its box or more. Settings -> Weekly summary's day select was slate with two white triangles
// on the owner's phone: its fill was light, but a `background` shorthand in light.css had reset the arrow's
// background-size, so the arrow's two gradients covered the whole select. And any other box with a near-black fill or
// gradient of its own (a car's page had #171c28 chips and #11151e notes on white). (Buttons are left out on purpose: the
// light theme's primary buttons and pressed chips are black with white text; their contrast is the text check's.)
if (document.documentElement.dataset.theme === 'light') {
  const FIELD = 'select, textarea, input:not([type=checkbox], [type=radio], [type=range], [type=color], [type=file], ' +
    '[type=hidden], [type=submit], [type=button], [type=reset], [type=image]), .inp, .edin, .seg, .chin, .choice';
  const SURFACE = '.card, .sheet, .panel, .syncp, .toast, aside#side, .tabbar, [role=dialog], [role=menu], [role=listbox]';
  const DARK = 0.25;   // relative luminance: the slate #5a6275 is 0.10, the light theme's lines (#cfd4de) 0.65, its fills 0.9+
  const NEARBLACK = 0.03;   // any other box: #171c28 is 0.011, #2b3140 0.028; the loan bar's slate #5b6784 (0.13) is fine
  const BUTTONS = 'button, [role=button], [role=switch], [role=tab], .btn, .cbtn, img, svg, canvas, video, iframe';
  const INK = '.ia, .av';   // a bank's or merchant's tile in its brand colour (U.S. Bank's navy) with a white initial
  const layers = (v) => { const out = []; let depth = 0, cur = '';   // a list property's layers (commas outside brackets)
    for (const ch of String(v)) { if (ch === '(') depth += 1; if (ch === ')') depth -= 1;
      if (ch === ',' && !depth) { out.push(cur.trim()); cur = ''; } else cur += ch; }
    return [...out, cur.trim()]; };
  const fillOf = (el) => {   // the colours behind el, from the page up to el itself (gradients aside)
    const cs = [];
    for (let e = el; e; e = e.parentElement) { const c = rgba(getComputedStyle(e).backgroundColor); if (c && c[3] > 0) { cs.push(c); if (c[3] >= 1) break; } }
    let out = [243, 244, 247];   // --bg (light)
    for (const c of cs.reverse()) out = over(c, out);
    return out;
  };
  const extent = (v, box) => (v === 'auto' || v === undefined ? box : v.endsWith('%') ? box * parseFloat(v) / 100 : parseFloat(v));
  for (const el of all) {
    const control = el.matches(FIELD) || el.matches(SURFACE);
    if (!control && (el.matches(BUTTONS) || el.closest(BUTTONS) || el.matches(INK))) continue;
    const box = el.getBoundingClientRect();
    if (!control && Math.min(box.width, box.height) < 8) continue;   // a line or a mark in ink (a bar's pace marker), not a box
    const s = getComputedStyle(el);
    const own = rgba(s.backgroundColor);
    if (!control && !(own && own[3] >= 0.5) && !s.backgroundImage.includes('gradient')) continue;
    seen.darkbg += 1;
    const limit = control ? DARK : NEARBLACK;
    const fill = control ? fillOf(el) : own && own[3] >= 0.5 ? own : null;
    if (fill && lum(fill) < limit) fail('darkbg', control ? 'a dark fill in the light theme' : 'a near-black box in the light theme',
      [el], {fill: fill.slice(0, 3).map(Math.round), background: s.backgroundColor});
    const r = el.getBoundingClientRect(), imgs = layers(s.backgroundImage), sizes = layers(s.backgroundSize), reps = layers(s.backgroundRepeat);
    imgs.forEach((img, i) => {
      if (!/gradient\(/.test(img)) return;
      const dark = (img.match(/rgba?\([^)]+\)/g) || []).map(rgba).filter((c) => c[3] >= 0.5 && lum(c) < limit);
      if (!dark.length) return;
      const size = sizes[i % sizes.length] || 'auto', rep = reps[i % reps.length] || 'repeat';
      const [sw, sh] = ['cover', 'contain'].includes(size) ? ['auto', 'auto'] : size.split(/\s+/);
      const w = Math.min(extent(sw, r.width), r.width), h = Math.min(extent(sh, r.height), r.height);
      const covered = rep.includes('no-repeat') ? w * h : r.width * r.height;
      if (covered >= 0.2 * r.width * r.height)
        fail('darkbg', 'a dark gradient paints a fifth of it or more in the light theme (an arrow drawn full size?)', [el],
          {layer: img.slice(0, 90), size, covers: +(covered / (r.width * r.height)).toFixed(2)});
    });
  }
}

// ---------- segments (TASK-12b) ----------
// A toggle is a button with aria-pressed or role=radio. A group: every .seg, every role=group / radiogroup of toggles,
// and any parent of 2+ toggles side by side. Per line: equal gaps between neighbouring toggles (1px). A dedicated
// control on one line that is stretched (its inner width is more than the buttons' natural widths + gaps + 8px, and
// the buttons spread across it) must give every button the same width (1px).
const isToggle = (b) => b.tagName === 'BUTTON' && (b.hasAttribute('aria-pressed') || b.getAttribute('role') === 'radio');
const groups = new Set();
for (const el of all) {
  const toggles = [...el.children].filter((c) => isToggle(c) && shown(c));
  if (el.matches('.seg') || toggles.length >= 2) groups.add(el);
}
for (const g of groups) {
  const dedicated = g.matches('.seg, [role=group], [role=radiogroup]');
  const btns = [...g.children].filter((c) => c.tagName === 'BUTTON' && shown(c) && (dedicated || isToggle(c)));
  if (btns.length < 2) continue;
  seen.segments += 1;
  const rects = btns.map((b) => b.getBoundingClientRect());
  const lines = [];
  btns.forEach((b, i) => {
    const line = lines.find((l) => Math.abs(l.top - rects[i].top) <= 2);
    if (line) line.items.push(i); else lines.push({top: rects[i].top, items: [i]});
  });
  for (const line of lines) {
    const gaps = [];
    for (let k = 0; k + 1 < line.items.length; k++) {
      const a = line.items[k], b = line.items[k + 1];
      if (btns[a].nextElementSibling !== btns[b]) continue;  // something else sits between them
      gaps.push(+(rects[b].left - rects[a].right).toFixed(1));
    }
    if (gaps.length >= 2 && Math.max(...gaps) - Math.min(...gaps) > 1)
      fail('segments', 'unequal gaps between the buttons of a segmented control', line.items.map((i) => btns[i]), {gaps});
  }
  if (dedicated && lines.length === 1) {
    const saved = btns.map((b) => [b.style.flex, b.style.width, b.style.minWidth]);
    btns.forEach((b) => { b.style.flex = '0 0 auto'; b.style.width = 'max-content'; b.style.minWidth = '0'; });
    const natural = btns.map((b) => b.getBoundingClientRect().width);
    btns.forEach((b, i) => { [b.style.flex, b.style.width, b.style.minWidth] = saved[i]; });
    const cs = getComputedStyle(g);
    const gap = parseFloat(cs.columnGap) || 0;
    const inner = g.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    const want = natural.reduce((x, v) => x + v, 0) + gap * (btns.length - 1);
    const span = rects[rects.length - 1].right - rects[0].left;
    if (inner > want + 8 && span > want + 8) {
      const widths = rects.map((r) => +r.width.toFixed(1));
      if (Math.max(...widths) - Math.min(...widths) > 1)
        fail('segments', 'a stretched segmented control whose buttons have different widths', btns,
             {widths, inner: Math.round(inner), natural: Math.round(want)});
    }
  }
}

// TASK-15: a switch (role=switch, or a button with role=checkbox: the Custom net worth sheet's account and group
// switches) is a track of at least 36 x 20 px with its knob inside it (0.5px). This task's first cut shared the legend
// swatch's class name and shrank to a 14px square with the knob hanging off its side; no other check saw it.
for (const sw of all.filter((x) => x.matches('[role=switch], button[role=checkbox]'))) {
  seen.segments += 1;
  const r = sw.getBoundingClientRect();
  const off = [...sw.querySelectorAll('*')].filter(shown).filter((k) => {
    const q = k.getBoundingClientRect();
    return q.left < r.left - 0.5 || q.right > r.right + 0.5 || q.top < r.top - 0.5 || q.bottom > r.bottom + 0.5;
  });
  if (r.width < 36 || r.height < 20 || off.length)
    fail('segments', 'a switch smaller than 36 x 20 px, or its knob outside its track', [sw, ...off],
         {size: [+r.width.toFixed(1), +r.height.toFixed(1)]});
}

// ---------- empty states (TASK-12b) ----------
// Its container: the chart area around an .emptyov, else the parent. Horizontally centred in the container's content
// box (4px). With no canvas drawn in it: vertically centred between the nearest content above and below (8px), and
// no more than 120px of blank space on either side. With a chart drawn: it must not cover any of its points.
for (const e of all.filter((x) => x.matches('.emptyov, .empty, [data-empty]'))) {
  if (e.parentElement && e.parentElement.closest('.emptyov, .empty, [data-empty]')) continue;
  const c = (e.classList.contains('emptyov') && e.closest('.chwrap, .cw')) || e.parentElement;
  if (!c) continue;
  seen.empty += 1;
  const cr = c.getBoundingClientRect(), cs = getComputedStyle(c), er = e.getBoundingClientRect();
  const px = (v) => parseFloat(v) || 0;
  const inL = cr.left + px(cs.borderLeftWidth) + px(cs.paddingLeft), inR = cr.right - px(cs.borderRightWidth) - px(cs.paddingRight);
  const inT = cr.top + px(cs.borderTopWidth) + px(cs.paddingTop), inB = cr.bottom - px(cs.borderBottomWidth) - px(cs.paddingBottom);
  const left = er.left - inL, right = inR - er.right;
  if (Math.abs(left - right) > 4)
    fail('empty', 'an empty-state message off the horizontal centre of its container', [e, c], {left: Math.round(left), right: Math.round(right)});
  const canvases = [...c.querySelectorAll('canvas')].filter(shown);
  if (!canvases.length) {
    const sibs = [...c.children].filter((x) => x !== e && !x.contains(e) && shown(x) && !['absolute', 'fixed'].includes(getComputedStyle(x).position));
    const above = Math.max(inT, ...sibs.map((x) => x.getBoundingClientRect()).filter((r) => r.bottom <= er.top + 1).map((r) => r.bottom));
    const below = Math.min(inB, ...sibs.map((x) => x.getBoundingClientRect()).filter((r) => r.top >= er.bottom - 1).map((r) => r.top));
    const top = er.top - above, bottom = below - er.bottom;
    if (Math.abs(top - bottom) > 8)
      fail('empty', 'an empty-state message off the vertical centre of its container (no chart drawn)', [e, c], {top: Math.round(top), bottom: Math.round(bottom)});
    if (Math.max(top, bottom) > 120)
      fail('empty', 'an empty container keeping more than 120px of blank space around its message', [e, c], {top: Math.round(top), bottom: Math.round(bottom)});
  } else {
    for (const cv of canvases) {
      const ch = window.Chart && Chart.getChart(cv);
      if (!ch) continue;
      const vr = cv.getBoundingClientRect();
      ch.data.datasets.forEach((ds, di) => ch.getDatasetMeta(di).data.forEach((pt, i) => {
        if (ds.data[i] == null || pt.skip) return;
        const x = vr.left + pt.x, y = vr.top + pt.y, r = 8;
        if (x + r > er.left && x - r < er.right && y + r > er.top && y - r < er.bottom)
          fail('empty', 'an empty-state message covers a point of the chart behind it', [e, cv], {point: [Math.round(x), Math.round(y)]});
      }));
    }
  }
}

// ---------- overlays ----------
// Sampled inside the rounded corners: the middle, and the middle of each edge 12px in. An overlay may only be
// covered by one stacked above it on purpose (drawer < panel < dialog < toast), e.g. "Mark as paid off?" over the panel.
const STACK = ['drawer', 'panel', 'chat', 'syncp', 'modal', 'toast'];   // (TASK-22f: Sync now's panel; TASK-23: the Ask panel)
const layerOf = (e) => (e.closest('#toast') ? 'toast' : e.closest('#modal') ? 'modal' : e.closest('#syncp, #syncscrim') ? 'syncp'
  : e.closest('#chat') ? 'chat' : e.closest('#panel, #dscrim') ? 'panel' : e.closest('#side, #scrim') ? 'drawer' : null);
const onTop = (el, id) => { const r = el.getBoundingClientRect(); const b = Math.min(r.bottom, innerHeight), cx = (r.left + r.right) / 2, cy = (r.top + b) / 2;
  const pts = [[cx, cy], [r.left + 12, cy], [r.right - 12, cy], [cx, r.top + 12], [cx, b - 12]];
  return pts.filter(([x, y]) => { const e = document.elementFromPoint(x, y);
    if (!e || e === el || el.contains(e)) return false;
    const l = layerOf(e);
    return !(l && STACK.indexOf(l) > STACK.indexOf(id)); }); };
for (const [id, sel] of [['toast', '#toast.on'], ['modal', '#modal.on .sheet'], ['panel', '#panel:not([hidden])'], ['syncp', '#syncp:not([hidden])'],
  ['chat', '#chat:not([hidden])'], ['drawer', 'body.drawer #side']]) {
  const el = document.querySelector(sel);
  if (!el) continue;
  seen.overlays += 1;
  const r = el.getBoundingClientRect();
  if (r.left < -0.5 || r.right > innerWidth + 0.5 || r.top < -0.5 || (id !== 'drawer' && r.bottom > innerHeight + 0.5 && getComputedStyle(el).overflowY === 'visible'))
    fail('overlays', `the ${id} is not fully on screen`, [el], {rect: [r.left, r.top, r.right, r.bottom].map(Math.round), viewport: [innerWidth, innerHeight]});
  const hidden = onTop(el, id);
  if (hidden.length) fail('overlays', `something covers the ${id}`, [el], {points: hidden.map((p) => p.map(Math.round))});
  if (phone && id === 'toast') {
    const bar = document.getElementById('tabbar').getBoundingClientRect();
    if (r.bottom > bar.top + 0.5) fail('overlays', 'the toast sits under the phone tab bar', [el], {toast_bottom: Math.round(r.bottom), tabbar_top: Math.round(bar.top)});
  }
}

// ---------- TASK-18: the reviewer's independent audit (sketches/017-launch/audit_independent.py) and shoot_017.py's row check,
// ported. Hidden layers are skipped exactly: [hidden], <template>, .sr text, aria-hidden, the content of a closed
// <details>, anything invisible. Text is measured by its own rects (Range over the text node), never an icon's.
const vis2 = (el) => {
  if (!el || !el.getClientRects().length) return false;
  if (el.closest('[hidden], template, .sr, [aria-hidden="true"], details:not([open]) > :not(summary)')) return false;
  const cs = getComputedStyle(el);
  if (cs.visibility === 'hidden' || cs.display === 'none') return false;
  for (let e = el; e; e = e.parentElement) if (Number(getComputedStyle(e).opacity) === 0) return false;
  return true;
};
const within = (el) => roots.some((r) => r.contains(el));
const rng2 = document.createRange();
const textRects = (node) => { rng2.selectNodeContents(node); return [...rng2.getClientRects()].filter((q) => q.width > 0.5 && q.height > 0.5); };
const textNodes = (root) => {
  const out = [];
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) {
    const n = w.currentNode;
    if (!n.data.trim() || !n.parentElement || n.parentElement.closest('svg, option, select, textarea, script, style')) continue;
    if (vis2(n.parentElement)) out.push(n);
  }
  return out;
};
const lines = (rects) => { const tops = []; for (const q of rects) if (!tops.some((t) => Math.abs(t - q.top) <= 2)) tops.push(q.top); return tops.length; };

// page overflow: nothing makes the page wider than the window (a sideways scroll on a phone, a cut edge on a laptop).
// TEST-SPEED: on a phone html and body are overflow-x: hidden (app.css), so a page wider than the window doesn't scroll
// at all: it is cut off at the right edge, which documentElement.scrollWidth never shows (body clips it); body's own
// scroll width past its box does. (Seen by planting a 640px Settings grid at 390: only the tap check noticed, through the
// controls it pushed off screen; a wide card with no control in it passed.)
seen.overflow += 1;
const bodyX = getComputedStyle(document.body).overflowX;
const wide = Math.max(document.documentElement.scrollWidth - innerWidth,
                      bodyX === 'visible' ? 0 : document.body.scrollWidth - document.body.clientWidth);
if (wide > 0) {
  const culprits = [...document.querySelectorAll('body *')].filter((e) => vis2(e) && e.getBoundingClientRect().right > innerWidth + 1
    && getComputedStyle(e).position !== 'fixed').slice(-3);
  fail('overflow', 'the page is wider than the window', culprits.length ? culprits : [document.body], {by: wide});
}

// escape: an element sticking out of its card (sheet, panel, frame) sideways; a horizontal scroller's content may
const UNITS = '.card, .sheet, .panel, .syncp, .frame, .chat';   // (TASK-23: the Ask panel)
const scrollerOf = (el, stop) => { for (let p = el.parentElement; p && p !== stop; p = p.parentElement) {
  if (/(auto|scroll)/.test(getComputedStyle(p).overflowX)) return p; } return null; };
for (const unit of [...document.querySelectorAll(UNITS)].filter((u) => within(u) && vis2(u))) {
  const cr = unit.getBoundingClientRect();
  for (const el of unit.querySelectorAll('*')) {
    if (el.closest('svg') && el.tagName !== 'svg') continue;
    if (!vis2(el) || getComputedStyle(el).position === 'fixed') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1 || scrollerOf(el, unit)) continue;
    seen.escape += 1;
    const by = Math.max(r.right - cr.right, cr.left - r.left);
    if (by > 1.5) fail('escape', 'an element sticks out of its card', [el, unit], {by: Math.round(by)});
  }
}

// cliptext: text cut by an overflow-hidden box. An ellipsis is fine only where one can be drawn: single-line text
// (nowrap / pre) in a block box. In a flex or grid box, or on a wrapping line, the text is simply cut ("Subscriptio").
const allText = roots.flatMap((r) => textNodes(r));
for (const node of allText) {
  const el = node.parentElement;
  const rects = textRects(node);
  if (!rects.length) continue;
  seen.cliptext += 1;
  for (let e = el; e && e !== document.documentElement; e = e.parentElement) {
    const cs = getComputedStyle(e);
    if (cs.textOverflow === 'ellipsis' && /nowrap|pre/.test(cs.whiteSpace) && !/flex|grid/.test(cs.display)) break;  // a drawn ellipsis
    if (/(auto|scroll)/.test(cs.overflowX) || /(auto|scroll)/.test(cs.overflowY)) break;  // in a scroller: scrolled to, not cut
    if (['hidden', 'clip'].includes(cs.overflowX) || ['hidden', 'clip'].includes(cs.overflowY)) {
      const b = e.getBoundingClientRect();
      const L = b.left + e.clientLeft, T = b.top + e.clientTop, Rr = L + e.clientWidth, B = T + e.clientHeight;
      const cut = rects.find((q) => q.right > Rr + 1 || q.bottom > B + 1 || q.left < L - 1 || q.top < T - 1);
      if (cut) {
        fail('cliptext', 'text cut off by its box (no ellipsis can be drawn there)', [el, e],
          {text: node.data.trim().slice(0, 40), over: Math.round(Math.max(cut.right - Rr, cut.bottom - B, L - cut.left, T - cut.top))});
        break;
      }
    }
  }
}

// wrapped: a button / pill / chip / tag / segment label on two lines. Multi-line by design: a choice card, an Add account
// option row, a ghost row (a title with a description under it).
const LABELS = 'button, .btn, .pill, .tag, .badge, .chip, .fchip, [role=tab], .seg > *, .choices > *';
for (const el of [...document.querySelectorAll(LABELS)].filter((e) => within(e) && vis2(e))) {
  if (el.matches('.choice, .opt, .ghost-row, .li, .ghost-row *') || el.closest('.choice, .opt, .ghost-row')) continue;
  // a composite button (a list row: a title with a description or cells under / beside it) is several lines by design;
  // a label is text straight inside the element or in inline pieces of it
  if ([...el.children].some((k) => vis2(k) && !/^inline/.test(getComputedStyle(k).display) && k.textContent.trim())) continue;
  const rects = textNodes(el).flatMap(textRects);
  if (!rects.length) continue;
  seen.wrapped += 1;
  const n = lines(rects);
  if (n > 1) fail('wrapped', 'a button or label wraps onto more lines', [el], {lines: n, text: el.textContent.trim().slice(0, 40)});
}

// rows: in a row of cards (tops within 2px, side by side), each card's title, hero number, bar and first detail line
// (a board frame's panel and heading) at the same y, within 2px
{
  const UNIT = '.card, .frame';
  const top = (el) => Math.round(el.getBoundingClientRect().top + scrollY);
  const own = (card, sel) => [...card.querySelectorAll(sel)].find((e) => e.closest(UNIT) === card && vis2(e));
  const cards = [...document.querySelectorAll(UNIT)].filter((c) => within(c) && vis2(c))
    .sort((a, b) => top(a) - top(b) || a.getBoundingClientRect().left - b.getBoundingClientRect().left);
  const rowsOf = [];
  for (const c of cards) {
    const t = top(c), cb = c.getBoundingClientRect();
    let row = rowsOf.find((r) => Math.abs(r.t - t) <= 2 && r.cards.every((o) => { const a = o.getBoundingClientRect();
      return (cb.left >= a.right - 1 || cb.right <= a.left + 1) && !o.contains(c) && !c.contains(o); }));
    if (!row) rowsOf.push(row = {t, cards: []});
    row.cards.push(c);
  }
  const hero = (c) => own(c, '.big');
  const parts = {
    title: (c) => own(c, ':scope > h3') || own(c, ':scope > .hd') || own(c, '.frh'),
    hero,
    bar: (c) => { const hr = hero(c); if (!hr) return null;
      return [...c.querySelectorAll('.split, .stk, .track, .pbar, .segs, .share, .meter')].find((e) => e.closest(UNIT) === c && vis2(e)
        && (hr.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING)); },
    detail: (c) => own(c, '.tl2'),
    panel: (c) => (c.matches('.frame') ? own(c, '.sheet') : null),
    heading: (c) => (c.matches('.frame') ? own(c, 'h2') : null),
  };
  for (const r of rowsOf.filter((x) => x.cards.length > 1)) {
    for (const [kind, f] of Object.entries(parts)) {
      const ys = r.cards.map((c) => { const e = f(c); return e ? [c, e, top(e)] : null; }).filter(Boolean);
      if (ys.length < 2) continue;
      seen.rows += 1;
      const spread = Math.max(...ys.map((x) => x[2])) - Math.min(...ys.map((x) => x[2]));
      if (spread > 2) fail('rows', `cards side by side whose ${kind} is not at one height`, ys.map((x) => x[1]), {spread, ys: ys.map((x) => x[2])});
    }
  }
}

// orphans: a lone fragment on the last line of a wrapped text ("29)" alone, "it", "ago") that shares the line with no
// other text of its block
{
  const blockOf = (el) => { let e = el; while (e && getComputedStyle(e).display.startsWith('inline')) e = e.parentElement; return e; };
  for (const node of allText) {
    const rects = textRects(node);
    if (lines(rects) < 2) continue;
    seen.orphans += 1;
    const lastTop = Math.max(...rects.map((q) => q.top));
    let start = 0;
    for (let i = node.data.length - 1; i >= 0; i--) {
      rng2.setStart(node, i); rng2.setEnd(node, i + 1);
      const b = rng2.getBoundingClientRect();
      if (!b.width && !b.height) continue;
      if (b.top < lastTop - 2) { start = i + 1; break; }
    }
    const tail = node.data.slice(start).trim();
    if (!(/^\S{0,5}\)[.,;:]?$/.test(tail) || (tail.length <= 3 && /\w/.test(tail)))) continue;
    const block = blockOf(node.parentElement);
    let shared = false;
    for (const o of textNodes(block)) {
      if (o !== node && textRects(o).some((q) => Math.abs(q.top - lastTop) < 3)) { shared = true; break; }
    }
    if (!shared) fail('orphans', 'a lone fragment on the last line of a text', [node.parentElement], {tail, text: node.data.trim().slice(0, 60)});
  }
}

// headers: a card or frame header is meant to be a fixed-height strip (h3/.hd min-height 32px, align-items
// center). If it grows a line, every part of the card below it — the hero number, the bar, the first detail line —
// drops out of line with the cards beside it, so the row looks ragged. That is what "Left to spend" did: a ~140px
// labelled trigger in the header beside a 32px icon trigger does not fit the content width, h3 wraps, and its hero
// fell 42px below the cards beside it.
// Only a header taller than its own declared strip counts. A header that wraps onto a second line at exactly
// min-height (a tall `extra` under a short title, which the base h3 rule allows on purpose) is fine, and `nowrap`
// headers that fit on one line are fine.
{
  const H = ':scope > h3, :scope > .hd';
  for (const hd of [...document.querySelectorAll('.card, .frame')].flatMap((c) => [...c.querySelectorAll(H)]
    .filter((e) => e.closest('.card, .frame') === c && vis2(e)))) {
    const kids = [...hd.children].filter((e) => vis2(e) && e.getBoundingClientRect().height);
    if (kids.length < 2) continue;
    seen.headers += 1;
    const cs = getComputedStyle(hd);
    const box = hd.getBoundingClientRect();
    // A line is a band of children that overlap vertically. (Children on ONE line have different tops: h3 centres
    // them, so a 24px title and a 20px extra sit 5px apart. Comparing tops alone reads every header as wrapped.)
    const bands = [];
    for (const e of [...kids].sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)) {
      const r = e.getBoundingClientRect();
      const band = bands.find((b) => r.top < b.bottom - 2 && r.bottom > b.top + 2);
      if (band) { band.top = Math.min(band.top, r.top); band.bottom = Math.max(band.bottom, r.bottom); }
      else bands.push({top: r.top, bottom: r.bottom});
    }
    const strip = parseFloat(cs.minHeight) || bands[0].bottom - bands[0].top;
    if (bands.length < 2 || box.height - strip <= 2) continue;
    const card = hd.closest('.card, .frame');
    // only matters when there is a card BESIDE it to be out of line with; a lone full-width card may wrap harmlessly
    const sideBySide = [...document.querySelectorAll('.card, .frame')].some((o) => {
      if (o === card) return false;
      const a = o.getBoundingClientRect(), cr = card.getBoundingClientRect();
      return Math.abs(a.top - cr.top) <= 2 && (cr.left >= a.right - 1 || a.left >= cr.right - 1);
    });
    if (!sideBySide) continue;
    fail('headers', 'a card header is taller than its one-line strip, so the card below it sits out of line', [hd],
      {height: Math.round(box.height), strip: Math.round(strip), lines: bands.length, flexWrap: cs.flexWrap,
       title: (hd.querySelector('.ttl') || {}).textContent,
       kids: kids.map((e) => `${e.className || e.tagName}:${(e.textContent || '').trim().slice(0, 24)}`)});
  }
}

// rowname (docs/ui-invariants.md rule 19): a list row's name stays on one line. When it wraps, its row grows taller
// than its neighbours and the list goes ragged. The "No budget yet" rows did that while app.css's
// `.li.wrap .grow > b { white-space: normal }` beat their nowrap on source order (budgets.css, OVERRIDE 1). Like
// headers, only the symptom counts: a row (.li) whose name (its .grow's <b>, link or <strong>: the name's own text, not
// a badge inside it) takes two lines or more AND which is taller than a row beside it in the same list, of the same
// shape (its classes, its children, the line count of everything else in its .grow), whose name is on one line. A lone
// row, or a list whose rows all wrap alike, isn't ragged.
// Which rows: a name is one line by default (app.css `.grow > b`: nowrap, an ellipsis). A `.li.wrap` row lets its name
// wrap on purpose (app.css `.li.wrap .grow > b { white-space: normal }`: Recurring's bills and subscriptions,
// "Zenith Financial Services" on two lines rather than cut), so those are left out, except the `.li.wrap` rows that
// keep their name on one line all the same, listed in ONE_LINE_WRAP_ROWS (budgets.css OVERRIDE 1).
{
  const ONE_LINE_WRAP_ROWS = '.nbrow';
  const NAME = ':scope > .grow > b, :scope > .grow > a, :scope > .grow > strong';
  const cls = (e) => (typeof e.className === 'string' ? e.className : '').trim();
  const lists = new Map();
  for (const row of document.querySelectorAll(`.li:not(.wrap), .li.wrap:is(${ONE_LINE_WRAP_ROWS})`)) {
    if (!within(row) || !vis2(row)) continue;
    const grow = row.querySelector(':scope > .grow');
    const name = row.querySelector(NAME);
    if (!grow || !name || !vis2(name)) continue;
    const own = [...name.childNodes].filter((n) => n.nodeType === 3 && n.data.trim());
    if (!own.length) continue;
    seen.rowname += 1;
    const rest = [...grow.children].filter((e) => e !== name && vis2(e))
      .map((e) => `${e.tagName}.${cls(e)}:${lines(textNodes(e).flatMap(textRects))}`);
    const shape = [cls(row), ...[...row.children].filter(vis2).map((e) => `${e.tagName}.${cls(e)}`), ...rest].join('|');
    if (!lists.has(row.parentElement)) lists.set(row.parentElement, []);
    lists.get(row.parentElement).push({name, shape, n: lines(own.flatMap(textRects)), h: row.getBoundingClientRect().height});
  }
  for (const rows of lists.values()) {
    for (const r of rows) {
      if (r.n < 2) continue;
      const ref = rows.find((o) => o !== r && o.shape === r.shape && o.n === 1 && r.h - o.h > 2);
      if (ref) fail('rowname', "a list row's name wraps onto a second line, so its row is taller than the one beside it",
        [r.name, ref.name], {lines: r.n, height: Math.round(r.h), neighbour: Math.round(ref.h),
          whiteSpace: getComputedStyle(r.name).whiteSpace});
    }
  }
}

// cutnum (docs/ui-invariants.md rule 20): a figure is never cut. A label may end in an ellipsis; an amount, a
// percentage or a number may not lose a single character, whether to an ellipsis, a line clamp or a box that clips it.
// A figure: a currency amount ($1,234.56, -$12, $1.2k), a percentage (12%, 4.5 %), a number with thousands separators
// (12,345) or a decimal one (3.75). A plain integer isn't one, nor are digits inside a word or a name ("7-Eleven",
// "Store 4521", "A1C", "v1.2.3"): a merchant name that merely holds digits may truncate like any label.
// Each figure is measured character by character (a Range per character: Chrome reports the rects of the characters
// an ellipsis hides where they would have been). Going up from its text to the first scroller, a box that clips
// (overflow hidden / clip) cuts it when a character lies outside its padding box, or, when that box truncates its text
// (text-overflow: ellipsis with its content wider than it, or a -webkit-line-clamp with its content taller), when a
// character on its last visible line reaches into the room the ellipsis takes at its right edge.
{
  const FIG = /(?<![\p{L}\p{N}#_@\/.,'’-])(?:[-−+]?[$€£¥]\s?\d[\d,]*(?:\.\d+)?(?:[kKMB](?![\p{L}]))?|\d+(?:\.\d+)?\s?%|\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+)(?![\p{L}\p{N}]|[.,]\d)/gu;
  const ctx = document.createElement('canvas').getContext('2d');
  const ellW = (cs) => { ctx.font = cs.font || `${cs.fontSize} ${cs.fontFamily}`; return ctx.measureText('…').width; };
  const rg = document.createRange();
  const charRects = (node, from, to) => {
    const out = [];
    for (let i = from; i < to; i++) {
      if (/\s/.test(node.data[i])) continue;
      rg.setStart(node, i); rg.setEnd(node, i + 1);
      const q = [...rg.getClientRects()].filter((x) => x.width > 0.5 && x.height > 0.5);
      if (q.length) out.push(q.reduce((a, x) => (x.right > a.right ? x : a)));   // (a split character: its far piece)
    }
    return out;
  };
  for (const node of allText) {
    FIG.lastIndex = 0;
    for (const m of node.data.matchAll(FIG)) {
      const chars = charRects(node, m.index, m.index + m[0].length);
      if (!chars.length) continue;
      seen.cutnum += 1;
      let cut = null;
      for (let e = node.parentElement; e && e !== document.documentElement && !cut; e = e.parentElement) {
        const cs = getComputedStyle(e);
        if (/(auto|scroll)/.test(cs.overflowX) || /(auto|scroll)/.test(cs.overflowY)) break;   // scrolled to, not cut
        if (!['hidden', 'clip'].includes(cs.overflowX) && !['hidden', 'clip'].includes(cs.overflowY)) continue;
        const b = e.getBoundingClientRect();
        const L = b.left + e.clientLeft, T = b.top + e.clientTop, Rr = L + e.clientWidth, B = T + e.clientHeight;
        const clamp = cs.webkitLineClamp && cs.webkitLineClamp !== 'none' && e.scrollHeight > e.clientHeight + 1;
        const ell = clamp || (cs.textOverflow === 'ellipsis' && e.scrollWidth > e.clientWidth + 1);
        const room = ell ? ellW(cs) : 0;
        for (const q of chars) {
          const outside = q.right > Rr + 1 || q.left < L - 1 || q.bottom > B + 1 || q.top < T - 1;
          const lastLine = !clamp || q.bottom > B - q.height - 1;
          if (outside || (ell && lastLine && q.right > Rr - room + 0.5)) {
            cut = {by: e, how: outside ? 'clipped' : (clamp ? 'line-clamped' : 'ellipsised')};
            break;
          }
        }
      }
      if (cut) fail('cutnum', 'a figure is cut (only a label may be truncated)', [node.parentElement, cut.by],
        {figure: m[0], how: cut.how, text: node.data.trim().slice(0, 60)});
    }
  }
}

// (TASK-18 addendum) the phone tab bar: four labels, 13px or more, each on one line and whole
if (phone && scope === 'page' && tabShown) {
  for (const s of document.querySelectorAll('#tabbar a span')) {
    seen.text += 1;
    const cs = getComputedStyle(s);
    const rects = textNodes(s).flatMap(textRects);
    if (parseFloat(cs.fontSize) < 13 - 0.01 || lines(rects) > 1 || s.scrollWidth > s.clientWidth + 1)
      fail('text', 'a tab label is under 13px, wraps, or is cut', [s], {fontSize: cs.fontSize, lines: lines(rects)});
  }
}

// tap (TASK-20): on a phone (under 1024px) every control a finger aims at is 44 x 44 px or more to hit: buttons and
// link-buttons, inputs, selects, switches, chips, the menu button, a sheet's close, the month arrows, category pickers,
// segmented controls, the tab bar and the drawer's links. The hit area is the element's box, grown by an absolutely
// placed ::before / ::after of its own (the transparent hit box a small control gets without looking bigger) and, for
// an input, by the <label> it sits in. An inline text link inside a sentence is exempt.
if (phone) {
  const TAP = 'button, [role=button], [role=switch], [role=tab], [role=radio], [role=checkbox], input:not([type=hidden]), '
    + 'select, textarea, summary, a[href]';
  const px = (v) => parseFloat(v) || 0;
  const blockOf2 = (el) => { let e = el.parentElement; while (e && getComputedStyle(e).display.startsWith('inline')) e = e.parentElement; return e; };
  const inSentence = (a) => {
    if (a.tagName !== 'A' || getComputedStyle(a).display !== 'inline') return false;
    const b = blockOf2(a);
    return Boolean(b) && b.textContent.replace(a.textContent, '').trim().length > 0;
  };
  const hitRect = (el) => {
    const r = el.getBoundingClientRect();
    let [L, T, Rr, B] = [r.left, r.top, r.right, r.bottom];
    const es = getComputedStyle(el);
    if (es.position !== 'static') {
      for (const pe of ['::before', '::after']) {
        const cs = getComputedStyle(el, pe);
        if (['none', 'normal'].includes(cs.content) || cs.display === 'none' || cs.position !== 'absolute'
            || cs.pointerEvents === 'none' || cs.visibility === 'hidden') continue;
        const x0 = r.left + px(es.borderLeftWidth) + px(cs.left), y0 = r.top + px(es.borderTopWidth) + px(cs.top);
        const w = px(cs.width) + (cs.boxSizing === 'border-box' ? 0 : px(cs.paddingLeft) + px(cs.paddingRight) + px(cs.borderLeftWidth) + px(cs.borderRightWidth));
        const hh = px(cs.height) + (cs.boxSizing === 'border-box' ? 0 : px(cs.paddingTop) + px(cs.paddingBottom) + px(cs.borderTopWidth) + px(cs.borderBottomWidth));
        [L, T, Rr, B] = [Math.min(L, x0), Math.min(T, y0), Math.max(Rr, x0 + w), Math.max(B, y0 + hh)];
      }
    }
    const lab = el.matches('input, select, textarea') ? el.closest('label') : null;
    if (lab) { const q = lab.getBoundingClientRect(); [L, T, Rr, B] = [Math.min(L, q.left), Math.min(T, q.top), Math.max(Rr, q.right), Math.max(B, q.bottom)]; }
    // only what no overflow-hidden box around it cuts away can be hit (the element's own clip cuts its ::before / ::after)
    // (up to a scroller: what's scrolled out of its view is scrolled to, not cut). TASK-22c: axis by axis. On a phone
    // body is overflow-x: hidden, which makes its overflow-y compute to auto: the walk used to stop there as if at a
    // scroller, so a hit box past the screen's edge counted in full (the header's Sync now dot: 4 px of its 44 cut off)
    let cutX = true, cutY = true;
    for (let e = /hidden|clip/.test(es.overflowX + es.overflowY) ? el : (lab || el).parentElement; e && e !== document.documentElement && (cutX || cutY); e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (e !== el && /auto|scroll/.test(cs.overflowX)) cutX = false;
      if (e !== el && /auto|scroll/.test(cs.overflowY)) cutY = false;
      if (!/hidden|clip/.test(cs.overflowX + cs.overflowY)) continue;
      const q = e.getBoundingClientRect(), own = e === el;   // its own border still takes a tap; a box around it: its padding box
      const x0 = own ? q.left : q.left + e.clientLeft, y0 = own ? q.top : q.top + e.clientTop;
      const cw = own ? q.width : e.clientWidth, ch = own ? q.height : e.clientHeight;
      if (cutX && /hidden|clip/.test(cs.overflowX)) [L, Rr] = [Math.max(L, x0), Math.min(Rr, x0 + cw)];
      if (cutY && /hidden|clip/.test(cs.overflowY)) [T, B] = [Math.max(T, y0), Math.min(B, y0 + ch)];
    }
    return [Math.max(0, Rr - L), Math.max(0, B - T)];
  };
  const tapped = [];
  for (const el of [...document.querySelectorAll(TAP)].filter((e) => within(e) && vis2(e))) {
    if (el.closest('svg') || inSentence(el)) continue;
    if (el.matches('input, select, textarea') && el.closest('button, a[href]')) continue;  // inside a control: that one counts
    seen.tap += 1;
    tapped.push(el);
    const [w, hh] = hitRect(el);
    if (Math.min(w, hh) < 44 - 0.5) fail('tap', 'a control is under 44 x 44 px to tap on a phone', [el], {w: Math.round(w), h: Math.round(hh)});
  }

  // tap-covered (TASK-22d): the topmost element (elementFromPoint) at each of these controls' centre is the control or
  // inside it (or inside its <label>). The size check measured the Net worth tile's All / Custom at 44 px, but the
  // tile's own hit box (tap.css's ::after, inset 0 on the card link) was painted over them: a tap on Custom opened
  // Accounts. The centre is taken where the control is on screen (in the viewport and inside every box round it that
  // clips); the page, and any scroller round a control, is scrolled through to bring each one on screen, then put
  // back. A control under the fixed header or tab bar, a toast, or an open sheet / drawer / panel and its scrim (a
  // fixed or sticky layer that doesn't hold it) is skipped at that scroll and taken at another; the tab bar's own
  // controls, and a sheet's, are checked like any other. What never comes on screen uncovered, is clamped (its centre
  // moved into the part on screen) at the scroll the state had.
  const cover = new Map();
  for (const el of tapped) {
    if (getComputedStyle(el).pointerEvents === 'none') continue;   // not meant to take a tap
    const clips = [], scrollers = [];
    let fixed = false;
    for (let e = el.parentElement; e && e !== document.body && e !== document.documentElement; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.position === 'fixed') fixed = true;
      if (['inline', 'contents'].includes(cs.display)) continue;
      const cx = cs.overflowX !== 'visible', cy = cs.overflowY !== 'visible';
      if (cx || cy) clips.push([e, cx, cy]);
      if ((/auto|scroll/.test(cs.overflowY) && e.scrollHeight > e.clientHeight + 1) || (/auto|scroll/.test(cs.overflowX) && e.scrollWidth > e.clientWidth + 1))
        scrollers.push(e);
    }
    cover.set(el, {clips, scroller: scrollers[0] || null, fixed});
  }
  const onScreen = (el) => {
    // a text link that wraps: its biggest line box (the middle of its bounding box can fall in the gap between two
    // lines, on the text around it: the car page's photo credit, 2 px between "... Wikimedia" and "Commons")
    const rs = el.getClientRects();
    const r = rs.length > 1 ? [...rs].reduce((a, q) => (q.width * q.height > a.width * a.height ? q : a)) : el.getBoundingClientRect();
    let [L, T, Rr, B] = [Math.max(r.left, 0), Math.max(r.top, 0), Math.min(r.right, innerWidth), Math.min(r.bottom, innerHeight)];
    for (const [e, cx, cy] of cover.get(el).clips) {
      const q = e.getBoundingClientRect(), x0 = q.left + e.clientLeft, y0 = q.top + e.clientTop;
      if (cx) [L, Rr] = [Math.max(L, x0), Math.min(Rr, x0 + e.clientWidth)];
      if (cy) [T, B] = [Math.max(T, y0), Math.min(B, y0 + e.clientHeight)];
    }
    return [r, L, T, Rr, B];
  };
  const owns = (el, hit) => hit === el || el.contains(hit) || [...(el.labels || [])].some((l) => l.contains(hit));
  const layer = (hit, el) => {   // hit is in a fixed / sticky layer that doesn't hold el (the header, the tab bar, a scrim...)
    for (let e = hit; e && e !== document.documentElement; e = e.parentElement)
      if (['fixed', 'sticky'].includes(getComputedStyle(e).position)) return !e.contains(el);
    return false;
  };
  const todo = new Set(cover.keys());
  const probe = (clamp) => {
    for (const el of [...todo]) {
      const [r, L, T, Rr, B] = onScreen(el);
      if (Rr - L < 2 || B - T < 2) continue;                          // not on screen at this scroll
      let x = (r.left + r.right) / 2, y = (r.top + r.bottom) / 2;
      if (x < L + 1 || x > Rr - 1 || y < T + 1 || y > B - 1) {
        if (!clamp && r.height < innerHeight * 0.6) continue;        // its centre comes on screen at another scroll
        x = Math.min(Math.max(x, L + 1), Rr - 1);
        y = Math.min(Math.max(y, T + 1), B - 1);
      }
      const hit = document.elementFromPoint(x, y);
      if (!hit || (!owns(el, hit) && layer(hit, el))) continue;
      todo.delete(el);
      seen['tap-covered'] += 1;
      if (!owns(el, hit)) fail('tap-covered', 'a control is covered at its centre: a tap there lands on something else', [el, hit],
        {at: [Math.round(x), Math.round(y)]});
    }
  };
  // 0 to max in overlapping steps (a step clears the header and the tab bar on the way)
  const steps = (max, step) => { const out = []; for (let p = 0; p < max; p += Math.max(40, step)) out.push(p); out.push(Math.max(0, max)); return out; };
  const [sx0, sy0] = [scrollX, scrollY];
  const go = (y) => window.scrollTo({left: sx0, top: y, behavior: 'instant'});
  probe(false);
  if ([...todo].some((el) => !cover.get(el).fixed)) {                // the page, a screen at a time
    const doc = document.scrollingElement || document.documentElement;
    for (const y of steps(doc.scrollHeight - innerHeight, innerHeight - 260)) { if (!todo.size) break; go(y); probe(false); }
  }
  for (const s of new Set([...todo].map((el) => cover.get(el).scroller).filter(Boolean))) {   // a scroller: through it
    const one = [...todo].find((el) => cover.get(el).scroller === s);
    if (!one) continue;
    if (!cover.get(one).fixed) go(Math.max(0, scrollY + s.getBoundingClientRect().top - innerHeight * 0.25));   // on screen
    const [t0, l0] = [s.scrollTop, s.scrollLeft];
    for (const y of steps(s.scrollHeight - s.clientHeight, s.clientHeight - 60))
      for (const x of steps(s.scrollWidth - s.clientWidth, s.clientWidth - 60)) { s.scrollTop = y; s.scrollLeft = x; probe(false); }
    [s.scrollTop, s.scrollLeft] = [t0, l0];
  }
  go(sy0);
  probe(true);
}

return {violations: V, seen, scroll: [scrollX, scrollY], size: [innerWidth, innerHeight],
  full: [document.documentElement.scrollWidth, document.documentElement.scrollHeight]};
})"""

OVERLAY = r"""((rects, fixed) => {
  const o = document.createElement('div');
  o.id = '__audit_overlay';
  o.style.cssText = `position:${fixed ? 'fixed' : 'absolute'};left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none`;
  for (const [x, y, w, h] of rects) {
    const b = document.createElement('div');
    b.style.cssText = `position:absolute;left:${x - (fixed ? scrollX : 0) - 3}px;top:${y - (fixed ? scrollY : 0) - 3}px;width:${w + 6}px;height:${h + 6}px;border:3px solid #ff1f1f;box-shadow:0 0 0 1px #fff`;
    o.append(b);
  }
  document.body.append(o);
  return true;
})"""


FIXTURES = ROOT / "tests" / "fixtures" / "import"


def import_js(name: str) -> str:
    """JS: drop a (synthetic) fixture file into the open import sheet's file input, as a person choosing it would."""
    import base64
    data = base64.b64encode((FIXTURES / name).read_bytes()).decode()
    return (f"(() => {{ const bin = atob({json.dumps(data)}); const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));"
            f" const dt = new DataTransfer(); dt.items.add(new File([bytes], {json.dumps(name)}));"
            f" const i = document.querySelector('#sheet input[type=file]'); i.files = dt.files;"
            f" i.dispatchEvent(new Event('change')); return 1; }})()")


def load(b: Chrome, base: str, page: str) -> None:
    """A fresh page load of #/page, back once the page is drawn (READY's test, without its two frames: settled() follows)."""
    b.goto(f"{base}/?layout={time.time():.3f}#/{page}", ready=False)
    b.js(READY_NOW)


def run_state(b: Chrome, base: str, width: int, name: str, page: str, steps: list[str], scope: str,
              inject: str | None = None, theme: str = "dark") -> dict:
    b.size(width, HEIGHT[width])
    load(b, base, page)
    b.problems()
    unsettled: list[str] = []

    def settled(after: str) -> None:   # TEST-SPEED: the page settled, however long that takes (see REPORT_LOCK's note)
        why = b.settled()
        if why:
            unsettled.append(f"{after}: {why}")
    if theme != "dark":  # this device's theme (the saved one stays dark); the page redraws its charts in the light ink
        # (switched once the page is drawn, as before TEST-SPEED; the switch's own redraw is what's waited for)
        b.js(f"window.HFTheme.set({json.dumps(theme)})")
        b.js(READY_NOW)  # (TASK-20) the redraw is a new route: wait until it's done, as after a page load (a busy machine)
        settled("theme")
    else:
        settled("load")
    if inject:  # a stylesheet after the page's own (the discriminating test re-introduces old CSS this way)
        b.js(f"(() => {{ const s = document.createElement('style'); s.textContent = {json.dumps(inject)}; "
             f"document.head.append(s); return 1; }})()")
        settled("stylesheet")
    try:
        for i, js in enumerate(steps, 1):
            b.js(import_js(js[7:]) if js.startswith("IMPORT:") else js)
            if i < len(steps) and steps[i].startswith("window.__appear("):
                continue   # the next step waits for its element itself (and settled() follows it)
            settled(f"step {i}")
        out = b.js(f"{CHECK_JS}({json.dumps(scope)})")
        out["problems"] = b.problems()
        out["unsettled"] = unsettled
    finally:  # the net worth mode and the theme are remembered on the device: the next state starts in All, dark
        b.js("localStorage.removeItem('hf.networth.mode'); localStorage.removeItem('hf.theme'); "
             "sessionStorage.removeItem('hf.chat.v1')")   # (TASK-23: the Ask panel's conversation)
    return out


# The self-test: each check ported from the independent audit must catch a bug planted on the Overview before the run
# counts for anything; with the plants gone, none of their violations is left. TEST-SPEED: every declaration a plant
# sets on itself is !important (imp()), so the page's own CSS can't restyle it: the self-test tests the checks, the
# states test the page. (A planted `select { background: #12151c !important }` in light.css painted the self-test's
# correct light select dark: the self-test stopped the run, and the pages' darkbg check never got to flag the bug.)
PLANT_JS = r"""(() => {
  const c = document.querySelector('#page .card');
  const tiles = [...document.querySelectorAll('#page .tiles > .card')].filter((t) => t.querySelector('.big'));
  const imp = (css) => css.split(';').map((d) => d.trim()).filter(Boolean).map((d) => d.replace(/\s*!important$/, '') + ' !important').join(';');
  const add = (parent, tag, props) => parent.append(Object.assign(document.createElement(tag), props, {style: imp(props.style || '')}));
  add(c, 'div', {id: 'plant-escape', textContent: 'planted escape', style: 'width:4000px'});
  add(c, 'div', {id: 'plant-clip', textContent: 'planted clipped line of text', style: 'width:60px;overflow:hidden;white-space:nowrap'});
  add(c, 'span', {id: 'plant-flexclip', textContent: 'Subscriptions monthly', style: 'display:flex;width:40px;overflow:hidden;text-overflow:ellipsis;white-space:normal'});
  add(c, 'button', {id: 'plant-wrap', textContent: 'planted long button label', style: 'width:60px'});
  add(c, 'p', {id: 'plant-orphan', textContent: 'aaaaaaaa 29)', style: 'width:9ch;font-family:monospace'});
  const spacer = Object.assign(document.createElement('div'), {id: 'plant-row', style: imp('height:14px')});
  if (tiles.length > 1) tiles[1].querySelector('.big').before(spacer);
  add(document.body, 'div', {id: 'plant-wide', textContent: 'x', style: 'position:absolute;left:0;top:0;width:4000px;height:1px'});
  return tiles.length;
})()"""
UNPLANT_JS = ("(() => { ['plant-escape', 'plant-clip', 'plant-flexclip', 'plant-wrap', 'plant-orphan', 'plant-row', 'plant-wide']"
              ".forEach((id) => { const e = document.getElementById(id); if (e) e.remove(); }); return 1; })()")


TAP_PLANT_JS = r"""(() => {
  const c = document.querySelector('#page .card');
  const st = Object.assign(document.createElement('style'), {id: 'plant-tap-style', textContent:
    '#plant-tap-hit { position: relative; } #plant-tap-hit::before { content: ""; position: absolute; inset: -7px; } '
    + '#plant-tap-small::after, #plant-tap-link::after { content: none !important; }'});   /* not grown by static/tap.css */
  document.head.append(st);
  const imp = (css) => css.split(';').map((d) => d.trim()).filter(Boolean).map((d) => d.replace(/\s*!important$/, '') + ' !important').join(';');
  const add = (parent, tag, props) => { const e = Object.assign(document.createElement(tag), props, {style: imp(props.style || '')}); parent.append(e); return e; };
  add(c, 'button', {id: 'plant-tap-small', type: 'button', textContent: 'tapsmall', style: 'width:30px;height:30px;padding:0;border:0;font-size:8px'});
  add(c, 'button', {id: 'plant-tap-hit', type: 'button', textContent: 'taphit', style: 'width:30px;height:30px;padding:0;border:0;font-size:8px'});
  add(add(c, 'div', {}), 'a', {id: 'plant-tap-link', href: '#/overview', textContent: 'taplonelink', style: 'font-size:13px;line-height:16px'});
  const p = add(c, 'p', {textContent: 'Read the '});
  add(p, 'a', {id: 'plant-tap-inline', href: '#/overview', textContent: 'tapinline'});
  p.append(' in a sentence.');
  p.id = 'plant-tap-p';
  // TASK-22d, tap-covered: two 50px buttons in a card that isn't a link, each under a transparent box; the second box
  // lets taps through (pointer-events: none)
  const k = [...document.querySelectorAll('#page .card')].find((x) => !x.closest('a[href], button')) || c;
  const w = add(k, 'div', {id: 'plant-cover', style: 'position:relative;display:flex;gap:12px'});
  for (const t of ['tapcovered', 'tapfree']) add(w, 'button', {type: 'button', textContent: t, style: 'width:50px;height:50px;padding:0;border:0;font-size:8px'});
  add(w, 'div', {style: 'position:absolute;left:0;top:0;width:50px;height:50px;z-index:5'});
  add(w, 'div', {style: 'position:absolute;left:62px;top:0;width:50px;height:50px;z-index:5;pointer-events:none'});
  return 1;
})()"""
TAP_UNPLANT_JS = ("(() => { ['plant-tap-style', 'plant-tap-small', 'plant-tap-hit', 'plant-tap-link', 'plant-tap-p', 'plant-cover']"
                  ".forEach((id) => { const e = document.getElementById(id); if (e) (id === 'plant-tap-link' ? e.parentElement : e).remove(); });"
                  " return 1; })()")


DARK_PLANT_JS = r"""(() => {
  const c = document.querySelector('#page .card');
  const imp = (css) => css.split(';').map((d) => d.trim()).filter(Boolean).map((d) => d.replace(/\s*!important$/, '') + ' !important').join(';');
  const arrow = 'linear-gradient(45deg, transparent 50%, #5a6275 50%), linear-gradient(135deg, #5a6275 50%, transparent 50%)';
  const add = (id, style, text) => { const s = Object.assign(document.createElement('select'), {id}); s.append(new Option(text));
    s.style.cssText = imp(`appearance:none;height:38px;width:160px;border:1px solid #cfd4de;border-radius:10px;${style}`); c.append(s); };
  add('plantdarkarrow', `background-color:#f7f8fb;background-image:${arrow}`, 'plantdarkarrow');   // the size reset: full box
  add('plantlightarrow', `background-color:#f7f8fb;background-image:${arrow};background-size:5px 5px,5px 5px;` +
    'background-repeat:no-repeat;background-position:calc(100% - 16px) 16px,calc(100% - 11px) 16px', 'plantlightarrow');
  add('plantdarkfill', 'background:#1d2331;color:#fff', 'plantdarkfill');
  c.append(Object.assign(document.createElement('span'), {id: 'plantdarkchip', textContent: 'plantdarkchip',
    style: imp('display:inline-block;padding:6px 10px;border-radius:99px;background:#171c28;color:#fff')}));
  c.append(Object.assign(document.createElement('button'), {id: 'plantlightbtn', type: 'button', className: 'btn',
    textContent: 'plantlightbtn'}));   // the light theme's primary button: black on purpose
  return 1;
})()"""
# TEST-SPEED, overflow on a phone: a 640px block on the page at 390 (html and body clip it there: see the check)
PHONE_PLANT_JS = ("(() => { const d = Object.assign(document.createElement('div'), {id: 'plant-phonewide', textContent: "
                  "'plantphonewide'}); d.style.cssText = 'display:block !important;width:640px !important;height:20px !important';"
                  " document.getElementById('page').append(d); return 1; })()")
PHONE_UNPLANT_JS = "(() => { const e = document.getElementById('plant-phonewide'); if (e) e.remove(); return 1; })()"
DARK_UNPLANT_JS = ("(() => { ['plantdarkarrow', 'plantlightarrow', 'plantdarkfill', 'plantdarkchip', 'plantlightbtn']"
                   ".forEach((id) => { const e = "
                   "document.getElementById(id); if (e) e.remove(); }); return 1; })()")


# rowname: two lists of two rows in a card, 220px wide. In the first, the long name wraps and its row is taller than its
# neighbour's (the plant); in the second, the same name is held to one line with an ellipsis (a correct control); a lone
# row whose name wraps is another (nothing beside it to be out of line with), and so is a `.li.wrap` list (a name there
# may wrap on purpose).
ROWNAME_PLANT_JS = r"""(() => {
  const c = document.querySelector('#page .card');
  const imp = (css) => css.split(';').map((d) => d.trim()).filter(Boolean).map((d) => d.replace(/\s*!important$/, '') + ' !important').join(';');
  const el = (tag, props, style, kids = []) => { const e = Object.assign(document.createElement(tag), props); e.style.cssText = imp(style); e.append(...kids); return e; };
  const row = (name, nameStyle) => el('div', {className: 'li'}, 'display:flex;align-items:center;width:220px;padding:6px 0;gap:8px',
    [el('span', {className: 'grow'}, 'display:block;flex:1 1 auto;min-width:0', [el('b', {textContent: name}, `display:block;font-size:15px;line-height:20px;${nameStyle}`)])]);
  const list = (id, a, b, style) => el('div', {id}, 'display:block', [row(a, style), ...(b ? [row(b, style)] : [])]);
  c.append(list('plant-rowname', 'plantrowshort', 'plantrowname wraps onto a second line', 'white-space:normal'),
           list('plant-rowname-ok', 'plantrowokshort', 'plantrowok stays on one line however long', 'white-space:nowrap;overflow:hidden;text-overflow:ellipsis'),
           list('plant-rowname-lone', 'plantrowlone wraps onto a second line alone', null, 'white-space:normal'),
           list('plant-rowname-wrap', 'plantrowwrapshort', 'plantrowwrap wraps on purpose in a wrap row', 'white-space:normal'));
  document.querySelectorAll('#plant-rowname-wrap > .li').forEach((r) => r.classList.add('wrap'));
  return 1;
})()"""
ROWNAME_UNPLANT_JS = ("(() => { ['plant-rowname', 'plant-rowname-ok', 'plant-rowname-lone', 'plant-rowname-wrap'].forEach((id) => { "
                      "const e = document.getElementById(id); if (e) e.remove(); }); return 1; })()")


# cutnum: three figures cut in a card on the Overview (the plants): one by an ellipsis, one by a one-line clamp, one by a
# box that clips it. And three correct controls: a merchant label with digits in it that ends in an ellipsis ("Store
# 4521" isn't a figure), a figure that stays whole at the start of a label cut after it, and figures in an ellipsis box
# wide enough for all of its text.
CUTNUM_PLANT_JS = r"""(() => {
  const c = document.querySelector('#page .card');
  const imp = (css) => css.split(';').map((d) => d.trim()).filter(Boolean).map((d) => d + ' !important').join(';');
  const box = (id, text, width, style) => { const e = Object.assign(document.createElement('div'), {id, textContent: text});
    e.style.cssText = imp(`display:block;width:${width}px;font-size:15px;line-height:20px;${style}`); return e; };
  const ELL = 'white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
  c.append(box('plant-cutnum-ell', 'plantcutell paid $1,234.56 today', 170, ELL),
           box('plant-cutnum-clamp', 'plantcutclamp the first line and then 42.5% below', 170,
               'display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:1;overflow:hidden'),
           box('plant-cutnum-clip', 'plantcutclip $98,765', 120, 'white-space:nowrap;overflow:hidden'),
           box('plant-cutnum-label', 'plantnumlabel Walgreens Store 4521 on Main Street', 170, ELL),
           box('plant-cutnum-fits', '$12.00 plantnumfits and a long label that gets cut', 170, ELL),
           box('plant-cutnum-whole', 'plantnumwhole 7-Eleven 34.5% of $1,200', 600, ELL));
  return 1;
})()"""
CUTNUM_UNPLANT_JS = ("(() => { document.querySelectorAll('[id^=plant-cutnum-]').forEach((e) => e.remove()); return 1; })()")

def selftest(b: Chrome, base: str, emit) -> None:
    """Plant one bug per new check on the Overview at 1440 (where the tiles sit side by side; TASK-20's tap check at 390,
    a phone, and TEST-SPEED's overflow there, where the page is clipped); each must be flagged, a correct control beside
    it must not be, and none may be left once the plants are gone. Raises AssertionError naming each check that failed
    and how (a plant missed, a control flagged)."""
    # TEST-SPEED: every plant goes into a settled page and is checked on a settled page (cdp.Chrome.settled): a plant put
    # into a page still being drawn is thrown away with it (under load the light-theme redraw below replaced the card the
    # dark plants were in, and the self-test "missed" them)
    unsettled: list[str] = []

    def settled(after: str) -> None:
        why = b.settled()
        if why:
            unsettled.append(f"{after}: {why}")
    b.size(1440, HEIGHT[1440])
    load(b, base, "overview")
    settled("load 1440")
    tiles = b.js(PLANT_JS)
    settled("plants")
    got = b.js(f"{CHECK_JS}('page')")["violations"]

    def hit(check, needle):
        return any(v["check"] == check and needle in json.dumps(v) for v in got)
    want = {"overflow": hit("overflow", "the page is wider"), "escape": hit("escape", "planted escape"),
            "cliptext": hit("cliptext", "planted clipped") and hit("cliptext", "Subscriptions monthly"),
            "wrapped": hit("wrapped", "planted long button"), "orphans": hit("orphans", '"29)"'),
            "rows": tiles >= 2 and hit("rows", "hero")}
    how: dict[str, str] = {}   # (TEST-SPEED) what each failing check did: a plant it missed, or a control it flagged

    def judge(check: str, seen: list[str], plants: tuple, controls: tuple = ()) -> None:
        missed = [n for n in plants if not any(n in v for v in seen)]
        flagged = [n for n in controls if any(n in v for v in seen)]
        want[check] = not missed and not flagged
        if missed or flagged:
            how[check] = (f"missed {missed}" if missed else "") + ("; " if missed and flagged else "") + \
                         (f"flagged its correct control(s) {flagged}" if flagged else "")
    b.js(UNPLANT_JS)
    settled("plants removed")  # the card packer re-lays the grid a frame or two after a card changes height (was 0.6 s)
    after = b.js(f"{CHECK_JS}('page')")["violations"]
    left = [v for v in after if any(x in json.dumps(v) for x in ("planted", "aaaaaaaa 29)", "Subscriptions monthly", "plant-"))]
    # rowname: the wrapped name taller than its neighbour is caught; the one held to one line, the lone row and the
    # .li.wrap row are not
    b.js(ROWNAME_PLANT_JS)
    settled("rowname plants")
    judge("rowname", [json.dumps(v) for v in b.js(f"{CHECK_JS}('page')")["violations"] if v["check"] == "rowname"],
          ("plantrowname wraps",), ("plantrowok", "plantrowlone", "plantrowwrap"))
    b.js(ROWNAME_UNPLANT_JS)
    settled("rowname plants removed")
    left += [v for v in b.js(f"{CHECK_JS}('page')")["violations"] if "plantrow" in json.dumps(v)]
    # cutnum: the figures cut by an ellipsis, a line clamp and a clipping box are caught; a label with digits that ends
    # in an ellipsis, a whole figure before a cut label and an ellipsis box that fits its text are not
    b.js(CUTNUM_PLANT_JS)
    settled("cutnum plants")
    judge("cutnum", [json.dumps(v) for v in b.js(f"{CHECK_JS}('page')")["violations"] if v["check"] == "cutnum"],
          ("plantcutell", "plantcutclamp", "plantcutclip"), ("plantnumlabel", "plantnumfits", "plantnumwhole"))
    b.js(CUTNUM_UNPLANT_JS)
    settled("cutnum plants removed")
    left += [v for v in b.js(f"{CHECK_JS}('page')")["violations"] if any(x in json.dumps(v) for x in ("plantcut", "plantnum"))]
    # TASK-20, tap (phones only): at 390 a planted 30px button and a lone small link are caught; the same button with a
    # transparent 44px ::before hit box, and a link inside a sentence, are not
    b.size(390, HEIGHT[390])
    load(b, base, "overview")
    settled("load 390")
    b.js(TAP_PLANT_JS)
    settled("tap plants")
    got390 = b.js(f"{CHECK_JS}('page')")["violations"]
    judge("tap", [json.dumps(v) for v in got390 if v["check"] == "tap"], ("tapsmall", "taplonelink"), ("taphit", "tapinline"))
    # TASK-22d, tap-covered: the button under the box is caught (wherever the card is: the page is scrolled to it); the
    # one under a box that lets taps through is not
    judge("tap-covered", [json.dumps(v) for v in got390 if v["check"] == "tap-covered"], ("tapcovered",), ("tapfree",))
    b.js(TAP_UNPLANT_JS)
    settled("tap plants removed")
    left += [v for v in b.js(f"{CHECK_JS}('page')")["violations"]
             if any(x in json.dumps(v) for x in ("tapsmall", "taphit", "taplonelink", "tapinline", "tapcovered", "tapfree"))]
    # TEST-SPEED, overflow on a phone (html and body overflow-x: hidden, so nothing scrolls): a 640px block is caught
    b.js(PHONE_PLANT_JS)
    settled("phone plant")
    judge("overflow@390", [json.dumps(v) for v in b.js(f"{CHECK_JS}('page')")["violations"] if v["check"] == "overflow"],
          ("plantphonewide",))
    b.js(PHONE_UNPLANT_JS)
    settled("phone plant removed")
    left += [v for v in b.js(f"{CHECK_JS}('page')")["violations"] if "plantphonewide" in json.dumps(v)]
    # HOTFIX-1, darkbg: in the light theme, a select whose arrow gradients cover the box and a field filled slate are
    # caught; the same select with its 5px arrow is not
    b.size(1440, HEIGHT[1440])
    load(b, base, "overview")
    settled("load light")
    b.js("window.HFTheme.set('light')")
    b.js(READY_NOW)   # the switch redraws the page (a new route: its answer, then the cards drawn again)...
    settled("theme")   # ...all of it before the plants go in
    b.js(DARK_PLANT_JS)
    settled("dark plants")
    judge("darkbg", [json.dumps(v) for v in b.js(f"{CHECK_JS}('page')")["violations"] if v["check"] == "darkbg"],
          ("plantdarkarrow", "plantdarkfill", "plantdarkchip"), ("plantlightarrow", "plantlightbtn"))
    blind = [k for k, ok in want.items() if not ok]
    b.js(DARK_UNPLANT_JS)
    settled("dark plants removed")
    left += [v for v in b.js(f"{CHECK_JS}('page')")["violations"] if "plantdark" in json.dumps(v) or "plantlight" in json.dumps(v)]
    b.js("localStorage.removeItem('hf.theme')")
    details = "; ".join(f"{k}: {how.get(k, 'missed its plant')}" for k in blind)
    assert not blind, f"layout gate self-test: these checks failed on their plants: {blind} ({details}) (unsettled: {unsettled})"
    assert not left, f"layout gate self-test: violations left after the plants were removed: {left[:3]} (unsettled: {unsettled})"
    emit(f"SELF  1440 overview               the {len(want)} ported checks each caught their planted bug "
         f"({', '.join(sorted(want))}); none left after removing them")


def annotate(b: Chrome, path: Path, rects: list[list[int]], scope: str) -> None:
    fixed = scope != "page"
    b.js(f"{OVERLAY}({json.dumps(rects)}, {json.dumps(fixed)})")
    if not fixed and rects:
        b.js(f"window.scrollTo(0, Math.max(0, {min(r[1] for r in rects)} - 80))")
    b.settled()
    b.screenshot(path, full=not fixed)
    b.js("document.getElementById('__audit_overlay').remove()")


def plan(label: str, widths, states: set[str] | None):
    """(width, theme, name, page, steps, scope) to run for a DB: every page at every width in both themes, the other
    states at the core widths in the dark theme (HOTFIX-1: and at 1440 and 390 in the light theme, where Settings' day
    select came out dark); on the sign-in DBs (label signin / signin-setup), their own states.
    TASK-20: at the extra phone widths (430, 360: the tap check's) the pages and the phone drawer, dark. TASK-21: the
    EVERY_WIDTH states at all seven widths in both themes."""
    table = AUTH_STATES.get(label, STATES)
    for width in widths:
        for theme in THEMES:
            for name, page, steps, scope, when in table:
                if states and name not in states:
                    continue
                if name in EVERY_WIDTH:  # TASK-21: every width, both themes
                    pass
                elif width in TAP_WIDTHS:
                    if theme != "dark" or (name not in EVERY and when != "phone"):
                        continue
                elif name not in EVERY and (width not in CORE_WIDTHS or (theme != "dark" and width not in LIGHT_WIDTHS)):
                    continue
                if (when == "phone" and width >= PHONE) or (when == "wide" and width < PHONE):
                    continue
                if (when == "dock" and width < DOCK) or (when == "nodock" and width >= DOCK):  # TASK-19
                    continue
                yield width, theme, name, page, steps, scope


def _state(b: Chrome, base: str, out: Path, label: str, item, inject: str | None) -> dict:
    """One planned state in browser `b`: its checks, a screenshot per failed check (taken in `b`, right after), and the
    lines the run prints for it."""
    width, theme, name, page, steps, scope = item
    key = name if theme == "dark" else f"{name}@{theme}"
    lines: list[str] = []
    try:
        res = run_state(b, base, width, name, page, steps, scope, inject, theme)
    except RuntimeError as e:
        if "nothing to click" in str(e) or "never appeared" in str(e):
            lines.append(f"SKIP  {width:>4} {key:<28} (not on this DB: {str(e).split(': ', 2)[-1][:70]})")
            return {"width": width, "key": key, "entry": {"skipped": str(e)[:200]}, "lines": lines, "pass": 0, "fail": 0}
        raise
    entry = {"scope": scope, "theme": theme, "seen": res["seen"], "checks": {}}
    if res["unsettled"]:  # TEST-SPEED: checked anyway (as the fixed waits did), and said
        entry["unsettled"] = res["unsettled"]
        lines.append(f"NOTE  {width:>4} {key:<28} not settled: {'; '.join(res['unsettled'])[:200]}")
    if res["problems"]:
        res["violations"].append({"check": "overlays", "rule": "JS error / CSP violation on the page", "what": res["problems"],
                                  "rects": [], "detail": {}})
    npass = nfail = 0
    for check in CHECKS:
        vs = [v for v in res["violations"] if v["check"] == check]
        entry["checks"][check] = {"pass": not vs, "examined": res["seen"].get(check, 0), "violations": vs}
        if vs:
            nfail += 1
            shot = out / f"fail-{label}-{width}-{key.replace('@', '-')}-{check}.png"
            annotate(b, shot, [r for v in vs for r in v["rects"]][:60], scope)
            entry["checks"][check]["screenshot"] = str(shot.relative_to(out.parent) if out.parent in shot.parents else shot)
            lines.append(f"FAIL  {width:>4} {key:<28} {check:<9} {len(vs)} problem(s), screenshot {shot.name}")
            for v in vs[:12]:
                if check == "tap-covered":  # TASK-22d: FAIL tap-covered: <control> under <covering element>
                    lines.append(f"        FAIL tap-covered: {v['what'][0][:150]} under {v['what'][1][:150]}  {json.dumps(v['detail'])}")
                    continue
                lines.append(f"        - {v['rule']}: {' | '.join(map(str, v['what']))[:230]}  {json.dumps(v['detail'])[:200]}")
            if len(vs) > 12:
                lines.append(f"        ... and {len(vs) - 12} more")
        else:
            npass += 1
            lines.append(f"PASS  {width:>4} {key:<28} {check:<9} ({res['seen'].get(check, 0)} examined)")
    return {"width": width, "key": key, "entry": entry, "lines": lines, "pass": npass, "fail": nfail}


def audit(base: str, out: Path, label: str = "site", widths=WIDTHS, log=None, states: set[str] | None = None,
          inject: str | None = None, themes=THEMES, self_test: bool = True, workers: int = WORKERS) -> dict:
    """Every planned state of `label` (plan()), each a fresh page load, over `workers` headless Chromes (audit_many)."""
    return audit_many({label: base}, out, widths, {label: log} if log else None, states, inject, themes, self_test,
                      workers)[label]


def audit_many(sites: dict[str, str], out: Path, widths=WIDTHS, logs: dict | None = None, states: set[str] | None = None,
               inject: str | None = None, themes=THEMES, self_test=True, workers: int = WORKERS,
               select=None) -> dict[str, dict]:
    """Every planned state of every site ({label: base URL}: one per DB), each a fresh page load. TASK-20: the states are
    shared out over `workers` headless Chromes (each its own profile, so a device setting such as the theme never leaks
    between them; each site its own origin). TEST-SPEED: one queue for all the sites (it was one run per DB, each with
    its share of the Chromes), so no Chrome idles while another DB still has states; the sites take turns in it, so
    their servers share the load. Each site's self-test (not the sign-in DBs') is in the queue first; one that fails
    stops the run. The report and the printed lines keep each site's plan order, its self-test's line first. Returns
    {label: report}. self_test: True (every site but the sign-in DBs), False, or the labels to self-test (scripts/check.py
    fast: one; the checks are the same code on every DB). select(label, width, theme, name, page) -> bool: only those
    states (scripts/check.py fast: the ones a change can affect)."""
    logs = logs or {}
    emits = {label: logs.get(label) or (lambda line: print(line, flush=True)) for label in sites}
    out.mkdir(parents=True, exist_ok=True)
    reports = {label: {"base_url": base, "label": label, "widths": {}, "fail": 0, "pass": 0, "selftest": None}
               for label, base in sites.items()}
    plans = {label: [it for it in plan(label, widths, states) if it[1] in themes
                     and (select is None or select(label, it[0], it[1], it[2], it[3]))] for label in sites}
    results: dict[str, list] = {label: [None] * len(items) for label, items in plans.items()}
    self_lines: dict[str, list[str]] = {label: [] for label in sites}
    tested = set(self_test) if isinstance(self_test, (set, list, tuple)) else set(sites) if self_test else set()
    queue: list[tuple[str, int | None]] = [(label, None) for label in sites if label in tested and label not in AUTH_STATES]
    longest = max((len(items) for items in plans.values()), default=0)
    queue += [(label, i) for i in range(longest) for label in sites if i < len(plans[label])]   # the sites take turns
    lock, stop, nxt = threading.Lock(), threading.Event(), iter(range(len(queue)))

    def take():
        with lock:
            return None if stop.is_set() else next(nxt, None)

    def run(b: Chrome, k: int) -> None:
        label, i = queue[k]
        try:
            if i is None:
                selftest(b, sites[label], self_lines[label].append)
                reports[label]["selftest"] = "ok"
            else:
                results[label][i] = _state(b, sites[label], out, label, plans[label][i], inject)
        except BaseException:
            stop.set()  # a blind self-test, a gate bug or a dead browser stops the run
            raise

    def work() -> None:
        k = take()
        if k is None:   # nothing left for this Chrome: don't start it
            return
        with Chrome() as b:
            while k is not None:
                run(b, k)
                k = take()

    n = max(1, min(workers, len(queue)))
    if n == 1:
        # Preserve caller-thread slot ownership for nested browser audits. Moving
        # this acquisition to a pool thread hides the held slot from cdp's
        # deadlock detector and can wait forever when other workers do the same.
        work()
    else:
        with ThreadPoolExecutor(n) as pool:
            for f in [pool.submit(work) for _ in range(n)]:
                f.result()
    path = out / "layout-report.json"
    for label, report in reports.items():
        emit = emits[label]
        for line in self_lines[label]:
            emit(line)
        for r in results[label]:
            report["widths"].setdefault(str(r["width"]), {})[r["key"]] = r["entry"]
            for line in r["lines"]:
                emit(line)
            report["pass"] += r["pass"]
            report["fail"] += r["fail"]
        with REPORT_LOCK:  # several labels (DBs) may be audited at once into one report
            merged = json.loads(path.read_text()) if path.exists() else {}
            merged[label] = report
            path.write_text(json.dumps(merged, indent=1))
        emit(f"\n[{label}] layout: {report['pass'] + report['fail']} state checks, {report['pass']} PASS, {report['fail']} FAIL "
             f"(report: {path})")
    return reports


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("base_url")
    ap.add_argument("--out", type=Path, default=ROOT / "shots" / "011")
    ap.add_argument("--label", default="site")
    ap.add_argument("--widths", default=",".join(map(str, WIDTHS)))
    ap.add_argument("--states", help="only these states (comma-separated names)")
    ap.add_argument("--inject-css", type=Path, help="a stylesheet added after each page loads")
    ap.add_argument("--themes", default=",".join(THEMES))
    ap.add_argument("--workers", type=int, default=WORKERS, help="headless Chromes sharing the states")
    args = ap.parse_args(argv)
    rep = audit(args.base_url.rstrip("/"), args.out, args.label, tuple(int(w) for w in args.widths.split(",")),
                states=set(args.states.split(",")) if args.states else None,
                inject=args.inject_css.read_text() if args.inject_css else None, themes=tuple(args.themes.split(",")),
                workers=args.workers)
    return 1 if rep["fail"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

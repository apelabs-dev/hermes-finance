"""Accessible explanation disclosure and calendar identity, on synthetic data only.
Run through scripts/check.py's safe_env; no provider calls or production writes.
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
WIDTHS = [360, 390, 430, 768, 1024, 1280, 1440]


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("density") / "finance.db"
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
    b.goto(f"{site.url}/?density={time.time()}#/{page}")
    b.settle()
    b.js("new Promise(r => setTimeout(r, 200))")


def tap(b, selector):
    box = b.js(f"(() => {{ const e=document.querySelector({json.dumps(selector)}); e.scrollIntoView({{block:'center'}}); const r=e.getBoundingClientRect(); return {{x:r.x+r.width/2,y:r.y+r.height/2}}; }})()")
    b.call("Input.dispatchMouseEvent", type="mousePressed", button="left", clickCount=1, **box)
    b.call("Input.dispatchMouseEvent", type="mouseReleased", button="left", clickCount=1, **box)
    b.js("new Promise(r => setTimeout(r, 150))")


def key(b, value):
    b.call("Input.dispatchKeyEvent", type="keyDown", key=value, code=value, windowsVirtualKeyCode={'Enter':13,'Escape':27,'Tab':9,' ':32}[value], text='\r' if value == 'Enter' else '')
    b.call("Input.dispatchKeyEvent", type="keyUp", key=value, code=value, windowsVirtualKeyCode={'Enter':13,'Escape':27,'Tab':9,' ':32}[value])
    b.js("new Promise(r => setTimeout(r, 100))")


def clean(b):
    assert b.problems() == []
    assert b.js("document.documentElement.scrollWidth <= innerWidth + 1")
    assert b.js("[...document.querySelectorAll('.help-trigger,.bchip,.agi')].every(e=>!e.parentElement.closest('a,button'))")


def test_calendar_same_name_and_repeated_occurrence_use_exact_key(chrome, site):
    b = chrome
    go(b, site, "recurring", 1440)
    # Change display labels only. The true keys still go to the real synthetic server.
    b.js("""window.__lookups=[]; window.__writes=[]; const orig=window.fetch;
      window.fetch=async (...args)=>{const u=new URL(args[0],location.href);
        if(u.pathname==='/api/bills/lookup') __lookups.push(u.searchParams.get('key'));
        if((args[1]?.method || 'GET')!=='GET') __writes.push(u.pathname);
        const res=await orig(...args);
        if(u.pathname!=='/api/bills-calendar') return res;
        const d=await res.json(); const unique=[...new Map(d.bills.map(x=>[x.key,x])).values()];
        if(unique.length<2) throw Error('fixture needs distinct bill identities');
        d.bills=[{...unique[0],merchant:'Same display name'}, {...unique[1],merchant:'Same display name'},
          {...unique[0],merchant:'Same display name',date:d.month+'-28',projected:true}];
        // The calendar lays chips out by date; keep the fixture in that order so chip i is bill i whatever today is
        d.bills.sort((a,b)=>a.date<b.date?-1:a.date>b.date?1:0);
        window.__expected=d.bills.map(x=>({key:x.key,date:x.date}));
        return new Response(JSON.stringify(d),{headers:{'Content-Type':'application/json'}});};""")
    b.js("document.querySelector('.calnav button:not([disabled])').click()")
    b.js("new Promise(r=>setTimeout(r,350))")
    expected = b.js("__expected")
    for i in range(3):
        sel = f".bchip:nth-of-type({i + 1})"  # resolve exact occurrence below, independent of day cell position
        b.js(f"window.__entry=[...document.querySelectorAll('.bchip')][{i}]; __entry.focus()")
        assert b.js("__entry.tagName") == "BUTTON", "calendar entries must be keyboard-operable detail buttons"
        assert b.js("document.activeElement===__entry"), b.js("({active:document.activeElement.outerHTML, visible:__entry.getClientRects().length, errors:window.__qaErrors})")
        key(b, "Enter")
        b.js("new Promise(r=>setTimeout(r,250))")
        assert b.js("__lookups.at(-1)") == expected[i]["key"]
        assert b.js("document.querySelector('#modal').classList.contains('on')")
        assert expected[i]["date"] == b.js("document.querySelector('.bill-occurrence').dataset.date")
        assert b.js("document.querySelector('.bill-occurrence').textContent")
        assert b.js("document.querySelector('.billsheet').dataset.billKey") == expected[i]["key"]
        key(b, "Escape")
        assert b.js("document.activeElement===__entry")
    assert b.js("__writes") == [], "opening a calendar detail must not mark paid or mutate"
    clean(b)


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_due_details_bounded_and_main_amounts_visible(chrome, site, width, theme):
    b = chrome
    go(b, site, "overview", width)
    b.js(f"HFTheme.set({json.dumps(theme)})")
    assert b.js("document.documentElement.dataset.theme") == theme
    # A non-round amount and unbounded names/list, through the actual overview caller.
    b.js("""const original=window.fetch; window.fetch=async (...args)=>{
      const r=await original(...args); if(!String(args[0]).includes('/api/overview')) return r;
      const d=await r.json(); const list=Array.from({length:60},(_,i)=>({merchant:'A very long synthetic merchant name '+i,
        amount:17.43,next_expected:d.summary.month+'-25',variable:i===0}));
      d.pace.bills_due=list; d.pace.bills_due_total=1045.8; d.pace.one_offs=[];
      d.budgets.bills_due=list; d.budgets.total_due=1045.8;
      return new Response(JSON.stringify(d),{headers:{'Content-Type':'application/json'}});};""")
    b.js("location.hash='#/transactions'")
    b.settle()
    b.js("location.hash='#/overview'")
    b.settle()
    sel = "[data-help='pace']"
    # (wait for the overview's redraw with the patched data, not a fixed 300 ms: slow-mode run 3 lost that race)
    assert b.until(f"!!document.querySelector({json.dumps(sel)})"), "pace needs a readable details disclosure"
    tap(b, sel)
    assert b.js("document.querySelector('.help-panel').getAttribute('role')") == "dialog"
    assert b.js("document.querySelectorAll('.help-panel .help-bill').length") == 60
    assert b.js("document.querySelector('.help-panel').textContent.includes('~')"), "variable estimate remains qualified"
    assert b.js("(() => {const e=document.querySelector('.help-panel'),r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight&&e.scrollHeight>e.clientHeight;})()")
    key(b, "Escape")
    assert b.js(f"document.activeElement===document.querySelector({json.dumps(sel)})")
    assert b.js("!document.querySelector('.help-panel')")
    clean(b)


def test_short_help_focus_touch_outside_and_before_data_action(chrome, site):
    b = chrome
    b.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
    go(b, site, "settings", 390)
    assert b.js("matchMedia('(pointer: coarse)').matches")
    # The Data card carries ONE explanation, in its header; it is reachable before any action and never attached to the
    # download link. Its panel covers export, backup and import.
    b.js("document.querySelector('[data-help=data]').focus()")
    key(b, "Enter")
    assert b.js("document.querySelector('.help-panel').textContent.includes('without bank tokens or sign-in secrets')")
    assert b.js("document.querySelector('.help-panel').textContent.includes('reconnect each bank')")
    assert b.js("document.querySelectorAll('.data-actions .help-trigger').length") == 0
    key(b, "Escape")
    assert b.js("document.activeElement.dataset.help") == "data"
    tap(b, "[data-help=data]")
    assert b.js("document.querySelector('.help-panel').getAttribute('role')") == "dialog"
    assert b.js("document.querySelector('[data-help=data]').getAttribute('aria-controls')===document.querySelector('.help-panel').id")
    b.call("Input.dispatchMouseEvent", type="mousePressed", button="left", clickCount=1, x=2, y=2)
    b.call("Input.dispatchMouseEvent", type="mouseReleased", button="left", clickCount=1, x=2, y=2)
    b.js("new Promise(r => setTimeout(r,100))")
    assert b.js("!document.querySelector('.help-panel')")
    b.call("Emulation.setTouchEmulationEnabled", enabled=False)
    go(b, site, "settings", 1440)
    b.call('Page.bringToFront')
    # The Data explanation is a dialog: it opens on Enter (a long panel never opens on focus alone), then closes.
    b.js("document.querySelector('[data-help=data]').focus()")
    key(b, "Enter")
    assert b.js("!!document.querySelector('.help-panel')"), b.js("({focused:document.hasFocus(),active:document.activeElement.dataset.help})")
    key(b, "Escape")
    assert b.js("document.activeElement.dataset.help") == "data"
    clean(b)


def _header_triggers(b):
    return b.js(r"""(() => {
      const out = [];
      for (const c of document.querySelectorAll('#page .card')) {
        if (!c.querySelector('h3')) continue;
        const all = [...c.querySelectorAll('.help-trigger')];
        if (!all.length) continue;
        const inHead = [...c.querySelectorAll('h3 .help-trigger')];
        const body = all.filter((t) => !c.querySelector('h3').contains(t));
        const cr = c.getBoundingClientRect(), hr = c.querySelector('h3').getBoundingClientRect();
        out.push({title: (c.querySelector('h3 .ttl') || {}).textContent, all: all.length, inHead: inHead.length,
          gaps: inHead.map((t) => Math.round(t.getBoundingClientRect().top - hr.bottom)),
          lefts: inHead.map((t) => Math.round(t.getBoundingClientRect().left)), cardRight: Math.round(cr.right),
          cardLeft: Math.round(cr.left),
          bodyLabelled: body.map((t) => ({labelled: t.classList.contains('labelled'),
            floatsRight: t.getBoundingClientRect().left > cr.left + cr.width * 0.6,
            txt: (t.textContent || '').trim().slice(0, 24)}))});
      }
      return out;
    })()""")


@pytest.mark.parametrize("page", ["overview", "settings"])
def test_card_help_triggers_anchor_to_the_header(chrome, site, page):
    """The bug the owner reported: every card-level trigger floated far from its title (the body span pushed it to the
    card's right edge). Each card now carries at least one trigger in the header, on the title line, left of the card's
    right edge.

    A *second*, labelled ("Budget breakdown") trigger does NOT go in a header: at ~140px it does not fit beside the
    icon badge, the title and the icon trigger, so h3 wraps, the header grows a line and this card's hero number drops
    out of line with the cards beside it (84 layout failures). That one stays in the body. So the rule is "at least one
    in the header", not "all of them" — and the header's own single-line height is what keeps the row aligned.
    """
    b = chrome
    go(b, site, page, 1440)
    cards = _header_triggers(b)
    assert cards, f"{page} has cards with help triggers"
    for c in cards:
        assert c["inHead"] >= 1, (page, c, "no trigger is anchored in the header")
        assert all(g <= 2 for g in c["gaps"]), (page, c, "the trigger is not on the title line")
        assert all(l < c["cardRight"] - 80 for l in c["lefts"]), (page, c, "the trigger is pushed to the right edge")
        # any trigger left in the body is a labelled one, and it must be next to the text it explains, not floating
        assert all(x['labelled'] for x in c['bodyLabelled']), (page, c, 'a bare trigger sits in the card body')
        assert all(not x["floatsRight"] for x in c["bodyLabelled"]), (page, c, "a body trigger is pushed to the card's right edge")


@pytest.mark.parametrize("page", ["overview", "budgets", "accounts", "debt", "recurring"])
def test_card_headers_stay_one_line(chrome, site, page):
    """A card header is a fixed-height strip. If it grows a line, the hero number, bar and first detail line below it
    drop out of line with the cards beside it. The layout gate's `headers` check covers this app-wide; this pins the
    specific cards that were measured breaking, so the cause is named if it ever comes back."""
    b = chrome
    for w in (1440, 1280, 1024):
        go(b, site, page, w)
        rows = b.js(r"""(() => {
          const out = [];
          for (const c of document.querySelectorAll('#page .card')) {
            const h3 = c.querySelector(':scope > h3'); if (!h3) continue;
            const kids = [...h3.children].filter((e) => e.getBoundingClientRect().height);
            if (kids.length < 2) continue;
            const bands = [];
            for (const e of kids.sort((a, b2) => a.getBoundingClientRect().top - b2.getBoundingClientRect().top)) {
              const r = e.getBoundingClientRect();
              const band = bands.find((x) => r.top < x.bottom - 2 && r.bottom > x.top + 2);
              if (band) band.bottom = Math.max(band.bottom, r.bottom);
              else bands.push({top: r.top, bottom: r.bottom});
            }
            out.push({title: (h3.querySelector('.ttl') || {}).textContent, bands: bands.length,
                      h: Math.round(h3.getBoundingClientRect().height),
                      strip: Math.round(parseFloat(getComputedStyle(h3).minHeight) || 0),
                      sideBySide: [...document.querySelectorAll('.card, .frame')].some((o) => {
                        if (o === c) return false;
                        const a = o.getBoundingClientRect(), b3 = c.getBoundingClientRect();
                        return Math.abs(a.top - b3.top) <= 2 && (b3.left >= a.right - 1 || a.left >= b3.right - 1);
                      })});
          }
          return out;
        })()""")
        for r in rows:
            # The rule is that a header never grows past its own declared strip: that is what keeps the hero number
            # below it in line with the cards beside it. (Measured before P2A: the "No budget yet" card sat beside two
            # budget cards at 1280, so its 55px header against their 32px ones really was a ragged row.) Same rule as
            # the gate's `headers` check.
            assert not r["sideBySide"] or r["bands"] <= 1 or r["h"] - r["strip"] <= 2, (page, w, r, "a wrapped neighboring card header exceeds its declared strip")
            # Calendar navigation may wrap in its own full-width frame. Figure-bearing peer cards may not.
            if r["title"] in ("Left to spend", "Spent in October", "On pace for", "Bills", "Subscriptions"):
                assert r["bands"] == 1, (page, w, r, "an Overview hero card's header wraps onto more than one line")


def test_account_row_help_triggers_anchor_left(chrome, site):
    """Accounts rows: the disclosure was a second grid column, floated to the card's right (x~791/1351). It now sits at
    the row's left, next to the account name (or leads a row that is itself a button)."""
    b = chrome
    go(b, site, "accounts", 1440)
    rows = b.js(r"""(() => {
      const out = [];
      for (const e of document.querySelectorAll('#page .account-entry')) {
        const t = e.querySelector('.help-trigger');
        if (!t) continue;
        const er = e.getBoundingClientRect(), tr = t.getBoundingClientRect();
        out.push({title: (e.closest('.card').querySelector('h3 .ttl') || {}).textContent,
          off: Math.round(tr.left - er.left), first: e.firstElementChild === t, inGrow: !!t.closest('.grow')});
      }
      return out;
    })()""")
    assert rows, "the features DB renders account entries"
    for r in rows:
        assert r["off"] < 300, (r, "the row trigger floated to the right")
        assert r["inGrow"] or r["first"], (r, "the row trigger is not anchored to the row's left")


def test_manual_loan_freshness_remains_visible(chrome, tmp_path):
    """Dates are status facts, not explanatory prose to hide in a disclosure."""
    from hermes_finance import db, manual
    path = tmp_path / "freshness.db"
    conn = db.connect(path)
    manual.create(conn, {"kind": "loan", "subtype": "personal", "name": "Freshness loan", "institution": "Fixture lender",
                         "balance": 500, "original_amount": 1000, "monthly_payment": 100}, date.today())
    conn.close()
    srv = Server(path, today=date.today())
    try:
        go(chrome, srv, "accounts", 1440)
        row = chrome.js("""[...document.querySelectorAll('#page .account-entry')]
          .find(e => e.textContent.includes('Freshness loan'))?.innerText""")
        assert row and "updated" in row.lower(), row
        clean(chrome)
    finally:
        srv.close()


def test_budget_planned_buttons_align_down_the_column(chrome, site):
    """The Planned buttons (the Edit control) must line up in one column, whatever else each row does or doesn't have.

    Rewritten for P2A (the Budgets page is a table, not a grid of cards): the old test lined neighbouring cards' Edit
    buttons up at one Y. A table row has the same promise on the other axis: every Planned / Spent / Left cell ends on
    one X, with or without bills due (which still decides whether a row's Details has a Bills section).
    """
    for width in (1440, 1280, 1024):
        go(chrome, site, 'budgets', width)
        rows = chrome.js("""[...document.querySelectorAll('#page tr.c')].map(c => {
          const q = (s) => c.querySelector(s).getBoundingClientRect();
          return {name: c.dataset.category, due: Number(c.dataset.due) > 0,
                  plan: q('.plan').right, spent: q('.act').right, left: q('.rp').right};
        })""")
        print(json.dumps({'width': width, 'rows': rows}))
        assert any(r['due'] for r in rows) and any(not r['due'] for r in rows), rows
        for key in ('plan', 'spent', 'left'):
            xs = [r[key] for r in rows]
            assert max(xs) - min(xs) <= 2, (width, key, rows)


@pytest.mark.parametrize('width', [360, 1024])
def test_recent_transaction_category_and_date_are_not_clipped(chrome, site, width):
    go(chrome, site, 'overview', width)
    result = chrome.js("""(() => {
      const row=document.querySelector('.txgrid .li'), meta=row.querySelector('.grow > span');
      row.querySelector('.amt').textContent='$2,252.00';
      meta.lastChild.textContent='Rent & Utilities · Oct\\u00a02';
      const r=document.createRange(); r.selectNodeContents(meta);
      const box=meta.getBoundingClientRect(), lines=[...r.getClientRects()];
      return {text:meta.textContent, width:box.width, scroll:meta.scrollWidth,
              over:Math.max(0,...lines.map(l=>l.right-box.right))};
    })()""")
    assert result['over'] <= 1 and result['scroll'] <= result['width'] + 1, result

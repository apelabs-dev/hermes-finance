"""Replayable synthetic window acceptance and the sixteen approved-state screenshots.

Run with the repo CDP helper, HF_CHROMES=2 and provider switches off. Screenshots
are written under .dispatch/out/shots; no fixture database is retained.
"""
from datetime import date, timedelta
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import factory as f
import gate_dbs
from cdp import Chrome, Server, find_chrome
from hermes_finance import db, txmeta

pytestmark = pytest.mark.skipif(find_chrome() is None, reason='Chrome is not installed')
TODAY = date(2026, 10, 21)
OUT = Path(__file__).resolve().parents[1] / '.dispatch/out'


@pytest.fixture(scope='module')
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp('txwindows') / 'synthetic.sqlite'
    gate_dbs.build('features', path, TODAY)
    conn = db.connect(path)
    f.bank(conn); f.acct(conn)
    # Dense all-time history tests list limits and full-result summary independently.
    for i in range(1700):
        f.tx(conn, (TODAY - timedelta(days=i % 210)).isoformat(), 1.25, 'Window merchant',
             cat='Groceries', tid=f'window-{i}')
    txmeta.save_filter(conn, 'Window groceries', {'category': ['Groceries'], 'q': 'Window'})
    conn.close()
    srv = Server(path, today=TODAY)
    assert srv.port != 8765
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT/'browser-servers.jsonl').open('a') as log:
        log.write(json.dumps({'event': 'start', 'port': srv.port})+'\n')
    try:
        yield srv
    finally:
        srv.close()
        with (OUT/'browser-servers.jsonl').open('a') as log:
            log.write(json.dumps({'event': 'stop', 'port': srv.port})+'\n')


@pytest.fixture(scope='module')
def chrome():
    with Chrome() as b:
        b.call('Page.addScriptToEvaluateOnNewDocument', source="""
          { const RealDate = Date; const now = new RealDate(2026, 9, 21, 12).getTime();
            window.Date = class extends RealDate {
              constructor(...args) { super(...(args.length ? args : [now])); }
              static now() { return now; }
            }; }
        """)
        yield b


def wait(b):
    b.settle()
    b.js('new Promise(r => setTimeout(r, 450))')


def go(b, site, route='transactions', width=1280, theme='dark'):
    b.size(width, 900 if width > 800 else 844)
    b.goto(f'{site.url}/#/{route}')
    b.js(f"document.documentElement.dataset.theme = document.body.dataset.theme = {json.dumps(theme)}")
    wait(b)


def click(b, selector):
    b.js(f'document.querySelector({json.dumps(selector)}).click()')
    wait(b)


def search(b, value):
    b.js(f"(() => {{const q=document.querySelector('#page .search'); q.value={json.dumps(value)}; q.dispatchEvent(new Event('input',{{bubbles:true}}));}})()")
    wait(b)


def label(b):
    return b.js("document.querySelector('#monl').textContent")


def label_offset(b):
    """Pixels between the centre of the label's content (text + chevron) and the centre of the whole pill."""
    return b.js("""(() => {const m=document.querySelector('#mon').getBoundingClientRect();
      const r=document.createRange(); r.selectNodeContents(document.querySelector('#monl'));
      const c=r.getBoundingClientRect(); return Math.abs((c.left+c.right)/2-(m.left+m.right)/2);})()""")


def window(b, value):
    click(b, '#monl')
    click(b, f'.rgpicker [aria-label="{value}"]')


def summary_parity(b, query):
    d = b.js(f"fetch('/api/transactions?{query}').then(r=>r.json())")
    text = b.js("document.querySelector('.rgsummary').textContent")
    assert text.startswith(f"{d['total']:,} payment") or text.startswith(f"{d['total']} payment"), text
    # Compare the complete rendered money string using the page's established locale sign semantics.
    want = b.js(f"(() => {{const n={d['sum']};return (n<0?'+':'')+new Intl.NumberFormat('en-US',{{style:'currency',currency:'USD'}}).format(Math.abs(n));}})()")
    assert want.removesuffix('.00') in text, (want, text)
    assert b.js("document.querySelectorAll('.tx tr[data-tid]').length") == min(50, d['total'])
    return d


@pytest.mark.parametrize('width', [390, 1280])
@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_approved_states_and_screenshots(site, chrome, width, theme):
    b = chrome
    shots = OUT/'shots'; shots.mkdir(exist_ok=True)
    go(b, site, width=width, theme=theme)
    assert label(b) == 'October 2026'
    click(b, '#monl')
    assert b.js("document.querySelectorAll('.rgpicker .rgm').length") == 12
    assert b.js("[...document.querySelectorAll('.rgpicker input')].map(x=>x.type)") == ['date', 'date']
    bounds = b.js("(() => {let r=document.querySelector('.rgpicker').getBoundingClientRect();return [r.left,r.right,r.bottom,innerWidth,innerHeight]})()")
    assert bounds[0] >= 0 and bounds[1] <= bounds[3] and bounds[2] <= bounds[4]
    if width == 390:
        assert bounds[2] == bounds[4]
        assert b.js("getComputedStyle(document.querySelector('#rgfrom')).fontSize") == '16px'
    assert b.js("""[...document.querySelectorAll('.rgpicker button,.rgpicker input')].every(e=>{
      const r=e.getBoundingClientRect(); const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      return r.width>=44 && r.height>=44 && (hit===e || e.contains(hit));
    })""")
    b.screenshot(shots/f'{width}-{theme}-sheet.png')
    click(b, '.rgpicker [aria-label="Last 30 days"]')
    assert label(b) == 'Last 30 days · Sep 22 – Oct 21'
    assert b.js("document.querySelector('#mprev').hidden && document.querySelector('#mnext').hidden")
    d = summary_parity(b, 'since=30d')
    assert b.js("document.querySelectorAll('.tx tr.mhd').length") >= 1
    assert d['period']['start'] == '2026-09-22'
    if width == 390:
        assert label_offset(b) <= 2, label_offset(b)
    b.screenshot(shots/f'{width}-{theme}-30day.png')
    click(b, '#rgclear'); search(b, 'Window')
    assert label(b) == 'All time · searching'
    if width == 390:
        assert label_offset(b) <= 2, label_offset(b)
    summary_parity(b, 'q=Window')
    b.screenshot(shots/f'{width}-{theme}-search.png')
    click(b, '#monl')
    b.js("document.querySelector('#rgfrom').value='2026-01-01'; document.querySelector('#rgto').value='2026-10-21'")
    click(b, '.rgapply')
    note = b.js("document.querySelector('.rgnote').textContent")
    coverage = b.js("fetch('/api/transactions').then(r=>r.json()).then(d=>d.coverage_month)")
    mon = date.fromisoformat(coverage+'-01').strftime('%b %Y')
    assert note == f'Some banks are missing before {mon}, so older totals look low.'
    assert b.js("!document.querySelector('.rgnote').hidden")
    b.screenshot(shots/f'{width}-{theme}-custom.png')
    assert b.js('document.documentElement.scrollWidth <= innerWidth')
    assert b.problems() == []



@pytest.mark.parametrize('width', [390, 1280])
@pytest.mark.parametrize('flow', ['month_then_search', 'range_x_then_search', 'search_x_clear_retype'])
def test_search_restarts_after_month_context(site, chrome, width, flow):
    b = chrome
    go(b, site, width=width)
    month = 'October 2026'
    if flow == 'month_then_search':
        click(b, '#monl'); click(b, '.rgpicker .rgm:nth-child(9)')
        month = 'September 2026'
    elif flow == 'range_x_then_search':
        window(b, 'Last 30 days'); click(b, '#rgclear')
    else:
        search(b, 'Window'); assert label(b) == 'All time · searching'
        b.requests()
        click(b, '#rgclear')
        assert label(b) == month
        assert b.js("document.querySelector('#page .search').value") == 'Window'
        scoped = [parse_qs(urlsplit(u).query) for _, u in b.requests() if '/api/transactions?' in u]
        assert scoped and all(q.get('month') == ['2026-10'] and q.get('q') == ['Window'] for q in scoped)
        search(b, '')
    assert label(b) == month
    b.requests()
    search(b, 'Window')
    assert label(b) == 'All time · searching'
    calls = [parse_qs(urlsplit(u).query) for _, u in b.requests() if '/api/transactions?' in u]
    assert calls and all(q.get('q') == ['Window'] and not {'month', 'since', 'until'} & q.keys() for q in calls)
    assert 'txmonth=' not in b.js('location.hash')
    assert summary_parity(b, 'q=Window')['total'] == 1700
    search(b, ''); assert label(b) == month
    assert b.problems() == []


def test_search_context_history_refresh_and_shared_links(site, chrome):
    b = chrome
    go(b, site, 'transactions?month=2026-09')
    search(b, 'Window'); assert label(b) == 'All time · searching'
    search(b, ''); assert label(b) == 'September 2026'
    search(b, 'Window'); click(b, '#rgclear')
    assert label(b) == 'September 2026'
    assert b.js("document.querySelector('#page .search').value") == 'Window'
    b.js('history.back()'); wait(b); assert label(b) == 'All time · searching'
    b.js('history.forward()'); wait(b); assert label(b) == 'September 2026'
    window(b, 'Last 7 days'); assert label(b) == 'Last 7 days · Oct 15 – Oct 21'
    b.requests()
    search(b, 'merchant'); assert 'Last 7 days' in label(b)
    scoped = [parse_qs(urlsplit(u).query) for _, u in b.requests() if '/api/transactions?' in u]
    assert scoped and all(q.get('since') == ['7d'] and 'month' not in q for q in scoped)
    summary_parity(b, 'since=7d&q=merchant')
    saved = b.js('location.href'); b.goto(saved); wait(b); assert 'Last 7 days' in label(b)
    search(b, ''); assert 'Last 7 days' in label(b)
    click(b, '#rgclear'); assert label(b) == 'September 2026'
    window(b, 'All time'); summary_parity(b, '')
    search(b, 'Window'); assert label(b) == 'All time'
    search(b, ''); assert label(b) == 'All time'
    click(b, '#rgclear'); assert label(b) == 'September 2026'
    go(b, site); assert label(b) == 'October 2026'
    click(b, '#mprev'); assert label(b) == 'September 2026'
    click(b, '#mnext'); assert label(b) == 'October 2026'
    click(b, '#monl'); click(b, '.rgpicker [aria-label="Previous year"]')
    assert b.js("document.querySelector('.rgyr b').textContent") == '2025'
    click(b, '.rgpicker .rgm'); assert label(b) == 'January 2025'
    assert b.problems() == []


def test_filter_preview_saved_filters_sort_review_totals_and_shared_navigation(site, chrome):
    b = chrome
    go(b, site)
    window(b, 'Last 90 days')
    summary_parity(b, 'since=90d')
    b.requests()
    click(b, '#page .tfbtn')
    calls = [u for _, u in b.requests() if '/api/transactions?' in u]
    assert calls
    for url in calls:
        q = parse_qs(urlsplit(url).query)
        assert q['since'] == ['90d'] and 'month' not in q
    # Group and category selections compose in the same draft/window.
    b.js("[...document.querySelectorAll('.tfg')].find(x=>x.textContent.includes('Food')).querySelector('.tfc').click()")
    wait(b)
    click(b, '.tfft .btn.w')
    assert 'since=90d' in b.js('location.hash') and 'group=Food' in b.js('location.hash')
    click(b, '.tfclr')
    b.js("[...document.querySelectorAll('.sfgo')].find(x=>x.textContent.includes('Window groceries')).click()"); wait(b)
    assert 'since=90d' in b.js('location.hash')
    assert b.js("document.querySelector('#page .search').value") == 'Window'
    d = summary_parity(b, 'since=90d&q=Window&category=Groceries')
    assert d['total'] > 50
    click(b, '.tx thead th.num button')
    assert b.js("document.querySelectorAll('.tx tr.day').length") == 0
    assert 'since=90d' in b.js('location.hash')
    click(b, '[data-kind="review"]')
    assert 'since=90d' in b.js('location.hash')
    summary_parity(b, 'since=90d&q=Window&category=Groceries&kind=review')
    # A direct shared link generated by other pages retains its provided month.
    for page in ['overview', 'budgets']:
        go(b, site, page+'?month=2026-09')
        assert b.js("document.querySelector('#monl').tagName") == 'SPAN'
        assert label(b) == 'September 2026'
        click(b, '#mprev'); assert label(b) == 'August 2026'
        click(b, '#mnext'); assert label(b) == 'September 2026'
        link = b.js("[...document.querySelectorAll('#page a[href]')].find(a=>a.hash.startsWith('#/transactions'))?.hash")
        assert link and 'month=2026-09' in link
        b.goto(site.url+'/'+link); wait(b); assert label(b) == 'September 2026'
    assert b.problems() == []

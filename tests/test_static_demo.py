"""Read-only demo contracts, using the real synthetic builder and JavaScript router."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import demo_parity
sys.path.insert(0, str(ROOT / 'tests'))
from offline_env import OFFLINE_SWITCHES

# The weekly pipeline (scripts/demo_weekly.sh) runs these on each variant: its seed and its day.
SEED, TODAY = os.environ.get('HF_DEMO_SEED', '0'), os.environ.get('HF_DEMO_TODAY', '2026-10-24')
MONTH = TODAY[:7]


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp('static-demo')
    dist = tmp / 'dist'
    # conftest's offline switches are taken out of the build's environment on purpose: build_demo.py makes its own process
    # safe (offline_env.OFFLINE_SWITCHES), and the demo must not depend on what the caller's shell or a test run sets
    env = {k: v for k, v in os.environ.items() if k not in OFFLINE_SWITCHES}
    subprocess.run([sys.executable, str(ROOT / 'scripts/build_demo.py'), '--today', TODAY, '--seed', SEED,
                    '--out', str(dist), '--keep-db', str(tmp / 'books'), '--no-canary'],
                   check=True, capture_output=True, env=env)
    return tmp, dist


def test_the_built_demo_carries_no_vendor_logo(built):
    """Owner rule: no Plaid or SimpleFIN logo file anywhere. The app fetches both vendors' icons from their sites into
    `institutions` (logos.VENDOR_LOGOS, source 'site') unless HERMES_PLAID / HERMES_SIMPLEFIN are off: a build that left a
    switch out could ship those bytes as api/institutions/<id>/logo. The demo's own logos are the invented ones."""
    from hermes_finance import logos
    _, dist = built
    vendor_ids = set(logos.VENDOR_LOGOS)
    assert vendor_ids == {'ins_hf_plaid', 'ins_hf_simplefin'}
    served = {p.parent.name for p in (dist / 'api/institutions').glob('*/logo')}
    assert served and not served & vendor_ids
    invented = {p.read_bytes() for p in (ROOT / 'demo/logos').glob('*.png')}
    for p in (dist / 'api/institutions').glob('*/logo'):
        assert p.read_bytes() in invented, f'{p.parent.name}: a logo that is not one of demo/logos'
    for p in (dist / 'demo/data').rglob('*.json'):   # data only: static/*.js names the ids as code, with no image bytes
        assert not any(i in p.read_text() for i in vendor_ids), p
    assert not [p for p in dist.rglob('*') if p.is_file() and any(i in p.as_posix() for i in vendor_ids)]


def route(built, posts=(), gets=()):
    tmp, dist = built
    req = tmp / 'requests.json'
    req.write_text(json.dumps({'posts': posts, 'gets': gets}))
    r = subprocess.run([shutil.which('node'), '-e', demo_parity.HARNESS, str(dist), str(req)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def _reports_requests(built, view):
    """Every request the Reports page can make for `view`: each month, the chip windows, the three forecast horizons;
    for the drill-down, the desktop cells that hold transactions and the phone's category rows (which open the month
    the page is on, whatever it holds)."""
    from build_demo import fkey
    _, dist = built
    data = json.loads((dist / 'demo/data/core.json').read_text())
    spending = [v for k, v in data['core'].items() if k.startswith('/api/reports/spending?')]
    path = f'/api/reports/{view}'
    if view in ('spending', 'cashflow'):
        params = ([{'month': m} for m in data['meta']['months']]
                  + [{'since': w} for w in ('7d', '30d', '90d')] + [{'window': 'all'}])
    elif view == 'forecast':
        params = [{'days': n} for n in (30, 60, 90)]
    else:
        desktop = {(c['category_id'], cell['month']) for d in spending for c in d['year']['categories']
                   for cell in c['months'] if not cell['nodata'] and cell['net']}
        phone = {(c['category_id'], d['month']) for d in spending for c in d['year']['categories']}
        params = [{'category_id': cid, 'month': m} for cid, m in sorted(desktop | phone)]
    return data, [fkey(path, p) for p in params]


def _app_and_rename(built):
    """The real App on the DB the build kept, and the build's own name map (names and ids change, amounts never)."""
    import build_demo
    import sqlite3
    tmp, _ = built
    books = tmp / 'books'
    saved = json.loads((books / 'rename.json').read_text())
    # the map the way demo_parity.py rebuilds it: from the kept DB, then the build's own path stand-ins
    conn = sqlite3.connect(books / 'finance.db')
    try:
        rename = build_demo.RenameMap(conn, {k: f'/nonexistent/{k}' for k in [*build_demo.FAKE_PATHS, 'tmp']})
    finally:
        conn.close()
    rename.paths.update(saved['paths'])
    rename._paths = sorted(rename.paths, key=len, reverse=True)
    from datetime import date
    return build_demo.Answers(books / 'finance.db', date.fromisoformat(TODAY)), rename


@pytest.mark.parametrize('view', ['spending', 'cashflow', 'forecast', 'activity'])
def test_reports_presets_match_the_app_on_the_same_books(built, view):
    """The demo replays frozen /api/reports/* answers. For the same books and the same day, what the demo's router
    serves for each preset and each cell must equal what the real app answers now (to the cent), and what it serves
    must be what the build froze."""
    from urllib.parse import parse_qsl
    data, keys = _reports_requests(built, view)
    app, rename = _app_and_rename(built)
    replies = route(built, gets=keys)['gets']
    served = [k for k in keys if replies[k][0] == 200]
    assert served, view
    mismatches = []
    for k in served:
        path, _, query = k.partition('?')
        expected = rename.json(app.json(path, dict(parse_qsl(query)))[1])
        mismatches.extend(demo_parity.diff([200, expected], replies[k], k, tol=0.0051))
        if k in data['core']:
            assert replies[k] == [200, data['core'][k]], k
    assert not mismatches, f'{view}: {len(mismatches)} differences across {len(served)} requests: {mismatches[:10]}'
    if view != 'activity':     # a month, a window or a horizon the page offers is never left unserved
        assert len(served) == len(keys), [k for k in keys if k not in served]


def test_every_drill_the_page_can_open_is_frozen(built):
    _, keys = _reports_requests(built, 'activity')
    replies = route(built, gets=keys)['gets']
    unserved = [k for k in keys if replies[k][0] != 200]
    assert not unserved, f'{len(unserved)} of {len(keys)} drill-downs answer with an error in the demo: {unserved[:3]}'


def test_money_writes_are_refused_and_gets_unchanged(built):
    gets = [f'/api/budgets?month={MONTH}', f'/api/overview?month={MONTH}', '/api/categories']
    before = route(built, gets=gets)['gets']
    posts = [('/api/budgets/cap', {'category': 'Groceries', 'monthly_cap': 1}),
             ('/api/budgets/rollover', {'category': 'Groceries', 'enabled': True}),
             ('/api/mcp/pending/approve', {'id': 1}), ('/api/sync', {}),
             ('/api/settings', {'networth_excluded': []}), ('/api/prefs', {'sync.interval': 1})]
    after = route(built, posts, gets)
    assert after['posts'] == [[403, {'error': 'Not available in the demo'}]] * len(posts)
    assert after['gets'] == before


def test_frozen_group_budgets_not_client_recomputation(built):
    _, dist = built
    month = json.loads((dist / f'demo/data/m/{MONTH}.json').read_text())
    answer = route(built, gets=[f'/api/budgets?month={MONTH}'])['gets'][f'/api/budgets?month={MONTH}']
    assert answer == [200, month['budgets']]
    assert any(g['budget'] is not None for g in month['budgets']['groups'])


def test_cross_origin_and_unknown_paths_fail_closed(built):
    out = route(built, gets=['https://example.org/api/accounts', '/api/unknown'])['gets']
    assert out['https://example.org/api/accounts'][0] == 403
    assert out['/api/unknown'][0] == 404


def test_assets_are_byte_identical_and_no_database(built):
    _, dist = built
    for p in (dist / 'static').rglob('*'):
        if p.is_file():
            assert p.read_bytes() == (ROOT / 'src/hermes_finance/static' / p.relative_to(dist / 'static')).read_bytes()
    assert not any(p.suffix in ('.db', '.sqlite', '.env') for p in dist.rglob('*'))
    html = (dist / 'index.html').read_text()
    assert 'cdn.plaid.com' not in html
    assert "connect-src 'self'" in html


def test_frozen_ask_presets_answer_their_questions(built):
    """t_d31fda2a (Ask v2, was test_exactly_four_frozen_tool_answers): ask.json holds 12 core presets built from the real
    tools at build time, each with its traces; the shim answers each preset's question with that preset's reply, and
    anything it can't match with a friendly reply naming 3 suggested questions (no longer the 'AI chat is off' line)."""
    import demo_ask
    _, dist = built
    ask = json.loads((dist / 'demo/data/ask.json').read_text())
    assert ask['version'] == 2 and len(ask['core']) == 12 and set(ask['core']) <= set(ask['presets'])
    for k, p in ask['presets'].items():
        assert p['traces'] and all(t['tool'].startswith('finance_') for t in p['traces']), k
        assert p['reply'] and p['blocks'], k
    posts = [('/api/chat', {'messages': [{'role': 'user', 'content': ask['presets'][k]['question']}]}) for k in ask['core']]
    got = route(built, posts + [('/api/chat', {'messages': [{'role': 'user', 'content': 'something else'}]})])['posts']
    for k, (status, body) in zip(ask['core'], got):
        p = ask['presets'][k]
        assert status == 200 and body['reply'] == demo_ask.render(p['reply']), k
        assert body['sources'] == [demo_ask.render(x) for x in p['sources']] and body['stats'] == p['stats'], k
        assert 'Canned demo answer' not in body['reply'] and '{{' not in body['reply'], k
    status, body = got[-1]
    assert status == 200 and body['reply'] == ask['unmatched']['reply']
    assert len(ask['unmatched']['suggestions']) == 3 and set(ask['unmatched']['suggestions']) <= set(ask['presets'])
    assert 'AI chat is off' not in body['reply']


def test_public_scan_cannot_read_ambient_owner_file(built, tmp_path):
    import os
    _, dist = built
    private = tmp_path / 'owner.txt'
    private.write_text('synthetic data\n')  # would hit every demo answer if consulted
    env = dict(os.environ, HERMES_FINANCE_PRIVATE_FILE=str(private), HERMES_FINANCE_TEST_PRIVATE='synthetic data')
    r = subprocess.run([sys.executable, str(ROOT / 'scripts/demo_canary.py'), str(dist), '--synthetic-only'],
                       env=env, text=True, capture_output=True)
    assert r.returncode == 0, r.stdout
    assert 'publication privacy approval NOT performed' in r.stdout
    env.pop('HERMES_FINANCE_TEST_PRIVATE')
    r = subprocess.run([sys.executable, str(ROOT / 'scripts/demo_canary.py'), str(dist)],
                       env=env, text=True, capture_output=True)
    assert r.returncode == 3  # a missing explicit owner scan still fails closed


def test_display_only_preferences_do_not_enable_network_settings(built):
    out = route(built, [('/api/prefs', {'appearance.theme': 'light'})], ['/api/settings/page'])
    # The real settings GET normally precedes the user's preference change.
    # A direct write before initialization is allowed only after loading its frozen schema.
    assert out['posts'][0][0] == 200
    assert out['gets']['/api/settings/page'][1]['prefs']['appearance']['theme'] == 'light'


def test_builder_scrubs_alpaca_provider_credentials(monkeypatch, tmp_path):
    import build_demo
    isolated = {'APCA_API_KEY_ID': 'invented', 'APCA_API_SECRET_KEY': 'invented',
                'OTHER_ACCESS_TOKEN': 'invented'}
    monkeypatch.setattr(build_demo.os, 'environ', isolated)
    build_demo.safe_env(tmp_path)
    assert 'APCA_API_KEY_ID' not in isolated
    assert 'APCA_API_SECRET_KEY' not in isolated
    assert 'OTHER_ACCESS_TOKEN' not in isolated
    assert not Path(isolated['FINANCE_SECRETS_FILE']).exists()


def test_goal_icons_survive_the_name_scrub(built):
    """A real merchant named like an icon key (Target -> 'target') must not rename the goal icon set."""
    from hermes_finance import goals
    _, dist = built
    core = json.loads((dist / 'demo/data/core.json').read_text())
    pages = core.get('core', core)
    seen = 0
    for key, body in pages.items():
        if key.startswith('/api/goals') and isinstance(body, dict) and 'appearance' in body:
            assert body['appearance']['icons'] == list(goals.ICONS), key
            seen += 1
            for g in (body.get('goals') or []) + (body.get('archived') or []):
                # t_d31fda2a: a picker icon, so the edit sheet shows it selected (the legacy defaults are not offered)
                assert g.get('icon') in goals.ICONS, (key, g.get('icon'))
                assert g.get('colour') in goals.COLOURS, (key, g.get('colour'))
    assert seen


def test_no_placeholder_names_remain(built):
    """t_d31fda2a: the Contoso / Northwind family and the plain "Example ..." stand-ins are gone from everything the
    build generates (the frozen answers, the page, the CSV, the demo's own scripts and every file name)."""
    import build_demo
    _, dist = built
    hits = []
    for p in sorted(dist.rglob('*')):
        rel = p.relative_to(dist).as_posix()
        if build_demo.RETIRED_NAMES.search(rel):
            hits.append((rel, 'file name'))
        if not p.is_file() or rel.startswith('static/') or p.suffix not in ('.json', '.html', '.csv', '.js', '.css'):
            continue
        for m in build_demo.RETIRED_NAMES.finditer(p.read_text(errors='replace')):
            hits.append((rel, m.group(0)))
    assert not hits, hits[:20]
    names = json.loads((dist / 'demo/data/core.json').read_text())['core']['/api/accounts']
    assert {i['name'] for i in names['institutions']} - {'Alpaca'} <= set(sum(build_demo.INSTITUTIONS.values(), []))


def test_every_merchant_and_institution_shown_has_a_logo(built):
    """t_d31fda2a: every connected institution carries a logo and every transaction with a merchant points at that
    merchant's same-origin logo, which the dist serves. (A manual account's provider that isn't a connected bank shows
    its kind's badge, as in the app.)"""
    import build_demo
    _, dist = built
    core = json.loads((dist / 'demo/data/core.json').read_text())['core']
    insts = core['/api/accounts']['institutions']
    assert insts and all(i['logo'] for i in insts), [i['name'] for i in insts if not i['logo']]
    for i in insts:
        assert (dist / f"api/institutions/{i['institution_id']}/logo").is_file(), i['name']
    rows = json.loads((dist / 'demo/data/transactions.json').read_text())['rows']
    named = [r for r in rows if r.get('merchant_name')]
    assert len(named) > 500
    missing = sorted({r['merchant_name'] for r in named if not r['logo_url']})
    assert not missing, missing
    for url in {r['logo_url'] for r in named}:
        assert url.startswith(build_demo.MERCHANT_LOGOS), url
        assert (dist / url.lstrip('/')).is_file(), url


def test_cars_and_landmarks_show_their_frozen_real_valuations(built):
    """t_d31fda2a: the demo's cars are real cars without a VIN and its properties are Los Angeles landmarks, each worth
    the one valuation scripts/demo_valuations_pull.py froze (with its source and date) in demo_assets.json."""
    import build_demo
    _, dist = built
    for a in build_demo.VEHICLES + build_demo.PROPERTIES:
        v = a['valuation']
        assert v and v['value'] > 0 and v['source'].startswith(('MarketCheck', 'RentCast')), a
        assert re.fullmatch(r'\d{4}-\d{2}-\d{2}', v['date']), a
    core = json.loads((dist / 'demo/data/core.json').read_text())['core']
    manual = core['/api/manual']
    manual = manual['accounts'] if isinstance(manual, dict) else manual
    shown = {m['name']: m for m in manual if m['kind'] in ('vehicle', 'property')}
    want = {f"{a['year']} {a['make']} {a['model']}": a for a in build_demo.VEHICLES}
    want.update({a['name']: a for a in build_demo.PROPERTIES})
    assert set(shown) == set(want)
    for name, m in shown.items():
        assert m['balance'] == want[name]['valuation']['value'], name
        assert not m.get('vin'), name
        if m['kind'] == 'property':
            assert m['address'] == want[name]['address']


def test_car_and_landmark_pages_show_a_photo_and_their_estimates(built):
    """The demo's car / landmark pages showed "Finding a photo…" forever and "not checked yet" in every estimate row: the
    build dropped the generator's pictures and estimates and put nothing in their place. Each page now has the real
    Commons photo demo_photos_pull.py froze (its file in the build, its credit and source page) and a year of monthly
    Carfax / Redfin checks ending on the frozen value, which the account follows."""
    import build_demo
    _, dist = built
    core = json.loads((dist / 'demo/data/core.json').read_text())['core']
    manual = core['/api/manual']
    manual = manual['accounts'] if isinstance(manual, dict) else manual
    frozen = {f"{a['year']} {a['make']} {a['model']}": a for a in build_demo.VEHICLES}
    frozen.update({a['name']: a for a in build_demo.PROPERTIES})
    pages = [core[f"/api/manual/{m['id']}/detail"] for m in manual if m['kind'] in ('vehicle', 'property')]
    assert len(pages) == len(frozen)
    for d in pages:
        a = frozen[d['name']]
        photo = d['photo']
        assert photo['status'] == 'ready' and photo['kind'] == ('cutout' if d['kind'] == 'vehicle' else 'photo'), d['name']
        assert (dist / photo['url'].lstrip('/').split('?')[0]).is_file(), d['name']
        assert photo['credit'] == a['photo']['credit'] and photo['credit'].endswith('Wikimedia Commons'), d['name']
        assert photo['credit_url'].startswith('https://commons.wikimedia.org/wiki/File:'), d['name']
        assert d['value']['following'] and d['value']['amount'] == a['valuation']['value'], d['name']
        assert d['value']['change'] and d['value']['change']['amount'] != 0, d['name']
        assert all(r['value'] for r in d['table']) and not any(r.get('empty') for r in d['table']), d['name']
        assert len(d['history']) >= 12 and d['history'][-1]['value'] == a['valuation']['value'], d['name']
        if d['kind'] == 'vehicle':
            assert [r[0] for r in d['specs']][:2] == ['Engine', 'Drive'], d['name']
            assert d['details']['mileage'] == a['mileage'], d['name']


def test_no_bill_is_late_on_the_build_day_and_history_covers_a_year(built):
    """t_d31fda2a: the generator's last cycle left some monthly bills unpaid, so the demo opened on 'late' bills. The build
    now records their payment on the due day (pay_late_bills) and backfills a year of weekly balances (extend_history),
    so every bill is upcoming from today and the net-worth history has more than a few points."""
    _, dist = built
    core = json.loads((dist / 'demo/data/core.json').read_text())
    today = core['meta']['today']
    items = core['core']['/api/recurring']['items']
    assert items and {i['status'] for i in items} <= {'upcoming', 'due_soon'}
    assert all(i['next_expected'] >= today for i in items)
    month = json.loads((dist / f'demo/data/m/{today[:7]}.json').read_text())
    assert all(b['next_expected'] >= today for b in month['overview']['upcoming'])


def test_pay_late_bills_pays_an_overdue_bill_but_not_one_due_today(monkeypatch, tmp_path):
    """A bill exactly one cadence after its last charge is due_soon, not late, yet its expected date has already
    passed (monthly: day 40) and the build must pay it. A bill due today or later is left untouched."""
    import sqlite3
    from datetime import date
    import build_demo
    from hermes_finance import tooldefs
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE transactions (transaction_id TEXT PRIMARY KEY, merchant_name TEXT, name TEXT, '
                 'pending INTEGER, amount REAL, date TEXT, authorized_date TEXT, raw_json TEXT, fitid TEXT)')
    conn.execute("INSERT INTO transactions VALUES ('t1', 'Pillbox & Co', 'PILLBOX', 0, 42.0, '2026-10-01', "
                 "'2026-10-01', '{}', NULL)")
    today = date(2026, 11, 10)
    overdue = {'merchant': 'Pillbox & Co', 'key': 'pillbox', 'next_expected': '2026-11-01', 'status': 'due_soon'}
    due_today = {'merchant': 'Rent', 'key': 'rent', 'next_expected': '2026-11-10', 'status': 'due_soon'}
    calls = []

    def fake_read(name, args, db_path, day):
        calls.append(day)
        return {'items': [overdue, due_today] if len(calls) == 1 else [due_today]}

    monkeypatch.setattr(tooldefs, 'run_read', fake_read)
    assert build_demo.pay_late_bills(conn, tmp_path / 'finance.db', today) == 1
    rows = conn.execute('SELECT transaction_id, date FROM transactions ORDER BY transaction_id').fetchall()
    assert [(r['transaction_id'], r['date']) for r in rows] == [
        ('t1', '2026-10-01'), ('t1-paid-2026-11-01', '2026-11-01')]


def test_extend_history_puts_every_account_on_one_weekly_grid():
    """The net-worth line grew a 3-4 day series: extend_history walked each account back on its own seven-day grid, so an
    account whose first snapshot came a few days after the earliest one (a month boundary) added points between the
    weekly ones. Every backfilled row must lie on the earliest account's grid."""
    import sqlite3
    from datetime import date
    import build_demo
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE accounts (account_id TEXT PRIMARY KEY, type TEXT)')
    conn.execute('CREATE TABLE balance_snapshots (account_id TEXT, date TEXT, current REAL, available REAL, '
                 'iso_currency TEXT, PRIMARY KEY (account_id, date))')
    conn.execute('CREATE TABLE transactions (account_id TEXT, date TEXT, amount REAL, pending INTEGER, removed INTEGER)')
    conn.execute('CREATE TABLE manual_balance_history (manual_account_id TEXT, date TEXT, balance REAL)')
    conn.executemany('INSERT INTO accounts VALUES (?, ?)',
                     [('acct-a', 'depository'), ('acct-b', 'depository')])
    conn.executemany('INSERT INTO balance_snapshots VALUES (?, ?, ?, ?, ?)',
                     [('acct-a', '2026-10-31', 1000.0, 1000.0, 'USD'),
                      ('acct-b', '2026-11-03', 500.0, 500.0, 'USD')])
    conn.execute("INSERT INTO transactions VALUES ('acct-a', '2026-10-20', -100.0, 0, 0)")
    today = date(2026, 11, 10)
    assert build_demo.extend_history(conn, today) > 0
    anchor = date(2026, 10, 31)
    for acc, first in (('acct-a', '2026-10-31'), ('acct-b', '2026-11-03')):
        dates = [r[0] for r in conn.execute(
            'SELECT date FROM balance_snapshots WHERE account_id = ? AND date < ?', (acc, first))]
        assert dates, acc
        assert sorted({(anchor - date.fromisoformat(d)).days % 7 for d in dates}) == [0], (acc, dates)


def test_every_real_ticker_held_shows_its_logo(built):
    """The Investments page showed letters ("AA", "VT") for AAPL and VTI: the build kept the made-up fund's logo but
    nothing for a real ticker, where the app shows the logo it caches from FMP. Each real ticker the demo holds now ships
    the logo demo_photos_pull.py --logos froze, byte for byte, at the URL the holdings row asks for."""
    import build_demo
    _, dist = built
    core = json.loads((dist / 'demo/data/core.json').read_text())['core']
    held = {h['ticker'] for h in core['/api/investments']['holdings'] if h.get('ticker') and ':' not in h['ticker']}
    assert {'AAPL', 'VTI'} <= held
    for t in held:
        logo = dist / f'api/securities/{t}/logo'
        assert logo.is_file(), f'{t}: no logo in the build'
        assert logo.read_bytes()[:8] == b'\x89PNG\r\n\x1a\n', t
        frozen = build_demo.TICKER_LOGOS / f'{t}.png'
        if frozen.is_file():
            assert logo.read_bytes() == frozen.read_bytes(), t


def test_every_cashflow_row_of_the_demo_opens_transactions_on_the_numbers_it_showed(built):
    """A Cash flow row links to Transactions (`#/transactions?...`, a hash route the static demo serves
    itself). For the 30-day window and for the current month, the shim answers each row's query with the row's own
    count and total, and refuses a source it does not know."""
    from urllib.parse import urlencode
    _, dist = built
    data = json.loads((dist / 'demo/data/transactions.json').read_text())
    income = {r['transaction_id'] for r in data['rows'] if r['is_income']}
    assert income and set(data['income_sources']) == income      # every income row has the one source the report files it under
    windows = [{'since': '30d'}, {'month': MONTH}]
    flows = route(built, gets=[f'/api/reports/cashflow?{urlencode(w)}' for w in windows])['gets']
    queries = {}
    for w in windows:
        status, flow = flows[f'/api/reports/cashflow?{urlencode(w)}']
        assert status == 200 and flow['groups'], w
        for s in flow['income']['sources']:
            queries[urlencode({'source': s['source'], 'kind': 'income', **w})] = (s['count'], s['amount'], True)
        for g in flow['groups']:
            queries[urlencode([*(('category', c) for c in g['categories']), ('kind', 'spend'), *w.items()])] = (
                g['count'], g['net'], False)
    answers = route(built, gets=[f'/api/transactions?{q}&limit=1' for q in queries])['gets']
    for q, (count, total, money_in) in queries.items():
        status, page = answers[f'/api/transactions?{q}&limit=1']
        assert status == 200, (q, page)
        assert page['total'] == count and abs(abs(page['sum']) - total) < 0.01 and (page['sum'] < 0) == money_in, (q, page['total'], page['sum'], count, total)
    status, page = route(built, gets=['/api/transactions?source=salary'])['gets']['/api/transactions?source=salary']
    assert status == 400, (status, page)

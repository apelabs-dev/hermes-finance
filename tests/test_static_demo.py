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

# The weekly pipeline (scripts/demo_weekly.sh) runs these on each variant: its seed and its day.
SEED, TODAY = os.environ.get('HF_DEMO_SEED', '0'), os.environ.get('HF_DEMO_TODAY', '2026-10-24')
MONTH = TODAY[:7]


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp('static-demo')
    dist = tmp / 'dist'
    subprocess.run([sys.executable, str(ROOT / 'scripts/build_demo.py'), '--today', TODAY, '--seed', SEED,
                    '--out', str(dist), '--no-canary'], check=True, capture_output=True)
    return tmp, dist


def route(built, posts=(), gets=()):
    tmp, dist = built
    req = tmp / 'requests.json'
    req.write_text(json.dumps({'posts': posts, 'gets': gets}))
    r = subprocess.run([shutil.which('node'), '-e', demo_parity.HARNESS, str(dist), str(req)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


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

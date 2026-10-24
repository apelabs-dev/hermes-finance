"""Read-only demo contracts, using the real synthetic builder and JavaScript router."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import demo_parity


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp('static-demo')
    dist = tmp / 'dist'
    subprocess.run([sys.executable, str(ROOT / 'scripts/build_demo.py'), '--today', '2026-10-24',
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
    gets = ['/api/budgets?month=2026-10', '/api/overview?month=2026-10', '/api/categories']
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
    month = json.loads((dist / 'demo/data/m/2026-10.json').read_text())
    answer = route(built, gets=['/api/budgets?month=2026-10'])['gets']['/api/budgets?month=2026-10']
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


def test_exactly_four_frozen_tool_answers(built):
    _, dist = built
    answers = json.loads((dist / 'demo/data/ask.json').read_text())
    assert list(answers) == ['How much did I spend on dining last month?',
                             'Am I on track with my budgets this month?',
                             'What bills are due in the next 7 days?',
                             'How has my net worth changed this year?']
    for q, answer in answers.items():
        assert answer['traces'] and 'Canned demo answer' in answer['reply']
        assert all(t['tool'].startswith('finance_') and t['result'] for t in answer['traces'])
        got = route(built, [('/api/chat', {'messages': [{'role': 'user', 'content': q}]})])['posts'][0]
        assert got == [200, answer]
    got = route(built, [('/api/chat', {'messages': [{'role': 'user', 'content': 'something else'}]})])['posts'][0]
    assert got[1]['reply'] == 'AI chat is off in the demo — install it to use your own model.'


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

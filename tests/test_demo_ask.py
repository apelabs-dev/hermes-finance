"""t_d31fda2a: Ask v2, the demo's frozen preset answers and the shim's fuzzy matcher (token overlap + synonyms, no ML)."""
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
import demo_ask
import demo_parity

PARAPHRASES = {
    'restaurants last month?': 'dining',
    'what did I spend eating out': 'dining',
    'am i over budget': 'budgets',
    'any bills due this week?': 'bills_week',
    'what do I owe soon': 'bills_week',
    'how much do streaming services cost me': 'subscriptions',
    'am I getting richer': 'networth',
    'what if I put aside more for vacation': 'whatif',
    'if I save extra each month': 'whatif',
    'top merchants': 'top_merchants',
    'where is my money going': 'top_merchants',
    'how much did I earn': 'cashflow',
    'cash flow': 'cashflow',
    'how are my stocks': 'investments',
    'portfolio': 'investments',
    'spending by category': 'spending',
    'how much on groceries': 'spending',
    'savings goals': 'goals',
}
SMALL_TALK = ['hello', 'tell me a joke', 'what is the weather like', '']


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp('demo-ask')
    dist, keep = tmp / 'dist', tmp / 'keep'
    subprocess.run([sys.executable, str(ROOT / 'scripts/build_demo.py'), '--today', os.environ.get('HF_DEMO_TODAY', '2026-10-24'),
                    '--seed', os.environ.get('HF_DEMO_SEED', '0'), '--out', str(dist), '--keep-db', str(keep), '--no-canary'],
                   check=True, capture_output=True)
    return tmp, dist, keep


def evaluate(built, exprs):
    tmp, dist, _ = built
    req = tmp / 'evals.json'
    req.write_text(json.dumps({'posts': [], 'gets': [], 'evals': exprs}))
    r = subprocess.run([shutil.which('node'), '-e', demo_parity.HARNESS, str(dist), str(req)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)['evals']


def ask_json(built):
    return json.loads((built[1] / 'demo/data/ask.json').read_text())


def test_paraphrases_match_their_preset_and_small_talk_matches_none(built):
    ask = ask_json(built)
    merchant = ask['presets']['merchant']['merchant']
    cases = {**PARAPHRASES, merchant.lower(): 'merchant', f'how often do I go to {merchant}': 'merchant'}
    cases.update({p['question']: k for k, p in ask['presets'].items()})
    texts = list(cases) + SMALL_TALK
    got = evaluate(built, [f'HFDemo.ask.data().then((a) => HFDemo.ask.match({json.dumps(t)}, a))' for t in texts])
    assert dict(zip(texts, got)) == {**cases, **{t: None for t in SMALL_TALK}}


def test_presets_are_short_linked_and_small(built):
    ask = ask_json(built)
    assert len(ask['core']) == 12 and len(set(ask['core'])) == 12
    for k in ask['starters'] + ask['unmatched']['suggestions']:
        assert k in ask['presets'], k
    for k, p in ask['presets'].items():
        assert len(p['question']) <= 50, k
        assert p['next'] and set(p['next']) <= set(ask['presets']) and k not in p['next'], k
        assert len(json.dumps(p['context'], separators=(',', ':'))) <= 6000, k
        assert {b['type'] for b in p['blocks']} <= {'tiles', 'bars', 'line', 'columns', 'cards', 'progress'}, k
        assert all(t['tool'].startswith('finance_') for t in p['traces']), k


def test_the_matcher_vocabulary_survives_the_name_scrub(built):
    """The build renames every generated name inside every string; a synonym that equals one would be renamed in the
    published matcher (and stop matching), so none may."""
    _, _, keep = built
    names = {n.lower() for n in json.loads((keep / 'rename.json').read_text())['names']}
    vocab = set(demo_ask.SYNONYMS) | {w for ws in demo_ask.SYNONYMS.values() for w in ws}
    vocab |= set(demo_ask.PHRASES) | set(demo_ask.PHRASES.values()) | set(demo_ask.STOPWORDS)
    assert not sorted(vocab & names)
    assert ask_json(built)['matcher'] == {'synonyms': demo_ask.SYNONYMS, 'phrases': demo_ask.PHRASES,
                                          'stopwords': demo_ask.STOPWORDS, 'threshold': demo_ask.THRESHOLD}


def test_python_and_shim_render_the_same_dates(built):
    ask = ask_json(built)
    texts = [p['reply'] for p in ask['presets'].values()] + [s for p in ask['presets'].values() for s in p['sources']]
    texts = [t for t in texts if '{{' in t]
    assert texts
    got = evaluate(built, [f'HFDemo.ask.render({json.dumps(t)})' for t in texts])
    assert got == [demo_ask.render(t) for t in texts]
    assert not any(re.search(r'\{\{|\}\}', t) for t in got)

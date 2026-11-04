"""t_d31fda2a: the weekly demo's seeded variants (build_demo.py --seed), their combined dist (demo_combine.py) and the
visitor-relative dates (demo/shim.js SHIFT)."""
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import demo_ask
import demo_combine
import demo_parity

WED = '2026-10-28'


def build(out: Path, seed: int, today: str = WED) -> Path:
    subprocess.run([sys.executable, str(ROOT / 'scripts/build_demo.py'), '--today', today, '--seed', str(seed),
                    '--out', str(out), '--no-canary'], check=True, capture_output=True)
    return out


@pytest.fixture(scope='module')
def builds(tmp_path_factory):
    tmp = tmp_path_factory.mktemp('demo-variants')
    return tmp, {'s0': build(tmp / 's0', 0), 's2': build(tmp / 's2', 2), 's2again': build(tmp / 's2again', 2),
                 's6': build(tmp / 's6', 6, '2026-10-25')}


# The dining repro reads its "Latest" cards from the frozen preset: seed 0's latest cards are 09-28/23/18, so the
# day shift leaves September at both 2026-10-29 (months=0) and 2026-11-03 (months=1). The report (the reply's
# {{m:}} month) must name the same month as every card date.
DINING_REPRO = [('2026-10-29', (2026, 9)), ('2026-11-03', (2026, 10))]


@pytest.fixture(scope='module')
def dining(builds):
    tmp, _ = builds
    return tmp, build(tmp / 'dining', 0, '2026-10-24')


@pytest.fixture(scope='module')
def combined(builds):
    tmp, b = builds
    # (the test maps the weekdays onto two builds; demo_weekly.sh builds one per weekday)
    demo_combine.combine(tmp / 'all', {k: b['s0'] if k == 0 else b['s2'] for k in range(7)})
    return tmp / 'all'


def tree(dist: Path) -> dict[str, bytes]:
    return {p.relative_to(dist).as_posix(): p.read_bytes() for p in sorted(dist.rglob('*')) if p.is_file()}


def harness(tmp: Path, dist: Path, query: str, gets=(), evals=()):
    req = tmp / 'variant-req.json'
    req.write_text(json.dumps({'gets': list(gets), 'posts': [], 'evals': list(evals), 'query': query}))
    r = subprocess.run([shutil.which('node'), '-e', demo_parity.HARNESS, str(dist), str(req)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def test_a_seed_builds_the_same_bytes_and_another_seed_the_same_structure(builds):
    _, b = builds
    assert tree(b['s2']) == tree(b['s2again'])
    one, two = (json.loads((b[k] / 'demo/data/core.json').read_text()) for k in ('s0', 's2'))
    assert set(one['core']) == set(two['core']) and one['meta']['months'] == two['meta']['months']
    assert (one['meta']['seed'], two['meta']['seed']) == (0, 2)
    rows = [json.loads((b[k] / 'demo/data/transactions.json').read_text())['rows'] for k in ('s0', 's2')]
    assert [r['amount'] for r in rows[0]] != [r['amount'] for r in rows[1]]   # the amounts and merchant mix vary
    names = [{i['name'] for i in c['core']['/api/accounts']['institutions']} for c in (one, two)]
    assert names[0] == names[1]                                              # the accounts don't



def test_a_long_merchant_name_gets_no_preset(builds):
    # seed 6's busiest merchant, a "Zelle payment to ..." line, made a question over 50 characters and failed the build
    _, b = builds
    presets = json.loads((b['s6'] / 'demo/data/ask.json').read_text())['presets']
    merchants = [p['merchant'] for p in presets.values() if 'merchant' in p]
    assert merchants and all(len(p['question']) <= 50 for p in presets.values())
    assert not any(m.lower().startswith('zelle') for m in merchants)



def test_a_figure_that_lands_on_a_generator_amount_is_built_again(builds):
    # seed 6 on 2026-10-25 summed to one of the generator's fixed amounts (salts 0 and 1), which the canary refuses
    _, b = builds
    assert json.loads((b['s6'] / 'demo/data/core.json').read_text())['meta']['salt'] == 2
    r = subprocess.run([sys.executable, str(ROOT / 'scripts/demo_canary.py'), str(b['s6']), '--fixed-amounts'],
                       capture_output=True, text=True)
    assert r.returncode == 0 and ' 0 generator fixed amount(s)' in r.stdout
    assert 'salt' not in json.loads((b['s0'] / 'demo/data/core.json').read_text())['meta']


def test_the_combined_dist_serves_each_weekdays_variant_unchanged(builds, combined):
    tmp, b = builds
    listing = json.loads((combined / 'demo/data/variants.json').read_text())['variants']
    assert [v['seed'] for v in listing] == [0, 2, 2, 2, 2, 2, 2]
    assert not (combined / 'demo/data/core.json').exists()
    gets = ['/api/accounts', f'/api/overview?month={WED[:7]}', '/api/transactions?limit=20&facets=1', '/api/recurring']
    for k, own in ((0, b['s0']), (3, b['s2'])):
        got = harness(tmp, combined, f'?demo-variant={k}&demo-today=build', gets, ['HFDemo.variant'])
        assert got['evals'] == [k]
        assert got['gets'] == harness(tmp, own, '?demo-today=build', gets)['gets']
    # unasked, the visitor's weekday picks it (Monday 0)
    assert harness(tmp, combined, '', evals=['HFDemo.variant'])['evals'] == [date.today().weekday()]
    shared = {rel for rel in tree(combined) if not rel.startswith('demo/data/') and rel != 'index.html'}
    assert {rel for rel in tree(b['s2']) if not rel.startswith('demo/data/') and rel != 'index.html'} <= shared


def test_the_demo_today_is_the_visitors_today(builds):
    tmp, b = builds
    local = '(() => { const d = new Date(); return [d.getFullYear(), d.getMonth() + 1, d.getDate()]; })()'
    today = date.today()
    got = harness(tmp, b['s0'], '', evals=[local, 'HFDemo.shift.days'])['evals']
    assert got == [[today.year, today.month, today.day], (today - date.fromisoformat(WED)).days]
    got = harness(tmp, b['s0'], '?demo-today=2026-11-03', ['/api/recurring'], [local, 'JSON.stringify(HFDemo.shift)'])
    assert got['evals'] == [[2026, 11, 3], '{"days":6,"months":1}']
    assert all(i['next_expected'] >= '2026-11-03' for i in got['gets']['/api/recurring'][1]['items'])
    built = harness(tmp, b['s0'], '?demo-today=build', ['/api/recurring'], [local])
    assert built['evals'] == [[2026, 10, 28]]
    assert got['gets']['/api/recurring'] == demo_parity.move(built['gets']['/api/recurring'], date(2026, 10, 28), 6, 1)
    # Ask streams an answer only while it's fresh, by the real clock; the demo's own clock is pinned to the build's noon
    # on the visitor's day (which is not always a day away from the real clock: on 2026-11-03 it is the same day).
    real, demo = harness(tmp, b['s0'], '?demo-today=2026-11-03', evals=['[HFDemo.ask.clock(), Date.now()]'])['evals'][0]
    pinned = datetime(2026, 11, 3, 12, tzinfo=timezone.utc).timestamp() * 1000
    assert abs(real - time.time() * 1000) < 60_000
    assert abs(demo - pinned) < 5_000


DATE_SHIFT_CASES = json.loads((ROOT / 'demo/date-shift-cases.json').read_text())['cases']
ISO_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
MONTH_YEAR_RE = re.compile(r'\b(January|February|March|April|May|June|July|August|September|October|November|December)'
                           r' (\d{4})\b')
MONTH_NUMBERS = {name: i + 1 for i, name in enumerate(
    ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November',
     'December'])}


def iso_dates(x):
    if isinstance(x, dict):
        return [d for v in x.values() for d in iso_dates(v)]
    if isinstance(x, list):
        return [d for v in x for d in iso_dates(v)]
    return [x] if isinstance(x, str) and ISO_RE.fullmatch(x) else []


def reply_month(reply):
    mt = MONTH_YEAR_RE.search(reply)
    return (int(mt.group(2)), MONTH_NUMBERS[mt.group(1)]) if mt else None


def test_shim_worker_and_python_agree_on_the_shared_date_shift_cases(builds):
    """One rule for demo/shim.js, demo/worker/src/index.js and scripts/demo_ask.py, including month boundaries."""
    tmp, b = builds
    expected = [c['expected'] for c in DATE_SHIFT_CASES]
    shim = harness(tmp, b['s0'], '?demo-today=build',
                   evals=[f'HFDemo.ask.renderShift({json.dumps(c["text"])}, {c["days"]}, {c["months"]})'
                          for c in DATE_SHIFT_CASES])['evals']
    assert shim == expected
    pairs = [[c['text'], c['days'], c['months']] for c in DATE_SHIFT_CASES]
    script = ("import('" + (ROOT / 'demo/worker/src/index.js').as_uri() + "').then((w) => "
              "process.stdout.write(JSON.stringify(" + json.dumps(pairs) +
              ".map(([t, d, m]) => w.renderDates(t, {days: d, months: m})))))")
    r = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script],
                       capture_output=True, text=True, timeout=60, check=True)
    assert json.loads(r.stdout) == expected
    assert [demo_ask.render(c['text'], c['days'], c['months']) for c in DATE_SHIFT_CASES] == expected


def test_a_month_labelled_answer_lists_only_that_months_dates(dining):
    """The dining repro: the reply's {{m:}} month and the 'Latest' cards' dates agree across a month boundary."""
    tmp, dist = dining
    ask = ("(async () => { const a = await HFDemo.ask.data();"
           " const q = 'How much did I spend on dining last month?';"
           " const id = HFDemo.ask.match(q, a);"
           " const [, r] = await HFDemo.route('POST', new URL('/api/chat', location.href).href,"
           "   JSON.stringify({messages: [{role: 'user', content: q}]}));"
           " const extra = HFDemo.ask.answerFor(r.reply);"
           " return {id, shift: HFDemo.shift, reply: r.reply, blocks: extra ? extra.blocks : null}; })()")
    bad = []
    for today, want in DINING_REPRO:
        got = harness(tmp, dist, f'?demo-today={today}', evals=[ask])['evals'][0]
        assert got['id'] == 'dining', got
        assert got['blocks'], got
        month = reply_month(got['reply'])
        dates = iso_dates(got['blocks'])
        inside = bool(dates) and all((date.fromisoformat(d).year, date.fromisoformat(d).month) == want for d in dates)
        if month != want or not inside:
            bad.append(f'{today}: reply month {month}, want {want}, cards {dates}')
    assert not bad, '\n'.join(bad)


def test_prose_dates_move_from_their_nearest_year():
    d = date(2026, 12, 30)
    assert demo_parity.move({'2026-12-30': ['sent Dec 29', 'next ~Jan 2, $5', '2026-12', '2026-12-30T10:00:00Z']}, d, 3, 1) == \
        {'2027-01-02': ['sent Jan 1', 'next ~Jan 5, $5', '2027-01', '2027-01-02T10:00:00Z']}
    assert demo_parity.move('Oct 2026 and May 5th', d, 1, 0) == 'Oct 2026 and May 5th'   # (not a "Mon D")


def test_the_date_rule_keeps_order_and_the_month_label_for_every_shift():
    """A shifted month's dates stay inside the shifted month and keep their order (a "latest payments" list
    stays sorted), for every day of a year of source dates and every shift up to 40 days either way."""
    from datetime import timedelta

    start = date(2026, 1, 1)
    days = [(start + timedelta(n)).isoformat() for n in range(366)]
    for shift in range(-40, 41):
        for k in (-2, -1, 0, 1, 2):
            prev = None
            for iso in days:
                out = demo_ask.shift_day(iso, shift, k)
                assert out[:7] == demo_ask.add_months(iso[:7], k), (iso, shift, k, out)
                if prev and prev[0][:7] == iso[:7]:
                    assert out >= prev[1], (prev, iso, shift, k, out)
                prev = (iso, out)

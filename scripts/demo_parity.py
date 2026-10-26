#!/usr/bin/env python3
"""Read-only GET parity: frozen demo answers versus the real App on the same sanitized synthetic DB.
Includes group budgets, all transaction sorts, repeated filters, splits, tags, search and date windows.
No application POST is executed. All demo data writes must be refused without changing any GET.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_demo as b  # noqa: E402

HARNESS = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const [dist, reqFile] = process.argv.slice(1);   // (node -e: no script path in argv)
const html = fs.readFileSync(path.join(dist, 'index.html'), 'utf8');
const tag = html.match(/<script src="\/demo\/shim\.js[^"]*" data-build="([^"]*)" data-now="([^"]*)"(?: data-variants="([^"]*)")?>/);
const request = JSON.parse(fs.readFileSync(reqFile, 'utf8'));
// The demo's today is the build's (not the visitor's) unless a request asks for another day or variant.
const query = request.query === undefined ? '?demo-today=build' : request.query;
const store = new Map();
const storage = {getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k), key: (i) => [...store.keys()][i], get length() { return store.size; }};
const ctx = {console, URL, URLSearchParams, Response, Request, Headers, Event, JSON, Math, Number, String, Object, Array,
  Promise, Set, Map, Error, RegExp, Symbol, isNaN, parseFloat, parseInt, setTimeout, clearTimeout, Date,
  localStorage: storage, sessionStorage: storage, HFDemoAskDelay: 0,
  location: {href: 'http://demo.local/' + query, origin: 'http://demo.local', reload() {}},
  document: {currentScript: {dataset: {build: tag[1], now: tag[2], ...(tag[3] ? {variants: tag[3]} : {})}}, documentElement: {dataset: {}, classList: {add() {}}},
    readyState: 'loading', addEventListener() {}, getElementById() { return null; }},
  XMLHttpRequest: class { open() {} send() {} },
  fetch: async (url) => {
    const u = new URL(url, 'http://demo.local/');
    const p = path.join(dist, decodeURIComponent(u.pathname));
    return fs.existsSync(p) ? new Response(fs.readFileSync(p)) : new Response('', {status: 404});
  }};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(dist, 'demo', 'shim.js'), 'utf8'), ctx);
(async () => {
  const {posts, gets, evals} = request;
  const out = {posts: [], gets: {}, evals: []};
  if (evals) await ctx.HFDemo.ask.data();
  for (const e of evals || []) out.evals.push(await vm.runInContext(e, ctx));
  for (const [url, body] of posts) out.posts.push(await ctx.HFDemo.route('POST', new URL(url, 'http://demo.local/').href, JSON.stringify(body)));
  for (const url of gets) out.gets[url] = await ctx.HFDemo.route('GET', new URL(url, 'http://demo.local/').href, null);
  out.store = ctx.HFDemo.store();
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(2); });
"""


def diff(a, b, where="$", out=None, tol=1e-9):
    out = [] if out is None else out
    if isinstance(a, bool) or isinstance(b, bool):
        if a is not b:
            out.append(f"{where}: {a!r} != {b!r}")
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if abs(a - b) > tol:
            out.append(f"{where}: {a!r} != {b!r}")
    elif isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{where}.{k}: {'missing in the app' if k not in a else 'missing in the shim'}")
            else:
                diff(a[k], b[k], f"{where}.{k}", out, tol)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{where}: {len(a)} items != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{where}[{i}]", out, tol)
    elif a != b:
        out.append(f"{where}: {str(a)[:80]!r} != {str(b)[:80]!r}")
    return out


MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
               'November', 'December']
DAY_RE = re.compile(r'(\d{4}-\d{2}-\d{2})([T ].*)?')
PROSE_RE = re.compile(r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{1,2})\b')


def shift_for(build_day: date, to: date) -> tuple[int, int]:
    """demo/shim.js SHIFT for a visitor whose today is `to`: (days, months)."""
    return (to - build_day).days, (to.year - build_day.year) * 12 + to.month - build_day.month


def move(x, build_day: date, days: int, months: int, prose: bool = True):
    """demo/shim.js moved(), written again: every ISO date (and a timestamp's date) moves `days`, every YYYY-MM moves
    `months`, and (prose) every "Mon D" moves `days` from its nearest year to the build day. Keys too."""
    import demo_ask

    def text(t: str) -> str:
        m = DAY_RE.fullmatch(t)
        if m:
            try:
                return (date.fromisoformat(m.group(1)) + timedelta(days=days)).isoformat() + (m.group(2) or '')
            except ValueError:
                pass
        if re.fullmatch(r'\d{4}-\d{2}', t) and 1 <= int(t[5:]) <= 12:
            return demo_ask.add_months(t, months)
        if not prose:
            return t

        def sub(mt):
            k = [n[:3] for n in MONTH_NAMES].index(mt.group(1))
            near = []
            for y in (build_day.year - 1, build_day.year, build_day.year + 1):
                try:
                    near.append(date(y, k + 1, int(mt.group(2))))
                except ValueError:
                    pass
            if not near:
                return mt.group(0)
            d = min(near, key=lambda d: abs((d - build_day).days)) + timedelta(days=days)
            return f'{MONTH_NAMES[d.month - 1][:3]} {d.day}'
        return PROSE_RE.sub(sub, t)

    if not days and not months:
        return x
    if isinstance(x, list):
        return [move(v, build_day, days, months, prose) for v in x]
    if isinstance(x, dict):
        return {text(k): move(v, build_day, days, months, prose) for k, v in x.items()}
    return text(x) if isinstance(x, str) else x


def moved_url(url: str, build_day: date, days: int, months: int) -> str:
    path, _, query = url.partition('?')
    params = [(k, move(v, build_day, days, months, prose=False)) for k, v in parse_qsl(query, keep_blank_values=True)]
    return path + ('?' + urlencode(params) if params else '')


def gets_for(months: list[str], banks: list[str]) -> list[str]:
    from hermes_finance import api
    out = ['/api/categories', '/api/category-groups', '/api/settings', '/api/settings/page', '/api/recurring']
    for m in months:
        out += [f'/api/{part}?month={m}' for part in ('budgets', 'overview', 'counts', 'saved-filters')]
    windows = [{}, {'month': months[-1]}, {'since': months[-3] + '-05', 'until': months[-1] + '-18'},
               {'since': '90d'}, {'until': months[-3] + '-10'}]
    filters = [{}]
    filters += [{'kind': k} for k in api.KINDS if k != 'all']
    filters += [{'category': 'Other Shopping'}, {'category': ['Groceries', 'Eating Out']}, {'group': 'Food'},
                {'group': ['Food', 'Leisure'], 'category': 'Travel'}, {'uncategorized': '1'},
                {'q': 'bean voyage'}, {'q': '%'}, {'q': 'shared'}, {'tag': 'shared'},
                {'bank': banks[0]}, {'q': 'rent', 'kind': 'spend'}]
    for window in windows:
        for f in filters:
            out.append('/api/transactions?' + urlencode({**window, **f, 'facets': '1', 'limit': '17', 'offset': '1'}, doseq=True))
        out.append('/api/saved-filters?' + urlencode({**window, 'window': '1'}))
    for sort in api.SORTS:
        out.append('/api/transactions?' + urlencode({'sort': sort, 'limit': '500', 'offset': '0'}))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--today', type=date.fromisoformat, default=date(2026, 10, 24))
    ap.add_argument('--seed', type=int, choices=list(b.SEEDS), default=0, help="the weekly variant to build (build_demo.py --seed)")
    ap.add_argument('--node', default=shutil.which('node'))
    args = ap.parse_args(argv)
    if not args.node:
        print('demo_parity: Node.js is required for the verification harness')
        return 2
    with tempfile.TemporaryDirectory(prefix='hf-demo-parity-') as td:
        tmp = Path(td); dist = tmp / 'dist'; keep = tmp / 'synthetic'
        if b.build_variant(dist, args.today, keep=keep, seed=args.seed) is None:
            raise SystemExit(f'demo_parity: seed {args.seed} has no clean salt on {args.today}')
        mapping = json.loads((keep / 'rename.json').read_text())
        conn = sqlite3.connect(keep / 'finance.db')
        paths = b.safe_env(tmp)
        rename = b.RenameMap(conn, paths)
        conn.close()
        rename.paths.update(mapping['paths'])
        rename._paths = sorted(rename.paths, key=len, reverse=True)
        back = {v: k for k, v in {**rename.names, **rename.ids, **rename.images}.items()}
        def original(text):
            for new in sorted(back, key=len, reverse=True):
                if new.casefold() == text.casefold(): return back[new]
            for new in sorted(back, key=len, reverse=True):
                text = text.replace(new, back[new])
            return text
        app = b.Answers(keep / 'finance.db', args.today)
        source, search = sqlite3.connect(keep / 'finance.db'), sqlite3.connect(tmp / 'search.db')
        source.backup(search); source.close()
        for table, idcol, columns in [('transactions', 'transaction_id', ('name', 'merchant_name')),
                                       ('transaction_notes', 'transaction_id', ('note',)), ('tags', 'id', ('name',))]:
            for row in search.execute(f"SELECT {idcol}, " + ','.join(columns) + f' FROM {table}').fetchall():
                search.execute(f"UPDATE {table} SET " + ','.join(f'{c}=?' for c in columns) + f' WHERE {idcol}=?',
                               [*(rename.text(v) if isinstance(v, str) else v for v in row[1:]), row[0]])
        search.commit(); search.close()
        search_app = b.Answers(tmp / 'search.db', args.today)
        c = json.loads((dist / 'demo/data/core.json').read_text())
        months = c['meta']['months'][-5:]
        banks = [i['name'] for i in c['core']['/api/accounts']['institutions']]
        gets = gets_for(months, banks)
        # Include every core snapshot and each historical financial page, not just current totals.
        gets += [('/api/bills/lookup?' + urlencode({'key': k.split('?key=')[1]}))
                 if k.startswith('/api/bills/lookup?key=') else k for k in c['core']]
        bill_keys = {rename.text(r['key']): r['key'] for r in app.json('/api/recurring')[1]['items']}
        gets += [f'/api/{part}?month={m}' for m in c['meta']['months'] for part in ('overview', 'budgets', 'counts')]
        gets = list(dict.fromkeys(gets))
        posts = [('/api/budgets/cap', {'category': 'Groceries', 'monthly_cap': 1}),
                 ('/api/mcp/pending/approve', {'id': 1}), ('/api/sync', {}),
                 ('/api/transactions/category', {'transaction_id': 'txn_00001', 'category': 'Other Shopping'})]
        req = tmp / 'req.json'; req.write_text(json.dumps({'gets': gets, 'posts': posts}))
        r = subprocess.run([args.node, '-e', HARNESS, str(dist), str(req)], capture_output=True, text=True, timeout=120, check=True)
        got = json.loads(r.stdout); failures = []
        for url in gets:
            path, _, query = url.partition('?')
            params = [(k, bill_keys.get(v, original(v).casefold()) if k == 'key' else original(v) if k in ('bank', 'transaction_id') else v) for k, v in parse_qsl(query)]
            actual_url = original(path) + ('?' + urlencode(params) if params else '')
            status, _, body = (search_app if any(k == 'q' for k, _ in params) else app).app.handle('GET', actual_url, b.HOST, b'')
            expected = b.with_merchant_logos(rename.json(json.loads(body)), set())  # the build's own logo mapping
            # Build-only Ask label and fake machine paths are intentional, documented page adapters.
            if url == '/api/chat':
                expected = c['core'][url]
            ds = diff([status, expected], got['gets'][url], url)
            failures += ds
        for reply in got['posts']:
            if reply != [403, {'error': 'Not available in the demo'}]: failures.append('A data write was accepted')
        # Ask v2: the frozen presets are the real tools' answers on this database, and the shim answers each preset's
        # question with exactly that preset (rendered), and anything else with the unmatched reply.
        ask = json.loads((dist / 'demo/data/ask.json').read_text())
        failures += diff(b.with_merchant_logos(rename.json(b.canned_answers(keep / 'finance.db', args.today)), set()), ask,
                         'real tool traces')
        import demo_ask
        asked = [*ask['presets'], None]
        chat = [('/api/chat', {'messages': [{'role': 'user', 'content': ask['presets'][k]['question'] if k else 'Tell me a joke'}]})
                for k in asked]
        req.write_text(json.dumps({'gets': [], 'posts': chat}))
        r = subprocess.run([args.node, '-e', HARNESS, str(dist), str(req)], capture_output=True, text=True, timeout=120, check=True)
        for k, (status, body) in zip(asked, json.loads(r.stdout)['posts']):
            p = ask['presets'][k] if k else {**ask['unmatched'], 'stats': []}
            want = {'reply': demo_ask.render(p['reply']), 'sources': [demo_ask.render(x) for x in p['sources']],
                    'stats': p['stats'], 'cards': []}
            failures += diff([200, want], [status, body], f'ask {k or "unmatched"}')
        # Visitor-relative dates (t_d31fda2a): on another day the shim answers every question, asked with that day's
        # dates, with the build day's answer moved by the same days (and months): a day later, a week later across a month
        # end, and three days earlier.
        shifts = [args.today + timedelta(days=n) for n in (1, 3)]
        shifts += [date(args.today.year + args.today.month // 12, args.today.month % 12 + 1, 1) + timedelta(days=2),
                   args.today - timedelta(days=3)]
        shifted = 0
        for to in shifts:
            days, months = shift_for(args.today, to)
            asked_then = {moved_url(u, args.today, days, months): u for u in gets}
            req.write_text(json.dumps({'gets': list(asked_then), 'posts': chat, 'query': f'?demo-today={to}'}))
            r = subprocess.run([args.node, '-e', HARNESS, str(dist), str(req)], capture_output=True, text=True, timeout=120, check=True)
            then = json.loads(r.stdout)
            for u, base in asked_then.items():
                failures += diff(move(got['gets'][base], args.today, days, months), then['gets'][u], f'{to} {u}')
            for k, (status, body) in zip(asked, then['posts']):
                p = ask['presets'][k] if k else {**ask['unmatched'], 'stats': []}
                want = {'reply': demo_ask.render(p['reply'], days, months),
                        'sources': [demo_ask.render(x, days, months) for x in p['sources']], 'stats': p['stats'], 'cards': []}
                failures += diff([200, want], [status, body], f'{to} ask {k or "unmatched"}')
            shifted += len(asked_then) + len(asked)
        for line in failures[:30]: print('FAIL ' + line)
        print(f'demo_parity: seed {args.seed}, {len(gets)} GET answers, {len(ask["presets"])} real tool answers/traces '
              f'({len(ask["core"])} preset questions), {len(posts)} refused writes, {shifted} answers on '
              f'{len(shifts)} other days ({", ".join(map(str, shifts))}); {len(failures)} differences')
        return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())

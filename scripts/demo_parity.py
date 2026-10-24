#!/usr/bin/env python3
"""Read-only GET parity: frozen demo answers versus the real App on the same sanitized synthetic DB.
Includes group budgets, all transaction sorts, repeated filters, splits, tags, search and date windows.
No application POST is executed. All demo data writes must be refused without changing any GET.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_demo as b  # noqa: E402

HARNESS = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const [dist, reqFile] = process.argv.slice(1);   // (node -e: no script path in argv)
const html = fs.readFileSync(path.join(dist, 'index.html'), 'utf8');
const tag = html.match(/<script src="\/demo\/shim\.js[^"]*" data-build="([^"]*)" data-now="([^"]*)">/);
const store = new Map();
const storage = {getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k), key: (i) => [...store.keys()][i], get length() { return store.size; }};
const ctx = {console, URL, URLSearchParams, Response, Request, Headers, Event, JSON, Math, Number, String, Object, Array,
  Promise, Set, Map, Error, RegExp, Symbol, isNaN, parseFloat, parseInt, setTimeout, clearTimeout, Date,
  localStorage: storage, sessionStorage: storage,
  location: {href: 'http://demo.local/', origin: 'http://demo.local', reload() {}},
  document: {currentScript: {dataset: {build: tag[1], now: tag[2]}}, documentElement: {dataset: {}, classList: {add() {}}},
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
  const {posts, gets} = JSON.parse(fs.readFileSync(reqFile, 'utf8'));
  const out = {posts: [], gets: {}};
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
                {'q': 'fourth coffee'}, {'q': '%'}, {'q': 'shared'}, {'tag': 'shared'},
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
    ap.add_argument('--node', default=shutil.which('node'))
    args = ap.parse_args(argv)
    if not args.node:
        print('demo_parity: Node.js is required for the verification harness')
        return 2
    with tempfile.TemporaryDirectory(prefix='hf-demo-parity-') as td:
        tmp = Path(td); dist = tmp / 'dist'; keep = tmp / 'synthetic'
        b.build(dist, args.today, keep=keep)
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
            expected = rename.json(json.loads(body))
            # Build-only Ask label and fake machine paths are intentional, documented page adapters.
            if url == '/api/chat':
                expected = c['core'][url]
            ds = diff([status, expected], got['gets'][url], url)
            failures += ds
        for reply in got['posts']:
            if reply != [403, {'error': 'Not available in the demo'}]: failures.append('A data write was accepted')
        ask = json.loads((dist / 'demo/data/ask.json').read_text())
        failures += diff(rename.json(b.canned_answers(keep / 'finance.db', args.today)), ask, 'real tool traces')
        for line in failures[:30]: print('FAIL ' + line)
        print(f'demo_parity: {len(gets)} GET answers, 4 real tool answers/traces, {len(posts)} refused writes; {len(failures)} differences')
        return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())

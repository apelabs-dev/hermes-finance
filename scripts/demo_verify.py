#!/usr/bin/env python3
"""Verify a scratch COPY of dist/demo using literal stdlib http.server and one slotted Chrome.
All pages and reachable asset details, both themes at 1440 and 390; read-only flows, Ask,
transaction windows/search/filtering, all local link resources, no errors or off-site network.
Screenshots and JSON evidence require --out outside the repository. Everything owned is stopped.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import shutil
import tempfile
import urllib.request
from urllib.parse import urlsplit, unquote
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import cdp  # noqa: E402
sys.path.insert(0, str(ROOT / "scripts"))
import demo_combine  # noqa: E402

PAGES = ("overview", "transactions", "budgets", "goals", "recurring", "accounts", "investments", "debt", "settings")
OTHER_PAGES = ("recurring", "accounts", "investments", "debt", "settings", "asset/3", "asset/8")   # dark, no screenshot each
VIEWPORTS = {"phone": (390, 844, True), "desktop": (1440, 900, False)}
PROBE = r"""(() => {
  const bar = document.getElementById('hf-demo-bar');
  const r = bar ? bar.getBoundingClientRect() : null;
  const hit = r ? document.elementFromPoint(r.left + 30, r.top + r.height / 2) : null;
  const top = document.querySelector('header.top'), side = document.getElementById('side');
  const rect = (el) => { if (!el) return null; const b = el.getBoundingClientRect(); return {top: Math.round(b.top), bottom: Math.round(b.bottom), left: Math.round(b.left), width: Math.round(b.width)}; };
  const page = document.getElementById('page');
  const text = page ? page.innerText : '';
  const errs = [...(page ? page.querySelectorAll('.err, [role=alert]') : [])].map((e) => e.textContent.trim()).filter(Boolean);
  return {theme: document.documentElement.dataset.theme, pref: document.documentElement.dataset.themePref,
    bar: r ? {top: Math.round(r.top), height: Math.round(r.height), width: Math.round(r.width), text: bar.innerText.replace(/\s+/g, ' ').trim(),
      visible: getComputedStyle(bar).visibility === 'visible' && getComputedStyle(bar).display !== 'none'} : null,
    bar_on_top: !!(hit && bar.contains(hit)), header: rect(top), first: rect(document.querySelector('#page > *')), sidebar: side && getComputedStyle(side).transform === 'none' ? rect(side) : null,
    title: (document.getElementById('title') || {}).textContent, amounts: (text.match(/\$\d[\d,]*(?:\.\d\d)?/g) || []).length,
    sample: (text.match(/\$\d[\d,]*(?:\.\d\d)?/g) || []).slice(0, 6), error_state: errs,
    error_text: /could not load|something went wrong|internal error|demo shim:|not in this demo|try again/i.test(text),
    skeletons: page ? page.querySelectorAll('.skel').length : -1, coarse: matchMedia('(pointer: coarse)').matches,
    width: innerWidth};
})()"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# The literal stdlib http.server (python -m http.server PORT --bind 127.0.0.1 --directory DIR), plus one thread that
# ends it once this process is gone. It runs in its own session so close() can stop it alone, which also means nothing
# stops it if this process dies before close(): a timeout's SIGTERM or a SIGKILL skips every finally. That left a server
# running for hours (retro, 2026-10-26). Now it exits once its parent is gone, however that ends: its stdin is a pipe
# only the parent holds, so the kernel closes it then, and the read sees EOF (no pid compared: no launcher or reparenting
# can fool it).
SERVE = """import os, runpy, sys, threading
port, directory = sys.argv[1], sys.argv[2]
def orphaned():
    while sys.stdin.buffer.read(1):
        pass
    os._exit(0)
threading.Thread(target=orphaned, daemon=True).start()
sys.argv = ['http.server', port, '--bind', '127.0.0.1', '--directory', directory]
runpy.run_module('http.server', run_name='__main__', alter_sys=True)
"""


class Site:
    """dist/ on http://127.0.0.1:<port>/ (scripts/serve_demo.py: http.server plus dist/_headers), in its own process group."""

    def __init__(self, dist: Path):
        self.scratch = tempfile.TemporaryDirectory(prefix='hf-demo-serve-')
        copied = Path(self.scratch.name) / 'site'
        shutil.copytree(dist, copied)
        self.port = free_port()
        self.errors = Path(self.scratch.name) / 'server.err'   # (why it didn't start, if it doesn't)
        with self.errors.open('wb') as err:
            self.proc = subprocess.Popen([sys.executable, '-c', SERVE, str(self.port), str(copied)], stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL, stderr=err, start_new_session=True)
        self.url = f"http://127.0.0.1:{self.port}"
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.1)
        why = f"exit {self.proc.poll()}: {self.errors.read_text(errors='replace')[-600:].strip()}" \
            if self.proc.poll() is not None else "still running, not listening after 15 s"
        self.close()
        raise RuntimeError(f"http.server did not start ({why})")

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.proc.wait(5)
        if self.proc.stdin:
            self.proc.stdin.close()   # (its end of the server's lifeline)
        self.scratch.cleanup()


class Run:
    def __init__(self, c: cdp.Chrome, site: Site, out: Path, variant: int | None = None):
        self.c, self.site, self.out = c, site, out
        # The build's own day (not the visitor's: the visitor_today flow checks that), and a combined dist's variant.
        self.query = 'demo-today=build' + (f'&demo-variant={variant}' if variant is not None else '')
        self.data = demo_combine.data_dir(Path(site.scratch.name) / 'site', variant)
        self.results: dict = {"checks": [], "pages": {}, "flows": {}, "requests_off_site": []}
        self.failed = 0

    def check(self, name: str, ok: bool, detail=None) -> bool:
        self.results["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f": {json.dumps(detail)[:300]}" if detail is not None else ""), flush=True)
        if not ok:
            self.failed += 1
        return ok

    def js(self, expr):
        return self.c.js(expr)

    def until(self, expr: str, what: str, timeout: float = 10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            v = self.js(expr)
            if v:
                return v
            time.sleep(0.1)
        try:   # what the page looked like then
            name = "timeout-" + "".join(ch if ch.isalnum() else "-" for ch in what)[:40]
            self.c.screenshot(self.out / f"{name}.png")
            print("timeout state:", json.dumps(self.js("({hash: location.hash, active: document.activeElement && document.activeElement.outerHTML.slice(0, 120), "
                                                       "chat: (document.getElementById('chat') || {}).innerText, log: sessionStorage.getItem('hf.chat.v1')})"))[:1500])
            print("timeout problems:", self.c.problems())
        except Exception as e:   # (the page may be gone)
            print("timeout state unavailable:", e)
        raise RuntimeError(f"timed out waiting for {what}")

    def viewport(self, name: str) -> None:
        w, h, mobile = VIEWPORTS[name]
        self.c.size(w, h, mobile=mobile)
        if mobile:
            self.c.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        else:
            self.c.call("Emulation.setTouchEmulationEnabled", enabled=False)

    def open(self, hash_: str, fresh: bool = False) -> None:
        url = f"{self.site.url}/?{self.query}{'&fresh=' + str(time.time()) if fresh else ''}#/{hash_}"
        self.c.goto(url)
        self.until("document.getElementById('hf-demo-bar') && !document.querySelector('#page .skel') && "
                   "document.querySelector('#page > *') ? true : false", f"#/{hash_} drawn")
        self.c.settled()

    def tap(self, expr: str, what: str) -> None:
        """A real pointer tap at the middle of the element `expr` finds (mouse events: the page sees a click)."""
        box = self.until(f"(() => {{ const el = {expr}; if (!el) return null; el.scrollIntoView({{block: 'center'}}); "
                         "const r = el.getBoundingClientRect(); return r.width ? [r.left + r.width / 2, r.top + r.height / 2] : null; })()",
                         what)
        x, y = box
        for t in ("mousePressed", "mouseReleased"):
            self.c.call("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left", clickCount=1)

    def shot(self, name: str) -> str:
        p = self.out / f"{name}.png"
        self.c.screenshot(p)
        return str(p.relative_to(ROOT)) if ROOT in p.resolve().parents else str(p)

    def problems(self, where: str) -> None:
        # Read requests/responses BEFORE problems() drains CDP events.
        reqs = self.c.requests()
        bad = [e['params']['response'] for e in self.c.events if e['method'] == 'Network.responseReceived'
               and e['params']['response']['status'] >= 400]
        off = [u for _, u in reqs if urlsplit(u).netloc != urlsplit(self.site.url).netloc and not u.startswith('data:')]
        self.results['requests_off_site'] += off
        self.results.setdefault('network', []).extend([{'method': m, 'url': u} for m, u in reqs])
        self.check(f'{where}: no HTTP errors', not bad, bad or None)
        self.check(f'{where}: every request is same origin', not off, off or None)
        probs = self.c.problems()
        self.check(f'{where}: no JS/console/CSP errors', not probs, probs or None)

    def links(self, where: str):
        links = self.js("[...document.querySelectorAll('[src],link[href],a[href]')].map(e=>e.src||e.href).filter(Boolean)")
        failures = []
        for url in set(links):
            u = urlsplit(url)
            if u.scheme == 'data' or u.fragment and u.path == '/': continue
            if u.netloc != urlsplit(self.site.url).netloc:
                failures.append({'url': url, 'error': 'external resource'})
                continue
            if u.path.startswith('/api/') and u.path != '/api/export.csv' and not any(x in u.path for x in ('/logo', '/images/', '/api/merchant-logos/')):
                failures.append({'url': url, 'error': 'unsnapshotted API anchor'})
                continue
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    if r.status != 200: failures.append({'url': url, 'status': r.status})
            except Exception as e:
                failures.append({'url': url, 'error': str(e)})
        self.results.setdefault('links', {})[where] = {'count': len(set(links)), 'failures': failures}
        self.check(f'{where}: link resources resolve locally', not failures, failures or None)

    def set_theme_by_settings(self, theme: str) -> None:
        """Settings -> Appearance -> Theme, tapped like a person would (the shim keeps it in its overlay)."""
        self.open("settings")
        label = theme.title()
        self.tap(f"[...document.querySelectorAll('#page button, #page [role=radio]')].find((b) => b.textContent.trim() === '{label}')",
                 f"the {label} theme button")
        self.until(f"document.documentElement.dataset.theme === '{theme}'", f"the {theme} theme")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('dist', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--viewports', default='desktop,phone', help='focused screenshot batch: desktop,phone')
    ap.add_argument('--flows-only', action='store_true', help='focused interaction batch without page screenshots')
    ap.add_argument('--variant', type=int, choices=range(7), default=None, help="a combined weekly dist's variant (demo_combine.py)")
    args = ap.parse_args(argv)
    dist, out = args.dist.resolve(), args.out.resolve()
    if out == ROOT or ROOT in out.parents:
        ap.error('--out must be outside the repository')
    out.mkdir(parents=True, exist_ok=True)
    core = json.loads((demo_combine.data_dir(dist, args.variant) / 'core.json').read_text())
    assets = ['asset/' + k.split('/')[3] for k in core['core'] if k.endswith('/detail')]
    pages = [*PAGES, *assets]
    site = c = run = None
    try:
        site = Site(dist)
        # Never sweep/kill another worker's orphan processes; close only the Chrome we own.
        cdp._swept = True
        c = cdp.Chrome()
        run = Run(c, site, out, args.variant)
        run.results.update(server_pid=site.proc.pid, server_command=site.proc.args, chrome_pgid=c.pgid,
                           scratch_copy=site.scratch.name, expected_pages=pages)
        for vp in args.viewports.split(','):
            run.viewport(vp)
            for theme in ('dark', 'light'):
                run.open('overview', fresh=True)
                run.set_theme_by_settings(theme)
                for page in ([] if args.flows_only else pages):
                    run.open(page, fresh=True)
                    m = run.js(PROBE); key = f"{vp}-{theme}-{page.replace('/', '-')}"
                    m['screenshot'] = run.shot(key); run.results['pages'][key] = m
                    run.check(f'{key}: correct theme, drawn, no error state', m['theme'] == theme and not m['error_state']
                              and not m['error_text'] and m['skeletons'] == 0, m['error_state'] or None)
                    run.check(f'{key}: exact demo banner visible on top', m['bar_on_top'] and m['bar']['visible']
                              and 'Demo — synthetic data' in m['bar']['text'] and m['bar']['top'] == 0)
                    run.check(f'{key}: content clears banner and header', m['header']['top'] >= m['bar']['height'] - 1
                              and m['first']['top'] >= m['header']['bottom'] - 1, [m['bar'], m['header'], m['first']])
                    run.links(key); run.problems(key)
            flows(run, core, vp)
            visitor_today(run, core, vp)
        manifest = [{'page': k, 'file': v['screenshot'], 'bytes': Path(v['screenshot']).stat().st_size}
                    for k, v in run.results['pages'].items()]
        run.results['screenshot_manifest'] = manifest
        run.check('exact screenshot page count', len(manifest) == (0 if args.flows_only else len(pages) * 2 * len(args.viewports.split(','))), len(manifest))
    except Exception as e:
        if run: run.check('verification completed without exception', False, str(e))
        raise
    finally:
        if c: c.close()
        if site: site.close()
        if run:
            run.results['cleanup'] = {'server_stopped': site.proc.poll() is not None,
                                      'scratch_removed': not Path(site.scratch.name).exists(), 'chrome_closed': c.proc.returncode is not None}
            run.results['passed'] = len(run.results['checks']) - run.failed
            run.results['failed'] = run.failed
            (out / 'verify.json').write_text(json.dumps(run.results, indent=2))
            print(f"demo_verify: {run.results['passed']}/{len(run.results['checks'])} checks passed; cleanup {run.results['cleanup']}", flush=True)
    return 1 if run is None or run.failed else 0


def flows(run, core, vp):
    c = run.c
    run.open('budgets', fresh=True)
    before = run.js("document.getElementById('page').innerText")
    # Click the real budget editor and save, proving the toast is visible and the snapshot remains unchanged.
    edit = "document.querySelector('#page tr.c button.plan')"
    run.tap(edit, 'a budget Edit control')
    run.until("!!document.querySelector('#modal input[inputmode=decimal]')", 'budget editor')
    run.js("(()=>{const e=document.querySelector('#modal input[inputmode=decimal]');e.value='1';e.dispatchEvent(new Event('input',{bubbles:true}));return true})()")
    run.tap("[...document.querySelectorAll('#modal button')].find(b=>b.textContent.trim()==='Save')", 'Save cap')
    run.until("document.getElementById('toast').innerText.trim()==='Not available in the demo'", 'exact refusal toast')
    run.check(f'{vp}: cap write refused with exact toast', run.js("document.getElementById('toast').innerText.trim()") == 'Not available in the demo')
    run.shot(f'{vp}-flow-cap-refused')
    run.open('budgets', fresh=True)
    run.check(f'{vp}: cap unchanged after reload', run.js("document.getElementById('page').innerText") == before)
    # Settings pending approval, sync and settings writes must all refuse; compare frozen GETs before and after.
    run.open('settings', fresh=True)
    run.tap("document.querySelector('#page [data-pending] button')", 'pending approval')
    run.until("document.getElementById('toast').innerText.trim()==='Not available in the demo'", 'approval refusal')
    run.check(f'{vp}: pending approval remains visible', run.js("!!document.querySelector('#page [data-pending]')"))
    result = run.js("""(async()=>{const out=[]; for(const [path,body] of [['/api/sync',{}],['/api/link_token',{}],['/api/settings',{}],['/api/prefs',{'sync.interval':1}]]){const r=await fetch(path,{method:'POST',body:JSON.stringify(body)});out.push([r.status,await r.json()]);}return out})()""")
    run.check(f'{vp}: sync/connect/settings writes refused', result == [[403, {'error':'Not available in the demo'}]] * 4, result)
    # The actual transaction sheet, filter drawer and date controls remain usable.
    run.open('transactions', fresh=True)
    # (the merchant's name: the middle of its cell can be a tag chip, which filters instead, as on seed 3's first row)
    run.tap("(document.querySelector('#page tr[data-tid] .mt .t') || document.querySelector('#page tr[data-tid] [data-txopen],"
            "#page tr[data-tid] .txname,#page tr[data-tid] td:nth-child(2)'))", 'transaction sheet')
    c.settled()
    run.check(f'{vp}: transaction sheet opens', run.js("!document.getElementById('txpanel').hidden || !!document.querySelector('#modal.on')"))
    run.shot(f'{vp}-flow-transaction-sheet')
    run.open('transactions', fresh=True)
    data = run.js("""(async()=>{const all=await (await fetch('/api/transactions?limit=500&facets=1')).json();const range=await(await fetch('/api/transactions?since=2026-09-01&until=2026-09-30&limit=500')).json();const ms={};for(const r of all.rows){if(r.merchant)(ms[r.merchant]??=new Set()).add(String(r.date).slice(0,7));}const query=Object.entries(ms).sort((a,b)=>b[1].size-a[1].size)[0][0];const search=await(await fetch('/api/transactions?'+new URLSearchParams({q:query,limit:500}))).json();return {all:all.total,range:range.total,rangePeriod:range.period,search:search.total,query,months:Object.keys(search.months),rows:search.rows};})()""")
    run.check(f'{vp}: all-time search and date ranges', data['all'] > data['range'] > 0 and data['search'] > 0
              and data['rangePeriod']['kind'] == 'range' and len(data['months']) > 1, {k:v for k,v in data.items() if k!='rows'})
    run.results['flows'][f'{vp}-transactions'] = {k:v for k,v in data.items() if k!='rows'}
    run.js("(()=>{const e=document.querySelector('#page input.search');e.value='Bean Voyage';e.dispatchEvent(new Event('input',{bubbles:true}));return true})()")
    run.until("document.getElementById('monl').textContent.includes('searching') && !!document.querySelector('#page tr[data-tid]')", 'automatic all-time search')
    run.check(f'{vp}: typing search switches to all history', run.js("document.getElementById('monl').textContent.includes('All time')"))
    run.shot(f'{vp}-flow-all-time-search')
    run.tap("document.querySelector('#page .tfbtn')", 'Filters')
    run.until("!!document.querySelector('.tfd input[aria-label=\"Food, the whole group\"]')", 'Food group filter')
    c.settled(force=True)
    run.tap("document.querySelector('.tfd input[aria-label=\"Food, the whole group\"]')", 'Food filter')
    run.until("document.querySelector('.tfd input[aria-label=\"Food, the whole group\"]').checked", 'group filter checked')
    c.settled()
    run.tap("[...document.querySelectorAll('.tfd button')].find(b=>/^Show .*payments?/.test(b.textContent))", 'Show filtered payments')
    run.until("!document.querySelector('.tfd')", 'filter drawer closed')
    run.until("document.querySelector('#page .txchips').textContent.includes('Food')", 'group filter chip')
    run.check(f'{vp}: group filter appears in current chips', run.js("document.querySelector('#page .txchips').textContent.includes('Food')"))
    run.shot(f'{vp}-flow-filtered')
    run.open('transactions', fresh=True)
    run.tap("document.getElementById('monl')", 'Time window')
    run.until("!!document.getElementById('rgfrom')", 'custom date range')
    run.js("(()=>{document.getElementById('rgfrom').value='2026-09-01';document.getElementById('rgto').value='2026-09-30';document.getElementById('rgfrom').closest('form').requestSubmit();return true})()")
    run.until("document.getElementById('monl').textContent.includes('Custom range')", 'selected date range')
    run.check(f'{vp}: date-range form changes displayed period', True)
    run.shot(f'{vp}-flow-date-range')
    run.open('overview', fresh=True)
    old_month = run.js("document.getElementById('monl').textContent")
    run.tap("document.getElementById('mprev')", 'previous month')
    run.until(f"document.getElementById('monl').textContent!=={json.dumps(old_month)}", 'previous month loaded')
    run.check(f'{vp}: month navigation redraws overview', True)
    run.problems(f'{vp} transaction filters/months')
    # Ask v2 (t_d31fda2a): the starter chips, two answers streamed through the real chat renderer with their blocks, the
    # follow-up chips, a question it can't match (a friendly reply with 3 suggestion chips), then the live-AI switch: the
    # static server has no /ai/chat, so it falls back to the same demo answer with the busy note.
    expected = json.loads((run.data / 'ask.json').read_text())
    sys.path.insert(0, str(ROOT / 'scripts'))
    import demo_ask
    presets = expected['presets']
    run.open('overview', fresh=True)
    run.js("sessionStorage.removeItem('hf.chat.v1'); localStorage.removeItem('hf-demo.live-ai'); true")
    run.open('overview', fresh=True)
    run.tap("document.getElementById('askb')", 'Ask')
    run.until("!document.getElementById('chat').hidden", 'Ask panel')
    chips = "[...document.querySelectorAll('#chat .chchips .fchip')].map(b=>b.textContent)"
    starters = [presets[k]['question'] for k in expected['starters']]
    run.until(f"JSON.stringify({chips})==={json.dumps(json.dumps(starters, separators=(',', ':')))}", 'starter chips')
    run.check(f'{vp}: Ask shows the starter chips', True, starters)
    bot = "[...document.querySelectorAll('#chat .msg.bot')]"
    first = expected['starters'][0]
    for i, pid in enumerate([first, presets[first]['next'][0]]):
        q, n = presets[pid]['question'], i + 1
        run.tap(f"[...document.querySelectorAll('#chat .fchip')].find(b=>b.textContent==={json.dumps(q)})", q)
        run.until(f"{bot}.length>={n} && {bot}[{n - 1}].classList.contains('hfd-typing')", f'answer {n} streaming')
        typing = run.js(f"{bot}[{n - 1}].querySelector(':scope > p:not(.src)').textContent.length")
        run.until(f"{bot}[{n - 1}].classList.contains('hfd-shown')", f'answer {n} fully shown', timeout=20)
        c.settled()
        text = run.js(f"[...{bot}[{n - 1}].querySelectorAll(':scope > p:not(.src)')].map(p=>p.textContent).join(String.fromCharCode(10))")
        want = demo_ask.render(presets[pid]['reply'])
        run.check(f'{vp}: answer {n} ({pid}) streamed, then exactly the build-time reply', typing < len(want) and
                  [x for x in text.split(chr(10)) if x] == [x for x in want.split(chr(10)) if x], [typing, len(want)])
        blocks = run.js(f"{bot}[{n - 1}].querySelectorAll('.hfd-blocks > *').length")
        run.check(f'{vp}: answer {n} draws its {len(presets[pid]["blocks"])} blocks', blocks == len(presets[pid]['blocks']), blocks)
        nxt = [presets[k]['question'] for k in presets[pid]['next']]
        run.until(f"JSON.stringify({chips})==={json.dumps(json.dumps(nxt, separators=(',', ':')))}", f'answer {n} follow-up chips')
        run.shot(f'{vp}-flow-ask-{n}')
    run.js("(()=>{const e=document.querySelector('#chat .chin');e.value='tell me a joke';e.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));return true})()")
    run.until(f"{bot}.length>=3 && {bot}[2].classList.contains('hfd-shown')", 'unmatched reply', timeout=20)
    sugg = [presets[k]['question'] for k in expected['unmatched']['suggestions']]
    run.until(f"JSON.stringify({chips})==={json.dumps(json.dumps(sugg, separators=(',', ':')))}", 'the 3 suggestion chips')
    run.check(f'{vp}: other Ask input gives the friendly reply and 3 suggestions',
              run.js(f"{bot}[2].textContent.includes({json.dumps(expected['unmatched']['reply'][:60])})"), sugg)
    run.shot(f'{vp}-flow-ask-unmatched')
    run.problems(f'{vp} ask')
    run.tap("document.getElementById('hfd-live')", 'the live AI switch')
    run.until("document.getElementById('hfd-live').getAttribute('aria-checked')==='true'", 'live AI on')
    run.tap(f"[...document.querySelectorAll('#chat .fchip')].find(b=>b.textContent==={json.dumps(sugg[0])})", sugg[0])
    run.until(f"{bot}.length>=4 && {bot}[3].classList.contains('hfd-shown')", 'live fallback answer', timeout=20)
    c.settled()
    pid = expected['unmatched']['suggestions'][0]
    run.check(f'{vp}: live AI unavailable falls back to the demo answer with the busy note',
              run.js(f"{bot}[3].textContent.includes({json.dumps(expected['live']['note'])}) && "
                     f"{bot}[3].textContent.includes({json.dumps(demo_ask.render(presets[pid]['reply']).split(chr(10))[0][:60])})"))
    run.shot(f'{vp}-flow-ask-live-fallback')
    reqs = run.c.requests()
    run.check(f'{vp}: the live call is one same-origin POST /ai/chat',
              [(m, urlsplit(u).path) for m, u in reqs if '/ai/' in u] == [('POST', '/ai/chat')], reqs)
    # The static server answers that POST 501 (the Worker isn't there); that one expected failure is all there is.
    bad = [e['params']['response'] for e in run.c.events if e['method'] == 'Network.responseReceived'
           and e['params']['response']['status'] >= 400 and urlsplit(e['params']['response']['url']).path != '/ai/chat']
    probs = [x for x in run.c.problems() if not x.endswith('/ai/chat')]
    run.check(f'{vp} ask live: no other HTTP or JS errors', not bad and not probs, (bad + probs) or None)
    run.tap("document.getElementById('hfd-live')", 'the live AI switch, off again')
    run.until("document.getElementById('hfd-live').getAttribute('aria-checked')==='false'", 'live AI off')
    run.problems(f'{vp} flows')

def visitor_today(run, core, vp):
    """t_d31fda2a: unasked, the demo's today is the visitor's: the page's clock is this machine's date, the Overview is on
    this month, and every bill is still ahead of today."""
    from datetime import date
    today = date.today()
    query = run.query
    run.query = query.replace('demo-today=build', '').lstrip('&')
    try:
        run.open('overview', fresh=True)
        got = run.js("(()=>{const d=new Date();return {day:[d.getFullYear(),d.getMonth()+1,d.getDate()],shift:HFDemo.shift,"
                     "month:document.getElementById('monl').textContent}})()")
        build_day = date.fromisoformat(core['meta']['today'])
        months = (today.year - build_day.year) * 12 + today.month - build_day.month
        run.check(f"{vp}: the demo's today is the visitor's today", got['day'] == [today.year, today.month, today.day] and
                  got['shift'] == {'days': (today - build_day).days, 'months': months} and
                  today.strftime('%B') in got['month'], got)
        bills = run.js("(async()=>(await (await fetch('/api/recurring')).json()).items.map(i=>i.next_expected))()")
        run.check(f'{vp}: every bill is due from the visitor\'s today on', bills and min(bills) >= today.isoformat(), min(bills or ['']))
        run.shot(f'{vp}-flow-visitor-today')
        run.problems(f'{vp} visitor today')
    finally:
        run.query = query


if __name__ == '__main__':
    sys.exit(main())

"""Capture the README's synthetic Overview at 1440 x 1500 in both themes.

    PYTHONPATH=src python scripts/capture_readme_shots.py --evidence-dir /path/outside/repo

Uses the exported gate fixtures, managed Chrome slots and loopback Server. All DBs
and profiles are temporary. The fixture and browser dates are pinned to 2026-09-25.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-dir', type=Path)
    args = parser.parse_args()
    if args.evidence_dir and args.evidence_dir.resolve().is_relative_to(ROOT):
        parser.error('evidence must be outside the repository')
    env = check.safe_env(HERMES_ENV_FILE='/nonexistent')
    os.environ.clear()
    os.environ.update(env)
    import gate_dbs
    from cdp import Chrome, Server
    from hermes_finance import db
    from run_gates import offline_valuation

    today = date(2026, 9, 25)
    out = ROOT / 'docs/assets/screenshots'
    out.mkdir(parents=True, exist_ok=True)
    record = {'today': today.isoformat(), 'fixture': 'gate_dbs.build("features", today=2026-09-25)',
              'sources': {}, 'screenshots': {}}
    for name in ('tests/demo_data.py', 'tests/gate_dbs.py'):
        record['sources'][name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='readme-shots-') as td, offline_valuation(Path(td)):
        path = Path(td) / 'synthetic.db'
        original_now = db.now
        try:
            db.now = lambda: '2026-09-25T12:00:00Z'
            gate_dbs.build('features', path, today=today)
        finally:
            db.now = original_now
        conn = db.connect(path)
        with conn:
            conn.execute("UPDATE items SET last_sync_at = '2026-09-25T11:18:00Z'")
        if args.evidence_dir:
            args.evidence_dir.mkdir(parents=True, exist_ok=True)
            (args.evidence_dir / 'synthetic-fixture.sql').write_text('\n'.join(conn.iterdump()) + '\n')
        conn.close()
        with closing(Server(path, today=today)) as server, Chrome() as browser:
            # Capture the native desktop layout; height includes the lower panels.
            browser.size(1440, 1500, scale=1)
            assert browser.js('[innerWidth, innerHeight, devicePixelRatio]') == [1440, 1500, 1]
            record['framing'] = {'css_width': 1440, 'css_height': 1500, 'device_scale': 1}
            browser.call('Emulation.setTimezoneOverride', timezoneId='UTC')
            browser.call('Emulation.setLocaleOverride', locale='en-US')
            browser.call('Page.addScriptToEvaluateOnNewDocument', source="""
                (() => {
                  const OriginalDate = Date;
                  window.Date = class extends OriginalDate {
                    constructor(...args) { super(...(args.length ? args : ['2026-09-25T12:00:00Z'])); }
                    static now() { return new OriginalDate('2026-09-25T12:00:00Z').getTime(); }
                  };
                })();
            """)
            browser.goto(server.url + '/#/overview?month=2026-09')
            for theme in ('light', 'dark'):
                browser.js(f'window.HFTheme.set({json.dumps(theme)})')
                assert not browser.settled(force=True), "page did not settle"
                browser.js('document.fonts.ready')
                browser.js('window.scrollTo(0, 0)')
                text = browser.js('document.body.innerText')
                assert 'Where it went' in text and 'Budgets' in text
                panels = browser.js("""[...document.querySelectorAll('#page h3 .ttl')]
                    .filter(el => ['Budgets', 'Where it went'].includes(el.textContent.trim()))
                    .map(el => ({title: el.textContent.trim(), top: el.getBoundingClientRect().top,
                                 visible: el.getBoundingClientRect().top < innerHeight - 250}))""")
                assert len(panels) == 2 and all(panel['visible'] for panel in panels), panels
                visible = browser.js("""(() => {
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    const lines = [];
                    while (walker.nextNode()) {
                      const node = walker.currentNode;
                      if (!node.textContent.trim()) continue;
                      const range = document.createRange(); range.selectNodeContents(node);
                      const box = range.getBoundingClientRect();
                      if (box.width && box.height && box.top < innerHeight && box.bottom > 0)
                        lines.push({text: node.textContent.trim(), x: box.x, y: box.y});
                    }
                    return lines;
                })()""")
                shot = out / f'overview-{theme}.png'
                browser.screenshot(shot)
                record['screenshots'][theme] = {'sha256': hashlib.sha256(shot.read_bytes()).hexdigest(),
                                                 'width': 1440, 'height': 1500, 'visible_text': text,
                                                 'visible_lines': visible, 'panels': panels}
                errors = browser.problems()
                if errors:
                    raise RuntimeError(f'browser errors: {errors}')
            with urlopen(server.url + '/api/overview?month=2026-09') as response:
                record['overview_api'] = json.load(response)
            for endpoint in ('accounts', 'counts?month=2026-09'):
                with urlopen(server.url + '/api/' + endpoint) as response:
                    record[endpoint.split('?')[0] + '_api'] = json.load(response)
    if args.evidence_dir:
        (args.evidence_dir / 'screenshots.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({theme: {k: v for k, v in data.items() if k in ('width', 'height', 'sha256')}
                      for theme, data in record['screenshots'].items()}, indent=2))


if __name__ == '__main__':
    main()

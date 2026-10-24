"""Capture Accounts acceptance evidence using synthetic DBs and one managed headless Chrome.

    HF_CHROMES=2 .venv/bin/python scripts/accounts_proof.py

Also measures the unmodified features DB before (bc5e1cb static files) and after at 390px.
Temporary static files/DBs are removed and every server/Chrome is closed even on failure.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tests'),str(ROOT/'scripts')]
import gate_dbs
from cdp import Chrome, Server
from hermes_finance import web
from run_gates import offline_valuation
from test_accounts_density import ACCOUNT_LAYOUT, CONNECTION_LAYOUT, HEADER_LAYOUT, CARD_NAME, long_names_db, open_accounts


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,default=ROOT/'shots/accounts',
                        help='Screenshot/evidence destination (defaults to shots/accounts)')
    out=parser.parse_args().output_dir.expanduser().resolve()
    out.mkdir(parents=True,exist_ok=True)
    static=web.STATIC_DIR
    with tempfile.TemporaryDirectory(prefix='proof-',dir=out) as td, offline_valuation(Path(td)):
        tmp=Path(td)
        baseline=tmp/'static'
        shutil.copytree(static,baseline)
        for name in ('app.js','app.css','alpaca.js','alpaca.css'):
            original=subprocess.run(['git','show',f'bc5e1cb:src/hermes_finance/static/{name}'],
                                    cwd=ROOT,capture_output=True,check=True).stdout
            (baseline/name).write_bytes(original)
        features=gate_dbs.build('features',tmp/'features-synthetic.db')
        measurements={}
        with Chrome() as b:
            for label,files in [('before',baseline),('after',static)]:
                web.STATIC_DIR=files
                server=None
                try:
                    server=Server(features)
                    open_accounts(b,server)
                    measurements[label]=b.js("({document:document.documentElement.scrollHeight,page:document.getElementById('page').getBoundingClientRect().height})")
                finally:
                    if server: server.close()
                    web.STATIC_DIR=static
            (out/'height-390-features.json').write_text(json.dumps(measurements,indent=2)+'\n')
            path=long_names_db(tmp/'long-names-synthetic.db')
            server=None
            try:
                server=Server(path)
                for width in (390,1280):
                    for theme in ('light','dark'):
                        open_accounts(b,server,width,theme)
                        for guard in (ACCOUNT_LAYOUT,HEADER_LAYOUT,CONNECTION_LAYOUT):
                            result=b.js(guard)
                            assert not result['errors'],result
                        b.screenshot(out/f'accounts-{width}-{theme}-custom.png',full=True)
                        b.js("document.querySelector('#page [data-api=alpaca] .help-trigger').click()")
                        b.settled(force=True)
                        b.screenshot(out/f'alpaca-details-{width}-{theme}.png')
                        b.js("document.querySelector('#sheet [aria-label=Close]').click()")
                        b.js(f"[...document.querySelectorAll('#page .account-entry')].find(e=>e.textContent.includes({json.dumps(CARD_NAME)})).querySelector('.help-trigger').click()")
                        b.settled(force=True)
                        b.screenshot(out/f'manual-card-details-{width}-{theme}.png')
                        b.js("document.querySelector('.help-panel [aria-label=\"Close details\"]').click()")
            finally:
                if server: server.close()
    print(json.dumps(measurements,indent=2))


if __name__=='__main__':
    main()

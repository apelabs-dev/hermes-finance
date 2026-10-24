"""Synthetic 390x844 WebKit checks and four light/dark screenshots.

Use Python Playwright from the dev group. An already installed Playwright Node CLI
can supply the same browser driver when the Python wheel is unavailable offline.
No package install or external page request is performed by this script.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from cdp import Server  # noqa: E402
import gate_dbs  # noqa: E402
from factory import cap  # noqa: E402
from hermes_finance import db  # noqa: E402
from datetime import date  # noqa: E402

OUT = ROOT / ".dispatch" / "out"
TODAY = date(2026, 10, 20)

# Measure actual text ranges: a button's icon/padding is not the start of its money.
MEASURE = r"""(pageName) => {
 const errors = [];
 const width = document.documentElement.clientWidth;
 const scroll = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth);
 if (scroll > width + 1) errors.push(`horizontal scroll: ${scroll} > ${width}`);
 const names = [], planned = [];
 const rangeBox = (el) => {
   const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
   let n;
   while ((n = walker.nextNode())) if (n.textContent.trim()) {
     const range = document.createRange();
     range.setStart(n, n.textContent.search(/\S/)); range.setEnd(n, n.textContent.trimEnd().length);
     return range.getBoundingClientRect();
   }
   throw new Error(`empty text: ${el.outerHTML}`);
 };
 if (pageName === 'budgets') {
   for (const el of document.querySelectorAll('.bt tr.c .nml')) {
     const rect = el.getBoundingClientRect(), line = parseFloat(getComputedStyle(el).lineHeight);
     names.push({name: el.textContent.trim(), height: rect.height, line});
     if (!el.textContent.trim() || !Number.isFinite(line) || rect.height > 1.3 * line)
       errors.push(`category wraps: ${JSON.stringify(names.at(-1))}`);
   }
   if (names.length < 3 || !names.some(n => n.name === 'Other Transport')) errors.push('missing representative category names');
   for (const group of document.querySelectorAll('.bt tbody.bg')) {
     const heading = group.querySelector('tr.g .plan');
     if (!heading) continue;
     const x = rangeBox(heading).x;
     for (const el of group.querySelectorAll('tr.c .plan')) {
       const categoryX = rangeBox(el).x;
       planned.push({group: group.dataset.group, groupX: x, categoryX});
       if (Math.abs(x - categoryX) > 1) errors.push(`Planned text starts differ: ${JSON.stringify(planned.at(-1))}`);
     }
   }
   if (!planned.length) errors.push('no group/category Planned text pairs measured');
 }
 return {page: pageName, viewport: [innerWidth, innerHeight], actualTheme: document.documentElement.dataset.theme, scroll, names, planned, errors};
}"""


class WebKitUnavailable(RuntimeError):
    """Missing installed driver/browser; genuine browser failures are not skipped."""


def _python_capture(url, out):
    from playwright.sync_api import sync_playwright
    reports = []
    with sync_playwright() as pw:
        if not Path(pw.webkit.executable_path).exists():
            raise WebKitUnavailable("WebKit is not installed; run playwright install webkit")
        browser = pw.webkit.launch(headless=True)
        try:
            for theme in ("light", "dark"):
                context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=theme,
                                              reduced_motion="reduce")
                try:
                    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(url + "/") else route.abort())
                    context.add_init_script(f"localStorage.setItem('hf.theme', {json.dumps(theme)})")
                    page = context.new_page()
                    for name in ("budgets", "transactions"):
                        page.goto(f"{url}/#/{name}")
                        page.wait_for_selector(".bt tr.c .nml" if name == "budgets" else "#page .tx")
                        page.evaluate("theme => HFTheme.set(theme)", theme)
                        page.evaluate("document.fonts.ready")
                        page.wait_for_load_state("networkidle")
                        page.screenshot(path=out / f"webkit-{name}-390-{theme}.png", full_page=False)
                        reports.append(dict(page.evaluate(MEASURE, name), theme=theme))
                finally:
                    context.close()
        finally:
            browser.close()
    return reports


def _node_capture(url, out):
    cli = shutil.which("playwright")
    node = shutil.which("node")
    if not cli or not node:
        raise WebKitUnavailable("Python Playwright and an installed Playwright Node CLI are unavailable offline")
    driver = Path(cli).resolve().parent
    code = r"""
const fs = require('fs'), path = require('path');
const {webkit} = require(process.argv[1]);
const [url, out] = process.argv.slice(2);
const measure = MEASURE;
(async () => {
 if (!fs.existsSync(webkit.executablePath())) { console.error('WebKit is not installed; run playwright install webkit'); process.exitCode = 77; return; }
 const browser = await webkit.launch({headless:true});
 const reports = [];
 try {
  for (const theme of ['light','dark']) {
   const context = await browser.newContext({viewport:{width:390,height:844},colorScheme:theme,reducedMotion:'reduce'});
   try {
    await context.route('**/*', route => route.request().url().startsWith(url + '/') ? route.continue() : route.abort());
    await context.addInitScript(theme => localStorage.setItem('hf.theme',theme), theme);
    const page = await context.newPage();
    for (const name of ['budgets','transactions']) {
     await page.goto(`${url}/#/${name}`);
     await page.waitForSelector(name === 'budgets' ? '.bt tr.c .nml' : '#page .tx');
     await page.evaluate(theme => HFTheme.set(theme), theme);
     await page.evaluate(() => document.fonts.ready);
     await page.waitForLoadState('networkidle');
     await page.screenshot({path:path.join(out,`webkit-${name}-390-${theme}.png`),fullPage:false});
     reports.push({...await page.evaluate(measure,name),theme});
    }
   } finally { await context.close(); }
  }
 } finally { await browser.close(); }
 console.log(JSON.stringify(reports));
})().catch(e => { console.error(e.stack); process.exitCode = 1; });
""".replace("MEASURE", MEASURE)
    print("Using installed Node Playwright (Python wheel unavailable offline)", flush=True)
    child = subprocess.Popen([node, "-e", code, str(driver), url, str(out)], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = child.communicate(timeout=120)
    finally:
        # The child is our own session leader; clear its browser descendants on interrupts/timeouts too.
        import signal
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=10)
    if child.returncode == 77:
        raise WebKitUnavailable(stderr.strip())
    if child.returncode:
        raise RuntimeError(stderr[-4000:])
    return json.loads(stdout)


def capture(out: Path = OUT):
    out.mkdir(parents=True, exist_ok=True)
    scratch = ROOT / ".dispatch" / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    # Keep all browser downloads local; the script itself never downloads anything.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(OUT / "webkit"))
    os.environ.update(HERMES_PLAID="off", HERMES_ALPACA="off", HERMES_CHROME_DEVTOOLS="off",
                      HERMES_ENV_FILE=str(scratch / "absent.env"), FINANCE_SECRETS_FILE=str(scratch / "absent-secrets.env"),
                      HERMES_ALPACA_KEY_FILE=str(scratch / "absent-alpaca.env"))
    with tempfile.TemporaryDirectory(prefix="webkit-", dir=scratch) as temp:
        path = Path(temp) / "synthetic.db"
        gate_dbs.build("features", path, TODAY)
        conn = db.connect(path)
        try:
            for name, amount in (("Groceries", 400), ("Eating Out", 150), ("Other Transport", 800), ("Other Health", 300)):
                cap(conn, name, amount)
            conn.commit()
        finally:
            conn.close()
        server = Server(path, TODAY)
        try:
            try:
                import playwright.sync_api  # noqa: F401
            except ImportError:
                reports = _node_capture(server.url, out)
            else:
                reports = _python_capture(server.url, out)
        finally:
            server.close()
    (out / "webkit-measurements.json").write_text(json.dumps(reports, indent=2) + "\n")
    errors = [f"{r['page']} {r['theme']}: {e}" for r in reports for e in r["errors"]]
    assert not errors, "\n".join(errors)
    print(f"PASS: four WebKit screenshots; no horizontal scroll; category heights and Planned text starts checked ({out})")
    return reports


if __name__ == "__main__":
    try:
        capture()
    except WebKitUnavailable as exc:
        print(f"SKIP: {exc}")
        sys.exit(0)

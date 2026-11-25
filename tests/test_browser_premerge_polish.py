"""Pre-merge polish, front end (B1 and B4), in headless Chrome (tests/cdp.py; https and plaid.com blocked) on copies of
the gate's "full" database.

B1: the "Link a bank or card" sheet. Both cards have the same four rows (head, strengths, drawbacks, button), the two
buttons are one size on one bottom line, the Plaid slot meter is gone from the card, "Plaid is full" shows only with no
connection left, every wide sheet is flush on a phone, and the SimpleFIN confirm row never clips its amount.

B4: Settings -> AI provider -> CLI: found, not found (with a path box), signed out (with the login command) and
"couldn't check" (never "Signed in" without a stored passing check)."""

from __future__ import annotations

import base64
import json
import shutil
import socket
import time
from datetime import date
from pathlib import Path

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome
from hermes_finance import catops, db, goals, insights, simplefin

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()
ACCESS = "https://bridge-user:bridge-pass@bridge.example/simplefin"
TOKEN = base64.urlsafe_b64encode(b"https://bridge.example/simplefin/claim/synthetic").decode().rstrip("=")
PUBLIC = lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]  # noqa: E731
THEMES = ["dark", "light"]
WIDTHS = [390, 1280]


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    path = tmp_path_factory.mktemp("browser_premerge_base") / "finance.db"
    gate_dbs.build("full", path, TODAY)
    return path


@pytest.fixture
def site(tmp_path, base, monkeypatch):
    monkeypatch.setenv("FINANCE_SECRETS_FILE", str(tmp_path / "secrets.env"))
    path = tmp_path / "finance.db"
    shutil.copy(base, path)
    srv = Server(path, today=TODAY)
    srv.path = path
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


def go(b, srv, width: int, theme: str, page: str = "accounts") -> None:
    b.size(width, 900 if width > 800 else 844)
    b.goto(f"{srv.url}/?t={time.time():.3f}#/{page}")
    b.js(f"window.HFTheme.set({json.dumps(theme)})")
    b.settle()
    assert b.js("document.documentElement.dataset.theme") == theme
    b.problems()


WAIT = """new Promise((res, rej) => { const t0 = Date.now(); const tick = () => { const v = (%s);
  if (v) res(v); else if (Date.now() - t0 > %d) rej(new Error(%s)); else setTimeout(tick, 50); }; tick(); })"""


def wait_for(b, expr: str, what: str, ms: int = 6000):
    return b.js(WAIT % (expr, ms, json.dumps("timed out waiting for " + what)))


def open_chooser(b) -> None:
    b.js("""(() => { const e = [...document.querySelectorAll('[data-add="home"]')].find((x) => x.getClientRects().length);
      if (!e) throw new Error('no Add account control'); e.click(); return 1; })()""")
    wait_for(b, """(() => { const e = [...document.querySelectorAll('#sheet .opt')]
      .find((x) => x.textContent.includes('Link a bank or card')); if (e) e.click(); return Boolean(e); })()""",
             "the Link a bank or card row")
    wait_for(b, "document.querySelector('#sheet .ways')", "the chooser")
    still(b)


def still(b) -> None:
    """The sheet pops in with a scale: measure only after every animation has finished."""
    wait_for(b, "document.getAnimations().every((a) => a.playState !== 'running')", "the sheet to stop animating")
    b.settle()


def fill_plaid_slots(path: Path, n: int = 10) -> None:
    conn = db.connect(path)
    try:
        with conn:
            have = db.slots_used(conn)
            for i in range(have, n):
                conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, "
                             "created_at) VALUES (?, ?, ?, 'access-production-x', 'active', 'now')",
                             (f"item_fill_{i}", f"ins_fill_{i}", f"Filler bank {i}"))
            assert db.slots_used(conn) >= n
    finally:
        conn.close()


def free_plaid_slots(path: Path) -> None:
    """A database with room: removed Plaid items still count, so delete the gate DB's extras outright."""
    conn = db.connect(path)
    try:
        with conn:
            conn.execute("DELETE FROM transactions WHERE item_id IN (SELECT item_id FROM items WHERE provider = 'plaid' "
                         "AND item_id LIKE 'item_fill_%')")
            conn.execute("DELETE FROM items WHERE item_id LIKE 'item_fill_%'")
        assert db.slots_used(conn) < 10
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------------------------- B1

CARDS = """(() => { const R = (e) => { const r = e.getBoundingClientRect(); return {l: r.left, t: r.top, r: r.right, b: r.bottom,
    w: r.width, h: r.height}; };
  const sheet = document.getElementById('sheet');
  const ways = [...sheet.querySelectorAll('.way')];
  return {sheet: R(sheet), iw: innerWidth,
    ways: ways.map((w) => ({box: R(w), rows: [...w.children].map((c) => c.className.split(' ')[0]),
      head: R(w.querySelector('.way-h')), pro: R(w.querySelector('.way-pro')), con: R(w.querySelector('.way-con')),
      btn: R(w.querySelector('.btn.w')), btnDisabled: w.querySelector('.btn.w').disabled,
      describedby: w.querySelector('.btn.w').getAttribute('aria-describedby'),
      bg: getComputedStyle(w.querySelector('.btn.w')).backgroundColor,
      radius: getComputedStyle(w.querySelector('.btn.w')).borderRadius})),
    meters: sheet.querySelectorAll('.slotm, .meter, .way-slot, [role=progressbar]').length,
    slotText: /of \\d+ used/.test(sheet.textContent)}; })()"""


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", WIDTHS)
def test_both_cards_have_the_same_four_rows_and_one_button(chrome, site, width, theme):
    free_plaid_slots(site.path)
    go(chrome, site, width, theme)
    open_chooser(chrome)
    got = chrome.js(CARDS)
    a, b = got["ways"]
    assert a["rows"] == b["rows"] == ["way-h", "way-pro", "way-con", "btn"]
    # one button: same height (48), same width, same fill; the PLAID button is not the odd one out
    assert a["btn"]["h"] == b["btn"]["h"] == 48, (a["btn"], b["btn"])
    assert abs(a["btn"]["w"] - b["btn"]["w"]) < 1, (a["btn"], b["btn"])
    assert a["bg"] == b["bg"] and a["radius"] == b["radius"]
    assert not got["meters"] and not got["slotText"], "the slot meter belongs to Settings, not to this card"
    if width == 1280:     # side by side: each section starts on the same line, buttons share one bottom
        assert abs(a["box"]["t"] - b["box"]["t"]) < 1 and abs(a["box"]["b"] - b["box"]["b"]) < 1
        assert abs(a["pro"]["t"] - b["pro"]["t"]) < 1, (a["pro"], b["pro"])
        assert abs(a["con"]["t"] - b["con"]["t"]) < 1, (a["con"], b["con"])
        assert abs(a["btn"]["b"] - b["btn"]["b"]) < 1, (a["btn"], b["btn"])
        assert abs(a["btn"]["t"] - b["btn"]["t"]) < 1
        assert b["box"]["l"] > a["box"]["r"]
    else:                 # one column: stacked, each with its own four rows
        assert b["box"]["t"] >= a["box"]["b"] - 0.5 and abs(a["box"]["l"] - b["box"]["l"]) < 1
    chrome.problems()


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", WIDTHS)
def test_plaid_is_full_says_so_once_and_disables_only_plaid(chrome, site, width, theme):
    fill_plaid_slots(site.path)
    go(chrome, site, width, theme)
    open_chooser(chrome)
    got = chrome.js(CARDS)
    line = chrome.js("""(() => { const e = document.querySelector('#sheet .way-full');
      return e && {text: e.textContent.replace(/\\s+/g, ' ').trim(), id: e.id, role: e.getAttribute('role'),
        above: e.getBoundingClientRect().bottom <= document.querySelector('#sheet .way').getBoundingClientRect().top}; })()""")
    assert line and line["text"] == "Plaid is full. SimpleFIN doesn't use a Plaid connection.", line
    assert line["above"] and line["id"]
    plaid, sfin = got["ways"]
    assert plaid["btnDisabled"] is True and plaid["describedby"] == line["id"]
    assert sfin["btnDisabled"] is False and not sfin["describedby"]
    assert not got["meters"] and not got["slotText"]
    assert plaid["btn"]["h"] == sfin["btn"]["h"] == 48 and abs(plaid["btn"]["w"] - sfin["btn"]["w"]) < 1
    chrome.problems()


def test_with_connections_left_nothing_about_slots_is_on_the_sheet(chrome, site):
    free_plaid_slots(site.path)
    go(chrome, site, 1280, "dark")
    open_chooser(chrome)
    got = chrome.js(CARDS)
    assert chrome.js("document.querySelector('#sheet .way-full')") is None
    assert "Plaid is full" not in chrome.js("document.getElementById('sheet').textContent")
    plaid = got["ways"][0]
    assert plaid["btnDisabled"] is False and not plaid["describedby"] and not got["meters"]


@pytest.mark.parametrize("theme", THEMES)
def test_wide_sheets_are_flush_on_a_phone(chrome, site, theme, monkeypatch):
    monkeypatch.setattr(simplefin, "urllib_transport", Bridge(200))
    monkeypatch.setattr(simplefin.socket, "getaddrinfo", PUBLIC)
    go(chrome, site, 390, theme)
    open_chooser(chrome)
    seen = {}

    def flush(name, wide=True):
        still(chrome)
        r = chrome.js("""(() => { const r = document.getElementById('sheet').getBoundingClientRect();
          return {l: r.left, r: r.right, iw: innerWidth, wide: document.getElementById('sheet').classList.contains('wide')}; })()""")
        seen[name] = r
        assert r["wide"] or not wide, (name, r)
        assert r["l"] == 0 and r["r"] == r["iw"], (name, r)

    flush("chooser")
    chrome.js("""[...document.querySelectorAll('#sheet .btn.w')].find((x) => x.textContent.includes('Continue with SimpleFIN')).click()""")
    wait_for(chrome, "document.querySelector('#sheet input')", "the token field")
    flush("token", wide=False)
    chrome.js("""(() => { const i = document.querySelector('#sheet input'); i.value = %s;
      i.dispatchEvent(new Event('input', {bubbles: true}));
      [...document.querySelectorAll('#sheet .btn.w')].find((b) => b.textContent.trim() === 'Connect').click(); return 1; })()"""
              % json.dumps(TOKEN))
    wait_for(chrome, "document.querySelector('#sheet .sf-acct')", "the confirm rows", 9000)
    flush("confirm")
    assert set(seen) == {"chooser", "token", "confirm"}


LONG_PAYLOAD = {"errlist": [], "connections": [{"conn_id": "CON-9", "name": "Northwestern Credit Union Online Banking"}],
                "accounts": [{"id": "ACT-9", "name": "Premier Platinum Rewards Business Checking",
                              "conn_id": "CON-9", "currency": "USD", "balance": "1234567.89",
                              "available-balance": "1234567.89", "balance-date": 1794787200, "transactions": []}]}


def Bridge(status=200, data=None):
    def call(method, url, headers, body, timeout):
        if method == "POST":
            return simplefin.Response(status, ACCESS.encode() if status == 200 else b"Forbidden")
        return simplefin.Response(200, json.dumps(data or LONG_PAYLOAD).encode())
    return call


@pytest.mark.parametrize("theme", THEMES)
def test_the_confirm_row_keeps_its_amount_whole_at_390(chrome, site, theme, monkeypatch):
    monkeypatch.setattr(simplefin, "urllib_transport", Bridge(200))
    monkeypatch.setattr(simplefin.socket, "getaddrinfo", PUBLIC)
    go(chrome, site, 390, theme)
    open_chooser(chrome)
    chrome.js("""[...document.querySelectorAll('#sheet .btn.w')].find((x) => x.textContent.includes('Continue with SimpleFIN')).click()""")
    wait_for(chrome, "document.querySelector('#sheet input')", "the token field")
    chrome.js("""(() => { const i = document.querySelector('#sheet input'); i.value = %s;
      i.dispatchEvent(new Event('input', {bubbles: true}));
      [...document.querySelectorAll('#sheet .btn.w')].find((b) => b.textContent.trim() === 'Connect').click(); return 1; })()"""
              % json.dumps(TOKEN))
    wait_for(chrome, "document.querySelector('#sheet .sf-acct')", "the confirm rows", 9000)
    still(chrome)
    got = chrome.js("""(() => { const m = document.querySelector('#sheet .sf-acct .meta2');
      const box = m.getBoundingClientRect(); const walker = document.createTreeWalker(m, NodeFilter.SHOW_TEXT);
      let node, hit = null;
      while ((node = walker.nextNode())) { const at = node.data.indexOf('$1,234,567.89'); if (at >= 0) { hit = [node, at]; break; } }
      if (!hit) return {found: false, text: m.textContent};
      const range = document.createRange(); range.setStart(hit[0], hit[1]); range.setEnd(hit[0], hit[1] + 13);
      const r = range.getBoundingClientRect();
      const sheet = document.getElementById('sheet').getBoundingClientRect();
      return {found: true, amountRight: r.right, amountLeft: r.left, boxRight: box.right, boxLeft: box.left,
        sheetRight: sheet.right, text: m.textContent, clipped: m.scrollWidth > m.clientWidth + 0.5,
        label: [...m.querySelectorAll('.mt, .chip2')].map((e) => e.scrollWidth > e.clientWidth + 0.5)}; })()""")
    assert got["found"], got
    assert got["amountRight"] <= got["boxRight"] + 0.5, got      # the amount is inside the visible box, not clipped
    assert got["amountLeft"] >= got["boxLeft"] - 0.5, got
    assert got["amountRight"] <= got["sheetRight"], got
    assert "looks like checking" in got["text"] or "looks like" in got["text"]
    chrome.problems()


# ---------------------------------------------------------------------------------------------------------------- B4
import cli_stub as cs                                           # noqa: E402  (the B4 tests' fake CLIs)
from hermes_finance import clibrain, prefs                      # noqa: E402

VERSION = cs.VERSION_CLAUDE.split()[0]
CODEX_VERSION = cs.VERSION_CODEX.split()[-1]
PASSING = cs.claude_stream({"type": "system", "subtype": "init", "tools": [], "mcp_servers": []},
                           {"type": "result", "result": json.dumps(
                               {"kind": "answer", "text": "Nothing was readable, writable or reachable."})})


@pytest.fixture
def stub(tmp_path, monkeypatch):
    s = cs.StubCLIs(tmp_path / "cli", monkeypatch)
    monkeypatch.delenv("HERMES_CLI_BRAINS", raising=False)
    return s


@pytest.fixture
def no_codex(tmp_path, monkeypatch, stub):
    """Codex is nowhere: no env var, an empty home, a minimal PATH (this machine has a real one in ~/.local/bin)."""
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.delenv("FINANCE_CODEX_BIN", raising=False)
    monkeypatch.delenv("NPM_CONFIG_PREFIX", raising=False)
    assert clibrain.executable("codex") is None
    return home


def cli_site(tmp_path, base, cli: str = "claude") -> Server:
    path = tmp_path / "finance.db"
    shutil.copy(base, path)
    with db.connect(path) as conn:
        prefs.save(conn, {"ai.mode": "cli", "ai.cli": cli})
    srv = Server(path, today=TODAY)
    srv.path = path
    return srv


def seed_check(path: Path, cli: str, version: str, passed: bool = True, signed_out: bool = False) -> None:
    with db.connect(path) as conn:
        clibrain.save_check(conn, cli, version, passed, "" if passed else "login expired", {}, signed_out=signed_out)


def open_cli(b, srv: Server, width: int = 1280, theme: str = "dark") -> None:
    b.size(width, 900 if width > 500 else 844)
    b.goto(f"{srv.url}/?t={time.time():.6f}#/settings")
    b.js(f"window.HFTheme.set({json.dumps(theme)})")
    b.settle()
    # Wait for the provider control itself, then click it. `settle()` is frames plus 60 ms, and the segmented control
    # is drawn from the page's `/api/settings/page` answer: on a loaded runner that answer lands later, `.find()`
    # returned undefined and the click threw `Cannot read properties of undefined (reading 'click')` (Nightly
    # 45589087302; the sibling test hit it in 45549226925). Same find-click-and-report shape as `open_chooser`.
    wait_for(b, """(() => { const e = [...document.querySelectorAll('[aria-label="AI provider"] button')]
      .find((x) => x.textContent === 'CLI'); if (e) e.click(); return Boolean(e); })()""",
             "the CLI provider button")
    wait_for(b, "document.querySelector('.aibody .aist')", "the CLI card")
    b.settle()


CLI_CARD = """(() => { const T = (e) => (e ? e.textContent.replace(/\\s+/g, ' ').trim() : null);
  const body = document.querySelector('.aibody');
  const rows = [...body.querySelectorAll('.srow')].map((r) => ({label: T(r.querySelector('b')), sub: T(r.querySelector('.grow > span')),
    buttons: [...r.querySelectorAll('button')].map(T), text: T(r), code: T(r.querySelector('code')),
    input: r.querySelector('input') ? {value: r.querySelector('input').value, label: r.querySelector('input').getAttribute('aria-label'),
      r: r.querySelector('input').getBoundingClientRect().right} : null,
    err: T(r.querySelector('.ferr, .formerr')), r: r.getBoundingClientRect().right}));
  const st = body.querySelector('.aist');
  return {strip: T(st), name: T(st.querySelector('.grow b')), lines: [...st.querySelectorAll('.grow > span')].map(T),
    pill: T(st.querySelector('.pill')), rows, labels: rows.map((r) => r.label), all: T(body), iw: innerWidth,
    scroll: document.documentElement.scrollWidth}; })()"""


def card(b) -> dict:
    return b.js(CLI_CARD)


def row(got: dict, label: str) -> dict:
    return next(r for r in got["rows"] if r["label"] == label)


def test_found_and_signed_in_says_so_with_the_check_date(chrome, tmp_path, base, stub):
    srv = cli_site(tmp_path, base)
    try:
        seed_check(srv.path, "claude", VERSION)
        open_cli(chrome, srv)
        got = card(chrome)
        assert got["name"] == f"Claude Code {VERSION}"
        assert got["lines"][0] == cs.shown(stub.claude.path)
        assert got["pill"] == "Signed in"
        assert len(got["lines"]) == 2 and got["lines"][1].startswith("Checked "), got["lines"]
        iso = row(got, "Isolation check")
        assert f"Passed · {VERSION}" in iso["text"] and iso["buttons"] == ["Check again"]
        assert "Sign in" not in got["labels"] and "Path to claude" not in got["labels"]
        chrome.problems()
    finally:
        srv.close()


@pytest.mark.parametrize("seeded", ["nothing", "other version", "failed isolation"])
def test_signed_in_is_never_shown_without_a_stored_pass_for_this_version(chrome, tmp_path, base, stub, seeded):
    srv = cli_site(tmp_path, base)
    try:
        if seeded == "other version":
            seed_check(srv.path, "claude", "0.0.1")
        elif seeded == "failed isolation":
            seed_check(srv.path, "claude", VERSION, passed=False)
        open_cli(chrome, srv)
        got = card(chrome)
        assert "Signed in" not in got["all"] and "Signed out" not in got["all"], got["strip"]
        if seeded == "failed isolation":        # the isolation verdict is the news there; still no "Signed in"
            assert got["pill"] == "Not used"
        else:                                   # no check for this version: no badge at all
            assert got["pill"] is None
        assert len(got["lines"]) == 1            # no check date line without a proof
        chrome.problems()
    finally:
        srv.close()


def test_not_found_says_where_it_looked_and_offers_a_path_box(chrome, tmp_path, base, stub, no_codex):
    srv = cli_site(tmp_path, base, "codex")
    try:
        open_cli(chrome, srv)
        got = card(chrome)
        searched = srv.api("/api/ai/cli")["items"][1]
        assert searched["found"] is False and searched["id"] == "codex"
        assert got["name"] == "Codex" and got["pill"] == "Not found"
        want = "Not found in PATH, " + ", ".join(searched["searched"][:-1]) + " or " + searched["searched"][-1]
        assert got["lines"] == [want], got["lines"]
        assert "~/.local/bin" in want and "/opt/homebrew/bin" in want and "/usr/local/bin" in want
        assert "Isolation check" not in got["labels"], "nothing to check"
        path = row(got, "Path to codex")
        assert path["sub"] == "Already installed? Paste where it lives."
        assert path["buttons"] == ["Save"] and path["input"]["label"] == "Path to codex" and path["input"]["value"] == ""
        assert "Sign in" not in got["labels"]
        chrome.problems()
    finally:
        srv.close()


def type_path(b, value: str) -> None:
    b.js("""(() => { const i = document.querySelector('.aibody .srow input[aria-label^="Path to"]'); i.focus(); i.value = %s;
      i.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()""" % json.dumps(value))


def click_button(b, label: str, scope: str = ".aibody") -> None:
    b.js("""(() => { const e = [...document.querySelectorAll(%s + ' button')].find((x) => x.textContent.trim() === %s);
      if (!e) throw new Error('no button ' + %s); e.click(); return 1; })()""" % (json.dumps(scope), json.dumps(label), json.dumps(label)))


def test_the_path_box_saves_and_flips_the_card_to_found(chrome, tmp_path, base, stub, no_codex):
    srv = cli_site(tmp_path, base, "codex")
    try:
        open_cli(chrome, srv)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        shutil.copy(stub.codex.path, elsewhere / "codex")
        (elsewhere / "codex").chmod(0o755)
        # a path that isn't the CLI's own binary: the server's plain error shows inline, the card stays not found
        type_path(chrome, str(tmp_path / "nowhere" / "codex"))
        click_button(chrome, "Save")
        wait_for(chrome, "document.querySelector('.aibody .srow .ferr, .aibody .srow .formerr')?.textContent.trim()", "the inline error")
        got = card(chrome)
        assert row(got, "Path to codex")["err"] == "That isn't an executable file named codex.", row(got, "Path to codex")
        assert got["pill"] == "Not found"
        # the real file: Save re-reads the state and the card is Found
        type_path(chrome, str(elsewhere / "codex"))
        click_button(chrome, "Save")
        wait_for(chrome, "document.querySelector('.aibody .aist .grow b')?.textContent.startsWith('Codex ' )", "the Found card")
        got = card(chrome)
        assert got["name"].startswith("Codex ") and CODEX_VERSION in got["name"], got["name"]
        assert got["lines"][0] == cs.shown(elsewhere / "codex")
        assert got["pill"] is None and "Path to codex" not in got["labels"]
        assert "Isolation check" in got["labels"]
        with db.connect(srv.path) as conn:
            assert prefs.get(conn, "ai.cli_path.codex") == str(elsewhere / "codex")
        chrome.problems()
    finally:
        srv.close()


def test_signed_out_shows_the_login_command_and_check_again_flips_it(chrome, tmp_path, base, stub):
    srv = cli_site(tmp_path, base)
    try:
        seed_check(srv.path, "claude", VERSION, passed=False, signed_out=True)
        open_cli(chrome, srv)
        got = card(chrome)
        assert got["pill"] == "Signed out" and got["name"] == f"Claude Code {VERSION}"
        sign = row(got, "Sign in")
        assert sign["sub"] == "Run this in a terminal, then check again."
        assert sign["code"] == "claude auth login" == srv.api("/api/ai/cli")["items"][0]["login_command"]
        assert sign["buttons"] == ["Copy", "Check again"], sign["buttons"]
        assert "Isolation check" not in got["labels"]
        # Copy: navigator.clipboard when there is one ...
        chrome.js("""(() => { window.__copied = []; Object.defineProperty(navigator, 'clipboard', {configurable: true,
          value: {writeText: (t) => { window.__copied.push(t); return Promise.resolve(); }}}); return 1; })()""")
        click_button(chrome, "Copy")
        wait_for(chrome, "window.__copied.length === 1", "the clipboard write")
        assert chrome.js("window.__copied") == ["claude auth login"]
        assert chrome.js("[...document.querySelectorAll('.aibody .srow button')].some((x) => /Copied/.test(x.textContent))")
        # ... and it selects the text when there isn't
        chrome.js("Object.defineProperty(navigator, 'clipboard', {configurable: true, value: undefined}); getSelection().removeAllRanges(); 1")
        wait_for(chrome, "[...document.querySelectorAll('.aibody .srow button')].some((x) => x.textContent.trim() === 'Copy')",
                 "Copied to go back to Copy")
        click_button(chrome, "Copy")
        wait_for(chrome, "getSelection().toString().trim() === 'claude auth login'", "the selected command")
        # Check again posts the existing check and re-renders: the stub's login works, so the card turns Signed in
        stub.claude.script(PASSING)
        click_button(chrome, "Check again")
        wait_for(chrome, "document.querySelector('.aibody .aist .pill')?.textContent === 'Signed in'", "Signed in", 20000)
        got = card(chrome)
        assert "Sign in" not in got["labels"] and "Isolation check" in got["labels"]
        assert got["lines"][1].startswith("Checked ")
        chrome.problems()
    finally:
        srv.close()


def test_signed_out_codex_reads_codex_login(chrome, tmp_path, base, stub):
    srv = cli_site(tmp_path, base, "codex")
    try:
        seed_check(srv.path, "codex", CODEX_VERSION, passed=False, signed_out=True)
        open_cli(chrome, srv)
        got = card(chrome)
        assert got["pill"] == "Signed out" and row(got, "Sign in")["code"] == "codex login"
    finally:
        srv.close()


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("state", ["found", "missing", "signed_out", "unknown"])
def test_the_cli_card_fits_a_phone(chrome, tmp_path, base, stub, monkeypatch, state, theme):
    if state == "missing":
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
        (tmp_path / "empty-home").mkdir()
        monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        monkeypatch.delenv("FINANCE_CODEX_BIN", raising=False)
    srv = cli_site(tmp_path, base, "codex" if state == "missing" else "claude")
    try:
        if state == "found":
            seed_check(srv.path, "claude", VERSION)
        elif state == "signed_out":
            seed_check(srv.path, "claude", VERSION, passed=False, signed_out=True)
        open_cli(chrome, srv, 390, theme)
        got = card(chrome)
        assert got["scroll"] <= got["iw"], got["scroll"]
        assert all(r["r"] <= got["iw"] for r in got["rows"])
        assert all(r["input"] is None or r["input"]["r"] <= got["iw"] for r in got["rows"])
        chrome.problems()
    finally:
        srv.close()


# ---------------------------------------------------------------------------------------------------------------
# B2: popovers close when the page moves under them (static/popclose.js), and focus goes back to the trigger.

SETTLE_FRAMES = "new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 250))))"


def pick_trigger(b, selector: str) -> None:
    """Remember the first visible match as window.__t and put it fully on screen (instant scroll, then wait it out)."""
    b.js(f"""(() => {{ const room = document.documentElement.scrollHeight - innerHeight - 100;   // a trigger with 220 px to scroll below it
      const t = [...document.querySelectorAll({json.dumps(selector)})].find((x) => x.getClientRects().length && x.getBoundingClientRect().top + scrollY <= room);
      if (!t) throw new Error('no visible ' + {json.dumps(selector)}); window.scrollTo({{top: 0, behavior: 'instant'}});
      t.scrollIntoView({{block: 'start', behavior: 'instant'}}); window.scrollBy({{top: -140, behavior: 'instant'}}); window.__t = t; return 1; }})()""")
    b.js(SETTLE_FRAMES)


def scroll_page(b, dy: int = 220) -> int:
    before = b.js("scrollY")
    b.js(f"window.scrollBy({{top: {dy}, behavior: 'instant'}})")
    after = b.js("scrollY")
    assert after - before == dy, ("no room to scroll the page", before, after)
    return after


def closed_and_focused(b, gone: str, y: int) -> dict:
    wait_for(b, f"!document.querySelector({json.dumps(gone)})", f"{gone} to close after the page scrolled")
    b.js(SETTLE_FRAMES)
    return b.js("({y: scrollY, focus: document.activeElement === window.__t, active: document.activeElement.className})") | {"y0": y}


@pytest.mark.parametrize("width", WIDTHS)
def test_the_category_picker_closes_when_the_page_scrolls_and_not_when_its_list_does(chrome, site, width):
    go(chrome, site, width, "dark", "transactions")
    chrome.size(width, 520 if width > 800 else 844)       # short enough that the list itself has to scroll
    wait_for(chrome, "document.querySelector('td.c-cat .catb')", "the transaction rows")
    pick_trigger(chrome, "td.c-cat .catb")
    chrome.js("window.__t.click()")
    wait_for(chrome, "document.querySelector('.cpk .cpkl')", "the picker")
    chrome.js(SETTLE_FRAMES)
    # scrolling inside its own list scrolls the list and leaves it open
    inner = chrome.js("(() => { const l = document.querySelector('.cpk .cpkl'); l.scrollTop = 60; return [l.scrollHeight > l.clientHeight, l.scrollTop]; })()")
    assert inner[0] and inner[1] > 0, inner
    chrome.js(SETTLE_FRAMES)
    assert chrome.js("Boolean(document.querySelector('.cpk'))"), "scrolling the list closed the picker"
    # scrolling the page 220 px closes it, the page stays where it scrolled to, focus goes back to the row's button
    y = scroll_page(chrome)
    got = closed_and_focused(chrome, ".cpk", y)
    assert got["y"] == y, got
    assert got["focus"], got
    assert chrome.js("window.__t.getAttribute('aria-expanded')") == "false"
    assert not chrome.js("Boolean(document.querySelector('.cpkscrim'))")
    chrome.problems()


def test_the_category_picker_closes_on_a_resize(chrome, site):
    go(chrome, site, 1280, "dark", "transactions")
    wait_for(chrome, "document.querySelector('td.c-cat .catb')", "the transaction rows")
    pick_trigger(chrome, "td.c-cat .catb")
    chrome.js("window.__t.click()")
    wait_for(chrome, "document.querySelector('.cpk .cpkl')", "the picker")
    chrome.js(SETTLE_FRAMES)
    try:
        chrome.size(1120, 900)
        wait_for(chrome, "!document.querySelector('.cpk')", "the picker to close on a resize")
        assert chrome.js("document.activeElement === window.__t")
    finally:
        chrome.size(1280, 900)


NEWCAT = """(() => { window.__seen = []; new MutationObserver(() => { const on = document.querySelector('#modal').classList.contains('on');
  if (on && !window.__seen.length) window.__seen.push({picker: Boolean(document.querySelector('.cpk')), title: document.querySelector('#sheet .shd h2')?.textContent}); })
  .observe(document.querySelector('#modal'), {attributes: true}); return 1; })()"""


def open_new_category_from_the_picker(b) -> None:
    pick_trigger(b, "td.c-cat .catb")
    b.js(NEWCAT)
    b.js("window.__t.click()")
    wait_for(b, "document.querySelector('.cpk .cpkadd .cpko')", "the New category row")
    b.js(SETTLE_FRAMES)
    b.js("document.querySelector('.cpk .cpkadd .cpko').click()")
    wait_for(b, "document.querySelector('#modal.on #sheet .shd h2')", "the New category sheet")
    still(b)


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("leave", ["Cancel", "Close", "Escape"])
def test_new_category_closes_the_picker_first_and_leaving_returns_to_the_row(chrome, site, width, leave):
    go(chrome, site, width, "dark", "transactions")
    wait_for(chrome, "document.querySelector('td.c-cat .catb')", "the transaction rows")
    open_new_category_from_the_picker(chrome)
    seen = chrome.js("window.__seen")
    assert seen == [{"picker": False, "title": "New category"}], seen      # the picker was gone before the sheet showed
    assert not chrome.js("Boolean(document.querySelector('.cpk, .cpkscrim'))")
    if leave == "Cancel":
        chrome.js("[...document.querySelectorAll('#sheet .row .btn')].find((x) => x.textContent === 'Cancel').click()")
    elif leave == "Close":
        chrome.js("document.querySelector('#sheet .shd .ib[aria-label=Close]').click()")
    else:
        chrome.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
    wait_for(chrome, "!document.querySelector('#modal.on')", "the sheet to close")
    assert chrome.js("document.activeElement === window.__t"), chrome.js("document.activeElement.outerHTML.slice(0, 120)")
    chrome.problems()


def test_add_category_gives_the_row_the_new_category_and_focus(chrome, site):
    go(chrome, site, 1280, "dark", "transactions")
    wait_for(chrome, "document.querySelector('td.c-cat .catb')", "the transaction rows")
    open_new_category_from_the_picker(chrome)
    tid = chrome.js("window.__t.closest('tr').dataset.tid")
    chrome.js("""(() => { const i = document.querySelector('#sheet .fld input'); i.value = 'Pet food';
      i.dispatchEvent(new Event('input', {bubbles: true})); })()""")
    chrome.js("[...document.querySelectorAll('#sheet .row .btn')].find((x) => x.textContent === 'Add category').click()")
    wait_for(chrome, f"document.querySelector('tr[data-tid=\"{tid}\"] .catb .catn')?.textContent === 'Pet food'", "the row to take Pet food")
    wait_for(chrome, f"!document.querySelector('tr[data-tid=\"{tid}\"] .catb').disabled", "the row's save to finish")
    assert chrome.js(f"document.activeElement === document.querySelector('tr[data-tid=\"{tid}\"] .catb')"), \
        chrome.js("document.activeElement.outerHTML.slice(0, 120)")
    assert any(c["name"] == "Pet food" for c in site.api("/api/categories"))
    chrome.problems()


def test_the_time_window_picker_closes_when_the_page_scrolls(chrome, site):
    go(chrome, site, 1280, "dark", "transactions")
    wait_for(chrome, "document.querySelector('td.c-cat .catb')", "the transaction rows")
    chrome.js("window.scrollTo({top: 0, behavior: 'instant'}); window.__t = document.querySelector('#monl')")
    chrome.js(SETTLE_FRAMES)
    y0 = chrome.js("scrollY")
    chrome.js("window.__t.click()")
    wait_for(chrome, "document.querySelector('.rgpicker')", "the Time window picker")
    chrome.js(SETTLE_FRAMES)
    assert chrome.js("scrollY") == y0, "opening the picker moved the page"
    y = scroll_page(chrome)
    got = closed_and_focused(chrome, ".rgpicker", y)
    assert got["y"] == y and got["focus"], got
    assert not chrome.js("Boolean(document.querySelector('.rgscrim'))")
    assert chrome.js("window.__t.getAttribute('aria-expanded')") == "false"


@pytest.mark.parametrize("kind", ["long", "short"])
def test_help_panels_close_when_the_page_scrolls(chrome, site, kind):
    go(chrome, site, 1280, "dark", "settings")
    wait_for(chrome, "document.querySelector('.help-trigger')", "a help trigger")
    if kind == "long":     # the Data card's explanation sits near the bottom of Settings: scroll the page up instead
        chrome.js("""(() => { const t = document.querySelector('.help-trigger[data-help=data]'); t.scrollIntoView({block: 'center', behavior: 'instant'});
          window.__t = t; return 1; })()""")
        chrome.js(SETTLE_FRAMES)
    else:
        pick_trigger(chrome, ".help-trigger:not([aria-haspopup])")
    chrome.js("window.__t.click()" if kind == "long" else
              "(() => { window.__t.focus({preventScroll: true}); window.__t.dispatchEvent(new PointerEvent('pointerenter')); })()")
    wait_for(chrome, "document.querySelector('.help-panel')", "the help panel")
    chrome.js(SETTLE_FRAMES)
    y = scroll_page(chrome, -220 if kind == "long" else 220)
    got = closed_and_focused(chrome, ".help-panel", y)
    assert got["y"] == y and got["focus"], got
    assert chrome.js("window.__t.getAttribute('aria-expanded')") == "false"


# ---------------------------------------------------------------------------------------------------------------
# B3: the New / Edit category sheet. Group is one plain select like Name; Colour follows the group unless one of the
# eight goal swatches is chosen; the icon picker is the real one and shows the effective colour; tiles draw it.

SWATCHES = ["#4fd1a5", "#8ea8ff", "#5fc6e8", "#c792ea", "#f5b454", "#ff8fb1", "#ff7b7b", "#9ba2b6"]
SWATCH_NAMES = ["Mint", "Blue", "Cyan", "Purple", "Amber", "Pink", "Coral", "Slate"]
GROUP_RGB = {"Food": "rgb(245, 180, 84)", "Misc": "rgb(155, 162, 182)"}


def rgb_of(hex_: str) -> str:
    return "rgb(%d, %d, %d)" % tuple(int(hex_[i:i + 2], 16) for i in (1, 3, 5))


def settings_categories(b, srv, width: int, theme: str) -> None:
    go(b, srv, width, theme, "settings")
    wait_for(b, "document.querySelector('.cats .rsum')", "the Categories card")
    b.js("document.querySelector('.cats .rsum').click()")
    wait_for(b, "!document.querySelector('#categories-body').hidden", "the Categories card to open")


def open_new_sheet(b, srv, width: int = 1280, theme: str = "dark") -> None:
    settings_categories(b, srv, width, theme)
    b.js("document.querySelector('.cats .catnew').click()")
    wait_for(b, "document.querySelector('#modal.on #sheet .shd h2')?.textContent === 'New category'", "the New category sheet")
    still(b)


def edit_sheet(b, srv, name: str, width: int = 1280, theme: str = "dark") -> None:
    settings_categories(b, srv, width, theme)
    b.js("document.querySelector('.cats .rall')?.click()")
    wait_for(b, f"document.querySelector('.catrow[data-cat=\"{name}\"]')", f"the {name} row")
    b.js(f"""(() => {{ const r = document.querySelector('.catrow[data-cat="{name}"]');
      (r.querySelector('[data-act=edit]') || r.querySelector('.rmore')).click();
      if (!document.querySelector('#modal.on')) [...r.querySelectorAll('.rmenu button')].find((x) => /Edit/.test(x.textContent)).click(); }})()""")
    wait_for(b, "document.querySelector('#modal.on #sheet .shd h2')?.textContent === 'Edit category'", "the Edit category sheet")
    still(b)


COLOUR = """(() => { const f = document.querySelector('#sheet .fld[data-f=colour]'); if (!f) return null;
  const vis = (x) => x && x.getClientRects().length > 0;
  const sw = [...f.querySelectorAll('.gcolour')].filter(vis);
  return {label: f.querySelector('label')?.textContent, swatches: sw.map((x) => x.dataset.colour),
    groups: sw.map((x) => x.dataset.group === '1'), names: sw.map((x) => x.getAttribute('aria-label')),
    pressed: sw.filter((x) => x.getAttribute('aria-pressed') === 'true').map((x) => x.dataset.colour),
    other: [...f.querySelectorAll('button')].filter((x) => vis(x) && !x.classList.contains('gcolour')).map((x) => x.textContent),
    text: f.textContent}; })()"""


def click_swatch(b, selector: str) -> None:
    b.js(f"document.querySelector({json.dumps(selector)}).click()")


def press(b, scope: str, label: str) -> None:
    b.js(f"""(() => {{ const x = [...document.querySelectorAll({json.dumps(scope + ' button')})].find((y) => y.textContent.trim() === {json.dumps(label)});
      if (!x) throw new Error('no button ' + {json.dumps(label)}); x.click(); }})()""")


def set_group(b, group: str) -> None:
    b.js(f"""(() => {{ const s = document.querySelector('#sheet select[aria-label=Group]'); s.value = {json.dumps(group)};
      s.dispatchEvent(new Event('change', {{bubbles: true}})); }})()""")


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", WIDTHS)
def test_group_is_one_plain_select_like_name(chrome, site, width, theme):
    open_new_sheet(chrome, site, width, theme)
    got = chrome.js("""(() => { const sel = document.querySelector('#sheet select[aria-label=Group]');
      const name = document.querySelector('#sheet .fld[data-f=name] .inp'); const s = sel.getBoundingClientRect(), n = name.getBoundingClientRect();
      const cs = getComputedStyle(sel), wrap = sel.parentElement;
      return {sh: s.height, nh: n.height, sw: s.width, nw: n.width, boxed: Boolean(sel.closest('.inp')), wrap: wrap.className,
        wrapBorder: parseFloat(getComputedStyle(wrap).borderTopWidth) + parseFloat(getComputedStyle(wrap).borderLeftWidth),
        selBorder: parseFloat(cs.borderTopWidth), radius: cs.borderTopLeftRadius, nameRadius: getComputedStyle(name).borderTopLeftRadius,
        left: s.left - n.left, size: cs.fontSize, nameSize: getComputedStyle(name.querySelector('input')).fontSize}; })()""")
    assert got["sh"] == got["nh"] == 48, got
    assert abs(got["sw"] - got["nw"]) <= 1 and abs(got["left"]) <= 1, got
    assert not got["boxed"] and got["wrapBorder"] == 0 and got["selBorder"] == 1, got      # no bordered box around a select
    assert got["radius"] == got["nameRadius"] and got["size"] == got["nameSize"], got      # (16px on a phone, tap.css)
    chrome.problems()


@pytest.mark.parametrize("theme", THEMES)
def test_the_colour_row_is_the_goal_swatches_with_the_group_preselected(chrome, site, theme):
    open_new_sheet(chrome, site, 1280, theme)
    got = chrome.js(COLOUR)
    assert got["label"] == "Colour", got
    assert got["swatches"] == SWATCHES, got
    assert got["pressed"] == ["#9ba2b6"], got                  # a new category follows Misc, which is Slate
    assert got["groups"] == [c == "#9ba2b6" for c in SWATCHES], got
    assert got["other"] == [], got
    assert not any(word in got["text"] for word in ("Change", "Use ", "Same as")), got
    order = chrome.js("[...document.querySelectorAll('#sheet .fld')].map((f) => f.dataset.f)")
    assert order == ["name", "group", "colour", "icon"], order
    set_group(chrome, "Food")                                  # following the group: the preselection moves
    got = chrome.js(COLOUR)
    assert got["pressed"] == ["#f5b454"] and got["swatches"] == SWATCHES, got
    chrome.problems()


def test_an_override_sticks_and_the_group_swatch_returns_to_following(chrome, site):
    open_new_sheet(chrome, site)
    set_group(chrome, "Food")
    click_swatch(chrome, '#sheet .fld[data-f=colour] .gcolour[data-colour="#ff8fb1"]')
    got = chrome.js(COLOUR)
    assert got["pressed"] == ["#ff8fb1"], got
    assert got["groups"] == [c == "#f5b454" for c in SWATCHES], got
    click_swatch(chrome, '#sheet .fld[data-f=colour] .gcolour[data-colour="#ff8fb1"]')   # not a toggle
    assert chrome.js(COLOUR)["pressed"] == ["#ff8fb1"]
    set_group(chrome, "Home")                                  # an override does not move
    assert chrome.js(COLOUR)["pressed"] == ["#ff8fb1"]
    click_swatch(chrome, '#sheet .fld[data-f=colour] .gcolour[data-group="1"]')   # Home's own swatch: follow again
    assert chrome.js(COLOUR)["pressed"] == ["#8ea8ff"]
    set_group(chrome, "Misc")                                  # following: the preselection moves with the group
    assert chrome.js(COLOUR)["pressed"] == ["#9ba2b6"]
    chrome.problems()


def test_a_group_colour_outside_the_palette_gets_a_leading_swatch(chrome, site):
    open_new_sheet(chrome, site)
    set_group(chrome, "Shopping")
    got = chrome.js(COLOUR)
    assert len(got["swatches"]) == 9 and got["swatches"][1:] == SWATCHES, got
    assert got["swatches"][0] == "rgb(245,158,139)" and got["names"][0] == "Shopping", got
    assert got["groups"] == [True] + [False] * 8, got
    assert got["pressed"] == [got["swatches"][0]], got
    click_swatch(chrome, '#sheet .fld[data-f=colour] .gcolour[data-colour="#ff8fb1"]')
    assert chrome.js(COLOUR)["pressed"] == ["#ff8fb1"]
    set_group(chrome, "Misc")                                  # back in the palette: the leading swatch goes
    got = chrome.js(COLOUR)
    assert got["swatches"] == SWATCHES and got["pressed"] == ["#ff8fb1"], got
    click_swatch(chrome, '#sheet .fld[data-f=colour] .gcolour[data-group="1"]')
    assert chrome.js(COLOUR)["pressed"] == ["#9ba2b6"]
    chrome.problems()


def test_the_icon_picker_is_the_real_one_and_shows_the_effective_colour(chrome, site):
    open_new_sheet(chrome, site)
    ink = "(() => { const t = document.querySelector('#sheet .ipk-quick .gicon'); return getComputedStyle(t).color; })()"
    assert chrome.js("Boolean(document.querySelector('#sheet .ipk .ipk-toggle'))"), "the picker's .ipk wrapper"
    assert chrome.js("document.querySelectorAll('#sheet .ipk-quick .gicon').length") == 16
    set_group(chrome, "Food")
    assert chrome.js(ink) == GROUP_RGB["Food"]
    click_swatch(chrome, '#sheet .fld[data-f=colour] .gcolour[data-colour="#5fc6e8"]')
    assert chrome.js(ink) == rgb_of("#5fc6e8")
    assert chrome.js("document.querySelectorAll('#sheet .ipk-quick .gicon').length") == 16


def add_category(b, name: str) -> None:
    b.js(f"""(() => {{ const i = document.querySelector('#sheet .fld[data-f=name] input'); i.value = {json.dumps(name)};
      i.dispatchEvent(new Event('input', {{bubbles: true}})); }})()""")
    press(b, "#sheet .row", "Add category")
    wait_for(b, "!document.querySelector('#modal.on')", "the sheet to close")


def stored(srv, name: str) -> dict:
    return next(c for c in srv.api("/api/categories") if c["name"] == name)


def test_a_chosen_swatch_is_saved_and_the_tile_draws_it(chrome, site):
    open_new_sheet(chrome, site)
    set_group(chrome, "Food")
    click_swatch(chrome, '#sheet .fld[data-f=colour] .gcolour[data-colour="#ff8fb1"]')
    add_category(chrome, "Pet food")
    got = stored(site, "Pet food")
    assert got["colour"] == "#ff8fb1" and got["effective_colour"] == "#ff8fb1" and got["group"] == "Food", got
    wait_for(chrome, "document.querySelector('.catrow[data-cat=\"Pet food\"] .cb')", "the new row")
    ink = chrome.js("getComputedStyle(document.querySelector('.catrow[data-cat=\"Pet food\"] .cb')).color")
    assert ink == rgb_of("#ff8fb1"), ink
    # every other tile of the group keeps the group's colour; the override reaches the transaction picker too
    other = chrome.js("getComputedStyle(document.querySelector('.catrow[data-cat=Coffee] .cb')).color") \
        if chrome.js("Boolean(document.querySelector('.catrow[data-cat=Coffee]'))") else GROUP_RGB["Food"]
    assert other == GROUP_RGB["Food"], other
    go(chrome, site, 1280, "dark", "transactions")
    wait_for(chrome, "document.querySelector('td.c-cat .catb')", "the transaction rows")
    chrome.js("document.querySelector('td.c-cat .catb').click()")
    wait_for(chrome, "document.querySelector('.cpk [data-cat=\"Pet food\"] .cb')", "Pet food in the picker")
    assert chrome.js("getComputedStyle(document.querySelector('.cpk [data-cat=\"Pet food\"] .cb')).color") == rgb_of("#ff8fb1")
    assert chrome.js("getComputedStyle(document.querySelector('.cpk [data-cat=Coffee] .cb')).color") == GROUP_RGB["Food"]


@pytest.mark.parametrize("group, want", [("Food", "#f5b454"), ("Shopping", "#f59e8b")])
def test_a_new_category_without_a_swatch_follows_its_group(chrome, site, group, want):
    open_new_sheet(chrome, site)
    set_group(chrome, group)
    got = chrome.js(COLOUR)                    # the group's own swatch is selected before anything is picked
    assert got["pressed"] == [{"Food": "#f5b454", "Shopping": "rgb(245,158,139)"}[group]], got
    assert got["groups"] == [c == got["pressed"][0] for c in got["swatches"]], got   # and it is marked as the group's
    add_category(chrome, "Pet food")           # untouched: leaves the group's swatch selected
    got = stored(site, "Pet food")
    assert got["colour"] is None and got["effective_colour"] == want, got


def make_category(site, colour: str | None) -> None:
    conn = db.connect(site.path)
    try:
        catops.create(conn, "Pet food", "Food", "utensils", colour)
    finally:
        conn.close()


@pytest.mark.parametrize("action", ["keep", "reset", "change"])
def test_editing_shows_the_stored_swatch_and_saving_keeps_or_resets_it(chrome, site, action):
    make_category(site, "#ff8fb1")
    edit_sheet(chrome, site, "Pet food")
    got = chrome.js(COLOUR)
    assert got["pressed"] == ["#ff8fb1"], got
    assert got["swatches"] == SWATCHES, got
    if action == "keep":                     # an unrelated edit (the name) must not drop the override
        chrome.js("""(() => { const i = document.querySelector('#sheet .fld[data-f=name] input'); i.value = 'Pet supplies';
          i.dispatchEvent(new Event('input', {bubbles: true})); })()""")
    elif action == "reset":                  # Food's own swatch: follow the group again
        click_swatch(chrome, '#sheet .fld[data-f=colour] .gcolour[data-group="1"]')
    else:
        click_swatch(chrome, '#sheet .fld[data-f=colour] .gcolour[data-colour="#5fc6e8"]')
    press(chrome, "#sheet .row", "Save")
    wait_for(chrome, "!document.querySelector('#modal.on')", "the sheet to close")
    got = stored(site, "Pet supplies" if action == "keep" else "Pet food")
    want = {"keep": "#ff8fb1", "reset": None, "change": "#5fc6e8"}[action]
    assert got["colour"] == want, got
    assert got["effective_colour"] == (want or "#f5b454"), got


def test_the_goal_icon_picker_still_has_its_eight_swatches(chrome, site):
    conn = db.connect(site.path)
    try:
        goals.create(conn, dict(kind="save", name="Trip", track="manual", target=1200, icon="plane", colour="#8ea8ff",
                                target_month=insights.add_months(TODAY.strftime("%Y-%m"), 3)), TODAY)
    finally:
        conn.close()
    go(chrome, site, 1280, "dark", "goals")
    wait_for(chrome, "document.querySelector('#page [data-add=goal]')", "the Add a new goal row")
    chrome.js("document.querySelector('#page [data-add=goal]').click()")
    wait_for(chrome, "document.querySelectorAll('#sheet .gcolours .gcolour').length", "the goal swatches")
    assert chrome.js("[...document.querySelectorAll('#sheet .gcolours .gcolour')].map((b) => b.dataset.colour)") == SWATCHES
    assert chrome.js("Boolean(document.querySelector('#sheet .ipk .ipk-toggle'))")

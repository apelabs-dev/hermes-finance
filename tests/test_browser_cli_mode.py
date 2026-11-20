"""Settings -> AI provider -> CLI and the Ask panel in headless Chrome, against synthetic databases: the fourth mode,
the status strip, the isolation check (passing and failing), the refusal a failed CLI gets, the modes that must not
change, and the phone layout. The CLIs are tests/cli_stub.py's stand-ins (FINANCE_CLAUDE_BIN): the real `claude` or
`codex` is never run, no model is reached, no network."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import cli_stub as cs
import gate_dbs
from cdp import Chrome, Server, find_chrome
from hermes_finance import db, prefs

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
VERSION = cs.VERSION_CLAUDE.split()[0]
SHOTS = Path(__file__).resolve().parent.parent / ".dispatch" / "shots"
PASSING = cs.claude_stream({"type": "system", "subtype": "init", "tools": [], "mcp_servers": []},
                           {"type": "result", "result": json.dumps(
                               {"kind": "answer", "text": "Nothing was readable, writable or reachable."})})
WITH_A_TOOL = cs.claude_stream({"type": "system", "subtype": "init", "tools": [{"name": "Read"}], "mcp_servers": []},
                               {"type": "result", "result": json.dumps(
                                   {"kind": "answer", "text": "Nothing was readable, writable or reachable."})})
REFUSAL = f"Claude Code didn't pass its isolation check. Check it again in Settings \u2192 AI provider."


@pytest.fixture(scope="module")
def browser():
    with Chrome() as b:
        yield b


@pytest.fixture
def stub(tmp_path, monkeypatch):
    s = cs.StubCLIs(tmp_path / "cli", monkeypatch)
    monkeypatch.delenv("HERMES_CLI_BRAINS", raising=False)
    return s


def site_for(tmp_path, mode: str = "hermes") -> Server:
    path = tmp_path / "finance.db"
    gate_dbs.build("full", path)
    with db.connect(path) as conn:
        prefs.save(conn, {"ai.mode": mode})
    return Server(path)


def row_js(label: str) -> str:
    return ("""(() => { const r = [...document.querySelectorAll('.aibody .srow')]
      .find((x) => x.querySelector('b')?.textContent === %s);
      return r ? {text: r.textContent, pill: r.querySelector('.pill')?.textContent || '',
        buttons: [...r.querySelectorAll('button')].map((x) => x.textContent)} : null; })()""" % json.dumps(label))


def pick(b, label: str) -> None:
    b.js(f"[...document.querySelectorAll('[aria-label=\"AI provider\"] button')]"
         f".find((x) => x.textContent === {json.dumps(label)}).click()")
    assert b.until("document.querySelector('[aria-label=\"AI provider\"] [aria-pressed=true]').textContent"
                   f" === {json.dumps(label)} && document.querySelector('.aibody').textContent.length > 0", 8000)


def check(b) -> None:
    b.js("""(() => { const r = [...document.querySelectorAll('.aibody .srow')]
      .find((x) => x.querySelector('b')?.textContent === 'Isolation check');
      [...r.querySelectorAll('button')].find((x) => x.textContent.includes('Check again')).click(); return true; })()""")


def pill_is(b, label: str, want: str) -> bool:
    return bool(b.until(f"(() => {{ const r = {row_js(label)}; return r && r.pill === {json.dumps(want)}; }})()", 20000))


def goto(b, site, width: int = 1280, page: str = "settings") -> None:
    b.size(width, 900 if width > 500 else 844)
    b.goto(f"{site.url}/?t={time.time():.6f}#/{page}")
    assert b.settled(cap_ms=6000) == ''


def test_cli_mode_shows_the_unchecked_cli(browser, tmp_path, stub):
    site = site_for(tmp_path)
    try:
        goto(browser, site, 1280)
        assert browser.js("document.documentElement.dataset.theme") == "dark"
        pick(browser, "CLI")
        assert browser.js("[...document.querySelectorAll('[aria-label=\"AI provider\"] button')]"
                          ".map((x) => x.textContent)") == ["Hermes", "API key", "CLI", "Off"]
        assert browser.js("document.querySelector('[aria-label=\"AI provider\"] [aria-pressed=true]').textContent") == "CLI"
        strip = browser.js("document.querySelector('.aibody .aist').textContent")
        assert f"Claude Code {VERSION}" in strip and f"Found at {cs.shown(stub.claude.path)}" in strip
        assert browser.js("document.querySelector('.aibody .aist .pill').textContent") == "Not checked"
        isolation = browser.js(row_js("Isolation check"))
        assert isolation["pill"] == "Not checked" and "Not checked on this version yet." in isolation["text"]
        assert isolation["buttons"] == ["Check again"]
        changes = browser.js(row_js("Changes still ask you first"))
        assert changes["pill"] == "Always" and "Nothing is applied without your OK" in changes["text"]
        safety = browser.js("document.querySelector('.aibody .safety').textContent")
        assert "your Claude subscription" in safety and "not the monthly cap here" in safety
        assert "Auto-apply confidence" in browser.js("document.querySelector('.aibody').parentElement.textContent")
        assert browser.problems() == []
    finally:
        site.close()


def test_check_again_passes_and_is_stored(browser, tmp_path, stub):
    stub.claude.script(PASSING)
    site = site_for(tmp_path)
    try:
        goto(browser, site)
        pick(browser, "CLI")
        check(browser)
        assert pill_is(browser, "Isolation check", f"Passed · {VERSION}")
        assert browser.js("document.querySelector('.aibody .aist .pill').textContent") == "Locked down"
        assert browser.js("document.querySelector('.aibody .aist .grow > span').textContent").startswith("Found at ")
        stored = next(i for i in site.api("/api/ai/cli")["items"] if i["id"] == "claude")
        assert stored["passed"] is True and stored["checked_version"] == VERSION and stored["version"] == VERSION
        assert browser.problems() == []
    finally:
        site.close()


def test_a_failed_check_is_refused_in_the_ask_panel(browser, tmp_path, stub):
    stub.claude.script(WITH_A_TOOL)
    site = site_for(tmp_path)
    try:
        goto(browser, site)
        pick(browser, "CLI")
        check(browser)
        assert pill_is(browser, "Isolation check", f"Failed · {VERSION}")
        assert browser.js("document.querySelector('.aibody .aist .pill').textContent") == "Not used"
        isolation = browser.js(row_js("Isolation check"))
        assert "it lists tools" in isolation["text"]
        assert browser.until("document.querySelector('#chat .brain').textContent === 'via Claude Code'", 8000)
        browser.js("document.querySelector('#askb').click()")
        assert browser.until("!document.querySelector('#chat').hidden && document.body.classList.contains('chaton')", 8000)
        browser.js("document.querySelector('#chat textarea').value = 'What did I spend on food?';"
                   " document.querySelector('#chsend').click()")
        note = browser.until("document.querySelector('#chat .msg.bot.err')?.textContent || null", 20000) or ''
        assert REFUSAL in note
        assert browser.js("document.querySelectorAll('#chat .msg.bot.err').length") == 1
        assert [p for p in browser.problems() if 'status of 503' not in p] == []   # the refusal is the one 503
    finally:
        site.close()


def test_the_other_modes_are_unchanged(browser, tmp_path, stub):
    site = site_for(tmp_path)
    try:
        goto(browser, site)
        pick(browser, "API key")
        assert browser.until("document.querySelector('.aibody').textContent.includes('Provider')", 8000)
        labels = browser.js("[...document.querySelectorAll('.aibody .srow .grow > b')].map((x) => x.textContent)")
        assert labels[:4] == ["Provider", "Endpoint", "Model", "API key"]
        pick(browser, "Hermes")
        assert browser.until("document.querySelector('.aibody').textContent.includes('Using your Hermes model')", 8000)
        assert "Using your Hermes model" in browser.js("document.querySelector('.aibody').textContent")
        assert browser.problems() == []
    finally:
        site.close()


def test_the_phone_layout_does_not_overflow_and_the_fonts_hold(browser, tmp_path, stub):
    site = site_for(tmp_path, "cli")
    SHOTS.mkdir(parents=True, exist_ok=True)
    try:
        for width in (1280, 390):
            for theme in ("dark", "light"):
                goto(browser, site, width)
                browser.js(f"window.HFTheme.set({json.dumps(theme)})")
                assert browser.settled(cap_ms=6000) == ''
                assert browser.js("document.documentElement.dataset.theme") == theme
                if width == 390:
                    assert browser.js("document.documentElement.scrollWidth <= window.innerWidth")
                    sizes = browser.js("""({
                      body: parseFloat(getComputedStyle(document.querySelector('.aibody')).fontSize),
                      secondary: parseFloat(getComputedStyle(document.querySelector('.aibody .srow .grow > span')).fontSize),
                      status: parseFloat(getComputedStyle(document.querySelector('.aibody .aist .grow > span')).fontSize),
                    })""")
                    assert sizes["body"] >= 15 and sizes["secondary"] >= 13 and sizes["status"] >= 13
                browser.screenshot(SHOTS / f"cli-mode-{width}-{theme}.png", full=True)
        assert browser.problems() == []
    finally:
        site.close()

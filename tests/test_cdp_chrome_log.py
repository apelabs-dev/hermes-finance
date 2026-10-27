"""HF_CHROME_LOG (t_37bf3e0b): each Chrome's own log in that directory, and for one that wedges, what it was doing,
looked at before it's replaced (slow-mode run 4's wedged Chrome left nothing behind: its stderr went to /dev/null and
_recycle stopped it before anyone looked). Unset, a Chrome is started exactly as before and nothing is written.
"""
import os
import subprocess
import time

import pytest

import cdp
from cdp import Chrome, _exited
from test_cdp_recycle import site  # noqa: F401  (the fixture: ok.html and a page that wedges its renderer)

OK_LAUNCH = ["--headless=new", "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
             "--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-extensions",
             "--disable-default-apps", "--hide-scrollbars", "--force-color-profile=srgb", "--mute-audio",
             "--password-store=basic", "--use-mock-keychain", "about:blank"]


@pytest.fixture
def launches(monkeypatch):
    """Every Popen cdp makes: (argv, stderr)."""
    seen, real = [], subprocess.Popen

    def popen(argv, *a, **k):
        seen.append((list(argv), k.get("stderr")))
        return real(argv, *a, **k)

    monkeypatch.setattr(cdp.subprocess, "Popen", popen)
    return seen


def test_unset_starts_chrome_as_before_and_writes_nothing(monkeypatch, launches, tmp_path, site):
    monkeypatch.delenv(cdp.LOG_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    with Chrome() as b:
        b.goto(site + "/ok.html", ready=False)
        assert b.log is None
    [(argv, err)] = [x for x in launches if x[0][0] == b.path]
    assert [a for a in argv[1:] if not a.startswith("--user-data-dir=")] == OK_LAUNCH and err is subprocess.DEVNULL
    assert list(tmp_path.iterdir()) == []


def test_a_clean_chrome_logs_while_it_runs_and_its_log_goes_with_it(monkeypatch, launches, tmp_path, site):
    monkeypatch.setenv(cdp.LOG_ENV, str(tmp_path / "logs"))
    with Chrome() as b:
        b.goto(site + "/ok.html", ready=False)
        assert os.path.dirname(b.log) == str(tmp_path / "logs") and os.path.exists(b.log)
        [(argv, err)] = [x for x in launches if x[0][0] == b.path]
        assert argv[-1 - len(cdp.LOG_ARGS):-1] == cdp.LOG_ARGS and err is not subprocess.DEVNULL
    assert _exited(b.proc) and list((tmp_path / "logs").iterdir()) == []


def test_a_wedged_chrome_is_looked_at_before_it_is_replaced_and_its_log_is_kept(monkeypatch, tmp_path, site):
    logs = tmp_path / "logs"
    monkeypatch.setenv(cdp.LOG_ENV, str(logs))
    with Chrome() as b:
        old_log, old_pid = b.log, b.proc.pid
        with pytest.raises(RuntimeError, match="stuck.html: not (loaded|drawn)"):
            b.goto(site + "/stuck.html")
        assert b.log != old_log and os.path.exists(b.log)   # the replacement's own log
        t = time.monotonic()
        b.goto(site + "/ok.html", ready=False)
        assert time.monotonic() - t < 10
    base = old_log[:-len(".log")]
    names = sorted(p.name for p in logs.iterdir())
    assert all(n.startswith(os.path.basename(base)) for n in names), names   # the clean replacement's log is gone
    assert os.path.basename(old_log) in names and f"{os.path.basename(base)}.wedge.txt" in names, names
    wedge = open(f"{base}.wedge.txt").read()
    assert "why: goto " in wedge and "test_a_wedged_chrome" in wedge, wedge
    assert "/stuck.html" in wedge and f"\n  {old_pid} " in wedge and "--type=renderer" in wedge, wedge
    if cdp.shutil.which("sample"):   # (macOS) the browser's and the renderer's stacks
        samples = [n for n in names if ".sample-" in n]
        assert f"{os.path.basename(base)}.sample-{old_pid}.txt" in samples and len(samples) >= 2, (names, wedge)

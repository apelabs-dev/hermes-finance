"""TEST-SPEED: scripts/check.py without running a check. fast's plan is conservative where it can't be sure (a file every
page shares, a new page's file, the gates' machinery, the synthetic data, the dependencies: every layout state on every
DB; a helper module: the tests that import it; a deleted file: planned by its path like any other); nothing named like a
credential reaches what it runs; and its watchdog stops what the run's ledger says is still running when the run itself
was killed, and nothing else (a pid the ledger names but that is now another process is left alone). Review round 3: a
failed process listing is never taken for "nothing running" (the watchdog, _proc_stat), and a run whose cleanup fails
(its watchdog left a process running, couldn't look, or didn't finish) fails and keeps its record. Review round 4: a
pytest run whose state can't be told is waited for within a bound (its group, empty, is its exit; still running, the
wait fails and leaves its record for the run's stop and the watchdog)."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "check.py"
sys.path.insert(0, str(ROOT / "scripts"))
import check  # noqa: E402


@pytest.fixture(scope="module")
def browser_files():
    return check.test_files()[1]


def plan(files, browser_files):
    return check.plan_fast(files, browser_files)


def everything(p) -> bool:
    return set(p.kinds()) == set(check.ALL_DBS) and all(
        p.select(k, w, t, "any-state", "anypage") for k in check.ALL_DBS for w in (1440, 390) for t in ("dark", "light"))


@pytest.mark.parametrize("f", ["src/hermes_finance/static/app.css", "src/hermes_finance/static/app.js",
                               "src/hermes_finance/static/chat.js", "src/hermes_finance/static/rules.css",
                               "src/hermes_finance/static/gone-now.css", "src/hermes_finance/web.py", "tests/cdp.py",
                               "scripts/audit_layout.py", "scripts/run_gates.py", "tests/gate_dbs.py",
                               "tests/demo_data.py", "tests/pngtools.py", "pyproject.toml", "uv.lock",
                               "src/hermes_finance/templates/new.html"])
def test_shared_new_and_gate_files_run_every_state_on_every_db(f, browser_files):
    p = plan([f], browser_files)
    assert everything(p), (f, p.kinds(), p.why)
    if f.startswith("src/hermes_finance/static/") or f in ("src/hermes_finance/web.py", "pyproject.toml", "uv.lock"):
        assert p.browser == set(browser_files) and p.numbers


def test_the_theme_and_phone_files_run_their_states_on_every_db_the_sign_in_page_included(browser_files):
    light = plan(["src/hermes_finance/static/light.css"], browser_files)
    assert set(light.kinds()) == set(check.ALL_DBS)
    assert light.select("signin", 390, "light", "login", "login") and not light.select("full", 390, "dark", "x", "overview")
    tap = plan(["src/hermes_finance/static/tap.css"], browser_files)
    assert set(tap.kinds()) == set(check.ALL_DBS)
    assert tap.select("empty", 360, "dark", "x", "goals") and not tap.select("empty", 1440, "dark", "x", "goals")


def test_app_code_runs_the_numbers_gate_and_every_state_on_the_rich_and_sign_in_dbs(browser_files):
    p = plan(["src/hermes_finance/some_new_module.py"], browser_files)
    assert p.numbers and set(p.kinds()) == {"full", "features", "signin", "signin-setup"}
    assert p.select("features", 390, "light", "anything", "anypage")
    ins = plan(["src/hermes_finance/insights.py"], browser_files)
    assert ins.numbers and "tests/test_browser_task11.py" in ins.browser


def test_a_helper_module_runs_the_tests_that_import_it(browser_files):
    p = plan(["tests/factory.py"], browser_files)      # imported by test_hotfix1 and test_browser_task14b
    assert {"tests/test_hotfix1.py", "tests/test_browser_task14b.py"} <= p.browser and not p.kinds()
    s = plan(["tests/sync_scene.py"], browser_files)   # test_task22g imports test_task22f, which imports sync_scene
    assert {"tests/test_task22f_sync_ui.py", "tests/test_task22g_plan_switch.py", "tests/test_hotfix1.py"} <= s.browser
    t = plan(["tests/test_task22f_sync_ui.py"], browser_files)
    assert "tests/test_task22g_plan_switch.py" in t.browser


def test_a_page_file_runs_that_page_on_every_db(browser_files):
    p = plan(["src/hermes_finance/static/billcal.js"], browser_files)
    assert set(p.kinds()) == set(check.GATE_KINDS) and p.numbers
    assert p.select("empty", 390, "light", "recurring", "recurring") and not p.select("full", 390, "dark", "x", "overview")


def test_docs_change_nothing_but_the_no_browser_tests_run(browser_files):
    p = plan(["docs/notes.md", "REPORT-SPEED.md"], browser_files)
    assert not p.kinds() and not p.browser and not p.numbers


def test_nothing_named_like_a_credential_is_passed_on(monkeypatch):
    fake = "fake-not-a-secret"
    for k in ("PLAID_SECRET", "PLAID_CLIENT_ID", "APCA_API_KEY_ID", "MARKETCHECK_API_KEY", "RENTCAST_API_KEY",
              "GITHUB_PERSONAL_ACCESS_TOKEN", "SOME_PASSWORD", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(k, fake)
    monkeypatch.setenv("HERMES_PLAID", "sandbox")
    env = check.safe_env()
    assert fake not in env.values()
    assert env["HERMES_PLAID"] == "off" and env["HERMES_ALPACA"] == "off" and env["PATH"]


def _sleeper(*argv) -> subprocess.Popen:
    """A process leading its own group (like a pytest run or a Chrome), its command line naming `argv`."""
    return subprocess.Popen([sys.executable, "-c", "import signal, sys, time\n"
                             "if 'deaf' in sys.argv: signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                             "time.sleep(120)", *argv], start_new_session=True)


def test_the_watchdog_stops_what_the_ledger_says_is_running_and_nothing_else(tmp_path, monkeypatch):
    logs = tmp_path / "hf-check-1-x"
    logs.mkdir()
    profile = tmp_path / "hf-cdp-fake"
    profile.mkdir()
    ledger = logs / "ledger.txt"
    run = _sleeper(str(logs))                   # a pytest run the ledger says hasn't ended: stopped
    done = _sleeper(str(logs))                  # one it says has ended (and so is someone else's by now): left alone
    reused = _sleeper("unrelated")              # a pid it names that is another process now: left alone
    chrome = _sleeper(str(profile), "deaf")     # a Chrome not stopped, deaf to SIGTERM: SIGKILL after the grace
    stopped = _sleeper(str(profile) + "-2")     # a Chrome the ledger says stopped: left alone
    procs = [run, done, reused, chrome, stopped]
    try:
        ledger.write_text(f"group 1.0 {run.pid} nobrowser\ngroup 1.0 {done.pid} browser\nended 2.0 {done.pid} browser\n"
                          f"group 1.0 {reused.pid} gates\n"
                          f"start 1.0 {chrome.pid} {chrome.pid} 1 {profile}\n"
                          f"start 1.0 {stopped.pid} {stopped.pid} 1 {profile}-2\nstop 3.0 {stopped.pid} {stopped.pid} 1 {profile}-2\n")
        t0 = time.monotonic()
        dog = subprocess.run([sys.executable, str(CHECK), "--watchdog", str(ledger)], input=b"", timeout=60,
                             env={**check.safe_env(), "CHECK_WATCHDOG_GRACE": "2"})
        took = time.monotonic() - t0
        assert dog.returncode == 0
        assert run.wait(5) == -signal.SIGTERM
        assert chrome.wait(5) == -signal.SIGKILL and 2 <= took < 15
        assert done.poll() is None and reused.poll() is None and stopped.poll() is None
        assert not profile.exists()
        last = ledger.read_text().splitlines()[-1]
        assert last.startswith("watchdog ") and "groups=2 chromes=1 stopped=2 sigkill=1" in last, last
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait(10)


# --- TEST-SPEED review corrections (F1, F3, F4, F6, the budget's ceiling) ------------------------------------------------

import json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402


def _group(pgid: int, mark: str) -> list[int]:
    """The live processes of group `pgid` whose command line or environment names `mark` (ps -E: ours only)."""
    out = subprocess.run(["ps", check.PS_ENV, "-ww", check.PS_ALL, "-o", "pid=,pgid=,stat=,command="], capture_output=True, text=True).stdout
    return [int(f[0]) for f in (ln.split(None, 3) for ln in out.splitlines())
            if len(f) == 4 and f[1] == str(pgid) and not f[2].startswith("Z") and mark in f[3]]


def _kill_marked(pgid: int, mark: str) -> None:
    """Cleanup: SIGKILL what is left of a test's own group, each process checked to be ours (`mark`) first."""
    for pid in _group(pgid, mark):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


@pytest.fixture
def saved_environ():
    """check.main() makes this process's environment the safe one (and names its ledger in it): put it back after."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


class FakeRun:
    """check.Pytest recorded instead of run: its name, arguments and environment; wait() answers `behave(self)`."""
    made: list = []
    behave = staticmethod(lambda run: {"tests": 10, "failed": 0, "skipped": 0})

    def __init__(self, name, args, logs, env):
        self.name, self.args, self.env, self.logs = name, args, env, logs
        self.xml, self.log, self.ended = logs / f"{name}.xml", logs / f"{name}.log", False
        FakeRun.made.append(self)

    def wait(self):
        return {"name": self.name, "rc": 0, "secs": 0.0, **FakeRun.behave(self)}

    def failures(self):
        return []


@pytest.fixture
def fake_runs(tmp_path, monkeypatch, saved_environ):
    # Unit invocations must not contend with the full check that is running this suite.
    real_lock = check.check_lock
    monkeypatch.setattr(check, "check_lock", lambda: real_lock(tmp_path / "check.lock"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))          # the run's log dir: in tmp_path
    monkeypatch.setattr(signal, "signal", lambda *a: None)           # main()'s handlers: not in this test's process
    monkeypatch.setattr(check, "_CHILDREN", [])
    monkeypatch.setattr(check, "LEDGER", None)                       # (main() names its own: put back after)
    monkeypatch.setattr(check, "Pytest", FakeRun)
    FakeRun.made = []
    FakeRun.behave = staticmethod(lambda run: {"tests": 10, "failed": 0, "skipped": 0})
    return FakeRun


@pytest.mark.parametrize("poison", ["full", "empty", "nocaps,signin", "bogus", ""])
def test_full_runs_the_gates_on_every_db_whatever_gate_kinds_the_shell_has(poison, fake_runs, capsys):
    """F1: an unrestricted `full` hands the gates the complete DB list, not an inherited (narrow, invalid, empty) one."""
    os.environ["GATE_KINDS"] = poison
    os.environ["PYTEST_ADDOPTS"] = "-k gate_1"     # another inherited narrowing: never reaches a check's pytest run
    check.main(["full", "--part", "2"])
    gates = [r for r in fake_runs.made if r.name == "gates"]
    assert len(gates) == 1
    assert gates[0].env["GATE_KINDS"] == ",".join(check.ALL_DBS), gates[0].env.get("GATE_KINDS")
    assert "PYTEST_ADDOPTS" not in gates[0].env


def test_a_split_gates_run_gets_exactly_its_validated_kinds(fake_runs, capsys):
    os.environ["GATE_KINDS"] = "empty"
    check.main(["full", "--part", "2", "--kinds", "nocaps,full"])
    assert [r.env["GATE_KINDS"] for r in fake_runs.made if r.name == "gates"] == ["nocaps,full"]
    for bad in ("bogus", "full,bogus", ",", "full,full"):
        with pytest.raises(SystemExit) as e:
            check.main(["full", "--part", "2", "--kinds", bad])
        assert e.value.code == 2, bad


def _gate_outputs(run, kinds, skip=(), drop_state=None, replace=None, skipped=(), hollow=None):
    """What tests/test_gates.py leaves for check.py: its JUnit XML (a test per gate and DB: `skip` ones skipped) and the
    layout report (every state audit_layout plans for each DB, each with every check's result; `drop_state` = (kind, n):
    n of them missing; review B5: `replace` = (kind, n): n missing and n others there instead, the same count;
    `skipped` = (kind, state, what its SKIP says): that state's entries skipped, as audit_layout writes them; round 3:
    `hollow` = (kind, n): n entries with every check's key but no result in it)."""
    sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
    import audit_layout
    cases = ([f"test_gate_1_numbers[{k}]" for k in check.GATE_KINDS] + [f"test_gate_2_layout[{k}]" for k in check.ALL_DBS]
             + [f"test_gate_3_controls[{k}]" for k in check.CONTROL_KINDS])
    xml = ['<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" errors="0" failures="0" '
           f'skipped="{sum(1 for c in cases if c.split("[")[1][:-1] not in kinds or c in skip)}" tests="{len(cases)}">']
    for c in cases:
        is_skipped = c.split("[")[1][:-1] not in kinds or c in skip
        xml.append(f'<testcase classname="tests.test_gates" name="{c}" time="1">'
                   + ('<skipped type="pytest.skip" message="not in GATE_KINDS">skip</skipped>' if is_skipped else "")
                   + "</testcase>")
    run.xml.write_text("".join(xml) + "</testsuite></testsuites>")
    checks = {c: {"pass": True, "examined": 1, "violations": []} for c in audit_layout.CHECKS}
    reports = {}
    for k in kinds:
        items = [it for it in audit_layout.plan(k, audit_layout.WIDTHS, None) if it[1] in audit_layout.THEMES]
        if drop_state and drop_state[0] == k:
            items = items[drop_state[1]:]
        rep = {"label": k, "widths": {}, "pass": 0, "fail": 0, "selftest": "ok" if k in check.GATE_KINDS else None}
        for w, theme, name, *_ in items:
            key = name if theme == "dark" else f"{name}@{theme}"
            said = [why for kk, state, why in skipped if kk == k and state == name]
            rep["widths"].setdefault(str(w), {})[key] = ({"skipped": f"js failed: Uncaught Error: {said[0]}\n    at x"}
                                                         if said else {"checks": dict(checks)})
            rep["pass"] += 0 if said else len(checks)
        if hollow and hollow[0] == k:
            for w, key in [(w, key) for w, states in rep["widths"].items() for key in states][:hollow[1]]:
                rep["widths"][w][key] = {"checks": {c: None for c in audit_layout.CHECKS}}
        if replace and replace[0] == k:   # n planned states gone, n that nobody planned there instead: the same count
            gone = [(w, key) for w, states in rep["widths"].items() for key in states][:replace[1]]
            for i, (w, key) in enumerate(gone):
                del rep["widths"][w][key]
                rep["widths"][w][f"{key}-{i}"] = {"checks": dict(checks)}
        reports[k] = rep
    out = run.logs / "tmp-gates" / "layout0"
    out.mkdir(parents=True, exist_ok=True)
    (out / "layout-report.json").write_text(json.dumps(reports))
    return {"tests": len(cases), "failed": 0, "skipped": 0}


def test_full_passes_only_when_every_requested_db_and_state_ran(fake_runs, capsys):
    """F1: what the gates were asked for is checked against what they did (a DB's gate test skipped, a layout state
    missing from its report: FAIL), with a clean positive control."""
    every = list(check.ALL_DBS)
    fake_runs.behave = staticmethod(lambda run: _gate_outputs(run, every))
    assert check.main(["full", "--part", "2"]) == 0, capsys.readouterr().out            # everything ran: passes
    fake_runs.behave = staticmethod(lambda run: _gate_outputs(run, every, skip={"test_gate_2_layout[features]"}))
    assert check.main(["full", "--part", "2"]) == 1
    assert "features" in capsys.readouterr().out
    fake_runs.behave = staticmethod(lambda run: _gate_outputs(run, every, skip={"test_gate_3_controls[nocaps]"}))
    assert check.main(["full", "--part", "2"]) == 1                                    # the controls gate skipped on a DB
    assert "test_gate_3_controls[nocaps]: skipped" in capsys.readouterr().out
    fake_runs.behave = staticmethod(lambda run: _gate_outputs(run, ["full"]))         # only one DB ran (the poison)
    assert check.main(["full", "--part", "2"]) == 1
    fake_runs.behave = staticmethod(lambda run: _gate_outputs(run, every, drop_state=("nocaps", 3)))
    assert check.main(["full", "--part", "2"]) == 1
    assert "nocaps" in capsys.readouterr().out
    fake_runs.behave = staticmethod(lambda run: _gate_outputs(run, ["onepoint", "signin"]))   # a split: its DBs only
    assert check.main(["full", "--part", "2", "--kinds", "onepoint,signin"]) == 0, capsys.readouterr().out


def test_a_pytest_run_that_ran_no_tests_fails(tmp_path, monkeypatch):
    """F6: pytest's exit 5 (no tests ran) with a valid JUnit XML of zero tests is a failed phase, not a green one; a run
    of one passing test is the control. Actual pytest processes."""
    monkeypatch.setattr(check, "_CHILDREN", [])
    (tmp_path / "test_nothing.py").write_text('"""no tests here"""\n')
    (tmp_path / "test_one.py").write_text("def test_one():\n    assert True\n")
    logs = tmp_path / "logs"
    logs.mkdir()
    env = check.safe_env(HF_NO_CHROME="1")
    empty = check.Pytest("empty", [str(tmp_path / "test_nothing.py")], logs, env).wait()
    one = check.Pytest("one", [str(tmp_path / "test_one.py")], logs, env).wait()
    assert (logs / "empty.xml").exists() and empty["rc"] == 5 and empty["tests"] == 0
    assert empty["failed"] >= 1, empty
    assert one["rc"] == 0 and one["tests"] == 1 and one["failed"] == 0, one


@pytest.mark.parametrize("how", ["wait", "stop"])
def test_a_pytest_run_whose_leader_dies_takes_its_surviving_workers_with_it(how, tmp_path, monkeypatch):
    """F3: the pytest process (the group's leader) is killed outright while its two xdist workers run a test: they go on
    running. Its run's wait() (or the run's stop, on the way out) must not call the group ended until they are gone
    too; the ledger's `ended` comes after. Actual pytest and xdist processes; cleanup kills only this test's own
    (marked) processes."""
    monkeypatch.setattr(check, "_CHILDREN", [])
    monkeypatch.setattr(check, "LEDGER", tmp_path / "ledger.txt")
    mark = f"HF_F3_MARK=f3-{os.getpid()}-{time.time_ns()}"
    (tmp_path / "test_sleepy.py").write_text("import time\n\n\ndef test_a():\n    time.sleep(90)\n\n\n"
                                             "def test_b():\n    time.sleep(90)\n")
    logs = tmp_path / "logs"
    logs.mkdir()
    run = check.Pytest("sleepy", ["-n", "2", str(tmp_path / "test_sleepy.py")], logs,
                       check.safe_env(HF_NO_CHROME="1", **dict([mark.split("=", 1)])))
    pg = run.proc.pid
    try:
        deadline = time.monotonic() + 60
        while len(_group(pg, mark)) < 3 and time.monotonic() < deadline:   # the leader and its two workers
            time.sleep(0.1)
        assert len(_group(pg, mark)) >= 3, _group(pg, mark)
        time.sleep(1.0)                                                    # (the workers into their tests)
        os.kill(pg, signal.SIGKILL)                                        # the leader only
        deadline = time.monotonic() + 10   # dead (a zombie, or reaped already), as a crash some time before the stop
        while subprocess.run(["ps", "-o", "stat=", "-p", str(pg)], capture_output=True, text=True).stdout.strip()[:1] \
                not in ("Z", "") and time.monotonic() < deadline:
            time.sleep(0.02)
        t0 = time.monotonic()
        if how == "wait":
            r = run.wait()
            assert r["failed"] >= 1, r
        else:
            check._stop_children()
        assert time.monotonic() - t0 < 40
        left = _group(pg, mark)
        assert left == [], f"{len(left)} worker(s) of a dead pytest leader still running: {left}"
        ended = [ln for ln in (tmp_path / "ledger.txt").read_text().splitlines() if ln.split()[:1] == ["ended"]]
        assert [ln.split()[2] for ln in ended] == [str(pg)], ended
    finally:
        _kill_marked(pg, mark)
        if run.proc.returncode is None:
            run.proc.wait(10)


def test_the_watchdog_stops_the_survivors_of_a_group_whose_leader_is_gone(tmp_path):
    """F3/N1, the run itself killed outright: a pytest run's leader that died (reaped since, as launchd does) with a
    worker still in its group, and a Chrome whose main process died with a helper left. The worker has the run's
    environment (HF_CHECK_RUN, as every process check.py starts), the helper the profile on its command line (as
    Chrome's do): both are stopped. A stranger's group the ledger names is left alone."""
    logs = tmp_path / "hf-check-2-x"
    logs.mkdir()
    profile = tmp_path / "hf-cdp-gone"
    profile.mkdir()
    ledger = logs / "ledger.txt"
    spawn = ("import subprocess, sys\n"
             "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)', *sys.argv[1:]])\n"
             "print(p.pid, flush=True)")
    env = {**check.safe_env(), "HF_CHECK_RUN": str(logs)}
    leader = subprocess.Popen([sys.executable, "-c", spawn], env=env, start_new_session=True, stdout=subprocess.PIPE,
                              text=True)
    chrome = subprocess.Popen([sys.executable, "-c", spawn, f"--user-data-dir={profile}"], env=check.safe_env(),
                              start_new_session=True, stdout=subprocess.PIPE, text=True)
    worker, helper = int(leader.stdout.readline()), int(chrome.stdout.readline())
    leader.wait(10)
    chrome.wait(10)
    stranger = _sleeper("unrelated")
    try:
        ledger.write_text(f"group 1.0 {leader.pid} browser\ngroup 1.0 {stranger.pid} gates\n"
                          f"start 1.0 {chrome.pid} {chrome.pid} 1 {profile}\n")
        dog = subprocess.run([sys.executable, str(CHECK), "--watchdog", str(ledger)], input=b"", timeout=60,
                             env={**check.safe_env(), "CHECK_WATCHDOG_GRACE": "2"})
        assert dog.returncode == 0
        deadline = time.monotonic() + 5
        while (_group(leader.pid, str(logs)) or _group(chrome.pid, str(profile))) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _group(leader.pid, str(logs)) == [], "the dead leader's worker is still running"
        assert _group(chrome.pid, str(profile)) == [], "the dead Chrome's helper is still running"
        assert stranger.poll() is None and not profile.exists()
    finally:
        _kill_marked(leader.pid, str(logs))
        _kill_marked(chrome.pid, str(profile))
        stranger.kill()
        stranger.wait(10)
        for p in (leader, chrome):
            p.stdout.close()


@pytest.mark.parametrize("f", ["src/hermes_finance/static/bills.css", "src/hermes_finance/static/bills.js"])
def test_a_bills_file_runs_the_transaction_sheet_states_that_draw_its_bill_section(f, browser_files):
    """F4: bills.js draws the Bill section inside the transaction sheet (docked beside the table from 1400px, the sheet
    below): a bills-only change runs those states (and their split variant) at every width, theme and DB they're on."""
    sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
    import audit_layout
    p = plan([f], browser_files)
    items = [(k, it) for k in check.GATE_KINDS for it in audit_layout.plan(k, audit_layout.WIDTHS, None)
             if it[2].startswith("tx-sheet") and it[1] in audit_layout.THEMES]
    assert {it[2] for _, it in items} == {"tx-sheet", "tx-sheet-split"} and {it[5] for _, it in items} == {"txpanel", "modal"}
    missed = [(k, it[:4]) for k, it in items if not p.select(k, *it[:4])]
    assert not missed, missed[:5]


def test_the_chrome_budget_is_never_more_than_four():
    """This machine's rule is a ceiling: HF_CHROMES (or the gates' / check.py's own worker settings) above 4 still
    means at most 4 headless Chromes (the slots) and at most 4 browser workers."""
    code = ("import sys; sys.path[:0] = sys.argv[1:]; import check, cdp, run_gates; "
            "print(check.CHROMES, check.BROWSER_WORKERS, cdp.SLOTS, run_gates.CHROMES)")
    env = {**check.safe_env(), "HF_CHROMES": "9", "CHECK_BROWSER_WORKERS": "12", "GATES_CHROMES": "9", "HF_NO_CHROME": "1"}
    out = subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts"), str(ROOT / "tests"), str(ROOT / "src")],
                         env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.split() == ["4", "4", "4", "4"], out.stdout


# --- TEST-SPEED review round 2: B3 (ended only once gone), B4 (a pid signalled only while it is the run's), B5 (scope) --

import io  # noqa: E402
import select  # noqa: E402
import threading  # noqa: E402
import types  # noqa: E402


def _line(proc: subprocess.Popen, secs: float = 20) -> str:
    """A child's next stdout line within `secs` (review B6: a bare readline() is bounded by nothing)."""
    fd, buf, deadline = proc.stdout.fileno(), b"", time.monotonic() + secs
    while not buf.endswith(b"\n"):
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            raise AssertionError(f"no line from the child in {secs} s; so far {buf!r}")
        chunk = os.read(fd, 1)
        if not chunk:
            raise AssertionError(f"the child ended without a line; so far {buf!r}")
        buf += chunk
    return buf.decode().strip()


@pytest.mark.parametrize("state", ["goal-edit", "track-sheet"])
def test_a_planned_state_that_never_appears_on_a_db_that_has_it_fails_full(state, fake_runs, capsys):
    """B5: a state skipped as "never appeared" (a wait that ran out, under load) on a DB that has it (goals on features,
    the tracking sheet everywhere) is not accepted as covered: FAIL. It counted as present, with no FAIL."""
    every = list(check.ALL_DBS)
    fake_runs.behave = staticmethod(lambda run: _gate_outputs(run, every, skipped=[
        ("features", state, f"never appeared: {'#sheet.gsheet .savenote' if state == 'goal-edit' else '#sheet .tsec'}")]))
    rc = check.main(["full", "--part", "2"])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert f"layout features:" in out and "skipped, and it should be on this DB" in out and state in out, out


def test_a_missing_state_with_another_in_its_place_fails_full(fake_runs, capsys):
    """B5: two planned states of nocaps missing from its report and two entries nobody planned there instead: the same
    count, so it passed. Now both sides are named: FAIL."""
    every = list(check.ALL_DBS)
    fake_runs.behave = staticmethod(lambda run: _gate_outputs(run, every, replace=("nocaps", 2)))
    rc = check.main(["full", "--part", "2"])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "layout nocaps: 2 missing" in out and "layout nocaps: 2 unexpected" in out, out


def test_states_absent_where_their_db_lacks_them_are_reported_not_checked(fake_runs, capsys):
    """B5, the control: skips where check.ABSENT says the DB lacks the state (goals off features, nothing owed on
    empty, no estimated bill off features) pass, and are said to be reported absent, apart from the states checked (the
    scope line said every planned state "ran")."""
    every = list(check.ALL_DBS)
    absent = [("full", "goal-edit", "nothing to click: #page .card.goal[data-goal] null"),
              ("empty", "debt-sort-apr", "nothing to click: #page .sorttabs button[data-s=apr] null"),
              ("nocaps", "recurring-bill-est", "never appeared: #page .li[data-est] Error: never appeared")]
    fake_runs.behave = staticmethod(lambda run: _gate_outputs(run, every, skipped=absent))
    rc = check.main(["full", "--part", "2"])
    out = capsys.readouterr().out
    assert rc == 0, out
    import audit_layout
    n = sum(1 for k in every for it in audit_layout.plan(k, audit_layout.WIDTHS, None) if it[1] in audit_layout.THEMES)
    skips = 5 + 5 + 14   # (goal-edit: 5 states on full; debt-sort-apr: 5 on empty; recurring-bill-est: 14 on nocaps)
    assert f"{n} planned layout states: {n - skips} checked, {skips} reported absent" in out, out
    assert any(ln.startswith("FULL") and f"({n - skips} states checked, {skips} reported absent)" in ln
               for ln in out.splitlines()), out
    assert "every planned layout state ran" not in out
    assert "goals are features' only" in out and "nothing owed" in out, out


def test_fast_checks_its_layout_states_exactly_too(tmp_path, monkeypatch, saved_environ, browser_files):
    """B5, fast's gates: the selected states against the report, by key (a count let a state missing with another in
    its place through)."""
    sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests"), str(ROOT / "src")]
    import audit_layout
    import run_gates
    p = plan(["src/hermes_finance/static/bills.css"], browser_files)
    p.numbers = False
    checks = {c: {"pass": True, "examined": 1, "violations": []} for c in audit_layout.CHECKS}

    def fake_run(kinds, which, out, select=None, self_test=None):
        res = []
        for k in kinds:
            rep = {"label": k, "widths": {}, "pass": 0, "fail": 0, "selftest": None}
            for it in audit_layout.plan(k, audit_layout.WIDTHS, None):
                if it[1] in audit_layout.THEMES and select(k, *it[:4]):
                    rep["widths"].setdefault(str(it[0]), {})[it[2] if it[1] == "dark" else f"{it[2]}@{it[1]}"] = {"checks": checks}
            if k == "full":   # one selected state missing, one never selected there instead
                w, states = next(iter(rep["widths"].items()))
                states.pop(next(iter(states)))
                states["settings"] = {"checks": checks}
            res.append({"kind": k, "layout": rep, "lines": []})
        return res
    monkeypatch.setattr(run_gates, "run", fake_run)
    res = check.run_gate_subset(p, tmp_path)
    assert any("layout full: 1 missing" in m for m in res["missing"]) and \
        any("layout full: 1 unexpected" in m for m in res["missing"]), res["missing"]


def _standin_run(logs: Path, name: str, script: str, env: dict):
    """A check.Pytest whose process is `script` (leading its own group, as pytest does), watched as check.py watches one."""
    r = check.Pytest.__new__(check.Pytest)
    r.name, r.log, r.xml = name, logs / f"{name}.log", logs / f"{name}.xml"
    r.t0, r.t1, r.ended, r.stuck = time.monotonic(), None, False, ""
    r.proc = subprocess.Popen([sys.executable, "-c", script], env=env, start_new_session=True, stdout=subprocess.PIPE)
    r.left, r.over, r.unknown = threading.Event(), threading.Event(), ""   # (round 4: wait() waits on `over`)
    check._CHILDREN.append(r)
    check.note("group", r.proc.pid, name)
    threading.Thread(target=r._watch, daemon=True).start()
    return r


LEADER = ("import subprocess, sys\n"
          "w = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
          "print(flush=True); time.sleep(120)'], stdout=subprocess.PIPE)\n"
          "w.stdout.readline()\nprint(w.pid, flush=True)")   # (a worker deaf to SIGTERM, up; then pytest dies by itself)


def test_a_pytest_group_still_there_after_sigkill_is_not_noted_ended(tmp_path, monkeypatch):
    """B3: a pytest run's leader exits and leaves a worker deaf to SIGTERM whose SIGKILL doesn't take in time (as for a
    process stuck in the kernel: withheld here): the run is not noted ended, its wait() fails saying why, and the
    group is left unreaped for the watchdog. Once the SIGKILL takes, ending it again notes it ended, once. (It noted it
    ended after 5 s with the worker still running.)"""
    monkeypatch.setattr(check, "_CHILDREN", [])
    monkeypatch.setattr(check, "LEDGER", tmp_path / "ledger.txt")
    monkeypatch.setattr(check, "KILL_WAIT", 0.5, raising=False)
    monkeypatch.setattr(check, "GRACE", 0.5)
    mark = f"HF_F3_MARK=b3-{os.getpid()}-{time.time_ns()}"
    logs = tmp_path / "logs"
    logs.mkdir()
    r = _standin_run(logs, "stuck", LEADER, check.safe_env(HF_NO_CHROME="1", **dict([mark.split("=", 1)])))
    pg, real_killpg, released = r.proc.pid, os.killpg, []

    def killpg(g, sig):
        if g == pg and sig == signal.SIGKILL and not released:
            return
        return real_killpg(g, sig)
    try:
        worker = int(_line(r.proc))
        assert r.left.wait(10)
        monkeypatch.setattr(os, "killpg", killpg)
        res = r.wait()
        kinds = [ln.split()[0] for ln in (tmp_path / "ledger.txt").read_text().splitlines()]
        assert "ended" not in kinds, f"noted ended while {_group(pg, mark)} of its group still run: {kinds}"
        assert _group(pg, mark) == [worker] and not r.ended and r.proc.returncode is None
        assert res["failed"] >= 1 and "not noted ended" in res.get("why", ""), res
        assert kinds.count("stuck") == 1, kinds
        released.append(1)
        r.end(0.5)
        kinds = [ln.split()[0] for ln in (tmp_path / "ledger.txt").read_text().splitlines()]
        assert kinds.count("ended") == 1 and _group(pg, mark) == [] and r.ended, kinds
    finally:
        monkeypatch.setattr(os, "killpg", real_killpg)
        _kill_marked(pg, mark)
        if r.proc.returncode is None:
            r.proc.wait(10)
        r.proc.stdout.close()


def _watchdog(ledger: Path, monkeypatch):
    """check.py's watchdog in this process (so what it signals can be watched), its pipe already closed; its verdict."""
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"")))
    monkeypatch.setattr(check, "GRACE", 0.5)
    monkeypatch.setattr(check, "KILL_WAIT", 0.5, raising=False)
    return check.watchdog(ledger)


def test_the_watchdog_keeps_the_profile_of_a_chrome_it_could_not_stop(tmp_path, monkeypatch):
    """B3: the run killed outright, a Chrome of it deaf to SIGTERM whose SIGKILL doesn't take in time (withheld): the
    watchdog looks again after SIGKILL, keeps that Chrome's profile, and counts it (left=1) instead of deleting the
    profile and reporting it stopped."""
    logs = tmp_path / "hf-check-7-x"
    logs.mkdir()
    profile = tmp_path / "hf-cdp-stuck"
    profile.mkdir()
    ledger = logs / "ledger.txt"
    chrome = _sleeper(f"--user-data-dir={profile}", "deaf")
    real_kill, withheld = os.kill, []

    def kill(pid, sig):
        if pid == chrome.pid and sig == signal.SIGKILL:
            withheld.append(pid)
            return
        return real_kill(pid, sig)
    try:
        time.sleep(0.3)   # (its SIGTERM ignored by then)
        ledger.write_text(f"start 1.0 {chrome.pid} {chrome.pid} 1 {profile}\n")
        monkeypatch.setattr(os, "kill", kill)
        _watchdog(ledger, monkeypatch)
        monkeypatch.setattr(os, "kill", real_kill)
        assert withheld, "(the watchdog never sent it SIGKILL)"
        assert chrome.poll() is None
        assert profile.exists(), "the watchdog deleted the profile of a Chrome still running"
        last = ledger.read_text().splitlines()[-1]
        assert "left=1" in last, last
    finally:
        monkeypatch.setattr(os, "kill", real_kill)
        chrome.kill()
        chrome.wait(10)


def test_the_watchdog_signals_a_pid_only_while_it_is_still_the_runs(tmp_path, monkeypatch):
    """B4: the watchdog's scan sees a Chrome's group with a helper whose pid, by the time it signals, is another
    process's (a stranger's, in no group of the run's, without its marker): a pid reuse, stood in for by a scan naming
    the stranger's pid as it would have been. Every pid it signals is, at that moment, still in the ledger's group
    and names the run's marker: the stranger is left alone, the Chrome is stopped (it signalled from the scan)."""
    logs = tmp_path / "hf-check-8-x"
    logs.mkdir()
    profile = tmp_path / "hf-cdp-reuse"
    profile.mkdir()
    ledger = logs / "ledger.txt"
    chrome = _sleeper(f"--user-data-dir={profile}")
    stranger = _sleeper("unrelated")
    real_run, real_kill, scans, signalled = subprocess.run, os.kill, [], []

    def run(cmd, *a, **k):
        res = real_run(cmd, *a, **k)
        if cmd[:2] == ["ps", check.PS_ENV] and "-p" not in cmd and not scans:   # the first scan: the helper as it was
            scans.append(1)
            fmt = cmd[cmd.index("-o") + 1]
            row = [str(stranger.pid), str(chrome.pid), "S"] + (["Thu", "Jan", "1", "00:00:00", "2026"] if "lstart" in fmt
                                                               else []) + [f"helper --user-data-dir={profile}"]
            res = subprocess.CompletedProcess(cmd, 0, res.stdout + " ".join(row) + "\n", res.stderr)
        return res

    def kill(pid, sig):   # who it is at the moment it's signalled
        now = real_run(["ps", check.PS_ENV, "-ww", "-o", "pgid=,command=", "-p", str(pid)], capture_output=True, text=True).stdout
        signalled.append((pid, signal.Signals(sig).name, (now.split() or [None])[0], str(profile) in now))
        return real_kill(pid, sig)
    try:
        time.sleep(0.3)
        ledger.write_text(f"start 1.0 {chrome.pid} {chrome.pid} 1 {profile}\n")
        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr(os, "kill", kill)
        _watchdog(ledger, monkeypatch)
        monkeypatch.setattr(os, "kill", real_kill)
        monkeypatch.setattr(subprocess, "run", real_run)
        assert scans, "(the watchdog's scan was never seen)"
        wrong = [s for s in signalled if s[2] != str(chrome.pid) or not s[3]]
        assert not wrong, f"signalled a pid that was, at that moment, not the run's (pid, signal, its group, marker): {wrong}"
        assert stranger.poll() is None
        assert chrome.wait(5) == -signal.SIGTERM and not profile.exists()
    finally:
        monkeypatch.setattr(os, "kill", real_kill)
        monkeypatch.setattr(subprocess, "run", real_run)
        for p in (chrome, stranger):
            if p.poll() is None:
                p.kill()
                p.wait(10)


@pytest.mark.parametrize("mod", ["check", "cdp"])
def test_a_process_state_from_a_layout_not_the_one_assumed_is_not_believed(mod, monkeypatch):
    """B4 (kinfo_proc): a sysctl answer of the right size whose p_pid (offset 40) isn't the pid asked about is a layout
    other than the one assumed: its p_stat byte isn't believed (it called this running process a zombie); ps is asked."""
    import ctypes
    sys.path.insert(0, str(ROOT / "tests"))
    m = check if mod == "check" else __import__("cdp")

    def sysctl(mib, n, buf, size, new, newlen):
        ctypes.memmove(buf, bytes(36) + bytes([m.SZOMB]) + bytes(648 - 37), 648)   # p_stat SZOMB, p_pid 0
        size._obj.value = 648
        return 0
    monkeypatch.setattr(m, "_SYSCTL", sysctl)
    assert m._proc_stat(os.getpid()) != m.SZOMB


# --- TEST-SPEED review round 3 (1301882): B3 a failed look is not "gone", and a failed cleanup fails the run; B4 the
# process-state fallback is bounded and fails closed; B5 the scope's accuracy -------------------------------------------

import contextlib  # noqa: E402


def _marked_pids(mark: str) -> list[int]:
    """The live processes whose environment names HF_TEST_MARK=`mark` (ps -E: this user's only). The real ps, without
    the mark (this process may carry it, and a test's PATH have a stand-in ps first)."""
    out = subprocess.run(["/bin/ps", check.PS_ENV, "-ww", check.PS_ALL, "-o", "pid=,stat=,command="], capture_output=True, text=True, timeout=30,
                         env={k: v for k, v in os.environ.items() if k != "HF_TEST_MARK"}).stdout
    return sorted(int(f[0]) for f in (ln.split(None, 2) for ln in out.splitlines())
                  if len(f) == 3 and not f[1].startswith("Z") and f"HF_TEST_MARK={mark}" in f[2])


@pytest.mark.parametrize("fail", ["nonzero", "timeout", "no-self-row", "after-sigkill"])
def test_the_watchdog_takes_a_failed_process_listing_for_unknown_not_for_gone(fail, tmp_path, monkeypatch):
    """Round 3 B3: the run killed outright, a Chrome of it still running (deaf to SIGTERM), and the watchdog's `ps`
    failing: exit 1 with nothing on stdout, no answer in time, a listing without the watchdog's own process in it (cut
    short), or all of those looks failing once SIGKILL has been sent (withheld here: it goes on). The watchdog says it
    doesn't know (stopped=unknown, left=unknown), keeps the profile, and its verdict is a failure. A failed look read
    as "none running": the profile of a Chrome still running deleted, left=0, a clean exit."""
    logs = tmp_path / "hf-check-9-x"
    logs.mkdir()
    profile = tmp_path / "hf-cdp-unseen"
    profile.mkdir()
    ledger = logs / "ledger.txt"
    chrome = _sleeper(f"--user-data-dir={profile}", "deaf")
    real_run, real_kill, sigkilled = subprocess.run, os.kill, []

    def kill(pid, sig):
        if pid == chrome.pid and sig == signal.SIGKILL:
            sigkilled.append(pid)
            return
        return real_kill(pid, sig)

    def run(cmd, *a, **k):
        if cmd[:2] == ["ps", check.PS_ENV] and (fail != "after-sigkill" or sigkilled):
            if fail == "timeout":
                raise subprocess.TimeoutExpired(cmd, k.get("timeout") or 30)
            if fail in ("nonzero", "after-sigkill"):
                return subprocess.CompletedProcess(cmd, 1, "", "ps: (the test's) failure\n")
            res = real_run(cmd, *a, **k)
            rows = [ln for ln in res.stdout.splitlines() if ln.split()[:1] != [str(os.getpid())] and str(profile) not in ln]
            return subprocess.CompletedProcess(cmd, 0, "\n".join(rows) + "\n", "")
        return real_run(cmd, *a, **k)
    try:
        time.sleep(0.3)   # (its SIGTERM ignored by then)
        ledger.write_text(f"start 1.0 {chrome.pid} {chrome.pid} 1 {profile}\n")
        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr(os, "kill", kill)
        verdict = _watchdog(ledger, monkeypatch)
        monkeypatch.setattr(os, "kill", real_kill)
        monkeypatch.setattr(subprocess, "run", real_run)
        last = ledger.read_text().splitlines()[-1]
        assert chrome.poll() is None and (fail != "after-sigkill" or sigkilled), "(the stand-in Chrome didn't run on)"
        assert profile.exists(), f"the watchdog deleted the profile of a Chrome it couldn't see ({fail}): {last}"
        assert "stopped=unknown" in last and "left=unknown" in last and verdict == 1, (verdict, last)
    finally:
        monkeypatch.setattr(os, "kill", real_kill)
        monkeypatch.setattr(subprocess, "run", real_run)
        chrome.kill()
        chrome.wait(10)


WITHHOLD = """import os, signal
_pid = int(os.environ.get("HF_TEST_WITHHOLD_SIGKILL") or 0)
if _pid:   # (a process stuck in the kernel, stood in for: SIGKILL to it withheld, in this process only)
    _kill = os.kill
    def kill(pid, sig):
        if pid == _pid and sig == signal.SIGKILL:
            return
        return _kill(pid, sig)
    os.kill = kill
"""


@pytest.mark.parametrize("cleanup", ["a survivor", "ps fails", "the watchdog hangs"])
def test_a_passing_run_whose_cleanup_fails_fails_and_keeps_its_record(cleanup, fake_runs, tmp_path, monkeypatch, capsys):
    """Round 3 B3, end to end: check.main() (`full --part 2`, every gate output passing) with its real watchdog (a
    check.py process of its own, as always), and a Chrome the run's ledger says is not stopped: a stand-in, its profile
    on its command line, deaf to SIGTERM. At the run's end the watchdog can't finish the job: the Chrome survives its
    SIGKILL (withheld in the watchdog's process, by a sitecustomize on its PYTHONPATH), or its `ps` fails (exit 1), or
    hangs (so the watchdog doesn't finish in time). The run fails, says why, and keeps its log dir with the ledger (what
    may still run) and the profile; the watchdog's group, a hung `ps` of it included, is gone. The watchdog's exit
    status was dropped: the run passed and deleted its ledger (and a failed ps had the profile deleted); a watchdog
    over its bound was killed alone (its ps went on) after GRACE + 20 s, and the run passed too."""
    every = list(check.ALL_DBS)
    profile = tmp_path / "hf-cdp-e2e0001"
    profile.mkdir()
    mark = f"e2e-{os.getpid()}-{time.time_ns()}"
    chrome = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                               "time.sleep(120)", f"--user-data-dir={profile}"], start_new_session=True,
                              env={**os.environ, "HF_TEST_MARK": mark})

    def behave(run):   # (as one of the gates' Chromes would leave it: its start in the run's ledger, no stop)
        with (run.logs / "ledger.txt").open("a") as f:
            f.write(f"start 1.0 {chrome.pid} {chrome.pid} {os.getpid()} {profile}\n")
        return _gate_outputs(run, every)
    fake_runs.behave = staticmethod(behave)
    os.environ["HF_TEST_MARK"] = mark            # (the watchdog, and what it starts, carry it: found below)
    os.environ["CHECK_WATCHDOG_GRACE"] = "0.5"   # (the watchdog's own GRACE)
    monkeypatch.setattr(check, "GRACE", 0.5)     # (this process's: main()'s wait for the watchdog)
    monkeypatch.setattr(check, "WATCHDOG_WAIT", 12 if cleanup != "the watchdog hangs" else 2, raising=False)
    if cleanup == "a survivor":
        site = tmp_path / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(WITHHOLD)
        os.environ["PYTHONPATH"] = str(site)
        os.environ["HF_TEST_WITHHOLD_SIGKILL"] = str(chrome.pid)
    else:
        fakebin = tmp_path / "bin"
        fakebin.mkdir()
        (fakebin / "ps").write_text("#!/bin/sh\nexit 1\n" if cleanup == "ps fails" else
                                    f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        (fakebin / "ps").chmod(0o755)
        os.environ["PATH"] = f"{fakebin}:{os.environ['PATH']}"
    try:
        time.sleep(0.3)   # (its SIGTERM ignored by then)
        t0 = time.monotonic()
        rc = check.main(["full", "--part", "2"])
        took = time.monotonic() - t0
        out = capsys.readouterr().out
        logs = [d for d in tmp_path.glob("hf-check-*") if d.is_dir()]
        left = [p for p in _marked_pids(mark) if p != chrome.pid]
        last = (logs[0] / "ledger.txt").read_text().splitlines()[-1] if logs and (logs[0] / "ledger.txt").exists() else None
        facts = (f"exit {rc} after {took:.1f} s ({cleanup}); log dir and ledger kept: {last is not None}; profile kept: "
                 f"{profile.exists()}; the watchdog's line: {last}; left of its group: {left}")
        assert rc == 1 and "the run's cleanup failed" in out, facts
        assert len(logs) == 1 and last is not None, facts
        assert profile.exists() and chrome.poll() is None, facts
        if cleanup != "the watchdog hangs":
            assert last.startswith("watchdog ") and f"left={'1' if cleanup == 'a survivor' else 'unknown'}" in last, facts
        assert not left, f"processes of the run's watchdog left running: {facts}"
        assert took < (15 if cleanup == "the watchdog hangs" else 30), took
    finally:
        chrome.kill()
        chrome.wait(10)
        for pid in _marked_pids(mark):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)


PROC_STAT = r"""
import json, subprocess, sys, time
sys.path[:0] = sys.argv[1:4]
mod = __import__(sys.argv[4])
mod._SYSCTL = lambda *a: -1   # (sysctl's answer not had: as one not believed, ps is asked)
mod.PS_WAIT = 1
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
t0 = time.monotonic()
said = mod._exited(p)
print(json.dumps({"exited": said, "secs": time.monotonic() - t0}), flush=True)
p.kill()
p.wait()
"""


@pytest.mark.parametrize("mod", ["check", "cdp"])
@pytest.mark.parametrize("ps", ["fails", "hangs"])
def test_a_process_state_that_cant_be_told_is_not_taken_for_an_exit(mod, ps, tmp_path):
    """Round 3 B4: with sysctl's answer not had, _exited() of a running child asks ps; that ps fails (exit 1, nothing
    on stdout) or doesn't answer. It says "not exited" (not known to have), within its bound. A failed ps read as "no
    such process" (a running child taken for exited: a pytest still running had its group stopped as if done), and one
    that didn't answer was waited for for ever. In a child process (its PATH's ps is this test's), killed at the bound
    with its group."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "ps").write_text("#!/bin/sh\nexit 1\n" if ps == "fails" else f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    (fakebin / "ps").chmod(0o755)
    child = subprocess.Popen([sys.executable, "-c", PROC_STAT, str(ROOT / "scripts"), str(ROOT / "tests"), str(ROOT / "src"),
                              mod], env={**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}"}, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = child.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, signal.SIGKILL)   # (unreaped: its group's id is its own)
        child.communicate()
        pytest.fail(f"{mod}._exited() still waiting for its ps after 10 s (no bound on it)")
    res = json.loads(out or "{}")
    assert res.get("exited") is False and res["secs"] < 5, \
        f"{mod}._exited() of a running child, its ps {'failing' if ps == 'fails' else 'hung'}: {res} {err[-500:]}"


def test_the_scope_line_counts_the_planned_states_of_a_db_with_no_report(fake_runs, capsys):
    """Round 3 B5 (accuracy): a DB with no layout report at all fails the run (as before), and the scope line still
    counts that DB's planned states: it counted only the DBs that had a report (fewer planned than were asked for)."""
    sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
    import audit_layout
    every = list(check.ALL_DBS)
    fake_runs.behave = staticmethod(lambda run: _gate_outputs(run, [k for k in every if k != "onepoint"]))
    rc = check.main(["full", "--part", "2"])
    out = capsys.readouterr().out
    n = sum(1 for k in every for it in audit_layout.plan(k, audit_layout.WIDTHS, None) if it[1] in audit_layout.THEMES)
    said = [ln.strip() for ln in out.splitlines() if ln.lstrip().startswith(("scope", "FULL"))]
    assert rc == 1 and "layout onepoint: no report" in out, (rc, said)
    assert f"{n} planned layout states:" in out, f"the gates were asked for {n} states; the run said: {said}"


def test_a_report_for_a_db_the_gates_werent_asked_for_fails(fake_runs, capsys):
    """Round 3 B5 (accuracy): a split's layout report with a DB it wasn't asked for (nocaps, beside onepoint and signin)
    fails: the gates run exactly the DBs asked for. It was ignored."""
    fake_runs.behave = staticmethod(lambda run: _gate_outputs(run, ["onepoint", "signin", "nocaps"]))
    rc = check.main(["full", "--part", "2", "--kinds", "onepoint,signin"])
    out = capsys.readouterr().out
    said = [ln.strip() for ln in out.splitlines() if ln.lstrip().startswith(("scope", "FULL"))]
    assert rc == 1 and "layout nocaps: a report, for a DB the gates weren't asked for" in out, f"exit {rc}: {said}"


def test_an_entry_with_every_checks_key_but_no_result_is_not_checked(fake_runs, capsys):
    """Round 3 B5 (accuracy): an entry naming every check with no result in it (null) is not a checked state: FAIL. The
    keys alone counted it checked."""
    every = list(check.ALL_DBS)
    fake_runs.behave = staticmethod(lambda run: _gate_outputs(run, every, hollow=("full", 2)))
    rc = check.main(["full", "--part", "2"])
    out = capsys.readouterr().out
    said = [ln.strip() for ln in out.splitlines() if ln.lstrip().startswith(("scope", "FULL"))]
    assert rc == 1 and "layout full: 2 without every check's result" in out, f"exit {rc}: {said}"



# --- TEST-SPEED review round 4 (7bf4d35): a pytest run whose state can't be told is waited for within a bound ---------

UNKNOWN_RUN = r"""
import json, os, sys, time
from pathlib import Path
sys.path[:0] = sys.argv[1:4]
import check
tmp, case = Path(sys.argv[4]), sys.argv[5]
check.LEDGER, check._CHILDREN = tmp / "ledger.txt", []
env = check.safe_env(HF_NO_CHROME="1")   # (pytest's own: the real ps on its PATH)
if case != "known":   # from now on, sysctl's answer not had (as one not believed) and every ps of this process failing
    check._SYSCTL = lambda *a: -1
    os.environ["PATH"] = f"{tmp / 'bin'}:{os.environ['PATH']}"
    if hasattr(os, "waitid"):   # (Linux: no waitid either, as on macOS, so the state really can't be told)
        del os.waitid
check.UNKNOWN_WAIT = 2
logs = tmp / "logs"
logs.mkdir()
r = check.Pytest(case.replace(" ", "-"), [str(tmp / ("test_sleepy.py" if case == "still running" else "test_one.py"))],
                 logs, env)
print(json.dumps({"pid": r.proc.pid}), flush=True)
def facts(t0):
    return {"secs": round(time.monotonic() - t0, 2), "reaped": r.proc.returncode is not None, "ended": r.ended,
            "ledger": [ln.split()[0] for ln in check.LEDGER.read_text().splitlines()]}
t0 = time.monotonic()
said = {"res": r.wait()}
said.update(facts(t0))
if case == "still running":   # the run's stop, on its way out, still ends it: by its group, its leader unreaped
    t0 = time.monotonic()
    check._stop_children()
    said["stop"] = facts(t0)
print(json.dumps(said), flush=True)
"""


@pytest.mark.parametrize("case", ["known", "exited", "still running"])
def test_a_pytest_run_whose_state_cant_be_told_is_waited_for_within_a_bound(case, tmp_path):
    """Round 4: check.Pytest's wait() for a real pytest run (one passing test, or one that sleeps) while its state can't
    be told for as long as the run lasts (sysctl's answer not had, every ps failing: _proc_stat UNKNOWN; no waitid, as
    on macOS: on Linux the child removes it). It waited for ever: a pytest that had exited was never seen to, and the run never got to its end. Now
    (exited) no process of its group running, its leader's zombie aside, is its exit: its result is its own (1 test,
    exit 0), its leader reaped and noted ended; (still running) its group still there, the wait ends UNKNOWN_WAIT s on
    (2 here) and fails, saying so, with its leader unreaped and nothing noted ended; the run's stop then ends it, by
    its group. (known) the control: with its state told, the same run as ever. Each in a child process, killed at the
    bound (its pytest, a session of its own, found by its mark)."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "ps").write_text("#!/bin/sh\nexit 1\n")
    (tmp_path / "bin" / "ps").chmod(0o755)
    (tmp_path / "test_one.py").write_text("def test_one():\n    assert True\n")
    (tmp_path / "test_sleepy.py").write_text("import time\n\n\ndef test_sleepy():\n    time.sleep(60)\n")
    mark = f"unknown-{os.getpid()}-{time.time_ns()}"
    child = subprocess.Popen([sys.executable, "-c", UNKNOWN_RUN, str(ROOT / "scripts"), str(ROOT / "tests"),
                              str(ROOT / "src"), str(tmp_path), case], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True, env={**os.environ, "HF_TEST_MARK": mark})
    try:
        try:
            out, err = child.communicate(timeout=40)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(child.pid, signal.SIGKILL)   # (unreaped: its group's id is its own)
            out, err = child.communicate()
            pytest.fail(f"({case}) Pytest.wait() still waiting 40 s on: {out!r} {err[-400:]}")
        lines = out.strip().splitlines()
        said = json.loads(lines[-1]) if len(lines) == 2 else {}
        res = said.get("res", {})
        assert child.returncode == 0 and said, (child.returncode, out, err[-800:])
        if case == "still running":
            assert res["failed"] >= 1 and "couldn't be told" in res.get("why", ""), said
            assert said["secs"] < 10 and not said["reaped"] and not said["ended"] and "ended" not in said["ledger"], said
            stop = said["stop"]
            assert stop["reaped"] and stop["ended"] and stop["ledger"].count("ended") == 1 and stop["secs"] < 30, said
        else:
            assert res["rc"] == 0 and res["tests"] == 1 and res["failed"] == 0 and said["secs"] < 20, said
            assert said["reaped"] and said["ended"] and said["ledger"] == ["group", "ended"], said
    finally:
        for pid in _marked_pids(mark):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        if child.poll() is None:
            child.kill()
            child.wait(10)


def test_the_controls_gate_dbs_are_the_ones_check_asks_for():
    """check.py's CONTROL_KINDS (what gate_scope requires of tests/test_gates.py) are scripts/audit_controls.py's KINDS
    (what test_gate_3_controls runs on)."""
    sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
    import audit_controls
    assert check.CONTROL_KINDS == audit_controls.KINDS
    assert set(check.CONTROL_KINDS) <= set(check.GATE_KINDS)

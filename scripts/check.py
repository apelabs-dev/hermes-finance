"""The check loop (TEST-SPEED): one command to check a change, fast while iterating, everything before shipping.

    uv run python scripts/check.py            # = full (deliberate change from the former bare = fast default)
    uv run python scripts/check.py --tier ship # exactly full: part 1 + part 2, unchanged acceptance
    uv run python scripts/check.py --tier fast --base main --dry-run # file selection, with reasons
    uv run python scripts/check.py full       # everything: the whole pytest suite, the gates included
    uv run python scripts/check.py fast --base main --dry-run    # print the plan, run nothing
    uv run python scripts/check.py fast --files src/hermes_finance/static/settings.css             # as if that changed
    uv run python scripts/check.py ci         # what CI runs: every test that needs no browser, plus the release scan

--tier fast: git diff --name-only <base>..HEAD (CHECK_BASE or main by default), or --files for a selection override.
scripts/tier_map.py owns the one source-to-test table, augmented by tests reading/importing changed files (including
through helpers). Hygiene/guard tests, audit selector/state wiring tests and test_text_density.py are always-on.
Static CSS/JS selects every browser/layout test file and test_money_numbers.py. An unmapped path or any change to
scripts/, conftest.py, cdp.py, gate state/fixture lists or the mapping falls back to the existing full run. Selected
files run one at a time, stopping on a failure. Every selected file and its reason is printed. Even on fallback or
failure the final line is `TIER fast (NOT a ship verdict)`; only full / --tier ship is acceptance. --part/--kinds remain
available with full / --tier ship, never with --tier fast. Positional modes and --tier are mutually exclusive.

Positional fast (legacy, retained): every test that needs no browser (all ~1700 of them: 15 s on 8 workers), the browser tests a change can affect,
and the gates narrowed to it: the layout gate's states of the pages it touches at every width in both themes on every
synthetic DB (one self-test for the run), the numbers gate when code that makes numbers changed (any .py or .js). The
map is RULES below. What it has no narrower rule for runs broadly: a shared or new front-end file, web.py, the gates'
machinery, the synthetic data or the dependencies run every layout state on every DB; other app code runs the numbers
gate and every layout state on the two richest DBs and the sign-in ones; a helper module of tests/ or scripts/ runs the
tests that import it (everything, when the gates import it). It is still a narrowed run, and its last line says what it
left out: `full` is what a task needs before it's done.
ci: what CI runs on every push (.github/workflows/ci.yml), the same no-browser file list as fast and full
(test_files()[0], under HF_NO_CHROME=1), plus the release scan of every tracked file that ships (scripts/dehardcode.py
--release-tracked, with the private words of HERMES_FINANCE_TEST_PRIVATE / HERMES_FINANCE_PRIVATE_FILE). No browser,
no gates: the nightly workflow runs `full`.
full: the whole pytest suite, tests/test_gates.py included (the numbers and layout gates on every DB, the controls gate
on full and nocaps). It runs in two halves, one after the
other: 1) every test but the gates (the browser tests take the Chromes, the others the CPU), 2) tests/test_gates.py with
every Chrome. `--part 1` / `--part 2` runs one half, `--part 2 --kinds full,nocaps,empty` the gates on some of the DBs
(so each call fits a 10-minute tool call).

Always in the safe environment (no provider keys, nothing named like a credential, HERMES_PLAID=off) and within the
headless Chrome budget (HF_CHROMES: 4 by default, and at most; held machine-wide by tests/cdp.py). Runs in the foreground; everything it
starts (pytest workers, gate servers, Chromes) is stopped when it ends, however it ends: by its own `finally` on a normal
end, an error, Ctrl-C or SIGTERM / SIGHUP, and when it is killed outright (SIGKILL) by its watchdog, a process of its own
session that stops whatever the run's ledger (every pytest run and Chrome it started, and whether each has stopped) says
is still running. Prints one line at the end:

    FAST: 1745 tests, 1240 layout checks, 402 numbers checks, 0 FAIL in 1m52s; narrowed: layout 689 of 4206 states, ...

and exits 1 on any failure: a failed test or check, a phase that ran no tests, gates that didn't run every DB and
exactly the layout states they were asked for (a state audit_layout skipped counts only where ABSENT says its DB lacks
it, and is reported absent, not checked), a pytest run or Chrome whose processes outlived SIGKILL, or (TEST-SPEED
review round 3) a cleanup its watchdog couldn't finish (a process of the run still running, a `ps` that failed, the
watchdog itself out of time). Logs: $TMPDIR/hf-check-<pid>-*/ (kept when something failed, or with --keep-logs).
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import fcntl
import hashlib
from contextlib import contextmanager, nullcontext
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
# Never passed on to anything check.py runs: the providers' variables and anything named like a credential. The tests use
# synthetic data and fake keys; a real key in the shell must not reach a test's child process (conftest.py's patches
# don't cover those). Only the names are looked at.
SECRET_ENV = re.compile(r"^(PLAID|APCA|ALPACA|MARKETCHECK|RENTCAST|CARFAX|REDFIN|OPENROUTER|OPENAI|ANTHROPIC)_|"
                        r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL", re.I)
# Nor what narrows a run from outside (TEST-SPEED review F1): a shell's GATE_KINDS (the gates' DBs: check.py says which
# itself, every one unless `--kinds`) or PYTEST_ADDOPTS (a -k, a -m): a full run said "every DB" and ran the shell's choice
INHERITED_SCOPE = ("GATE_KINDS", "PYTEST_ADDOPTS")
MAX_CHROMES = 4   # this Mac mini's rule: a ceiling, whatever HF_CHROMES says (tests/cdp.py's slots have it too)
CHROMES = max(1, min(MAX_CHROMES, int(os.environ.get("HF_CHROMES") or MAX_CHROMES)))
CPU_WORKERS = int(os.environ.get("CHECK_CPU_WORKERS") or min(8, max(2, (os.cpu_count() or 4) - 2)))
# One Chrome slot always stays free of the browser workers' own: each worker can hold a Chrome for a whole file (a
# module fixture) while a test in it runs a layout gate (audit_layout.audit, workers=2) whose pool threads need slots
# of their own. With a worker per slot, two such files at once held every slot and their pools waited forever (cdp's
# deadlock check can't see it: a pool thread holds no slot itself). Nightly 40362432360 hung part 1 for 2h30 so.
BROWSER_WORKERS = max(1, min(max(1, CHROMES - 1), int(os.environ.get("CHECK_BROWSER_WORKERS") or CHROMES)))
GATE_KINDS = ("full", "nocaps", "empty", "history-empty", "onepoint", "features")
AUTH_KINDS = ("signin", "signin-setup")
ALL_DBS = GATE_KINDS + AUTH_KINDS
GRACE = float(os.environ.get("CHECK_WATCHDOG_GRACE") or 10)   # s: the watchdog gives what it stops this long after SIGTERM, then SIGKILL
KILL_WAIT = 5   # s: (TEST-SPEED review B3) after SIGKILL, how long to wait to see it gone; still there: not recorded ended
WATCHDOG_WAIT = 20   # s: (review round 3) past GRACE, how long a run's end waits for its watchdog's verdict; then it's killed
# `ps` showing each process's environment after its command line (the environments are this user's processes' only), and
# every process: macOS -E / -ax, Linux procps the BSD-style e / -e (procps has no -E, and takes no e with -ax).
PS_ENV = "-E" if sys.platform == "darwin" else "e"
PS_ALL = "-ax" if sys.platform == "darwin" else "-e"
PS_WAIT = 30    # s: (review round 3) any `ps` here; one that hasn't answered by then has failed
UNKNOWN_WAIT = 2 * PS_WAIT   # s: (review round 4) a pytest run whose state can't be told this long, its group still there: its wait fails

# A test file needs a browser when it starts a headless Chrome, runs a gate, or imports a module that does.
BROWSER_RE = re.compile(r"Chrome\(|audit_layout\.(audit|selftest)|audit_numbers\.audit|audit_controls\.(audit|selftest|probe)|"
                        r"run_gates\.run|an\.audit\(")
# The gates' own modules: a module they import (a test helper, the synthetic data) changes what every gate checks
GATE_MODULES = {"run_gates", "audit_layout", "audit_numbers", "audit_controls", "gate_dbs", "cdp"}
CONTROL_KINDS = ("full", "nocaps")   # the controls gate's DBs (scripts/audit_controls.py KINDS; tests/test_check_plan.py)
_IMPORT = re.compile(r"^[ \t]*(?:from[ \t]+(\w+)[ \t]+import\b|import[ \t]+(\w[\w \t,]*))", re.M)


def safe_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k) and k not in INHERITED_SCOPE}
    env.update(HERMES_PLAID="off", HERMES_ALPACA="off", HERMES_CHROME_DEVTOOLS="off", HF_CHROMES=str(CHROMES),
               PYTHONDONTWRITEBYTECODE="1", **extra)
    return env


def _imports(src: str) -> set[str]:
    """The top-level modules a Python source imports (by name: `import a, b as c`, `from a import x`)."""
    out: set[str] = set()
    for a, b in _IMPORT.findall(src):
        out.update([a] if a else [m.split()[0] for m in b.split(",") if m.strip()])
    return out


def modules() -> dict[str, str]:
    """{module name: source} of tests/ and scripts/ (what their files import one another by)."""
    return {f.stem: f.read_text() for d in (TESTS, ROOT / "scripts") for f in sorted(d.glob("*.py"))}


def importers(name: str, mods: dict[str, str]) -> set[str]:
    """The modules of tests/ and scripts/ that import `name`, directly or through one another."""
    found: set[str] = set()
    todo = [name]
    while todo:
        n = todo.pop()
        for k, s in mods.items():
            if k not in found and n in _imports(s):
                found.add(k)
                todo.append(k)
    return found


def test_files() -> tuple[list[str], list[str]]:
    """(no-browser files, browser files), tests/test_gates.py in neither. A browser file is one that starts a Chrome or
    runs a gate, or imports a module of tests/ that is one (another test's fixtures, a helper; not cdp.py itself, the
    driver). A browser file missed here fails loudly in the no-browser run (tests/cdp.py refuses Chrome under HF_NO_CHROME)."""
    mods = {k: s for k, s in modules().items() if (TESTS / f"{k}.py").exists() and k not in ("cdp", "conftest")}
    browser = {k for k, s in mods.items() if BROWSER_RE.search(s)}
    grown = True
    while grown:   # test_task22g takes test_task22f's Chrome; a helper that starts one makes its importers browser files
        grown = False
        for k, s in mods.items():
            if k not in browser and _imports(s) & browser:
                browser.add(k)
                grown = True
    tests = sorted(k for k in mods if k.startswith("test_") and k != "test_gates")
    return [f"tests/{k}.py" for k in tests if k not in browser], [f"tests/{k}.py" for k in tests if k in browser]


# --- fast: what a changed file can affect ------------------------------------------------------------------------------

def _page(*pages):
    return lambda label, width, theme, name, page: page.split("?")[0] in pages


def _names(*prefixes):
    return lambda label, width, theme, name, page: name.startswith(prefixes)


def _any(*sels):
    return lambda *a: any(s(*a) for s in sels)


EVERYTHING = lambda *a: True                                                   # noqa: E731
LIGHT = lambda label, width, theme, name, page: theme == "light"             # noqa: E731
NARROW = lambda label, width, theme, name, page: width < 1024                # noqa: E731
AUTH = lambda label, width, theme, name, page: label in AUTH_KINDS           # noqa: E731
RICH = ("full", "features")   # the DBs with every page state on them

# App code no rule names (a .py under src/hermes_finance): the numbers it makes (the numbers gate, every page and DB),
# the pages drawn from it (every state on the two richest DBs and the sign-in ones), the browser tests that name it
APP_CODE = (EVERYTHING, RICH + AUTH_KINDS)

# (glob, layout states (selector), DBs for them, browser test files (regex over their source), numbers gate?)
# First match wins. A page's own files: its states on every DB. What every page shares, or a file no rule names (a new
# page's: chat.*, a rules manager's), and what the gates are made of: every state on every DB. App code: APP_CODE;
# helper modules of tests/ and scripts/: the tests importing them (see plan_fast).
RULES: list[tuple[str, object, tuple, str | None, bool]] = [
    ("src/hermes_finance/static/settings.css", _any(_page("settings", "setup"), _names("settings", "login-setup"), AUTH),
     ALL_DBS, r"#/settings|#/setup|['\"]settings['\"]|build\(['\"]signin", False),
    ("src/hermes_finance/static/settings.js", _any(_page("settings"), _names("settings")), GATE_KINDS,
     r"#/settings|['\"]settings['\"]", True),
    ("src/hermes_finance/static/setup.js", _page("setup"), GATE_KINDS, r"#/setup|setup\?step", True),
    ("src/hermes_finance/static/login.js", _any(AUTH, _names("settings-login-setup")), ALL_DBS, r"signin|login", True),
    ("src/hermes_finance/static/import.js", _names("import-"), GATE_KINDS, r"import", True),
    ("src/hermes_finance/static/billcal.*", _page("recurring"), GATE_KINDS, r"calnav|bchip|calcard|bills-calendar", True),
    # (and the transaction sheet's states, docked and not, split or not: bills.js draws its Bill section there)
    ("src/hermes_finance/static/bills.*", _any(_page("recurring"), _names("track-sheet", "bill-", "tx-sheet")), GATE_KINDS,
     r"billsheet|trackf|bt-usual|bprev|data-est|/api/bills", True),
    ("src/hermes_finance/static/goals.*", _any(_page("goals", "overview"), _names("goal")), GATE_KINDS,
     r"#/goals|['\"]goals['\"]|gsheet|gform", True),
    ("src/hermes_finance/static/txmeta.*", _any(_page("transactions")), GATE_KINDS,
     r"txpanel|txsheet|\.spl\b|saved-filters|sfbar", True),
    ("src/hermes_finance/static/rollover.*", _page("budgets"), GATE_KINDS, r"#/budgets|['\"]budgets['\"]|rollover", True),
    ("src/hermes_finance/static/alpaca.*", _any(_page("investments"), _names("add-alpaca", "alpaca-", "settings-alpaca")),
     GATE_KINDS, r"alpaca", True),
    # the whole light theme, every phone width: those states of every page, on every DB (the sign-in page's too)
    ("src/hermes_finance/static/light.css", LIGHT, ALL_DBS, r"HFTheme\.set\(['\"]light|theme.*light", False),
    ("src/hermes_finance/static/tap.css", NARROW, ALL_DBS, r"\btap\b|44 x 44|hit box", False),
    # shared by every page, or no rule for it: everything
    ("src/hermes_finance/static/*", EVERYTHING, ALL_DBS, r".", True),
    ("src/hermes_finance/web.py", EVERYTHING, ALL_DBS, r".", True),
    # the gates' own machinery, the synthetic data, the dependencies: everything
    ("tests/cdp.py", EVERYTHING, ALL_DBS, r".", True),
    ("scripts/audit_layout.py", EVERYTHING, ALL_DBS, r"audit_layout", False),
    ("scripts/run_gates.py", EVERYTHING, ALL_DBS, r"run_gates|gate_dbs", True),
    ("tests/gate_dbs.py", EVERYTHING, ALL_DBS, r".", True),
    ("tests/demo_data.py", EVERYTHING, ALL_DBS, r".", True),
    ("tests/pngtools.py", EVERYTHING, ALL_DBS, r".", True),
    ("pyproject.toml", EVERYTHING, ALL_DBS, r".", True),
    ("uv.lock", EVERYTHING, ALL_DBS, r".", True),
    ("scripts/audit_numbers.py", None, (), r"audit_numbers", True),
    ("tests/conftest.py", None, (), r".", False),
    ("src/*", EVERYTHING, ALL_DBS, r".", True),   # (but a .py under src/hermes_finance: APP_CODE)
]


def changed_files(base: str) -> list[str]:
    """Every path the branch changed since it left `base`, the working tree's changes and untracked files included; a
    deleted or renamed-away path too (--no-renames: a rename is its old path deleted and its new one added)."""
    def git(*a):
        return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True, check=True).stdout.split("\n")
    mb = git("merge-base", "HEAD", base)[0]
    files = set(git("diff", "--name-only", "--no-renames", mb)) | set(git("ls-files", "--others", "--exclude-standard"))
    return sorted(f for f in files if f)


class Plan:
    def __init__(self):
        self.browser: set[str] = set()
        self.selectors: list[tuple[object, tuple]] = []   # (selector, DBs)
        self.numbers = False
        self.why: list[str] = []

    def kinds(self) -> list[str]:
        return [k for k in ALL_DBS if any(k in dbs for _, dbs in self.selectors)]

    def select(self, label, width, theme, name, page) -> bool:
        return any(label in dbs and sel(label, width, theme, name, page) for sel, dbs in self.selectors)


def plan_fast(files: list[str], browser_files: list[str]) -> Plan:
    p = Plan()
    sources = {f: (ROOT / f).read_text() for f in browser_files}
    mods = modules()
    for f in files:
        path = Path(f)
        if f.startswith("tests/test_") and f.endswith(".py"):   # itself, and the test files that import it
            imp = [b for b in sources if Path(b).stem in importers(path.stem, mods) | {path.stem}]
            p.browser.update(imp)
            if f == "tests/test_gates.py":
                p.selectors.append((EVERYTHING, ALL_DBS))
                p.numbers = True
            p.why.append(f"{f}: itself{f' (and {len(imp) - (f in sources)} browser file(s) importing it)' if len(imp) > (f in sources) else ''}")
            continue
        rule = next((r for r in RULES if fnmatch.fnmatch(f, r[0])), None)
        if rule and rule[0] == "src/*" and f.startswith("src/hermes_finance/") and f.endswith(".py"):   # APP_CODE
            mod = path.stem
            hits = [b for b, s in sources.items() if re.search(rf"\b{mod}\b", s)]
            p.browser.update(hits)
            p.selectors.append(APP_CODE)
            p.numbers = True
            p.why.append(f"{f}: app code -> numbers gate, every layout state on {','.join(APP_CODE[1])}, "
                         f"browser tests naming {mod} ({len(hits)})")
            continue
        if rule is None and f.endswith(".py") and len(path.parts) == 2 and path.parts[0] in ("tests", "scripts"):
            # a helper module: the test files that import it (directly or through other helpers); everything when a gate does
            imp = importers(path.stem, mods)
            hits = [b for b in sources if Path(b).stem in imp]
            p.browser.update(hits)
            gates = sorted(imp & GATE_MODULES)
            if gates:
                p.selectors.append((EVERYTHING, ALL_DBS))
                p.numbers = True
            p.why.append(f"{f}: imported by {len(imp)} module(s): {len(hits)} browser test file(s)"
                         f"{'; the gates (' + ','.join(gates) + ') -> every layout state on every DB, numbers gate' if gates else ''}")
            continue
        if rule is None and f.startswith("tests/"):   # a fixture, test data: the browser tests that name it
            hits = [b for b, s in sources.items() if path.name in s or (len(path.stem) > 3 and path.stem in s)]
            p.browser.update(hits)
            p.why.append(f"{f}: named by {len(hits)} browser test file(s) (the no-browser tests run anyway)")
            continue
        if rule is None:
            p.why.append(f"{f}: no browser test or gate state depends on it (the no-browser tests run anyway)")
            continue
        glob, sel, dbs, brx, numbers = rule
        hits = [b for b, s in sources.items() if brx and re.search(brx, s)]
        p.browser.update(hits)
        if sel is not None:
            p.selectors.append((sel, dbs))
        p.numbers |= numbers
        p.why.append(f"{f}: rule {glob} -> layout {'-' if sel is None else ','.join(dbs)}; "
                     f"{len(hits)} browser test file(s){'; numbers gate' if numbers else ''}")
    return p


def _gate_path() -> None:
    for d in (str(ROOT / "scripts"), str(TESTS), str(ROOT / "src")):
        if d not in sys.path:
            sys.path.insert(0, d)


def planned(kinds, select=None) -> dict[str, set[tuple[str, str]]]:
    """{DB: the layout states the gate plans for it (those `select` picks)}, each as its report has it: (width,
    name, or name@theme when not dark). TEST-SPEED review B5: the states themselves, not how many."""
    _gate_path()
    import audit_layout  # noqa: E402
    return {k: {(str(it[0]), it[2] if it[1] == "dark" else f"{it[2]}@{it[1]}")
                for it in audit_layout.plan(k, audit_layout.WIDTHS, None) if it[1] in audit_layout.THEMES
                and (select is None or select(k, it[0], it[1], it[2], it[3]))} for k in kinds}


def layout_scope(p: Plan) -> tuple[int, int]:
    """(states the plan selects, states the whole layout gate plans), over every DB."""
    return (sum(map(len, planned(ALL_DBS, p.select).values())), sum(map(len, planned(ALL_DBS).values())))


# TEST-SPEED review B5: the planned states a DB may not have, and why: audit_layout skips a state whose first click
# finds nothing, or whose content never appears ("not on this DB"). That is right on a DB without what it opens, and a
# lost state anywhere else (a wait that ran out under load looks just the same), so a skip is accepted only here: on
# these DBs, with this in its reason. {state: (DBs, what its skip says, why the DB lacks it)}. Seen identically in four
# full runs of the original TEST-SPEED lane (Q03/Q04, Q06/Q07, R24/R25, R27/R28: 530 of the 4206 states); `features`
# (every state) and the sign-in DBs skip none. A state skipped anywhere else is a FAIL; to add one, say why here.
_BARE = ("full", "nocaps", "empty", "history-empty", "onepoint")   # all but features
_NO_EXTRAS = ("nocaps", "empty", "history-empty", "onepoint")       # without full's extras and TASK-13b's valuations
ABSENT: dict[str, tuple[tuple[str, ...], str, str]] = {
    "alpaca-remove": (_BARE, "nothing to click: #page .apirow .apictl .lnk Remove", "the Alpaca account is features' only"),
    "accounts-alpaca-details": (_BARE, "nothing to click: #page .li.acct.r2.apirow[data-api=alpaca]",
                              "the Alpaca account is features' only (density D3 B: its row opens the sheet)"),
    "accounts-alpaca-replace": (_BARE, "nothing to click: #page .li.acct.r2.apirow[data-api=alpaca]",
                              "the Alpaca account is features' only (density D3 B: its row opens the sheet)"),
    "accounts-alpaca-remove": (_BARE, "nothing to click: #page .li.acct.r2.apirow[data-api=alpaca]",
                              "the Alpaca account is features' only (density D3 B: its row opens the sheet)"),
    "archive-confirm": (_NO_EXTRAS, "nothing to click: #page [data-edit]", "no manual account to edit"),
    "toast": (_NO_EXTRAS, "nothing to click: #page [data-edit]", "no manual account to edit (its save's toast)"),
    "edit-manual": (_NO_EXTRAS, "nothing to click: #page [data-edit]", "no manual account"),
    "asset-car-photo": (("nocaps", "empty"), "nothing to click: #page a[data-asset][data-kind=vehicle] 2019 Zenith Mesa",
                        "no Mesa (nocaps' car is a 2003 Accord; empty has none)"),
    "asset-car-silhouette": (_NO_EXTRAS, "nothing to click: #page a[data-asset][data-kind=vehicle] 2015 Honda Fit",
                             "no Honda Fit (TASK-13b's valuations: full and features)"),
    "edit-vehicle-your-value": (_NO_EXTRAS, "nothing to click: #page a[data-asset][data-kind=vehicle] 2015 Honda Fit",
                                "no Honda Fit (TASK-13b's valuations: full and features)"),
    "edit-vehicle": (("empty",), "nothing to click: #page a[data-asset][data-kind=vehicle]", "no vehicle"),
    "asset-property-photo": (_NO_EXTRAS, "nothing to click: #page a[data-asset][data-kind=property] Condo",
                             "no property (the valuations: full and features)"),
    "asset-property-map": (_NO_EXTRAS, "nothing to click: #page a[data-asset][data-kind=property] Cabin",
                           "no property (the valuations: full and features)"),
    "asset-property-no-estimate": (_NO_EXTRAS, "nothing to click: #page a[data-asset][data-kind=property] Studio",
                                   "no property (the valuations: full and features)"),
    "edit-property": (_NO_EXTRAS, "nothing to click: #page a[data-asset][data-kind=property]", "no property"),
    "bill-sheet": (_BARE, "nothing to click: #page .li[data-est]", "no bill with an estimate (TASK-21's bills: features)"),
    "recurring-bill-est": (_BARE, "never appeared: #page .li[data-est]",
                           "no bill with an estimate (TASK-21's bills: features)"),
    "recurring-every-2-months": (_BARE, "never appeared: an every-2-months bill on the calendar",
                                 "no every-2-months bill (LADWP: features)"),
    "goal-edit": (_BARE, "nothing to click: #page .card.goal[data-goal]", "goals are features' only"),
    "goal-edit-entries": (_BARE, "nothing to click: #page .card.goal[data-goal] Trip", "goals are features' only"),
    "budgets-editor": (("empty",), "nothing to click: #page tr.c .plan, #page .nb .nbrow[data-category] .setc", "no budget suggestion to edit"),
    "budgets-editor-error": (("empty",), "nothing to click: #page tr.c .plan, #page .nb .nbrow[data-category] .setc", "no budget suggestion to edit"),
    # 2026-10-02: the bulk "See suggestions" link is gone; the state now opens one no-budget row's own suggestion
    # disclosure, which exists in every fixture that HAS uncapped rows. `nocaps` has none (every category is capped,
    # so there is no row to disclose on) and `empty` has no rows at all — those are the two skips, unchanged in spirit.
    "budgets-suggestions": (("nocaps", "empty"), "nothing to click: #page .nb .nbrow .help-trigger",
                            "no uncapped row to disclose a suggestion on (nocaps: every category is capped)"),
    "debt-cards-open": (("empty",), "nothing to click: #page .dg .hd[aria-expanded]", "nothing owed"),
    "debt-sort-apr": (("empty",), "nothing to click: #page .sorttabs button[data-s=apr]", "nothing owed"),
    "loan-panel": (("nocaps", "empty"), "nothing to click: #page [data-loan]", "no tracked loan (nocaps: \"Track a loan\")"),
    "paid-off-confirm": (("nocaps", "empty"), "nothing to click: #page [data-loan]", "no tracked loan"),
    # The empty fixture explicitly sets ai.mode=off and has no rules or user-filed charges. These states remain
    # required on every AI-enabled/rules-populated fixture; only the actual absent controls are accounted for here.
    "chat-closed": (("empty",), "never appeared: #askb", "AI off: no Ask button"),
    **{name: (("empty",), "nothing to click: #askb", "AI off: no Ask button")
       for name in ("chat-open", "chat-answer", "chat-approved", "chat-error", "chat-retry")},
    **{name: (("empty",), "never appeared: #page .rules .rrow[data-rule]", "no rules in the empty fixture")
       for name in ("rules-edit", "rules-delete")},
    "rules-suggestions": (("empty",), "never appeared: #page .rules .rsugs", "no user-filed charges in the empty fixture"),
}


def scope(reports: dict[str, dict], want: dict[str, set[tuple[str, str]]]) -> tuple[dict, list[str]]:
    """TEST-SPEED review B5: each DB's layout report against exactly the states planned for it (by DB, width, theme and
    state; not a count: a state missing and another there instead counted the same). Returns ({"checked", "absent",
    "planned": n, "why": {reason: n}}, what is wrong). Checked: its entry has every check's result. Absent: skipped where
    ABSENT says the DB lacks it (reported, not checked). Wrong: a planned state with no entry (missing), an entry nobody
    planned (unexpected), a state skipped on a DB that should have it, an entry without every check's result (review
    round 3: each one as audit_layout writes it), a report for a DB not in `want`."""
    _gate_path()
    import audit_layout  # noqa: E402
    checks = set(audit_layout.CHECKS)
    out: dict = {"checked": 0, "absent": 0, "planned": 0, "why": collections.Counter()}
    wrong: list[str] = []
    for k, states in want.items():
        rep = reports.get(k)
        out["planned"] += len(states)   # (review round 3: a DB without a report still had them planned)
        if rep is None:
            wrong.append(f"layout {k}: no report ({len(states)} states planned)")
            continue
        got = {(w, key): e for w, entries in rep["widths"].items() for key, e in entries.items()}
        bad: dict[str, list[str]] = collections.defaultdict(list)
        for w, key in sorted(states - set(got)):
            bad["missing"].append(f"{key} {w}")
        for w, key in sorted(set(got) - states):
            bad["unexpected"].append(f"{key} {w}")
        for (w, key), e in sorted((x, got[x]) for x in states & set(got)):
            name = key.split("@")[0]
            if "skipped" in e:
                dbs, says, why = ABSENT.get(name, ((), "", ""))
                if k in dbs and says in e["skipped"]:
                    out["absent"] += 1
                    out["why"][f"{name}: {why}"] += 1
                else:
                    bad["skipped, and it should be on this DB"].append(f"{key} {w} ({e['skipped'].split(chr(10))[0][:90]})")
            elif _every_check(e.get("checks"), checks):
                out["checked"] += 1
            else:
                bad["without every check's result"].append(f"{key} {w}")
        for what, items in bad.items():
            wrong.append(f"layout {k}: {len(items)} {what}: {', '.join(items[:6])}{' ...' if len(items) > 6 else ''}")
    for k in sorted(set(reports) - set(want)):   # (review round 3: the gates run only the DBs asked for)
        wrong.append(f"layout {k}: a report, for a DB the gates weren't asked for")
    return out, wrong


def _every_check(got, checks: set) -> bool:
    """(Review round 3) an entry's `checks`: a result for each of audit_layout's, each one as it writes them (a dict
    with a true / false `pass`), not merely a key."""
    return isinstance(got, dict) and set(got) >= checks and all(isinstance(got[c], dict) and isinstance(got[c].get("pass"), bool)
                                                                for c in checks)


def scope_line(sc: dict) -> str:
    return (f"{sc['planned']} planned layout states: {sc['checked']} checked, {sc['absent']} reported absent (not "
            f"checked: their DB lacks them, per check.py ABSENT)")


def gate_scope(xml: Path, want: list[str], reports: dict[str, dict]) -> tuple[dict, list[str]]:
    """TEST-SPEED review F1: what `full` asked of tests/test_gates.py and it didn't do. It skips (and passes) a DB it
    isn't given, so each DB of `want` must have had its gate tests run (passed or failed, not skipped or absent), and
    (review B5) its layout report exactly the states planned for it (scope()). Returns (scope's counts, what's wrong)."""
    ran: dict[str, str] = {}
    try:
        for case in ET.parse(xml).getroot().iter("testcase"):
            ran[case.get("name", "")] = "skipped" if case.find("skipped") is not None else "ran"
    except (OSError, ET.ParseError):
        pass   # (no report at all: a FAIL of its own)
    out = [f"{t}: {ran.get(t, 'not run')}" for k in want
           for t in ([f"test_gate_1_numbers[{k}]"] if k in GATE_KINDS else []) + [f"test_gate_2_layout[{k}]"]
           + ([f"test_gate_3_controls[{k}]"] if k in CONTROL_KINDS else [])
           if ran.get(t) != "ran"]
    sc, wrong = scope(reports, planned(want))
    return sc, out + wrong


# --- running -------------------------------------------------------------------------------------------------------------

_CHILDREN: list[Pytest] = []
LEDGER: Path | None = None


def note(event: str, *fields) -> None:
    """One line in the run's ledger, next to tests/cdp.py's (a Chrome's start / stop): a pytest run's process group
    started (`group`), or ended (`ended`: nothing of it left, its leader reaped)."""
    if LEDGER:
        with LEDGER.open("a") as f:
            f.write(" ".join([event, f"{time.time():.3f}", *map(str, fields)]) + "\n")


def _stop_children(*_):
    runs = [r for r in _CHILDREN if not r.ended]
    for r in runs:
        r.signal(signal.SIGTERM)   # its own group: pytest, its workers; their Chromes close on SIGTERM
    deadline = time.monotonic() + 15
    for r in runs:
        r.end(max(0.1, deadline - time.monotonic()))


KINFO_PROC_SIZE = 648   # macOS: sizeof(struct kinfo_proc)
SZOMB = 5               # its kp_proc.p_stat for a zombie
UNKNOWN = -1            # (review round 3) _proc_stat(): neither sysctl nor ps could tell
_SYSCTL = None


def _ps_out(cmd: list[str]) -> str | None:
    """TEST-SPEED review round 3: the output of `cmd`, a ps that lists this process among others (pid first on each
    line), or None when it can't be taken for a whole answer: ps failed, didn't answer in PS_WAIT s, exited nonzero,
    or didn't list this process (a listing without its own caller isn't one; a query by pid names this process too,
    so its exit status can't mean just "none of those"). A failed one used to read as "nothing running"."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=PS_WAIT)
    except (OSError, subprocess.SubprocessError):
        return None
    me = str(os.getpid())
    if res.returncode != 0 or not any(ln.split(None, 1)[:1] == [me] for ln in res.stdout.splitlines()):
        return None
    return res.stdout


def _proc_stat(pid: int) -> int | None:
    """macOS: process `pid`'s state (sysctl kern.proc.pid: SZOMB for a zombie), None when there is no such process,
    UNKNOWN when neither sysctl (its answer not believed) nor ps (_ps_out: failed, or no answer in PS_WAIT s; it waited
    for ever, and a failed one said "no such process") can tell."""
    global _SYSCTL
    try:
        import ctypes
        _SYSCTL = _SYSCTL or ctypes.CDLL(None, use_errno=True).sysctl
        buf, size = ctypes.create_string_buffer(KINFO_PROC_SIZE), ctypes.c_size_t(KINFO_PROC_SIZE)
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)   # CTL_KERN, KERN_PROC, KERN_PROC_PID
        if _SYSCTL(mib, 4, buf, ctypes.byref(size), None, 0) == 0 and size.value in (0, KINFO_PROC_SIZE):
            if not size.value:
                return None
            # kp_proc.p_stat: after p_un (16), 2 pointers (16), p_flag (4); then kp_proc.p_pid at 40. (TEST-SPEED review
            # B4: the size alone doesn't show the layout is the one assumed: the pid read back at 40 is looked at too;
            # anything else, ps below. Still not a full check of the layout: the offsets of p_stat and p_pid only.)
            if int.from_bytes(buf.raw[40:44], sys.byteorder) == pid:
                return buf.raw[36]
    except (OSError, AttributeError, ImportError):
        pass
    out = _ps_out(["ps", "-o", "pid=,stat=", "-p", f"{pid},{os.getpid()}"])
    if out is None:
        return UNKNOWN
    st = next((f[1] for f in map(str.split, out.splitlines()) if len(f) >= 2 and f[0] == str(pid)), None)
    return None if st is None else SZOMB if st.startswith("Z") else 0


def _exited(proc: subprocess.Popen) -> bool:
    """Has our child `proc` exited? Without reaping it (Popen.poll() reaps): until it's reaped, an exited leader's pid (its
    group's id) can't be given to another process, so what is left of its group can still be signalled by that id
    (TEST-SPEED review F3; tests/cdp.py has the same for its Chromes). Review round 3: a state that can't be told
    (_proc_stat UNKNOWN) is not an exit (it was one: a pytest still running then had its group stopped as if done)."""
    return _state(proc) == "exited"


def _state(proc: subprocess.Popen) -> str:
    """Our child `proc`, not reaped: "exited", "running", or (review round 4) "unknown" when that can't be told
    (_proc_stat UNKNOWN), which _exited() takes for not exited and Pytest._watch() tells apart."""
    if proc.returncode is not None:
        return "exited"
    if hasattr(os, "waitid"):
        try:
            return "exited" if os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None else "running"
        except ChildProcessError:
            return "exited"
    st = _proc_stat(proc.pid)
    return "unknown" if st == UNKNOWN else "exited" if st in (None, SZOMB) else "running"


def _on_signal(signum, frame):
    """SIGTERM / SIGHUP: unwind to main()'s `finally`, which stops everything. Nothing else here: the signal can land
    inside a Popen.wait(), and waiting on that child again from the handler would deadlock on its lock."""
    raise SystemExit(128 + signum)


def unfinished(ledger: Path) -> tuple[list[int], list[tuple[int, str]]]:
    """Per the ledger: (the pytest runs' process groups not ended, (pgid, profile) of the Chromes not stopped)."""
    groups: dict[int, bool] = {}
    chromes: dict[int, str] = {}
    for line in ledger.read_text().splitlines() if ledger.exists() else []:
        f = line.split(None, 5)
        try:
            if f[0] == "group":
                groups[int(f[2])] = True
            elif f[0] == "ended":
                groups.pop(int(f[2]), None)
            elif f[0] == "start":
                chromes[int(f[3])] = f[5]
            elif f[0] == "stop":
                chromes.pop(int(f[3]), None)
        except (IndexError, ValueError):
            continue   # a line being written
    return list(groups), list(chromes.items())


def _alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):   # (macOS: EPERM when only a zombie is left)
        return False
    return _proc_group_running(pgid) is not False   # (Linux succeeds then: /proc tells a zombie apart)


def _proc_group_running(pgid: int) -> bool | None:
    """Linux: does a process of group `pgid` other than a zombie run (from /proc/<pid>/stat)? None where there's no
    /proc to ask (macOS), which _alive takes for "yes": the answer killpg gave. (tests/cdp.py has the same.)"""
    try:
        pids = [e for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()   # state, ppid, pgrp, ...
        except OSError:
            continue   # gone since
        if len(fields) >= 3 and fields[2] == str(pgid) and fields[0] not in ("Z", "X"):
            return True
    return False


def _members(pgid: int) -> list[int] | None:
    """The live processes of group `pgid` (to say how many; never to signal: that's by the group, see Pytest.end());
    None when ps fails (review round 3: _ps_out; it waited for ever)."""
    out = _ps_out(["ps", "-axo", "pid=,pgid=,stat="])
    if out is None:
        return None
    return [int(f[0]) for f in map(str.split, out.splitlines()) if len(f) == 3 and f[1] == str(pgid) and f[2][0] != "Z"]


def _ps(*pids: int) -> list[tuple[int, int, str, str, str]] | None:
    """(pid, pgid, state, start time, command line and environment) of every process but this one (only `pids`' when
    given): one `ps` with environments (PS_ENV; the environments are this user's processes' only). Review round 3: None when it can't be taken
    for a whole answer (_ps_out), not [], which read as "none of them running": a failed look let the watchdog delete
    the profile of a Chrome still running and say left=0."""
    me = os.getpid()
    which = ["-p", ",".join(map(str, sorted({*pids, me})))] if pids else [PS_ALL]
    out = _ps_out(["ps", PS_ENV, "-ww", *which, "-o", "pid=,pgid=,stat=,lstart=,command="])
    if out is None:
        return None
    rows = []
    for line in out.splitlines():
        f = line.split(None, 8)   # (lstart is 5 words: "Wed Sep 30 11:00:09 2026")
        try:
            row = (int(f[0]), int(f[1]), f[2], " ".join(f[3:8]), f[8] if len(f) > 8 else "")
        except (IndexError, ValueError):
            continue
        if row[0] != me:
            rows.append(row)
    return rows


def _marker(m: str) -> re.Pattern:
    return re.compile(re.escape(m) + r"(?=[/\s]|$)")


def _owned(marks: list[tuple[int, str]]) -> dict[int, dict[int, str]] | None:
    """{pgid: {pid: its start time} of the processes of that group still the ledger's}: each (pgid, marker) names a
    group and what its processes carry, the run's log dir for a pytest run (on the pytest command line, and in every
    process's environment: HF_CHECK_RUN), the profile for a Chrome (on its processes' command lines). One `ps -E`. A
    process is the run's only if it is in that group *and* names that marker: the group's leader may be gone (killed,
    crashed; reaped since) with its workers or helpers still running (TEST-SPEED review F3 / N1), and a pid or group id
    reused since by someone else's process names neither. Zombies are left out. (A process whose environment ps can't
    show, a SIP platform binary's, is found only by its command line: left alone otherwise, and not seen: so an empty
    answer says no process of the run *that can be told for one* runs.) None when the `ps` failed (review round 3)."""
    rx = {pg: _marker(m) for pg, m in marks}
    found: dict[int, dict[int, str]] = {pg: {} for pg in rx}
    rows = _ps()
    if rows is None:
        return None
    for pid, pgid, stat, start, cmd in rows:
        if pgid in rx and not stat.startswith("Z") and rx[pgid].search(cmd):
            found[pgid][pid] = start
    return found


def _kill(pg: int, pids: dict[int, str], marker: re.Pattern, sig: int) -> list[int]:
    """TEST-SPEED review B4: signal those of `pids` ({pid: start time}, from _owned()) that are, looked at again just
    before (one `ps -p` of them), still the same processes: the same start time, in group `pg`, naming the marker, not
    zombies. A pid whose process has ended since, and been given to another, isn't signalled; nor is a bare group id,
    ever. What is left (review round 3: not closed, and not claimed to be) is the moment between that look and each
    signal (no pid handle on macOS; one look for all of them, so a later signal is a little further from it), and a
    start time to the second (ps's lstart): a pid reused within that second, in that group, carrying the marker,
    would pass. A failed look (_ps None) signals nothing. Returns the pids signalled."""
    rows = _ps(*pids) if pids else []
    now = {pid: (pgid, stat, start, cmd) for pid, pgid, stat, start, cmd in rows or []}
    sent = []
    for pid, start in pids.items():
        cur = now.get(pid)
        if cur and cur[0] == pg and not cur[1].startswith("Z") and cur[2] == start and marker.search(cur[3]):
            try:
                os.kill(pid, sig)
                sent.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
    return sent


def watchdog(ledger: Path) -> int:
    """`check.py --watchdog <ledger>`: started by every run before anything else, in a session of its own, reading a pipe
    from the run. When the pipe closes (the run has ended, however: killed with SIGKILL, its terminal gone) it stops what
    the ledger says is still running: each pytest run's process group (SIGTERM: its workers close their Chromes) and every
    Chrome not stopped, SIGKILL for whatever is left after GRACE s. TEST-SPEED review B3: then it looks again (for up to
    KILL_WAIT s) and deletes the profiles only of the Chromes seen gone; what is still running is kept (profile and all),
    counted (left=N) and said on stderr. After a normal end the run has stopped all of it itself, and this finds nothing.
    It writes one line of what it did in the ledger.

    Review round 3: returns its verdict (the exit status of `check.py --watchdog`, which the run's end reads: main()):
    0 once every process of the run it looks for has been seen gone (or the ledger has none left to look for), 1 when
    some still run after SIGKILL or the last look at them failed (_owned None: then nothing is known gone and no
    profile is deleted; a failed look used to read as "none running"). Its line: found= the groups seen with processes
    of the run (in any look before SIGKILL), sigkill= how many were sent SIGKILL, stopped= those found and seen gone at the
    end, left= the processes still running ("unknown" for the last two when the last look failed; it said stopped= for
    the groups found, gone or not)."""
    sys.stdin.buffer.read()
    t0 = time.monotonic()
    groups, chromes = unfinished(ledger)
    marks = [(pg, str(ledger.parent)) for pg in groups] + [(pg, prof) for pg, prof in chromes]
    rx = {pg: _marker(m) for pg, m in marks}
    # TEST-SPEED review F3 / N1: each process checked to be the run's (_owned), and signalled by its own pid, never a bare
    # group id: a group whose leader has gone may still have workers / helpers, or be a stranger's by now (review B4: and
    # each pid looked at again just before it's signalled, _kill). Round 3: a look that fails stands for nothing (the
    # last good one stands); each process found gets SIGTERM once, whichever look found it
    found: set[int] | None = None
    left: dict[int, dict[int, str]] = {}
    termed: set[int] = set()
    deadline = time.monotonic() + GRACE
    while marks:
        now = _owned(marks)
        if now is not None:
            left = {pg: pids for pg, pids in now.items() if pids}
            found = (found or set()) | set(left)
            for pg, pids in left.items():
                if new := {pid: start for pid, start in pids.items() if pid not in termed}:
                    termed.update(_kill(pg, new, rx[pg], signal.SIGTERM))
        if (now is not None and not left) or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    killed = [pg for pg, pids in left.items() if _kill(pg, pids, rx[pg], signal.SIGKILL)]   # (sent: not a failed look's)
    still: dict[int, dict[int, str]] | None = {}
    gone_by = time.monotonic() + KILL_WAIT
    while marks:
        now = _owned(marks)
        still = None if now is None else {pg: pids for pg, pids in now.items() if pids}
        if still == {} or time.monotonic() >= gone_by:
            break
        time.sleep(0.1)
    if still is not None:
        for pg, prof in chromes:
            if pg not in still and Path(prof).name.startswith("hf-cdp-"):
                shutil.rmtree(prof, ignore_errors=True)
    n = "unknown" if still is None else sum(len(pids) for pids in still.values())
    stopped = "unknown" if still is None or (found is None and marks) else len((found or set()) - set(still))
    with ledger.open("a") as f:
        f.write(f"watchdog {time.time():.3f} groups={len(groups)} chromes={len(chromes)} stopped={stopped} "
                f"sigkill={len(killed)} found={'unknown' if found is None and marks else len(found or ())} left={n} "
                f"secs={time.monotonic() - t0:.1f}\n")
    if still is None:
        print(f"check.py watchdog: the processes of the run couldn't be listed (ps failed): none of them is known to be "
              f"stopped, and every profile is kept ({ledger})", file=sys.stderr, flush=True)
    elif n:
        print(f"check.py watchdog: {n} process(es) of the run still running {KILL_WAIT} s after SIGKILL "
              f"({'; '.join(f'group {pg}: {sorted(p)}' for pg, p in still.items())}); their profiles are kept "
              f"({ledger})", file=sys.stderr, flush=True)
    return 0 if still == {} else 1


class Pytest:
    """One pytest run in its own process group, output to a log, counts from its JUnit XML."""

    def __init__(self, name: str, args: list[str], logs: Path, env: dict):
        self.name, self.log, self.xml = name, logs / f"{name}.log", logs / f"{name}.xml"
        self.basetemp = logs / f"tmp-{name}"
        # CHECK_MAXFAIL=N: stop at the Nth failure instead of running on (a fix round learns of a failure in minutes,
        # not at the end of a 40-minute run; the Nightly's on-demand maxfail input sets it)
        maxfail = [f"--maxfail={int(m)}"] if (m := os.environ.get("CHECK_MAXFAIL", "").strip()) else []
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:cacheprovider", "-rfE",
               f"--junitxml={self.xml}", f"--basetemp={self.basetemp}", *maxfail, *args]
        self.t0 = time.monotonic()
        self.proc = subprocess.Popen(cmd, cwd=ROOT, env={**env, "HF_CHECK_RUN": str(logs)}, stdout=self.log.open("w"),
                                     stderr=subprocess.STDOUT, start_new_session=True)
        self.ended = False
        self.stuck = ""   # (TEST-SPEED review B3) why its group couldn't be ended
        _CHILDREN.append(self)
        note("group", self.proc.pid, name)
        self.t1 = None   # when it ended (its own time: the run may only wait for it after the gates)
        self.left = threading.Event()   # the pytest process has exited (not reaped: see end())
        self.over = threading.Event()   # (review round 4) wait()'s wait is over: `left`, or its state unknown too long
        self.unknown = ""               # (review round 4) why it is over without `left`
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        """Sets `left` once the pytest process has exited, seen without reaping it. Review round 4: a state that can't be
        told (_state "unknown": sysctl's answer not believed and ps failing) is no exit, and was waited on for ever (a
        pytest that had exited was never seen to, and the run never ended). Then its group is asked (_alive: the kernel,
        no ps; by its id, safe while its leader is unreaped): no process of it running, pytest's own included (the
        leader's zombie is all that's left), is its exit, as end() takes an empty group. A group still running, with
        the state unknown for UNKNOWN_WAIT s on end, ends the wait (`over`): wait() fails, saying so, and leaves the
        run's record as it is (its leader unreaped, its group not noted ended: the run's stop and its watchdog see to
        it). This goes on watching, so a later end() still sees the exit."""
        since = None   # since when its state has been unknown (without a break)
        while (st := _state(self.proc)) != "exited" and not (st == "unknown" and not _alive(self.proc.pid)):
            if st != "unknown":
                since = None
            elif since is None:
                since = time.monotonic()
            elif not self.over.is_set() and time.monotonic() - since >= UNKNOWN_WAIT:
                self.unknown = (f"its state couldn't be told for {UNKNOWN_WAIT:.0f} s (sysctl's answer not believed, ps "
                                f"failing) while its process group still ran: not known to have ended, so not ended here "
                                f"(its leader left unreaped, the run's stop and its watchdog see to its group)")
                self.over.set()
            time.sleep(0.05 if st != "unknown" else 0.25)   # (each unknown look ran a failed ps: not 20 a second)
        self.t1 = time.monotonic()
        self.left.set()
        self.over.set()

    def signal(self, sig: int) -> None:
        """The whole group: only while its leader is unreaped (the group's id is still its own then)."""
        if self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    def end(self, grace: float) -> int:
        """TEST-SPEED review F3: the run's process group ends, and only then is it noted ended. Its pytest process (the
        leader) exits first; what's still in its group a moment later (workers that outlived it: pytest was killed or
        crashed) gets SIGTERM, then SIGKILL `grace` s from now (so does a leader still running then). The leader is
        reaped last: until then its pid, the group's id, can't be another process's, so signalling the group is safe.
        Review B3: a group still there KILL_WAIT s after SIGKILL (or a leader that never exited) isn't reaped or noted
        ended: `stuck` says why, and the ledger's open `group` leaves it to the watchdog. Returns how many processes
        outlived the leader (0 when the group ended with it)."""
        if self.ended:
            return 0
        pg, deadline = self.proc.pid, time.monotonic() + grace
        self.left.wait(grace)
        settle = min(deadline, time.monotonic() + 2)
        while self.left.is_set() and _alive(pg) and time.monotonic() < settle:   # (a worker's children, exiting after it)
            time.sleep(0.05)
        members = _members(pg) if self.left.is_set() and _alive(pg) else []
        outlived = 1 if members is None else len(members)   # (round 3: ps failed, the group still there: one at least)
        if outlived:
            self.signal(signal.SIGTERM)
            while _alive(pg) and time.monotonic() < deadline:
                time.sleep(0.05)
        if _alive(pg) or not self.left.is_set():
            self.signal(signal.SIGKILL)
            self.left.wait(KILL_WAIT)
            stop = time.monotonic() + KILL_WAIT
            while _alive(pg) and time.monotonic() < stop:
                time.sleep(0.05)
        if not self.left.is_set() or _alive(pg):
            n = len(m) if (m := _members(pg)) is not None else "?"
            self.stuck = (f"{n} process(es) of its group{'' if self.left.is_set() else ', pytest itself among them,'} still "
                          f"running {KILL_WAIT} s after SIGKILL: not noted ended (the watchdog stops them)")
            note("stuck", pg, self.name, f"alive={n}")
            return outlived
        rc = self.proc.wait()
        self.ended, self.stuck = True, ""
        note("ended", pg, self.name, f"rc={rc}", f"outlived={outlived}")
        return outlived

    def wait(self) -> dict:
        self.over.wait()   # (review round 4: bounded when its state can't be told; see _watch())
        if not self.left.is_set():   # (its report, if any, may be half written: not read)
            return {"name": self.name, "rc": None, "secs": time.monotonic() - self.t0, "tests": 0, "failed": 1,
                    "skipped": 0, "why": self.unknown}
        outlived = self.end(GRACE)
        rc = self.proc.returncode
        out = {"name": self.name, "rc": rc, "secs": (self.t1 or time.monotonic()) - self.t0, "tests": 0, "failed": 0,
               "skipped": 0}
        if outlived:
            out["why"] = f"{outlived} process(es) of its group outlived pytest (exit {rc}): stopped"
        try:
            root = ET.parse(self.xml).getroot()
            for s in root.iter("testsuite"):
                out["tests"] += int(s.get("tests", 0))
                out["failed"] += int(s.get("failures", 0)) + int(s.get("errors", 0))
                out["skipped"] += int(s.get("skipped", 0))
        except (OSError, ET.ParseError):
            out["failed"] += 1   # no report: it crashed
        if rc == 5 or not out["tests"]:   # TEST-SPEED review F6: every run here is asked for tests; none ran is a FAIL
            out["failed"] += 1
            out["why"] = f"no tests ran (pytest exit {rc})"
        elif rc != 0 and not out["failed"]:
            out["failed"] = 1
        if self.stuck:   # (review B3)
            out["failed"] += 1
            out["why"] = "; ".join(filter(None, [out.get("why"), self.stuck]))
        return out

    def failures(self) -> list[str]:
        lines = self.log.read_text(errors="replace").splitlines() if self.log.exists() else []
        return [ln for ln in lines if ln.startswith(("FAILED", "ERROR"))] or lines[-25:]


def layout_summary(reports: list[dict]) -> dict:
    """Over layout reports (one per DB): checks, FAILs, the states skipped (by reason: a SKIP can hide a state that never
    came, so they're shown, never only counted; scope() says which were allowed), and the states checked before they
    settled."""
    out = {"checks": 0, "fail": 0, "skips": 0, "reasons": collections.Counter(), "unsettled": 0, "dbs": 0, "selftests": 0}
    for rep in reports:
        out["dbs"] += 1
        out["selftests"] += rep.get("selftest") == "ok"
        out["checks"] += rep["pass"] + rep["fail"]
        out["fail"] += rep["fail"]
        for states in rep["widths"].values():
            for e in states.values():
                if "skipped" in e:
                    out["skips"] += 1
                    out["reasons"]["nothing to click" if "nothing to click" in e["skipped"] else
                                   "never appeared" if "never appeared" in e["skipped"] else e["skipped"][:40]] += 1
                out["unsettled"] += bool(e.get("unsettled"))
    return out


def layout_line(s: dict) -> str:
    reasons = ", ".join(f"{k} {n}" for k, n in s["reasons"].most_common())
    return (f"{s['checks']} layout checks on {s['dbs']} DB(s) (self-test passed on {s['selftests']}), {s['fail']} FAIL; "
            f"{s['skips']} states skipped, not checked{f' ({reasons})' if reasons else ''}; {s['unsettled']} checked "
            f"before they settled")


def run_gate_subset(p: Plan, logs: Path) -> dict:
    """fast's gates: the selected layout states on the selected DBs (one self-test), and the numbers gate. In this
    process (run_gates is imported here, after the environment is made safe)."""
    sys.path[:0] = [str(ROOT / "src"), str(TESTS), str(ROOT / "scripts")]
    os.environ["HF_GATE_SERVER_LOG"] = str(logs / "gate-servers.log")
    import run_gates  # noqa: E402
    res = {"layout": None, "numbers": 0, "numbers_fail": 0, "lines": [], "secs": 0.0, "missing": [], "scope": None}
    t0 = time.monotonic()
    kinds = p.kinds()
    if p.numbers:
        for r in run_gates.run(list(GATE_KINDS), "numbers", logs / "numbers"):
            res["numbers"] += len(r["numbers"])
            res["numbers_fail"] += len(r["numbers_fail"])
            res["lines"] += r["lines"]
            if not r["numbers"]:   # (TEST-SPEED review F1: a DB the numbers gate checked nothing on didn't run)
                res["missing"].append(f"numbers {r['kind']}: no checks")
    if kinds:
        first = next((k for k in kinds if k not in AUTH_KINDS), None)
        reports = {}
        for r in run_gates.run(kinds, "layout", logs / "layout", select=p.select, self_test={first} if first else False):
            reports[r["kind"]] = r["layout"]
            res["lines"] += r["lines"]
        res["layout"] = layout_summary(list(reports.values()))
        res["scope"], wrong = scope(reports, planned(kinds, p.select))   # (review B5: exactly the states selected)
        res["missing"] += wrong
    res["secs"] = time.monotonic() - t0
    return res


def fmt(secs: float) -> str:
    return f"{int(secs // 60)}m{int(secs % 60):02d}s"


KEEP_OLD_LOGS = 2


def _log_dir_pid(path: Path) -> int | None:
    m = re.fullmatch(r"hf-check-(\d+)-.+", path.name)
    return int(m.group(1)) if m else None


def _prune_old_logs(keep: Path) -> None:
    """Bound retained scratch directories; never remove live runs or unrecognized names."""
    dirs = sorted((p for p in Path(tempfile.gettempdir()).glob("hf-check-*")
                   if p.is_dir() and _log_dir_pid(p) is not None),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    kept = 0
    for directory in dirs:
        pid = _log_dir_pid(directory)
        if directory != keep:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            except PermissionError:
                continue  # inability to inspect a process is not proof that it is dead
            else:
                continue
        if directory == keep or kept < KEEP_OLD_LOGS:
            kept += 1
            continue
        shutil.rmtree(directory, ignore_errors=True)
        print(f"check.py: removed stale scratch directory: {directory}", flush=True)


def parse_args(argv: list[str]):
    """Parse policy separately from execution, so the safe default is testable without running acceptance."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", nargs="?", choices=("fast", "full", "ci"))
    ap.add_argument("--tier", choices=("fast", "ship"), help="fast: derived test files; ship: existing full acceptance")
    ap.add_argument("--base", default=os.environ.get("CHECK_BASE", "main"), help="fast: diff against this branch")
    ap.add_argument("--files", nargs="*", help="fast: check as if these files (repo paths) had changed")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    ap.add_argument("--part", type=int, choices=(1, 2), help="full: only its first half (every test but the gates) or its "
                    "second (tests/test_gates.py), for a caller that can't wait 10 minutes in one go")
    ap.add_argument("--kinds", help=f"full --part 2: the gates on these DBs only (comma-separated, of {','.join(ALL_DBS)})")
    ap.add_argument("--keep-logs", action="store_true", help="keep the log dir (the JUnit XML, the ledger, the gate "
                    "reports) even when everything passed")
    args = ap.parse_args(argv)
    if args.tier and args.mode:
        ap.error("choose either a positional mode or --tier")
    args.mode = args.mode or "full"
    if args.tier == "fast" and (args.part is not None or args.kinds is not None):
        ap.error("--tier fast cannot be narrowed with --part or --kinds (use full / --tier ship)")
    if args.mode == "ci" and (args.part is not None or args.files is not None):
        ap.error("ci runs its whole list: no --part, --kinds or --files")
    kinds = args.kinds.split(",") if args.kinds is not None else []
    if args.kinds is not None and (args.mode != "full" or args.part != 2 or not all(kinds) or set(kinds) - set(ALL_DBS)
                                   or len(set(kinds)) != len(kinds)):
        ap.error(f"--kinds needs `full --part 2` and DBs of {','.join(ALL_DBS)} (each once)")
    return args


def tree_fingerprint() -> str:
    """HEAD plus tracked changes and untracked source content, independent of mtimes."""
    def git(*args):
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True).stdout

    head = git("rev-parse", "HEAD").strip().decode("ascii")
    digest = hashlib.sha256(git("diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv"))
    for name in sorted(git("ls-files", "--others", "--exclude-standard", "-z", "--", "src/", "tests/").split(b"\0")):
        if name:
            content = (ROOT / os.fsdecode(name)).read_bytes()
            digest.update(len(name).to_bytes(8, "big") + name)
            digest.update(len(content).to_bytes(8, "big") + content)
    return f"{head}:{digest.hexdigest()}"


@contextmanager
def check_lock(path: Path | None = None):
    """Serialize explicit full/ship runs across worktrees. Fast never enters here."""
    path = path or Path.home() / ".hermes" / "locks" / "hermes-finance-check.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock:
        next_notice = 0.0
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                now = time.monotonic()
                if now >= next_notice:
                    lock.seek(0)
                    try:
                        owner = json.load(lock)
                        if not isinstance(owner, dict):
                            owner = {}
                    except (ValueError, OSError):
                        owner = {}
                    print(f"waiting for check lock held by pid {owner.get('pid', 'unknown')} "
                          f"({owner.get('branch', 'unknown')}, {owner.get('worktree', 'unknown')})", flush=True)
                    next_notice = now + 60
                time.sleep(0.2)
        try:
            branch = subprocess.run(["git", "-C", str(ROOT), "branch", "--show-current"],
                                    capture_output=True, text=True, check=True).stdout.strip() or "(detached)"
            lock.seek(0)
            lock.truncate()
            json.dump({"pid": os.getpid(), "branch": branch, "worktree": str(ROOT)}, lock)
            lock.flush()
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    start = tree_fingerprint()
    print(f"TREE: {start}", flush=True)
    # --tier fast has mode=full internally, including its fallback: use the requested tier.
    guard = check_lock() if args.tier != "fast" and args.mode == "full" else nullcontext()
    with guard:
        try:
            rc = _main(args)
        finally:
            end = tree_fingerprint()
            stale = start != end
            if stale:
                print(f"STALE: tree changed mid-run ({start} -> {end})", flush=True)
            if args.tier == "fast":
                print("TIER fast (NOT a ship verdict)", flush=True)
    return 1 if stale else rc


def _main(args) -> int:
    kinds = args.kinds.split(",") if args.kinds is not None else []
    t0 = time.monotonic()
    for k in [k for k in os.environ if SECRET_ENV.search(k) or k in INHERITED_SCOPE]:
        del os.environ[k]
    os.environ.update(safe_env())
    cpu, browser = test_files()
    logs = Path(tempfile.mkdtemp(prefix=f"hf-check-{os.getpid()}-"))
    _prune_old_logs(logs)
    global LEDGER
    LEDGER = logs / "ledger.txt"
    LEDGER.touch()
    os.environ["HF_CHROME_LEDGER"] = str(LEDGER)   # tests/cdp.py: every Chrome this run starts, and stops, goes in it
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _on_signal)
    dog = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--watchdog", str(LEDGER)],
                           stdin=subprocess.PIPE, start_new_session=True, env=safe_env())
    ok, why, rc = False, "", 1
    try:
        rc = _run(args, kinds, cpu, browser, logs, t0)
        ok = rc == 0
    finally:
        _stop_children()
        why = _end_watchdog(dog)
        if why:   # (TEST-SPEED review round 3: the run fails, and its record of what may still run is kept)
            print(f"check.py: the run's cleanup failed: {why}. The run fails; its ledger and logs are kept, and so is "
                  f"every profile not seen stopped ({LEDGER})", flush=True)
        if ok and not why and not args.keep_logs:
            shutil.rmtree(logs, ignore_errors=True)
        else:
            print(f"(logs: {logs})")
    return 1 if why else rc


def _end_watchdog(dog: subprocess.Popen) -> str:
    """The run's end (TEST-SPEED review round 3): the watchdog's cue (its pipe closed), then its verdict: '' when it saw
    every process of the run gone (watchdog()), else why not. Its exit status was dropped (a Chrome it couldn't stop,
    or couldn't look for, and the run passed all the same and deleted its ledger), and one that didn't finish in time
    was killed, alone (a `ps` it had started went on), and the run passed too. Now the run fails either way, and one
    not done in GRACE + WATCHDOG_WAIT s is killed with its group (it leads one; unreaped, so the id is still its own)."""
    with contextlib.suppress(OSError):
        dog.stdin.close()
    bound = GRACE + WATCHDOG_WAIT
    try:
        rc = dog.wait(bound)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(dog.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            dog.wait(KILL_WAIT)
        return f"its watchdog didn't finish in {bound:.0f} s (killed): nothing it was to stop is known to be stopped"
    return "" if rc == 0 else (f"its watchdog (exit {rc}) found processes of the run still running after SIGKILL, or "
                               f"couldn't list them")


def _run(args, kinds: list[str], cpu: list[str], browser: list[str], logs: Path, t0: float) -> int:
    if getattr(args, "tier", None) == "fast":
        return _run_tier_fast(args, cpu, browser, logs, t0)
    return _run_existing(args, kinds, cpu, browser, logs, t0)


def tier_changed_files(base: str) -> list[str]:
    """Release tier's explicit two-dot diff; legacy changed_files() keeps its old semantics."""
    return subprocess.run(["git", "-C", str(ROOT), "diff", "--name-only", f"{base}..HEAD"],
                          capture_output=True, text=True, check=True).stdout.splitlines()


def tier_selection(files: list[str], browser: list[str]):
    from tier_map import select_tests

    sources = {str(p.relative_to(ROOT)): p.read_text() for directory in (TESTS, ROOT / "scripts", ROOT / "src",
                                                                       ROOT / "hermes_plugin")
               for p in sorted(directory.rglob("*.py")) if "__pycache__" not in p.parts}
    return select_tests(files, sources, browser)


def _run_tier_fast(args, cpu: list[str], browser: list[str], logs: Path, t0: float) -> int:
    files = args.files if args.files is not None else tier_changed_files(args.base)
    selected, fallback = tier_selection(files, browser)
    print(f"check tier fast: {len(files)} changed file(s) vs {args.base if args.files is None else '--files'}", flush=True)
    for test, reasons in selected.items():
        print(f"  {test}: {'; '.join(dict.fromkeys(reasons))}", flush=True)
    if fallback:
        print("  -> FULL fallback: " + "; ".join(fallback), flush=True)
        full = argparse.Namespace(**vars(args))
        full.mode = "full"
        return _run_existing(full, [], cpu, browser, logs, t0)
    print(f"  -> {len(selected)} selected test files (per-file runs, stop on failure)", flush=True)
    if args.dry_run:
        return 0
    count = 0
    # Density catches common CSS breaks early; wiring checks all states before
    # lengthy regression suites. Each Pytest retains the existing process ledger.
    priority = {"tests/test_text_density.py": 0, "tests/test_gate_wiring.py": 1}
    for i, test in enumerate(sorted(selected, key=lambda t: (priority.get(t, 2), t))):
        env = safe_env(HF_NO_CHROME="1") if test in cpu else safe_env()
        run = Pytest(f"tier-{i}-{Path(test).stem}", ["--maxfail=1", test], logs, env)
        result = run.wait()
        count += result["tests"]
        print(f"  {test}: {result['tests']} tests, {result['failed']} failed in {fmt(result['secs'])}", flush=True)
        if result.get("why"):
            print(f"    {result['why']}", flush=True)
        if result["failed"]:
            print("    " + "\n    ".join(run.failures()[:30]), flush=True)
            return 1
    print(f"tier fast: {count} tests in {fmt(time.monotonic() - t0)}", flush=True)
    return 0


def _run_ci(args, cpu: list[str], logs: Path, t0: float) -> int:
    """`ci`: the release scan, then the no-browser files (the same list and environment as fast and full)."""
    scan_cmd = [sys.executable, str(ROOT / "scripts" / "dehardcode.py"), "--release-tracked"]
    print(f"check ci: 1) the release scan of every tracked file that ships; 2) {len(cpu)} no-browser files on "
          f"{CPU_WORKERS} workers (logs {logs})", flush=True)
    if args.dry_run:
        return 0
    scan = subprocess.run(scan_cmd, cwd=ROOT, env=safe_env(), capture_output=True, text=True, timeout=600)
    print(f"  release    {(scan.stdout + scan.stderr).strip()}", flush=True)   # path:line: rule, never what it found
    run = Pytest("nobrowser", ["-n", str(CPU_WORKERS), "--dist", "loadfile", *cpu], logs, safe_env(HF_NO_CHROME="1"))
    r = run.wait()
    print(f"  {r['name']:<10} {r['tests']:>5} tests, {r['failed']} failed, {r['skipped']} skipped in {fmt(r['secs'])}")
    if r.get("why"):
        print(f"    {r['why']}")
    if r["failed"]:
        print("    " + "\n    ".join(run.failures()[:30]))
    fails = r["failed"] + (scan.returncode != 0)
    print(f"CI: {r['tests']} tests, release scan exit {scan.returncode}, {fails} FAIL in {fmt(time.monotonic() - t0)}",
          flush=True)
    return 0 if fails == 0 and r["tests"] else 1


def _run_existing(args, kinds: list[str], cpu: list[str], browser: list[str], logs: Path, t0: float) -> int:
    """main()'s run: the plan, the pytest runs and gates, the summary; its exit status (main() adds its cleanup's)."""
    if args.mode == "ci":
        return _run_ci(args, cpu, logs, t0)
    if args.mode == "full":
        parts = (args.part,) if args.part else (1, 2)
        gates = ",".join(kinds) if kinds else "every DB"
        print(f"check full{f' part {args.part}' if args.part else ''}: "
              + (f"1) {len(cpu)} no-browser files on {CPU_WORKERS} workers with {len(browser)} browser files on "
                 f"{BROWSER_WORKERS}; " if 1 in parts else "")
              + (f"2) tests/test_gates.py (numbers, layout and controls gates, {gates}); " if 2 in parts else "")
              + f"{CHROMES} headless Chromes at most (logs {logs})", flush=True)
        if args.dry_run:
            return 0
        runs: list[Pytest] = []
        results: list[dict] = []
        if 1 in parts:   # the browser tests take every Chrome; the no-browser ones, the rest of the CPU meanwhile
            phase = [Pytest("nobrowser", ["-n", str(CPU_WORKERS), "--dist", "loadfile", *cpu], logs, safe_env(HF_NO_CHROME="1")),
                     Pytest("browser", ["-n", str(BROWSER_WORKERS), "--dist", "loadfile", *browser], logs, safe_env())]
            runs += phase
            results += [r.wait() for r in phase]
        want = kinds or list(ALL_DBS)
        if 2 in parts:   # then the gates, alone with every Chrome (one queue of every DB's states: all busy to the end)
            # (TEST-SPEED review F1: the DBs said, always: every one, or the --kinds; never what the shell had)
            env = safe_env(GATES_CHROMES=str(CHROMES), GATE_KINDS=",".join(want))
            runs.append(Pytest("gates", ["tests/test_gates.py"], logs, env))
            results.append(runs[-1].wait())
        reports = {rep.get("label"): rep for path in {p.resolve() for p in (logs / "tmp-gates").glob("layout*/layout-report.json")}
                   for rep in json.loads(path.read_text()).values()}   # (layoutcurrent is a symlink)
        lay = layout_summary(list(reports.values()))
        sc, missing = gate_scope(runs[-1].xml, want, reports) if 2 in parts else (None, [])
        fails = sum(r["failed"] for r in results) + len(missing)
        for r, run in zip(results, runs):
            print(f"  {r['name']:<10} {r['tests']:>5} tests, {r['failed']} failed, {r['skipped']} skipped in {fmt(r['secs'])}")
            if r.get("why"):
                print(f"    {r['why']}")
            if r["failed"]:
                print("    " + "\n    ".join(run.failures()[:30]))
        if reports:
            print(f"  layout     {layout_line(lay)}")
        if 2 in parts:   # (review B5: checked and reported-absent told apart; only a checked state was checked)
            print(f"  scope      the gates on {len(want)} DB(s) ({','.join(want)}): {scope_line(sc)}; "
                  + ("each DB's gate tests ran, and its report has exactly its planned states" if not missing else
                     f"{len(missing)} WRONG: " + "; ".join(missing[:12])))
            for why, n in sc["why"].most_common():
                print(f"    absent {n:>3}  {why}")
        print(f"FULL{f' (part {args.part}{f', {gates}' if kinds else ''})' if args.part else ''}: "
              f"{sum(r['tests'] for r in results)} tests, {lay['checks']} layout checks"
              + (f" ({sc['checked']} states checked, {sc['absent']} reported absent)" if sc else "")
              + f", {fails} FAIL in {fmt(time.monotonic() - t0)}", flush=True)
        return 0 if fails == 0 else 1

    files = args.files if args.files is not None else changed_files(args.base)
    p = plan_fast(files, browser)
    chosen, planned = layout_scope(p)
    print(f"check fast: {len(files)} changed file(s) vs {args.base if args.files is None else '--files'}", flush=True)
    for w in p.why:
        print(f"  {w}")
    print(f"  -> {len(cpu)} no-browser test files; browser: {', '.join(sorted(Path(b).stem for b in p.browser)) or 'none'}; "
          f"layout: {chosen} of {planned} states on {', '.join(p.kinds()) or 'none'}; numbers gate: "
          f"{'yes' if p.numbers else 'no'}", flush=True)
    narrowed = (f"layout {chosen} of {planned} states, browser tests {len(p.browser)} of {len(browser)} files, "
                f"numbers gate {'yes' if p.numbers else 'no'}")
    if args.dry_run:
        return 0
    runs = [Pytest("nobrowser", ["-n", str(CPU_WORKERS), "--dist", "loadfile", *cpu], logs, safe_env(HF_NO_CHROME="1"))]
    if p.browser:
        n = min(BROWSER_WORKERS, len(p.browser))
        runs.append(Pytest("browser", [*(["-n", str(n), "--dist", "loadfile"] if n > 1 else []), *sorted(p.browser)],
                           logs, safe_env()))
    # the gates here, on this (main) thread, while the pytest runs go on in their processes: on SIGTERM tests/cdp.py's
    # handler stops the gate's Chromes, then ours unwinds to main()'s `finally`
    gate = run_gate_subset(p, logs) if p.numbers or p.kinds() else {}
    results = [r.wait() for r in runs]
    fails = sum(r["failed"] for r in results)
    for r, run in zip(results, runs):
        print(f"  {r['name']:<10} {r['tests']:>5} tests, {r['failed']} failed, {r['skipped']} skipped in {fmt(r['secs'])}")
        if r.get("why"):
            print(f"    {r['why']}")
        if r["failed"]:
            print("    " + "\n    ".join(run.failures()[:30]))
    lay = gate.get("layout") or layout_summary([])
    if gate:
        print(f"  gates      {layout_line(lay)}; {gate['numbers']} numbers checks ({gate['numbers_fail']} FAIL) "
              f"in {fmt(gate['secs'])}")
        for ln in gate["lines"]:
            if ln.startswith(("FAIL", "SELF", "        ")) or ": got " in ln:
                print("    " + ln[:240])
        if gate["scope"]:
            print(f"  scope      {scope_line(gate['scope'])}")
        if gate["missing"]:
            print(f"  scope      {len(gate['missing'])} WRONG: " + "; ".join(gate["missing"][:12]))
        fails += lay["fail"] + gate["numbers_fail"] + len(gate["missing"])
    print(f"FAST: {sum(r['tests'] for r in results)} tests, {lay['checks']} layout checks, "
          f"{gate.get('numbers', 0)} numbers checks, {fails} FAIL in {fmt(time.monotonic() - t0)}; "
          f"narrowed: {narrowed} (`check.py full` before a task is done)", flush=True)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--watchdog"]:
        sys.exit(watchdog(Path(sys.argv[2])))
    sys.exit(main(sys.argv[1:]))

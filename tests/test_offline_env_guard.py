"""Every from-scratch environment a test or a script builds carries the offline switches (t_73aecf60).

A child process has none of conftest's monkeypatches, only its environment. A child started with an environment built from
scratch that lacks HERMES_PLAID or HERMES_SIMPLEFIN, and that starts the app, really fetches the two vendors' sites on a
background thread (`logos.VENDOR_LOGOS`, read by `logos._vendor_off`): a real outbound call from the test suite, and a race
that made `test_task19_rollover` flaky. This test reads every .py under tests/ and scripts/ (ast only, nothing runs) and
fails on a site that lacks either switch (`offline_env.VENDOR_SWITCHES`).

A *from-scratch env* is one of these, and it is checked when it names an app-environment key (MARKERS: a switch, or the
env, secrets or Alpaca key file the safe environment points at nothing):
  1. a dict display or `dict(...)` call, such as the `env=` of a subprocess call or a helper's return value, unless it
     starts from the parent's environment (a `**` spread, or a positional argument, that mentions `environ` or `safe_env`:
     those inherit conftest's switches);
  2. an `os.environ.update(...)` or `env.update(...)` call (keywords, or a dict display argument) that names a marker;
  3. the `os.environ[NAME] = ...` / `os.environ.setdefault(NAME, ...)` statements of one function (or of the module's top
     level) that name a marker: a script's safe-environment block.
A site has a switch when its literal key is there, or when it spreads / mentions `OFFLINE_SWITCHES` or `VENDOR_SWITCHES`.
A site that deliberately lacks a switch goes on ALLOWLIST with its reason; an entry that no longer matches fails too.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from hermes_finance import logos
from offline_env import OFFLINE_SWITCHES, VENDOR_SWITCHES

ROOT = Path(__file__).resolve().parents[1]
SCAN = ("tests", "scripts")
REQUIRED = tuple(VENDOR_SWITCHES)
MARKERS = {"HERMES_PLAID", "HERMES_SIMPLEFIN", "HERMES_ALPACA", "HERMES_CHROME_DEVTOOLS",
           "HERMES_ENV_FILE", "FINANCE_SECRETS_FILE", "HERMES_ALPACA_KEY_FILE"}
SHARED = {"OFFLINE_SWITCHES", "VENDOR_SWITCHES"}

# (path, scope) -> (the switches deliberately absent there, why)
ALLOWLIST = {
    ("tests/test_doctor.py", "base_env"): (
        {"HERMES_PLAID"}, "`finance doctor` makes no call; its 'ok' (not 'off') plaid status needs the switch unset"),
}


def _spread_inherits(node: ast.AST) -> bool:
    text = ast.unparse(node)
    return "environ" in text or "safe_env" in text


def _provides(node: ast.AST, keys: set[str]) -> set[str]:
    named = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} & SHARED
    return {k for k in REQUIRED if k in keys or named}


def _str_keys(d: ast.Dict) -> set[str]:
    return {k.value for k in d.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _scopes(tree: ast.Module):
    """(name, the nodes directly in that scope): the module's top level, and each function without its nested ones."""
    def direct(body):
        out, stack = [], list(body)
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue    # its own scope
            out.append(n)
            stack.extend(ast.iter_child_nodes(n))
        return out
    yield "<module>", direct(tree.body)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n.name, direct(n.body)


def sites(source: str) -> list[tuple[int, str, set[str]]]:
    """(line, scope, switches the site lacks) for every from-scratch env in `source` that names a marker."""
    tree = ast.parse(source)
    found = []
    update_args: set[int] = set()
    for name, nodes in _scopes(tree):
        sets = [n for n in nodes if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Subscript) and ast.unparse(t.value) == "os.environ" and isinstance(t.slice, ast.Constant)
            for t in n.targets)]
        setd = [n for n in nodes if isinstance(n, ast.Call) and ast.unparse(n.func) == "os.environ.setdefault" and n.args
                and isinstance(n.args[0], ast.Constant)]
        keys = {t.slice.value for n in sets for t in n.targets if isinstance(t, ast.Subscript)}
        keys |= {n.args[0].value for n in setd}
        if keys & MARKERS:
            lines = [n.lineno for n in (*sets, *setd)]
            holder = ast.Module(body=[n for n in nodes if isinstance(n, ast.stmt)], type_ignores=[])
            found.append((min(lines), name, set(REQUIRED) - _provides(holder, keys)))
        for n in nodes:
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "update" \
                    and ast.unparse(n.func.value) in ("os.environ", "env", "environ"):
                keys = {k.arg for k in n.keywords if k.arg}
                for a in n.args:
                    if isinstance(a, ast.Dict):
                        keys |= _str_keys(a)
                        update_args.add(id(a))
                if keys & MARKERS:
                    found.append((n.lineno, name, set(REQUIRED) - _provides(n, keys)))
    for n in ast.walk(tree):
        if isinstance(n, ast.Dict) and id(n) not in update_args:
            keys = _str_keys(n)
            spreads = [v for k, v in zip(n.keys, n.values) if k is None]
            if keys & MARKERS and not any(_spread_inherits(v) for v in spreads):
                found.append((n.lineno, "?", set(REQUIRED) - _provides(n, keys)))
        elif isinstance(n, ast.Call) and ast.unparse(n.func) == "dict":
            keys = {k.arg for k in n.keywords if k.arg}
            if keys & MARKERS and not any(_spread_inherits(a) for a in (*n.args, *(k.value for k in n.keywords if not k.arg))):
                found.append((n.lineno, "?", set(REQUIRED) - _provides(n, keys)))
    return [(line, scope, lacks) for line, scope, lacks in found if lacks]


def _scope_of(source: str, line: int) -> str:
    """The innermost function that holds `line` (for the allowlist), else <module>."""
    best, span = "<module>", None
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno <= line <= (n.end_lineno or n.lineno):
            if span is None or n.end_lineno - n.lineno < span:
                best, span = n.name, n.end_lineno - n.lineno
    return best


def violations() -> tuple[list[str], list[str]]:
    bad, used = [], set()
    for sub in SCAN:
        for path in sorted((ROOT / sub).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            src = path.read_text()
            for line, scope, lacks in sites(src):
                if scope == "?":
                    scope = _scope_of(src, line)
                allowed, _ = ALLOWLIST.get((rel, scope), (set(), ""))
                if allowed:
                    used.add((rel, scope))
                lacks = lacks - allowed
                if lacks:
                    bad.append(f"{rel}:{line} ({scope}) lacks {', '.join(sorted(lacks))}")
    stale = [f"{p} {s}" for (p, s) in ALLOWLIST if (p, s) not in used]
    return bad, stale


def test_every_from_scratch_env_carries_both_vendor_switches():
    bad, _ = violations()
    assert not bad, "a from-scratch env without the offline switches (use tests/offline_env.py):\n" + "\n".join(bad)


def test_the_allowlist_has_no_stale_entry():
    _, stale = violations()
    assert not stale, f"allowlisted sites that no longer lack a switch (or moved): {stale}"


# --- the guard cannot rot into a no-op: it flags bad snippets and passes good ones --------------------------------------

BAD = {
    "dict display": 'import subprocess\nsubprocess.run([], env={"PATH": "/bin", "HERMES_ENV_FILE": "x", "HERMES_ALPACA": "off"})\n',
    "only plaid": 'def f():\n    return {"HERMES_PLAID": "off", "HERMES_ALPACA": "off"}\n',
    "dict()": 'env = dict(PATH="/bin", HERMES_PLAID="off")\n',
    "os.environ.update": 'import os\nos.environ.update({"HERMES_PLAID": "off", "HERMES_ALPACA": "off"})\n',
    "env.update keywords": 'def f(env):\n    env.update(HERMES_PLAID="off", HERMES_SIMPLEFIN="off", HERMES_ENV_FILE="x") or env.update(HERMES_ALPACA="off")\n',
    "os.environ block": 'import os\nos.environ["HERMES_PLAID"] = "off"\nos.environ["HERMES_ALPACA"] = "off"\n',
    "spread of something else": 'def f(x):\n    return {**x, "HERMES_ENV_FILE": "y", "HERMES_PLAID": "off"}\n',
}
GOOD = {
    "both keys": 'x = {"HERMES_PLAID": "off", "HERMES_SIMPLEFIN": "off", "HERMES_ENV_FILE": "y"}\n',
    "shared constant": 'from offline_env import OFFLINE_SWITCHES\nx = {"PATH": "/bin", **OFFLINE_SWITCHES, "HERMES_ENV_FILE": "y"}\n',
    "inherits environ": 'import os\nx = {**os.environ, "HERMES_ENV_FILE": "y"}\ny = dict(os.environ, HERMES_ENV_FILE="y")\n',
    "inherits safe_env": 'x = {**check.safe_env(), "HERMES_ENV_FILE": "y"}\n',
    "update with constant": 'import os\nos.environ.update(OFFLINE_SWITCHES)\nos.environ.update(HERMES_ENV_FILE="y", **VENDOR_SWITCHES)\n',
    "os.environ block with both": 'import os\nos.environ["HERMES_PLAID"] = "off"\nos.environ["HERMES_SIMPLEFIN"] = "off"\n',
    "no marker": 'x = {"PATH": "/bin", "FINANCE_DATA_DIR": "d"}\nimport os\nos.environ["TZ"] = "UTC"\n',
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_the_scan_flags_a_bad_snippet(name):
    assert sites(BAD[name]), name


@pytest.mark.parametrize("name", sorted(GOOD))
def test_the_scan_passes_a_good_snippet(name):
    assert sites(GOOD[name]) == [], name


def test_the_scan_reads_the_files_it_is_meant_to():
    files = [p for sub in SCAN for p in (ROOT / sub).rglob("*.py")]
    assert len(files) > 100 and ROOT / "tests/conftest.py" in files and ROOT / "scripts/check.py" in files


# --- the shared list stays in step with the code and with conftest ----------------------------------------------------

def test_the_vendor_switches_are_every_switch_the_vendor_logos_read():
    assert {v[2] for v in logos.VENDOR_LOGOS.values()} == set(VENDOR_SWITCHES)


def test_the_shared_switches_are_off_in_conftests_environment():
    for k, v in OFFLINE_SWITCHES.items():
        assert os.environ.get(k) == v, k

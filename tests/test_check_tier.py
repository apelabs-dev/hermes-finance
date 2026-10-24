"""Release-tier policy without importing or launching a browser."""
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check
from tier_map import ALWAYS_ON, TIER_MAP, select_tests


@pytest.fixture
def sources():
    return {
        "tests/test_money_text.py": "pass",
        "tests/test_money_numbers.py": "pass",
        "tests/test_text_density.py": "pass",
        "tests/test_secrets.py": "pass",
        "tests/test_gate_wiring.py": "pass",
        "tests/test_new_wiring.py": "from audit_layout import STATES",
        "tests/test_number_wiring.py": "from audit_numbers import check_settings",
        "tests/test_unrelated.py": "pass",
        "tests/test_browser.py": "pass",
    }


def test_mapped_source_uses_coverage_table(sources):
    selected, fallback = select_tests(["src/hermes_finance/money.py"], sources, [])
    assert not fallback
    assert "mapped from src/hermes_finance/money.py" in selected["tests/test_money_text.py"]
    assert "tests/test_unrelated.py" not in selected


def test_always_on_including_new_audit_wiring_even_with_empty_diff(sources):
    selected, fallback = select_tests([], sources, [])
    assert not fallback
    for path in ("tests/test_text_density.py", "tests/test_secrets.py", "tests/test_gate_wiring.py",
                 "tests/test_new_wiring.py", "tests/test_number_wiring.py"):
        assert selected[path] == ["always-on"]


def test_always_on_wiring_follows_audit_imports_through_helpers(sources):
    sources["tests/wiring_helper.py"] = "from audit_layout import STATES"
    sources["tests/test_indirect_wiring.py"] = "from wiring_helper import STATES"
    selected, fallback = select_tests([], sources, [])
    assert not fallback
    assert selected["tests/test_indirect_wiring.py"] == ["always-on"]


@pytest.mark.parametrize("path", ["docs/new.md", "src/hermes_finance/new.py", "tests/unknown_helper.py"])
def test_unmapped_file_falls_back_even_if_a_test_reads_it(sources, path):
    sources["tests/test_reader.py"] = f"PATH = {path!r}"
    selected, fallback = select_tests([path], sources, [])
    assert fallback == [f"unmapped changed file: {path}"]
    assert set(selected) == {p for p in sources if p.startswith("tests/test_")}
    assert any(r.startswith("fallback:") for r in selected["tests/test_unrelated.py"])


@pytest.mark.parametrize("path", ["scripts/check.py", "scripts/new_helper.sh", "scripts/nested/new.py",
                                 "scripts/audit_layout.py", "scripts/audit_numbers.py", "scripts/tier_map.py",
                                 "tests/conftest.py", "tests/cdp.py", "tests/gate_dbs.py"])
def test_gate_infrastructure_and_mapping_changes_fall_back(sources, path):
    selected, fallback = select_tests([path], sources, [])
    assert fallback == [f"protected gate infrastructure: {path}"]
    assert any(r.startswith("fallback:") for r in selected["tests/test_unrelated.py"])


@pytest.mark.parametrize("path", ["static/app.css", "static/new.js", "src/hermes_finance/static/app.css",
                                 "src/hermes_finance/static/settings.js", "src/hermes_finance/static/deleted.css"])
def test_static_files_select_all_browser_files_and_money(sources, path):
    browser = ["tests/test_browser.py", "tests/test_new_wiring.py"]
    selected, fallback = select_tests([path], sources, browser)
    assert not fallback
    for test in [*browser, "tests/test_money_numbers.py"]:
        assert f"mapped from {path}" in selected[test]


def test_readers_importers_and_helper_importers_are_added(sources):
    sources.update({
        "src/hermes_finance/money.py": "pass",
        "tests/helper.py": "from hermes_finance.money import whole",
        "tests/test_direct.py": "import hermes_finance.money as money",
        "tests/test_from.py": "from hermes_finance import money",
        "tests/test_indirect.py": "from helper import thing",
        "tests/test_reader.py": "DATA = (ROOT / 'src' / 'hermes_finance' / 'money.py').read_bytes()",
        "tests/test_similar.py": "from hermes_finance import moneybox",
    })
    selected, fallback = select_tests(["src/hermes_finance/money.py"], sources, [])
    assert not fallback
    for suffix in ("direct", "from", "indirect", "reader"):
        assert any(r.startswith("reads/imports") for r in selected[f"tests/test_{suffix}.py"])
    assert "tests/test_similar.py" not in selected


def test_changed_test_and_its_importers_are_selected(sources):
    sources["tests/test_importer.py"] = "from test_unrelated import fixture"
    selected, fallback = select_tests(["tests/test_unrelated.py"], sources, [])
    assert not fallback
    assert "tests/test_unrelated.py" in selected and "tests/test_importer.py" in selected


def test_bare_invocation_is_full_and_explicit_modes_still_work():
    assert check.parse_args([]).mode == "full"
    assert check.parse_args(["fast"]).mode == "fast"
    assert check.parse_args(["full"]).mode == "full"
    assert check.parse_args(["--tier", "ship"]).mode == "full"
    assert check.parse_args(["--tier", "ship", "--part", "2", "--kinds", "features"]).part == 2


@pytest.mark.parametrize("argv", [["fast", "--tier", "ship"], ["full", "--tier", "fast"],
                                 ["--tier", "fast", "--part", "1"], ["--tier", "fast", "--kinds", "full"]])
def test_conflicting_or_incomplete_tiers_are_rejected(argv):
    with pytest.raises(SystemExit) as error:
        check.parse_args(argv)
    assert error.value.code == 2


def test_tier_uses_exact_two_dot_diff(monkeypatch):
    calls = []
    def git(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "src/hermes_finance/money.py\n")
    monkeypatch.setattr(check.subprocess, "run", git)
    assert check.tier_changed_files("flybudget-polish") == ["src/hermes_finance/money.py"]
    assert calls == [["git", "-C", str(ROOT), "diff", "--name-only", "flybudget-polish..HEAD"]]


def test_real_static_selection_includes_every_browser_and_guard_file():
    cpu, browser = check.test_files()
    selected, fallback = check.tier_selection(["src/hermes_finance/static/app.css"], browser)
    assert not fallback
    # every always-on guard this checkout has (the public release leaves the dev-only lane wrappers' tests out)
    assert set(browser) | {t for t in ALWAYS_ON if t in set(cpu) | set(browser)} <= set(selected)
    assert "tests/test_money_numbers.py" in selected
    assert "tests/test_gates.py" not in selected  # the aggregate full-grid driver remains ship acceptance
    assert "tests/test_gate_wiring.py" in browser


def test_every_mapping_target_exists():
    tests = [p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").glob("test_*.py")]
    import fnmatch
    for source, targets in TIER_MAP.items():
        for target in targets:
            assert target.startswith("@") or any(fnmatch.fnmatchcase(t, target) for t in tests), (source, target)


def test_ship_and_bare_dispatch_to_existing_full_runner(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(check, "_run_existing", lambda args, *rest: calls.append((args.mode, args.part, rest)) or 7)
    for argv in ([], ["full"], ["--tier", "ship"]):
        args = check.parse_args(argv)
        assert check._run(args, [], ["unit"], ["browser"], tmp_path, 0) == 7
    assert calls[0] == calls[1] == calls[2]


def test_fast_fallback_dispatches_both_full_parts_and_lists_reason(monkeypatch, tmp_path, capsys):
    args = check.parse_args(["--tier", "fast", "--files", "docs/new.md"])
    monkeypatch.setattr(check, "tier_selection", lambda *a: ({"tests/test_x.py": ["fallback: unmapped docs/new.md"]},
                                                          ["unmapped docs/new.md"]))
    calls = []
    monkeypatch.setattr(check, "_run_existing", lambda args, *rest: calls.append((args.mode, args.part)) or 3)
    assert check._run(args, [], [], [], tmp_path, 0) == 3
    assert calls == [("full", None)]
    assert "tests/test_x.py: fallback: unmapped docs/new.md" in capsys.readouterr().out


def test_fast_runs_per_file_and_stops_at_failure(monkeypatch, tmp_path):
    args = check.parse_args(["--tier", "fast", "--files"])
    monkeypatch.setattr(check, "tier_selection", lambda *a: ({"tests/test_b.py": ["always-on"],
                                                          "tests/test_text_density.py": ["always-on"]}, []))
    calls = []
    class FailingRun:
        def __init__(self, name, argv, logs, env):
            calls.append((argv, env))
        def wait(self):
            return {"tests": 1, "failed": 1, "secs": 0}
        def failures(self):
            return ["FAILED synthetic"]
    monkeypatch.setattr(check, "Pytest", FailingRun)
    assert check._run(args, [], [], ["tests/test_text_density.py"], tmp_path, time.monotonic()) == 1
    assert len(calls) == 1
    assert calls[0][0] == ["--maxfail=1", "tests/test_text_density.py"]
    assert calls[0][1]["HERMES_PLAID"] == "off"


@pytest.mark.parametrize("case", ["selection", "fallback", "failure"])
def test_fast_cli_final_line_even_after_cleanup_or_failure(tmp_path, case):
    env = check.safe_env(TMPDIR=str(tmp_path), PYTHONPATH=str(ROOT / "scripts"))
    if case == "failure":
        code = "import check; check._run = lambda *a: 1; raise SystemExit(check.main(['--tier', 'fast']))"
        command = [sys.executable, "-c", code]
    else:
        path = "src/hermes_finance/static/app.css" if case == "selection" else "docs/unmapped.md"
        command = [sys.executable, str(ROOT / "scripts/check.py"), "--tier", "fast", "--dry-run", "--files", path]
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == (1 if case == "failure" else 0), result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == "TIER fast (NOT a ship verdict)"

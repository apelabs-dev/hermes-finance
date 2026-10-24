"""File-level release-tier policy. RULES in check.py still owns legacy fast's gate states.

This is the ONE source-to-test table for --tier fast. Entries are additive, not
first-match. @browser means check.py's existing browser classification (excluding
the full acceptance driver test_gates.py); @self means the changed test itself.
New source files must acquire coverage here or deliberately take the full gate.
"""
from __future__ import annotations

import ast
import fnmatch
from pathlib import PurePosixPath


TIER_MAP = {
    "static/*.css": ("@browser", "tests/test_money_numbers.py"),
    "static/*.js": ("@browser", "tests/test_money_numbers.py"),
    "src/hermes_finance/static/*.css": ("@browser", "tests/test_money_numbers.py"),
    "src/hermes_finance/static/*.js": ("@browser", "tests/test_money_numbers.py"),
    "src/hermes_finance/access.py": ("tests/test_access_task18.py", "tests/test_tailnet.py"),
    "src/hermes_finance/admin.py": ("tests/test_settings_task18.py", "tests/test_backup_restore.py"),
    "src/hermes_finance/ai.py": ("tests/test_ai.py", "tests/test_chat*.py"),
    "src/hermes_finance/alpaca.py": ("tests/test_task19_alpaca.py", "tests/test_task20_merge.py"),
    "src/hermes_finance/api.py": ("tests/test_web.py", "tests/test_dashboard.py"),
    "src/hermes_finance/auth.py": ("tests/test_auth_task18.py",),
    "src/hermes_finance/backup.py": ("tests/test_backup_restore.py", "tests/test_settings_task18.py"),
    "src/hermes_finance/billcal.py": ("tests/test_task19_billcal.py", "tests/test_billcal_fit.py"),
    "src/hermes_finance/bills.py": ("tests/test_task21_bills.py", "tests/test_recurring_status_fit.py"),
    "src/hermes_finance/bridge.py": ("tests/test_bridge*.py",),
    "src/hermes_finance/cdp_client.py": ("tests/test_valuation*.py",),
    "src/hermes_finance/chat.py": ("tests/test_chat*.py", "tests/test_browser_task23.py"),
    "src/hermes_finance/classify.py": ("tests/test_classify.py", "tests/test_review.py", "tests/test_task24*.py"),
    "src/hermes_finance/cli.py": ("tests/test_web.py", "tests/test_reads.py", "tests/test_query.py", "tests/test_doctor.py",
                                  "tests/test_backup_restore.py"),
    "src/hermes_finance/config.py": ("tests/test_config_plaid.py", "tests/test_settings_task18.py"),
    "src/hermes_finance/cronpipe.py": ("tests/test_cronpipe.py", "tests/test_plugin_crons.py"),
    "src/hermes_finance/db.py": ("tests/test_schema*.py", "tests/test_task19_schema.py"),
    "src/hermes_finance/doctor.py": ("tests/test_doctor.py",),
    "src/hermes_finance/features.py": ("tests/test_dashboard.py", "tests/test_settings_task18.py"),
    "src/hermes_finance/goals.py": ("tests/test_task19_goals.py",),
    "src/hermes_finance/importer.py": ("tests/test_sync.py", "tests/test_import_task18.py"),
    "src/hermes_finance/importfile.py": ("tests/test_importfile.py", "tests/test_import_task18.py"),
    "src/hermes_finance/insights.py": ("tests/test_insights.py", "tests/test_browser_task11.py"),
    "src/hermes_finance/link.py": ("tests/test_link*.py", "tests/test_browser_relink_ui.py"),
    "src/hermes_finance/logos.py": ("tests/test_logos.py", "tests/test_task22e_logo_retry.py"),
    "src/hermes_finance/manual.py": ("tests/test_manual.py",),
    "src/hermes_finance/mcp_server.py": ("tests/test_mcp_task17.py", "tests/test_tools_task17.py"),
    "src/hermes_finance/mcp_state.py": ("tests/test_mcp_task17.py",),
    "src/hermes_finance/money.py": ("tests/test_money*.py",),
    "src/hermes_finance/photos.py": ("tests/test_valuation_task13b.py", "tests/test_browser_task13b.py"),
    "src/hermes_finance/plaid.py": ("tests/test_sync.py", "tests/test_link.py", "tests/test_config_plaid.py"),
    "src/hermes_finance/portfolio.py": ("tests/test_portfolio.py",),
    "src/hermes_finance/prefs.py": ("tests/test_settings_task18.py",),
    "src/hermes_finance/qr.py": ("tests/test_qr.py",),
    "src/hermes_finance/reads.py": ("tests/test_reads.py", "tests/test_query.py", "tests/test_tools_task17.py"),
    "src/hermes_finance/redact.py": ("tests/test_secrets.py",),
    "src/hermes_finance/report.py": ("tests/test_query.py", "tests/test_reads.py"),
    "src/hermes_finance/rollover.py": ("tests/test_task19_rollover.py",),
    "src/hermes_finance/rulebook.py": ("tests/test_task24*.py", "tests/test_browser_task24*.py"),
    "src/hermes_finance/secretstore.py": ("tests/test_secrets.py", "tests/test_settings_task18.py"),
    "src/hermes_finance/sync.py": ("tests/test_sync.py", "tests/test_pending_reconciliation.py"),
    "src/hermes_finance/syncnow.py": ("tests/test_task22*.py",),
    "src/hermes_finance/tooldefs.py": ("tests/test_tools_task17.py",),
    "src/hermes_finance/transit.py": ("tests/test_bridge*.py",),
    "src/hermes_finance/txmeta.py": ("tests/test_task19_txmeta.py",),
    "src/hermes_finance/usage.py": ("tests/test_settings_task18.py",),
    "src/hermes_finance/valuation.py": ("tests/test_valuation*.py",),
    "src/hermes_finance/web.py": ("tests/test_web.py", "tests/test_dashboard.py", "@browser"),
    "tests/test_*.py": ("@self",),
}

# Entire files: these include repository scans and guard tests embedded in larger
# feature suites. Keep the guard even when that feature's source did not change.
ALWAYS_ON = frozenset({
    "tests/test_dehardcode_task18.py", "tests/test_secrets.py",
    "tests/test_dashboard.py", "tests/test_valuation.py", "tests/test_networth_task15.py",
    "tests/test_plugin_crons.py", "tests/test_tools_task17.py", "tests/test_task20_merge.py",
    "tests/test_task23_wrappers.py", "tests/test_task24_wrappers.py",
    "tests/test_check_plan.py", "tests/test_check_log_retention.py", "tests/test_check_tier.py",
    "tests/test_gate_fixture_reachability.py", "tests/test_gate_wiring.py",
    "tests/test_text_density.py", "tests/test_doctor.py",
})
HELPER_STOP = frozenset({"tests/conftest.py", "tests/cdp.py", "scripts/check.py"})
FORCE_FULL = ("scripts/*", "tests/conftest.py", "tests/cdp.py", "tests/gate_dbs.py")


def _module(path: str) -> str:
    path = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    return path.removesuffix(".__init__")


def dependencies(source: str, paths: set[str]) -> set[str]:
    """Conservative static imports and file reads, without importing app/browser code.

    String basenames also cover Path chains, web.STATIC_DIR, open/read_bytes,
    importlib and fixture paths. Helpers and app imports are followed transitively.
    False positives cost time; they cannot drop coverage.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(paths)  # malformed Python must not silently lose its importers
    imports, strings = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            imports.add(base)
            imports.update(f"{base}.{a.name}".strip(".") for a in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.add(node.value)
    # Index names once, rather than comparing every long JS/source literal with
    # every path (selection itself must not consume the fast tier's time budget).
    names: dict[str, set[str]] = {}
    for path in paths:
        for name in (path, PurePosixPath(path).name, PurePosixPath(path).stem, _module(path)):
            names.setdefault(name, set()).add(path)
    found = set()
    for name in imports:
        found.update(names.get(name, ()))
    for literal in strings:
        found.update(names.get(literal, ()))
        found.update(names.get(literal.rsplit("/", 1)[-1], ()))
    return found


def select_tests(files: list[str], sources: dict[str, str], browser: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Pure (test path -> reasons, full-fallback reasons); no git, IO or browser.

    sources contains repository Python source, including tests/helpers. An imported
    or read file adds coverage but does NOT excuse an unmapped changed path.
    """
    audits = {"scripts/audit_layout.py", "scripts/audit_numbers.py", "scripts/audit_controls.py"}
    paths = set(sources) | set(files) | audits
    tests = {p for p in sources if p.startswith("tests/test_") and p.endswith(".py")}
    selected: dict[str, list[str]] = {}
    fallback = []

    def add(test, reason):
        selected.setdefault(test, []).append(reason)

    graph = {p: dependencies(s, paths) for p, s in sources.items()}
    wiring = set(audits)
    while True:
        # Walk only through helpers (scripts/ and non-test tests/ modules). Walking through
        # src/ modules or cdp/conftest/check would make every importer of web.py "wiring".
        more = {p for p, deps in graph.items() if deps & wiring and not p.startswith("src/")
                and p not in HELPER_STOP and (p in tests or not p.startswith("tests/test_"))} - wiring
        if not more:
            break
        wiring.update(more)
    for test in sorted(tests):
        # Gate-wiring coverage is derived from source, so a new wiring test is
        # always-on automatically. The aggregate acceptance driver is ship-only.
        if test in ALWAYS_ON or (test != "tests/test_gates.py" and (test in wiring or
                                  any(n in sources[test] for n in ("audit_layout", "audit_numbers", "audit_controls")))):
            add(test, "always-on")
    for raw in sorted(set(files)):
        path = PurePosixPath(raw).as_posix()
        if any(fnmatch.fnmatchcase(path, pat) for pat in FORCE_FULL):
            fallback.append(f"protected gate infrastructure: {path}")
            continue
        matches = [targets for pat, targets in TIER_MAP.items() if fnmatch.fnmatchcase(path, pat)]
        if not matches:
            fallback.append(f"unmapped changed file: {path}")
        for targets in matches:
            for target in targets:
                hits = set(browser) if target == "@browser" else {path} & tests if target == "@self" else {
                    t for t in tests if fnmatch.fnmatchcase(t, target)}
                if not hits and target != "@browser":
                    fallback.append(f"mapping has no test files: {path} -> {target}")
                for test in sorted(hits):
                    add(test, f"mapped from {path}")
        affected = {path}
        # An asset (css/js/json...) is served by src modules that every importer of web.py
        # "reaches"; that is not a read. Only tests and their helpers count as readers of an
        # asset, so a static change doesn't select the whole non-browser suite. The browser
        # tests that load the app are selected by the explicit TIER_MAP entry (@browser).
        asset = not path.endswith(".py")
        while True:
            more = {p for p, deps in graph.items() if deps & affected and not (asset and p.startswith("src/"))} - affected
            if not more:
                break
            affected.update(more)
        for test in sorted(tests & affected):
            # test_files() deliberately separates the aggregate full-grid gate
            # from browser files. Importing cdp.find_chrome also exposes Server's
            # lazy web import, but does not make this driver a CSS reader. Direct
            # reads/imports (or changing the driver itself) still select it.
            if test == "tests/test_gates.py" and path != test and path not in graph[test]:
                continue
            add(test, f"reads/imports {path} (directly or through helpers)")
    if fallback:
        for test in sorted(tests):
            add(test, "fallback: " + "; ".join(fallback))
    return dict(sorted(selected.items())), fallback

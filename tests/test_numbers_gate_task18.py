"""TASK-18: the numbers gate checks the Settings page's figures (scripts/audit_numbers.py check_settings), and each
check fails when its figure is wrong: the Plaid slots, a value source's calls / cap and meter, a switch the env file
sets, and the export's transaction count. No Chrome: the page's DOM reading and the API answers are made up here."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_numbers as an  # noqa: E402

API = {
    "/api/status": {"slots_used": 3, "slot_limit": 10},
    "/api/settings/page": {"valuations": [
        {"key": "carfax", "used": 2, "cap": 6, "on": True, "env_flag": "CARFAX_ENABLED"},
        {"key": "redfin", "used": 2, "cap": 20, "on": False, "env_flag": None},
        {"key": "photos", "used": 0, "cap": 60, "on": True, "env_flag": None}]},
    "/api/transactions?kind=all&limit=1": {"total": 1148},
}


def source(name, used, cap, on, locked=False, flag=None):
    return {"name": name, "use": f"Used this month {used} / {cap}", "bar": {"now": str(used), "max": str(cap), "fill": used / cap},
            "on": on, "locked": locked, "text": name + (f"Set by {flag} in your env file" if flag else "")}


DOM = {
    "slotn": "3/10", "slotline": "3 of 10 used · removing a bank doesn't free its slot",
    "slotbar": {"now": "3", "max": "10", "fill": 0.3},
    "sources": [source("Carfax", 2, 6, "true", True, "CARFAX_ENABLED"), source("Redfin", 2, 20, "false"), source("Photos", 0, 60, "true")],
    "exported": "Export: every transaction (1,148) as a spreadsheet, with its category and notes.",
}


class FakeSite:
    def get(self, path, **params):
        return API[path + ("?" + an.urlencode(params) if params else "")]


def run(dom):
    A = an.Audit(lambda line: None)
    an.check_settings(A, FakeSite(), dom, None)
    return A


def test_the_settings_checks_pass_on_the_right_figures():
    A = run(DOM)
    assert A.fails() == [] and len(A.results) == 26


def _wrong(path, value):
    dom = copy.deepcopy(DOM)
    *keys, last = path
    d = dom
    for k in keys:
        d = d[k]
    d[last] = value
    return dom


@pytest.mark.parametrize("path,value,check", [
    (("slotn",), "4/10", "Settings 'N/10' slots"),
    (("slotline",), "4 of 10 used", "Settings 'N of 10 used'"),
    (("slotbar", "fill"), 0.5, "Settings slot meter fill"),
    (("sources", 0, "use"), "Used this month 3 / 6", "Carfax: calls this month"),
    (("sources", 1, "use"), "Used this month 2 / 25", "Redfin: cap"),
    (("sources", 2, "use"), "Used 0 of 60", "Photos: 'Used this month N / cap' is shown"),
    (("sources", 1, "bar", "fill"), 0.9, "Redfin: meter fill"),
    (("sources", 0, "locked"), False, "Carfax: set in the env file"),
    (("sources", 0, "on"), "false", "Carfax: switch = the env file's value"),
    (("sources", 1, "on"), "true", "Redfin: switch = /api/settings/page"),
    (("sources", 2, "name"), "Pictures", "Settings shows every value source"),
    (("exported",), "Export: every transaction (1,149) as a spreadsheet", "export count = Transactions page"),
])
def test_each_settings_check_fails_on_a_wrong_figure(path, value, check):
    fails = [f["check"] for f in run(_wrong(path, value)).fails()]
    assert any(f.startswith(check) for f in fails), fails

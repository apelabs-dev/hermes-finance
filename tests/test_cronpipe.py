"""hermes_finance.cronpipe: the pre-run steps the plugin's entry scripts run before each daily / weekly report job
(plugin-scope.md section 5, group 7, pipeline half).

Parity with the deployed pre-run scripts: the daily run syncs once, snapshots Alpaca, matches loan payments, lists card
autopay suggestions (saving none), revalues cars and homes, queues at most 120 merchant groups for classification and
collects the alerts; the weekly run syncs once (the existing extra weekly sync, and no other) and saves the overview,
budgets and recurring snapshots. Everything runs against a synthetic database and the mocked Plaid transport
(tests/conftest.py); artifacts are private; a failed or stale step says so."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import re
import stat
from datetime import date, timedelta
from pathlib import Path

import pytest

from conftest import TOKEN_B, add_item, err, page
from factory import acct, bank, cap, tx
from hermes_finance import classify, cli, config, db, prefs, tooldefs

TODAY = date.today()
DAILY_SECTIONS = ["date", "settings", "sync", "alpaca", "loan payments", "card autopay suggestions",
                  "car / property estimates", "classification queue", "alerts", "status"]
WEEKLY_SECTIONS = ["date", "settings", "sync", "snapshots", "status"]


def cronpipe():
    return importlib.import_module("hermes_finance.cronpipe")


def inprocess(args: list[str], timeout: float) -> tuple[int, str]:
    """The pipeline's runner, in this process: `finance <args>` through cli.main, so the mocked Plaid client (the
    cli_client fixture) answers the sync."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = cli.main(list(args))
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
    return rc or 0, buf.getvalue()


class Recorder:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        return inprocess(args, timeout)


def run(role: str, artifacts: Path, runner=None) -> tuple[str, dict]:
    out = io.StringIO()
    report = cronpipe().run(role, artifacts, runner=runner or inprocess, today=TODAY, out=out)
    return out.getvalue(), report


def sections(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"^== ([a-z /]+?)(?::|$| \()", text, re.M)]


def once_per_bank(fake) -> bool:
    """At most one /transactions/sync per bank (access token) in what the mock recorded, and at least one."""
    tokens = [b["access_token"] for b in fake.bodies("/transactions/sync")]
    return bool(tokens) and len(tokens) == len(set(tokens))


def day(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


BROKEN_TOKEN = "access-production-item_b-0000"   # factory.bank's token for item_b


@pytest.fixture
def ledger(conn, fake):
    """One healthy bank (item_a, with the mocked Plaid token) and its accounts; this month's spending puts Groceries at
    85% of its cap and Other Shopping over its cap; a second bank needs a re-login (and its sync keeps saying so)."""
    fake.broken = {BROKEN_TOKEN}
    fake.on("/transactions/sync", lambda b: err("ITEM_LOGIN_REQUIRED") if b["access_token"] in fake.broken else page())
    add_item(conn)
    acct(conn)
    acct(conn, "acc_cc", name="Card", type_="credit", subtype="credit card", current=250.0, limit=5000.0, mask="9999")
    bank(conn, "item_b", name="Credit Union", status="login_required", error_code="ITEM_LOGIN_REQUIRED")
    first = TODAY.replace(day=1).isoformat()
    tx(conn, first, 85.0, "Safeway", cat="Groceries")
    tx(conn, first, 80.0, "Target", cat="Other Shopping")
    cap(conn, "Groceries", 100.0)
    cap(conn, "Other Shopping", 50.0)
    classify.apply(conn)
    return conn


@pytest.fixture
def artifacts(tmp_path):
    return tmp_path / "hermes-home" / "cache" / "hermes-finance"


def test_the_daily_run_takes_each_step_once_in_the_deployed_order(ledger, artifacts, fake, cli_client):
    rec = Recorder()
    text, report = run("daily", artifacts, rec)
    assert sections(text) == DAILY_SECTIONS, text
    assert rec.calls == [["sync"], ["alpaca-sync"], ["match-payments"], ["match-payments", "--suggest"], ["revalue"],
                         ["ai-queue", "--max-groups", "120"]]
    assert once_per_bank(fake)                                            # one sync per bank: no second pass
    assert not any("refresh" in path for path in fake.paths())             # never Plaid's billed refresh
    assert report["steps"]["sync"] == "partial"                            # item_b needs a re-login


def test_a_paid_plan_with_refresh_on_still_never_refreshes(ledger, artifacts, fake, cli_client):
    prefs.save(ledger, {"plaid.plan": "paid", "plaid.on_demand_refresh": True})
    run("daily", artifacts)
    assert once_per_bank(fake)
    fake.calls.clear()
    run("weekly", artifacts)
    assert once_per_bank(fake)
    assert not any("refresh" in path for path in fake.paths())


def test_the_weekly_run_syncs_once_and_saves_three_private_snapshots(ledger, artifacts, fake, cli_client):
    rec = Recorder()
    text, report = run("weekly", artifacts, rec)
    assert sections(text) == WEEKLY_SECTIONS, text
    assert rec.calls == [["sync"]]
    assert once_per_bank(fake)
    for name in ("overview", "budgets", "recurring"):
        path = artifacts / f"week-{name}-{TODAY.isoformat()}.json"
        assert f"{name}_file={path}" in text
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        json.loads(path.read_text())
    assert stat.S_IMODE(artifacts.stat().st_mode) == 0o700
    assert report["steps"]["snapshots"] == "ok"
    assert report["status"] == "partial" and report["failed"] == ["sync"]   # item_b needs a re-login: said so


def test_the_queue_holds_at_most_120_merchant_groups_and_stays_out_of_the_output(ledger, artifacts, fake, cli_client):
    for i in range(130):
        tx(ledger, day(40 + i % 20), 3.0 + i, f"Synthetic Merchant {i:03d}", cat="Other Shopping")
    classify.apply(ledger)
    text, report = run("daily", artifacts)
    path = artifacts / f"queue-{TODAY.isoformat()}.json"
    queue = json.loads(path.read_text())
    assert len(queue["groups"]) == 120 and queue["truncated"] is True
    assert f"queue_file={path}" in text and "groups=120" in text and "truncated=True" in text
    assert "Synthetic Merchant" not in text                                # counts and paths only, as deployed
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(artifacts.stat().st_mode) == 0o700
    assert f"decisions_file={artifacts / f'decisions-{TODAY.isoformat()}.json'}" in text
    assert "ai-apply" in text


@pytest.mark.parametrize("off", [(), ("alerts.cap_80",), ("alerts.over_cap",), ("alerts.broken_connection",),
                                 ("alerts.cap_80", "alerts.over_cap", "alerts.broken_connection")])
def test_alerts_follow_the_three_switches_and_nothing_else(ledger, artifacts, fake, cli_client, off):
    if off:
        prefs.save(ledger, {k: False for k in off})
    text, _ = run("daily", artifacts)
    saved = json.loads((artifacts / f"alerts-{TODAY.isoformat()}.json").read_text())
    kinds = sorted((a["kind"], a["level"]) for a in saved["alerts"])
    expected = sorted(k for k, switch in ((("cap", "warn"), "alerts.cap_80"), (("cap", "over"), "alerts.over_cap"),
                                          (("connection", "error"), "alerts.broken_connection")) if switch not in off)
    assert kinds == expected
    assert saved["suppressed_by_settings"] == 3 - len(expected)
    assert f"alerts={len(expected)}" in text
    assert {a["kind"] for a in saved["alerts"]} <= {"cap", "connection"}


def test_switched_off_integrations_are_skipped_and_said_so(ledger, artifacts, fake, cli_client):
    prefs.save(ledger, {"ai.mode": "off", "valuations.carfax": False, "valuations.redfin": False})
    runs_before = ledger.execute("SELECT COUNT(*) FROM ai_runs").fetchone()[0]
    text, report = run("daily", artifacts)
    assert "AI is off" in text and not (artifacts / f"queue-{TODAY.isoformat()}.json").exists()
    assert ledger.execute("SELECT COUNT(*) FROM ai_runs").fetchone()[0] == runs_before
    assert report["steps"]["classification queue"] == "skipped"
    assert "ai off" in text.split("== settings", 1)[1].splitlines()[0]
    assert "carfax off" in text and "redfin off" in text
    assert report["steps"]["alpaca"] in ("ok", "skipped")                  # no key file (tests): nothing to do


def test_a_partial_sync_is_named_partial(ledger, artifacts, fake, cli_client):
    add_item(ledger, "item_c", institution_id="ins_3", name="Other Bank", token=TOKEN_B)
    fake.broken.add(TOKEN_B)
    text, report = run("daily", artifacts)
    assert report["steps"]["sync"] == "partial" and "sync" in report["failed"]
    assert re.search(r"^== sync: partial \(1 of \d+ ", text, re.M), text
    assert report["status"] == "partial"
    assert text.rstrip().splitlines()[-1].startswith("== status: partial")


def test_a_failed_step_renames_todays_earlier_file_stale_and_says_so(ledger, artifacts, fake, cli_client, monkeypatch):
    artifacts.mkdir(parents=True)
    earlier = artifacts / f"alerts-{TODAY.isoformat()}.json"
    earlier.write_text('{"from": "an earlier run today"}')
    older = artifacts / f"alerts-{day(1)}.json"
    older.write_text('{"from": "yesterday"}')
    real = tooldefs.read_connection
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] > 1:        # the settings read works; the alerts read fails
            raise tooldefs.ToolError("database is locked")
        return real(path)

    monkeypatch.setattr(tooldefs, "read_connection", flaky)
    text, report = run("daily", artifacts)
    assert report["steps"]["alerts"] == "failed" and report["status"] == "partial"
    assert "== alerts: FAILED (database is locked)" in text
    assert not earlier.exists() and (artifacts / f"alerts-{TODAY.isoformat()}.stale.json").exists()
    assert "stale" in text and "not from this run" in text
    assert older.read_text() == '{"from": "yesterday"}'                   # other days' files: left alone


def test_a_run_out_of_time_stops_starting_steps_and_says_so(ledger, artifacts, fake, cli_client):
    """Hermes kills a pre-run script after an hour with no output at all; the run keeps within its budget instead,
    each step's timeout capped by what's left, and names what it didn't run."""
    now, timeouts = [0.0], []

    def slow(args, timeout):
        timeouts.append(timeout)
        now[0] += 1400.0
        return inprocess(args, timeout)

    out = io.StringIO()
    report = cronpipe().run("daily", artifacts, runner=slow, today=TODAY, out=out, clock=lambda: now[0])
    text = out.getvalue()
    assert timeouts[:2] == [cronpipe().STEP_TIMEOUT] * 2 and timeouts[2] == cronpipe().BUDGET - 2800
    for name in ("card autopay suggestions", "car / property estimates", "classification queue"):
        assert report["steps"][name] == "failed" and f"== {name}: FAILED (not run: out of time" in text
    assert report["steps"]["alerts"] == "ok" and text.rstrip().splitlines()[-1].startswith("== status: partial")


def test_a_missing_database_is_an_error_not_a_new_empty_database(data_dir, artifacts):
    rec = Recorder()
    text, report = run("daily", artifacts, rec)
    assert "PRE-RUN ERROR" in text and str(config.db_path()) in text
    assert report["status"] == "failed" and rec.calls == []
    assert not config.db_path().exists() and not artifacts.exists()


def test_the_output_never_prints_a_token(ledger, artifacts, fake, cli_client):
    text, _ = run("daily", artifacts)
    tokens = [r[0] for r in ledger.execute("SELECT access_token FROM items")]
    assert tokens and not any(t in text for t in tokens)


def test_the_runner_runs_the_apps_own_cli_in_its_own_process_group(data_dir):
    rc, out = cronpipe().subprocess_runner(["--help"], 60)
    assert rc == 0 and "usage: finance" in out


# --- the installed entry script, end to end (Hermes's script runner, bash, the app's Python) ----------------------

import hermes_runtime as hr  # noqa: E402

RUN_SCRIPT = r"""
import json, sys
from cron.scheduler import _run_job_script
ok, out = _run_job_script(sys.argv[1], sys.argv[2])
print(json.dumps({"ok": ok, "out": out}))
"""


@pytest.mark.env_dependent("scheduler_delivery")
def test_the_installed_entry_scripts_run_the_pipeline_with_the_apps_python(tmp_path):
    p = hr.Profile(tmp_path / "p")
    config.ensure_data_dir(p.data_dir)
    conn = db.connect(p.data_dir / "finance.db")
    add_item(conn)
    acct(conn)
    tx(conn, TODAY.replace(day=1).isoformat(), 12.0, "Blue Bottle", cat="Eating Out")
    classify.apply(conn)
    conn.close()
    plan = p.crons("plan", "--json", "--deliver", hr.DESTINATION).json()
    r = p.crons("apply", "--plan-id", plan["plan_id"], "--deliver", hr.DESTINATION)
    assert r.returncode == 0, str(r)
    cache = p.hermes_home / "cache" / "hermes-finance"
    jobs = {j["name"]: j for j in p.jobs()}
    for role in ("daily", "weekly"):
        job = jobs[f"hermes-finance-{role}"]
        # Hermes's own runner, as a job's fire would call it: its sanitized environment, the job's workdir as cwd,
        # its script timeout and process-group kill. FINANCE_DATA_DIR comes from the script (the plugin setting).
        got = p.py_json(RUN_SCRIPT, job["script"], job["workdir"], env={"FINANCE_DATA_DIR": ""})
        assert got["ok"], got["out"]
        out = got["out"]
        assert "== sync: FAILED" in out or "== sync: partial" in out, out   # no Plaid keys, no network here
        assert out.rstrip().splitlines()[-1].startswith("== status: partial"), out
        assert "PRE-RUN ERROR" not in out
        assert f"FINANCE_DATA_DIR={p.data_dir}" in (p.hermes_home / "scripts" / job["script"]).read_text()
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert (cache / f"alerts-{TODAY.isoformat()}.json").exists()
    assert (cache / f"week-overview-{TODAY.isoformat()}.json").exists()
    assert all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in cache.iterdir())
    text = "\n".join(f.read_text() for f in cache.iterdir())
    assert not any(s in text for s in hr.SENTINELS)

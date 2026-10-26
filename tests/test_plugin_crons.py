"""`hermes finance crons status|plan|apply`: the plugin's explicit, idempotent registration of the daily and weekly
finance report jobs, tested through the installed Hermes runtime (its plugin loader, its argparse, its cron module) in
disposable profiles (tests/hermes_runtime.py): network denied, the real ~/.hermes unwritable, synthetic job IDs,
synthetic destinations, sentinel secrets.

Groups follow the plugin's scope notes (section 5): 1 discovery/import, 3 approval in cron sessions,
4 adoption, 5 duplicates/concurrency, 6 reconciliation, 7 schedule parity (the pipeline half of 7 is
tests/test_cronpipe.py)."""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hermes_runtime as hr
from agent_tools import build, table_hashes
from hermes_finance import db, prefs, tooldefs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dehardcode  # noqa: E402  (the private words' one source)


DAILY, WEEKLY = "daily", "weekly"
# What would give a real install away, with none of its names or IDs written down here (not even as digests: a short
# name or ID hashes back by guessing): the synthetic destinations and sentinel secrets the tests use in place of real
# ones, the runner's own login, home and name, an absolute user path, and anything shaped like a Hermes job ID or a
# Discord ID. A local install's own names and IDs can be added, untracked: HERMES_FINANCE_TEST_PRIVATE=word,word,...
PRIVATE_ENV = "HERMES_FINANCE_TEST_PRIVATE"
SYNTHETIC = (*hr.SENTINELS, hr.DESTINATION, hr.OTHER_DESTINATION)
SHAPES = re.compile(r"/(?:Users|home)/[^/\s]+|\b[0-9a-f]{12}\b|\b\d{17,20}\b", re.I)   # user path, job ID, Discord ID


def private_words(env: dict[str, str] = os.environ) -> set[str]:
    """The runner's login, home directory and name (words of 4+ letters, not a CI or container account's),
    HERMES_FINANCE_TEST_PRIVATE's words and the private word file: scripts/dehardcode.py's sources, the one place
    they're read."""
    return dehardcode.private_words(env) | ({hr.REAL_HOME.name.lower()} - dehardcode.SERVICE_LOGINS
                                            if len(hr.REAL_HOME.name) >= dehardcode.MIN_OWN else set())


def personal(text: str, words: set[str] | None = None) -> list[str]:
    """What in `text` could be a real install's: a synthetic stand-in, a private word, a user path, an ID."""
    lowered = text.lower()
    hits = [s for s in SYNTHETIC if s in text]
    hits += sorted(w for w in (private_words() if words is None else words)
                   if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", lowered))
    return hits + SHAPES.findall(text)


@pytest.fixture
def p(tmp_path):
    return hr.Profile(tmp_path / "p")


def plan(p: hr.Profile, *args: str) -> tuple[hr.Result, dict | None]:
    r = p.crons("plan", "--json", *args)
    return r, (r.json() if r.stdout.strip().startswith("{") else None)


def plan_ok(p: hr.Profile, *args: str) -> dict:
    r, data = plan(p, *args)
    assert r.returncode == 0 and data and data["ok"], str(r)
    return data


def apply(p: hr.Profile, plan_id: str, *args: str, **kw) -> tuple[hr.Result, dict | None]:
    r = p.crons("apply", "--json", "--plan-id", plan_id, *args, **kw)
    return r, (r.json() if r.stdout.strip().startswith("{") else None)


def apply_ok(p: hr.Profile, *args: str) -> dict:
    data = plan_ok(p, *args)
    r, out = apply(p, data["plan_id"], *args)
    assert r.returncode == 0 and out and out["ok"], str(r)
    return out


def report_jobs(p: hr.Profile) -> list[dict]:
    """The jobs the plugin owns, from its state, as stored in jobs.json."""
    state = p.state() or {}
    return [p.job(role["job_id"]) for role in state.get("roles", {}).values()]


# --- 1. discovery / import ----------------------------------------------------------------------------------------

REGISTRY = r"""
import json
from hermes_cli.plugins import discover_plugins, get_plugin_manager
discover_plugins()
m = get_plugin_manager()
lp = m._plugins.get("hermes-finance")
print(json.dumps({"loaded": lp is not None and lp.enabled, "error": lp.error if lp else "missing",
                  "tools": lp.tools_registered if lp else [], "hooks": lp.hooks_registered if lp else [],
                  "manifest_tools": lp.manifest.provides_tools if lp else [],
                  "manifest_hooks": lp.manifest.provides_hooks if lp else [],
                  "skills": sorted(k for k in m._plugin_skills if k.startswith("hermes-finance:")),
                  "cli": sorted(n for n, c in m._cli_commands.items() if c.get("plugin") == "hermes-finance")}))
"""


@pytest.mark.env_dependent("runtime")
def test_the_enabled_plugin_registers_what_its_manifest_lists_and_the_finance_command(p):
    got = p.py_json(REGISTRY)
    assert got["loaded"] and got["error"] is None, got
    # dynamic: whatever the app defines today (15 tools at 46d6ade), the manifest and the real registrations agree
    assert got["tools"] == got["manifest_tools"] == list(tooldefs.TOOLS), got
    assert len(got["tools"]) == len(tooldefs.TOOLS)
    assert got["hooks"] == got["manifest_hooks"] == ["pre_tool_call"]
    assert got["skills"] == ["hermes-finance:usage"]
    assert got["cli"] == ["finance"]


@pytest.mark.env_dependent("runtime")
def test_a_disabled_plugin_registers_nothing_and_has_no_command(tmp_path):
    p = hr.Profile(tmp_path / "off", enabled=False)
    got = p.py_json(REGISTRY)
    assert not got["loaded"] and got["tools"] == [] and got["cli"] == []
    r = p.crons("status")
    assert r.returncode == 2 and ("invalid choice: 'finance'" in r.stderr or "'finance' is not a `hermes` command." in r.stderr), str(r)


@pytest.mark.env_dependent("scheduler_delivery")
def test_import_register_doctor_help_status_and_plan_write_nothing(p):
    p.run("plugins", "list", "--plain")   # Hermes's own first-run files (SOUL.md, sessions/, ...) are not the plugin's
    before = p.snapshot()
    # doctor loads the plugin in its own isolated home (no plugin settings there): the app comes from the environment
    runs = [p.run("plugins", "doctor", str(p.hermes_home / "plugins" / "hermes-finance"), "--ci",
                  env={"HERMES_FINANCE_APP_DIR": str(hr.ROOT)}),
            p.run("finance", "--help"), p.run("finance", "crons", "--help"), p.crons("status"), p.crons("status", "--json"),
            p.crons("plan"), p.crons("plan", "--deliver", hr.DESTINATION)]
    p.py("from hermes_cli.plugins import discover_plugins\ndiscover_plugins()\ndiscover_plugins(force=True)")
    assert runs[0].returncode == 0 and "registration passed" in runs[0].stdout, str(runs[0])
    assert f"{len(tooldefs.TOOLS)} tool(s), 1 hook(s)" in runs[0].stdout
    assert runs[1].returncode == 0 and "crons" in runs[1].stdout, str(runs[1])
    assert runs[2].returncode == 0 and all(w in runs[2].stdout for w in ("status", "plan", "apply")), str(runs[2])
    assert runs[3].returncode == 0, str(runs[3])
    assert runs[5].returncode == 1 and "destination" in runs[5].out, str(runs[5])   # nothing to deliver to: refused
    assert runs[6].returncode == 0, str(runs[6])
    assert p.snapshot() == before
    for rel in ("plugin-data", "scripts", "cache"):
        assert not (p.hermes_home / rel).exists(), rel
    # cron/ itself is Hermes's own (every command creates its profile dirs); nothing may be in it
    assert not p.jobs_file.exists() and [f for f in (p.hermes_home / "cron").rglob("*") if f.is_file()] == []
    assert not p.data_dir.exists()                    # no finance DB, created or migrated


# --- 3. approval: a request, not a human, under approvals.cron_mode: approve ---------------------------------------

CALLS = r"""
import json, os, sys
from hermes_cli.plugins import discover_plugins, _get_pre_tool_call_directive_details
discover_plugins()
import model_tools
if os.environ.get("CRON_CONTEXTVAR") == "1":     # how the scheduler marks a job's turn (cron/scheduler.py), no env var
    from gateway.session_context import _VAR_MAP
    _VAR_MAP["HERMES_CRON_SESSION"].set("1")
if os.environ.get("DESCRIBE_RAISES") == "1":     # the database can't be read while the hook describes the change
    import hermes_finance.tooldefs as t
    def broken(*a, **k):
        raise RuntimeError("database is locked")
    t.describe = t.approval_needed = broken
calls = json.loads(sys.argv[1])
out = {"directives": {}, "results": {}}
for name, args in calls:
    d = _get_pre_tool_call_directive_details(name, args)
    out["directives"][name] = {"action": d.action, "message": d.message}
    if os.environ.get("RUN_CALLS") == "1":
        out["results"][name] = model_tools.handle_function_call(name, args, task_id="cron-test")
print(json.dumps(out))
"""
WRITE_CALLS = [("finance_set_cap", {"category": "Groceries", "monthly_cap": 1}),
               ("finance_add_rule", {"field": "merchant_name", "pattern": "Costco", "category": "Other Shopping"}),
               ("finance_sync", {}), ("finance_sync", {"refresh": True})]


@pytest.fixture
def finance_db(p):
    """The demo database on a paid Plaid plan with on-demand refresh on: a finance_sync refresh is billed there, so the
    hook would ask for it."""
    path = p.data_dir / "finance.db"
    build("demo", path)
    conn = db.connect(path)
    prefs.save(conn, {"plaid.plan": "paid", "plaid.on_demand_refresh": True})
    conn.close()
    return path


@pytest.mark.env_dependent("runtime")
def test_in_a_cron_session_finance_writes_and_syncs_are_blocked_not_auto_approved(p, finance_db):
    """cron_mode: approve turns a plugin's approval request into an automatic yes when a cron job runs. The finance
    hook must not rely on it: in a cron session every write (and finance_sync, which the job's own pre-run already
    ran) is blocked, and the database stays byte-identical."""
    before = table_hashes(finance_db)
    got = p.py_json(CALLS, json.dumps(WRITE_CALLS[:3]), env={"HERMES_CRON_SESSION": "1", "RUN_CALLS": "1"})
    for name, _ in WRITE_CALLS[:3]:
        assert got["directives"][name]["action"] == "block", got
        assert "scheduled" in got["directives"][name]["message"], got
        assert "block" in got["results"][name].lower() or "scheduled" in got["results"][name], got
    refresh = p.py_json(CALLS, json.dumps(WRITE_CALLS[3:]), env={"HERMES_CRON_SESSION": "1"})
    assert refresh["directives"]["finance_sync"]["action"] == "block"
    assert table_hashes(finance_db) == before


@pytest.mark.parametrize("platform", ["api_server", "webhook"])
@pytest.mark.env_dependent("runtime")
def test_on_an_unattended_platform_finance_writes_are_blocked_not_auto_approved(p, finance_db, platform):
    """The API server and webhooks have nobody to answer an approval either (approvals.unattended_mode, "approve"
    here): the writes are blocked there too, so an Ask-origin change can only go through the app's own approval
    card. A plain sync is not a write and still runs as it does in chat."""
    before = table_hashes(finance_db)
    got = p.py_json(CALLS, json.dumps(WRITE_CALLS[:3]), env={"HERMES_SESSION_PLATFORM": platform, "RUN_CALLS": "1"})
    for name in ("finance_set_cap", "finance_add_rule"):
        assert got["directives"][name]["action"] == "block", got
        assert "nobody here to approve" in got["directives"][name]["message"], got
    assert got["directives"]["finance_sync"]["action"] is None
    refresh = p.py_json(CALLS, json.dumps(WRITE_CALLS[3:]), env={"HERMES_SESSION_PLATFORM": platform})
    assert refresh["directives"]["finance_sync"]["action"] == "block"                 # the billed refresh
    assert "nobody here to approve" in refresh["directives"]["finance_sync"]["message"]
    assert table_hashes(finance_db) == before


@pytest.mark.env_dependent("runtime")
def test_the_schedulers_own_session_marker_blocks_them_too(p, finance_db):
    """The scheduler marks a job's turn in the session context (not the environment): the hook reads it there."""
    before = table_hashes(finance_db)
    got = p.py_json(CALLS, json.dumps(WRITE_CALLS), env={"CRON_CONTEXTVAR": "1", "RUN_CALLS": "1"})
    assert {n: d["action"] for n, d in got["directives"].items()} == \
        {"finance_set_cap": "block", "finance_add_rule": "block", "finance_sync": "block"}
    assert table_hashes(finance_db) == before


@pytest.mark.parametrize("where", [{"HERMES_SESSION_PLATFORM": "api_server"}, {"HERMES_SINGLE_QUERY_SESSION": "1"}])
@pytest.mark.env_dependent("runtime")
def test_where_nobody_answers_a_hook_that_cant_describe_the_change_still_blocks(p, finance_db, where):
    """Elsewhere a change the hook can't describe is still put to the person, with its arguments; with nobody there to
    answer, that request would be approved by config, so it's a block (the billed refresh included)."""
    before = table_hashes(finance_db)
    got = p.py_json(CALLS, json.dumps(WRITE_CALLS), env={**where, "DESCRIBE_RAISES": "1"})
    assert {n: d["action"] for n, d in got["directives"].items()} == \
        {"finance_set_cap": "block", "finance_add_rule": "block", "finance_sync": "block"}
    assert table_hashes(finance_db) == before
    interactive = p.py_json(CALLS, json.dumps(WRITE_CALLS[:1]), env={"DESCRIBE_RAISES": "1"})
    assert interactive["directives"]["finance_set_cap"]["action"] == "approve"          # a person decides


@pytest.mark.env_dependent("runtime")
def test_a_single_query_run_blocks_the_writes(p, finance_db):
    """`hermes chat -q` has nobody to answer mid-run either (approvals.single_query_mode)."""
    before = table_hashes(finance_db)
    got = p.py_json(CALLS, json.dumps(WRITE_CALLS), env={"HERMES_SINGLE_QUERY_SESSION": "1", "RUN_CALLS": "1"})
    assert got["directives"]["finance_set_cap"]["action"] == "block"
    assert "single-query" in got["directives"]["finance_set_cap"]["message"]
    assert got["directives"]["finance_sync"]["action"] == "block"                      # the billed refresh
    assert table_hashes(finance_db) == before


@pytest.mark.env_dependent("runtime")
def test_outside_cron_the_writes_still_ask_and_a_plain_sync_runs_without_asking(p, finance_db):
    got = p.py_json(CALLS, json.dumps(WRITE_CALLS[:3]))
    assert got["directives"]["finance_set_cap"]["action"] == "approve"
    assert "Groceries" in got["directives"]["finance_set_cap"]["message"]
    assert got["directives"]["finance_add_rule"]["action"] == "approve"
    assert got["directives"]["finance_sync"]["action"] is None
    refresh = p.py_json(CALLS, json.dumps(WRITE_CALLS[3:]))
    assert refresh["directives"]["finance_sync"]["action"] == "approve"                # billed: asks, as before
    assert "Plaid bills each refresh" in refresh["directives"]["finance_sync"]["message"]


@pytest.mark.env_dependent("runtime")
def test_a_cron_session_read_still_answers(p, finance_db):
    got = p.py_json(CALLS, json.dumps([("finance_budget_status", {})]), env={"HERMES_CRON_SESSION": "1", "RUN_CALLS": "1"})
    assert got["directives"]["finance_budget_status"]["action"] is None
    assert '"month"' in got["results"]["finance_budget_status"]


# --- 4. adoption ---------------------------------------------------------------------------------------------------

@pytest.mark.env_dependent("scheduler_delivery")
def test_plan_writes_nothing_and_apply_adopts_the_legacy_jobs_unchanged_twice(p):
    daily, weekly, coordinator = hr.seed(p, hr.legacy_specs(str(hr.ROOT)))
    before = {j["id"]: hr.comparable(j) for j in p.jobs()}
    snap = p.snapshot()
    data = plan_ok(p, "--adopt", f"daily={daily}", "--adopt", f"weekly={weekly}")
    assert p.snapshot() == snap                                             # plan: not a byte written
    assert data["roles"][DAILY]["action"] == "adopt" and data["roles"][DAILY]["job_id"] == daily
    assert data["roles"][WEEKLY]["action"] == "adopt" and data["roles"][WEEKLY]["job_id"] == weekly
    assert data["roles"][DAILY]["changes"] == {} == data["roles"][WEEKLY]["changes"]
    assert data["unrelated_jobs"] == 1 and data["scripts"] == []

    r, out = apply(p, data["plan_id"], "--adopt", f"daily={daily}", "--adopt", f"weekly={weekly}")
    assert r.returncode == 0 and out["ok"], str(r)
    assert {j["id"]: hr.comparable(j) for j in p.jobs()} == before           # every field of every job, as it was
    state = p.state()
    assert {k: v["job_id"] for k, v in state["roles"].items()} == {DAILY: daily, WEEKLY: weekly}
    assert out["result"][DAILY]["did"] == "adopted" and out["result"][WEEKLY]["did"] == "adopted"
    assert not (p.hermes_home / hr.SCRIPTS).exists()                        # legacy scripts kept, nothing installed

    # the second run: owned IDs, no changes, not a byte written (jobs.json and the plugin state alike)
    jobs_bytes, state_bytes = p.jobs_file.read_bytes(), p.state_path().read_bytes()
    again = plan_ok(p)
    assert again["roles"][DAILY]["action"] == "keep" and again["roles"][DAILY]["job_id"] == daily
    assert again["roles"][WEEKLY]["action"] == "keep" and again["roles"][WEEKLY]["job_id"] == weekly
    r, out = apply(p, again["plan_id"])
    assert r.returncode == 0 and out["result"][DAILY]["did"] == "unchanged", str(r)
    assert p.jobs_file.read_bytes() == jobs_bytes and p.state_path().read_bytes() == state_bytes
    jobs = p.jobs()
    assert len(jobs) == 3 and len(report_jobs(p)) == 2
    assert p.job(daily)["schedule"]["expr"] == "30 6 * * *" and p.job(weekly)["schedule"]["expr"] == "0 18 * * 0"
    assert p.job(daily)["deliver"] == p.job(weekly)["deliver"] == hr.DESTINATION
    assert p.job(coordinator) == before[coordinator]


@pytest.mark.env_dependent("scheduler_delivery")
def test_first_adoption_by_exact_name_script_and_workdir(p):
    daily, weekly, _ = hr.seed(p, hr.legacy_specs(str(hr.ROOT)))
    data = plan_ok(p)
    assert (data["roles"][DAILY]["action"], data["roles"][DAILY]["how"], data["roles"][DAILY]["job_id"]) == \
        ("adopt", "signature", daily)
    assert (data["roles"][WEEKLY]["action"], data["roles"][WEEKLY]["job_id"]) == ("adopt", weekly)
    r, out = apply(p, data["plan_id"])
    assert r.returncode == 0, str(r)
    assert {k: v["job_id"] for k, v in p.state()["roles"].items()} == {DAILY: daily, WEEKLY: weekly}


@pytest.mark.env_dependent("scheduler_delivery")
def test_a_different_workdir_is_not_a_signature_match(p, tmp_path):
    other = tmp_path / "other-checkout"
    other.mkdir()
    hr.seed(p, hr.legacy_specs(str(other))[:2])
    data = plan_ok(p, "--deliver", hr.DESTINATION)
    assert data["roles"][DAILY]["action"] == "create" and data["roles"][WEEKLY]["action"] == "create"


@pytest.mark.env_dependent("scheduler_delivery")
def test_a_paused_daily_job_stays_paused_and_is_not_replaced(p):
    daily, weekly, _ = hr.seed(p, hr.legacy_specs(str(hr.ROOT), daily_paused=True))
    paused = hr.comparable(p.job(daily))
    assert paused["enabled"] is False and paused["state"] == "paused"
    data = plan_ok(p)
    assert data["roles"][DAILY]["action"] == "adopt" and data["roles"][DAILY]["job"]["enabled"] is False
    r, _ = apply(p, data["plan_id"])
    assert r.returncode == 0, str(r)
    assert hr.comparable(p.job(daily)) == paused and len(p.jobs()) == 3
    status = p.crons("status", "--json").json()
    assert status["roles"][DAILY]["job"]["state"] == "paused" and status["roles"][DAILY]["job"]["enabled"] is False


@pytest.mark.env_dependent("scheduler_delivery")
def test_the_roadmap_coordinator_is_never_matched_or_adoptable(p):
    (coordinator,) = hr.seed(p, hr.legacy_specs(str(hr.ROOT))[2:])
    record = hr.comparable(p.job(coordinator))
    data = plan_ok(p, "--deliver", hr.DESTINATION)
    assert data["roles"][DAILY]["action"] == "create" and data["unrelated_jobs"] == 1
    snap = p.snapshot()
    r, bad = plan(p, "--deliver", hr.DESTINATION, "--adopt", f"daily={coordinator}")
    assert r.returncode == 1 and bad["roles"][DAILY]["action"] == "blocked", str(r)
    assert "doesn't look like" in bad["roles"][DAILY]["reason"]
    r, _ = apply(p, bad["plan_id"], "--deliver", hr.DESTINATION, "--adopt", f"daily={coordinator}")
    assert r.returncode == 1 and p.snapshot() == snap
    apply_ok(p, "--deliver", hr.DESTINATION)
    assert hr.comparable(p.job(coordinator)) == record and len(p.jobs()) == 3


# --- 5. duplicates / concurrency -----------------------------------------------------------------------------------

@pytest.mark.env_dependent("scheduler_delivery")
def test_two_matching_daily_jobs_fail_closed_with_zero_writes_until_one_is_chosen(p):
    specs = hr.legacy_specs(str(hr.ROOT))
    daily, weekly, _, twin = hr.seed(p, specs + [dict(specs[0], extra={})])
    snap = p.snapshot()
    r, data = plan(p)
    assert r.returncode == 1 and not data["ok"], str(r)
    assert data["roles"][DAILY]["action"] == "blocked" and sorted(data["roles"][DAILY]["candidates"]) == sorted([daily, twin])
    assert data["roles"][WEEKLY]["action"] == "adopt"
    r, out = apply(p, data["plan_id"])
    assert r.returncode == 1 and "blocked" in r.out, str(r)
    assert p.snapshot() == snap                                             # zero writes: no state, no lock, no job
    twin_record = hr.comparable(p.job(twin))
    chosen = plan_ok(p, "--adopt", f"daily={daily}")
    assert chosen["roles"][DAILY]["job_id"] == daily and any(twin in w for w in chosen["warnings"])
    r, _ = apply(p, chosen["plan_id"], "--adopt", f"daily={daily}")
    assert r.returncode == 0, str(r)
    assert hr.comparable(p.job(twin)) == twin_record and len(p.jobs()) == 4


DRIVER = r"""
import os, sys, time
sys.path.insert(0, os.environ["HERMES_AGENT_DIR"])
from cron import jobs
real = jobs.create_job
mode = os.environ.get("DRIVER_MODE", "")
def create_job(*a, **k):
    if mode == "slow":
        time.sleep(0.8)
    job = real(*a, **k)
    if mode == "crash-after-first-create":
        os._exit(137)          # the process dies with the job created and nothing else done
    return job
jobs.create_job = create_job
from hermes_cli.main import main
sys.argv = ["hermes", *sys.argv[1:]]
sys.exit(main())
"""


def driver(p: hr.Profile, mode: str, *argv: str) -> hr.Child:
    env = p.env({"DRIVER_MODE": mode, "HERMES_AGENT_DIR": str(hr.HERMES_AGENT)})
    return hr.Child([str(hr.SANDBOX_EXEC), "-f", str(p.sandbox), str(hr.HERMES_PY), "-B", "-c", DRIVER, *argv], env,
                    p.root)


@pytest.mark.parametrize("mode", ["", "slow"])
@pytest.mark.env_dependent("scheduler_delivery")
def test_concurrent_applies_on_an_empty_home_make_exactly_one_job_per_role(p, mode):
    data = plan_ok(p, "--deliver", hr.DESTINATION)
    argv = ("finance", "crons", "apply", "--json", "--plan-id", data["plan_id"], "--deliver", hr.DESTINATION,
            "--lock-timeout", "90")        # the losers wait for the winner, however slow, then find the plan stale
    children = [driver(p, mode, *argv) for _ in range(3)]
    try:
        results = [c.wait(120) for c in children]
    finally:
        for c in children:
            c.kill()
    codes = sorted(r.returncode for r in results)
    assert codes == [0, 1, 1], "\n".join(map(str, results))
    assert all("stale" in r.out for r in results if r.returncode == 1)
    jobs = p.jobs()
    assert sorted(j["name"] for j in jobs) == ["hermes-finance-daily", "hermes-finance-weekly"]
    assert sorted(v["job_id"] for v in p.state()["roles"].values()) == sorted(j["id"] for j in jobs)


@pytest.mark.env_dependent("scheduler_delivery")
def test_a_crash_after_create_is_recovered_by_adoption_not_a_second_job(p):
    data = plan_ok(p, "--deliver", hr.DESTINATION)
    r = driver(p, "crash-after-first-create", "finance", "crons", "apply", "--plan-id", data["plan_id"],
               "--deliver", hr.DESTINATION).wait(120)
    assert r.returncode == 137, str(r)
    (created,) = p.jobs()
    assert DAILY not in ((p.state() or {}).get("roles") or {})             # ownership was never written
    retry = plan_ok(p, "--deliver", hr.DESTINATION)
    assert (retry["roles"][DAILY]["action"], retry["roles"][DAILY]["job_id"]) == ("adopt", created["id"])
    assert retry["roles"][WEEKLY]["action"] == "create"
    r, out = apply(p, retry["plan_id"], "--deliver", hr.DESTINATION)
    assert r.returncode == 0, str(r)
    jobs = p.jobs()
    assert len(jobs) == 2 and p.state()["roles"][DAILY]["job_id"] == created["id"]
    assert hr.comparable(p.job(created["id"])) == hr.comparable(created)


HOLD = r"""
import fcntl, os, sys, time
path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
open(sys.argv[2], "w").write("locked")
time.sleep(120)
"""


def hold_lock(p: hr.Profile) -> hr.Child:
    marker = p.root / "held"
    child = hr.Child([str(hr.SANDBOX_EXEC), "-f", str(p.sandbox), str(hr.HERMES_PY), "-B", "-c", HOLD,
                      str(p.lock_path()), str(marker)], p.env(), p.root)
    deadline = time.monotonic() + 30
    while not marker.exists():
        assert time.monotonic() < deadline and child.proc.poll() is None, "the lock holder didn't start"
        time.sleep(0.05)
    return child


@pytest.mark.env_dependent("scheduler_delivery")
def test_a_held_lock_times_out_and_fails_closed(p):
    data = plan_ok(p, "--deliver", hr.DESTINATION)
    with hold_lock(p):
        snap = p.snapshot()
        started = time.monotonic()
        r, out = apply(p, data["plan_id"], "--deliver", hr.DESTINATION, "--lock-timeout", "1")
        assert r.returncode == 3 and out and not out["ok"] and "lock" in out["error"], str(r)
        assert time.monotonic() - started < 30
        assert p.snapshot() == snap and p.jobs() == []
    r, out = apply(p, data["plan_id"], "--deliver", hr.DESTINATION)
    assert r.returncode == 0, str(r)
    assert len(p.jobs()) == 2


@pytest.mark.env_dependent("scheduler_delivery")
def test_profiles_share_no_jobs_state_or_lock(tmp_path):
    a, b = hr.Profile(tmp_path / "a"), hr.Profile(tmp_path / "b")
    plan_ok(a, "--deliver", hr.DESTINATION)
    with hold_lock(a):
        apply_ok(b, "--deliver", hr.OTHER_DESTINATION)
    assert a.jobs() == [] and a.state() is None
    assert [j["deliver"] for j in b.jobs()] == [hr.OTHER_DESTINATION] * 2


# --- 6. reconciliation ---------------------------------------------------------------------------------------------

DERIVED_BY_SCHEDULE = {"schedule", "schedule_display", "next_run_at"}


@pytest.mark.env_dependent("scheduler_delivery")
def test_schedule_and_delivery_changes_are_a_diff_first_and_change_only_those_fields(p):
    daily, weekly, coordinator = hr.seed(p, hr.legacy_specs(str(hr.ROOT)))
    apply_ok(p)
    before = {j["id"]: hr.comparable(j) for j in p.jobs()}
    args = ("--daily-time", "07:15", "--deliver", hr.OTHER_DESTINATION)
    snap = p.snapshot()
    data = plan_ok(p, *args)
    assert p.snapshot() == snap
    assert data["roles"][DAILY]["action"] == "update"
    assert data["roles"][DAILY]["changes"] == {"schedule": {"from": "30 6 * * *", "to": "15 7 * * *"},
                                               "deliver": {"from": hr.DESTINATION, "to": hr.OTHER_DESTINATION}}
    assert data["roles"][WEEKLY]["changes"] == {"deliver": {"from": hr.DESTINATION, "to": hr.OTHER_DESTINATION}}
    r, out = apply(p, data["plan_id"], *args)
    assert r.returncode == 0 and out["result"][DAILY]["did"] == "updated", str(r)
    after = {j["id"]: hr.comparable(j) for j in p.jobs()}
    assert after[daily]["schedule"]["expr"] == "15 7 * * *" and after[daily]["deliver"] == hr.OTHER_DESTINATION
    assert after[daily]["next_run_at"].endswith("07:15:00-07:00") or "T07:15:00" in after[daily]["next_run_at"]
    assert {k: v for k, v in after[daily].items() if k not in DERIVED_BY_SCHEDULE | {"deliver"}} == \
        {k: v for k, v in before[daily].items() if k not in DERIVED_BY_SCHEDULE | {"deliver"}}
    assert {k: v for k, v in after[weekly].items() if k != "deliver"} == \
        {k: v for k, v in before[weekly].items() if k != "deliver"}
    assert after[coordinator] == before[coordinator]


@pytest.mark.parametrize("deliver", [None, "Hermes chat", "local", "origin", "all", "discord", "nosuchplatform:123",
                                     "discord:1,telegram:2", "discord: 12", ""])
@pytest.mark.env_dependent("scheduler_delivery")
def test_a_new_job_needs_a_routable_destination(p, deliver):
    args = () if deliver is None else ("--deliver", deliver)
    snap = p.snapshot()
    r, data = plan(p, *args)
    assert r.returncode == 1 and data and not data["ok"], str(r)
    assert data["roles"][DAILY]["action"] == "blocked" and "destination" in data["roles"][DAILY]["reason"]
    r, _ = apply(p, data["plan_id"], *args)
    assert r.returncode == 1 and p.snapshot() == snap and p.jobs() == []


@pytest.mark.env_dependent("scheduler_delivery")
def test_the_destination_can_come_from_the_plugin_setting_but_not_the_settings_label(tmp_path):
    label = hr.Profile(tmp_path / "label", settings={"HERMES_FINANCE_CRON_DELIVER": "Hermes chat"})
    r, data = plan(label)
    assert r.returncode == 1 and data["roles"][DAILY]["action"] == "blocked", str(r)
    ok = hr.Profile(tmp_path / "ok", settings={"HERMES_FINANCE_CRON_DELIVER": hr.DESTINATION})
    apply_ok(ok)
    assert [j["deliver"] for j in ok.jobs()] == [hr.DESTINATION] * 2


@pytest.mark.env_dependent("scheduler_delivery")
def test_a_stale_plan_is_refused_with_zero_writes(p):
    daily, weekly, _ = hr.seed(p, hr.legacy_specs(str(hr.ROOT)))
    data = plan_ok(p)
    p.py_json("import json, sys\nfrom cron import jobs\n"
              "print(json.dumps(jobs.update_job(sys.argv[1], {'deliver': sys.argv[2]})['id']))", daily,
              hr.OTHER_DESTINATION)
    snap = p.snapshot()
    r, out = apply(p, data["plan_id"])
    assert r.returncode == 1 and "stale" in out["error"], str(r)
    assert p.snapshot() == snap and p.state() is None
    r, out = apply(p, plan_ok(p, "--deliver", hr.DESTINATION)["plan_id"], "--deliver", hr.OTHER_DESTINATION)
    assert r.returncode == 1 and "stale" in out["error"], str(r)           # other options than the plan's
    assert p.snapshot() == snap


@pytest.mark.env_dependent("scheduler_delivery")
def test_a_job_that_fires_between_plan_and_apply_doesnt_make_the_plan_stale(p):
    daily, weekly, _ = hr.seed(p, hr.legacy_specs(str(hr.ROOT)))
    data = plan_ok(p)
    p.py_json("import json, sys\nfrom cron import jobs\n"            # its run bookkeeping moves, nothing else
              "print(json.dumps(jobs.update_job(sys.argv[1], {'last_run_at': '2026-09-30T06:30:02-07:00',"
              " 'next_run_at': '2026-10-01T06:30:00-07:00', 'last_status': 'ok'})['id']))", daily)
    r, out = apply(p, data["plan_id"])
    assert r.returncode == 0 and out["result"][DAILY]["did"] == "adopted", str(r)


@pytest.mark.env_dependent("scheduler_delivery")
def test_a_crash_after_create_is_recovered_even_after_the_app_dir_setting_changes(p, tmp_path):
    """The pending record written before create_job names the job it was making: the retry adopts that job although it
    no longer matches the (changed) app directory."""
    data = plan_ok(p, "--deliver", hr.DESTINATION)
    r = driver(p, "crash-after-first-create", "finance", "crons", "apply", "--plan-id", data["plan_id"],
               "--deliver", hr.DESTINATION).wait(120)
    assert r.returncode == 137, str(r)
    (orphan,) = p.jobs()
    other = tmp_path / "other-app"                   # another checkout the plugin can load: the same sources, a Python
    other.mkdir()
    (other / "src").symlink_to(hr.ROOT / "src", target_is_directory=True)
    (other / ".venv" / "bin").mkdir(parents=True)
    (other / ".venv" / "bin" / "python").symlink_to(hr.APP_PYTHON)
    p.settings["HERMES_FINANCE_APP_DIR"] = str(other)
    p.write_config()
    retry = plan_ok(p, "--deliver", hr.DESTINATION)
    assert (retry["roles"][DAILY]["action"], retry["roles"][DAILY]["how"], retry["roles"][DAILY]["job_id"]) == \
        ("adopt", "recovered", orphan["id"])
    r, _ = apply(p, retry["plan_id"], "--deliver", hr.DESTINATION)
    assert r.returncode == 0, str(r)
    assert sum(j["name"] == "hermes-finance-daily" for j in p.jobs()) == 1 and len(p.jobs()) == 2
    assert p.state()["pending"] == {} and p.state()["roles"][DAILY]["job_id"] == orphan["id"]


@pytest.mark.env_dependent("scheduler_delivery")
def test_a_relative_data_dir_setting_blocks_the_scripts_rather_than_falling_back(tmp_path):
    p = hr.Profile(tmp_path / "rel", settings={"HERMES_FINANCE_DATA_DIR": "data"})
    r, data = plan(p, "--deliver", hr.DESTINATION)
    assert r.returncode == 1 and any("HERMES_FINANCE_DATA_DIR" in b for b in data["blocked"]), str(r)


@pytest.mark.env_dependent("runtime")
def test_adopt_names_one_job_per_role(p):
    r = p.crons("plan", "--adopt", "daily=aaaaaaaaaaaa", "--adopt", "daily=bbbbbbbbbbbb")
    assert r.returncode == 2 and "given twice" in r.stderr, str(r)


@pytest.mark.parametrize("broken", ["jobs", "state"])
@pytest.mark.env_dependent("scheduler_delivery")
def test_an_unreadable_store_refuses_everything_and_status_says_so(p, broken):
    if broken == "jobs":
        p.jobs_file.parent.mkdir(parents=True, exist_ok=True)
        p.jobs_file.write_text('{"jobs": {"not": "a list"}}')
    else:
        apply_ok(p, "--deliver", hr.DESTINATION)
        p.state_path().write_text(json.dumps({"crons": ["not", "an", "object"]}))
    snap = p.snapshot()
    st = p.crons("status", "--json")
    assert st.returncode == 1 and st.json()["scheduler"].startswith("unavailable"), str(st)
    r, data = plan(p, "--deliver", hr.DESTINATION)
    assert r.returncode == 1 and "unavailable" in data["error"], str(r)
    r, out = apply(p, "0" * 16, "--deliver", hr.DESTINATION)
    assert r.returncode == 1 and not out["ok"], str(r)
    assert p.snapshot() == snap


@pytest.mark.env_dependent("scheduler_delivery")
def test_status_and_plan_write_nothing_on_a_set_up_profile(p):
    hr.seed(p, hr.legacy_specs(str(hr.ROOT)))
    apply_ok(p, "--use-bundled-scripts")
    snap = p.snapshot()
    for argv in (("status",), ("status", "--json"), ("plan",), ("plan", "--json", "--daily-time", "05:00")):
        assert p.crons(*argv).returncode == 0
    assert p.snapshot() == snap


@pytest.mark.env_dependent("scheduler_delivery")
def test_without_dont_write_bytecode_only_pythons_own_cache_appears(p):
    """Everything else runs with PYTHONDONTWRITEBYTECODE; here Python may cache the plugin's bytecode as it does for any
    module it imports. That cache, under the plugin's own directory, is all that may appear."""
    hr.seed(p, hr.legacy_specs(str(hr.ROOT)))
    snap = p.snapshot()
    env = {"PYTHONDONTWRITEBYTECODE": ""}
    for argv in (("status",), ("plan",)):
        assert p.run("finance", "crons", *argv, env=env, bytecode=True).returncode == 0
    new = {k for k in p.snapshot() if k not in snap}
    assert all(k.startswith("plugins/hermes-finance/") and "__pycache__" in k for k in new), sorted(new)


@pytest.mark.env_dependent("runtime")
def test_apply_needs_a_plan_id(p):
    r = p.crons("apply", "--deliver", hr.DESTINATION)
    assert r.returncode == 2 and "--plan-id" in r.stderr, str(r)
    assert p.jobs() == []


@pytest.mark.parametrize("escape", ["dir-link", "file-link", "scripts-link", "backup-link"])
@pytest.mark.env_dependent("scheduler_delivery")
def test_script_paths_that_escape_the_profile_scripts_dir_are_refused(p, tmp_path, escape):
    outside = tmp_path / "outside"
    outside.mkdir()
    scripts = p.hermes_home / "scripts"
    if escape == "scripts-link":
        scripts.symlink_to(outside, target_is_directory=True)
    elif escape == "dir-link":
        scripts.mkdir()
        (scripts / "hermes-finance").symlink_to(outside, target_is_directory=True)
    elif escape == "backup-link":            # where a replaced script's old copy would go
        (scripts / "hermes-finance").mkdir(parents=True)
        (scripts / "hermes-finance" / ".backup").symlink_to(outside, target_is_directory=True)
    else:
        (scripts / "hermes-finance").mkdir(parents=True)
        (scripts / "hermes-finance" / "daily.sh").symlink_to(outside / "daily.sh")
    snap = p.snapshot()
    r, data = plan(p, "--deliver", hr.DESTINATION)
    assert r.returncode == 1 and not data["ok"] and any("script" in b for b in data["blocked"]), str(r)
    r, _ = apply(p, data["plan_id"], "--deliver", hr.DESTINATION)
    assert r.returncode == 1 and p.snapshot() == snap and list(outside.iterdir()) == []


@pytest.mark.env_dependent("scheduler_delivery")
def test_new_jobs_are_generic_their_scripts_versioned_private_and_read_back(p):
    out = apply_ok(p, "--deliver", hr.DESTINATION)
    daily, weekly = (p.job(out["result"][r]["job_id"]) for r in (DAILY, WEEKLY))
    assert (daily["name"], daily["schedule"]["expr"], daily["script"]) == \
        ("hermes-finance-daily", "30 6 * * *", "hermes-finance/daily.sh")
    assert (weekly["name"], weekly["schedule"]["expr"], weekly["script"]) == \
        ("hermes-finance-weekly", "0 18 * * 0", "hermes-finance/weekly.sh")
    for job in (daily, weekly):
        assert job["deliver"] == hr.DESTINATION and job["workdir"] == str(hr.ROOT) and job["enabled"] is True
        assert job["skills"] == [] and job["model"] is None and job["provider"] is None
        assert "finance_sync" in job["prompt"] and personal(job["prompt"]) == []
    state = p.state()
    for role in (DAILY, WEEKLY):
        path = p.hermes_home / hr.SCRIPTS / f"{role}.sh"
        text = path.read_text()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert f"hermes-finance cron entry script: {role} (version 2)" in text
        assert "hermes_finance.cronpipe" in text and str(hr.APP_PYTHON) in text
        assert state["scripts"][f"hermes-finance/{role}.sh"]["sha256"] == __import__("hashlib").sha256(
            path.read_bytes()).hexdigest()
    again = plan_ok(p, "--deliver", hr.DESTINATION)
    assert again["roles"][DAILY]["action"] == "keep" and again["scripts"] == []


@pytest.mark.env_dependent("scheduler_delivery")
def test_a_changed_installed_script_is_replaced_only_by_apply_with_a_backup(p):
    apply_ok(p, "--deliver", hr.DESTINATION)
    path = p.hermes_home / hr.SCRIPTS / "daily.sh"
    original = path.read_bytes()
    path.write_bytes(original + b"echo hand-edited\n")
    data = plan_ok(p)
    assert [(s["path"], s["action"]) for s in data["scripts"]] == [("hermes-finance/daily.sh", "replace")]
    assert path.read_bytes().endswith(b"hand-edited\n")                    # plan changes nothing
    r, _ = apply(p, data["plan_id"])
    assert r.returncode == 0, str(r)
    assert path.read_bytes() == original
    backups = list((p.hermes_home / hr.SCRIPTS / ".backup").iterdir())
    assert len(backups) == 1 and backups[0].read_bytes().endswith(b"hand-edited\n")


def _entry(monkeypatch):
    monkeypatch.syspath_prepend(str(hr.ROOT))
    import importlib
    return importlib.import_module("hermes_plugin.crons.entry"), importlib.import_module("hermes_plugin.crons.roles")


@pytest.mark.parametrize("what", ["plugin-dir", "backup-dir"])
def test_a_directory_swapped_for_a_link_mid_install_cant_redirect_a_write(tmp_path, monkeypatch, what):
    """Past the path checks, install works through open directories (O_NOFOLLOW): a directory renamed away and replaced
    by a link to somewhere else just then gets nothing written through the link."""
    entry, roles = _entry(monkeypatch)
    scripts, outside = tmp_path / "scripts", tmp_path / "outside"
    (scripts / "hermes-finance").mkdir(parents=True)
    (scripts / "hermes-finance" / "daily.sh").write_bytes(b"old\n")
    outside.mkdir()
    real = entry._subdir

    def swap(parent_fd, name):
        if what == "backup-dir" and name == ".backup":           # a link appears where .backup will be made
            (scripts / "hermes-finance" / ".backup").symlink_to(outside, target_is_directory=True)
        fd = real(parent_fd, name)
        if what == "plugin-dir" and name == "hermes-finance":    # the directory moves, a link takes its name
            os.rename(scripts / "hermes-finance", scripts / "moved")
            (scripts / "hermes-finance").symlink_to(outside, target_is_directory=True)
        return fd

    monkeypatch.setattr(entry, "_subdir", swap)
    if what == "plugin-dir":
        got = entry.install(scripts, roles.DAILY, b"new\n")
        assert got["action"] == "replace" and (scripts / "moved" / "daily.sh").read_bytes() == b"new\n"
        assert [f.read_bytes() for f in (scripts / "moved" / ".backup").iterdir()] == [b"old\n"]
    else:
        with pytest.raises(entry.ScriptProblem):
            entry.install(scripts, roles.DAILY, b"new\n")
        assert (scripts / "hermes-finance" / "daily.sh").read_bytes() == b"old\n"
    assert list(outside.iterdir()) == []


@pytest.mark.env_dependent("scheduler_delivery")
def test_legacy_jobs_move_to_the_bundled_scripts_only_when_asked(p):
    daily, weekly, _ = hr.seed(p, hr.legacy_specs(str(hr.ROOT)))
    apply_ok(p)
    before = hr.comparable(p.job(daily))
    data = plan_ok(p, "--use-bundled-scripts")
    assert data["roles"][DAILY]["changes"] == {"script": {"from": "finance_daily_prerun.sh",
                                                          "to": "hermes-finance/daily.sh"}}
    r, _ = apply(p, data["plan_id"], "--use-bundled-scripts")
    assert r.returncode == 0, str(r)
    after = hr.comparable(p.job(daily))
    assert after["script"] == "hermes-finance/daily.sh" and after["skills"] == ["personal-finance-aggregation"]
    assert {k: v for k, v in after.items() if k != "script"} == {k: v for k, v in before.items() if k != "script"}


# --- 7. schedule parity: local wall-clock times across DST ---------------------------------------------------------

FIRES = r"""
import json, shutil, sys, tempfile
from datetime import datetime
from pathlib import Path
import hermes_time
from cron import jobs
job = [j for j in json.load(open(sys.argv[1]))["jobs"] if j["id"] == sys.argv[2]][0]
until = datetime.fromisoformat(sys.argv[3])
tz = hermes_time.get_timezone()           # the profile timezone, None when only the host's local time applies
def clock(instant):                        # what hermes_time.now() returns at that instant
    return instant.astimezone(tz) if tz else datetime.fromtimestamp(instant.timestamp()).astimezone()
fires, home = [], Path(tempfile.mkdtemp())
(home / "cron").mkdir()
(home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [job]}))
try:
    with jobs.use_cron_store(home):
        at = datetime.fromisoformat(job["next_run_at"])
        while at < until:
            jobs._hermes_now = lambda at=at: clock(at)
            due = [j["id"] for j in jobs.get_due_jobs()]
            if job["id"] in due:                               # the ticker's order: advance, run, mark
                fires.append(clock(at).isoformat())
                jobs.advance_next_runs([job["id"]])
                done = at.timestamp() + 120
                jobs._hermes_now = lambda: clock(datetime.fromtimestamp(done).astimezone())
                jobs.mark_job_run(job["id"], True)
            nxt = datetime.fromisoformat(jobs.get_job(job["id"])["next_run_at"])
            at = nxt if nxt > at else datetime.fromtimestamp(at.timestamp() + 60).astimezone()
finally:
    shutil.rmtree(home)
print(json.dumps({"tz": str(tz) if tz else None, "fires": fires}))
"""


# What the installed Hermes (d31d12d) and its croniter do on the two DST switch days, found by the test below. With a
# profile `timezone`, croniter's next run from the day before the fall-back lands an hour past the local time, Hermes's
# stale-expression guard re-anchors it to the next occurrence without firing, and that day's run is skipped (the
# fall-back day is a Sunday in both zones, so the weekly run too). With only the host's local time, the stored fixed
# offset makes the spring-forward day's run one hour late. Every other day is exact. Hermes's scheduling, not the
# plugin's (it hands Hermes plain cron expressions and no timezone): recorded here so a change shows up, and so a fixed
# Hermes still passes.
#
# The jobs are created "now" (Hermes's own clock), so the window is a rolling year from today and the switch days are
# worked out from the zone's rules for that window. A fixed end date and hard-coded 2026-27 switch days broke the
# test as the calendar moved on (2026-10-06: one fire short of the old 190 floor; after 2026-10-25 Berlin's fall-back
# would have dropped out of the window).
WINDOW_DAYS = 380                              # > a year, so both switches are always inside


def dst_days(zone, start, days):
    """{'fall_back': {...}, 'spring_forward': {...}} ISO dates of the zone's clock changes in [start, start + days)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    z, out = ZoneInfo(zone), {"fall_back": set(), "spring_forward": set()}
    noon = lambda d: datetime(d.year, d.month, d.day, 12, tzinfo=z).utcoffset()
    for n in range(days):
        d = start + timedelta(n)
        before, after = noon(d - timedelta(1)), noon(d)
        if after < before:
            out["fall_back"].add(d.isoformat())
        elif after > before:
            out["spring_forward"].add(d.isoformat())
    return out


@pytest.mark.parametrize("zone,where", [("America/Los_Angeles", "profile"), ("Europe/Berlin", "profile"),
                                        ("America/Los_Angeles", "host"), ("Europe/Berlin", "host")])
@pytest.mark.env_dependent("scheduler_delivery")
def test_default_schedules_fire_at_local_0630_and_sunday_1800_across_dst(tmp_path, zone, where):
    """The jobs' schedule is a plain cron expression and Hermes supplies the timezone: the profile's `timezone` when set
    (the host's is UTC here, to show the profile wins), else the host's local time. A copy of each created job is
    driven through Hermes's own fire path (get_due_jobs, advance_next_runs, mark_job_run) with a controlled clock,
    through both DST switches of 2026-27, and every fire's local time is checked."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    p = hr.Profile(tmp_path / "tz", timezone=zone if where == "profile" else None,
                   host_tz=zone if where == "host" else "UTC")
    out = apply_ok(p, "--deliver", hr.DESTINATION)
    start = datetime.now(ZoneInfo(zone)).date()
    until = datetime(start.year, start.month, start.day, tzinfo=ZoneInfo(zone)) + timedelta(WINDOW_DAYS)
    switches = dst_days(zone, start, WINDOW_DAYS)
    fall_back, spring_forward = switches["fall_back"], switches["spring_forward"]
    assert fall_back and spring_forward                                               # the window spans both
    until = until.isoformat()
    for role, (hour, minute), step in ((DAILY, (6, 30), 1), (WEEKLY, (18, 0), 7)):
        got = p.py_json(FIRES, str(p.jobs_file), out["result"][role]["job_id"], until)
        assert got["tz"] == (zone if where == "profile" else None)
        local = [datetime.fromisoformat(s).astimezone(ZoneInfo(zone)) for s in got["fires"]]
        days = [d.date() for d in local]
        assert len(days) == len(set(days)) and len(days) >= (WINDOW_DAYS // 7 - 3 if step == 7 else WINDOW_DAYS - 4)  # never twice a day
        assert len({d.utcoffset() for d in local}) == 2                                   # both sides of DST
        assert all(d.weekday() == 6 for d in local) if step == 7 else True
        span = [days[0] + timedelta(n) for n in range(0, (days[-1] - days[0]).days + 1, step)]
        missing = {d.isoformat() for d in span if d not in set(days)}
        late = {d.date().isoformat(): d.strftime("%H:%M") for d in local if (d.hour, d.minute) != (hour, minute)}
        print(json.dumps({"zone": zone, "tz_from": where, "role": role, "fires": len(local), "missing": sorted(missing),
                          "not_on_time": late}))
        if where == "profile":
            assert missing <= fall_back and late == {}
        else:
            assert missing == set() and set(late) <= spring_forward
            assert all(v == f"{hour + 1:02d}:{minute:02d}" for v in late.values())


# --- negative control: nothing reaches the default profile, the network or the secrets -----------------------------

PROBES = r"""
import errno, json, os, socket, sys
out = {}
try:
    open(sys.argv[1], "a").close(); out["write_real_profile"] = "allowed"
except PermissionError:
    out["write_real_profile"] = "denied"
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(2)
try:
    s.connect(("192.0.2.1", 9)); out["network"] = "allowed"
except PermissionError:
    out["network"] = "denied"
except OSError as e:
    out["network"] = f"other: {e.errno}"
finally:
    s.close()
try:
    socket.getaddrinfo("example.com", 443); out["dns"] = "allowed"
except socket.gaierror:
    out["dns"] = "denied"
print(json.dumps(out))
"""


@pytest.mark.env_dependent("scheduler_delivery")
def test_nothing_reaches_the_default_profile_the_network_or_the_secrets(p):
    real = hr.REAL_HOME / ".hermes"
    # what the plugin would write in a profile, at the real default profile's paths (the data dir under the namespace
    # Hermes derives for it, asked of the runtime); only ever stat()ed here, never read
    ours = [real / "scripts" / "hermes-finance", real / "plugin-data" / p.plugin_data().name,
            real / "cache" / "hermes-finance"]
    assert p.plugin_data().name.startswith("agent-plugin-hermes-finance")
    assert not any(path.exists() for path in ours)
    probes = p.py_json(PROBES, str(real / "config.yaml"))
    assert probes == {"write_real_profile": "denied", "network": "denied", "dns": "denied"}   # the sandbox is on
    daily, weekly, _ = hr.seed(p, hr.legacy_specs(str(hr.ROOT)))
    runs = [p.crons("status"), p.crons("plan", "--json")]
    runs.append(p.crons("apply", "--plan-id", runs[1].json()["plan_id"]))
    runs += [p.crons("status", "--json"), p.crons("plan", "--json", "--use-bundled-scripts", "--deliver",
                                                  hr.OTHER_DESTINATION)]
    runs.append(p.crons("apply", "--use-bundled-scripts", "--deliver", hr.OTHER_DESTINATION, "--plan-id",
                        runs[-1].json()["plan_id"]))
    assert [r.returncode for r in runs] == [0] * 6, "\n".join(map(str, runs))
    assert not any(path.exists() for path in ours)
    text = "\n".join(r.out for r in runs) + p.all_text()
    for sentinel in hr.SENTINELS:
        assert sentinel not in text


def test_the_shipped_cron_files_are_generic():
    """No owner name, job ID, channel or absolute user path in anything this lane ships: the plugin's command modules,
    template and prompts, and the app's pipeline (the IDs and channel of a real install only ever arrive as --adopt /
    --deliver arguments or plugin settings)."""
    shipped = [*sorted((hr.PLUGIN_SRC / "crons").rglob("*")), hr.PLUGIN_SRC / "finance_cli.py",
               hr.PLUGIN_SRC / "__init__.py", hr.PLUGIN_SRC / "plugin.yaml", hr.ROOT / "src" / "hermes_finance" / "cronpipe.py"]
    files = [f for f in shipped if f.is_file() and "__pycache__" not in f.parts]
    assert {f.suffix for f in files} >= {".py", ".md", ".sh", ".yaml"} and len(files) >= 12
    hits = [f"{f.relative_to(hr.ROOT)}: {w}" for f in files for w in personal(f.read_text())]
    assert hits == []
    # the check itself catches a planted leak of each kind, and nothing in plain text
    assert personal(f"deliver to {hr.DESTINATION}") and personal(f"key={hr.SENTINEL_AUTH}")
    assert personal("ask ZYXWVU in #x", {"zyxwvu"}) == ["zyxwvu"] and personal(f"cd {hr.REAL_HOME}/x")
    assert personal("job 0f1e2d3c4b5a, channel 123456789012345678", set()) == ["0f1e2d3c4b5a", "123456789012345678"]
    assert private_words({PRIVATE_ENV: "Zyxwvu, 42"}) >= {"zyxwvu", "42"}
    assert personal("the daily job runs at 06:30 and posts to the channel", set()) == []


# --- the packaging gate: the app needs 3.12, Hermes runs the plugin on its own Python ------------------------------

@pytest.mark.env_dependent("interpreter")
def test_runtime_python_gate():
    pyproject = (hr.ROOT / "pyproject.toml").read_text()
    assert re.search(r'^requires-python = ">=3\.12"$', pyproject, re.M)     # not lowered
    assert hr.py_version_of(hr.APP_PYTHON) >= (3, 12)
    runtime = hr.py_version_of(hr.HERMES_PY)
    # everything Hermes's interpreter loads: the plugin package, and the app source its tools import
    files = sorted(hr.PLUGIN_SRC.rglob("*.py")) + sorted((hr.ROOT / "src" / "hermes_finance").rglob("*.py"))
    code = ("import sys, json\nbad = []\nfor f in sys.argv[1:]:\n    try:\n        compile(open(f).read(), f, 'exec')\n"
            "    except SyntaxError as e:\n        bad.append([f, e.lineno, e.msg])\nprint(json.dumps(bad))")
    got = subprocess.run([str(hr.HERMES_PY), "-B", "-I", "-c", code, *map(str, files)], capture_output=True, text=True,
                         timeout=120)
    assert got.returncode == 0 and json.loads(got.stdout) == [], got.stdout + got.stderr
    # the cron modules are stdlib (+ Hermes, imported lazily) only: nothing a 3.11 runtime would lack
    for f in sorted((hr.PLUGIN_SRC / "crons").rglob("*.py")) + [hr.PLUGIN_SRC / "finance_cli.py"]:
        tree = ast.parse(f.read_text())
        top = {(n.module or "").split(".")[0] for n in tree.body if isinstance(n, ast.ImportFrom) and n.level == 0}
        top |= {a.name.split(".")[0] for n in tree.body if isinstance(n, ast.Import) for a in n.names}
        assert top <= set(sys.stdlib_module_names) | {"__future__"}, (f, top)
    # the scheduled pipeline never runs on the runtime's interpreter: it refuses anything older than the app's floor
    if runtime < (3, 12):
        r = subprocess.run([str(hr.HERMES_PY), "-B", "-I", "-c",
                            f"import sys; sys.path.insert(0, {str(hr.ROOT / 'src')!r}); "
                            "from hermes_finance import cronpipe; sys.exit(cronpipe.main(['daily', '--artifacts', '/nonexistent']))"],
                           capture_output=True, text=True, timeout=120)
        assert "needs Python 3.12" in r.stdout and not Path("/nonexistent").exists(), r.stdout + r.stderr

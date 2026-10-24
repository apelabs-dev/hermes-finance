"""The pre-run steps of the Hermes plugin's daily and weekly finance report jobs (hermes_plugin/crons).

`hermes finance crons apply` installs an entry script per job in the Hermes profile's scripts directory; Hermes runs it
just before the job's agent, which gets what it prints. The script runs this module with the app's own Python:

    python -m hermes_finance.cronpipe daily|weekly --artifacts DIR

daily: the bank sync, the Alpaca snapshot, loan payments, card autopay suggestions (listed, never saved), car and
property estimates, the classification queue (at most 120 merchant groups) and the alerts the Settings switches select
(cap 80%, over cap, broken connection; no other kind). weekly: the bank sync (the weekly job's one extra sync, as
before) and the overview, budgets and recurring snapshots. These are the steps, in their order, of the pre-run scripts
the jobs ran before (scripts/finance_daily_prerun.sh and its weekly twin), without their dependency on the dashboard:
alerts and snapshots are read from the database directly, read-only, instead of from the running page's API.

Nothing here asks the banks for fresh data (Plaid's billed refresh): `finance sync` pulls what they already sent. Each
`finance` step runs in its own process with a timeout, as the shell scripts did, and a failed step doesn't stop the
others. The output is short: a line per step (ok, partial, skipped, FAILED), the step's last lines, file paths and
counts; never the queue or a transaction. Artifacts go in DIR (0700), each file 0600, named by date. A step that fails
writes no file under today's name and renames an earlier one from today to *.stale.json, so nothing stale passes for
current. A missing database is an error, not a new empty database."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, TextIO

MIN_PYTHON = (3, 12)        # the app's floor (pyproject requires-python); Hermes's own interpreter may be older
MAX_GROUPS = 120            # classification queue groups per daily run
STEP_TIMEOUT = 900.0        # seconds per `finance` step at most
BUDGET = 3000.0             # seconds for the whole run, under Hermes's default script timeout (3600 s), which kills a
                            # run with no output at all; a step without time left isn't started and is marked FAILED
MIN_STEP = 30.0             # a step isn't started with less than this left
ALERTS_SHOWN = 15
DAILY_STEPS = (             # (section, `finance` arguments, lines of its output kept)
    ("sync", ["sync"], 12),
    ("alpaca", ["alpaca-sync"], 4),
    ("loan payments", ["match-payments"], 8),
    ("card autopay suggestions", ["match-payments", "--suggest"], 8),
    ("car / property estimates", ["revalue"], 8),
)
WEEKLY_SYNC_TAIL = 3
SNAPSHOTS = ("overview", "budgets", "recurring")
SETTINGS = ("ai.mode", "valuations.carfax", "valuations.redfin", "alerts.cap_80", "alerts.over_cap",
            "alerts.broken_connection")
ALERT_SWITCH = {("cap", "warn"): "alerts.cap_80", ("cap", "over"): "alerts.over_cap",
                ("connection", "error"): "alerts.broken_connection"}
SYNCED = re.compile(r"synced (\d+)/(\d+) item")

Runner = Callable[[list[str], float], "tuple[int, str]"]


def subprocess_runner(args: list[str], timeout: float) -> tuple[int, str]:
    """`finance <args>` with this interpreter in its own process group (killed with it after `timeout` seconds);
    (exit code, stdout and stderr)."""
    proc = subprocess.Popen([sys.executable, "-m", "hermes_finance.cli", *args], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _killpg(proc)
        out, _ = proc.communicate()
        return 124, f"{out or ''}\n(timed out after {timeout:g}s)"
    finally:
        _killpg(proc)
    return proc.returncode, out or ""


def _killpg(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _tail(text: str, n: int) -> list[str]:
    return [line for line in text.strip().splitlines() if line.strip()][-n:]


class Pipeline:
    def __init__(self, role: str, artifacts: Path, runner: Runner, today: date, out: TextIO,
                 clock: Callable[[], float] = time.monotonic):
        self.role, self.artifacts, self.runner, self.today, self.out = role, Path(artifacts), runner, today, out
        self.clock = clock
        self.deadline = clock() + BUDGET
        self.steps: dict[str, str] = {}
        self.switches: dict[str, Any] = {}

    def time_left(self, name: str) -> float | None:
        """Seconds this step may take, or None (then it's marked FAILED, not run: the run is out of time)."""
        left = self.deadline - self.clock()
        if left < MIN_STEP:
            self.section(name, "failed", f"not run: out of time, the run's {BUDGET:g}s budget is spent")
            return None
        return min(STEP_TIMEOUT, left)

    # --- output and files ----------------------------------------------------------------------------------------

    def say(self, line: str = "") -> None:
        print(line, file=self.out)

    def section(self, name: str, status: str, detail: str = "") -> None:
        self.steps[name] = status
        shown = "FAILED" if status == "failed" else status
        self.say(f"== {name}: {shown}" + (f" ({detail})" if detail else ""))

    def path(self, name: str) -> Path:
        return self.artifacts / f"{name}-{self.today.isoformat()}.json"

    def write(self, name: str, payload: Any) -> Path:
        self.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.artifacts, 0o700)
        path = self.path(name)
        fd, tmp = tempfile.mkstemp(dir=self.artifacts, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, default=str, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return path

    def stale(self, name: str) -> None:
        """This run has no current `name`: an earlier file from today is renamed so it isn't taken for this run's."""
        path = self.path(name)
        if path.exists():
            renamed = path.with_name(f"{name}-{self.today.isoformat()}.stale.json")
            os.replace(path, renamed)
            self.say(f"an earlier {name} file from today was renamed {renamed.name}: it is not from this run")
        self.say(f"no current {name} file for today")

    # --- steps ---------------------------------------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        from . import config
        self.say(f"== date: {datetime.now().astimezone().strftime('%A %Y-%m-%d %H:%M %Z')}")
        db_path = config.db_path()
        if not db_path.exists():
            self.say(f"PRE-RUN ERROR: no finance database at {db_path} (is HERMES_FINANCE_DATA_DIR / FINANCE_DATA_DIR "
                     "right? `finance serve` creates it when a bank is connected); nothing was run")
            self.say("== status: failed")
            return self.report("failed")
        self.settings(db_path)
        if self.role == "daily":
            for name, args, keep in DAILY_STEPS:
                self.finance_step(name, args, keep)
            self.queue()
            self.alerts(db_path)
        else:
            self.finance_step("sync", ["sync"], WEEKLY_SYNC_TAIL)
            self.snapshots(db_path)
        failed = [k for k, v in self.steps.items() if v in ("failed", "partial")]
        status = "partial" if failed else "ok"
        self.say(f"== status: {status}" + (f" (FAILED or partial: {', '.join(failed)})" if failed else ""))
        return self.report(status)

    def report(self, status: str) -> dict[str, Any]:
        return {"role": self.role, "status": status, "steps": dict(self.steps),
                "failed": [k for k, v in self.steps.items() if v in ("failed", "partial")],
                "artifacts": str(self.artifacts)}

    def settings(self, db_path: Path) -> None:
        from . import prefs, tooldefs
        try:
            conn = tooldefs.read_connection(db_path)
            try:
                self.switches = prefs.get_many(conn, list(SETTINGS))
            finally:
                conn.close()
        except Exception as e:
            self.switches = {k: prefs.SPEC[k].default for k in SETTINGS}
            self.section("settings", "failed", f"{e}; the defaults apply")
            return
        s = self.switches
        on = {k: "on" if s[k] else "off" for k in SETTINGS if k != "ai.mode"}
        ai = "ai off" if s["ai.mode"] == "off" else f"ai on ({s['ai.mode']})"
        self.steps["settings"] = "ok"
        self.say(f"== settings: {ai}, carfax {on['valuations.carfax']}, redfin {on['valuations.redfin']}; alerts: "
                 f"cap_80 {on['alerts.cap_80']}, over_cap {on['alerts.over_cap']}, "
                 f"broken_connection {on['alerts.broken_connection']}")

    def finance_step(self, name: str, args: list[str], keep: int) -> None:
        timeout = self.time_left(name)
        if timeout is None:
            return
        rc, output = self.runner(list(args), timeout)
        status, detail = "ok", ""
        if rc != 0:
            status, detail = "failed", f"exit {rc}"
        elif name == "sync" and (m := SYNCED.search(output)) and int(m[1]) < int(m[2]):
            status, detail = "partial", f"{m[1]} of {m[2]} connections synced"
        elif name == "alpaca" and "alpaca-sync: none:" in output:
            status, detail = "skipped", "no Alpaca key file"
        elif name == "card autopay suggestions":
            detail = "listed only, nothing is saved: the owner decides"
        self.section(name, status, detail)
        for line in _tail(output, keep):
            self.say(line)

    def queue(self) -> None:
        name = "classification queue"
        timeout = self.time_left(name)
        if timeout is None:
            return
        rc, output = self.runner(["ai-queue", "--max-groups", str(MAX_GROUPS)], timeout)
        payload = None
        if rc == 0:
            for line in reversed(output.strip().splitlines()):
                if line.startswith("{"):
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        pass
                    break
        if payload is None:
            self.section(name, "failed", f"exit {rc}" if rc else "no queue in its output")
            for line in _tail(output, 3):
                self.say(line)
            self.stale("queue")
            return
        if payload.get("ai_off"):
            self.section(name, "skipped", "AI is off in Settings")
            self.say(payload.get("note") or "AI is off: nothing is queued")
            return
        path = self.write("queue", payload)
        decisions = self.path("decisions")
        self.section(name, "ok")
        self.say(f"run_id={payload.get('run_id')} groups={len(payload.get('groups') or [])} "
                 f"queued_txns={payload.get('queued_txns')} truncated={payload.get('truncated')}")
        self.say(f"queue_file={path}")
        self.say(f"decisions_file={decisions}")
        env = [f"{k}={shlex.quote(os.environ[k])}" for k in ("FINANCE_DATA_DIR", "PYTHONPATH") if os.environ.get(k)]
        self.say("apply_with=" + " ".join((["env", *env] if env else []) +
                                          [shlex.quote(sys.executable), "-m", "hermes_finance.cli", "ai-apply",
                                           shlex.quote(str(decisions))]))

    def alerts(self, db_path: Path) -> None:
        from . import api, tooldefs
        try:
            conn = tooldefs.read_connection(db_path)
            try:
                overview = api.overview(conn, {}, self.today)
            finally:
                conn.close()
        except Exception as e:
            self.section("alerts", "failed", str(e))
            self.stale("alerts")
            return
        wanted, off = [], 0
        for a in overview.get("alerts") or []:
            switch = ALERT_SWITCH.get((a.get("kind"), a.get("level")))
            if switch is None:
                continue          # only the three alert kinds Settings offers are ever reported
            if self.switches.get(switch, True):
                wanted.append(a)
            else:
                off += 1
        record = {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                  "date": self.today.isoformat(), "switches": {k: v for k, v in self.switches.items()
                                                               if k.startswith("alerts.")},
                  "alerts": wanted, "suppressed_by_settings": off, "connections": overview.get("connections")}
        path = self.write("alerts", record)
        self.section("alerts", "ok")
        self.say(f"alerts={len(wanted)}" + (f" (turned off in Settings: {off})" if off else ""))
        for a in wanted[:ALERTS_SHOWN]:
            self.say(f" - {a.get('message')}")
        c = overview.get("connections") or {}
        self.say(f"connections total={c.get('total')} broken={c.get('broken')}")
        self.say(f"alerts_file={path}")

    def snapshots(self, db_path: Path) -> None:
        from . import api, tooldefs
        lines, failed = [], []
        try:
            conn = tooldefs.read_connection(db_path)
        except Exception as e:
            self.section("snapshots", "failed", str(e))
            for name in SNAPSHOTS:
                self.stale(f"week-{name}")
            return
        try:
            for name in SNAPSHOTS:
                try:
                    payload = api.GET_ROUTES[f"/api/{name}"](conn, {}, self.today)
                    lines.append(f"{name}_file={self.write(f'week-{name}', payload)}")
                except Exception as e:
                    failed.append(name)
                    lines.append(f"{name}: FAILED ({e})")
        finally:
            conn.close()
        status = "ok" if not failed else ("failed" if len(failed) == len(SNAPSHOTS) else "partial")
        self.section("snapshots", status, f"FAILED: {', '.join(failed)}" if failed else "")
        for line in lines:
            self.say(line)
        for name in failed:
            self.stale(f"week-{name}")


def run(role: str, artifacts: Path, *, runner: Runner | None = None, today: date | None = None,
        out: TextIO | None = None, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    if role not in ("daily", "weekly"):
        raise ValueError(f"unknown role {role!r}")
    return Pipeline(role, artifacts, runner or subprocess_runner, today or date.today(), out or sys.stdout,
                    clock).run()


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        print(f"PRE-RUN ERROR: hermes_finance.cronpipe needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer (the "
              f"finance app's own); this is {sys.version.split()[0]} at {sys.executable}. The entry script must run "
              "the app's Python; nothing was run.")
        return 0
    ap = argparse.ArgumentParser(prog="python -m hermes_finance.cronpipe",
                                 description="The pre-run steps of the daily and weekly finance report jobs.")
    ap.add_argument("role", choices=("daily", "weekly"))
    ap.add_argument("--artifacts", required=True, type=Path, help="where the queue, alerts and snapshots are saved")
    args = ap.parse_args(argv)
    os.umask(0o077)
    run(args.role, args.artifacts)
    return 0


if __name__ == "__main__":
    sys.exit(main())

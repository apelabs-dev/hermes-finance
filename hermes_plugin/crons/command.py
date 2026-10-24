"""`hermes finance crons status|plan|apply`: the argparse tree and the three handlers.

Settings (plugins.entries.hermes-finance.settings): HERMES_FINANCE_APP_DIR, the finance app checkout the jobs run from
(else the environment variable of that name, else the checkout this plugin directory sits in); HERMES_FINANCE_DATA_DIR,
the data directory the entry scripts point the app at; HERMES_FINANCE_CRON_DELIVER, where a new job is delivered when
--deliver isn't given. The Settings page's "destination" label is not a delivery target and is never read here.

Exit codes: 0 done (or nothing to do); 1 refused or blocked: no job, script or state was changed (a refusal after
waiting for the lock leaves the lock file, nothing else); 2 usage; 3 the lock was busy, nothing written; 4 apply
stopped after writing something (run plan again: it shows where things stand)."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from .roles import BY_KEY, DAYS, ROLES, check_day, check_time

APP_DIR = "HERMES_FINANCE_APP_DIR"
DATA_DIR = "HERMES_FINANCE_DATA_DIR"
DELIVER = "HERMES_FINANCE_CRON_DELIVER"
STATE_KEY = "crons"
JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
PLUGIN_DIR = Path(__file__).resolve().parents[1]

DESCRIPTION = """\
The plugin's two report jobs in this Hermes profile: a daily one (06:30 by default) and a weekly one (Sunday 18:00),
each running the finance app's own pre-run steps before the agent writes the report. Times are local: Hermes applies
the profile's timezone setting, else the host's.

  status   what the jobs are now (read-only)
  plan     what apply would change, with a plan id (read-only)
  apply    carry out that plan: --plan-id and the same options as the plan

Jobs set up before this plugin are adopted, never duplicated: by the ID this profile recorded, by --adopt ROLE=ID, or
by their exact name, script and working directory. An adopted job keeps every field (schedule, delivery, prompt,
skills, model, paused or not, run history) unless an option changes one, and plan shows each change first."""


def _setting(ctx: Any, key: str) -> str | None:
    try:
        value = ctx.get_config(key)
    except Exception:
        value = None
    return value.strip() if isinstance(value, str) and value.strip() else None


class _AdoptAction(argparse.Action):
    """--adopt ROLE=JOB_ID, repeatable once per role: a second ID for the same role is a usage error."""

    def __call__(self, parser, namespace, value, option_string=None):
        items = list(getattr(namespace, self.dest, None) or [])
        if any(role == value[0] for role, _ in items):
            parser.error(f"--adopt {value[0]}= given twice: one job per role")
        items.append(value)
        setattr(namespace, self.dest, items)


def _role_id(text: str) -> tuple[str, str]:
    role, sep, job_id = str(text).partition("=")
    if not sep or role not in BY_KEY or not JOB_ID_RE.fullmatch(job_id):
        raise argparse.ArgumentTypeError(f"expected ROLE=JOB_ID with ROLE daily or weekly, got {text!r}")
    return role, job_id


def _typed(check):
    def parse(text: str) -> str:
        try:
            return check(text)
        except ValueError as e:
            raise argparse.ArgumentTypeError(str(e)) from None
    parse.__name__ = check.__name__.removeprefix("check_")
    return parse


def _timeout(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = -1.0
    if not 0 < value <= 600:
        raise argparse.ArgumentTypeError("a number of seconds, more than 0 and at most 600")
    return value


def _plan_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--deliver", metavar="PLATFORM:CHAT_ID",
                   help="where the reports go (e.g. discord:<channel id>): a new job's destination, or a change to an "
                        "existing job's (default for new jobs: the HERMES_FINANCE_CRON_DELIVER setting)")
    p.add_argument("--adopt", type=_role_id, action=_AdoptAction, default=[], metavar="ROLE=JOB_ID",
                   help="adopt this existing job as the daily or weekly job (repeatable)")
    p.add_argument("--daily-time", type=_typed(check_time), metavar="HH:MM",
                   help="the daily job's local time (new job default 06:30; an existing job keeps its own)")
    p.add_argument("--weekly-day", type=_typed(check_day), metavar="DAY", help=f"one of {', '.join(DAYS)} "
                                                                                "(new job default sun)")
    p.add_argument("--weekly-time", type=_typed(check_time), metavar="HH:MM", help="new job default 18:00")
    p.add_argument("--use-bundled-scripts", action="store_true",
                   help="point adopted jobs at the plugin's own entry scripts (installed by apply)")
    p.add_argument("--recreate", choices=[r.key for r in ROLES], action="append", default=[],
                   help="set up a new job for a role whose owned job was deleted")
    p.add_argument("--json", action="store_true", help="print JSON")


def setup(parser: argparse.ArgumentParser, ctx: Any) -> None:
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.description = DESCRIPTION
    sub = parser.add_subparsers(dest="crons_command", metavar="{status,plan,apply}")
    s = sub.add_parser("status", help="what the daily and weekly jobs are now (read-only)")
    s.add_argument("--json", action="store_true", help="print JSON")
    s.set_defaults(finance_handler=lambda args: status(ctx, args))
    p = sub.add_parser("plan", help="what apply would change, and its plan id (read-only)")
    _plan_arguments(p)
    p.set_defaults(finance_handler=lambda args: plan(ctx, args))
    a = sub.add_parser("apply", help="carry out a plan (its --plan-id, with the same options)")
    _plan_arguments(a)
    a.add_argument("--plan-id", required=True, help="the plan id `plan` printed")
    a.add_argument("--lock-timeout", type=_timeout, default=10.0, metavar="SECONDS",
                   help="how long to wait for another apply in this profile (default 10)")
    a.set_defaults(finance_handler=lambda args: apply(ctx, args))
    parser.set_defaults(finance_handler=lambda args: (parser.print_help(), 0)[1])


# --- the profile ---------------------------------------------------------------------------------------------------

def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()).expanduser().resolve()
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser().resolve()


def _app_dir(ctx: Any) -> tuple[Path | None, str | None]:
    configured = _setting(ctx, APP_DIR) or (os.environ.get(APP_DIR) or "").strip() or None
    where = f"the {APP_DIR} setting" if _setting(ctx, APP_DIR) else f"${APP_DIR}"
    if configured:
        root = Path(configured).expanduser()
        if not root.is_absolute():
            return None, f"{where} ({configured}) is not an absolute path"
    elif (PLUGIN_DIR.parent / "src" / "hermes_finance").is_dir():
        root, where = PLUGIN_DIR.parent, "the checkout this plugin sits in"
    else:
        return None, (f"set plugins.entries.hermes-finance.settings.{APP_DIR} to the hermes-finance checkout the jobs "
                      "should run")
    root = root.resolve()
    if not (root / "src" / "hermes_finance" / "cronpipe.py").is_file():
        return None, f"{root} ({where}) is not a hermes-finance checkout with src/hermes_finance/cronpipe.py"
    return root, None


def context(ctx: Any):
    from . import adapter, planner
    app_dir, problem = _app_dir(ctx)
    python = app_dir / ".venv" / "bin" / "python" if app_dir else None
    app_python = python if python and python.is_file() and os.access(python, os.X_OK) else None
    if app_dir and not app_python:
        problem = f"the app's Python ({python}) is missing: run `uv sync` in {app_dir}"
    data = _setting(ctx, DATA_DIR)
    data_dir, data_problem = (Path(data).expanduser() if data else None), None
    if data_dir and not data_dir.is_absolute():
        data_dir, data_problem = None, f"the {DATA_DIR} setting ({data}) is not an absolute path"
    return planner.Context(home=_home(), app_dir=app_dir, app_dir_problem=problem, app_python=app_python,
                           data_dir=data_dir, data_dir_problem=data_problem, setting_deliver=_setting(ctx, DELIVER),
                           known_platform=adapter.known_platform)


def options(args: argparse.Namespace):
    from .planner import Options
    return Options(deliver=args.deliver, adopt=tuple(sorted(dict(args.adopt).items())), daily_time=args.daily_time,
                   weekly_day=args.weekly_day, weekly_time=args.weekly_time,
                   use_bundled_scripts=bool(args.use_bundled_scripts), recreate=tuple(sorted(set(args.recreate))))


def state_path(ctx: Any) -> Path:
    return Path(ctx.state.path)


def lock_path(ctx: Any) -> Path:
    return Path(ctx.state.data_dir) / "crons.lock"


def read_state(ctx: Any) -> dict[str, Any]:
    """The plugin's cron state, read without PluginState.get (which creates its directory and a lock file)."""
    from .adapter import SchedulerUnavailable
    path = state_path(ctx)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise SchedulerUnavailable(f"the plugin state {path} can't be read ({e})") from None
    if not isinstance(data, dict):
        raise SchedulerUnavailable(f"the plugin state {path} is not a JSON object; nothing is planned from it")
    value = data.get(STATE_KEY, {})
    if not isinstance(value, dict) or not isinstance(value.get("roles", {}), dict) \
            or not isinstance(value.get("pending", {}), dict):
        raise SchedulerUnavailable(f"the plugin state {path} has an unreadable {STATE_KEY!r} entry; nothing is "
                                   "planned from it")
    return value


def _compute(ctx: Any, args: argparse.Namespace):
    """(context, plan). SchedulerUnavailable when Hermes's cron functions aren't usable: checked here, before any
    apply writes, so a missing scheduler refuses the whole apply rather than stopping it half-way."""
    from . import adapter, planner
    c = context(ctx)
    adapter.core()
    return c, planner.make_plan(c, options(args), adapter.read_jobs(c.home), read_state(ctx))


# --- output --------------------------------------------------------------------------------------------------------

def _emit(args: argparse.Namespace, payload: dict[str, Any], lines: list[str]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print("\n".join(lines))


def _job_line(job: dict[str, Any] | None, paused_note: str = " (paused: stays paused)") -> str:
    if not job:
        return ""
    paused = "" if job.get("enabled", True) else paused_note
    return (f"{job.get('name')!r}: {job.get('schedule')} -> {job.get('deliver')}, script {job.get('script')}"
            f"{paused}")


def _plan_lines(plan: dict[str, Any], command: str) -> list[str]:
    lines = [f"hermes finance crons {command} (profile {plan['profile']})"]
    for key, e in plan["roles"].items():
        a = e["action"]
        if a == "create":
            n = e["new"]
            lines.append(f"  {key:6}  create {n['name']!r}: {n['schedule']} -> {n['deliver']}, script {n['script']}, "
                         f"workdir {n['workdir']}")
        elif a == "blocked":
            lines.append(f"  {key:6}  BLOCKED: {e['reason']}")
        else:
            how = {"owned": "owned", "explicit": "--adopt", "signature": "matched by name, script and workdir",
                   "recovered": "created by an apply that stopped before recording it"}
            lines.append(f"  {key:6}  {a} {e['job_id']} ({how.get(e['how'], e['how'])}) {_job_line(e.get('job'))}")
            for name, ch in e["changes"].items():
                lines.append(f"          {name}: {ch['from']} -> {ch['to']}")
            if not e["changes"]:
                lines.append("          no field changes")
    for s in plan["scripts"]:
        lines.append(f"  script  {s['action']} {s['path']} (version {s['version']}, sha256 {s['sha256'][:12]})")
    lines += [f"  warning: {w}" for w in plan["warnings"]]
    lines.append(f"  unrelated jobs left alone: {plan['unrelated_jobs']}")
    return lines


# --- the handlers ----------------------------------------------------------------------------------------------------

def plan(ctx: Any, args: argparse.Namespace) -> int:
    from .adapter import SchedulerUnavailable
    try:
        _, p = _compute(ctx, args)
    except SchedulerUnavailable as e:
        _emit(args, {"command": "plan", "ok": False, "error": f"scheduler unavailable: {e}"},
              [f"scheduler unavailable: {e}"])
        return 1
    lines = _plan_lines(p, "plan")
    if p["ok"]:
        lines.append(f"plan id: {p['plan_id']}")
        lines.append(f"apply it: hermes finance crons apply --plan-id {p['plan_id']} (with the same options)")
    else:
        lines.append(f"blocked (plan id {p['plan_id']}): nothing can be applied until the above is resolved")
    _emit(args, {"command": "plan", **p}, lines)
    return 0 if p["ok"] else 1


def apply(ctx: Any, args: argparse.Namespace) -> int:
    from . import entry, reconcile
    from .adapter import SchedulerUnavailable
    holder: dict[str, Any] = {}

    def compute():
        holder["ctx"], p = _compute(ctx, args)
        return p

    def render(role):
        c = holder["ctx"]
        return entry.render(role, app_dir=c.app_dir, app_python=c.app_python, data_dir=c.data_dir,
                            artifacts=c.artifacts)

    def write_state(value):
        ctx.state.set(STATE_KEY, value)

    try:
        c = context(ctx)
        out = reconcile.apply(plan_id=args.plan_id, compute=compute, home=c.home, scripts_dir=c.scripts_dir,
                              render=render, read_state=lambda: read_state(ctx), write_state=write_state,
                              lock_path=lock_path(ctx), lock_timeout=args.lock_timeout, prompt=lambda r: r.prompt())
    except reconcile.Refused as e:
        _emit(args, {"command": "apply", "ok": False, "error": str(e), "plan_id": args.plan_id, **e.details},
              [f"refused: {e}"])
        return e.code
    except SchedulerUnavailable as e:
        _emit(args, {"command": "apply", "ok": False, "error": f"scheduler unavailable: {e}", "plan_id": args.plan_id},
              [f"refused: scheduler unavailable: {e}"])
        return reconcile.EXIT_REFUSED
    except reconcile.ApplyFailed as e:
        msg = (f"apply stopped part-way: {e}. Some changes may be made; `hermes finance crons plan` shows where things "
               "stand and a new apply reconciles from there (it adopts, never duplicates)")
        _emit(args, {"command": "apply", "ok": False, "error": msg, "plan_id": args.plan_id}, [msg])
        return reconcile.EXIT_FAILED
    except Exception as e:   # reconcile.apply turns anything after its first write into ApplyFailed
        msg = f"refused: {type(e).__name__}: {e} (before anything was written)"
        _emit(args, {"command": "apply", "ok": False, "error": msg, "plan_id": args.plan_id}, [msg])
        return reconcile.EXIT_REFUSED
    lines = [f"applied plan {out['plan_id']} (profile {c.home})"]
    for key, r in out["result"].items():
        lines.append(f"  {key:6}  {r['did']} {r['job_id']}")
    for s in out["scripts"]:
        backup = f" (previous copy: {s['backup']})" if s.get("backup") else ""
        lines.append(f"  script  {s['action']} {s['path']}{backup}")
    _emit(args, {"command": "apply", "profile": str(c.home), "state_file": str(state_path(ctx)), **out}, lines)
    return reconcile.EXIT_OK


def status(ctx: Any, args: argparse.Namespace) -> int:
    from . import adapter, entry, planner
    from .adapter import SchedulerUnavailable
    c = context(ctx)
    payload: dict[str, Any] = {"command": "status", "profile": str(c.home), "state_file": str(state_path(ctx)),
                               "lock_file": str(lock_path(ctx)), "roles": {}}
    lines = [f"hermes finance crons status (profile {c.home})"]
    try:
        jobs = adapter.read_jobs(c.home)
        state = read_state(ctx)
        adapter.core()
        payload["scheduler"] = "available"
    except SchedulerUnavailable as e:
        payload["scheduler"] = f"unavailable: {e}"
        _emit(args, payload, lines + [f"  scheduler unavailable: {e}"])
        return 1

    def routable(value: Any) -> bool | None:
        try:
            return planner.destination_problem(value, c.known_platform) is None
        except SchedulerUnavailable:
            return None

    by_id = {j["id"]: j for j in jobs}
    for role in ROLES:
        owned = ((state.get("roles") or {}).get(role.key) or {}).get("job_id")
        job = by_id.get(owned) if owned else None
        look_alikes = [j["id"] for j in jobs if planner.matches_signature(role, j, c) and j["id"] != owned]
        info: dict[str, Any] = {"owned_job_id": owned, "exists": job is not None,
                                "job": planner.summary(job) if job else None, "look_alikes": look_alikes}
        if job:
            info["routable"] = routable(job.get("deliver"))
            script = planner.norm_script(job.get("script"), c.scripts_dir)
            if script == role.script:
                recorded = (state.get("scripts") or {}).get(role.script, {}).get("sha256")
                try:
                    path = entry.target(c.scripts_dir, role)
                    current = entry.sha256(path.read_bytes()) if path.exists() else None
                except entry.ScriptProblem as e:
                    current = f"unsafe: {e}"
                except OSError as e:
                    current = f"unreadable: {e.strerror or e}"
                info["script"] = {"path": role.script, "installed_sha256": current, "recorded_sha256": recorded,
                                  "as_recorded": current == recorded}
            when = f"next run {job.get('next_run_at')}" if info["job"]["enabled"] else "no run until it's resumed"
            lines.append(f"  {role.key:6}  {owned} {_job_line(info['job'], ' (paused)')}; {when}"
                         + {True: "", False: "; destination NOT routable", None: "; destination unchecked"}[
                             info["routable"]])
        elif owned:
            lines.append(f"  {role.key:6}  owned job {owned} is gone (plan --recreate {role.key} sets up a new one)")
        else:
            lines.append(f"  {role.key:6}  not set up by this plugin"
                         + (f"; matching jobs: {', '.join(look_alikes)} (plan adopts)" if look_alikes else
                            " (plan shows what apply would do)"))
        payload["roles"][role.key] = info
    _emit(args, payload, lines)
    return 0

"""What `apply` would do, worked out from what's on disk. Read-only: no Hermes write, no lock, no state write.

For each role the job is, in this order:
  1. the one this profile already owns (the plugin state's job ID), if it still exists; a gone one blocks the role
     until --recreate says to set up a new one;
  2. else the one named by --adopt ROLE=ID, if it looks like that role's job (its name or script is the role's);
  3. else the one job matching the role's exact signature: name, script and working directory together (a first
     adoption), or the name, script and working directory an apply recorded as pending just before it created a job
     and stopped (a recovery, whatever the settings say now). Two or more is ambiguous: the role is blocked, nothing
     is written, and --adopt picks one;
  4. else a new job, which needs a routable destination (--deliver, else the HERMES_FINANCE_CRON_DELIVER setting).
An existing job keeps every field; only what an option asks for changes (--deliver, the schedule options,
--use-bundled-scripts), each shown as from/to. A paused job stays paused.

The plan id is a hash of the options, every action, and what they were decided from (the fields of each job the plan
looked at, the owned IDs, the scripts' current hashes). Apply recomputes it under the lock and refuses a different
one: anything that changed in between (another apply, a job edited by hand) makes the plan stale."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import entry
from .roles import BY_KEY, ROLES, SCRIPT_VERSION, Role, cron_expr, parse_expr

DESTINATION_RE = re.compile(r"([a-z][a-z0-9_]*):([^\s,]+)")
PLAN_VERSION = 1


@dataclass(frozen=True)
class Options:
    deliver: str | None = None
    adopt: tuple[tuple[str, str], ...] = ()          # (role, job ID)
    daily_time: str | None = None
    weekly_day: str | None = None
    weekly_time: str | None = None
    use_bundled_scripts: bool = False
    recreate: tuple[str, ...] = ()

    def canonical(self) -> dict[str, Any]:
        out = asdict(self)
        out["adopt"] = dict(sorted(self.adopt))
        out["recreate"] = sorted(set(self.recreate))
        return out


@dataclass(frozen=True)
class Context:
    home: Path                                       # the profile's HERMES_HOME (resolved)
    app_dir: Path | None                             # the finance app checkout the jobs run from
    app_dir_problem: str | None
    app_python: Path | None
    data_dir: Path | None
    data_dir_problem: str | None
    setting_deliver: str | None
    known_platform: Callable[[str], bool] = field(compare=False)

    @property
    def scripts_dir(self) -> Path:
        return self.home / "scripts"

    @property
    def artifacts(self) -> Path:
        return self.home / "cache" / "hermes-finance"


def destination_problem(value: str | None, known: Callable[[str], bool]) -> str | None:
    if value is None or not str(value).strip():
        return ("no destination: pass --deliver PLATFORM:CHAT_ID (or set the HERMES_FINANCE_CRON_DELIVER plugin "
                "setting)")
    m = DESTINATION_RE.fullmatch(str(value))
    if not m:
        return (f"destination {value!r} is not a routable target: use one PLATFORM:CHAT_ID, e.g. "
                "discord:<channel id> (not local, origin, all or a bare platform name)")
    if not known(m[1]):
        return f"destination {value!r}: Hermes can't deliver to a platform called {m[1]!r}"
    return None


def norm_script(value: Any, scripts_dir: Path) -> str | None:
    text = str(value).strip() if isinstance(value, str) else ""
    if not text:
        return None
    p = Path(text).expanduser()
    if p.is_absolute():
        for base in (scripts_dir, scripts_dir.resolve()):
            try:
                return p.relative_to(base).as_posix()
            except ValueError:
                continue
        return text
    return p.as_posix()


def norm_workdir(value: Any) -> str | None:
    text = str(value).strip() if isinstance(value, str) else ""
    return str(Path(text).expanduser().resolve()) if text else None


def expr_of(job: dict[str, Any]) -> str | None:
    s = job.get("schedule")
    return s.get("expr") if isinstance(s, dict) and s.get("kind") == "cron" else None


def summary(job: dict[str, Any]) -> dict[str, Any]:
    s = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
    return {"id": job.get("id"), "name": job.get("name"), "schedule": s.get("expr") or s.get("display"),
            "schedule_kind": s.get("kind"), "deliver": job.get("deliver"), "enabled": job.get("enabled", True) is not False,
            "state": job.get("state"), "paused_at": job.get("paused_at"), "script": job.get("script"),
            "workdir": job.get("workdir"), "skills": job.get("skills"), "next_run_at": job.get("next_run_at")}


def fingerprint(job: dict[str, Any]) -> dict[str, Any]:
    """The fields a plan is decided from (run bookkeeping such as next_run_at is left out: a job that fires between plan
    and apply doesn't make the plan stale, and apply never touches those fields)."""
    return {k: v for k, v in summary(job).items() if k != "next_run_at"}


def matches_signature(role: Role, job: dict[str, Any], ctx: Context) -> bool:
    return (ctx.app_dir is not None and job.get("name") in role.names
            and norm_script(job.get("script"), ctx.scripts_dir) in role.scripts
            and norm_workdir(job.get("workdir")) == str(ctx.app_dir))


def matches_pending(job: dict[str, Any], pending: dict[str, Any] | None, ctx: Context) -> bool:
    """The job an apply recorded as about to be created (name, script, working directory), then stopped."""
    return bool(pending) and job.get("name") == pending.get("name") \
        and norm_script(job.get("script"), ctx.scripts_dir) == pending.get("script") \
        and norm_workdir(job.get("workdir")) == norm_workdir(pending.get("workdir"))


def looks_like(role: Role, job: dict[str, Any], ctx: Context) -> bool:
    return job.get("name") in role.names or norm_script(job.get("script"), ctx.scripts_dir) in role.scripts


def wanted_schedule(role: Role, opts: Options, current: str | None, new: bool) -> tuple[str | None, str | None]:
    """(the expression to set, or None to keep the job's; a problem, or None)."""
    time_ = opts.weekly_time if role.weekly else opts.daily_time
    day = opts.weekly_day if role.weekly else None
    if new:
        return cron_expr(role, time_ or role.default_time, day or role.default_day), None
    if time_ is None and day is None:
        return None, None
    parsed = parse_expr(role, current) if current else None
    if parsed is None and (time_ is None or (role.weekly and day is None)):
        need = "--weekly-day and --weekly-time" if role.weekly else "--daily-time"
        return None, f"the job's schedule isn't a plain {role.key} time ({current or 'not a cron expression'}): give {need}"
    base_time, base_day = parsed or (None, None)
    return cron_expr(role, time_ or base_time, (day or base_day) if role.weekly else None), None


def make_plan(ctx: Context, opts: Options, jobs: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any]:
    by_id = {j["id"]: j for j in jobs}
    owned = {k: (v or {}).get("job_id") for k, v in (state.get("roles") or {}).items() if k in BY_KEY}
    explicit = dict(opts.adopt)
    claimed: dict[str, str] = {}                  # job ID -> the role that owns it or explicitly adopts it
    for key, job_id in list(owned.items()) + list(explicit.items()):
        if job_id:
            claimed.setdefault(job_id, key)
    roles: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    referenced: set[str] = set()
    scripts_needed: list[Role] = []

    def blocked(role: Role, reason: str, **extra: Any) -> dict[str, Any]:
        return {"action": "blocked", "job_id": extra.pop("job_id", None), "how": extra.pop("how", None), "changes": {},
                "reason": reason, **extra}

    pending = state.get("pending") or {}
    for role in ROLES:
        candidates = [j for j in jobs if matches_signature(role, j, ctx)
                      or matches_pending(j, pending.get(role.key), ctx)]
        referenced.update(j["id"] for j in candidates)
        owned_id, adopt_id = owned.get(role.key), explicit.get(role.key)
        job, how = None, None
        if owned_id and owned_id in by_id:
            if adopt_id and adopt_id != owned_id:
                roles[role.key] = blocked(role, f"this profile already owns job {owned_id} as the {role.key} job; "
                                                f"--adopt {role.key}={adopt_id} is refused", job_id=owned_id)
                continue
            job, how = by_id[owned_id], "owned"
        elif owned_id and role.key not in opts.recreate:
            roles[role.key] = blocked(role, f"the {role.key} job this profile owns ({owned_id}) is gone; pass "
                                            f"--recreate {role.key} to set up a new one", job_id=owned_id)
            continue
        elif adopt_id:
            job = by_id.get(adopt_id)
            if job is None:
                roles[role.key] = blocked(role, f"there is no job {adopt_id} to adopt")
                continue
            if claimed.get(adopt_id, role.key) != role.key:
                roles[role.key] = blocked(role, f"job {adopt_id} is already the {claimed[adopt_id]} job")
                continue
            if not looks_like(role, job, ctx):
                roles[role.key] = blocked(role, f"job {adopt_id} doesn't look like the {role.key} finance job (name "
                                                f"{job.get('name')!r}, script {job.get('script')!r}): not adopted",
                                          job_id=adopt_id)
                referenced.add(adopt_id)
                continue
            how = "explicit"
        else:
            free = [j for j in candidates if claimed.get(j["id"], role.key) == role.key]
            if len(free) > 1:
                ids = [j["id"] for j in free]
                roles[role.key] = blocked(role, f"{len(ids)} jobs match the {role.key} job's name, script and working "
                                                f"directory ({', '.join(ids)}); choose one with --adopt "
                                                f"{role.key}=ID", candidates=ids)
                continue
            if free:
                job = free[0]
                how = "signature" if matches_signature(role, job, ctx) else "recovered"
        if job is not None:
            referenced.add(job["id"])
            others = [j["id"] for j in candidates if j["id"] != job["id"]]
            if others:
                warnings.append(f"{role.key}: look-alike jobs left alone (not owned, not changed): {', '.join(others)}")
            entry_ = _existing(role, job, how, opts, ctx, warnings)
        else:
            entry_ = _new(role, opts, ctx)
        roles[role.key] = entry_
        final_script = (entry_.get("changes", {}).get("script", {}).get("to") or
                        (entry_.get("new") or {}).get("script") or
                        (norm_script(job.get("script"), ctx.scripts_dir) if job else None))
        if entry_["action"] != "blocked" and final_script == role.script:
            scripts_needed.append(role)

    scripts, script_problems, observed_scripts = [], [], {}
    for role in scripts_needed:
        try:
            if ctx.app_dir is None or ctx.app_python is None:
                raise entry.ScriptProblem(f"the {role.key} entry script runs the finance app: "
                                          f"{ctx.app_dir_problem or 'its Python is missing (run uv sync in the app)'}")
            if ctx.data_dir_problem:
                raise entry.ScriptProblem(f"the {role.key} entry script points the app at its data: "
                                          f"{ctx.data_dir_problem}")
            content = entry.render(role, app_dir=ctx.app_dir, app_python=ctx.app_python, data_dir=ctx.data_dir,
                                   artifacts=ctx.artifacts)
            got = entry.inspect(ctx.scripts_dir, role, content)
        except entry.ScriptProblem as e:
            script_problems.append(f"script {role.script}: {e}")
            continue
        observed_scripts[role.script] = got["current_sha256"]
        if got["action"] != "unchanged":
            scripts.append({k: got[k] for k in ("path", "action", "version", "sha256")})

    blocked_list = [f"{k}: {v['reason']}" for k, v in roles.items() if v["action"] == "blocked"] + script_problems
    if ctx.app_dir is None:
        warnings.append(f"no finance app directory ({ctx.app_dir_problem}): no job can be recognised by its "
                        "working directory")
    observed = {jid: fingerprint(by_id[jid]) for jid in sorted(referenced) if jid in by_id}
    # each role pinned by its decision and the job's fingerprint (not its run bookkeeping, e.g. next_run_at)
    pinned_roles = {k: {**v, "job": {f: x for f, x in v["job"].items() if f != "next_run_at"}} if v.get("job") else v
                    for k, v in roles.items()}
    pinned = {"v": PLAN_VERSION, "home": str(ctx.home), "options": opts.canonical(), "roles": pinned_roles,
              "observed": observed, "owned": owned, "pending": pending, "scripts": scripts,
              "observed_scripts": observed_scripts,
              "script_version": SCRIPT_VERSION, "blocked": blocked_list}
    plan_id = hashlib.sha256(json.dumps(pinned, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return {"plan_id": plan_id, "ok": not blocked_list, "profile": str(ctx.home), "roles": roles, "scripts": scripts,
            "blocked": blocked_list, "warnings": warnings,
            "unrelated_jobs": sum(1 for j in jobs if j["id"] not in referenced)}


def _existing(role: Role, job: dict[str, Any], how: str, opts: Options, ctx: Context,
              warnings: list[str]) -> dict[str, Any]:
    changes: dict[str, dict[str, Any]] = {}
    if opts.deliver is not None:
        problem = destination_problem(opts.deliver, ctx.known_platform)
        if problem:
            return {"action": "blocked", "job_id": job["id"], "how": how, "changes": {}, "reason": problem,
                    "job": summary(job)}
        if opts.deliver != job.get("deliver"):
            changes["deliver"] = {"from": job.get("deliver"), "to": opts.deliver}
    elif destination_problem(job.get("deliver"), ctx.known_platform):
        warnings.append(f"{role.key}: job {job['id']} delivers to {job.get('deliver')!r}, which isn't a routable "
                        "target; --deliver changes it")
    current = expr_of(job)
    expr, problem = wanted_schedule(role, opts, current, new=False)
    if problem:
        return {"action": "blocked", "job_id": job["id"], "how": how, "changes": {}, "reason": problem,
                "job": summary(job)}
    if expr and expr != current:
        changes["schedule"] = {"from": current or summary(job)["schedule"], "to": expr}
    if opts.use_bundled_scripts and norm_script(job.get("script"), ctx.scripts_dir) != role.script:
        changes["script"] = {"from": job.get("script"), "to": role.script}
    action = ("update" if changes else "keep") if how == "owned" else "adopt"
    return {"action": action, "job_id": job["id"], "how": how, "changes": changes, "job": summary(job)}


def _new(role: Role, opts: Options, ctx: Context) -> dict[str, Any]:
    deliver = opts.deliver if opts.deliver is not None else ctx.setting_deliver
    problem = destination_problem(deliver, ctx.known_platform)
    if problem:
        return {"action": "blocked", "job_id": None, "how": "new", "changes": {},
                "reason": f"a new {role.key} job needs a destination: {problem}"}
    if ctx.app_dir is None:
        return {"action": "blocked", "job_id": None, "how": "new", "changes": {},
                "reason": f"a new {role.key} job runs from the finance app's directory: {ctx.app_dir_problem}"}
    expr, _ = wanted_schedule(role, opts, None, new=True)
    prompt = role.prompt()
    return {"action": "create", "job_id": None, "how": "new", "changes": {},
            "new": {"name": role.name, "schedule": expr, "deliver": deliver, "script": role.script,
                    "workdir": str(ctx.app_dir), "skills": [],
                    "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}}

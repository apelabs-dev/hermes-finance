"""`apply`: the plan the operator saw, carried out under the profile's reconcile lock, every change read back.

  1. The plan is recomputed without the lock; a different plan id (stale) or a blocked plan is refused before anything
     is written, not even the lock file.
  2. The lock (plugin-data/<plugin>/crons.lock, one per profile) is taken, waiting at most --lock-timeout seconds;
     on a timeout nothing is written. Every apply in the profile goes through it, so the read / recheck / create or
     adopt / state write sequence below never interleaves with another one.
  3. Under the lock the plan is recomputed once more and must still have the same id.
  4. Entry scripts are installed (backup, atomic write, read back) and recorded.
  5. Each role: a new job is created with Hermes's own create_job, read back (get_job and jobs.json) and only then
     recorded as owned; an adopted or owned job gets update_job with exactly the planned fields, and is read back to
     check those changed and nothing else did (bar what Hermes derives from a schedule: schedule_display,
     next_run_at). Ownership is written per role, right after its readback.
A crash anywhere leaves at worst a created job that isn't recorded yet. Before create_job, its name, script and working
directory are written to the state as pending; the next plan finds the job by those (or by the role's signature) and
adopts it, so a retry never makes a second one, even if the settings changed in between."""

from __future__ import annotations

import contextlib
import copy
import fcntl
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import adapter, entry
from .roles import BY_KEY, ROLES, SCRIPT_VERSION

EXIT_OK, EXIT_REFUSED, EXIT_LOCK_TIMEOUT, EXIT_FAILED = 0, 1, 3, 4
DERIVED = {"schedule": {"schedule", "schedule_display", "next_run_at"}}


class Refused(Exception):
    """Nothing was written."""

    def __init__(self, message: str, code: int = EXIT_REFUSED, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details


class ApplyFailed(Exception):
    """Something was written before this: the next plan shows where things stand, and apply reconciles from there."""


@contextlib.contextmanager
def reconcile_lock(path: Path, timeout: float):
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise Refused(f"can't open the lock {path} ({e.strerror or e}); nothing was changed") from None
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Refused(f"another apply holds the lock {path} (waited {timeout:g}s); nothing was changed",
                                  EXIT_LOCK_TIMEOUT) from None
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _skills(job: dict[str, Any]) -> list[str]:
    raw = job.get("skills")
    items = [job.get("skill")] if raw is None else ([raw] if isinstance(raw, str) else list(raw))
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _value(job: dict[str, Any], key: str) -> Any:
    """A planned field as stored: the schedule by its expression, anything else as is."""
    return (job.get("schedule") or {}).get("expr") if key == "schedule" else job.get(key)


def unexpected_changes(before: dict[str, Any], after: dict[str, Any], changed: set[str]) -> list[str]:
    allowed = set(changed)
    for name in changed:
        allowed |= DERIVED.get(name, set())
    bad = []
    for key in sorted(set(before) | set(after)):
        if key in allowed or before.get(key) == after.get(key):
            continue
        if key in ("skill", "skills") and _skills(before) == _skills(after):
            continue      # Hermes aligns the legacy `skill` with `skills` on every update; the list itself is unchanged
        bad.append(key)
    return bad


def apply(*, plan_id: str, compute: Callable[[], dict[str, Any]], home: Path, scripts_dir: Path,
          render: Callable[[Any], bytes], read_state: Callable[[], dict[str, Any]],
          write_state: Callable[[dict[str, Any]], None], lock_path: Path, lock_timeout: float,
          prompt: Callable[[Any], str]) -> dict[str, Any]:
    plan = compute()          # compute() also checks the scheduler: SchedulerUnavailable here means nothing was written
    _check(plan, plan_id)
    with reconcile_lock(lock_path, lock_timeout):
        try:
            plan = compute()
        except adapter.SchedulerUnavailable as e:
            raise Refused(f"scheduler unavailable: {e}; nothing was changed") from None
        _check(plan, plan_id)
        try:
            return _carry_out(plan, plan_id, home, scripts_dir, render, read_state, write_state, prompt)
        except ApplyFailed:
            raise
        except Exception as e:   # whatever it is, something may be written by now: say so (exit 4), never "nothing"
            raise ApplyFailed(f"{type(e).__name__}: {e}") from None


def _check(plan: dict[str, Any], plan_id: str) -> None:
    if plan["plan_id"] != plan_id:
        raise Refused(f"stale plan: {plan_id} was planned, but what's on disk now plans {plan['plan_id']}; run plan "
                      "again (with the same options) and apply that", current_plan_id=plan["plan_id"])
    if not plan["ok"]:
        raise Refused("the plan is blocked: " + "; ".join(plan["blocked"]), current_plan_id=plan["plan_id"])


def _carry_out(plan, plan_id, home, scripts_dir, render, read_state, write_state, prompt) -> dict[str, Any]:
    state = copy.deepcopy(read_state()) or {}
    state.setdefault("schema", 1)
    state.setdefault("roles", {})
    state.setdefault("scripts", {})
    state.setdefault("pending", {})

    def save() -> None:
        write_state(state)
        if read_state() != state:
            raise ApplyFailed("the plugin state didn't read back as written")

    scripts_done = []
    for s in plan["scripts"]:
        role = BY_KEY[s["path"].split("/")[-1].removesuffix(".sh")]
        content = render(role)
        if entry.sha256(content) != s["sha256"]:
            raise ApplyFailed(f"{s['path']} renders differently than planned")
        try:
            got = entry.install(scripts_dir, role, content)
        except entry.ScriptProblem as e:
            raise ApplyFailed(str(e)) from None
        scripts_done.append(got)
        state["scripts"][s["path"]] = {"version": SCRIPT_VERSION, "sha256": got["sha256"], "installed_at": _now(),
                                       "plan_id": plan_id}
        save()

    result: dict[str, dict[str, Any]] = {}
    for role in ROLES:
        e = plan["roles"][role.key]
        if e["action"] == "create":
            spec = e["new"]
            text = prompt(role)
            # recorded before create_job: if the process dies between the two, the next plan recognises the job by
            # this exact name, script and working directory even if the settings changed in between
            state["pending"][role.key] = {"name": spec["name"], "script": spec["script"], "workdir": spec["workdir"],
                                          "since": _now(), "plan_id": plan_id}
            save()
            job = adapter.create(home, prompt=text, schedule=spec["schedule"], name=spec["name"],
                                 deliver=spec["deliver"], script=spec["script"], workdir=spec["workdir"])
            job_id = job["id"]
            back = adapter.get(home, job_id)
            stored = [j for j in adapter.read_jobs(home) if j["id"] == job_id]
            if not back or len(stored) != 1:
                raise ApplyFailed(f"job {job_id} was created but doesn't read back from {adapter.jobs_file(home)}")
            got = stored[0]
            want = {"name": spec["name"], "deliver": spec["deliver"], "script": spec["script"],
                    "workdir": spec["workdir"], "prompt": text}
            wrong = [k for k, v in want.items() if got.get(k) != v] + \
                ([] if (got.get("schedule") or {}).get("expr") == spec["schedule"] else ["schedule"])
            if wrong:
                raise ApplyFailed(f"job {job_id} was created but reads back different: {', '.join(wrong)}")
            state["roles"][role.key] = {"job_id": job_id, "how": "created", "since": _now(), "plan_id": plan_id}
            state["pending"].pop(role.key, None)
            save()
            result[role.key] = {"job_id": job_id, "did": "created"}
            continue
        job_id = e["job_id"]
        stored = [j for j in adapter.read_jobs(home) if j["id"] == job_id]
        if len(stored) != 1:
            raise ApplyFailed(f"job {job_id} is gone")
        before = stored[0]
        changes = e["changes"]
        if changes:
            updates = {k: v["to"] for k, v in changes.items()}
            if adapter.update(home, job_id, updates) is None:
                raise ApplyFailed(f"job {job_id} is gone")
            after = [j for j in adapter.read_jobs(home) if j["id"] == job_id]
            if len(after) != 1:
                raise ApplyFailed(f"job {job_id} doesn't read back after the update")
            after = after[0]
            wrong = [k for k, v in updates.items() if _value(after, k) != v]
            extra = unexpected_changes(before, after, set(updates))
            if wrong or extra:
                raise ApplyFailed(f"job {job_id} reads back wrong after the update (not as planned: "
                                  f"{', '.join(wrong) or '-'}; changed without being planned: {', '.join(extra) or '-'})")
        did = "adopted" if e["action"] == "adopt" else ("updated" if changes else "unchanged")
        owned = state["roles"].get(role.key) or {}
        if owned.get("job_id") != job_id or role.key in state["pending"]:
            state["roles"][role.key] = {"job_id": job_id, "how": "adopted", "since": _now(), "plan_id": plan_id}
            state["pending"].pop(role.key, None)
            save()
        elif changes:
            state["roles"][role.key] = dict(owned, plan_id=plan_id)
            save()
        result[role.key] = {"job_id": job_id, "did": did}

    final = read_state() or {}
    for key, r in result.items():
        if (final.get("roles") or {}).get(key, {}).get("job_id") != r["job_id"]:
            raise ApplyFailed(f"the plugin state doesn't record job {r['job_id']} as the {key} job")
    return {"ok": True, "plan_id": plan_id, "result": result, "scripts": scripts_done}

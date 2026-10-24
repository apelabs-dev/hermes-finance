"""The one place the plugin touches Hermes's scheduler.

Hermes documents no plugin API for cron jobs (there is no ctx.register_cron), so this uses the installed runtime's own
cron.jobs functions: create_job, update_job and get_job, pinned to the profile with use_cron_store. That's internal
coupling: every use first checks the functions and the parameters it passes are there, and says "scheduler unavailable"
otherwise. No job is ever written here any other way.

Reads are different: `status` and `plan` must not write a byte, and cron.jobs.load_jobs() can (it creates the cron
directories and rewrites a jobs.json it has to repair). So jobs are read by parsing jobs.json read-only, and a file that
isn't in the form Hermes itself writes ({"jobs": [...]}) is refused rather than guessed at: `hermes cron list` repairs
it. After every write, apply reads the job back both ways (get_job and this parse), which also proves the two agree on
where the profile's jobs live."""

from __future__ import annotations

import contextlib
import inspect
import json
from pathlib import Path
from typing import Any

REQUIRED = {
    "create_job": {"prompt", "schedule", "name", "deliver", "script", "workdir"},
    "update_job": {"job_id", "updates"},
    "get_job": {"job_id"},
}


class SchedulerUnavailable(Exception):
    """Hermes's scheduler can't be used from here (missing module or function, unreadable jobs.json): nothing is
    planned or changed."""


def jobs_file(home: Path) -> Path:
    return home / "cron" / "jobs.json"


def read_jobs(home: Path) -> list[dict[str, Any]]:
    """Every job in the profile, disabled ones included, exactly as stored. Read-only."""
    path = jobs_file(home)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as e:
        raise SchedulerUnavailable(f"can't read {path}: {e.strerror or e}") from None
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise SchedulerUnavailable(f"{path} isn't valid JSON; `hermes cron list` lets Hermes repair it") from None
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list) or not all(isinstance(j, dict) and isinstance(j.get("id"), str) for j in jobs):
        raise SchedulerUnavailable(f"{path} isn't in the form Hermes writes ({{\"jobs\": [...]}}); "
                                   "`hermes cron list` lets Hermes repair it")
    return jobs


def core():
    """Hermes's cron.jobs module, checked for what this adapter calls."""
    try:
        from cron import jobs
    except Exception as e:
        raise SchedulerUnavailable(f"Hermes's cron module can't be imported ({type(e).__name__}: {e})") from None
    for name, params in REQUIRED.items():
        fn = getattr(jobs, name, None)
        if not callable(fn):
            raise SchedulerUnavailable(f"this Hermes has no cron.jobs.{name}")
        signature = inspect.signature(fn).parameters
        takes_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.values())
        missing = set() if takes_any else params - set(signature)
        if missing:
            raise SchedulerUnavailable(f"this Hermes's cron.jobs.{name} lacks {', '.join(sorted(missing))}")
    return jobs


@contextlib.contextmanager
def _store(jobs, home: Path):
    pin = getattr(jobs, "use_cron_store", None)
    with (pin(home) if callable(pin) else contextlib.nullcontext()):
        yield


def known_platform(name: str) -> bool:
    """Whether Hermes can deliver to this platform (its own list, plus platform plugins that declare cron delivery)."""
    try:
        from cron import scheduler
    except Exception as e:
        raise SchedulerUnavailable(f"Hermes's cron scheduler can't be imported ({type(e).__name__}: {e})") from None
    check = getattr(scheduler, "_is_known_delivery_platform", None)
    if callable(check):
        return bool(check(name))
    known = getattr(scheduler, "_KNOWN_DELIVERY_PLATFORMS", None)
    if known is None:
        raise SchedulerUnavailable("can't tell which delivery platforms this Hermes knows")
    return name.lower() in known


def create(home: Path, **fields: Any) -> dict[str, Any]:
    jobs = core()
    with _store(jobs, home):
        return jobs.create_job(**fields)


def update(home: Path, job_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    jobs = core()
    with _store(jobs, home):
        return jobs.update_job(job_id, dict(updates))


def get(home: Path, job_id: str) -> dict[str, Any] | None:
    jobs = core()
    with _store(jobs, home):
        return jobs.get_job(job_id)

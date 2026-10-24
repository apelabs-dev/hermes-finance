"""The two report jobs the plugin owns (a "role" each), what a new one looks like, and the exact signature an older job
is recognised by on its first adoption.

A new job is named `hermes-finance-<role>`, runs the plugin's entry script `hermes-finance/<role>.sh` (under the
profile's scripts directory) from the app directory, and is delivered where the operator said. The legacy names and
scripts are this project's own earlier setup (scripts/finance_daily_prerun.sh and its weekly twin); a job is only
ever recognised by all three of name, script and working directory together, never by a word like "finance" in it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = "hermes-finance"   # the plugin's directory under $HERMES_HOME/scripts
SCRIPT_VERSION = 1              # the entry script template's version (in its header and the plugin state)
DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")   # cron day-of-week 0..6
TIME_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
PROMPTS = Path(__file__).resolve().parent / "prompts"


@dataclass(frozen=True)
class Role:
    key: str                       # "daily" | "weekly"
    name: str                      # a new job's name
    legacy_names: tuple[str, ...]
    legacy_scripts: tuple[str, ...]
    default_time: str              # HH:MM, local wall-clock time (Hermes supplies the timezone)
    default_day: str | None        # weekly only

    @property
    def script(self) -> str:
        return f"{SCRIPT_DIR}/{self.key}.sh"

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.legacy_names)

    @property
    def scripts(self) -> tuple[str, ...]:
        return (self.script, *self.legacy_scripts)

    @property
    def weekly(self) -> bool:
        return self.default_day is not None

    def prompt(self) -> str:
        return (PROMPTS / f"{self.key}.md").read_text(encoding="utf-8").strip()


DAILY = Role("daily", "hermes-finance-daily", ("finance-daily-classify",), ("finance_daily_prerun.sh",), "06:30", None)
WEEKLY = Role("weekly", "hermes-finance-weekly", ("finance-weekly-summary",), ("finance_weekly_prerun.sh",), "18:00",
              "sun")
ROLES = (DAILY, WEEKLY)
BY_KEY = {r.key: r for r in ROLES}


def check_time(value: str) -> str:
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        raise ValueError(f"{value!r} is not a time as HH:MM (24-hour)")
    return value


def check_day(value: str) -> str:
    if value not in DAYS:
        raise ValueError(f"{value!r} is not a day ({', '.join(DAYS)})")
    return value


def cron_expr(role: Role, time: str, day: str | None = None) -> str:
    """The cron expression for a local time (and, weekly, a day): the timezone is Hermes's (the profile's `timezone`,
    else the host's local time), not the plugin's."""
    m = TIME_RE.fullmatch(check_time(time))
    dow = str(DAYS.index(check_day(day))) if role.weekly else "*"
    return f"{int(m[2])} {int(m[1])} * * {dow}"


def parse_expr(role: Role, expr: str) -> tuple[str, str | None] | None:
    """(HH:MM, day or None) for an expression of the shape cron_expr writes, else None."""
    parts = str(expr or "").split()
    if len(parts) != 5 or parts[2:4] != ["*", "*"] or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    minute, hour = int(parts[0]), int(parts[1])
    if not (0 <= minute < 60 and 0 <= hour < 24):
        return None
    if role.weekly:
        if not (parts[4].isdigit() and 0 <= int(parts[4]) <= 6):
            return None
        return f"{hour:02d}:{minute:02d}", DAYS[int(parts[4])]
    return (f"{hour:02d}:{minute:02d}", None) if parts[4] == "*" else None

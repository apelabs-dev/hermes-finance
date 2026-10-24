"""The installed Hermes runtime, run against a disposable profile: a temp HOME whose .hermes holds a copy of the plugin,
its own config.yaml, and sentinel .env / auth.json files (fake values the tests look for in every output).

Every child process runs under macOS sandbox-exec with outbound IP traffic and DNS denied, writes denied to the real
~/.hermes, ~/.config/hermes-finance and this checkout, and reads denied to the real ~/.hermes/.env, ~/.hermes/auth.json
and ~/.config/hermes-finance. The real home is found from the password database, not $HOME, so nothing here depends
on a test's environment. Each child gets its own process group and is killed with it on a timeout."""

from __future__ import annotations

import hashlib
import functools
import tempfile
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ROOT / "hermes_plugin"
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
HERMES_AGENT = Path(os.environ.get("HERMES_FINANCE_TEST_HERMES_AGENT") or REAL_HOME / ".hermes" / "hermes-agent")
HERMES_PY = HERMES_AGENT / "venv" / "bin" / "python"
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
APP_PYTHON = ROOT / ".venv" / "bin" / "python"

SENTINEL_ENV = "hf-sentinel-env-5b1f0c7a"
SENTINEL_AUTH = "hf-sentinel-auth-9d2e71c4"
SENTINELS = (SENTINEL_ENV, SENTINEL_AUTH)
DESTINATION = "discord:100000000000000001"      # synthetic
OTHER_DESTINATION = "discord:100000000000000002"

SCRIPTS = Path("scripts") / "hermes-finance"   # the plugin's entry scripts, under the profile's scripts dir


def skip_reason() -> str | None:
    if not HERMES_PY.exists():
        return f"no Hermes runtime at {HERMES_AGENT}"
    if not SANDBOX_EXEC.exists():
        return "no sandbox-exec (the runtime tests deny network and real-profile access with it)"
    if not APP_PYTHON.exists():
        return "no app venv (uv sync --group dev --extra mcp)"
    return None


def sandbox_profile(scratch: Path | None = None) -> str:
    real = REAL_HOME
    checkout = f'(subpath "{ROOT}")'
    if scratch is not None and scratch.resolve().is_relative_to(ROOT / ".dispatch" / "tmp"):
        checkout = f'(require-all {checkout} (require-not (subpath "{scratch.resolve()}")))'
    return "\n".join([
        "(version 1)",
        "(allow default)",
        "(deny network-outbound (remote ip))",
        '(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))',   # no DNS
        f'(deny file-write* (subpath "{real / ".hermes"}") (subpath "{real / ".config" / "hermes-finance"}")'
        f' {checkout})',
        f'(deny file-read* (literal "{real / ".hermes" / ".env"}") (literal "{real / ".hermes" / "auth.json"}")'
        f' (subpath "{real / ".config" / "hermes-finance"}"))',
        "",
    ])


@dataclass
class Result:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def out(self) -> str:
        return self.stdout + self.stderr

    def json(self) -> Any:
        return json.loads(self.stdout)

    def __str__(self) -> str:
        return f"$ {' '.join(self.argv)}\n-> {self.returncode}\n{self.stdout}\n{self.stderr}"


class Child:
    """A running child in its own process group; always reaped (killpg) on exit."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path, stdin: str | None = None):
        self.argv = argv
        self.proc = subprocess.Popen(argv, env=env, cwd=cwd, text=True, start_new_session=True,
                                     stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.stdin = stdin

    def wait(self, timeout: float) -> Result:
        try:
            out, err = self.proc.communicate(self.stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            out, err = self.proc.communicate()
            raise AssertionError(f"timed out after {timeout}s: {' '.join(self.argv)}\n{out}\n{err}") from None
        finally:
            self.kill()
        return Result(self.argv, self.proc.returncode, out, err)

    def kill(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):   # gone, or a zombie waiting to be reaped
                pass
        else:   # the leader exited: still clear anything left in its group
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def __enter__(self) -> Child:
        return self

    def __exit__(self, *exc) -> None:
        self.kill()
        if self.proc.poll() is None:
            self.proc.wait(timeout=10)


class Profile:
    """One disposable Hermes profile. `enabled`: whether config.yaml lists the plugin in plugins.enabled."""

    def __init__(self, root: Path, *, enabled: bool = True, settings: dict[str, str] | None = None,
                 timezone: str | None = None, cron_mode: str = "approve", plugin: bool = True,
                 host_tz: str = "America/Los_Angeles", warm: bool = True):
        self.root = root
        self.host_tz = host_tz
        self.home = root / "home"
        self.hermes_home = self.home / ".hermes"
        self.data_dir = root / "data"
        self.hermes_home.mkdir(parents=True)
        (self.hermes_home / ".env").write_text(f"SENTINEL_SECRET={SENTINEL_ENV}\n")
        (self.hermes_home / "auth.json").write_text(json.dumps({"sentinel": SENTINEL_AUTH}))
        if plugin:
            shutil.copytree(PLUGIN_SRC, self.hermes_home / "plugins" / "hermes-finance",
                            ignore=shutil.ignore_patterns("__pycache__"))
        base = {"HERMES_FINANCE_APP_DIR": str(ROOT), "HERMES_FINANCE_DATA_DIR": str(self.data_dir)}
        base.update(settings or {})
        self.settings = {k: v for k, v in base.items() if v is not None}
        self.write_config(enabled=enabled, timezone=timezone, cron_mode=cron_mode)
        self.sandbox = root / "sandbox.sb"
        self.sandbox.write_text(sandbox_profile(root))
        if warm:   # Hermes's own first-run files (SOUL.md, cron/, sessions/, ...) exist before any test looks
            r = self.run("plugins", "list", "--plain")
            assert r.returncode == 0, str(r)

    def write_config(self, *, enabled: bool = True, timezone: str | None = None, cron_mode: str = "approve",
                     unattended_mode: str = "approve") -> None:
        """approvals: manual for people, and "approve" for cron and unattended platforms (the live profile's cron_mode;
        unattended_mode set the same way so the plugin can't lean on Hermes's default deny)."""
        lines = ["plugins:", f"  enabled: [{'hermes-finance' if enabled else ''}]", "  entries:",
                 "    hermes-finance:", "      settings:"]
        lines += [f"        {k}: {json.dumps(v)}" for k, v in self.settings.items()]
        lines += ["approvals:", "  mode: manual", f"  cron_mode: {cron_mode}", f"  unattended_mode: {unattended_mode}"]
        if timezone:
            lines.append(f"timezone: {timezone}")
        (self.hermes_home / "config.yaml").write_text("\n".join(lines) + "\n")

    # --- running things -------------------------------------------------------------------------------------------

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "HOME": str(self.home), "HERMES_HOME": str(self.hermes_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8", "TERM": "dumb", "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "TZ": self.host_tz, "TMPDIR": str(self.root / "tmp"), "UV_OFFLINE": "1",
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            # the finance app's own switches, as tests/conftest.py sets them: no Plaid, no Alpaca, no real key files
            "HERMES_PLAID": "off", "HERMES_ALPACA": "off", "HERMES_CHROME_DEVTOOLS": "off",
            "HERMES_ENV_FILE": str(self.root / "no-such.env"),
            "FINANCE_SECRETS_FILE": str(self.root / "no-such-secrets.env"),
            "HERMES_ALPACA_KEY_FILE": str(self.root / "no-such-alpaca.env"),
            "FINANCE_DATA_DIR": str(self.data_dir),
        }
        (self.root / "tmp").mkdir(exist_ok=True)
        env.update(extra or {})
        return env

    def _argv(self, argv: list[str]) -> list[str]:
        return [str(SANDBOX_EXEC), "-f", str(self.sandbox), *argv]

    def spawn(self, *argv: str, env: dict[str, str] | None = None, stdin: str | None = None,
              bytecode: bool = False) -> Child:
        """`hermes <argv>`, the installed runtime's own entry point (-B unless `bytecode`)."""
        code = "import sys; from hermes_cli.main import main; sys.argv[0] = 'hermes'; sys.exit(main())"
        flags = [] if bytecode else ["-B"]
        return Child(self._argv([str(HERMES_PY), *flags, "-c", code, *argv]), self.env(env), self.root, stdin)

    def run(self, *argv: str, timeout: float = 180, env: dict[str, str] | None = None, bytecode: bool = False) -> Result:
        return self.spawn(*argv, env=env, bytecode=bytecode).wait(timeout)

    def py(self, code: str, *args: str, timeout: float = 180, env: dict[str, str] | None = None) -> Result:
        """A Python snippet under the Hermes runtime (its cron and plugin modules importable), in this profile."""
        prelude = f"import sys; sys.path.insert(0, {str(HERMES_AGENT)!r})\n"
        return Child(self._argv([str(HERMES_PY), "-B", "-c", prelude + code, *args]), self.env(env),
                     self.root).wait(timeout)

    def py_json(self, code: str, *args: str, **kw: Any) -> Any:
        r = self.py(code, *args, **kw)
        assert r.returncode == 0, str(r)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def crons(self, *argv: str, **kw: Any) -> Result:
        return self.run("finance", "crons", *argv, **kw)

    # --- reading what's on disk (the test's own reads, never through the plugin) ----------------------------------

    @property
    def jobs_file(self) -> Path:
        return self.hermes_home / "cron" / "jobs.json"

    def jobs(self) -> list[dict[str, Any]]:
        if not self.jobs_file.exists():
            return []
        return json.loads(self.jobs_file.read_text())["jobs"]

    def job(self, job_id: str) -> dict[str, Any]:
        found = [j for j in self.jobs() if j["id"] == job_id]
        assert len(found) == 1, (job_id, [j["id"] for j in self.jobs()])
        return found[0]

    def plugin_data(self) -> Path:
        """The plugin's own data directory, as Hermes names it (PluginState: plugin-data/<namespace>), asked of the
        runtime itself rather than assumed."""
        if not hasattr(self, "_plugin_data"):
            self._plugin_data = Path(self.py_json(
                "import json\nfrom hermes_cli.plugins import PluginState, discover_plugins, get_plugin_manager\n"
                "discover_plugins()\nm = get_plugin_manager()._plugins['hermes-finance'].manifest\n"
                "print(json.dumps(str(PluginState(m.key or m.name, m.skill_namespace).data_dir)))"))
        return self._plugin_data

    def state_path(self) -> Path:
        return self.plugin_data() / "state.json"

    def lock_path(self) -> Path:
        return self.plugin_data() / "crons.lock"

    def state(self) -> dict[str, Any] | None:
        """The plugin's cron state (its PluginState key "crons"), or None."""
        path = self.state_path()
        return json.loads(path.read_text()).get("crons") if path.exists() else None

    def snapshot(self, *, skip_logs: bool = True) -> dict[str, str]:
        """relative path -> sha256 (or 'dir' / 'link -> target') of everything under the profile's .hermes."""
        out: dict[str, str] = {}
        for p in sorted(self.hermes_home.rglob("*")):
            rel = p.relative_to(self.hermes_home).as_posix()
            if skip_logs and (rel == "logs" or rel.startswith("logs/")):
                continue
            if p.is_symlink():
                out[rel] = f"link -> {os.readlink(p)}"
            elif p.is_dir():
                out[rel] = "dir"
            else:
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    def all_text(self) -> str:
        """Every file under the profile (logs included) and the data dir, as text: for the sentinel scan."""
        chunks = []
        for base in (self.hermes_home, self.data_dir):
            if not base.exists():
                continue
            for p in base.rglob("*"):
                if p.is_file() and not p.is_symlink() and p.name not in (".env", "auth.json"):
                    chunks.append(p.read_bytes().decode("utf-8", "replace"))
        return "\n".join(chunks)


# --- seeding jobs through the real cron API -------------------------------------------------------------------------

SEED = r"""
import json, sys
from cron import jobs
specs = json.loads(sys.argv[1])
out = []
for s in specs:
    extra = s.pop("extra", {})
    paused = s.pop("paused", False)
    job = jobs.create_job(**s)
    if extra:
        job = jobs.update_job(job["id"], extra)
    if paused:
        job = jobs.pause_job(job["id"], reason="paused by the owner")
    out.append(job["id"])
print(json.dumps(out))
"""


def seed(profile: Profile, specs: list[dict[str, Any]]) -> list[str]:
    return profile.py_json(SEED, json.dumps(specs))


def legacy_specs(workdir: str, *, destination: str = DESTINATION, daily_paused: bool = False) -> list[dict[str, Any]]:
    """The two legacy report jobs as this project's own scripts/finance_daily_prerun.sh era set them up, with run history
    and fields the plugin never owns, plus an unrelated roadmap coordinator that mentions finance everywhere."""
    history = {"last_run_at": "2026-09-29T06:30:04-07:00", "last_status": "ok", "failure_streak": 0,
               "repeat": {"times": None, "completed": 41}, "model": "synthetic-model-a", "provider": "synthetic-provider",
               "x_unknown_future_field": {"kept": True, "n": 3}}
    return [
        {"prompt": "Classify the queued finance transactions and report alerts (synthetic legacy prompt).",
         "schedule": "30 6 * * *", "name": "finance-daily-classify", "deliver": destination,
         "skills": ["personal-finance-aggregation"], "script": "finance_daily_prerun.sh", "workdir": workdir,
         "extra": dict(history), "paused": daily_paused},
        {"prompt": "Write the weekly finance summary (synthetic legacy prompt).", "schedule": "0 18 * * 0",
         "name": "finance-weekly-summary", "deliver": destination, "skills": ["personal-finance-aggregation"],
         "script": "finance_weekly_prerun.sh", "workdir": workdir,
         "extra": dict(history, last_run_at="2026-09-27T18:00:03-07:00")},
        {"prompt": "Resume the finance roadmap: check the finance builders, merge finance branches (synthetic).",
         "schedule": "every 20m", "name": "finance-roadmap-coordinator (through Batch D)", "deliver": destination,
         "workdir": workdir, "extra": {"last_run_at": "2026-09-30T06:40:00-07:00", "last_status": "ok"}},
    ]


def comparable(job: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(job, sort_keys=True))


def py_version_of(python: Path) -> tuple[int, int]:
    out = subprocess.run([str(python), "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
                         capture_output=True, text=True, timeout=60, check=True).stdout.split()
    return int(out[0]), int(out[1])


if __name__ == "__main__":   # a manual smoke run: python tests/hermes_runtime.py /tmp/somewhere
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Profile(Path(d))
        print(p.run(*(sys.argv[1:] or ["plugins", "list", "--plain"])))


@functools.lru_cache(maxsize=None)
def environment_reason(feature: str = "runtime") -> str | None:
    """Offline host-capability probes, never product test outcomes or connectivity requests."""
    if feature not in {"runtime", "interpreter", "scheduler_delivery"}:
        raise ValueError(f"unknown environment capability: {feature}")
    reason = skip_reason()
    if reason or feature == "interpreter":
        return reason
    scratch = ROOT / ".dispatch" / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="runtime-probe-", dir=scratch) as temp:
        profile = Profile(Path(temp), warm=False, plugin=False)
        result = profile.py(PROBE, feature, timeout=30)
    # An unexpected probe crash is a failure, never a reason to skip product assertions.
    assert result.returncode == 0, str(result)
    return json.loads(result.stdout.strip().splitlines()[-1])["reason"]


PROBE = r"""
import importlib, json, sys
reason = None
try:
    importlib.import_module('ruamel.yaml')
    importlib.import_module('hermes_cli.plugins')
    if sys.argv[1] == 'scheduler_delivery':
        scheduler = importlib.import_module('cron.scheduler')
        if not callable(getattr(scheduler, '_is_known_delivery_platform', None)) and getattr(scheduler, '_KNOWN_DELIVERY_PLATFORMS', None) is None:
            reason = 'installed Hermes runtime lacks cron.scheduler delivery-platform discovery; cron planning is unavailable and cannot produce plan_id'
except ModuleNotFoundError as exc:
    reason = f'installed Hermes runtime dependency {exc.name} is unavailable; dependency installation requires network, disabled for these tests'
print(json.dumps({'reason': reason}))
"""

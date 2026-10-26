"""Runtime configuration: Plaid credentials (from the Hermes .env file) and data paths.

Hermes mode (catalog rule 11): under the Hermes plugin (it calls use_hermes_credentials() with Hermes's get_secret) and
in its report jobs (their entry script sets HERMES_FINANCE_CREDENTIAL_SOURCE=hermes), the keys come from Hermes only:
the provider, else the environment Hermes runs the job with. Then no key file is opened or even looked at (the env
file, the secrets file, Alpaca's key file), and nothing falls back to them. Without either, the app reads its files
as before."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import redact

DEFAULT_ENV_FILE = Path.home() / ".hermes" / ".env"
# TASK-18: keys pasted in the app (first-run setup, Settings -> Replace keys, the AI key, Alpaca's) go to this file
# (0600, in a 0700 dir); FINANCE_SECRETS_FILE moves it. Its values win over the env file's, which keeps working as
# before for the installs that keep their keys there (secretstore.py).
DEFAULT_SECRETS_FILE = Path.home() / ".config" / "hermes-finance" / "secrets.env"
PLAID_KEYS = ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ENV")
SLOT_LIMIT = 10  # Plaid Trial plan: 10 Items for the life of the account; removals don't free slots.
# Who may open the app through a proxy login is a setting now (access.py: access.allowed_identities), seeded once from
# --allow-user / FINANCE_ALLOWED_USERS; there is no built-in default.


class ConfigError(Exception):
    pass


# --- Hermes mode (catalog rule 11) -------------------------------------------------------------------------------------
CREDENTIAL_SOURCE_ENV = "HERMES_FINANCE_CREDENTIAL_SOURCE"   # "hermes": the keys are in the environment Hermes set
HERMES_CREDENTIALS = ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ENV", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "APCA_ENV")
PLAID_NOT_CONFIGURED = ("Plaid not configured in Hermes (hermes config set PLAID_CLIENT_ID <client id> and hermes config "
                        "set PLAID_SECRET <secret>; PLAID_ENV defaults to production)")
_provider: Callable[[str], str | None] | None = None


def use_hermes_credentials(getter: Callable[[str], str | None] | None) -> None:
    """Read every key through `getter` (the plugin's wrapper of Hermes's get_secret), process-wide; None: stop."""
    global _provider
    _provider = getter


def hermes_credentials() -> bool:
    """True when the keys come from Hermes (a provider is set, or HERMES_FINANCE_CREDENTIAL_SOURCE=hermes)."""
    return _provider is not None or (os.environ.get(CREDENTIAL_SOURCE_ENV) or "").strip().lower() == "hermes"


def hermes_value(name: str) -> str | None:
    """A key from Hermes: the provider's answer, else (no provider) the environment. Blank, or a provider that raises:
    None (never a fallback to a file or, with a provider, to the environment)."""
    getter = _provider
    if getter is not None:
        try:
            value = getter(name)
        except Exception:
            return None
    else:
        value = os.environ.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True)
class Settings:
    client_id: str
    secret: str = field(repr=False)
    env: str = "production"


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser: skips blanks/comments, tolerates `export ` and quotes."""
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def env_list(name: str) -> list[str]:
    """A comma-separated environment variable as a list (blank entries dropped)."""
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def env_file_path() -> Path:
    return Path(os.environ.get("HERMES_ENV_FILE", DEFAULT_ENV_FILE))


def secrets_file_path() -> Path:
    return Path(os.environ.get("FINANCE_SECRETS_FILE") or DEFAULT_SECRETS_FILE)


def key_values() -> dict[str, str]:
    """The env file's values with the secrets file's on top (either may be missing). Hermes mode: Hermes's values."""
    if hermes_credentials():
        found = {name: hermes_value(name) for name in (*HERMES_CREDENTIALS, "AI_API_KEY")}
        return {k: v for k, v in found.items() if v}
    values: dict[str, str] = {}
    for path in (env_file_path(), secrets_file_path()):
        if path.exists():
            values.update({k: v for k, v in parse_env_file(path).items() if v})
    return values


def load_settings(path: Path | None = None) -> Settings:
    """Plaid credentials: from `path` alone when given, else the env file with the secrets file on top. Hermes mode:
    from Hermes only (no file is read, not even `path`)."""
    if hermes_credentials():
        values = {k: hermes_value(k) for k in PLAID_KEYS}
        if not values["PLAID_CLIENT_ID"] or not values["PLAID_SECRET"]:
            raise ConfigError(PLAID_NOT_CONFIGURED)
        settings = Settings(client_id=values["PLAID_CLIENT_ID"], secret=values["PLAID_SECRET"],
                            env=values["PLAID_ENV"] or "production")
        redact.register(settings.secret)
        return settings
    if path is not None or not secrets_file_path().exists():
        path = path or env_file_path()
        if not path.exists():
            raise ConfigError(f"Plaid credentials file not found: {path}")
        values = parse_env_file(path)
    else:
        values = key_values()
        path = secrets_file_path()
    missing = [k for k in PLAID_KEYS[:2] if not values.get(k)]
    if missing:
        raise ConfigError(f"missing {', '.join(missing)} in {path}")
    settings = Settings(
        client_id=values["PLAID_CLIENT_ID"],
        secret=values["PLAID_SECRET"],
        env=values.get("PLAID_ENV") or "production",
    )
    redact.register(settings.secret)
    return settings


# --- asset valuation (TASK-12, TASK-13) ----------------------------------------------------------------
# Every value / photo source is free and keyless. What we allow ourselves per calendar month (a lookup counts once;
# every call counts, failed ones too):
#   nhtsa   the free, rate-controlled VIN decoder (once per VIN)
#   carfax  carfax.com/value through the local Chrome (bot protection: kept tiny)
#   redfin  the Redfin Estimate (and, in the same lookup, the listing photo's address)
#   commons Wikimedia Commons search / image files for a car's photo (once per year, make and model)
#   osm     OpenStreetMap map tiles for a home without a listing photo (one per home)
API_CAPS = {"nhtsa": 300, "carfax": 6, "redfin": 20, "commons": 60, "osm": 20}
# A block (a 403, a captcha page, an answer without a price; for Commons a refusal) lasts this many days; NHTSA's lasts
# the rest of the month.
BLOCK_DAYS = {"carfax": 7, "redfin": 7, "commons": 1, "osm": 7}
# The keyless sources: Settings > Valuations switches them (prefs valuations.*); a flag in the env file wins
# (CARFAX_ENABLED=0/1, REDFIN_ENABLED, PHOTOS_ENABLED). TASK-18: a new install starts with Carfax and Redfin off (free
# public pages, not APIs: the owner turns them on); an install from before keeps them on (db._migrate_task18 saves
# its switches once). The photos stay on by default.
FREE_SOURCE_NAMES = {"carfax": "CARFAX_ENABLED", "redfin": "REDFIN_ENABLED", "photos": "PHOTOS_ENABLED"}
FREE_SOURCES_DEFAULT = {"carfax": False, "redfin": False, "photos": True}
OFF_WORDS = ("0", "false", "off", "no")


def _env_values(path: Path | None = None) -> dict[str, str]:
    if hermes_credentials():   # the switches only (not secrets), from the environment Hermes runs us with; no file
        return {name: os.environ[name] for name in FREE_SOURCE_NAMES.values() if name in os.environ}
    path = path or env_file_path()
    return parse_env_file(path) if path.exists() else {}


def free_sources(path: Path | None = None, conn=None) -> dict[str, bool]:
    """{source: on?} for the keyless sources (Carfax through the local Chrome, Redfin, the automatic photos). A flag
    set in the env file wins (CARFAX_ENABLED=0 ...); else the Settings page's switch (TASK-18: prefs valuations.*, read
    through `conn`); else the default."""
    values = _env_values(path)
    saved = {}
    if conn is not None:
        from . import prefs  # lazy: prefs imports db, which imports this module
        saved = prefs.valuation_switches(conn)
    out = {}
    for provider, name in FREE_SOURCE_NAMES.items():
        raw = (values.get(name) or "").strip().lower()
        default = saved.get(provider, FREE_SOURCES_DEFAULT[provider])
        out[provider] = default if not raw else raw not in OFF_WORDS
    return out


def free_source_overrides(path: Path | None = None) -> dict[str, str]:
    """{source: flag name} for the switches the env file sets (the Settings page shows them as set there)."""
    values = _env_values(path)
    return {p: name for p, name in FREE_SOURCE_NAMES.items() if (values.get(name) or "").strip()}


def api_caps() -> dict[str, int]:
    """Monthly call caps per provider. ConfigError when one is not a whole number >= 0 (then no call is made)."""
    caps = dict(API_CAPS)
    for provider, cap in caps.items():
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
            raise ConfigError(f"the {provider} monthly cap must be a whole number >= 0")
    return caps


def project_root() -> Path:
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").exists() else Path.cwd()


def data_dir() -> Path:
    return Path(os.environ.get("FINANCE_DATA_DIR", project_root() / "data"))


def ensure_data_dir(path: Path | None = None) -> Path:
    path = path or data_dir()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def db_path() -> Path:
    return data_dir() / "finance.db"


def token_backup_path() -> Path:
    return data_dir() / "tokens.backup.jsonl"


# TASK-19: the Alpaca live account's read-only API keys (alpaca.py). Never read by a test: conftest points
# HERMES_ALPACA_KEY_FILE at a file that doesn't exist.
DEFAULT_ALPACA_KEY_FILE = Path.home() / ".config" / "hermes-finance" / "alpaca-live.env"


def alpaca_key_path() -> Path:
    return Path(os.environ.get("HERMES_ALPACA_KEY_FILE") or DEFAULT_ALPACA_KEY_FILE)

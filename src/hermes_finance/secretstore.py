"""The secrets file (TASK-18): keys pasted in the app, kept out of the database and out of every answer.

    PLAID_CLIENT_ID / PLAID_SECRET / PLAID_ENV   first-run setup, Settings -> Connections -> Replace keys
    AI_API_KEY                                   Settings -> AI provider (an OpenAI-compatible endpoint's key)

Alpaca's read-only key is not kept here: it has its own 0600 file (config.alpaca_key_path(), written by alpaca.connect
after its one read-only check), and status() only says whether that file is there.

It lives at config.secrets_file_path() (default ~/.config/hermes-finance/secrets.env), mode 0600 in a 0700 directory,
written atomically (a temp file in the same directory, then a rename). Values are never returned to the browser, never
printed and never logged: status() says only whether a key is set, and every value read or written is registered with
redact so it's scrubbed from any output that could carry it. A backup of the database never includes this file.

Hermes mode (config.hermes_credentials(), catalog rule 11): the keys are kept in Hermes. Then this file is never opened
or looked at: read() is empty, write() is refused, and status() says what Hermes has.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from . import config, redact

PLAID = ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ENV")
AI_KEY = "AI_API_KEY"
KNOWN = (*PLAID, AI_KEY)
PLAID_ENVS = ("production", "sandbox")
MAX_VALUE = 4096
IN_HERMES = ("Keys are kept in Hermes here: set them with hermes config set (PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV) "
             "or the Hermes Desktop's plugin settings")


class SecretError(ValueError):
    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


def path() -> Path:
    return config.secrets_file_path()


def shown_path(p: Path | None = None) -> str:
    """The path as the page shows it: under the home directory as ~/..."""
    p = p or path()
    try:
        return "~/" + str(p.relative_to(Path.home()))
    except ValueError:
        return str(p)


def read() -> dict[str, str]:
    if config.hermes_credentials():
        return {}
    p = path()
    values = config.parse_env_file(p) if p.exists() else {}
    for k, v in values.items():
        if k != "PLAID_ENV":
            redact.register(v)
    return values


def _clean(key: str, value: str) -> str:
    if key not in KNOWN:
        raise SecretError(f"unknown key {key!r}")
    if not isinstance(value, str):
        raise SecretError(f"{key} must be text", key)
    value = value.strip()
    if not value:
        raise SecretError(f"{key} is empty", key)
    if len(value) > MAX_VALUE or any(c in value for c in "\r\n\0\"'") or value != value.split()[0]:
        raise SecretError(f"{key} doesn't look like a key (no spaces, quotes or line breaks)", key)
    if key == "PLAID_ENV" and value not in PLAID_ENVS:
        raise SecretError("PLAID_ENV must be production or sandbox", key)
    return value


def write(updates: dict[str, str | None]) -> None:
    """Set (a value) or remove (None) keys; every other line of the file is kept as it was."""
    if config.hermes_credentials():
        raise SecretError(IN_HERMES)
    clean = {k: (None if v is None else _clean(k, v)) for k, v in updates.items()}
    p = path()
    lines = p.read_text().splitlines() if p.exists() else ["# hermes-finance keys (0600). Written by the app; never shared."]
    out, done = [], set()
    for line in lines:
        key = line.strip().removeprefix("export ").partition("=")[0].strip()
        if key in clean and "=" in line and not line.lstrip().startswith("#"):
            if clean[key] is not None and key not in done:
                out.append(f"{key}={clean[key]}")
            done.add(key)
            continue
        out.append(line)
    out += [f"{k}={v}" for k, v in clean.items() if v is not None and k not in done]
    for k, v in clean.items():
        if v is not None and k != "PLAID_ENV":
            redact.register(v)
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except PermissionError:  # a directory that isn't ours (FINANCE_SECRETS_FILE pointed into /tmp): the file is 0600
        pass
    fd, tmp = tempfile.mkstemp(prefix=".secrets-", dir=p.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    os.chmod(p, 0o600)


def status() -> dict:
    """What the Settings page may know: which keys are set (never a value) and where they're kept."""
    if config.hermes_credentials():   # no file is looked at
        values = config.key_values()
        plaid_set = bool(values.get("PLAID_CLIENT_ID") and values.get("PLAID_SECRET"))
        return {
            "path": "Hermes",
            "plaid": {"set": plaid_set, "env": (values.get("PLAID_ENV") or "production") if plaid_set else None,
                      "where": "Hermes" if plaid_set else None},
            "ai_key": {"set": bool(values.get(AI_KEY)), "where": "Hermes"},
            "alpaca": {"set": bool(values.get("APCA_API_KEY_ID") and values.get("APCA_API_SECRET_KEY")),
                       "where": "Hermes"},
        }
    env_file = config.env_file_path()
    env_values = config.parse_env_file(env_file) if env_file.exists() else {}
    mine = read()
    values = config.key_values()
    plaid_set = bool(values.get("PLAID_CLIENT_ID") and values.get("PLAID_SECRET"))
    in_secrets = bool(mine.get("PLAID_CLIENT_ID") or mine.get("PLAID_SECRET"))
    return {
        "path": shown_path(),
        "plaid": {"set": plaid_set, "env": (values.get("PLAID_ENV") or "production") if plaid_set else None,
                  "where": shown_path() if in_secrets else shown_path(env_file) if env_values.get("PLAID_SECRET") else None},
        "ai_key": {"set": bool(values.get(AI_KEY)), "where": shown_path()},
        # TASK-20: the key file alpaca.py reads and its Connect form writes (never its contents)
        "alpaca": {"set": config.alpaca_key_path().exists(), "where": shown_path(config.alpaca_key_path())},
    }

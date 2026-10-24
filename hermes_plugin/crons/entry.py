"""The entry scripts: `$HERMES_HOME/scripts/hermes-finance/<role>.sh`, rendered from templates/entry.sh.

Hermes only runs a job's script from the profile's scripts directory, so that's where these go, and nowhere else: the
scripts directory, the plugin's subdirectory, its .backup/ and the script itself must each be what they look like (a
real directory, a real file; no symbolic link on the way), checked when planning and again, under the lock, when
installing, where every file is then created and renamed relative to an open directory (O_NOFOLLOW, O_EXCL), so a path
swapped for a link half-way can't send a write elsewhere. An installed script that differs from the rendered one is
replaced only by apply, which first copies it to .backup/, writes the new one atomically and reads it back. Rendering
bakes in the profile's own paths (the app directory, its Python, the data directory, the artifacts directory),
shell-quoted; the template itself has none."""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import shlex
import time
from pathlib import Path

from .roles import SCRIPT_DIR, SCRIPT_VERSION, Role

TEMPLATE = Path(__file__).resolve().parent / "templates" / "entry.sh"
BACKUPS = ".backup"


class ScriptProblem(Exception):
    """An entry script can't be written safely: nothing is planned or installed."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def render(role: Role, *, app_dir: Path, app_python: Path, data_dir: Path | None, artifacts: Path) -> bytes:
    data = (f"export FINANCE_DATA_DIR={shlex.quote(str(data_dir))}" if data_dir else
            "# FINANCE_DATA_DIR is not set (no HERMES_FINANCE_DATA_DIR plugin setting): the app's own default database")
    values = {"ROLE": role.key, "VERSION": str(SCRIPT_VERSION), "APP_DIR": shlex.quote(str(app_dir)),
              "APP_PYTHON": shlex.quote(str(app_python)), "ARTIFACTS": shlex.quote(str(artifacts)),
              "DATA_DIR_LINE": data}
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace(f"@@{key}@@", value)
    if "@@" in text:
        raise ScriptProblem(f"the entry script template has an unfilled placeholder: {TEMPLATE}")
    return text.encode("utf-8")


def target(scripts_dir: Path, role: Role) -> Path:
    """The script's path, once everything on the way checks out (read-only)."""
    plugin_dir = scripts_dir / SCRIPT_DIR
    path = scripts_dir / role.script
    for p, kind in ((scripts_dir, "directory"), (plugin_dir, "directory"), (plugin_dir / BACKUPS, "directory"),
                    (path, "file")):
        if p.is_symlink():
            raise ScriptProblem(f"script path {p} is a symbolic link: the entry scripts are only written inside the "
                                "profile's own scripts directory")
        if p.exists() and not (p.is_dir() if kind == "directory" else p.is_file()):
            raise ScriptProblem(f"script path {p} exists but is not a {kind}")
    root = scripts_dir.resolve()
    if not path.resolve().is_relative_to(root):
        raise ScriptProblem(f"script path {path} resolves outside {root}")
    return path


def inspect(scripts_dir: Path, role: Role, content: bytes) -> dict:
    """What installing `content` would do: install (missing), replace (differs) or unchanged. Read-only."""
    path = target(scripts_dir, role)
    current = sha256(path.read_bytes()) if path.exists() else None
    wanted = sha256(content)
    action = "install" if current is None else ("unchanged" if current == wanted else "replace")
    return {"path": role.script, "action": action, "version": SCRIPT_VERSION, "sha256": wanted,
            "current_sha256": current}


# --- install: every step relative to an open directory, never through a path that could be swapped for a link -----

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _subdir(parent_fd: int, name: str) -> int:
    """An open fd of the directory `name` in `parent_fd`, made (0700) if missing; a link there fails (ELOOP)."""
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return os.open(name, DIR_FLAGS, dir_fd=parent_fd)


def _read_at(dir_fd: int, name: str) -> bytes | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as f:
        return f.read()


def _write_new_at(dir_fd: int, name: str, data: bytes, mode: int) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
    with os.fdopen(fd, "wb") as f:
        os.fchmod(f.fileno(), mode)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


@contextlib.contextmanager
def _inside(dir_fd: int):
    """Run with the open directory as the working directory, so a rename by bare name lands in that directory itself
    (this Python has no os.replace(dir_fd=...) on every platform). The previous working directory comes back after."""
    here = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fchdir(dir_fd)
        yield
    finally:
        os.fchdir(here)
        os.close(here)


def install(scripts_dir: Path, role: Role, content: bytes) -> dict:
    """Write the script (0700) if it differs, keeping the old one in .backup/, then read it back. Call under the lock."""
    path = target(scripts_dir, role)
    name = path.name
    wanted = sha256(content)
    try:
        if not scripts_dir.exists():
            scripts_dir.mkdir(parents=True, mode=0o700)
        scripts_fd = os.open(scripts_dir, DIR_FLAGS)
        try:
            plugin_fd = _subdir(scripts_fd, SCRIPT_DIR)
            try:
                return _install_at(plugin_fd, name, role, content, wanted, path)
            finally:
                os.close(plugin_fd)
        finally:
            os.close(scripts_fd)
    except OSError as e:
        raise ScriptProblem(f"can't write {path} safely ({e.strerror or e})") from None


def _install_at(plugin_fd: int, name: str, role: Role, content: bytes, wanted: str, path: Path) -> dict:
    current = _read_at(plugin_fd, name)
    if current is not None and sha256(current) == wanted:
        return {"path": role.script, "action": "unchanged", "sha256": wanted, "backup": None}
    backup = None
    if current is not None:
        backups_fd = _subdir(plugin_fd, BACKUPS)
        try:
            backup = f"{name}.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.{sha256(current)[:8]}"
            _write_new_at(backups_fd, backup, current, 0o600)
        finally:
            os.close(backups_fd)
    tmp = f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    _write_new_at(plugin_fd, tmp, content, 0o700)
    try:
        with _inside(plugin_fd):
            os.replace(tmp, name)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=plugin_fd)
        raise
    got = _read_at(plugin_fd, name)
    if got is None or sha256(got) != wanted:
        raise ScriptProblem(f"{path} reads back as sha256 {sha256(got or b'')}, not the {wanted} just written")
    return {"path": role.script, "action": "replace" if current is not None else "install", "sha256": wanted,
            "backup": f"{SCRIPT_DIR}/{BACKUPS}/{backup}" if backup else None}

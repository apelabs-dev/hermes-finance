"""Backup and restore of the database: the one copy routine (`make_backup`, shared by GET /api/backup and `finance
backup`) and `restore`, for `finance restore`. See docs/backup-restore.md.

A backup is a consistent copy (SQLite's online backup API, so a running app's WAL is included and nothing has to stop)
with every credential taken out and the file vacuumed so nothing of them is left in its free pages: bank access
tokens, the built-in login (password hash, 2FA key, recovery codes, sessions, lockouts) and the MCP tokens. The secrets
file and Alpaca's key file are never part of it.

A restore never deletes anything: a database already in place is moved aside first (a timestamped `.pre-restore` file
next to it), and an existing one with data is only replaced with `force`.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config, db

STRIPPED_TOKEN = "removed-from-backup"
# What a backup takes out (and so what has to be set up again after restoring one)
STRIPPED_TABLES = ("auth_sessions", "auth_recovery_codes", "auth_failures", "auth_users", "mcp_tokens")
STRIPPED_SETTINGS = ("access.last_proxy", "mcp.last_used")
# Tables a hermes-finance database always has; a file without them is something else
REQUIRED_TABLES = {"items", "accounts", "transactions", "settings"}
# The tables whose rows make a database "not empty" (a fresh one has only seeded categories and defaults)
DATA_TABLES = ("items", "accounts", "transactions", "manual_accounts", "group_budgets", "goals")


def make_backup(conn: sqlite3.Connection, dest: Path) -> Path:
    """Write the backup of `conn`'s database to `dest` (0600; written beside it and renamed into place, so `dest` is
    either the whole backup or untouched). Returns `dest`."""
    dest = Path(dest)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        dst = sqlite3.connect(tmp)
        try:
            conn.backup(dst)
            dst.execute("PRAGMA secure_delete = ON")
            with dst:
                dst.execute("UPDATE items SET access_token = ?", (STRIPPED_TOKEN,))
                for table in STRIPPED_TABLES:
                    dst.execute(f"DELETE FROM {table}")
                dst.execute(f"DELETE FROM settings WHERE key IN ({', '.join('?' * len(STRIPPED_SETTINGS))})",
                            STRIPPED_SETTINGS)
            dst.execute("VACUUM")
        finally:
            dst.close()
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    finally:
        for p in (tmp, Path(f"{tmp}-wal"), Path(f"{tmp}-shm"), Path(f"{tmp}-journal")):
            p.unlink(missing_ok=True)
    return dest


def open_readonly(path: Path) -> sqlite3.Connection:
    """`path`, read-only, creating nothing next to it: `mode=ro` while its -wal is there (a database in use: its -shm
    is there too), else `immutable=1` (SQLite then needs no -shm, so none is created)."""
    path = Path(path)
    q = "mode=ro" if Path(f"{path}-wal").exists() else "immutable=1"
    conn = sqlite3.connect(f"file:{urllib.parse.quote(str(path.resolve()))}?{q}", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


class RestoreError(Exception):
    """Refused, nothing changed. The message says why and what to do."""


@dataclass
class Restored:
    path: Path
    schema_from: int
    schema_to: int
    moved_aside: Path | None
    stripped_items: list[str] = field(default_factory=list)    # institutions whose bank link has to be redone
    login_removed: bool = False                                 # the built-in login has to be set up again
    mcp_tokens_removed: bool = False
    access_mode: str = "local"
    missing_pictures: int = 0                                   # cached pictures whose file isn't in images/

    def redo(self) -> list[str]:
        """What to do now, one line each."""
        out = []
        if self.stripped_items:
            out.append(f"reconnect each bank (Accounts > Reconnect, or `finance serve` > Add account): "
                       f"{', '.join(self.stripped_items)}")
        if self.access_mode == "builtin" and self.login_removed:
            out.append("set the built-in login up again (Settings > Access & security), or `finance access mode local`")
        if self.mcp_tokens_removed:
            out.append("if other agents connected with MCP tokens, issue them new ones (Settings > Connect other agents)")
        if self.missing_pictures:
            out.append(f"{self.missing_pictures} cached car / home picture(s) aren't in {self.path.parent / 'images'}: "
                       "copy the old data directory's images/ folder there (a backup holds the database only)")
        out.append("the keys (Plaid, AI) and Alpaca's key file were never in the backup: `finance doctor` says which "
                   "are missing on this machine")
        return out


def _rows(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:   # a table an old schema doesn't have yet
        return 0


def _check_source(src: Path) -> tuple[int, sqlite3.Connection]:
    if not src.is_file():
        raise RestoreError(f"{src} is not a file")
    try:
        conn = open_readonly(src)
        result = [r[0] for r in conn.execute("PRAGMA integrity_check")]
        if result != ["ok"]:
            conn.close()
            raise RestoreError(f"{src} fails SQLite's integrity check ({result[0][:120]}): not restored")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.DatabaseError as e:
        raise RestoreError(f"{src} is not a readable SQLite database ({type(e).__name__}: {e}): not restored") from None
    if version > db.SCHEMA_VERSION:
        conn.close()
        raise RestoreError(f"{src} has schema v{version}, newer than this code's v{db.SCHEMA_VERSION}: upgrade the app "
                           "first (downgrades aren't supported)")
    if version < 1 or not REQUIRED_TABLES <= tables:
        conn.close()
        raise RestoreError(f"{src} is not a hermes-finance database (no {', '.join(sorted(REQUIRED_TABLES - tables)) or 'schema version'})")
    return version, conn


def _has_data(path: Path) -> bool:
    try:
        conn = open_readonly(path)
    except sqlite3.Error:
        return True   # unreadable: not ours to judge empty
    try:
        return any(_rows(conn, t) for t in DATA_TABLES)
    except sqlite3.DatabaseError:
        return True
    finally:
        conn.close()


def _quiesce(path: Path) -> None:
    """Fold the -wal into `path` and take it out of WAL mode, so the file alone is the whole database and nothing next
    to it would be replayed into what replaces it. Fails while another process has it open (`finance serve`)."""
    try:
        conn = sqlite3.connect(path, timeout=1)
    except sqlite3.Error as e:
        raise RestoreError(f"{path} can't be opened to move it aside ({e})") from None
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        mode = conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
    except sqlite3.Error as e:
        mode = f"error: {e}"
    finally:
        conn.close()
    if mode != "delete" or Path(f"{path}-wal").exists():
        raise RestoreError(f"{path} is in use (stop `finance serve` and anything else using it, then run the restore "
                           "again)")


def _aside_name(path: Path, now: datetime) -> Path:
    base = path.with_name(f"{path.name}.{now.strftime('%Y%m%d-%H%M%S')}.pre-restore")
    out, n = base, 1
    while out.exists():
        out, n = base.with_name(f"{base.name}.{n}"), n + 1
    return out


def restore(src: Path, force: bool = False, target: Path | None = None, now: datetime | None = None) -> Restored:
    """Replace the database (`target`, default config.db_path()) with the backup `src`. Checks first (integrity, a
    hermes-finance schema not newer than the code, the target empty or `force`), changing nothing when one fails
    (RestoreError). Then: a consistent copy of `src` beside the target, the old database (if any) moved aside, the copy
    renamed into place, and the result opened through db.connect so the migrations run."""
    src, target = Path(src), Path(target or config.db_path())
    if src.resolve() == target.resolve():
        raise RestoreError(f"{src} is the database itself")
    version, sconn = _check_source(src)
    try:
        if target.exists() and _has_data(target) and not force:
            raise RestoreError(f"{target} already holds data: run with --force to replace it (it's moved aside, "
                               "never deleted)")
        config.ensure_data_dir(target.parent)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.restore-", dir=target.parent)
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            dst = sqlite3.connect(tmp)
            try:
                sconn.backup(dst)   # a consistent snapshot, the source's -wal included; the source is never written
                dst.execute("PRAGMA journal_mode = DELETE")
                if [r[0] for r in dst.execute("PRAGMA integrity_check")] != ["ok"]:
                    raise RestoreError(f"the copy of {src} fails SQLite's integrity check: not restored")
            finally:
                dst.close()
            os.chmod(tmp, 0o600)
            aside = None
            if target.exists():
                _quiesce(target)
                aside = _aside_name(target, now or datetime.now())
                os.rename(target, aside)
                Path(f"{target}-shm").unlink(missing_ok=True)   # a stale index of the old file's WAL, never data
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
            Path(f"{tmp}-journal").unlink(missing_ok=True)
    finally:
        sconn.close()
    conn = db.connect(target)   # migrations run here (an older backup is upgraded)
    try:
        items = [r[0] or r[1] for r in conn.execute(
            "SELECT institution_name, item_id FROM items WHERE access_token = ? AND removed_at IS NULL "
            "ORDER BY institution_name, item_id", (STRIPPED_TOKEN,))]
        from . import access
        try:
            mode = access.load(conn, [], []).mode
        except ValueError:   # a saved public URL the access code refuses: `finance doctor` says so
            mode = "unknown"
        images = target.parent / "images"   # photos.images_dir(): next to the database
        pictures = [r[0] for r in conn.execute("SELECT file FROM asset_images WHERE status = 'ready' AND file IS NOT NULL")]
        return Restored(target, version, conn.execute("PRAGMA user_version").fetchone()[0], aside, items,
                        login_removed=_rows(conn, "auth_users") == 0, mcp_tokens_removed=_rows(conn, "mcp_tokens") == 0,
                        access_mode=mode, missing_pictures=sum(not (images / f).is_file() for f in pictures))
    finally:
        conn.close()

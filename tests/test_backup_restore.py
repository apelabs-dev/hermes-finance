"""Backup and restore (src/hermes_finance/backup.py): a synthetic database (the `full` gate DB plus credentials) is
backed up through GET /api/backup and through `finance backup`, each backup is restored with `finance restore` into a
fresh data directory, and the result must hold the same rows and the same read output, the credentials aside. Then the
refusals: a corrupt file, a newer schema, something that isn't ours, an existing database without --force, one in use.
The commands run as real subprocesses with a clean environment (no key, no real path)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from conftest import TOKEN_A, add_item
from hermes_finance import auth, backup, db, prefs
from hermes_finance.web import App

import gate_dbs

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 9, 25)
STRIPPED_TABLES = set(backup.STRIPPED_TABLES)
READS = (["networth", "--json", "--history"], ["budget-status", "--json", "--month", "2026-09"],
         ["txns", "--json", "--limit", "500"], ["networth", "--json", "--custom"])


def cli_env(tmp: Path, data: Path) -> dict:
    (tmp / "home").mkdir(exist_ok=True)
    return {"HOME": str(tmp / "home"), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src"),
            "HERMES_ENV_FILE": str(tmp / "no-env-file"), "FINANCE_SECRETS_FILE": str(tmp / "no-secrets-file"),
            "HERMES_ALPACA_KEY_FILE": str(tmp / "no-alpaca-file"), "HERMES_PLAID": "off", "HERMES_ALPACA": "off",
            "HERMES_CHROME_DEVTOOLS": "off", "FINANCE_DATA_DIR": str(data), "TZ": "UTC"}


def finance(tmp: Path, data: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "hermes_finance.cli", *args], env=cli_env(tmp, data), cwd=ROOT,
                          capture_output=True, text=True, timeout=120)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tables(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE "
                                       "'sqlite_%' ORDER BY name")]


def content(path: Path) -> dict[str, tuple[int, str]]:
    """Per table: (row count, a hash of its rows in a stable order), credentials as a backup leaves them."""
    conn = sqlite3.connect(path)
    try:
        out = {}
        for t in tables(conn):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
            rows = [dict(zip(cols, r)) for r in conn.execute(f"SELECT * FROM {t}")]
            if t == "items":
                for r in rows:
                    r["access_token"] = "<token>"
            if t == "settings":
                rows = [r for r in rows if r["key"] not in backup.STRIPPED_SETTINGS]
            digest = hashlib.sha256(json.dumps(sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=repr)),
                                               sort_keys=True, default=repr).encode()).hexdigest()
            out[t] = (len(rows), digest)
        return out
    finally:
        conn.close()


@pytest.fixture
def source(tmp_path):
    """The `full` gate database, plus a bank token, a built-in login, a session, an MCP token and the stripped
    settings: everything a backup takes out."""
    data = tmp_path / "src"
    path = data / "finance.db"
    gate_dbs.build_full(path, TODAY)
    conn = db.connect(path)
    add_item(conn, "item_z", "ins_z", name="Zeta Credit Union", token=TOKEN_A)
    with conn:
        conn.execute("INSERT INTO auth_users (username, password_hash, totp_secret, created_at, password_changed_at) "
                     "VALUES ('owner', '$argon2id$secret-hash', 'TOTPSECRETKEY', 'z', 'z')")
    auth.create_session(conn, 1)
    auth.mcp_token_create(conn, "Cursor")
    prefs.save(conn, {"access.mode": "builtin"}, allowed=["access.mode"])
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('mcp.last_used', '\"x\"', 'z')")
    conn.close()
    return data


def route_backup(src: Path, out: Path) -> Path:
    conn = db.connect(src / "finance.db")
    token, _ = auth.create_session(conn, 1)   # the built-in login is on: Settings > Backup is behind it
    conn.close()
    app = App(src / "finance.db", lambda: None, "127.0.0.1", 8765)
    app.today = lambda: TODAY
    status, ctype, body, extra = app.respond("GET", "/api/backup", {"Host": "127.0.0.1:8765",
                                                                    "Cookie": f"{auth.COOKIE}={token}"}, b"")
    assert status == 200 and ctype == "application/vnd.sqlite3"
    assert dict(extra)["Content-Disposition"] == 'attachment; filename="hermes-finance-backup-2026-09-25.db"'
    with body.open() as f:
        out.write_bytes(f.read())
    return out


def test_route_and_cli_backups_restore_to_the_same_data(source, tmp_path):
    original = content(source / "finance.db")
    assert original["transactions"][0] > 100 and original["items"][0] >= 4 and original["auth_users"][0] == 1
    before = {cmd[0] + str(i): finance(tmp_path, source, *cmd).stdout for i, cmd in enumerate(READS)}
    assert all(before.values())
    assert len(json.dumps(json.loads(before["txns2"]))) > 10000       # real content, not an empty answer

    via_route = route_backup(source, tmp_path / "route.db")
    r = finance(tmp_path, source, "backup", "--out", str(tmp_path / "cli.db"))
    assert r.returncode == 0, r.stderr
    via_cli = tmp_path / "cli.db"
    assert (os.stat(via_cli).st_mode & 0o777) == 0o600
    assert not list(tmp_path.glob(".cli.db.*"))                      # no temp file left beside it
    for f in (via_route, via_cli):
        raw = f.read_bytes()
        for secret in (TOKEN_A.encode(), b"TOTPSECRETKEY", b"secret-hash"):
            assert secret not in raw                                  # not even in free pages

    for name, f in (("route", via_route), ("cli", via_cli)):
        data = tmp_path / f"restored-{name}"
        r = finance(tmp_path, data, "restore", str(f))
        assert r.returncode == 0, r.stderr
        assert sorted(p.name for p in data.iterdir()) == ["finance.db"]   # nothing left beside it (no -wal, no temp)
        assert "reconnect each bank" in r.stdout and "Zeta Credit Union" in r.stdout
        assert "set the built-in login up again" in r.stdout and "finance doctor" in r.stdout
        got = content(data / "finance.db")
        assert set(got) == set(original)
        for t in original:
            if t in STRIPPED_TABLES:
                assert got[t][0] == 0, (name, t)
            else:
                assert got[t] == original[t], (name, t)
        conn = sqlite3.connect(data / "finance.db")
        assert {x[0] for x in conn.execute("SELECT access_token FROM items")} == {backup.STRIPPED_TOKEN}
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        conn.close()
        after = {cmd[0] + str(i): finance(tmp_path, data, *cmd).stdout for i, cmd in enumerate(READS)}
        assert after == before, name


def test_cli_backup_refuses_to_overwrite_and_needs_a_database(source, tmp_path):
    out = tmp_path / "exists.db"
    out.write_bytes(b"keep me")
    r = finance(tmp_path, source, "backup", "--out", str(out))
    assert r.returncode == 1 and "not overwritten" in r.stderr and out.read_bytes() == b"keep me"
    r = finance(tmp_path, tmp_path / "nothing-here", "backup", "--out", str(tmp_path / "x.db"))
    assert r.returncode == 1 and "no database" in r.stderr
    assert not (tmp_path / "nothing-here").exists() and not (tmp_path / "x.db").exists()
    outdir = tmp_path / "into"
    outdir.mkdir()
    # the CLI names the file by its own local date, and cli_env runs it with TZ=UTC (either side of a midnight)
    days = {datetime.now(timezone.utc).date().isoformat()}
    assert finance(tmp_path, source, "backup", "--out", str(outdir)).returncode == 0
    days.add(datetime.now(timezone.utc).date().isoformat())
    assert [p.name for p in outdir.iterdir()] in [[f"hermes-finance-backup-{d}.db"] for d in days]


@pytest.fixture
def good_backup(source, tmp_path):
    return route_backup(source, tmp_path / "good.db")


def refused(tmp_path, data: Path, path: Path, *extra: str) -> str:
    listing = sorted(p.name for p in data.iterdir()) if data.exists() else None
    r = finance(tmp_path, data, "restore", str(path), *extra)
    assert r.returncode == 1 and r.stderr.startswith("not restored: "), (r.stdout, r.stderr)
    assert (sorted(p.name for p in data.iterdir()) if data.exists() else None) == listing   # nothing changed
    return r.stderr


def test_refuses_a_corrupt_file(tmp_path, good_backup):
    junk = tmp_path / "junk.db"
    junk.write_bytes(os.urandom(8192))
    assert "not a readable SQLite database" in refused(tmp_path, tmp_path / "fresh", junk)
    # a real SQLite file with damaged pages: the integrity check catches what opening it doesn't
    damaged = tmp_path / "damaged.db"
    raw = bytearray(good_backup.read_bytes())
    page = int.from_bytes(raw[16:18], "big")
    for n in range(3, len(raw) // page, 7):
        raw[n * page + 8:n * page + 64] = os.urandom(56)
    damaged.write_bytes(bytes(raw))
    err = refused(tmp_path, tmp_path / "fresh", damaged)
    assert "integrity check" in err or "not a readable SQLite database" in err
    assert not (tmp_path / "fresh").exists()


def test_refuses_a_newer_schema(tmp_path, good_backup):
    newer = tmp_path / "newer.db"
    newer.write_bytes(good_backup.read_bytes())
    with sqlite3.connect(newer) as c:
        c.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    err = refused(tmp_path, tmp_path / "fresh", newer)
    assert f"schema v{db.SCHEMA_VERSION + 1}, newer than" in err and "downgrades" in err


def test_refuses_a_database_that_is_not_ours(tmp_path):
    other = tmp_path / "other.db"
    with sqlite3.connect(other) as c:
        c.execute("CREATE TABLE notes (x)")
        c.execute("PRAGMA user_version = 3")
    assert "not a hermes-finance database" in refused(tmp_path, tmp_path / "fresh", other)


def test_an_existing_database_needs_force_and_is_moved_aside(tmp_path, source, good_backup):
    target = source / "finance.db"
    old, old_content = sha(target), content(target)
    err = refused(tmp_path, source, good_backup)
    assert "--force" in err and sha(target) == old
    r = finance(tmp_path, source, "restore", "--force", str(good_backup))
    assert r.returncode == 0, r.stderr
    aside = [p for p in source.iterdir() if p.name.endswith(".pre-restore")]
    assert len(aside) == 1 and aside[0].name.startswith("finance.db.") and str(aside[0]) in r.stdout
    assert content(aside[0]) == old_content                          # the old database, whole, never deleted
    assert content(target) != content(aside[0])                      # replaced: its credentials are gone
    conn = sqlite3.connect(target)
    assert conn.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0] == 0
    conn.close()


def test_an_empty_database_is_replaced_without_force_and_still_kept(tmp_path, good_backup):
    data = tmp_path / "fresh"
    assert finance(tmp_path, data, "status").returncode in (0, 1)   # a new install's empty database
    assert (data / "finance.db").exists()
    r = finance(tmp_path, data, "restore", str(good_backup))
    assert r.returncode == 0, r.stderr
    assert len([p for p in data.iterdir() if p.name.endswith(".pre-restore")]) == 1


def test_a_database_in_use_is_not_replaced(tmp_path, source, good_backup):
    target = source / "finance.db"
    live = db.connect(target)                    # as `finance serve` holds it: WAL, open
    try:
        live.execute("SELECT COUNT(*) FROM transactions").fetchone()
        before = sha(target)
        with pytest.raises(backup.RestoreError, match="in use"):
            backup.restore(good_backup, force=True, target=target, now=datetime(2026, 10, 1, 12, 0, 0))
        assert sha(target) == before and not [p for p in source.iterdir() if "pre-restore" in p.name or
                                              p.name.startswith(".finance.db.restore-")]
    finally:
        live.close()


def test_refuses_restoring_the_database_onto_itself(tmp_path, source):
    with pytest.raises(backup.RestoreError, match="the database itself"):
        backup.restore(source / "finance.db", force=True, target=source / "finance.db")

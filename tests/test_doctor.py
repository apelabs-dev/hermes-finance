"""`finance doctor` (src/hermes_finance/doctor.py), run as the real command in a subprocess with a clean environment:
HOME, the env file, the secrets file, Alpaca's key file, the data directory and Hermes's home all under tmp_path, so
nothing of this machine's is read. The keys are sentinels: none may come back in the output."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from hermes_finance import db, doctor  # noqa: E402

NAMES = [n for n, _ in doctor.INTEGRATIONS]
SENTINELS = {
    "PLAID_CLIENT_ID": "SENTINEL-plaid-id-7f3a",
    "PLAID_SECRET": "SENTINEL-plaid-secret-91c2",
    "AI_API_KEY": "SENTINEL-ai-key-55d0",
    "APCA_API_KEY_ID": "AKSENTINEL-alpaca-id-0b1e",
    "APCA_API_SECRET_KEY": "SENTINEL-alpaca-secret-c4d8",
}


def base_env(tmp: Path) -> dict:
    """Only what the command needs: no key, no real path of this machine."""
    (tmp / "home").mkdir(exist_ok=True)
    return {"HOME": str(tmp / "home"), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src"),
            "HERMES_ENV_FILE": str(tmp / "no-env-file"), "FINANCE_SECRETS_FILE": str(tmp / "no-secrets-file"),
            "HERMES_ALPACA_KEY_FILE": str(tmp / "no-alpaca-file"), "FINANCE_DATA_DIR": str(tmp / "data"),
            "HERMES_HOME": str(tmp / "no-hermes")}


def doctor_run(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "hermes_finance.cli", "doctor", *args], env=env, cwd=ROOT,
                          capture_output=True, text=True, timeout=60)


def report(env: dict) -> tuple[dict, subprocess.CompletedProcess, subprocess.CompletedProcess]:
    text, js = doctor_run(env), doctor_run(env, "--json")
    assert text.returncode == js.returncode, (text, js)
    return json.loads(js.stdout), text, js


def make_db(env: dict) -> Path:
    """A real database, made by the db layer (as `finance status` would)."""
    subprocess.run([sys.executable, "-c", "from hermes_finance import db; db.connect().close()"], env=env, cwd=ROOT,
                   check=True, timeout=60)
    return Path(env["FINANCE_DATA_DIR"]) / "finance.db"


def write(path: Path, values: dict) -> None:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    path.chmod(0o600)


def everything_set(tmp: Path) -> dict:
    """Every integration set up, with sentinel keys."""
    env = base_env(tmp)
    env["FINANCE_SECRETS_FILE"] = str(tmp / "secrets.env")
    write(tmp / "secrets.env", {k: SENTINELS[k] for k in ("PLAID_CLIENT_ID", "PLAID_SECRET", "AI_API_KEY")}
          | {"PLAID_ENV": "production"})
    env["HERMES_ENV_FILE"] = str(tmp / "hermes.env")
    write(tmp / "hermes.env", {"CARFAX_ENABLED": "1", "REDFIN_ENABLED": "1", "PHOTOS_ENABLED": "1"})
    env["HERMES_ALPACA_KEY_FILE"] = str(tmp / "alpaca-live.env")
    write(tmp / "alpaca-live.env", {"APCA_API_KEY_ID": SENTINELS["APCA_API_KEY_ID"],
                                    "APCA_API_SECRET_KEY": SENTINELS["APCA_API_SECRET_KEY"], "APCA_ENV": "live"})
    exe = tmp / "hermes-bin"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    env["FINANCE_HERMES_BIN"] = str(exe)
    path = make_db(env)
    if not _have_mcp():   # the optional package isn't in this venv: MCP switched off, which is a fine state too
        with sqlite3.connect(path) as c:
            c.execute("INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES ('mcp.enabled', 'false', '')")
    return env


def _have_mcp() -> bool:
    import importlib.util
    return importlib.util.find_spec("mcp") is not None


def assert_no_leak(*outs: str) -> None:
    for out in outs:
        for k, v in SENTINELS.items():
            assert v not in out and v[:8] not in out and v[-6:] not in out, (k, out)


def by_name(rep: dict) -> dict:
    return {c["name"]: c for c in rep["integrations"]}


def test_empty_environment(tmp_path):
    env = base_env(tmp_path)
    rep, text, js = report(env)
    assert text.returncode == 1 and not text.stderr and not js.stderr
    assert rep["schema"] == doctor.SCHEMA == 1 and rep["ok"] is False
    assert [c["name"] for c in rep["integrations"]] == NAMES
    s = by_name(rep)
    assert {n: c["status"] for n, c in s.items()} == {
        "database": "missing", "plaid": "missing", "ai": "missing", "login": "ok", "alpaca": "missing",
        "mcp": "ok" if _have_mcp() else "missing", "carfax": "off", "redfin": "off", "photos": "ok"}
    assert rep["missing"] == [n for n in NAMES if s[n]["status"] in ("missing", "error")]
    assert s["plaid"]["missing_keys"] == ["PLAID_CLIENT_ID", "PLAID_SECRET"]
    assert s["ai"]["missing_keys"] == ["FINANCE_HERMES_BIN"]
    assert rep["files"]["schema_version"] is None and rep["files"]["code_schema_version"] == db.SCHEMA_VERSION
    out = text.stdout.splitlines()
    assert len(out) == len(NAMES)
    for line, c in zip(out, rep["integrations"]):
        if c["status"] == "ok":
            assert line == f"ok {c['name']}"
        else:
            assert line == f"{c['status']} {c['name']}: {c['detail']} -> README.md#{dict(doctor.INTEGRATIONS)[c['name']]}"
    assert "missing plaid: PLAID_CLIENT_ID and PLAID_SECRET not set" in text.stdout
    # read-only: no data directory, no database, nothing at all was created
    assert sorted(p.name for p in tmp_path.iterdir()) == ["home"] and not any((tmp_path / "home").iterdir())


def test_everything_set_exits_0_and_prints_no_secret(tmp_path):
    env = everything_set(tmp_path)
    dbfile = Path(env["FINANCE_DATA_DIR"]) / "finance.db"
    before = hashlib.sha256(dbfile.read_bytes()).hexdigest()
    listing = sorted(p.name for p in dbfile.parent.iterdir())
    rep, text, js = report(env)
    assert text.returncode == 0, text.stdout
    assert rep["ok"] is True and rep["missing"] == []
    assert all(c["status"] in ("ok", "off") for c in rep["integrations"])
    assert by_name(rep)["plaid"]["status"] == "ok" and by_name(rep)["alpaca"]["status"] == "ok"
    assert by_name(rep)["carfax"]["status"] == "ok" and by_name(rep)["ai"]["status"] == "ok"
    assert text.stdout.splitlines()[:5] == [f"ok {n}" for n in NAMES[:5]]
    assert_no_leak(text.stdout, text.stderr, js.stdout, js.stderr)
    assert rep["files"]["schema_version"] == db.SCHEMA_VERSION
    # the database is untouched, and no -wal / -shm was left behind
    assert hashlib.sha256(dbfile.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in dbfile.parent.iterdir()) == listing


@pytest.mark.parametrize("name", ["plaid", "ai", "alpaca", "database"])
def test_one_integration_missing_is_the_only_one_listed(tmp_path, name):
    env = everything_set(tmp_path)
    if name == "plaid":
        write(Path(env["FINANCE_SECRETS_FILE"]), {"AI_API_KEY": SENTINELS["AI_API_KEY"]})
    elif name == "ai":
        env["FINANCE_HERMES_BIN"] = str(tmp_path / "not-there")
    elif name == "alpaca":
        Path(env["HERMES_ALPACA_KEY_FILE"]).unlink()
    else:
        env["FINANCE_DATA_DIR"] = str(tmp_path / "elsewhere")
    rep, text, js = report(env)
    assert text.returncode == 1
    assert rep["missing"] == [name] and rep["ok"] is False
    bad = [line for line in text.stdout.splitlines() if not line.startswith(("ok ", "off "))]
    assert len(bad) == 1 and bad[0].startswith(f"missing {name}: ")
    assert bad[0].endswith(f" -> README.md#{dict(doctor.INTEGRATIONS)[name]}")
    assert_no_leak(text.stdout, js.stdout)


def test_bad_values_are_never_echoed(tmp_path):
    env = everything_set(tmp_path)
    write(Path(env["FINANCE_SECRETS_FILE"]), {"PLAID_CLIENT_ID": SENTINELS["PLAID_CLIENT_ID"],
                                              "PLAID_SECRET": SENTINELS["PLAID_SECRET"],
                                              "PLAID_ENV": "SENTINEL-env-e9"})
    write(Path(env["HERMES_ALPACA_KEY_FILE"]), {"APCA_API_KEY_ID": "PK" + SENTINELS["APCA_API_KEY_ID"],
                                                "APCA_API_SECRET_KEY": SENTINELS["APCA_API_SECRET_KEY"],
                                                "APCA_ENV": "live"})   # a paper key: refused
    rep, text, js = report(env)
    assert text.returncode == 1
    s = by_name(rep)
    assert s["plaid"]["status"] == "missing" and "PLAID_ENV" in s["plaid"]["missing_keys"]
    assert s["alpaca"]["status"] == "error"
    for out in (text.stdout, js.stdout):
        assert "SENTINEL-env" not in out
    assert_no_leak(text.stdout, js.stdout)


def test_database_states(tmp_path):
    env = everything_set(tmp_path)
    path = Path(env["FINANCE_DATA_DIR"]) / "finance.db"
    with sqlite3.connect(path) as c:
        c.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    rep, text, _ = report(env)
    assert text.returncode == 1 and rep["missing"] == ["database"]
    assert by_name(rep)["database"]["status"] == "error" and "newer" in by_name(rep)["database"]["detail"]
    path.write_bytes(b"not a database at all, just bytes" * 200)
    rep, text, _ = report(env)
    assert by_name(rep)["database"]["status"] == "error" and rep["missing"] == ["database"]


def test_json_shape_is_pinned(tmp_path):
    rep, _, _ = report(base_env(tmp_path))
    assert set(rep) == {"schema", "ok", "missing", "integrations", "files"}
    assert set(rep["files"]) == {"env_file", "secrets_file", "data_dir", "db_path", "schema_version",
                                 "code_schema_version"}
    for c in rep["integrations"]:
        assert set(c) == {"name", "status", "detail", "keys", "missing_keys", "optional", "fix"}
        assert c["status"] in ("ok", "off", "missing", "error")
        assert c["optional"] == (c["name"] in doctor.OPTIONAL)
        assert c["detail"].isascii() and " -> " not in c["detail"]


def test_bad_usage_exits_2(tmp_path):
    assert doctor_run(base_env(tmp_path), "--bogus").returncode == 2


def github_slugs(markdown: str) -> list[str]:
    """GitHub's heading anchors: lowercased, punctuation dropped, spaces to hyphens, -1/-2... for repeats."""
    seen: dict[str, int] = {}
    out = []
    in_code = False
    for line in markdown.splitlines():
        if line.startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r"^#{1,6} +(.+?) *#*$", line)
        if not m:
            continue
        text = re.sub(r"`|\*\*|\[([^\]]*)\]\([^)]*\)", lambda g: g.group(1) or "", m.group(1)).lower()
        slug = "".join(ch for ch in text if ch in " -_" or unicodedata.category(ch)[0] in "LNM").replace(" ", "-")
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.append(slug if n == 0 else f"{slug}-{n}")
    return out


def test_every_fix_anchor_is_a_readme_heading():
    slugs = github_slugs((ROOT / "README.md").read_text())
    assert github_slugs("# A b\n## A b\n### Plaid (banks)\n") == ["a-b", "a-b-1", "plaid-banks"]
    for name, anchor in doctor.INTEGRATIONS:
        assert anchor in slugs, name
        assert slugs.count(anchor) == 1, anchor
    readme = (ROOT / "README.md").read_text()
    targets = set(slugs) | set(re.findall(r'<a id="([^"]+)"', readme))   # headings and explicit anchors
    for anchor in re.findall(r"\]\(#([^)]+)\)", readme):   # every in-page link of the README resolves too
        assert anchor in targets, anchor
    conf = (ROOT / "docs" / "configuration.md").read_text()
    for anchor in re.findall(r"\(\.\./README\.md#([^)]+)\)", conf):
        assert anchor in slugs, anchor

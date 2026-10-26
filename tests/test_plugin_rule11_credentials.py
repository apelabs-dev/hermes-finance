"""Hermes plugin catalog rule 11: credentials come through Hermes, never from files.

Under the plugin (register() hands Hermes's get_secret to the app) and in its report jobs (the entry script exports
HERMES_FINANCE_CREDENTIAL_SOURCE=hermes), the app takes the Plaid and Alpaca keys from Hermes only and never opens, or
even looks at, the secrets file, the Hermes env file or Alpaca's key file. Every file here is a decoy under tmp_path
holding its own sentinel, so a leak names its source. No browser, no network.
"""

from __future__ import annotations

import contextlib
import builtins
import importlib
import io
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import threading
import types
from datetime import date
from pathlib import Path

import pytest

from agent_tools import load_plugin, plugin_ctx
from conftest import FAKE_CLIENT_ID, FAKE_SECRET, account, add_item, page, txn
from hermes_finance import alpaca, config, doctor, secretstore, syncnow, tooldefs
from hermes_finance.plaid import PlaidClient

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes_plugin"
APP_ROOT = Path(config.__file__).resolve().parents[2]      # the checkout whose hermes_finance is under test
SENTINEL = "SENTINEL-rule11"
SIX = ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ENV", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "APCA_ENV")
# what Hermes holds (get_secret, or the environment Hermes runs a job with)
HERMES = {"PLAID_CLIENT_ID": f"{SENTINEL}-hermes-client", "PLAID_SECRET": f"{SENTINEL}-hermes-secret",
          "PLAID_ENV": "production", "APCA_API_KEY_ID": "AKSENTINELrule11hermes",
          "APCA_API_SECRET_KEY": f"{SENTINEL}-hermes-alpaca-secret", "APCA_ENV": "live"}
NOT_CONFIGURED = "Plaid not configured in Hermes (hermes config set"
FLAG = "HERMES_FINANCE_CREDENTIAL_SOURCE"
DAY = date(2026, 9, 28)


@pytest.fixture(autouse=True)
def no_provider_left():
    yield
    reset = getattr(config, "use_hermes_credentials", None)
    if reset is not None:
        reset(None)


@pytest.fixture
def decoys(data_dir, tmp_path, monkeypatch):
    """The three key files the app reads standalone, each with its own sentinels, and the variables pointing at them."""
    d = tmp_path / "decoys"
    d.mkdir()
    files = {
        "secrets": (d / "secrets.env", f"PLAID_CLIENT_ID={SENTINEL}-secretsfile-client\n"
                                       f"PLAID_SECRET={SENTINEL}-secretsfile-secret\nPLAID_ENV=production\n"
                                       f"AI_API_KEY={SENTINEL}-secretsfile-ai\n"),
        "env": (d / "hermes.env", f"PLAID_CLIENT_ID={SENTINEL}-envfile-client\nPLAID_SECRET={SENTINEL}-envfile-secret\n"
                                  "CARFAX_ENABLED=1\n"),
        "alpaca": (d / "alpaca-live.env", f"APCA_API_KEY_ID=AKSENTINELrule11keyfile\n"
                                          f"APCA_API_SECRET_KEY={SENTINEL}-alpacafile-secret\nAPCA_ENV=live\n"),
    }
    for path, text in files.values():
        path.write_text(text)
    monkeypatch.setenv("FINANCE_SECRETS_FILE", str(files["secrets"][0]))
    monkeypatch.setenv("HERMES_ENV_FILE", str(files["env"][0]))
    monkeypatch.setenv("HERMES_ALPACA_KEY_FILE", str(files["alpaca"][0]))
    for name in (*SIX, "AI_API_KEY", "CARFAX_ENABLED", "REDFIN_ENABLED", "PHOTOS_ENABLED", FLAG):
        monkeypatch.delenv(name, raising=False)
    return {k: v[0] for k, v in files.items()}


@contextlib.contextmanager
def trap(paths):
    """Record every open / stat / exists of a decoy path (in this process) while the block runs."""
    targets = {str(p) for p in paths} | {os.path.abspath(str(p)) for p in paths}
    touched: list[str] = []

    def hit(p):
        try:
            s = os.fsdecode(p)
        except TypeError:
            return
        if s in targets or os.path.abspath(s) in targets:
            touched.append(s)

    def wrap(fn):
        def spy(p, *a, **k):
            hit(p)
            return fn(p, *a, **k)
        return spy

    with pytest.MonkeyPatch.context() as mp:
        for owner, names in ((builtins, ("open",)), (io, ("open",)), (os, ("open", "stat", "lstat", "access")),
                             (pathlib.Path, ("open", "read_text", "read_bytes", "exists", "is_file", "stat"))):
            for name in names:
                mp.setattr(owner, name, wrap(getattr(owner, name)))
        yield touched


class FakeHermes:
    """A stand-in for Hermes's agent.secret_scope: get_secret answers from `values` (or raises), recording each ask."""

    class UnscopedSecretError(RuntimeError):
        pass

    def __init__(self, values=None):
        self.values = dict(HERMES if values is None else values)
        self.calls: list[str] = []
        self.raises = False

    def get_secret(self, name, default=None):
        self.calls.append(name)
        if self.raises:
            raise self.UnscopedSecretError(f"no secret scope for {name} (multiplexing)")
        return self.values.get(name, default)


@pytest.fixture
def hermes(monkeypatch):
    fake = FakeHermes()
    pkg = types.ModuleType("agent")
    pkg.__path__ = []
    mod = types.ModuleType("agent.secret_scope")
    mod.get_secret = fake.get_secret
    mod.UnscopedSecretError = FakeHermes.UnscopedSecretError
    pkg.secret_scope = mod
    monkeypatch.setitem(sys.modules, "agent", pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", mod)
    return fake


@pytest.fixture
def bank(conn, fake, monkeypatch):
    """One bank due a sync; the Plaid client finance_sync makes goes through `relay`, which records the keys it was
    given and answers from the fake Plaid (with conftest's fake keys swapped in)."""
    add_item(conn)
    fake.on("/transactions/sync", page(added=[txn("t1")], accounts=[account()]))
    seen: list[tuple[str, str, str]] = []
    state = {"fail": False}

    def relay(url, data, headers, timeout):
        body = json.loads(data)
        seen.append((body["client_id"], body["secret"], url.split("/")[2]))
        if state["fail"]:
            raise OSError("connection reset by peer")
        body.update(client_id=FAKE_CLIENT_ID, secret=FAKE_SECRET)
        return fake(url, json.dumps(body).encode(), headers, timeout)

    monkeypatch.setattr(syncnow, "PlaidClient", lambda settings: PlaidClient(settings, transport=relay))
    return types.SimpleNamespace(seen=seen, state=state)


def promptly(fn, seconds=30):
    """fn() in a thread; fails if it doesn't return within `seconds`."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:   # surfaced below
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), f"no answer within {seconds}s"
    if "error" in box:
        raise box["error"]
    return box["value"]


def no_sentinel(*texts):
    for text in texts:
        assert SENTINEL not in text and "AKSENTINELrule11" not in text, text


# --- (a) Hermes mode never touches a key file -------------------------------------------------------------------------

def test_hermes_mode_opens_no_key_file_in_process(decoys, conn, monkeypatch):
    for kill in ("HERMES_PLAID", "HERMES_ALPACA"):   # so doctor checks the keys (it makes no call either way)
        monkeypatch.delenv(kill, raising=False)
    config.use_hermes_credentials(lambda name: HERMES.get(name))
    with trap(decoys.values()) as touched:
        s = config.load_settings()
        s_path = config.load_settings(decoys["secrets"])         # an explicit path isn't read either
        values = config.key_values()
        status = secretstore.status()
        mine = secretstore.read()
        keys = alpaca.load_keys()
        free = config.free_sources()
        overrides = config.free_source_overrides()
        state = alpaca.key_state()
        report = doctor.run()
        checks = [doctor.alpaca_check(), doctor.plaid(values)]
    assert touched == []
    assert (s.client_id, s.secret, s.env) == (HERMES["PLAID_CLIENT_ID"], HERMES["PLAID_SECRET"], "production")
    assert (s_path.client_id, s_path.secret) == (s.client_id, s.secret)
    assert values == HERMES and mine == {}
    assert (keys.key_id, keys.secret) == (HERMES["APCA_API_KEY_ID"], HERMES["APCA_API_SECRET_KEY"])
    assert status["plaid"] == {"set": True, "env": "production", "where": "Hermes"}
    assert status["alpaca"] == {"set": True, "where": "Hermes"} and state == {"key_file": True}
    assert free["carfax"] is False and overrides == {}           # CARFAX_ENABLED=1 sits in the decoy env file only
    assert [c.status for c in checks] == ["ok", "ok"]
    shown = json.dumps(report) + json.dumps(status)
    assert not any(str(p) in shown for p in decoys.values()) and report["files"]["credentials"] == "Hermes"
    no_sentinel(json.dumps(report), json.dumps(status))
    assert HERMES["PLAID_SECRET"] not in repr(s) and HERMES["APCA_API_SECRET_KEY"] not in repr(keys)


PROBE = r"""
import json, os, sys
opened = []
def hook(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        opened.append(os.fsdecode(args[0]))
sys.addaudithook(hook)
import hermes_finance
from hermes_finance import alpaca, config, secretstore
out = {"app": hermes_finance.__file__, "flag": os.environ.get("HERMES_FINANCE_CREDENTIAL_SOURCE")}
try:
    s = config.load_settings()
    out["settings"] = [s.client_id, s.secret, s.env]
except config.ConfigError as e:
    out["settings_error"] = str(e)
out["key_values"] = config.key_values()
out["status"] = secretstore.status()
try:
    k = alpaca.load_keys()
    out["alpaca"] = k and [k.key_id, k.secret]
except alpaca.Refused as e:
    out["alpaca_error"] = str(e)
out["free"] = config.free_sources()
out["opened"] = list(opened)
print(json.dumps(out))
"""


def child_env(decoys, tmp_path, **extra) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PLAID_", "APCA_", "AI_API_KEY", "PYTHON"))}
    env.update(FINANCE_SECRETS_FILE=str(decoys["secrets"]), HERMES_ENV_FILE=str(decoys["env"]),
               HERMES_ALPACA_KEY_FILE=str(decoys["alpaca"]), FINANCE_DATA_DIR=str(tmp_path / "data"), **extra)
    return env


def probe(cmd, env, cwd) -> tuple[dict, str]:
    r = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1]), r.stdout + r.stderr


def test_hermes_mode_opens_no_key_file_in_a_child_process(decoys, tmp_path):
    env = child_env(decoys, tmp_path, HERMES_FINANCE_CREDENTIAL_SOURCE="hermes", PYTHONPATH=str(APP_ROOT / "src"))
    out, text = probe([sys.executable, "-c", PROBE], env, tmp_path)
    assert out["app"].startswith(str(APP_ROOT / "src"))
    assert not set(out["opened"]) & {str(p) for p in decoys.values()}, out["opened"]
    assert out["settings_error"].startswith(NOT_CONFIGURED) and out["key_values"] == {}
    assert out["alpaca"] is None and out["status"]["plaid"]["set"] is False and out["free"]["carfax"] is False
    no_sentinel(text)


def test_hermes_environment_reaches_a_child_with_the_flag(decoys, tmp_path):
    """The report jobs' path: Hermes runs the job with the profile's keys in the environment and the flag set."""
    env = child_env(decoys, tmp_path, HERMES_FINANCE_CREDENTIAL_SOURCE="hermes", PYTHONPATH=str(APP_ROOT / "src"),
                    REDFIN_ENABLED="1", **HERMES)
    out, text = probe([sys.executable, "-c", PROBE], env, tmp_path)
    assert not set(out["opened"]) & {str(p) for p in decoys.values()}, out["opened"]
    assert out["settings"] == [HERMES["PLAID_CLIENT_ID"], HERMES["PLAID_SECRET"], "production"]
    assert out["alpaca"] == [HERMES["APCA_API_KEY_ID"], HERMES["APCA_API_SECRET_KEY"]]
    assert out["free"]["redfin"] is True and out["free"]["carfax"] is False
    assert "secretsfile" not in text and "envfile" not in text and "alpacafile" not in text and "keyfile" not in text


def test_the_entry_script_runs_the_app_with_keys_from_hermes(decoys, tmp_path, monkeypatch):
    """The rendered report-job script, run the way Hermes's scheduler runs it (the profile's keys in its environment):
    the app it starts takes the keys from that environment and opens no key file."""
    monkeypatch.syspath_prepend(str(ROOT))
    entry = importlib.import_module("hermes_plugin.crons.entry")
    roles = importlib.import_module("hermes_plugin.crons.roles")
    (tmp_path / "probe.py").write_text(PROBE)
    fake_python = tmp_path / "python"
    fake_python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{tmp_path / "probe.py"}"\n')
    fake_python.chmod(0o755)
    script = tmp_path / "daily.sh"
    script.write_bytes(entry.render(roles.DAILY, app_dir=APP_ROOT, app_python=fake_python,
                                    data_dir=tmp_path / "data", artifacts=tmp_path / "artifacts"))
    text = script.read_text()
    assert "\nexport HERMES_FINANCE_CREDENTIAL_SOURCE=hermes\n" in text and roles.SCRIPT_VERSION == 2
    assert "hermes-finance cron entry script: daily (version 2)" in text
    out, output = probe(["bash", str(script)], child_env(decoys, tmp_path, **HERMES), tmp_path)
    assert out["flag"] == "hermes" and out["app"].startswith(str(APP_ROOT / "src"))
    assert not set(out["opened"]) & {str(p) for p in decoys.values()}, out["opened"]
    assert out["settings"] == [HERMES["PLAID_CLIENT_ID"], HERMES["PLAID_SECRET"], "production"]
    assert "secretsfile" not in output and "envfile" not in output and "alpacafile" not in output


# --- (b) get_secret's values reach the app, at call time ---------------------------------------------------------------

def test_get_secret_values_reach_finance_sync_at_call_time(decoys, hermes, bank, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    ctx = plugin_ctx()
    assert hermes.calls == []                                   # nothing read at register
    s = config.load_settings()
    assert (s.client_id, s.secret) == (HERMES["PLAID_CLIENT_ID"], HERMES["PLAID_SECRET"])
    hermes.values["PLAID_CLIENT_ID"] = f"{SENTINEL}-hermes-client-2"   # changed in Hermes: the next use sees it
    assert config.load_settings().client_id == f"{SENTINEL}-hermes-client-2"
    hermes.values["PLAID_CLIENT_ID"] = HERMES["PLAID_CLIENT_ID"]
    with trap(decoys.values()) as touched:
        assert ctx.hooks["pre_tool_call"][0](tool_name="finance_sync", args={}, task_id="t") is None
        out = promptly(lambda: ctx.call("finance_sync"))
    assert touched == []
    assert out.get("ok") is True and out["synced"] == 1, out
    assert bank.seen and set(bank.seen) == {(HERMES["PLAID_CLIENT_ID"], HERMES["PLAID_SECRET"], "production.plaid.com")}
    assert {"PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ENV"} <= set(hermes.calls)
    captured = capsys.readouterr()
    no_sentinel(json.dumps(out), caplog.text, captured.out, captured.err)


# --- (c) a secret never shows, on any path; (d) a missing key is a clean message ---------------------------------------

@pytest.mark.parametrize("case", ["plaid-raises", "no-client-id", "get-secret-raises", "all-missing"])
def test_no_secret_in_any_output_and_a_clean_message(case, decoys, hermes, bank, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    if case == "plaid-raises":
        bank.state["fail"] = True
    elif case == "no-client-id":
        del hermes.values["PLAID_CLIENT_ID"]                   # the secret is set, the id isn't
    elif case == "get-secret-raises":
        hermes.raises = True                                   # e.g. UnscopedSecretError under multiplexing
    else:
        hermes.values = {}
    ctx = plugin_ctx()
    hook = ctx.hooks["pre_tool_call"][0]
    errors = []
    with trap(decoys.values()) as touched:
        for args in ({}, {"refresh": True}):
            directive = promptly(lambda: hook(tool_name="finance_sync", args=args, task_id="t"))
            assert directive is None or directive["action"] in ("approve", "block"), directive
            errors.append(json.dumps(directive))
        out = promptly(lambda: json.loads(ctx.tools["finance_sync"]({})))
    assert touched == []
    if case == "plaid-raises":
        assert bank.seen and bank.seen[0][:2] == (HERMES["PLAID_CLIENT_ID"], HERMES["PLAID_SECRET"])
        assert out.get("ok") is False or "error" in out, out
    else:
        assert bank.seen == [] and out["error"].startswith(NOT_CONFIGURED), out
    captured = capsys.readouterr()
    no_sentinel(json.dumps(out), caplog.text, captured.out, captured.err, *errors)


def test_get_secret_raising_is_a_missing_key_not_a_crash(decoys, hermes):
    hermes.raises = True
    plugin = load_plugin()
    assert plugin._hermes_secret("PLAID_SECRET") is None        # never raises
    config.use_hermes_credentials(plugin._hermes_secret)
    with trap(decoys.values()) as touched, pytest.raises(config.ConfigError) as e:
        syncnow.make_client()
    assert touched == [] and str(e.value) == config.PLAID_NOT_CONFIGURED
    assert config.key_values() == {} and alpaca.load_keys() is None


# --- the plugin's own reader --------------------------------------------------------------------------------------------

def test_the_plugin_reads_only_its_six_keys_and_only_through_get_secret(hermes, monkeypatch):
    plugin = load_plugin()
    assert tuple(plugin.CREDENTIALS) == SIX
    assert plugin._hermes_secret("PLAID_SECRET") == HERMES["PLAID_SECRET"]
    assert plugin._hermes_secret("AI_API_KEY") is None and plugin._hermes_secret("HOME") is None
    assert hermes.calls == ["PLAID_SECRET"]
    hermes.values["PLAID_SECRET"] = "   "
    assert plugin._hermes_secret("PLAID_SECRET") is None        # blank: not set
    monkeypatch.setenv("PLAID_SECRET", f"{SENTINEL}-osenviron")
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)   # an older Hermes: no secret_scope
    assert plugin._hermes_secret("PLAID_SECRET") is None        # never os.environ instead


def test_the_plugin_source_reads_no_credential_from_the_environment_or_a_file():
    sources = {p: p.read_text() for p in PLUGIN_DIR.rglob("*.py")}
    names = "|".join(SIX + ("AI_API_KEY",))
    for path, text in sources.items():
        assert not re.search(rf"(environ(\.get)?\s*[\[(]|getenv\s*\()\s*['\"]({names})['\"]", text), path
        for word in ("secrets.env", "alpaca-live.env", "parse_env_file", "load_env_file", "dotenv", '".env"',
                     "secrets_file_path", "env_file_path", "alpaca_key_path", "FINANCE_SECRETS_FILE",
                     "HERMES_ALPACA_KEY_FILE"):
            assert word not in text, (path, word)
    init = sources[PLUGIN_DIR / "__init__.py"]
    assert "from agent.secret_scope import get_secret" in init and "use_hermes_credentials" in init
    assert "use(_hermes_secret)" in init


def test_the_manifest_declares_the_six_keys_as_optional_hermes_secrets():
    lines = (PLUGIN_DIR / "plugin.yaml").read_text().splitlines()
    assert not any(line.startswith("requires_env") for line in lines)
    start = lines.index("config_schema:")
    schema: dict[str, dict[str, str]] = {}
    for line in lines[start + 1:]:
        if not line.startswith("  "):
            break
        if not line.startswith("    "):
            current = schema.setdefault(line.strip().rstrip(":"), {})
        else:
            key, _, value = line.strip().partition(":")
            current[key] = value.strip().strip('"')
    for name in SIX:
        entry = schema[name]
        assert entry["type"] == "secret" and entry["env"] == name and entry["label"], entry
        assert entry["description"].startswith("Optional") and "hermes config set" in entry["description"]
        assert "required" not in entry and "default" not in entry
    assert "production or sandbox; empty: production" in schema["PLAID_ENV"]["description"]
    assert "must be live; paper keys are refused" in schema["APCA_ENV"]["description"]


def test_an_old_app_keeps_the_reads_and_refuses_finance_sync(decoys, hermes, bank, monkeypatch):
    """An app checkout from before this change can't take its keys from Hermes: every tool still registers and reads
    answer, but finance_sync is refused (hook and handler) instead of reading the key files."""
    monkeypatch.delattr(config, "use_hermes_credentials", raising=False)
    ctx = plugin_ctx()
    assert list(ctx.tools) == list(tooldefs.TOOLS)
    with trap(decoys.values()) as touched:
        status = ctx.call("finance_status")
        directive = ctx.hooks["pre_tool_call"][0](tool_name="finance_sync", args={}, task_id="t")
        out = json.loads(ctx.tools["finance_sync"]({}))
    assert touched == [] and bank.seen == []
    assert "error" not in status
    assert directive["action"] == "block" and "too old" in directive["message"]
    assert "too old" in out["error"] and "update" in out["error"].lower()
    no_sentinel(json.dumps(status), json.dumps(directive), json.dumps(out))


# --- Alpaca in Hermes mode ----------------------------------------------------------------------------------------------

SNAP = {"status": "ACTIVE", "account_mask": "0042", "currency": "USD", "cash": 10.0, "equity": 10.0,
        "reported_equity": 10.0, "positions": []}


@pytest.mark.parametrize("source", ["plugin", "report-job"])
def test_alpaca_without_keys_in_hermes_never_forgets_saved_snapshots(source, decoys, conn, monkeypatch):
    """`finance alpaca-sync` in a report job (the flag) or anything under the plugin (the provider): no key in Hermes is
    "nothing to do", never "the key was removed"."""
    alpaca.save(conn, SNAP, DAY)
    if source == "plugin":
        config.use_hermes_credentials(lambda name: None)
    else:
        monkeypatch.setenv(FLAG, "hermes")
    with trap(decoys.values()) as touched:
        out = alpaca.sync(conn, DAY)
    assert touched == []
    assert out == {"ok": True, "action": "none", "message": alpaca.ALPACA_NOT_CONFIGURED}
    assert out["message"].startswith("Alpaca not configured in Hermes (hermes config set APCA_API_KEY_ID")
    assert [tuple(r) for r in conn.execute("SELECT counted FROM alpaca_snapshots")] == [(1,)]


def test_alpaca_keys_from_hermes_keep_the_live_only_checks(decoys, conn):
    values = dict(HERMES)
    config.use_hermes_credentials(values.get)
    with trap(decoys.values()) as touched:
        assert alpaca.load_keys().key_id == HERMES["APCA_API_KEY_ID"]
        values["APCA_ENV"] = "paper"
        with pytest.raises(alpaca.Refused, match="only a live key"):
            alpaca.load_keys()
        values.update(APCA_ENV="live", APCA_API_KEY_ID="PKSENTINELrule11paper")
        with pytest.raises(alpaca.Refused, match="paper key"):
            alpaca.load_keys()
        del values["APCA_API_SECRET_KEY"]
        with pytest.raises(alpaca.Refused) as e:
            alpaca.load_keys()
        for call in (lambda: alpaca.write_key_file(alpaca.Keys("AKX1234567", "s" * 20)),
                     lambda: alpaca.connect(conn, "AKX1234567", "s" * 20, DAY), lambda: alpaca.disconnect(conn)):
            with pytest.raises(alpaca.Refused, match="kept in Hermes"):
                call()
    assert touched == [] and "Hermes" in str(e.value)
    assert decoys["alpaca"].read_text().startswith("APCA_API_KEY_ID=AKSENTINELrule11keyfile")   # untouched
    with pytest.raises(secretstore.SecretError, match="kept in Hermes"):
        secretstore.write({"PLAID_CLIENT_ID": "abc"})


# --- (e) standalone: unchanged ------------------------------------------------------------------------------------------

def test_standalone_still_reads_the_files_as_before(decoys, conn, monkeypatch):
    for flag in (None, "files", ""):
        if flag is None:
            monkeypatch.delenv(FLAG, raising=False)
        else:
            monkeypatch.setenv(FLAG, flag)
        assert config.hermes_credentials() is False
        s = config.load_settings()
        assert (s.client_id, s.secret) == (f"{SENTINEL}-secretsfile-client", f"{SENTINEL}-secretsfile-secret")
        assert config.load_settings(decoys["env"]).client_id == f"{SENTINEL}-envfile-client"
        assert config.key_values()["AI_API_KEY"] == f"{SENTINEL}-secretsfile-ai"
        assert secretstore.read()["PLAID_CLIENT_ID"] == f"{SENTINEL}-secretsfile-client"
        assert config.free_sources()["carfax"] is True and config.free_source_overrides() == {"carfax": "CARFAX_ENABLED"}
        assert alpaca.load_keys().key_id == "AKSENTINELrule11keyfile" and alpaca.key_state() == {"key_file": True}
        status = secretstore.status()
        assert status["plaid"]["set"] is True and status["alpaca"]["set"] is True
        assert status["plaid"]["where"] == secretstore.shown_path() and status["alpaca"]["where"] != "Hermes"
        assert doctor.run()["files"]["secrets_file"] == str(decoys["secrets"])
    os.remove(decoys["alpaca"])
    alpaca.save(conn, SNAP, DAY)
    assert alpaca.sync(conn, DAY)["action"] == "removed"           # standalone: a removed key file still forgets

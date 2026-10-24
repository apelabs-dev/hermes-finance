"""The Ask bridge through the installed Hermes runtime (tests/hermes_runtime.py): the app's Hermes-mode turn runs the
real `hermes finance completion` (Hermes's own CLI entry point, its plugin loader and argparse, the plugin's command, the
real PluginContext and ctx.llm.complete_structured, auxiliary_client.call_llm and the OpenAI client) in a disposable
profile under sandbox-exec, against a FAKE OpenAI-compatible provider on a loopback port that the profile's config.yaml
names as its model (synthetic key, synthetic model). Everything else is denied the network, the real ~/.hermes is
unwritable and its secrets unreadable.

What this proves: the app's side and the installed plugin/host code path, end to end, with a synthetic provider. What it
does not: the real default profile's auth, a real provider, or the gateway (none is touched here; activation is a
separate, approved step)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import bridge_stub as bs
import hermes_runtime as hr
from agent_tools import TODAY, build, rows, table_hashes
from hermes_finance import bridge, chat, config, db, prefs, tooldefs, usage
from hermes_finance.web import App

pytestmark = pytest.mark.env_dependent("runtime")

LOCAL = {"Host": "127.0.0.1:8765"}
JSONH = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
MODEL = "fake-model-1"                         # synthetic
PROVIDER_KEY = "synthetic-provider-key-0001"   # synthetic: the profile's own, never the app's
SET_CAP = {"category": "Other Shopping", "monthly_cap": 900}
QUESTION_SENTINEL = "hf-bridge-question-3f1d"  # in the question: must not persist in the profile
MERCHANT_SENTINEL = "HFBRIDGEMERCHANT7C2A"     # in a read result: must not persist either


class FakeProvider:
    """A fake OpenAI-compatible /chat/completions (the profile's model): each request recorded (body, headers, when it
    came and was answered), answered by the next scripted step: {content, usage?, delay?, status?}."""

    def __init__(self):
        self.requests: list[dict] = []
        self.script: list[dict] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                t0 = time.monotonic()
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                step = fake.script.pop(0) if fake.script else {"content": json.dumps({"kind": "answer", "text": "(no script)"})}
                entry = {"path": self.path, "body": body, "headers": dict(self.headers.items()), "t0": t0}
                fake.requests.append(entry)
                if step.get("delay"):
                    time.sleep(step["delay"])
                out = json.dumps({"id": "fake", "object": "chat.completion", "model": MODEL,
                                  "choices": [{"index": 0, "finish_reason": "stop",
                                               "message": {"role": "assistant", "content": step["content"]}}],
                                  "usage": step.get("usage", {"prompt_tokens": 1200, "completion_tokens": 40,
                                                              "total_tokens": 1240})}).encode()
                try:
                    self.send_response(step.get("status", 200))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                entry["t1"] = time.monotonic()

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def decisions(self, *decisions: dict, **step) -> None:
        self.script = [{"content": json.dumps(d), **step} for d in decisions]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class BridgeProfile(hr.Profile):
    """A disposable Hermes profile whose model is the fake provider (or none: `provider=None`), and the executable the
    app runs for it: a wrapper that records its pid (Hermes keeps it: exec) and execs the installed `hermes` entry
    point under this profile's sandbox, which lets it reach only the fake provider's port."""

    def __init__(self, root: Path, provider: FakeProvider | None, *, enabled: bool = True, hide_jsonschema: bool = False,
                 plugin_src: Path | None = None):
        super().__init__(root, enabled=enabled, plugin=plugin_src is None)
        if plugin_src is not None:
            shutil.copytree(plugin_src, self.hermes_home / "plugins" / "hermes-finance",
                            ignore=shutil.ignore_patterns("__pycache__"))
        if provider is not None:
            cfg = self.hermes_home / "config.yaml"
            cfg.write_text(cfg.read_text() + f"model:\n  provider: custom\n  default: {MODEL}\n"
                                              f"  base_url: http://127.0.0.1:{provider.port}/v1\n  api_key: {PROVIDER_KEY}\n")
            self.sandbox.write_text(hr.sandbox_profile(self.root) +
                                    f'(allow network-outbound (remote ip "localhost:{provider.port}"))\n')
        self.pids_file = root / "pids"
        hide = ""
        if hide_jsonschema:   # the optional jsonschema package, not importable: Hermes then skips its schema check
            pkg = root / "hide" / "jsonschema"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("raise ImportError('jsonschema hidden by the test')\n")
            hide = f"export PYTHONPATH={root / 'hide'}\n"
        self.exe = root / "hermes"
        self.exe.write_text(f"#!/bin/sh\necho $$ >> {self.pids_file}\nexport PYTHONDONTWRITEBYTECODE=1\nexport UV_OFFLINE=1 HERMES_DISABLE_LAZY_INSTALLS=1\n{hide}"
                            f"exec {hr.SANDBOX_EXEC} -f {self.sandbox} {hr.HERMES_AGENT / 'venv' / 'bin' / 'hermes'} \"$@\"\n")
        self.exe.chmod(0o755)
        # Warm the real executable after writing the synthetic model config. New hosts
        # create config backups/install locks on first entry, before privacy snapshots.
        with hr.Child([str(self.exe), "finance", "completion", "--help"], self.env(), self.root) as child:
            warm = child.wait(30)
        assert warm.returncode in (0, 2), str(warm)
        self.pids_file.unlink(missing_ok=True)

    def use(self, monkeypatch) -> None:
        """The app runs this profile's Hermes: FINANCE_HERMES_BIN, and HOME / HERMES_HOME / TMPDIR (what the app passes
        its child, bridge.ENV_KEEP)."""
        monkeypatch.setenv("FINANCE_HERMES_BIN", str(self.exe))
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("HERMES_HOME", str(self.hermes_home))
        monkeypatch.setenv("TMPDIR", str(self.root / "tmp"))

    def pids(self) -> list[int]:
        return [int(x) for x in self.pids_file.read_text().split()] if self.pids_file.exists() else []


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    monkeypatch.setattr(tooldefs, "today", lambda: TODAY)


@pytest.fixture
def provider():
    f = FakeProvider()
    yield f
    f.close()


@pytest.fixture
def app(data_dir):
    build("full", config.db_path())
    conn = db.connect(config.db_path())
    prefs.save(conn, {"ai.mode": "hermes", "ai.monthly_cap": 5.0})
    conn.close()
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


@pytest.fixture
def profile(tmp_path, provider, monkeypatch):
    p = BridgeProfile(tmp_path / "p", provider)
    p.use(monkeypatch)
    yield p
    assert bs.gone(p.pids()), "a Hermes child outlived its request"


def http(app, method, path, body=None):
    status, ctype, out, _ = app.respond(method, path, JSONH if method != "GET" else LOCAL,
                                        json.dumps(body).encode() if body is not None else b"")
    return status, json.loads(out)


def ask(app, text):
    return http(app, "POST", "/api/chat", {"messages": [{"role": "user", "content": text}]})


def cap_of(category: str) -> float | None:
    return rows(config.db_path(), "SELECT monthly_cap FROM categories WHERE name = ?", (category,))[0][0]


def user_tables() -> dict[str, str]:
    return {k: v for k, v in table_hashes(config.db_path()).items() if k not in ("pending_actions", "api_calls", "api_usage")}


def user_text(body: dict) -> list[str]:
    """The text parts of the one user message Hermes built (plugin_llm._build_structured_messages)."""
    [user] = [m for m in body["messages"] if m["role"] == "user"]
    return [p["text"] for p in user["content"] if p.get("type") == "text"]


def ai_notes() -> list[tuple]:
    return rows(config.db_path(), "SELECT outcome, note FROM api_calls WHERE provider = 'ai' ORDER BY id")


# --- the whole sequence through the real host -----------------------------------------------------------------------

def test_the_real_cli_carries_a_read_a_proposal_and_the_answer(app, profile, provider):
    """schema advertised -> structured call -> the app's own check -> read run by the app -> proposal -> approval card
    -> applied exactly once, and nothing applied before the owner's Approve."""
    before = user_tables()
    provider.decisions({"kind": "read", "tool": "finance_budget_status", "arguments": {"month": "2026-09"}},
                       {"kind": "propose", "tool": "finance_set_cap", "arguments": SET_CAP},
                       {"kind": "answer", "text": "You're over on Shopping; raising its cap to $900 would cover it."})
    s, r = ask(app, "Why am I over on Shopping?")
    assert s == 200, r
    assert r["reply"].startswith("You're over on Shopping") and r["brain"] == "via Hermes"
    assert r["sources"] == ["from budget-status · Sep 2026"]
    [card] = r["cards"]
    assert (card["tool"], card["status"], card["title"]) == ("finance_set_cap", "pending", "Raise Other Shopping cap to $900?")
    assert user_tables() == before and cap_of("Other Shopping") == 850.0
    # what reached the provider, through Hermes: three requests, each with the app's schema and the profile's model/key
    assert len(provider.requests) == 3 and len(profile.pids()) == 3
    for q in provider.requests:
        assert q["path"] == "/v1/chat/completions"
        assert q["body"]["model"] == MODEL and q["headers"]["Authorization"] == f"Bearer {PROVIDER_KEY}"
        assert q["body"]["response_format"]["json_schema"]["schema"] == bridge.schema()
        head, text = user_text(q["body"])
        assert head.startswith(chat._system(TODAY, "hermes").strip()) and f"Schema name: {bridge.SCHEMA_NAME}" in head
        assert "tools" not in q["body"] and "tool_choice" not in q["body"]
    first, second, third = (user_text(q["body"])[1] for q in provider.requests)
    assert "Why am I over on Shopping?" in first and "result:" not in first
    read = tooldefs.render(tooldefs.run_read("finance_budget_status", {"month": "2026-09"}, config.db_path(), TODAY))
    assert f"   result: {read}" in second
    assert '"status":"pending_approval","applied":false' in third
    # each request charged: Hermes's token counts, priced at the app's estimate, via hermes
    notes = ai_notes()
    assert [o for o, _ in notes] == ["ok"] * 3
    assert all("in=1200 out=40 estimated via=hermes" in n for _, n in notes), notes
    # the owner's Approve applies it once
    s, d = http(app, "POST", "/api/chat/decide", {"id": card["id"], "approve": True})
    assert s == 200 and d["card"]["status"] == "applied" and cap_of("Other Shopping") == 900.0
    assert http(app, "POST", "/api/chat/decide", {"id": card["id"], "approve": True})[0] == 409
    assert rows(config.db_path(), "SELECT origin, status FROM pending_actions") == [("ask_hermes", "applied")]


# --- positive controls: without the bridge, no answer -----------------------------------------------------------------

def test_a_disabled_plugin_means_no_bridge_and_nothing_sent(app, tmp_path, provider, monkeypatch):
    p = BridgeProfile(tmp_path / "off", provider, enabled=False)
    p.use(monkeypatch)
    provider.decisions({"kind": "answer", "text": "should never be asked"})
    s, r = ask(app, "Hi")
    assert s == 503 and r["error"] == bridge.ERRORS["unavailable"][0]
    assert provider.requests == [] and ai_notes() == [("error", "usd=0.000000 in=0 out=0 estimated via=hermes")]
    assert bs.gone(p.pids())


def test_a_plugin_without_the_completion_command_means_no_bridge(app, tmp_path, provider, monkeypatch):
    old = tmp_path / "old-plugin"
    shutil.copytree(hr.PLUGIN_SRC, old, ignore=shutil.ignore_patterns("__pycache__"))
    cli = old / "finance_cli.py"
    text = cli.read_text()
    line = next(x for x in text.splitlines() if '("completion",' in x)
    cli.write_text(text.replace(line + "\n", ""))
    p = BridgeProfile(tmp_path / "old", provider, plugin_src=old)
    p.use(monkeypatch)
    provider.decisions({"kind": "answer", "text": "should never be asked"})
    s, r = ask(app, "Hi")
    assert s == 503 and r["error"] == bridge.ERRORS["unavailable"][0] and provider.requests == []


# --- the host's schema check is optional: the app's isn't ------------------------------------------------------------

WRONG_SHAPE = {"kind": "answer", "text": "Done, I raised it.", "apply": {"tool": "finance_set_cap", "arguments": SET_CAP}}


def test_with_jsonschema_hermes_refuses_a_wrong_shape_itself(profile, provider):
    provider.decisions(WRONG_SHAPE)
    req = bridge.request("Answer.", [{"role": "user", "content": "Raise it"}], [], 256)
    with pytest.raises(bridge.BridgeError) as e:
        bridge.complete(profile.exe, req, time.monotonic() + 30)
    assert e.value.code == "schema_mismatch" and len(provider.requests) == 1


def test_without_jsonschema_hermes_passes_it_on_and_the_app_refuses_it(app, tmp_path, provider, monkeypatch):
    p = BridgeProfile(tmp_path / "noschema", provider, hide_jsonschema=True)
    p.use(monkeypatch)
    provider.decisions(WRONG_SHAPE)
    req = bridge.request("Answer.", [{"role": "user", "content": "Raise it"}], [], 256)
    reply = bridge.complete(p.exe, req, time.monotonic() + 30)            # Hermes checked nothing
    assert json.loads(reply.text) == WRONG_SHAPE
    with pytest.raises(bridge.Invalid):
        bridge.decision(reply.text)
    before = user_tables()
    provider.decisions(WRONG_SHAPE)
    s, r = ask(app, "Raise it")                                            # the turn: refused before any write
    assert s == 502 and r["error"] == bridge.INVALID
    assert rows(config.db_path(), "SELECT COUNT(*) FROM pending_actions") == [(0,)] and user_tables() == before
    assert bs.gone(p.pids())


@pytest.mark.parametrize("content", [
    "Sure! Your Shopping is fine.",                                                              # malformed: prose
    json.dumps({"kind": "propose", "tool": "tooldefs.apply", "arguments": SET_CAP}),             # an injected tool name
    json.dumps({"kind": "read", "tool": "terminal", "arguments": {"command": "cat ~/.hermes/.env"}}),
    json.dumps({"kind": "read", "tool": "finance_spend", "arguments": {}, "sql": "DELETE FROM transactions"}),
    json.dumps({"kind": "answer", "text": "x", "provider": "openrouter", "model": "big"}),        # an override request
], ids=["prose", "injected-tool", "terminal", "sql-field", "override-fields"])
def test_a_bad_answer_through_the_real_host_writes_nothing(app, profile, provider, content):
    before = user_tables()
    provider.script = [{"content": content}]
    s, r = ask(app, "Raise it")
    assert s == 502 and r["error"] in (bridge.INVALID, bridge.ERRORS["schema_mismatch"][0])
    assert rows(config.db_path(), "SELECT COUNT(*) FROM pending_actions") == [(0,)] and user_tables() == before


def test_an_oversized_answer_is_refused_by_the_bridge(app, profile, provider):
    """Not JSON, so Hermes passes it back as text: the plugin's command refuses to carry more than MAX_TEXT."""
    provider.script = [{"content": "x" * 200_000}]
    s, r = ask(app, "Hi")
    assert s == 502 and r["error"] == bridge.ERRORS["output_too_large"][0]
    [(outcome, note)] = ai_notes()
    assert outcome == "error" and not note.startswith("usd=0.000000")      # it may have cost: charged its worst case


# --- the request the plugin accepts --------------------------------------------------------------------------------

def raw_request(**extra) -> str:
    req = bridge.request("Answer.", [{"role": "user", "content": "Hi"}], [], 256)
    return json.dumps({**req, "timeout": 20, **extra})


@pytest.mark.parametrize("stdin,code", [
    (raw_request(provider="openrouter"), "bad_request"),
    (raw_request(model="other-model"), "bad_request"),
    (raw_request(profile="work"), "bad_request"),
    (raw_request(timeout=600), "bad_request"),
    ("not json", "bad_request"),
    ("x" * (600 * 1024), "request_too_large"),
], ids=["provider", "model", "profile", "timeout", "not-json", "too-large"])
def test_the_command_refuses_anything_but_the_protocol(profile, provider, stdin, code):
    r = profile.spawn("finance", "completion", stdin=stdin).wait(120)
    assert r.returncode == 0, str(r)
    [line] = [x for x in r.stdout.splitlines() if x.strip()]
    assert json.loads(line) == {"protocol": bridge.PROTOCOL, "ok": False, "error": code}
    assert provider.requests == []


def test_only_the_envelope_goes_to_stdout(profile, provider):
    provider.decisions({"kind": "answer", "text": "hi"})
    r = profile.spawn("finance", "completion", stdin=raw_request()).wait(120)
    lines = [x for x in r.stdout.splitlines() if x.strip()]
    assert r.returncode == 0 and len(lines) == 1, str(r)
    env = json.loads(lines[0])
    assert (env["ok"], env["provider"], env["model"], env["usage"]["input_tokens"]) == (True, "custom", MODEL, 1200)
    assert "parsed" not in env and json.loads(env["text"]) == {"kind": "answer", "text": "hi"}


# --- no host auth, the cap, the deadline, two turns ---------------------------------------------------------------

def test_without_a_model_in_hermes_nothing_is_sent_and_nothing_charged(app, tmp_path, monkeypatch):
    p = BridgeProfile(tmp_path / "nomodel", None)
    p.use(monkeypatch)
    s, r = ask(app, "Hi")
    assert s == 503 and r["error"] == bridge.ERRORS["no_provider"][0], r
    assert ai_notes() == [("error", "usd=0.000000 in=0 out=0 estimated via=hermes")]
    assert bs.gone(p.pids())


def test_at_the_cap_hermes_is_never_started(app, profile, provider):
    conn = db.connect(config.db_path())
    prefs.save(conn, {"ai.monthly_cap": 0.5})
    usage.ai_finish(conn, usage.ai_reserve(conn, TODAY, 0.5), "ok", 200, 0.75)
    conn.close()
    provider.decisions({"kind": "answer", "text": "never"})
    s, r = ask(app, "Hi")
    assert s == 429 and profile.pids() == [] and provider.requests == []


def test_the_deadline_kills_hermes_mid_request(app, profile, provider, monkeypatch):
    monkeypatch.setattr(chat, "TIMEOUT", 8.0)
    provider.script = [{"content": json.dumps({"kind": "answer", "text": "late"}), "delay": 30}]
    t0 = time.monotonic()
    s, r = ask(app, "Hi")
    took = time.monotonic() - t0
    assert s == 504 and r["error"] == chat.LATE and took < 9.5, (s, r, took)
    assert len(profile.pids()) == 1 and bs.gone(profile.pids())
    [(outcome, note)] = ai_notes()
    assert outcome == "timeout" and "estimated via=hermes" in note and not note.startswith("usd=0.000000")


def test_two_turns_at_once_send_one_request_at_a_time(app, profile, provider):
    provider.script = [{"content": json.dumps({"kind": "answer", "text": f"answer {i}"}), "delay": 1.0} for i in (1, 2)]
    got = []
    threads = [threading.Thread(target=lambda: got.append(ask(app, "Hi"))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(90)
    assert sorted(s for s, _ in got) == [200, 200], got
    a, b = sorted(provider.requests, key=lambda q: q["t0"])
    assert b["t0"] >= a["t1"], (a["t0"], a["t1"], b["t0"])                 # never two out at once
    assert len(ai_notes()) == 2


# --- privacy: nothing of the owner's is kept by Hermes ---------------------------------------------------------------

def _expected_profile_change(profile: hr.Profile, name: str) -> bool:
    if name.startswith("logs/"):
        return True
    path = profile.hermes_home / name
    # Hermes saves a last-known-good copy on config load. Only the test's exact
    # config bytes are benign; backups remain in all_text()'s sentinel scan.
    return (path.parent == profile.hermes_home / "backups" / "config"
            and path.name.startswith("config.yaml.good.")
            and not path.is_symlink() and path.is_file()
            and path.read_bytes() == (profile.hermes_home / "config.yaml").read_bytes())


def test_profile_change_exception_only_accepts_identical_good_config_backups(tmp_path):
    p = hr.Profile(tmp_path / "changes", warm=False, plugin=False)
    cfg = p.hermes_home / "config.yaml"
    good = "backups/config/config.yaml.good.synthetic"
    for name in (good, "backups/config/config.yaml.bad.synthetic",
                 "backups/config/nested/config.yaml.good.synthetic",
                 "backups/config.yaml.good.synthetic", "plugin-data/config.yaml.good.synthetic"):
        path = p.hermes_home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(cfg.read_bytes())
        assert _expected_profile_change(p, name) == (name == good), name
    assert not _expected_profile_change(p, "config.yaml")
    assert _expected_profile_change(p, "logs/agent.log")  # the original allowance
    backup = p.hermes_home / good
    backup.write_text(MERCHANT_SENTINEL)
    assert not _expected_profile_change(p, good)
    assert MERCHANT_SENTINEL in p.all_text()  # even a backup is scanned for leaks
    backup.unlink()
    assert not _expected_profile_change(p, good)  # deletion
    backup.symlink_to(cfg)
    assert not _expected_profile_change(p, good)
    backup.unlink()
    backup.mkdir()
    assert not _expected_profile_change(p, good)


@pytest.mark.parametrize("load_inside_window", [False, True], ids=["before-window", "inside-window"])
def test_first_config_load_writes_good_backup_in_snapshot_window(tmp_path, load_inside_window):
    # Use the installed loader, in a fresh sandboxed profile: no CLI dispatch,
    # scheduling, sleeps, or simulated backup writer decides when the load happens.
    p = hr.Profile(tmp_path / "fresh", warm=False, plugin=False)
    config_path = p.hermes_home / "config.yaml"
    backup_dir = p.hermes_home / "backups" / "config"
    assert not list(backup_dir.glob("config.yaml.good.*"))

    def load():
        result = p.py("from hermes_cli.config import load_config; load_config()")
        assert result.returncode == 0, str(result)

    if not load_inside_window:
        load()
    before = p.snapshot(skip_logs=False)
    load()
    after = p.snapshot(skip_logs=False)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    [backup] = backup_dir.glob("config.yaml.good.*")
    assert backup.read_bytes() == config_path.read_bytes()
    rel = backup.relative_to(p.hermes_home).as_posix()
    assert (rel in changed) == load_inside_window, changed
    if load_inside_window:
        # This is precisely the original privacy assertion: a benign config copy
        # makes it fail when the first load is between the two snapshots.
        assert changed and not all(k.startswith("logs/") for k in changed), changed
    else:
        assert changed == []  # a fresh process skips the byte-identical backup
    load()
    assert p.snapshot(skip_logs=False) == after


def test_no_prompt_or_row_is_kept_in_the_profile(app, profile, provider):
    c = sqlite3.connect(config.db_path())
    tid = c.execute("SELECT transaction_id FROM transactions WHERE date LIKE '2026-09%' ORDER BY date LIMIT 1").fetchone()[0]
    c.execute("UPDATE transactions SET merchant_name = ? WHERE transaction_id = ?", (MERCHANT_SENTINEL, tid))
    c.commit()
    c.close()
    before = profile.snapshot(skip_logs=False)
    provider.decisions({"kind": "read", "tool": "finance_transactions", "arguments": {"search": MERCHANT_SENTINEL}},
                       {"kind": "answer", "text": "That's one charge."})
    s, r = ask(app, f"What is {QUESTION_SENTINEL}?")
    assert s == 200, r
    assert MERCHANT_SENTINEL in user_text(provider.requests[1]["body"])[1]      # it did go through Hermes
    after = profile.snapshot(skip_logs=False)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    assert changed and all(_expected_profile_change(profile, k) for k in changed), changed
    logs = "".join(p.read_text(errors="replace") for p in (profile.hermes_home / "logs").rglob("*") if p.is_file())
    assert "plugin_llm.complete_structured plugin=hermes-finance" in logs     # the host did log the calls ...
    text = profile.all_text() + "".join(p.read_text(errors="replace") for p in (profile.root / "tmp").rglob("*") if p.is_file())
    for sentinel in (QUESTION_SENTINEL, MERCHANT_SENTINEL, *hr.SENTINELS):   # ... and kept nothing of the owner's
        assert sentinel not in text, sentinel

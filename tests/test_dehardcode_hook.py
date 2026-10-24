"""The de-hardcode pre-commit hook (scripts/hooks/pre-commit) and its shared scanner (scripts/dehardcode.py), in a
throwaway git repo under tmp_path: never this repo, its config or its hooks. The private word is a made-up one, given
through HERMES_FINANCE_TEST_PRIVATE / HERMES_FINANCE_PRIVATE_FILE as a real install would."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import dehardcode  # noqa: E402

WORD = "zyxwvqk"   # made up: no real name, and in no file of the repo


def _env(tmp: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in (dehardcode.PRIVATE_ENV, dehardcode.PRIVATE_FILE_ENV)}
    env.update(HERMES_FINANCE_HOOK_PYTHON=sys.executable, HERMES_FINANCE_PRIVATE_FILE=str(tmp / "no-such-file"),
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    env.update(extra)
    return env


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "scripts" / "hooks").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "hooks" / "pre-commit", r / "scripts" / "hooks" / "pre-commit")
    shutil.copy2(ROOT / "scripts" / "dehardcode.py", r / "scripts" / "dehardcode.py")
    (r / "src" / "pkg").mkdir(parents=True)
    (r / "hermes_plugin").mkdir()
    (r / "tests").mkdir()
    env = _env(tmp_path)
    subprocess.run(["git", "init", "-q", str(r)], check=True, env=env)
    return r


def git(repo: Path, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)


def hook(repo: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(repo / "scripts" / "hooks" / "pre-commit")], cwd=repo, env=env,
                          capture_output=True, text=True)


def stage(repo: Path, rel: str, text: str, env: dict) -> None:
    (repo / rel).write_text(text)
    assert git(repo, "add", rel, env=env).returncode == 0


def test_hook_is_executable_posix_sh():
    p = ROOT / "scripts" / "hooks" / "pre-commit"
    assert os.access(p, os.X_OK) and p.read_text().startswith("#!/bin/sh\n")
    assert os.access(ROOT / "scripts" / "install-hooks.sh", os.X_OK)
    assert subprocess.run(["sh", "-n", str(p)]).returncode == 0
    assert ".private-words" in (ROOT / ".gitignore").read_text().split()   # the default word file stays untracked


def test_a_staged_private_word_under_src_blocks_and_a_clean_file_passes(repo, tmp_path):
    env = _env(tmp_path, **{dehardcode.PRIVATE_ENV: f"{WORD}, other"})
    stage(repo, "src/pkg/a.py", f"x = 1\n# written by {WORD.title()}\n", env)
    r = hook(repo, env)
    assert r.returncode == 1, r
    assert f"src/pkg/a.py:2: {WORD.title()}" in r.stderr                 # path, line and token
    assert "--no-verify" in r.stderr and "test_dehardcode_task18" in r.stderr   # how to bypass; the gate still runs
    stage(repo, "src/pkg/a.py", "x = 1\n# clean\n", env)
    r = hook(repo, env)
    assert r.returncode == 0, r


def test_the_hook_reads_the_index_not_the_working_tree(repo, tmp_path):
    env = _env(tmp_path, **{dehardcode.PRIVATE_ENV: WORD})
    stage(repo, "hermes_plugin/b.py", "ok = True\n", env)
    (repo / "hermes_plugin" / "b.py").write_text(f"ok = '{WORD}'\n")   # only in the working tree: doesn't block
    assert hook(repo, env).returncode == 0
    stage(repo, "hermes_plugin/b.py", f"ok = '{WORD}'\n", env)
    (repo / "hermes_plugin" / "b.py").write_text("ok = True\n")         # staged, then cleaned only on disk: blocks
    r = hook(repo, env)
    assert r.returncode == 1 and f"hermes_plugin/b.py:1: {WORD}" in r.stderr, r


def test_the_word_check_is_src_and_the_plugin_the_release_scan_everything_whole_words_only(repo, tmp_path):
    env = _env(tmp_path, **{dehardcode.PRIVATE_ENV: WORD})
    stage(repo, "tests/t.py", f"name = '{WORD}'\n", env)                 # outside src/: the release scan's to judge
    r = hook(repo, env)
    assert r.returncode == 1 and "tests/t.py:1: private-word" in r.stderr, r
    assert WORD not in r.stderr                                           # the release scan never echoes the word
    stage(repo, "tests/t.py", "name = 'Someone'\n", env)
    stage(repo, "src/pkg/c.py", f"v = '{WORD}s1'\n", env)               # inside a longer word: not the word
    assert hook(repo, env).returncode == 0


def test_words_from_the_private_file(repo, tmp_path):
    words = tmp_path / "words.txt"
    words.write_text(f"# my words\n{WORD}  # a comment\n\n")
    env = _env(tmp_path, HERMES_FINANCE_PRIVATE_FILE=str(words))
    stage(repo, "src/pkg/d.js", f"const who = '{WORD.upper()}';\n", env)
    r = hook(repo, env)
    assert r.returncode == 1 and f"src/pkg/d.js:1: {WORD.upper()}" in r.stderr, r


def test_a_real_commit_is_blocked_through_core_hookspath(repo, tmp_path):
    env = _env(tmp_path, **{dehardcode.PRIVATE_ENV: WORD})
    assert git(repo, "config", "core.hooksPath", "scripts/hooks", env=env).returncode == 0   # the throwaway repo's own config
    stage(repo, "src/pkg/e.py", f"# {WORD}\n", env)
    r = git(repo, "commit", "-q", "-m", "x", env=env)
    assert r.returncode != 0 and "src/pkg/e.py:1:" in r.stderr, r
    assert git(repo, "rev-parse", "--verify", "-q", "HEAD", env=env).returncode != 0          # nothing was committed
    stage(repo, "src/pkg/e.py", "# fine\n", env)
    assert git(repo, "commit", "-q", "-m", "x", env=env).returncode == 0


def test_cli_scans_given_paths_and_the_tree(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(dehardcode.PRIVATE_ENV, WORD)
    monkeypatch.setenv(dehardcode.PRIVATE_FILE_ENV, str(tmp_path / "none"))
    assert dehardcode.main([]) == 0                                       # the real tree is clean
    assert dehardcode.main(["--bogus"]) == 2
    m = dehardcode.Matcher({WORD}, ["JTX"])
    assert m.scan("src/x", f"a {WORD}\nVIN JTXAB1\n{WORD}9\n") == [("src/x", 1, WORD), ("src/x", 2, "JTX")]


def test_the_allowlist_is_stated_and_empty():
    assert dehardcode.SCANNED_ROOTS == ("src", "hermes_plugin")
    assert dehardcode.EXEMPT == {}
    assert dehardcode.scanned("src/hermes_finance/web.py") and not dehardcode.scanned("tests/test_web.py")
    assert not dehardcode.scanned("src/hermes_finance/static/fonts/InterVariable.woff2")


def test_own_words_are_the_runners(monkeypatch):
    monkeypatch.delenv(dehardcode.PRIVATE_ENV, raising=False)
    import pwd
    me = pwd.getpwuid(os.getuid()).pw_name.lower()
    words = dehardcode.private_words({dehardcode.PRIVATE_FILE_ENV: os.devnull})
    assert (me in words) == (len(me) >= dehardcode.MIN_OWN and me not in dehardcode.SERVICE_LOGINS)   # (CI's: runner)
    assert dehardcode.private_words({dehardcode.PRIVATE_ENV: "Abcdq, ", dehardcode.PRIVATE_FILE_ENV: os.devnull},
                                    include_own=False) == {"abcdq"}

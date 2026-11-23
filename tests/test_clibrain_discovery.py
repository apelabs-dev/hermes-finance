"""Where the CLI is (clibrain.executable) when the service runs under launchd's PATH=/usr/bin:/bin:/usr/sbin:/sbin:
the env var, then the path the owner saved, then PATH, then the well-known install folders under the real home. And
the child's PATH: its own folder first, so an `#!/usr/bin/env node` wrapper still runs, nothing else added."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_finance import clibrain, prefs

MINIMAL = "/usr/bin:/bin:/usr/sbin:/sbin"


def make(path: Path, body: str = "#!/bin/sh\necho 'tool 9.8.7'\n", mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("PATH", MINIMAL)
    for name in ("FINANCE_CLAUDE_BIN", "FINANCE_CODEX_BIN", "NPM_CONFIG_PREFIX", clibrain.OFF_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(clibrain, "SYSTEM_DIRS", (), raising=False)        # the machine's own /opt/homebrew/bin must not leak in
    return h


def test_the_searched_dirs_are_the_documented_ones_in_order(home):
    assert clibrain.search_dirs() == [home / ".local" / "bin", home / ".claude" / "local",
                                      home / ".npm-global" / "bin"]
    assert Path.home() == home


def test_the_system_dirs_come_after_the_home_ones_and_before_npm(home, monkeypatch):
    monkeypatch.undo()                                      # the real defaults
    monkeypatch.setenv("HOME", str(home))
    dirs = clibrain.search_dirs()
    assert dirs[:4] == [home / ".local" / "bin", home / ".claude" / "local", Path("/opt/homebrew/bin"),
                        Path("/usr/local/bin")]


def test_the_npm_prefix_from_the_environment_is_used_when_it_is_absolute(home, monkeypatch):
    monkeypatch.setenv("NPM_CONFIG_PREFIX", str(home / "npmroot"))
    assert clibrain.search_dirs()[-1] == home / "npmroot" / "bin"
    monkeypatch.setenv("NPM_CONFIG_PREFIX", "relative/prefix")
    assert clibrain.search_dirs()[-1] == home / ".npm-global" / "bin"


@pytest.mark.parametrize("cli_id", ["claude", "codex"])
def test_a_cli_in_local_bin_is_found_under_the_minimal_launchd_path(home, cli_id):
    exe = make(home / ".local" / "bin" / cli_id)
    assert clibrain.executable(cli_id) == exe
    assert clibrain.installed(cli_id)["found"] is True and clibrain.installed(cli_id)["path"] == str(exe)


def test_the_well_known_dirs_are_tried_in_order(home):
    npm = make(home / ".npm-global" / "bin" / "claude")
    assert clibrain.executable("claude") == npm
    legacy = make(home / ".claude" / "local" / "claude")
    assert clibrain.executable("claude") == legacy
    local = make(home / ".local" / "bin" / "claude")
    assert clibrain.executable("claude") == local


def test_nothing_found_is_none(home):
    assert clibrain.executable("claude") is None and clibrain.installed("claude")["found"] is False


def test_precedence_is_env_then_saved_then_path_then_well_known(home, conn, monkeypatch, tmp_path):
    known = make(home / ".local" / "bin" / "claude")
    assert clibrain.executable("claude", conn) == known
    on_path = make(tmp_path / "pathdir" / "claude")
    monkeypatch.setenv("PATH", f"{on_path.parent}:{MINIMAL}")
    assert clibrain.executable("claude", conn) == on_path               # PATH beats the well-known folders
    saved = make(tmp_path / "saved" / "claude")
    prefs.save(conn, {"ai.cli_path.claude": str(saved)})
    assert clibrain.executable("claude", conn) == saved                 # the owner's saved path beats PATH
    env = make(tmp_path / "envdir" / "claude")
    monkeypatch.setenv("FINANCE_CLAUDE_BIN", str(env))
    assert clibrain.executable("claude", conn) == env                   # the env var beats everything


def test_a_candidate_that_is_not_an_executable_file_is_skipped(home, conn, monkeypatch, tmp_path):
    make(home / ".local" / "bin" / "claude", mode=0o644)                # present, not executable
    assert clibrain.executable("claude", conn) is None
    (home / ".claude" / "local").mkdir(parents=True)
    (home / ".claude" / "local" / "claude").mkdir()                     # a directory with the right name
    assert clibrain.executable("claude", conn) is None
    good = make(home / ".npm-global" / "bin" / "claude")
    assert clibrain.executable("claude", conn) == good                  # the next real one is still found
    monkeypatch.setenv("FINANCE_CLAUDE_BIN", "claude")                  # relative: never used
    assert clibrain.executable("claude", conn) == good
    monkeypatch.setenv("FINANCE_CLAUDE_BIN", str(tmp_path / "missing" / "claude"))
    assert clibrain.executable("claude", conn) == good
    monkeypatch.setenv("FINANCE_CLAUDE_BIN", str(home / ".local" / "bin"))   # a directory
    assert clibrain.executable("claude", conn) == good


def test_a_relative_directory_on_path_is_not_searched(home, monkeypatch, tmp_path):
    make(tmp_path / "rel" / "claude")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"rel:{MINIMAL}")
    assert clibrain.executable("claude") is None


def test_child_env_puts_the_executables_own_dir_first_and_adds_nothing_else(home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic")
    monkeypatch.setenv("NODE_OPTIONS", "--synthetic")
    bare = clibrain.child_env()
    assert bare["PATH"] == clibrain.PATH
    env = clibrain.child_env(home / ".local" / "bin")
    assert env["PATH"] == f"{home / '.local' / 'bin'}:{clibrain.PATH}"
    assert {k: v for k, v in env.items() if k != "PATH"} == {k: v for k, v in bare.items() if k != "PATH"}
    assert "ANTHROPIC_API_KEY" not in env and "NODE_OPTIONS" not in env
    assert clibrain.ENV_KEEP == ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")


def test_a_wrapper_script_cli_runs_through_version_with_the_child_env(home):
    """npm's launcher is `#!/usr/bin/env node`: `node` sits beside it, not on the system PATH."""
    exe = make(home / ".local" / "bin" / "claude", "#!/usr/bin/env fakenode\n")
    make(home / ".local" / "bin" / "fakenode", "#!/bin/sh\necho \"fakenode runs $1\"\necho 'claude 3.4.5'\n")
    assert clibrain.version(exe) == "3.4.5"
    got = clibrain._probe([str(exe), "--version"], 5.0)
    assert got is not None and got[1] == 0 and "claude 3.4.5" in got[0]


def test_run_and_probe_both_pass_the_executables_dir_to_the_child(home, monkeypatch, tmp_path):
    seen = []

    class Boom(OSError):
        pass

    def popen(argv, **kw):
        seen.append((argv[0], kw["env"]["PATH"]))
        raise Boom("no")

    monkeypatch.setattr(clibrain.subprocess, "Popen", popen)
    exe = home / ".local" / "bin" / "claude"
    assert clibrain._probe([str(exe), "--version"], 1.0) is None
    with pytest.raises(clibrain.CliError):
        clibrain._run("claude", [str(exe), "-p"], b"", clibrain.time.monotonic() + 5, tmp_path)
    assert seen == [(str(exe), f"{exe.parent}:{clibrain.PATH}")] * 2


def test_the_cli_ids_match_the_pref_names():
    assert tuple(clibrain.CLIS) == prefs.CLI_BINS
    for cli_id, spec in clibrain.CLIS.items():
        assert spec["bin"] == cli_id and f"ai.cli_path.{cli_id}" in prefs.SPEC
        assert os.sep not in spec["bin"]

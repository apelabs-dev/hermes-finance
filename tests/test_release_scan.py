"""The release scan (scripts/dehardcode.py `--release*`): every rule catches a planted positive, every exemption and
placeholder lets its own case through and nothing wider, a hit never echoes what it found, and every tracked file that
ships is clean.

Every planted value is assembled at run time from pieces, so this file holds no private word, home path, credential
shape, real email or real id itself (the scan of the tracked tree covers this file too). The private word is a made-up
one, never a real name.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import dehardcode  # noqa: E402

WORD = "qwzx" + "vk"          # made up
J = "".join


def rules(text: str, words=()) -> list[str]:
    return [rule for _, rule, _ in dehardcode.release_hits_in_text(text, words)]


def entropic(n: int = 32) -> str:
    """A long mixed value with a real key's entropy, made up (base64 of a hash of a fixed string)."""
    raw = base64.b64encode(hashlib.sha256(b"not a key").digest()).decode()
    return re.sub(r"[^A-Za-z0-9]", "", raw)[:n]


POSITIVES = {
    "private-word": f"written by {WORD.title()} at home",
    "personal-path": J(["see /", "Users", "/", WORD, "/notes.txt"]),
    "token:plaid": J(["access-", "production", "-", "a1b2c3d4", "-e5f6-4a7b-9c8d-0e1f2a3b4c5d"]),
    "token:alpaca": J(["key = P", "K", "Q7W3E9R5T1Y8U2I6O4"]),
    "token:anthropic": J(["sk-", "ant-", "api03-", entropic(40)]),
    "token:openai": J(["sk-", "proj-", entropic(40)]),
    "token:github": J(["gh", "p_", entropic(36)]),
    "token:slack": J(["xox", "b-", "1029384756-", entropic(20)]),
    "token:aws": J(["AK", "IA", "Q7W3E9R5T1Y8U2I6"]),
    "token:private-key": J(["-----BEGIN ", "RSA PRIVATE", " KEY-----"]),
    "token:assigned": J(['PLAID_SECRET = "', entropic(30), '"']),
    "email": J(["mail ", "jo.doe", "@", "realmail", ".com"]),
    "long-id": J(["channel ", "1472839016", "52837461"]),
    "hex-id": J(["job ", "a1b2c3", "d4e5f7"]),
}


@pytest.mark.parametrize("rule", sorted(POSITIVES))
def test_every_rule_catches_its_planted_positive(rule):
    assert rule in rules(POSITIVES[rule], {WORD}), POSITIVES[rule]


@pytest.mark.parametrize("text", [
    J(["/", "Users", "/you/projects"]), J(["/", "home", "/<user>/x"]), J(["/", "home", "/$USER/x"]),
    J(["/", "Users", "/owner/hermes-finance"]), J(["C:", "\\", "Users", "\\", "you", "\\x"]),
    J(["owner", "@", "example.com"]), J(["a", "@", "b.test"]), J(["x", "@", "users.noreply.github.com"]),
    J(["1000000", "000000000000"]), J(["1234567890", "1234567890"]), J(["9876543210", "987654321"]),
    J(["sha256:ab", "12345678901234567890", "cd"]), J(["v1.", "23456789012345678"]),
    J(["000000", "000000"]), J(["012345", "6789ab"]), J(["dead", "beef", "cafe"]), J(["123456", "789012"]),
    J(["color: #", "a1b2c3", "d4e5f7"]),
    'PLAID_SECRET = "test-secret-not-real"', 'API_KEY: "${OPENROUTER_API_KEY}"', 'password = "xxxxxxxxxxxxxxxxxxxxxx"',
    f"{WORD}s and x{WORD}: never the whole word",
])
def test_placeholders_and_lookalikes_pass(text):
    assert rules(text, {WORD}) == [], text


def test_allowed_public_is_empty_and_masks_exactly_what_it_lists(monkeypatch):
    assert dehardcode.ALLOWED_PUBLIC == {}                     # no shipped file names the repo's URL
    slug = f"{WORD}/hermes-finance"
    assert rules(J(["github.com/", slug]), {WORD}) == ["private-word"]
    monkeypatch.setattr(dehardcode, "ALLOWED_PUBLIC", {slug: "a repository's own slug"})
    assert rules(J(["github.com/", slug]), {WORD}) == []                            # the listed string, masked
    assert rules(J(["by ", WORD]), {WORD}) == ["private-word"]                      # the login alone still hits


# Each exemption: a sample its regex lets through at its path, which is a hit anywhere else
EXEMPT_SAMPLES = {
    ("tests/*", "token:plaid"): J(["access-", "production", "-aaaaaaaa-2222-3333-4444"]),
    ("tests/*", "token:openai"): J(["sk-", "or-test-", "abcdefghijklmnopqrstu"]),
    ("tests/test_doctor.py", "token:assigned"): J(['PLAID_SECRET = "', "SENTINEL-", entropic(24), '"']),
    ("tests/fixtures/import/card.qfx", "long-id"): J(["<FITID>", "2026", "0115", "2469216000", "3"]),
    ("tests/test_importfile.py", "long-id"): J(["'", "2026", "0115", "2469216000", "3", "'"]),
    ("tests/test_plugin_crons.py", "hex-id"): J(["job ", "0f1e2d", "3c4b5a"]),
    ("tests/test_valuation.py", "hex-id"): J(["VIN 1HGCM ", "82633a", "004352"]),
}


def test_every_exemption_has_a_reason_and_a_sample():
    assert {(pat, rule) for pat, rule, _, _ in dehardcode.RELEASE_EXEMPT} == set(EXEMPT_SAMPLES)
    for pat, rule, rx, why in dehardcode.RELEASE_EXEMPT:
        assert len(why) > 30, (pat, rule)


@pytest.mark.parametrize("key", sorted(EXEMPT_SAMPLES))
def test_an_exemption_covers_its_own_path_and_nothing_wider(key):
    pat, rule = key
    text = EXEMPT_SAMPLES[key]
    path = pat.replace("*", "test_x.py")
    assert rules(text) == [rule]                                                   # the shape is a hit on its own
    assert [h[2] for h in dehardcode.release_scan_bytes(path, text.encode(), ())] == []   # exempt at its path
    assert [h[2] for h in dehardcode.release_scan_bytes("docs/elsewhere.md", text.encode(), ())] == [rule]


def test_a_binary_is_scanned_as_its_raw_bytes():
    data = b"\x89PNG\r\n\x1a\n\x00\x00" + J(["/", "Users", "/", WORD, "/x"]).encode() + b"\x00\xff"
    assert dehardcode.is_binary(data)
    assert sorted(h[2] for h in dehardcode.release_scan_bytes("a.png", data, {WORD})) == ["personal-path", "private-word"]


def test_a_hit_prints_path_line_rule_and_never_what_it_found(tmp_path, monkeypatch, capsys):
    (tmp_path / "docs").mkdir()
    secret = POSITIVES["token:openai"]
    (tmp_path / "docs" / "a.md").write_text(f"ok\n{secret}\nby {WORD}\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x").write_text(WORD)                 # the scanned tree's repository is not the tree
    monkeypatch.setenv(dehardcode.PRIVATE_ENV, WORD)
    monkeypatch.setenv(dehardcode.PRIVATE_FILE_ENV, os.devnull)
    assert dehardcode.main(["--release", str(tmp_path)]) == 1
    out = capsys.readouterr()
    assert out.out.splitlines() == ["docs/a.md:2: token:openai", "docs/a.md:3: private-word"]
    assert secret not in out.out + out.err and WORD not in out.out + out.err
    (tmp_path / "docs" / "a.md").write_text("clean\n")
    assert dehardcode.main(["--release", str(tmp_path)]) == 0
    assert dehardcode.main(["--release"]) == 2 and dehardcode.main(["--release", str(tmp_path / "none")]) == 2


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", HOME=str(repo.parent), GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
               **{dehardcode.PRIVATE_ENV: "", dehardcode.PRIVATE_FILE_ENV: os.devnull})
    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)


def test_the_hook_blocks_a_staged_home_path_anywhere_and_never_echoes_it(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts" / "hooks").mkdir(parents=True)
    for rel in ("scripts/dehardcode.py", "scripts/hooks/pre-commit"):
        (repo / rel).write_bytes((ROOT / rel).read_bytes())
    (repo / "scripts" / "hooks" / "pre-commit").chmod(0o755)
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "core.hooksPath", "scripts/hooks").returncode == 0   # the throwaway repo's own config
    leak = J(["/", "home", "/", WORD, "/finance.db"])
    (repo / "docs").mkdir()
    (repo / "docs" / "notes.md").write_text(f"the DB is at {leak}\n")
    _git(repo, "add", "-A")
    r = _git(repo, "commit", "-q", "-m", "x")
    assert r.returncode != 0 and "docs/notes.md:1: personal-path" in r.stderr, r
    assert leak not in r.stderr and WORD not in r.stderr
    (repo / "docs" / "notes.md").write_text("the DB is at data/finance.db\n")
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-q", "-m", "x").returncode == 0


def test_every_tracked_file_that_ships_is_clean():
    """The release scan of this checkout: every tracked file the public export would hold (all of them in a released
    tree), with the private words of the three sources (CI: the HERMES_FINANCE_TEST_PRIVATE secret)."""
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout: the export itself is scanned by the export")
    ships = dehardcode.shipped_filter(ROOT)
    files = [f for f in dehardcode.tracked_files(ROOT) if ships(f)]
    assert len(files) > 300                                    # it really looked
    hits = dehardcode.release_scan(ROOT, files, dehardcode.private_words())
    assert hits == [], "\n".join(dehardcode.format_release_hit(*h) for h in hits)


def test_the_exemption_paths_ship():
    """An exemption for a file the release doesn't hold is dead weight: each pattern matches a tracked file that ships."""
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    ships = dehardcode.shipped_filter(ROOT)
    files = [f for f in dehardcode.tracked_files(ROOT) if ships(f)]
    for pat, rule, _, _ in dehardcode.RELEASE_EXEMPT:
        assert any(fnmatch.fnmatch(f, pat) for f in files), pat

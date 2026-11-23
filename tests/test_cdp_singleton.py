"""SINGLETON-LEAK (2026-11-15): macOS Chrome ignores TMPDIR. It makes a `com.google.Chrome.XXXXXX` dir in the per-user
temp dir (`getconf DARWIN_USER_TEMP_DIR`) for SingletonSocket/SingletonCookie and links `<user-data-dir>/SingletonSocket`
at it, and a Chrome killed at teardown never removes it. tests/cdp.py deleted the profile and left that dir behind, so
every launch of the driver leaked one: 5 launches left 5 dirs on the base commit (about 980 had piled up on the Mac
mini). Teardown now removes exactly the dir its own profile links to, once its browser is down and only when nothing
still listens on that socket: a `com.google.Chrome.*` dir no profile of ours links is never touched (another run's
Chrome, or an interactive one).

Only the first test starts a real browser. The others build the link by hand and skip where Chrome makes no such dir.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

import cdp


def darwin_temp_dir() -> str | None:
    """The per-user temp dir Chrome puts its singleton dir in, or None where Chrome makes none (not macOS)."""
    try:
        out = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """A stand-in for the per-user temp dir, short: a unix socket's path is 104 bytes at most, and pytest's tmp_path is
    already long. cdp is told this is the user's temp dir, so the code's own check decides what is in it."""
    d = tempfile.mkdtemp(prefix="cdp-sing-", dir="/tmp")
    monkeypatch.setattr(cdp, "_user_temp_dir", lambda: d, raising=False)
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def dead_profile(tmp_path: Path, name: str, singleton: Path) -> Path:
    """A profile of a run that died the hard way: an old hf-cdp-* dir whose SingletonSocket link names `singleton`."""
    profile = tmp_path / name
    profile.mkdir()
    os.symlink(singleton / "SingletonSocket", profile / "SingletonSocket")
    old = time.time() - 3600   # older than ORPHAN_AGE, as a run killed an hour ago is
    os.utime(profile, (old, old))
    return profile


@pytest.mark.skipif(cdp.find_chrome() is None, reason="Chrome is not installed")
def test_a_closed_chrome_takes_its_singleton_dir_with_it():
    """The leak itself: one launch of the driver, and the singleton temp dir its Chrome made is gone after close()."""
    if not darwin_temp_dir():
        pytest.skip("only macOS Chrome uses a com.google.Chrome.* dir in the per-user temp dir")
    made = profile = None
    try:
        b = cdp.Chrome()
        try:
            profile = b.profile
            link = Path(b.profile) / "SingletonSocket"
            assert link.is_symlink(), "Chrome did not link its singleton socket into the profile"
            made = os.path.dirname(os.readlink(link))
            assert os.path.basename(made).startswith("com.google.Chrome.") and os.path.isdir(made), made
            assert b.js("1 + 1") == 2
        finally:
            b.close()
        print(f"\nPROOF close: the singleton dir {Path(made).name} (linked from {Path(profile).name}) exists="
              f"{os.path.exists(made)} after close()")
        assert not os.path.exists(made), f"close() left the Chrome singleton temp dir it made behind: {made}"
        assert not Path(profile).exists(), f"close() left the profile behind: {profile}"
        b.close()   # (idempotent: a second close finds nothing to remove)
    finally:
        if made:   # the test's own clean-up, whatever cdp did
            shutil.rmtree(made, ignore_errors=True)


def test_a_dead_runs_profile_takes_its_singleton_dir_with_it(tmp_path, temp_root):
    """The crash path: a run killed with SIGKILL leaves its profile and its Chrome's singleton dir; reap_orphans()
    deletes both, together."""
    made = temp_root / "com.google.Chrome.DEAD01"
    made.mkdir()
    (made / "SingletonSocket").touch()
    (made / "SingletonCookie").touch()
    profile = dead_profile(tmp_path, "hf-cdp-dead0001", made)
    cdp.reap_orphans(tmp=tmp_path)
    print(f"\nPROOF reap: profile gone={not profile.exists()}, singleton dir gone={not made.exists()}")
    assert not profile.exists(), f"the sweep kept the dead run's profile: {profile}"
    assert not made.exists(), f"the sweep kept the dead run's Chrome singleton temp dir: {made}"


def test_a_singleton_dir_outside_the_users_temp_dir_is_never_touched(tmp_path, temp_root):
    """Only a dir in the per-user temp dir is a candidate: a link naming one somewhere else leaves it alone."""
    elsewhere = tmp_path / "not-the-temp-dir" / "com.google.Chrome.NOTMP1"
    elsewhere.mkdir(parents=True)
    (elsewhere / "SingletonSocket").touch()
    profile = dead_profile(tmp_path, "hf-cdp-else0001", elsewhere)
    cdp.reap_orphans(tmp=tmp_path)
    assert not profile.exists()
    assert elsewhere.is_dir(), f"a com.google.Chrome.* dir outside the per-user temp dir was removed: {elsewhere}"


def test_a_singleton_dir_still_listening_is_kept(tmp_path, temp_root):
    """A dir whose socket still accepts connections belongs to a live Chrome: kept, even for a profile being swept."""
    made = temp_root / "com.google.Chrome.LIVE07"
    made.mkdir()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(made / "SingletonSocket"))
        server.listen(1)
        profile = dead_profile(tmp_path, "hf-cdp-live0001", made)
        cdp.reap_orphans(tmp=tmp_path)
        assert not profile.exists()
        assert made.is_dir(), f"a Chrome singleton dir with a live listener was removed: {made}"
    finally:
        server.close()


def test_a_chrome_dir_no_profile_links_is_never_touched(tmp_path, temp_root):
    """Another run's (or an interactive Chrome's) dir: nothing links it, so no sweep may remove it."""
    other = temp_root / "com.google.Chrome.OTHER1"
    other.mkdir()
    (other / "SingletonSocket").touch()
    made = temp_root / "com.google.Chrome.DEAD02"
    made.mkdir()
    (made / "SingletonSocket").touch()
    dead_profile(tmp_path, "hf-cdp-dead0002", made)
    cdp.reap_orphans(tmp=tmp_path)
    assert not made.exists(), "the dead run's own dir was kept"
    assert other.is_dir(), f"a singleton dir no profile of ours links was removed: {other}"

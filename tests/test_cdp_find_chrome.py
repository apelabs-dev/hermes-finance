"""tests/cdp.py's find_chrome(): HF_CHROME first (and only it, when set), then the fixed install paths (the macOS app
first, so a Mac finds the same Chrome as before), then a Chrome on PATH (a Linux runner's google-chrome or chromium)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cdp  # noqa: E402


def fake_exe(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


def test_hf_chrome_wins_and_a_missing_one_is_no_chrome(tmp_path, monkeypatch):
    exe = fake_exe(tmp_path / "my-chrome")
    monkeypatch.setattr(cdp, "CHROME_PATHS", (fake_exe(tmp_path / "fixed" / "chrome"),))
    monkeypatch.setenv("HF_CHROME", exe)
    assert cdp.find_chrome() == exe
    monkeypatch.setenv("HF_CHROME", str(tmp_path / "nowhere"))
    assert cdp.find_chrome() is None                       # never silently another Chrome


def test_the_fixed_paths_come_before_path(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_CHROME", raising=False)
    fixed = fake_exe(tmp_path / "fixed" / "chrome")
    fake_exe(tmp_path / "bin" / "chromium")
    monkeypatch.setattr(cdp, "CHROME_PATHS", (str(tmp_path / "absent"), fixed))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert cdp.find_chrome() == fixed


def test_a_chrome_on_path_is_found_by_its_usual_names(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_CHROME", raising=False)
    monkeypatch.setattr(cdp, "CHROME_PATHS", (str(tmp_path / "absent"),))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert cdp.find_chrome() is None
    exe = fake_exe(tmp_path / "bin" / "google-chrome-stable")
    assert cdp.find_chrome() == exe
    assert os.path.basename(exe) in cdp.CHROME_NAMES


def test_the_mac_app_is_still_the_first_fixed_path():
    assert cdp.CHROME_PATHS[0] == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

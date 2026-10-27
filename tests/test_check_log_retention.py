"""Retention tests use real scratch directories in a private test root, never the user's existing logs."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check


@pytest.fixture(autouse=True)
def private_root(tmp_path, monkeypatch):
    monkeypatch.setattr(check.tempfile, 'gettempdir', lambda: str(tmp_path))
    return tmp_path


def make(root, name, age):
    directory = root / name
    directory.mkdir()
    (directory / 'ledger.txt').write_text('test artifact')
    os.utime(directory, (age, age))
    return directory


def test_retention_caps_stale_directories(private_root):
    stale = [make(private_root, f'hf-check-{999000+i}-test', i+1) for i in range(5)]
    own = make(private_root, f'hf-check-{os.getpid()}-current', 10)
    check._prune_old_logs(own)
    assert own.exists() and stale[-1].exists()
    assert sum(p.exists() for p in stale) == check.KEEP_OLD_LOGS - 1


def test_retention_preserves_live_run(private_root):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        live = make(private_root, f'hf-check-{child.pid}-live', 1)
        own = make(private_root, f'hf-check-{os.getpid()}-current', 10)
        for i in range(5):
            make(private_root, f'hf-check-{999100+i}-test', i+2)
        check._prune_old_logs(own)
        assert live.exists() and own.exists()
    finally:
        child.kill()
        child.wait()


def test_retention_leaves_old_unrecognized_directory(private_root):
    unknown = make(private_root, 'hf-check-notapid-unknown', 1)
    own = make(private_root, f'hf-check-{os.getpid()}-current', 10)
    for i in range(5):
        make(private_root, f'hf-check-{999200+i}-test', i+2)
    check._prune_old_logs(own)
    assert unknown.exists()


def test_permission_failure_does_not_prove_a_dead_process(private_root, monkeypatch):
    protected = make(private_root, 'hf-check-999300-protected', 1)
    own = make(private_root, f'hf-check-{os.getpid()}-current', 10)
    for i in range(5):
        make(private_root, f'hf-check-{999400+i}-test', i+2)
    real_kill = os.kill
    def denied(pid, signal):
        if pid == 999300:
            raise PermissionError('test denied inspection')
        return real_kill(pid, signal)
    monkeypatch.setattr(check.os, 'kill', denied)
    check._prune_old_logs(own)
    assert protected.exists()


def test_chrome_log_run_puts_the_chromes_logs_in_the_runs_own_dir(private_root, monkeypatch):
    """t_37bf3e0b: HF_CHROME_LOG=run sends tests/cdp.py's Chrome logs (and a wedged one's samples) into the run's log
    dir, which CI uploads; a wedge report there is what keeps that dir after a run that passed."""
    logs = make(private_root, f'hf-check-{os.getpid()}-current', 10)
    monkeypatch.setenv(check.CHROME_LOG_ENV, 'run')
    chrome = check.chrome_log_dir(logs)
    assert chrome == logs / 'chrome' and os.environ[check.CHROME_LOG_ENV] == str(logs / 'chrome')
    assert check.wedged_chromes(chrome) == []
    chrome.mkdir()
    (chrome / 'chrome-hf-cdp-x.log').write_text('a clean one is deleted by cdp; a wedged one stays')
    (chrome / 'chrome-hf-cdp-x.wedge.txt').write_text('why: goto ...')
    assert check.wedged_chromes(chrome) == [chrome / 'chrome-hf-cdp-x.wedge.txt']


def test_chrome_log_is_passed_through_as_it_is_or_not_at_all(private_root, monkeypatch):
    logs = make(private_root, f'hf-check-{os.getpid()}-current', 10)
    monkeypatch.delenv(check.CHROME_LOG_ENV, raising=False)
    assert check.chrome_log_dir(logs) is None and check.CHROME_LOG_ENV not in os.environ
    assert check.wedged_chromes(None) == []
    mine = private_root / 'mine'
    monkeypatch.setenv(check.CHROME_LOG_ENV, str(mine))
    assert check.chrome_log_dir(logs) == mine and os.environ[check.CHROME_LOG_ENV] == str(mine)
    assert check.CHROME_LOG_ENV in check.safe_env()   # (not taken for a credential: it reaches pytest and its Chromes)

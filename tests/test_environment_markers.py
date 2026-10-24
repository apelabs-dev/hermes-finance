"""Environment skips depend on declared host capabilities, never failed product assertions."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import conftest
import hermes_runtime as hr


def request(marker):
    return SimpleNamespace(node=SimpleNamespace(get_closest_marker=lambda name: marker))


def test_unmarked_unit_contract_never_probes_the_runtime(monkeypatch):
    monkeypatch.setattr(hr, "environment_reason", lambda _: pytest.fail("unexpected runtime probe"))
    conftest.environment_requirements.__wrapped__(request(None))


def test_missing_declared_capability_skips_with_explicit_reason(monkeypatch):
    monkeypatch.setattr(hr, "environment_reason", lambda feature: f"missing {feature}")
    with pytest.raises(pytest.skip.Exception, match="env_dependent: missing scheduler_delivery"):
        conftest.environment_requirements.__wrapped__(request(pytest.mark.env_dependent("scheduler_delivery").mark))


def test_available_capability_runs_and_unexpected_probe_failure_is_not_skipped(monkeypatch):
    marked = request(pytest.mark.env_dependent("runtime").mark)
    monkeypatch.setattr(hr, "environment_reason", lambda _: None)
    conftest.environment_requirements.__wrapped__(marked)
    def failure(_):
        raise AssertionError("product failure")
    monkeypatch.setattr(hr, "environment_reason", failure)
    with pytest.raises(AssertionError, match="product failure"):
        conftest.environment_requirements.__wrapped__(marked)


def test_unknown_capability_is_an_error():
    with pytest.raises(ValueError, match="unknown environment capability"):
        hr.environment_reason("typo")


def test_single_query_flag_is_removed_before_collection_and_from_children():
    env = dict(os.environ, HERMES_SINGLE_QUERY_SESSION="1")
    code = "import conftest, os, subprocess, sys; assert 'HERMES_SINGLE_QUERY_SESSION' not in os.environ; subprocess.run([sys.executable, '-c', \"import os; assert 'HERMES_SINGLE_QUERY_SESSION' not in os.environ\"], check=True)"
    env["PYTHONPATH"] = str(Path(conftest.__file__).parent)
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr

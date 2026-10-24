"""A pytest plugin: no private word in a test report. CI runs the suite with the private words in its environment (a
secret), and its log is public; a failing test's report shows the assertion's values, the frames' arguments and the
captured output, any of which can hold the very word a privacy test found. Every report's text has each private word
(scripts/dehardcode.py's sources, matched as it matches them) replaced by REDACTED before pytest prints or sends it.

tests/conftest.py loads it; `python -m pytest -p redact` (with tests/ on the path) loads it anywhere else."""
from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dehardcode  # noqa: E402

REDACTED = "<private>"


@lru_cache(maxsize=1)
def pattern() -> Optional[re.Pattern]:
    return dehardcode.Matcher(dehardcode.private_words()).rx


def redact(text: str, rx: Optional[re.Pattern] = None) -> str:
    rx = pattern() if rx is None else rx
    return rx.sub(REDACTED, text) if rx else text


def redact_report(rep, rx: Optional[re.Pattern] = None) -> None:
    rx = pattern() if rx is None else rx
    if not rx:
        return
    if isinstance(rep.longrepr, tuple) and len(rep.longrepr) == 3:          # a skip: (path, line, reason)
        path, line, reason = rep.longrepr
        rep.longrepr = (path, line, redact(str(reason), rx))
    elif rep.longrepr is not None:
        text = str(rep.longrepr)
        if rx.search(text):
            rep.longrepr = redact(text, rx)                                   # pytest prints a str report as it is
    rep.sections = [(title, redact(body, rx)) for title, body in rep.sections]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    redact_report(outcome.get_result())


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    outcome = yield
    redact_report(outcome.get_result())

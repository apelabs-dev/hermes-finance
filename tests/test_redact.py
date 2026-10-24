"""tests/redact.py: a failing test's report (its assertion values, its frames' arguments, its captured output) never
shows a private word, since CI runs with the private words in its environment and its log is public."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import redact  # noqa: E402

TESTS = Path(__file__).resolve().parent
WORD = "zyxwvu" + "qtsr"                     # an invented private word, split so this file holds no whole match

LEAKY = f'''
def check(words):
    raise AssertionError("found " + sorted(words)[0])

def test_assert_values():
    hits = [("README.md", 3, "private-word", "{WORD[:6]}" + "{WORD[6:]}")]
    assert hits == []

def test_frame_arguments_and_output():
    print("captured: " + "{WORD[:6]}".upper() + "{WORD[6:]}")
    check({{"{WORD[:6]}" + "{WORD[6:]}"}})
'''


def test_a_failing_report_names_no_private_word(tmp_path):
    (tmp_path / "test_leaky.py").write_text(LEAKY)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "HERMES_FINANCE_"))}
    env |= {"HERMES_FINANCE_TEST_PRIVATE": WORD, "HERMES_FINANCE_PRIVATE_FILE": str(tmp_path / "none"),
            "PYTHONPATH": os.pathsep.join([str(TESTS), env.get("PYTHONPATH", "")])}
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "redact", "-p", "no:cacheprovider", "-rA", "-l",
                        "--rootdir", str(tmp_path), "--confcutdir", str(tmp_path), str(tmp_path / "test_leaky.py")],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert r.returncode == 1 and "2 failed" in out, out
    assert out.count(redact.REDACTED) >= 3          # the assertion, the argument, the captured line
    assert WORD not in out.lower()


def test_redact_matches_as_the_scanner_does():
    rx = redact.dehardcode.Matcher({WORD, "4417"}).rx
    assert redact.redact(f"a {WORD.upper()} b, 4417 c, 44170 d, x{WORD}", rx) == \
        f"a {redact.REDACTED} b, {redact.REDACTED} c, 44170 d, x{WORD}"

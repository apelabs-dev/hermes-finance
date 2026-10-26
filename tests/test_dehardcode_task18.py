"""TASK-18 section 4: no personal data in the code. The owner's name, email, car, address and bank are data (the DB,
the settings), never strings in src/ (static/ is inside it) or the Hermes plugin, nor in the test fixtures and their
generators, which use invented values only. No private word is written here either: they come from the untracked
sources scripts/dehardcode.py reads (the runner's own login, home and name; HERMES_FINANCE_TEST_PRIVATE; the word file
HERMES_FINANCE_PRIVATE_FILE names, else .private-words). See docs/dehardcode.md."""

from __future__ import annotations

import sys
from pathlib import Path

from hermes_finance import config, web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import dehardcode  # noqa: E402  (the one scanner: these gates, the release scan and scripts/hooks/pre-commit share it)


def _hits(roots) -> tuple[list[str], list[tuple[str, int, str]]]:
    files = list(dehardcode.tree_files(ROOT, roots))
    matcher = dehardcode.Matcher(dehardcode.private_words())
    return files, dehardcode.check(files, lambda rel: (ROOT / rel).read_text(errors="replace"), matcher)


def test_no_personal_token_in_the_code():
    files, hits = _hits(dehardcode.SCANNED_ROOTS)
    assert len(files) > 40                               # it really looked (src, static, the plugin)
    assert {f.split("/")[0] for f in files} == set(dehardcode.SCANNED_ROOTS)
    assert hits == [], "\n".join(f"{rel}:{n}: private-word" for rel, n, _ in hits)   # never the word: CI logs


def test_no_private_token_in_the_test_fixtures_and_generators():
    """The fixtures (tests/fixtures/*.sql, csv, ofx, qfx) and their generators (conftest, demo_data, gate_dbs,
    sync_scene, rules_scene, factory) and every test carry invented values only: one of a local install's private words
    (a bank, a mask, a vehicle, a street, a ZIP) reintroduced anywhere under tests/ fails here."""
    files, hits = _hits(dehardcode.FIXTURE_ROOTS)
    assert len(files) > 150                              # it really looked (the fixtures, the generators, the tests)
    for name in ("tests/conftest.py", "tests/demo_data.py", "tests/gate_dbs.py", "tests/sync_scene.py",
                 "tests/rules_scene.py", "tests/factory.py", "tests/fixtures/schema_v7.sql",
                 "tests/fixtures/import/checking.ofx", "tests/fixtures/import/card.qfx"):
        assert name in files, name
    assert hits == [], "\n".join(f"{rel}:{n}: private-word" for rel, n, _ in hits)   # never the word: CI logs


def test_the_gates_catch_a_planted_word_and_leave_a_lookalike():
    # made up, none of them anyone's; given the way a local install gives its own (the env var, the word file)
    # (and no word file: unnamed, it is this machine's default one, whose real words aren't this test's)
    words = dehardcode.private_words({dehardcode.PRIVATE_ENV: "Zyxwvu, Qtsr Grove ,,",
                                      dehardcode.PRIVATE_FILE_ENV: "/nonexistent/private-words"}, include_own=False)
    assert words == {"zyxwvu", "qtsr grove"}
    m = dehardcode.Matcher(words)
    assert [t for _, _, t in m.scan("a.py", "by Zyxwvu, near QTSR GROVE")] == ["Zyxwvu", "QTSR GROVE"]
    assert m.scan("a.py", "zyxwvutsr, qtsr groves, xzyxwvu") == []        # a whole word only
    assert dehardcode.Matcher({"0000"}).scan("a.sql", "'card ••0000'\n'10000'") == [("a.sql", 1, "0000")]


def test_no_default_login_and_no_personal_user_agent():
    assert not hasattr(config, "DEFAULT_ALLOWED_USERS")
    from hermes_finance import photos
    assert "github.com" not in photos.USER_AGENT
    html = web.render_page({"slots_used": 0, "slot_limit": 10, "blocked_duplicate_links": 0, "items": []})
    assert dehardcode.Matcher(dehardcode.private_words()).scan("page.html", html) == []
    assert dehardcode.release_hits_in_text(html, dehardcode.private_words()) == []

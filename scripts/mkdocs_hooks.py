"""MkDocs hook (mkdocs.yml `hooks:`): the docs build fails while a generated reference page is stale, so the CLI, tool
and settings pages can't drift from the code. The pages come from scripts/gen_docs_reference.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True   # no __pycache__ for the generator or src/ (this hook's own: PYTHONDONTWRITEBYTECODE=1)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_docs_reference  # noqa: E402


def on_pre_build(config, **kwargs):
    from mkdocs.exceptions import PluginError
    try:
        stale = gen_docs_reference.stale()
    except SystemExit as e:   # not Python 3.12: the pages can't be checked, so the build can't vouch for them
        raise PluginError(str(e)) from None
    if stale:
        raise PluginError("generated reference pages are out of date: " + ", ".join(stale)
                          + ". Run: python scripts/gen_docs_reference.py")

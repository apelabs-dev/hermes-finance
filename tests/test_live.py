"""The ONE live Plaid call: /link/token/create on production (free, creates no Item).

Excluded by default; run explicitly with:  uv run pytest -m live
"""

from __future__ import annotations

import pytest

from hermes_finance import config
from hermes_finance.plaid import PlaidClient


@pytest.mark.live
def test_live_link_token_create_production():
    settings = config.load_settings()
    assert settings.env == "production"
    resp = PlaidClient(settings).link_token_create()
    assert resp["link_token"].startswith("link-production-")

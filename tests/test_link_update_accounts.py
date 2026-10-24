"""Reconnect (update mode) must let the user pick accounts, so new accounts at a connected bank
(e.g. a PayPal credit card opened after the first link) can join the SAME Item without a new slot."""

from __future__ import annotations

from conftest import TOKEN_A, add_item
from hermes_finance import link


def test_update_link_token_enables_account_selection(conn, client, fake):
    add_item(conn, "item_a", "ins_1", token=TOKEN_A)
    fake.on("/link/token/create", {"link_token": "link-production-u", "expiration": "x"})
    out = link.update_link_token(conn, client, "item_a")
    assert out["link_token"] == "link-production-u"
    (sent,) = fake.bodies("/link/token/create")
    assert sent["access_token"] == TOKEN_A
    assert sent["update"] == {"account_selection_enabled": True}
    # update mode never asks for products and never exchanges: no new Item, no slot
    assert "products" not in sent
    assert fake.paths() == ["/link/token/create"]

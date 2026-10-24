"""Tiny Plaid HTTP client (stdlib urllib). The HTTP layer is injectable so tests never hit Plaid.

Nothing in here logs request or response bodies: bodies carry the secret and access tokens.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable

from . import redact
from .config import Settings

log = logging.getLogger(__name__)

BASE_URLS = {
    "production": "https://production.plaid.com",
    "sandbox": "https://sandbox.plaid.com",
}
PLAID_VERSION = "2020-09-14"
CLIENT_NAME = "Hermes Budget"
CLIENT_USER_ID = "hermes-finance-local-user"
# Endpoints that create Items out of thin air. Never allowed from this code base.
FORBIDDEN_PREFIXES = ("/sandbox/",)

# transport(url, body_bytes, headers, timeout) -> (http_status, response_bytes)
Transport = Callable[[str, bytes, dict, float], "tuple[int, bytes]"]


def urllib_transport(url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
    if os.environ.get("HERMES_PLAID") == "off":   # the tests, and every process a test starts: never a real Plaid call
        raise OSError("Plaid is switched off (HERMES_PLAID=off)")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class PlaidError(Exception):
    def __init__(self, error_type: str, error_code: str, error_message: str = "",
                 status: int | None = None, request_id: str | None = None,
                 display_message: str | None = None):
        self.error_type = error_type
        self.error_code = error_code
        self.error_message = redact.scrub(error_message or "")
        self.display_message = display_message
        self.status = status
        self.request_id = request_id
        super().__init__(f"{error_code}: {self.error_message}")

    @property
    def is_item_error(self) -> bool:
        return self.error_type == "ITEM_ERROR"


class PlaidClient:
    def __init__(self, settings: Settings, transport: Transport | None = None, timeout: float = 60.0):
        if settings.env not in BASE_URLS:
            raise ValueError(f"unsupported PLAID_ENV {settings.env!r}")
        self.settings = settings
        self.base_url = BASE_URLS[settings.env]
        self.transport = transport or urllib_transport
        self.timeout = timeout
        redact.register(settings.secret)

    def post(self, path: str, body: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        """One Plaid call. `timeout` (seconds) overrides the client's for this call (HOTFIX-1: the refresh endpoints)."""
        if path.startswith(FORBIDDEN_PREFIXES):
            raise RuntimeError(f"refusing to call {path}: Item-creating endpoints are forbidden")
        redact.register(body.get("access_token"))
        payload = dict(body, client_id=self.settings.client_id, secret=self.settings.secret)
        headers = {"Content-Type": "application/json", "Plaid-Version": PLAID_VERSION}
        try:
            status, raw = self.transport(self.base_url + path, json.dumps(payload).encode(), headers,
                                         self.timeout if timeout is None else timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise PlaidError("NETWORK_ERROR", "NETWORK_ERROR", f"{path}: {e}") from None
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        if status >= 400 or data.get("error_code"):
            raise PlaidError(
                data.get("error_type") or "API_ERROR",
                data.get("error_code") or f"HTTP_{status}",
                data.get("error_message") or "",
                status=status,
                request_id=data.get("request_id"),
                display_message=data.get("display_message"),
            )
        redact.register(data.get("access_token"))
        log.debug("plaid %s ok (request_id=%s)", path, data.get("request_id"))
        return data

    # --- Link ----------------------------------------------------------------------------------
    def _link_base(self) -> dict[str, Any]:
        return {
            "client_name": CLIENT_NAME,
            "language": "en",
            "country_codes": ["US"],
            "user": {"client_user_id": CLIENT_USER_ID},
        }

    def link_token_create(self) -> dict[str, Any]:
        """Link token for connecting a NEW bank (this is the only flow that can create an Item)."""
        return self.post("/link/token/create", {
            **self._link_base(),
            "products": ["transactions"],
            "optional_products": ["liabilities", "investments"],
            "transactions": {"days_requested": 730},
        })

    def link_token_create_update(self, access_token: str) -> dict[str, Any]:
        """Update-mode Link token: re-authenticates an existing Item, never creates one.

        account_selection_enabled lets the user tick accounts opened since the first link (a new card at
        the same bank) so they join this Item instead of costing a new slot."""
        return self.post("/link/token/create", {
            **self._link_base(),
            "access_token": access_token,
            "update": {"account_selection_enabled": True},
        })

    def item_public_token_exchange(self, public_token: str) -> dict[str, Any]:
        return self.post("/item/public_token/exchange", {"public_token": public_token})

    # --- Item data -----------------------------------------------------------------------------
    def item_get(self, access_token: str) -> dict[str, Any]:
        return self.post("/item/get", {"access_token": access_token})

    def transactions_sync(self, access_token: str, cursor: str | None, count: int = 500) -> dict[str, Any]:
        body: dict[str, Any] = {
            "access_token": access_token,
            "count": count,
            "options": {"include_original_description": True},
        }
        if cursor:
            body["cursor"] = cursor
        return self.post("/transactions/sync", body)

    # TASK-22: the "refresh endpoints": ask the bank for fresh data now. Plaid answers at once and fetches in the
    # background (the new data lands on a later /transactions/sync). Billed per request on paid plans; free on Trial.
    def transactions_refresh(self, access_token: str, timeout: float | None = None) -> dict[str, Any]:
        return self.post("/transactions/refresh", {"access_token": access_token}, timeout)

    def investments_refresh(self, access_token: str, timeout: float | None = None) -> dict[str, Any]:
        return self.post("/investments/refresh", {"access_token": access_token}, timeout)

    def accounts_get(self, access_token: str) -> dict[str, Any]:
        return self.post("/accounts/get", {"access_token": access_token})

    def liabilities_get(self, access_token: str) -> dict[str, Any]:
        return self.post("/liabilities/get", {"access_token": access_token})

    def investments_holdings_get(self, access_token: str) -> dict[str, Any]:
        return self.post("/investments/holdings/get", {"access_token": access_token})

    def institutions_get(self, count: int = 1, offset: int = 0) -> dict[str, Any]:
        return self.post("/institutions/get", {"count": count, "offset": offset, "country_codes": ["US"]})

    def institutions_get_by_id(self, institution_id: str) -> dict[str, Any]:
        """Name, logo (base64 PNG or null), primary colour and url of one institution. Free; creates nothing."""
        return self.post("/institutions/get_by_id", {"institution_id": institution_id, "country_codes": ["US"],
                                                      "options": {"include_optional_metadata": True}})

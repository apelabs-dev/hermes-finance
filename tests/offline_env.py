"""The offline switches: the one list every from-scratch child environment (and every script's safe-environment block)
carries, because a child process has none of conftest's monkeypatches, only its environment.

VENDOR_SWITCHES are the ones the vendor-logo fetch reads (`logos.VENDOR_LOGOS`: HERMES_PLAID, HERMES_SIMPLEFIN): without
them a child that starts the app really fetches the vendors' sites on a background thread.
OFFLINE_SWITCHES adds the rest of the safe environment (Alpaca, Chrome's DevTools). Importable from tests/ (conftest puts
it on sys.path) and from scripts/ (they add tests/ to sys.path); `tests/test_offline_env_guard.py` keeps every site honest.
"""
from __future__ import annotations

VENDOR_SWITCHES = {"HERMES_PLAID": "off", "HERMES_SIMPLEFIN": "off"}
OFFLINE_SWITCHES = {**VENDOR_SWITCHES, "HERMES_ALPACA": "off", "HERMES_CHROME_DEVTOOLS": "off"}

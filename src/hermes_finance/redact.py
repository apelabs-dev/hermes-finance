"""Scrub secrets (Plaid secret, access tokens) out of anything we print or log.

Two layers:
  * exact values registered at runtime (the Plaid secret, every access token we load), and
  * a pattern for anything shaped like a Plaid access/public token, registered or not.
"""

from __future__ import annotations

import logging
import re
import threading

_TOKEN_RE = re.compile(r"\b(access|public)-(production|sandbox|development)-[A-Za-z0-9-]+")
_lock = threading.Lock()
_secrets: set[str] = set()

MASK = "[REDACTED]"


def register(value: str | None) -> None:
    """Remember a secret value so scrub() removes it from output."""
    if value and len(value) >= 6:
        with _lock:
            _secrets.add(value)


def scrub(text: object) -> str:
    s = str(text)
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        if secret in s:
            s = s.replace(secret, MASK)
    return _TOKEN_RE.sub(lambda m: f"{m.group(1)}-{m.group(2)}-{MASK}", s)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = None
        return True


def install_logging(level: int = logging.INFO) -> None:
    """Configure root logging to stderr with redaction on every handler."""
    root = logging.getLogger()
    if not any(isinstance(f, RedactingFilter) for h in root.handlers for f in h.filters):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
    root.setLevel(level)

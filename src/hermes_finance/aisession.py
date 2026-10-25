"""The per-install session id an OpenCode endpoint requires (the Ask panel's api mode, chat.py).

OpenCode's gateway (opencode.ai, e.g. https://opencode.ai/zen/go/v1) refuses a /chat/completions request without an
`x-opencode-session` header (400 MissingSessionID). When the configured AI base URL's host is opencode.ai or one of its
subdomains (the parsed host name, never a substring of the URL), every request of a turn carries one: a random UUID made
once per install, the first time a request goes to such a host, and kept in the settings table under SETTING. It's
never derived from the API key, never written to the secrets file, and not a prefs setting (no route reads or writes
it). A backup leaves it out (backup.STRIPPED_SETTINGS), so a restored copy on another computer makes its own. Any other
host gets no such header, and no id is made for it.

Stdlib only.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from urllib.parse import urlsplit

from .db import now

SETTING = "ai.opencode_session"
HEADER = "x-opencode-session"
HOST = "opencode.ai"
_SHAPE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")   # uuid4(), as str() writes it


def is_opencode(url: str | None) -> bool:
    """Whether `url`'s host is opencode.ai or a subdomain of it (any scheme or port; one trailing dot allowed)."""
    try:
        host = urlsplit(url or "").hostname   # (lower-cased; userinfo, path, query and fragment aren't part of it)
    except ValueError:
        return False
    if not host:
        return False
    host = host[:-1] if host.endswith(".") else host
    return host == HOST or (host.endswith("." + HOST) and not host.startswith("."))


def _stored(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """(the row's raw value or None when there's no row, its id when it's a well-formed one)."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (SETTING,)).fetchone()
    if row is None:
        return None, None
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return row[0], None
    return row[0], value if isinstance(value, str) and _SHAPE.fullmatch(value) else None


def session_id(conn: sqlite3.Connection) -> str:
    """This install's id: the stored one, else a new one saved first. Two first turns at once agree on one (INSERT OR
    IGNORE, then read back); a stored value that isn't a well-formed id is replaced, never sent. A write that finds the
    database locked raises sqlite3.OperationalError having committed nothing (chat._write tries it again)."""
    raw, current = _stored(conn)
    if current:
        return current
    fresh = json.dumps(str(uuid.uuid4()))
    with conn:
        if raw is not None:   # malformed: replaced, unless another connection replaced it in between
            conn.execute("UPDATE settings SET value = ?, updated_at = ? WHERE key = ? AND value = ?",
                         (fresh, now(), SETTING, raw))
        else:
            conn.execute("INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                         (SETTING, fresh, now()))
    _, current = _stored(conn)
    if current is None:   # (another connection wrote a malformed value in between: nothing is sent)
        raise RuntimeError(f"the {SETTING} setting couldn't be saved")
    return current


def headers(conn: sqlite3.Connection, url: str | None) -> dict[str, str]:
    """The extra headers a request to `url` needs: {HEADER: this install's id} for an OpenCode host, else none (and no
    id is made)."""
    return {HEADER: session_id(conn)} if is_opencode(url) else {}

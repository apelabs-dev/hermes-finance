"""Bank and holding logos: fetched server-side once, checked, cached in the DB, served from /api/.../logo.

The page never loads an image from anywhere but this server: the CSP img-src stays first-party (plus Plaid's
merchant/category hosts it already had). Every fetch is https only (redirects too), 5 s, at most 256 KB, and the
bytes must be a real image of an image content type (never SVG), at least 16 px, and not blank.

Banks (`institutions`): Plaid's free /institutions/get_by_id with optional metadata (logo, colour, url) first.
When it has no logo: the bank's own apple-touch-icon, else its largest <link rel=icon>, else /favicon.ico, from
the url Plaid gives. Otherwise the row keeps no logo and the page draws initials in the bank's primary colour; the
daily `finance sync` looks such a bank up again once a day (INSTITUTION_RETRY_DAYS, so a network blip at connect time
heals the next day), and a logo once found is kept. Alpaca's icon (alpaca.py) is retried on the same schedule.
Holdings (`security_logos`): financialmodelingprep.com/image-stock/<TICKER>.png for stocks, ETFs and funds.
A miss is remembered and retried after 30 days; network trouble is not a miss (the next run tries again).
"""

from __future__ import annotations

import base64
import binascii
import http.client
import logging
import re
import sqlite3
import struct
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from .db import now
from .plaid import PlaidClient

log = logging.getLogger(__name__)

MAX_BYTES = 256 * 1024
TIMEOUT = 5.0
MIN_PX = 16
MAX_PX = 1024          # bigger than any logo; also bounds the blank check's work
MISS_RETRY_DAYS = 30
INSTITUTION_RETRY_DAYS = 1   # a bank (or Alpaca) drawn with initials: tried again by the next day's run, never sooner
MAX_ICON_TRIES = 4     # icon URLs tried per bank site, after its home page
USER_AGENT = "hermes-finance/0.1 (logo cache; one fetch per bank or ticker)"
FMP_URL = "https://financialmodelingprep.com/image-stock/{}.png"
INSTITUTION_ID = re.compile(r"ins_[A-Za-z0-9_]{1,40}")
TICKER = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,11}")
COLOR = re.compile(r"#[0-9a-fA-F]{6}")
LOGO_TYPES = {"equity", "etf", "mutual fund"}   # Plaid security.type values that have a company/fund logo
# content type -> the magic bytes its data must start with
MAGIC = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
    "image/x-icon": (b"\x00\x00\x01\x00",),
}
ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg", "image/vnd.microsoft.icon": "image/x-icon",
           "image/ico": "image/x-icon", "image/icon": "image/x-icon", "image/x-ico": "image/x-icon"}


class LogoError(Exception):
    """A definite no: not https, too big, not an image, blank, a 404..."""


class FetchError(LogoError):
    """The network itself failed (timeout, DNS, refused connection): worth trying again later."""


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes
    url: str


Fetch = Callable[[str], Response]


# --- fetching -----------------------------------------------------------------------------------------

def _https(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url.strip())
        parts.port  # noqa: B018 - ValueError on a malformed port
    except ValueError:
        return False
    return (parts.scheme == "https" and bool(parts.hostname) and parts.username is None
            and parts.password is None)


class HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 4

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _https(newurl):
            raise LogoError(f"refused a redirect off https ({str(newurl)[:80]})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(HttpsOnlyRedirect)


def _open(req: urllib.request.Request, timeout: float):
    """The one place that touches the network."""
    return _OPENER.open(req, timeout=timeout)


def http_get(url: str, timeout: float = TIMEOUT, max_bytes: int = MAX_BYTES) -> Response:
    """GET an https URL (redirects must stay on https). An image over `max_bytes` is an error; an HTML page is cut
    off there instead (only its <head> matters). An HTTP error status comes back as a Response."""
    if not _https(url):
        raise LogoError(f"https only: {str(url)[:80]!r}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "image/png,image/*;q=0.9,text/html;q=0.5"})
    try:
        resp = _open(req, timeout)
    except urllib.error.HTTPError as e:
        ctype = e.headers.get("Content-Type", "") if e.headers else ""
        e.close()
        return Response(e.code, ctype, b"", url)
    except LogoError:
        raise
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        raise FetchError(f"{urlsplit(url).hostname}: {type(e).__name__}") from None
    with resp:
        final = resp.geturl() or url
        if not _https(final):
            raise LogoError("ended up off https")
        ctype = resp.headers.get("Content-Type", "")
        page = "html" in ctype.lower()
        length = (resp.headers.get("Content-Length") or "").strip()
        if not page and length.isdigit() and int(length) > max_bytes:
            raise LogoError(f"over {max_bytes // 1024} KB")
        try:
            body = resp.read(max_bytes + 1)
        except (http.client.HTTPException, OSError) as e:
            raise FetchError(f"{urlsplit(url).hostname}: {type(e).__name__}") from None
        if len(body) > max_bytes:
            if not page:
                raise LogoError(f"over {max_bytes // 1024} KB")
            body = body[:max_bytes]
        return Response(getattr(resp, "status", 200), ctype, body, final)


# --- image checks -------------------------------------------------------------------------------------

def sniff(body: bytes) -> str | None:
    """The image type the bytes really are (PNG, JPEG, GIF, WebP, ICO), or None."""
    for ctype, magics in MAGIC.items():
        if any(body.startswith(m) for m in magics):
            if ctype == "image/webp" and body[8:12] != b"WEBP":
                continue
            return ctype
    return None


def _png_size(body: bytes) -> tuple[int, int]:
    if len(body) < 33 or body[12:16] != b"IHDR":
        raise LogoError("not a readable PNG")
    return struct.unpack(">II", body[16:24])


def _jpeg_size(body: bytes) -> tuple[int, int]:
    i = 2
    while i + 9 < len(body):
        if body[i] != 0xFF:
            raise LogoError("not a readable JPEG")
        marker = body[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = struct.unpack(">H", body[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", body[i + 5:i + 9])
            return w, h
        i += 2 + seg
    raise LogoError("not a readable JPEG")


def _webp_size(body: bytes) -> tuple[int, int]:
    kind = body[12:16]
    if kind == b"VP8X" and len(body) >= 30:
        return 1 + int.from_bytes(body[24:27], "little"), 1 + int.from_bytes(body[27:30], "little")
    if kind == b"VP8L" and len(body) >= 25:
        b = int.from_bytes(body[21:25], "little")
        return 1 + (b & 0x3FFF), 1 + ((b >> 14) & 0x3FFF)
    if kind == b"VP8 " and len(body) >= 30:
        return int.from_bytes(body[26:28], "little") & 0x3FFF, int.from_bytes(body[28:30], "little") & 0x3FFF
    raise LogoError("not a readable WebP")


def _ico_entries(body: bytes) -> list[tuple[int, int, bytes]]:
    """(width, height, image bytes) for each image in an .ico."""
    if len(body) < 6:
        raise LogoError("not a readable ICO")
    n = struct.unpack("<H", body[4:6])[0]
    if not 0 < n <= 64 or len(body) < 6 + 16 * n:
        raise LogoError("not a readable ICO")
    out = []
    for k in range(n):
        w, h, _, _, _, _, size, offset = struct.unpack("<BBBBHHII", body[6 + 16 * k:22 + 16 * k])
        data = body[offset:offset + size]
        if len(data) != size or not size:
            raise LogoError("not a readable ICO")
        out.append((w or 256, h or 256, data))
    return out


def image_size(kind: str, body: bytes) -> tuple[int, int]:
    if kind == "image/png":
        return _png_size(body)
    if kind == "image/jpeg":
        return _jpeg_size(body)
    if kind == "image/gif":
        if len(body) < 10:
            raise LogoError("not a readable GIF")
        return struct.unpack("<HH", body[6:10])
    if kind == "image/webp":
        return _webp_size(body)
    w, h, _ = max(_ico_entries(body), key=lambda e: e[0] * e[1])
    return w, h


def png_blank(body: bytes) -> bool:
    """True when every pixel is the same colour (or fully transparent). Images this checker can't decode
    (interlaced, 16-bit) count as not blank."""
    pos, idat, head, palette = 8, [], None, None
    while pos + 8 <= len(body):
        n = struct.unpack(">I", body[pos:pos + 4])[0]
        kind, data = body[pos + 4:pos + 8], body[pos + 8:pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            head = struct.unpack(">IIBBBBB", data[:13])
        elif kind == b"PLTE":
            palette = data
        elif kind == b"IDAT":
            idat.append(data)
        elif kind == b"IEND":
            break
    if not head or not idat:
        raise LogoError("not a readable PNG")
    width, height, depth, ctype, _, _, interlace = head
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None or interlace or depth not in (1, 2, 4, 8):
        return False
    bits = depth * channels
    stride = (width * bits + 7) // 8
    bpp = max(1, bits // 8)
    try:
        raw = zlib.decompressobj().decompress(b"".join(idat), height * (stride + 1) + 1)
    except zlib.error:
        raise LogoError("not a readable PNG") from None
    if len(raw) < height * (stride + 1):
        raise LogoError("not a readable PNG")
    seen = set()
    prev = bytearray(stride)
    for y in range(height):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                if f == 1:
                    line[i] = (line[i] + a) & 255
                elif f == 2:
                    line[i] = (line[i] + b) & 255
                elif f == 3:
                    line[i] = (line[i] + ((a + b) >> 1)) & 255
                elif f == 4:
                    c = prev[i - bpp] if i >= bpp else 0
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        if depth < 8:  # several pixels per byte: every byte (but the padded last one) the same, one value in it
            mask = (1 << depth) - 1
            for v in set(line[:-1]) or {line[-1]}:
                seen.add(v)
                if len({(v >> sh) & mask for sh in range(0, 8, depth)}) > 1:
                    return False
        elif ctype in (4, 6):
            for i in range(0, stride, bpp):
                px = bytes(line[i:i + bpp])
                seen.add(b"T" if px[-1] == 0 else px)
        elif ctype == 3 and palette:
            seen.update(palette[3 * v:3 * v + 3] for v in set(line))
        else:
            for i in range(0, stride, bpp):
                seen.add(bytes(line[i:i + bpp]))
        if len(seen) > 1:
            return False
        prev = line
    return True


def check_image(content_type: str, body: bytes) -> str:
    """The real image type of `body` when it's a usable logo, else LogoError. The content type must be an image
    type and the bytes must really be an image (the sniffed type wins: favicons are often served mislabelled)."""
    declared = (content_type or "").split(";")[0].strip().lower()
    declared = ALIASES.get(declared, declared)
    if declared not in MAGIC:
        raise LogoError(f"not an image type ({declared or 'none'})")
    if len(body) > MAX_BYTES:
        raise LogoError(f"over {MAX_BYTES // 1024} KB")
    kind = sniff(body)
    if kind is None:
        raise LogoError("not an image")
    try:
        w, h = image_size(kind, body)
    except (struct.error, IndexError):
        raise LogoError("not a readable image") from None
    if min(w, h) < MIN_PX:
        raise LogoError(f"too small ({w}x{h})")
    if max(w, h) > MAX_PX:
        raise LogoError(f"too large for a logo ({w}x{h})")
    png = best_png(kind, body)
    if png is not None and png_blank(png):
        raise LogoError("blank")
    return kind


def best_png(kind: str, body: bytes) -> bytes | None:
    """The PNG to keep: the image itself, or the largest PNG inside an .ico (None when there isn't one)."""
    if kind == "image/png":
        return body
    if kind == "image/x-icon":
        pngs = [e for e in _ico_entries(body) if e[2].startswith(MAGIC["image/png"][0])]
        if pngs:
            return max(pngs, key=lambda e: e[0] * e[1])[2]
    return None


def usable(content_type: str, body: bytes) -> bytes:
    """check_image, then the bytes to store (an .ico's PNG when it has one)."""
    kind = check_image(content_type, body)
    return best_png(kind, body) or body


def retry_due(fetched_at: str | None, days: int, today: date) -> bool:
    """Is a cached miss (last tried at `fetched_at`) due another try? By date, so a daily run always gets its retry
    and a run later the same day never does; no try yet is due."""
    return (fetched_at or "")[:10] <= (today - timedelta(days=days)).isoformat()


def utc_today() -> date:
    """Today in UTC: the calendar `fetched_at` (db.now) is written in."""
    return date.fromisoformat(now()[:10])


def fetch_image(fetch: Fetch, url: str) -> bytes:
    r = fetch(url)
    if r.status != 200:
        raise LogoError(f"HTTP {r.status}")
    return usable(r.content_type, r.body)


# --- bank sites ---------------------------------------------------------------------------------------

class _IconLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[set[str], str, str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k.lower(): (v or "").strip() for k, v in attrs}
        rels = set(a.get("rel", "").lower().split())
        if rels & {"icon", "apple-touch-icon", "apple-touch-icon-precomposed"} and a.get("href"):
            self.links.append((rels, a["href"], a.get("sizes", "").lower(), a.get("type", "").lower()))


def _side(sizes: str, default: int) -> int:
    found = [int(w) for w, h in re.findall(r"(\d+)x(\d+)", sizes)]
    return max(found) if found else default


def icon_candidates(page_url: str, text: str) -> list[str]:
    """Icon URLs to try, best first: apple-touch-icon(s), then <link rel=icon> by size, then /favicon.ico."""
    parser = _IconLinks()
    try:
        parser.feed(text)
    except Exception:  # a broken page just yields fewer links
        pass
    touch, icons = [], []
    for rels, href, sizes, typ in parser.links:
        if "svg" in typ or href.lower().split("?")[0].endswith(".svg"):
            continue
        if rels & {"apple-touch-icon", "apple-touch-icon-precomposed"}:
            touch.append((_side(sizes, 180), href))
        else:
            icons.append((_side(sizes, 16), href))
    ordered = [h for _, h in sorted(touch, key=lambda t: -t[0])] + [h for _, h in sorted(icons, key=lambda t: -t[0])]
    out = []
    for href in ordered + ["/favicon.ico"]:
        url = urljoin(page_url, href)
        if _https(url) and url not in out:
            out.append(url)
    return out


def site_icon(fetch: Fetch, site_url: str) -> bytes:
    """The bank's own icon, from its home page's <link> tags or /favicon.ico. LogoError when none is usable."""
    base = site_url.strip()
    if base.lower().startswith("http://"):
        base = "https://" + base[7:]
    if not _https(base):
        raise LogoError("no https site")
    text = ""
    try:
        page = fetch(base)
        if page.status == 200 and "html" in page.content_type.lower():
            text = page.body.decode("utf-8", "replace")
            base = page.url or base
    except FetchError:
        pass  # still try /favicon.ico
    last = LogoError("no icon")
    for url in icon_candidates(base, text)[:MAX_ICON_TRIES]:
        try:
            return fetch_image(fetch, url)
        except LogoError as e:
            last = e
    raise LogoError(f"no usable icon ({last})")


# --- institutions -------------------------------------------------------------------------------------

def refresh_institution(conn: sqlite3.Connection, client: PlaidClient, institution_id: str,
                        fetch: Fetch | None = None) -> dict[str, Any]:
    """Look one bank up (free; never creates an Item), pick its logo, and store the row. Plaid errors propagate."""
    fetch = fetch or http_get
    inst = client.institutions_get_by_id(institution_id).get("institution") or {}
    name = inst.get("name") if isinstance(inst.get("name"), str) else None
    color = inst.get("primary_color") if isinstance(inst.get("primary_color"), str) and \
        COLOR.fullmatch(inst["primary_color"]) else None
    url = inst.get("url") if isinstance(inst.get("url"), str) else None
    logo, source, note = None, "none", ""
    if inst.get("logo"):
        try:
            logo = usable("image/png", base64.b64decode(inst["logo"], validate=True))   # documented: a base64 PNG
            source = "plaid"
        except (binascii.Error, ValueError, LogoError) as e:
            note = f"Plaid's logo is unusable ({e})"
    if logo is None and url:
        try:
            logo, source = site_icon(fetch, url), "site"
        except LogoError as e:
            note = f"{note + '; ' if note else ''}site icon: {e}"
    with conn:
        conn.execute(
            """INSERT INTO institutions (institution_id, name, logo_png, logo_source, primary_color, url, fetched_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(institution_id) DO UPDATE SET name=excluded.name, logo_png=excluded.logo_png,
                   logo_source=excluded.logo_source, primary_color=excluded.primary_color, url=excluded.url,
                   fetched_at=excluded.fetched_at""",
            (institution_id, name, logo, source, color, url, now()))
    return {"institution_id": institution_id, "name": name, "source": source, "color": color, "note": note,
            "error": None}


def refresh_institutions(conn: sqlite3.Connection, client: PlaidClient, fetch: Fetch | None = None,
                         missing_only: bool = False) -> list[dict]:
    """Every connected bank, once. A failure for one bank is reported and leaves its old row alone.
    missing_only (the daily `finance sync`): only the banks with no logo stored (or no row: the lookup failed when it was
    connected) whose last try is INSTITUTION_RETRY_DAYS old; a failed lookup then still counts as that day's try."""
    rows = conn.execute(
        """SELECT i.institution_id, MIN(i.created_at) AS first, n.logo_png IS NOT NULL AS has_logo, n.fetched_at
           FROM items i LEFT JOIN institutions n USING (institution_id)
           WHERE i.removed_at IS NULL AND i.institution_id IS NOT NULL GROUP BY i.institution_id ORDER BY first""")
    today = utc_today()
    ids = [r["institution_id"] for r in rows
           if not missing_only or (not r["has_logo"] and retry_due(r["fetched_at"], INSTITUTION_RETRY_DAYS, today))]
    out = []
    for iid in ids:
        try:
            out.append(refresh_institution(conn, client, iid, fetch))
        except Exception as e:  # PlaidError, a DB hiccup: the next bank still gets its turn
            log.warning("institution lookup failed for %s: %s", iid, e)
            if missing_only:  # (the row has no logo: only its try date moves)
                with conn:
                    conn.execute("INSERT INTO institutions (institution_id, logo_source, fetched_at) VALUES (?, 'none', ?) "
                                 "ON CONFLICT(institution_id) DO UPDATE SET fetched_at=excluded.fetched_at", (iid, now()))
            out.append({"institution_id": iid, "name": None, "source": None, "color": None, "note": "",
                        "error": f"{type(e).__name__}: {e}"})
    return out


def institutions_payload(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """What the page needs to draw each bank: a logo exists (and its version for the URL) or a colour."""
    return [{"institution_id": r["institution_id"], "name": r["name"], "logo": r["has_logo"] == 1,
             "source": r["logo_source"], "v": re.sub(r"\D", "", r["fetched_at"] or "")[:14] or "0",
             "color": r["primary_color"] if isinstance(r["primary_color"], str) and COLOR.fullmatch(r["primary_color"])
             else None}
            for r in conn.execute("SELECT institution_id, name, logo_png IS NOT NULL AS has_logo, logo_source, "
                                  "primary_color, fetched_at FROM institutions ORDER BY institution_id")]


def institution_logo(conn: sqlite3.Connection, institution_id: str) -> tuple[str, bytes] | None:
    if not INSTITUTION_ID.fullmatch(institution_id or ""):
        return None
    r = conn.execute("SELECT logo_png FROM institutions WHERE institution_id = ? AND logo_png IS NOT NULL",
                     (institution_id,)).fetchone()
    kind = sniff(r[0]) if r else None
    return (kind, r[0]) if kind else None


# --- holdings -----------------------------------------------------------------------------------------

def logo_ticker(ticker: str | None, security_type: str | None) -> str | None:
    """The ticker to look up, or None: cash, crypto, unknown types and odd or empty tickers get letters."""
    if not ticker or (security_type or "").strip().lower() not in LOGO_TYPES:
        return None
    t = ticker.strip().upper()
    return t if TICKER.fullmatch(t) else None


def refresh_security_logos(conn: sqlite3.Connection, fetch: Fetch | None = None, today: date | None = None,
                           force: bool = False) -> dict[str, Any]:
    """Fetch the logo of each held stock/ETF/fund not cached yet (or whose miss is 30+ days old; all with force)."""
    fetch = fetch or http_get
    stamp = now() if today is None else f"{today.isoformat()}T00:00:00Z"
    today = today or date.today()
    cached = {r["ticker"]: r for r in conn.execute("SELECT ticker, png IS NOT NULL AS hit, fetched_at FROM security_logos")}
    tickers = sorted({t for t in (logo_ticker(r[0], r[1]) for r in
                                  conn.execute("SELECT ticker_symbol, security_type FROM holdings")) if t})
    out: dict[str, Any] = {"found": [], "missed": [], "cached": [], "error": None}
    for t in tickers:
        c = cached.get(t)
        if c is not None and not force and (c["hit"] or not retry_due(c["fetched_at"], MISS_RETRY_DAYS, today)):
            out["cached"].append(t)
            continue
        try:
            png, source = fetch_image(fetch, FMP_URL.format(t)), "fmp"
        except FetchError as e:  # the network, not the answer: nothing recorded, the next run tries again
            out["error"] = str(e)
            break
        except LogoError as e:
            png, source = None, "none"
            log.info("no logo for %s: %s", t, e)
        with conn:
            conn.execute("INSERT INTO security_logos (ticker, png, source, fetched_at) VALUES (?,?,?,?) "
                         "ON CONFLICT(ticker) DO UPDATE SET png=excluded.png, source=excluded.source, "
                         "fetched_at=excluded.fetched_at", (t, png, source, stamp))
        out["found" if png else "missed"].append(t)
    return out


def security_logo(conn: sqlite3.Connection, ticker: str) -> tuple[str, bytes] | None:
    if not TICKER.fullmatch(ticker or ""):
        return None
    r = conn.execute("SELECT png FROM security_logos WHERE ticker = ? AND png IS NOT NULL", (ticker,)).fetchone()
    kind = sniff(r[0]) if r else None
    return (kind, r[0]) if kind else None

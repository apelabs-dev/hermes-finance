"""The picture on a car / property page (TASK-13b): fetched automatically, once, in the background. Never uploaded.

  * A car: a Wikimedia Commons photo of that year / make / model (brand-agnostic: any make works). The Commons search
    API is asked "{year} {make} {model}", then (only if that found nothing usable) "{make} {model}": the file titles
    first (bitmaps), then imageinfo for the best few that name the car. A descriptive User-Agent and >= 1.5 s between
    calls (Commons answers 429 otherwise). A candidate's title must hold the make and
    ALL the model's words and none of the words that mean it isn't the outside of the car (interior, dashboard, engine,
    badge, wheel, rear, crash, concept, show, logo, seat); it must be a JPEG / PNG at least 1200 px wide under CC0,
    public domain, CC BY or CC BY-SA. Ranked: the exact year, then a front / side shot, then size. The background is
    removed with rembg (`birefnet-general`, the optional `photos` extra): only the largest shape is kept, thin scraps
    under it are trimmed, and the cut must cover >= 20% of the image with a car-like width / height (1.3 to 3.5), else
    the next candidate (at most 3), else the drawn silhouette. Without rembg (or if it fails) the photo is used as is,
    cover-cropped. The credit line (artist, licence, the file page) is shown under it.
  * A home: its Redfin listing photo (the aboveTheFold answer's fullScreenPhotoUrl; the MLS watermark stays; "Photo:
    listing via Redfin"), else an OpenStreetMap tile around its Redfin location with a pin the page draws ("©
    OpenStreetMap contributors"), else the drawn house outline. Never Redfin's Google Street View URL (it carries
    Redfin's own Google key). No background removal: the setting is the point.

Each picture is cached per car (year, make, model: shared by every account with that car) or per home in asset_images
and data/images/, and fetched again only when a try found nothing (after 30 days) or failed (the next day). Every
outbound request goes through the call guard (usage.py: Commons, OpenStreetMap and Redfin caps; counted before it's
sent). HTTP goes through valuation.transport, the cut through `cutter`, the pause through `sleep` and the background
run through `schedule` (tests replace all four).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from . import config, manual, usage, valuation
from .db import now

log = logging.getLogger(__name__)

# Wikimedia asks for a way to reach whoever runs the client: FINANCE_CONTACT (an email or URL) is put in when set.
USER_AGENT = "hermes-finance/1.0 (" + (os.environ.get("FINANCE_CONTACT", "").strip()[:120] + "; " if os.environ.get(
    "FINANCE_CONTACT", "").strip() else "") + "self-hosted personal net-worth dashboard)"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
COMMONS_IMAGE_HOSTS = ("https://upload.wikimedia.org/", "https://thumb.wikimedia.org/")
OSM_TILE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ZOOM = 17
API_PAUSE = 1.5              # seconds between two Commons calls (Commons answers 429 to a quicker client)
THUMB_WIDTH = 1600           # the Commons copy downloaded (a scaled thumbnail of the original)
MIN_WIDTH = 1200
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_CANDIDATES = 3            # photos tried for a clean cut-out
MAX_INFO = 8                  # titles asked about at once (imageinfo with metadata is heavy on a long list)
RETRY_NONE_DAYS = 30
RETRY_ERROR_DAYS = 1
REJECT_WORDS = ("interior", "dashboard", "engine", "badge", "wheel", "rear", "crash", "concept", "show", "logo", "seat")
ANGLE_WORDS = ("front", "side", "profile", "three-quarter", "3/4")
LICENSE_RE = re.compile(r"^(cc0|public domain|pd\b.*|cc[ -]by(?:-sa)?(?:[ -]\d(?:\.\d)?)?(?:[ -][a-z]{2,6})?)$", re.I)
FILE_RE = re.compile(r"[0-9a-f]{16}\.(?:png|jpg)")
MIN_COVERAGE = 0.20
ASPECT = (1.3, 3.5)

sleep: Callable[[float], None] = time.sleep


class CutUnavailable(Exception):
    """rembg (the optional `photos` extra) isn't installed."""


class CutFailed(Exception):
    """rembg ran and broke on this image."""


@dataclass
class Cut:
    ok: bool                 # the cut-out passed the sanity check
    png: bytes | None        # the cut-out (a transparent PNG, cropped to the car)
    coverage: float = 0.0    # share of the image the kept shape covers
    aspect: float = 0.0      # its width / height
    reason: str = ""


# --- keys, the page's view ---------------------------------------------------------------------------------------

def _words(text: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").casefold().replace("_", " "))


def model_words(model: Any) -> list[str]:
    """'Accord Hybrid (HEV)' -> ['accord', 'hybrid'] (NHTSA's bracketed notes aren't part of the name)."""
    return _words(re.sub(r"\([^)]*\)", " ", str(model or "")))


def key_for(row: sqlite3.Row | dict) -> str | None:
    if row["kind"] == "vehicle":
        if not (row["vehicle_make"] and row["vehicle_model"]):
            return None
        return "vehicle:" + "|".join((str(row["vehicle_year"] or ""), " ".join(_words(row["vehicle_make"])),
                                      " ".join(model_words(row["vehicle_model"]))))
    subj = valuation.subject(row)
    return f"property:{subj}" if subj else None


def file_name(key: str, ext: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:16] + f".{ext}"


def images_dir(db_path: Path | str | None = None) -> Path:
    """data/images/, next to the DB (a test's or a gate's DB keeps its own)."""
    return Path(db_path or config.db_path()).parent / "images"


def record(conn: sqlite3.Connection, row: sqlite3.Row | dict) -> sqlite3.Row | None:
    key = key_for(row)
    return conn.execute("SELECT * FROM asset_images WHERE key = ?", (key,)).fetchone() if key else None


def _age_days(ts: str) -> float:
    t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 86400


def due(conn: sqlite3.Connection, row: sqlite3.Row | dict) -> bool:
    """Should the picture be fetched (never tried, or a try that found nothing / failed long enough ago)?"""
    if not config.free_sources(conn=conn).get("photos", True) or key_for(row) is None or row["archived_at"]:
        return False
    r = record(conn, row)
    if r is None:
        return True
    if r["status"] == "ready":
        return False
    return _age_days(r["fetched_at"]) >= (RETRY_NONE_DAYS if r["status"] == "none" else RETRY_ERROR_DAYS)


def view(conn: sqlite3.Connection, row: sqlite3.Row | dict) -> dict[str, Any]:
    """The page's picture: {"status": ready | pending | none, "kind": cutout | photo | map | silhouette | outline,
    "url", "credit", "credit_url", "pin" ({"x", "y"} 0..1 on a map), "note"}."""
    r = record(conn, row)
    drawn = "silhouette" if row["kind"] == "vehicle" else "outline"
    if r is None or r["status"] != "ready" or not r["file"]:
        pending = due(conn, row)
        return {"status": "pending" if pending else "none", "kind": drawn, "url": None, "credit": None,
                "credit_url": None, "pin": None, "note": r["note"] if r else None}
    kind = "map" if r["source"] == "osm" else "cutout" if r["cutout"] else "photo"
    stamp = re.sub(r"\D", "", r["fetched_at"])
    return {"status": "ready", "kind": kind, "url": f"/api/images/{r['file']}?v={stamp}", "credit": r["credit"],
            "credit_url": r["credit_url"], "pin": {"x": r["pin_x"], "y": r["pin_y"]} if kind == "map" else None,
            "note": r["note"], "width": r["width"], "height": r["height"]}


def _save(conn: sqlite3.Connection, key: str, kind: str, status: str, **fields: Any) -> dict[str, Any]:
    cols = ("source", "file", "cutout", "width", "height", "credit", "credit_url", "artist", "license", "lat", "lng",
            "pin_x", "pin_y", "note")
    values = {c: fields.get(c) for c in cols}
    values["cutout"] = int(bool(values["cutout"]))
    with conn:
        conn.execute(f"INSERT OR REPLACE INTO asset_images (key, kind, status, {', '.join(cols)}, fetched_at) "
                     f"VALUES (?, ?, ?, {', '.join('?' * len(cols))}, ?)",
                     (key, kind, status, *(values[c] for c in cols), now()))
    return {"key": key, "status": status, **values}


# --- guarded requests --------------------------------------------------------------------------------------------

def _get(conn: sqlite3.Connection, provider: str, url: str, headers: dict[str, str], today: date, trigger: str,
         max_bytes: int, what: str) -> bytes:
    """One guarded GET (counted before it's sent, account-free: the monthly cap applies). Returns the body of a 200;
    raises valuation.ProviderError (counted) or usage.Refused (nothing sent)."""
    valuation.not_paused(provider, today)
    res = usage.reserve(conn, provider, today, None, trigger)
    name = usage.LABELS[provider]
    try:
        status, body = valuation.transport(url, headers, valuation.TIMEOUT, max_bytes=max_bytes)
    except TimeoutError:
        valuation._settle(conn, res, today, "timeout", None, f"{name} timed out ({what})", False)
        raise valuation.ProviderError(provider, "timeout", f"{name} timed out; not retried") from None
    except (OSError, ValueError) as e:
        note = f"{name} unreachable ({type(e).__name__}, {what})"
        valuation._settle(conn, res, today, "error", None, note, False)
        raise valuation.ProviderError(provider, "network", f"{note}; not retried") from None
    if status != 200:
        message, stop = valuation._refusal(provider, status, valuation._body_message(body), valuation._body_text(body))
        until = valuation._settle(conn, res, today, "error", status, f"{message} ({what})", stop)
        raise valuation.ProviderError(provider, "http", message + (f". Paused until {until}" if until else ""), status,
                                      until)
    valuation._settle(conn, res, today, "ok", status, what, False)
    return body


def _image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return None


def _image(conn: sqlite3.Connection, provider: str, url: str, headers: dict[str, str], today: date,
           trigger: str) -> tuple[bytes, str]:
    data = _get(conn, provider, url, headers, today, trigger, MAX_IMAGE_BYTES, "image")
    ext = _image_type(data)
    if not ext or len(data) >= MAX_IMAGE_BYTES:
        raise valuation.ProviderError(provider, "bad_response", f"{usage.LABELS[provider]} sent something that isn't a "
                                      "whole JPEG / PNG image")
    return data, ext


# --- a car: Wikimedia Commons ------------------------------------------------------------------------------------

def _text(html: Any) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", str(html or "")).split())[:120]


def search_url(query: str) -> str:
    """Commons full-text search of the File namespace, bitmaps only: titles (the proven request, TASK-13b notes)."""
    return COMMONS_API + "?" + urlencode({"action": "query", "format": "json", "formatversion": "2", "list": "search",
                                          "srsearch": f"{query} filetype:bitmap", "srnamespace": "6", "srlimit": "30"})


def info_url(titles: list[str]) -> str:
    """imageinfo (url, size, mime, the licence / artist metadata and a 1600 px thumbnail) for a few chosen titles."""
    return COMMONS_API + "?" + urlencode({
        "action": "query", "format": "json", "formatversion": "2", "titles": "|".join(titles), "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata", "iiextmetadatafilter": "Artist|LicenseShortName|License",
        "iiurlwidth": str(THUMB_WIDTH)})


def title_ok(title: str, make: list[str], model: list[str]) -> bool:
    """The make and ALL the model's words in the title, and none of the words that mean it isn't the car's outside."""
    words = _words(re.sub(r"^File:", "", title, flags=re.I).rsplit(".", 1)[0])
    return bool(make and model and all(w in words for w in make + model)
                and not any(bad in " ".join(words) for bad in REJECT_WORDS))


def candidate(page: dict[str, Any], year: int | None, make: list[str], model: list[str]) -> dict[str, Any] | None:
    """A Commons file (with its imageinfo) as a usable candidate, or None: title_ok(), a JPEG / PNG at least MIN_WIDTH
    wide, a free licence (CC0, public domain, CC BY, CC BY-SA)."""
    title = str(page.get("title") or "")
    name = re.sub(r"^File:", "", title, flags=re.I).rsplit(".", 1)[0]
    if not title_ok(title, make, model):
        return None
    info = (page.get("imageinfo") or [None])[0]
    if not isinstance(info, dict):
        return None
    if info.get("mime") not in ("image/jpeg", "image/png") or not isinstance(info.get("width"), int) \
            or info["width"] < MIN_WIDTH:
        return None
    meta = info.get("extmetadata") or {}
    lic = _text((meta.get("LicenseShortName") or {}).get("value"))
    if not LICENSE_RE.match(lic) or re.search(r"\b(nc|nd)\b", lic, re.I):
        return None
    years = {int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", name)}
    year_rank = 0 if year in years else 1 if year and ({year - 1, year + 1} & years) else 2
    angle_rank = 0 if any(w in name.casefold() for w in ANGLE_WORDS) else 1
    thumb = info.get("thumburl") or info.get("url")
    # Commons serves thumbnails from thumb.wikimedia.org (seen live 2026-09-27) and originals from upload.wikimedia.org.
    if not isinstance(thumb, str) or not thumb.startswith(COMMONS_IMAGE_HOSTS):
        return None
    return {"title": title, "thumb": thumb, "width": info["width"], "height": info.get("height"),
            "page_url": info.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/{title.replace(' ', '_')}",
            "artist": _text((meta.get("Artist") or {}).get("value")) or "unknown", "license": lic,
            "rank": (year_rank, angle_rank, -info["width"])}


def _title_rank(title: str, year: int | None) -> tuple[int, int]:
    name = re.sub(r"^File:", "", title, flags=re.I).rsplit(".", 1)[0]
    years = {int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", name)}
    return (0 if year in years else 1 if year and ({year - 1, year + 1} & years) else 2,
            0 if any(w in name.casefold() for w in ANGLE_WORDS) else 1)


def _json(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def commons_candidates(conn: sqlite3.Connection, row: sqlite3.Row | dict, today: date, trigger: str) -> list[dict[str, Any]]:
    """The usable Commons photos of this car, best first. For "{year} {make} {model}" (then, only if that found nothing
    usable, "{make} {model}"): search the titles, keep the ones naming the car, then ask imageinfo for the best
    MAX_INFO of them (licence, size, thumbnail). >= API_PAUSE seconds between calls."""
    year = row["vehicle_year"]
    make, model = _words(row["vehicle_make"]), model_words(row["vehicle_model"])
    model_name = " ".join(re.sub(r"\([^)]*\)", " ", str(row["vehicle_model"] or "")).split())   # "Accord Hybrid (HEV)" -> "Accord Hybrid"
    named = " ".join(x for x in (" ".join(str(row["vehicle_make"] or "").split()), model_name) if x)
    queries = ([f"{year} {named}"] if year else []) + [named]
    calls = 0

    def api(url: str, what: str) -> dict[str, Any]:
        nonlocal calls
        if calls:
            sleep(API_PAUSE)
        calls += 1
        return _json(_get(conn, "commons", url, {"User-Agent": USER_AGENT}, today, trigger, valuation.MAX_BYTES * 4, what))

    for q in queries:
        hits = api(search_url(q), f"search {q!r}").get("query", {}).get("search") or []
        titles = list(dict.fromkeys(str(h.get("title") or "") for h in hits if isinstance(h, dict)))
        named_titles = sorted((t for t in titles if title_ok(t, make, model)), key=lambda t: _title_rank(t, year))
        if not named_titles:
            continue
        pages = api(info_url(named_titles[:MAX_INFO]), f"imageinfo of {min(len(named_titles), MAX_INFO)}").get(
            "query", {}).get("pages") or []
        found = [c for c in (candidate(p, year, make, model) for p in pages if isinstance(p, dict)) if c]
        if found:
            return sorted(found, key=lambda c: c["rank"])
    return []


def cut(data: bytes) -> Cut:
    """rembg (MODEL), then clean(). CutUnavailable without the `photos` extra; CutFailed if it breaks."""
    try:
        import rembg  # noqa: F401  (the optional `photos` extra)
    except ImportError:
        raise CutUnavailable("rembg isn't installed (uv sync --extra photos)") from None
    try:
        out = rembg.remove(data, session=_session())
    except Exception as e:  # noqa: BLE001  (a model / image problem: the uncut photo is used)
        raise CutFailed(f"rembg failed ({type(e).__name__})") from None
    return clean(out)


# birefnet-general (~973 MB, downloaded once into ~/.rembg/models, ~10 s a car on the M4, once per car): isnet-general-use left a pavement chunk
# under the bumper, dropped a mirror and left ~7% of the car see-through on a hatchback photo (2026-09-27 A/B).
MODEL = "birefnet-general"

_SESSION: dict[str, Any] = {}
_SESSION_LOCK = threading.Lock()


def _session() -> Any:
    with _SESSION_LOCK:
        if "s" not in _SESSION:
            from rembg import new_session
            _SESSION["s"] = new_session(MODEL)
        return _SESSION["s"]


def clean(png: bytes) -> Cut:
    """Post-process a cut-out: keep only the largest shape, trim thin scraps along its bottom (a bit of pavement under
    the bumper), check it (covers >= MIN_COVERAGE of the image, width / height in ASPECT), crop it with a margin."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    img = Image.open(io.BytesIO(png)).convert("RGBA")
    a = np.array(img)
    alpha = a[..., 3]
    mask = alpha > 40

    def largest(m: Any) -> Any:
        lab, n = ndimage.label(m)
        if not n:
            return None
        sizes = np.bincount(lab.ravel())
        sizes[0] = 0
        return lab == int(sizes.argmax())

    mask = largest(mask)
    if mask is None:
        return Cut(False, None, reason="nothing left after the cut")
    ys, xs = np.nonzero(mask)
    y0, y1 = int(ys.min()), int(ys.max())
    k = max(3, round((y1 - y0 + 1) * 0.03))
    top = y0 + int((y1 - y0) * 0.75)                      # the bottom quarter: tyres survive, thin strips don't
    band = mask[top:y1 + 1]
    mask[top:y1 + 1] = ndimage.binary_opening(band, structure=np.ones((k, k), dtype=bool)) & band
    mask = largest(mask)
    if mask is None:
        return Cut(False, None, reason="nothing left after trimming")
    ys, xs = np.nonzero(mask)
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())
    h, w = mask.shape
    coverage = float(mask.sum()) / float(h * w)
    aspect = (x1 - x0 + 1) / max(1, (y1 - y0 + 1))
    if coverage < MIN_COVERAGE or not ASPECT[0] <= aspect <= ASPECT[1]:
        return Cut(False, None, coverage, aspect, f"the cut-out doesn't look like a whole car (covers {coverage:.0%}, "
                                                  f"width/height {aspect:.2f})")
    a[..., 3] = np.where(mask, alpha, 0).astype(a.dtype)
    pad = max(4, round(max(x1 - x0, y1 - y0) * 0.03))
    box = (max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad + 1), min(h, y1 + pad + 1))
    out = Image.fromarray(a).crop(box)
    if out.width > 1400:
        out = out.resize((1400, round(out.height * 1400 / out.width)), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return Cut(True, buf.getvalue(), coverage, aspect)


cutter: Callable[[bytes], Cut] = cut


def _credit(c: dict[str, Any]) -> str:
    return f"Photo: {c['artist']}, {c['license']} · Wikimedia Commons"


def fetch_vehicle(conn: sqlite3.Connection, row: sqlite3.Row | dict, today: date, folder: Path,
                  trigger: str = "refresh") -> dict[str, Any]:
    key = key_for(row)
    what = " ".join(str(x) for x in (row["vehicle_year"], row["vehicle_make"], row["vehicle_model"]) if x)
    cands = commons_candidates(conn, row, today, trigger)
    if not cands:
        return _save(conn, key, "vehicle", "none", note=f"Wikimedia Commons has no usable photo of a {what}")
    tried = []
    for i, c in enumerate(cands[:MAX_CANDIDATES]):
        if i:
            sleep(API_PAUSE)
        try:
            data, ext = _image(conn, "commons", c["thumb"], {"User-Agent": USER_AGENT}, today, trigger)
        except valuation.ProviderError as e:
            tried.append(f"{c['title']}: {e.message}")
            continue
        base = {"source": "commons", "credit": _credit(c), "credit_url": c["page_url"], "artist": c["artist"],
                "license": c["license"]}
        try:
            result = cutter(data)
        except (CutUnavailable, CutFailed) as e:   # the photo as it is, cover-cropped by the page
            name = file_name(key, ext)
            _write(folder, name, data)
            return _save(conn, key, "vehicle", "ready", file=name, cutout=False, width=c["width"], height=c["height"],
                         note=f"{c['title']} (not cut out: {e})", **base)
        if result.ok and result.png:
            name = file_name(key, "png")
            _write(folder, name, result.png)
            return _save(conn, key, "vehicle", "ready", file=name, cutout=True, width=c["width"], height=c["height"],
                         note=f"{c['title']} (cut out: covers {result.coverage:.0%}, width/height {result.aspect:.2f})",
                         **base)
        tried.append(f"{c['title']}: {result.reason}")
    return _save(conn, key, "vehicle", "none", note="; ".join(tried)[:600] or "no candidate could be downloaded")


def _write(folder: Path, name: str, data: bytes) -> None:
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = folder / f".{name}.tmp"
    tmp.write_bytes(data)
    tmp.replace(folder / name)


# --- a home: the Redfin listing photo, else an OpenStreetMap tile ------------------------------------------------

def tile_for(lat: float, lng: float, zoom: int = OSM_ZOOM) -> tuple[int, int, float, float]:
    """(x, y, pin_x, pin_y): the OSM tile holding lat / lng, and where on it the point is (0..1)."""
    n = 2 ** zoom
    fx = (lng + 180.0) / 360.0 * n
    r = math.radians(lat)
    fy = (1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * n
    x, y = int(fx), int(fy)
    return x, y, round(fx - x, 4), round(fy - y, 4)


def map_tile(conn: sqlite3.Connection, key: str, lat: float, lng: float, today: date, folder: Path, trigger: str,
             note: str = "") -> dict[str, Any]:
    """One OpenStreetMap tile around (lat, lng), cached; the page draws the pin at (pin_x, pin_y)."""
    x, y, px, py = tile_for(lat, lng)
    data, ext = _image(conn, "osm", OSM_TILE.format(z=OSM_ZOOM, x=x, y=y), {"User-Agent": USER_AGENT}, today, trigger)
    name = file_name(key, ext)
    _write(folder, name, data)
    return _save(conn, key, "property", "ready", source="osm", file=name, width=256, height=256,
                 credit="© OpenStreetMap contributors", credit_url="https://www.openstreetmap.org/copyright",
                 lat=lat, lng=lng, pin_x=px, pin_y=py, note=note or f"tile {OSM_ZOOM}/{x}/{y}")


def fetch_property(conn: sqlite3.Connection, row: sqlite3.Row | dict, today: date, folder: Path,
                   trigger: str = "refresh") -> dict[str, Any]:
    key = key_for(row)
    est = valuation.latest(conn, row, "redfin")
    d = json.loads(est["details_json"] or "{}") if est else {}
    photo, lat, lng = d.get("photo_url"), d.get("lat"), d.get("lng")
    if not est or "photo_url" not in d:    # valued before the photo was noted, or no estimate: ask Redfin once for it
        place = valuation.redfin_place(conn, row, today, trigger)
        photo = photo or place["photo_url"]
        lat, lng = (lat, lng) if lat is not None else (place["lat"], place["lng"])
    if photo:
        try:
            data, ext = _image(conn, "redfin", photo, dict(valuation.BROWSER_HEADERS, Accept="image/*"), today, trigger)
        except valuation.ProviderError as e:
            data, ext = None, e.message
        if data:
            name = file_name(key, ext)
            _write(folder, name, data)
            return _save(conn, key, "property", "ready", source="redfin", file=name, credit="Photo: listing via Redfin",
                         credit_url=None, lat=lat, lng=lng, note=photo[:200])
    if lat is not None and lng is not None:
        return map_tile(conn, key, float(lat), float(lng), today, folder, trigger)
    return _save(conn, key, "property", "none", note="no listing photo and no location on Redfin")


# --- running it ----------------------------------------------------------------------------------------------------

def ensure(conn: sqlite3.Connection, account_id: int, today: date, folder: Path, force: bool = False,
           trigger: str = "refresh") -> dict[str, Any] | None:
    """Fetch this account's picture if it's due (or `force`). A refused / failed lookup is recorded ('error', tried
    again the next day) and never raised. Returns the saved record, or None when nothing was due."""
    row = manual.get(conn, account_id, include_archived=True)
    if row["kind"] not in manual.VALUE_KINDS or key_for(row) is None or not config.free_sources(conn=conn).get("photos", True):
        return None
    if not force and not due(conn, row):
        return None
    try:
        if row["kind"] == "vehicle":
            return fetch_vehicle(conn, row, today, folder, trigger)
        return fetch_property(conn, row, today, folder, trigger)
    except usage.Refused as e:
        return _save(conn, key_for(row), row["kind"], "error", note=str(e)[:300])
    except valuation.ProviderError as e:
        log.warning("picture for manual account #%s: %s", account_id, e.message)
        return _save(conn, key_for(row), row["kind"], "error", note=e.message[:300])


_running: set[str] = set()
_running_lock = threading.Lock()


def run_in_background(db_path: Path | str, account_id: int) -> threading.Thread | None:
    """ensure() in a daemon thread with its own connection, at most one per picture at a time. The page never waits:
    it shows the silhouette / outline until the picture is ready."""
    from . import db
    tag = f"{db_path}#{account_id}"
    with _running_lock:
        if tag in _running:
            return None
        _running.add(tag)

    def work() -> None:
        try:
            conn = db.connect(Path(db_path))
            try:
                ensure(conn, account_id, date.today(), images_dir(db_path))
            finally:
                conn.close()
        except Exception:  # noqa: BLE001  (a background job never takes the server down)
            log.exception("picture for manual account #%s failed", account_id)
        finally:
            with _running_lock:
                _running.discard(tag)

    t = threading.Thread(target=work, name=f"photo-{account_id}", daemon=True)
    t.start()
    return t


schedule: Callable[[Path | str, int], Any] = run_in_background


def file_bytes(db_path: Path | str, name: str) -> tuple[str, bytes] | None:
    """A cached picture for /api/images/<name>: (content type, bytes), or None (unknown name, or not there)."""
    if not FILE_RE.fullmatch(name):
        return None
    path = images_dir(db_path) / name
    if not path.is_file():
        return None
    return ("image/png" if name.endswith(".png") else "image/jpeg"), path.read_bytes()


def next_retry(r: sqlite3.Row) -> str | None:
    if r["status"] == "ready":
        return None
    days = RETRY_NONE_DAYS if r["status"] == "none" else RETRY_ERROR_DAYS
    t = datetime.strptime(r["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(days=days)
    return t.astimezone().date().isoformat()

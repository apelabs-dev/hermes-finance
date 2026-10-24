"""TASK-13b: the automatic pictures (a car's Wikimedia Commons photo, cut out; a home's Redfin listing photo, else an
OpenStreetMap tile with a pin, else a drawn house), the images route and the background fetch, the explicit loan link
(equity) and the v8 -> v9 upgrade.

Mocked only: Commons, Redfin (and its photo CDN) and OSM answer through conftest.FakeHttp; rembg is a fake `cutter`
(its real post-processing is tested on synthetic images when numpy / Pillow / scipy are installed); the background run
records instead of starting a thread (conftest.SCHEDULED)."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import ALDER, SCHEDULED, VIN, BIRCH_BUILDING, Redfin
from hermes_finance import config, db, manual, photos, usage, valuation
from hermes_finance.web import App

TODAY = date(2026, 11, 2)
LOCAL = {"Host": "127.0.0.1:8765"}
POST = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes" * 100
PNG_CUT = b"\x89PNG\r\n\x1a\n" + b"cut-out" * 100


def car(conn, year=2020, make="Zenith", model="Comet Prime (PHEV)", **kw):
    return manual.create(conn, {"kind": "vehicle", "vehicle_year": year, "vehicle_make": make, "vehicle_model": model,
                                "balance": 13520, **kw}, TODAY)


def page(title, width=3108, mime="image/jpeg", lic="CC BY-SA 4.0", artist='<a href="//commons.wikimedia.org/wiki/User:SsmIntrigue">SsmIntrigue</a>'):
    name = title.replace(" ", "_")
    return {"title": f"File:{title}", "imageinfo": [{
        "url": f"https://upload.wikimedia.org/wikipedia/commons/a/ab/{name}", "width": width, "height": round(width * 0.6),
        "mime": mime, "thumburl": f"https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/{name}/1600px-{name}",
        "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{name}",
        "extmetadata": {"Artist": {"value": artist}, "LicenseShortName": {"value": lic}}}]}


class Commons:
    """The Commons API, the two requests the picker makes (the shape a reviewer proved live): list=search (titles in the
    File namespace, bitmaps) by query, then prop=imageinfo for chosen titles; and the image files. Records queries,
    the titles asked about and the headers."""

    def __init__(self, results, no_info=()):
        self.results = results
        self.no_info = set(no_info)        # files whose imageinfo doesn't come back (Commons' heavy-metadata limit)
        self.queries: list[str] = []
        self.info: list[list[str]] = []
        self.headers: list[dict] = []
        self.files: list[str] = []

    def api(self, url, headers):
        q = parse_qs(urlparse(url).query)
        self.headers.append(headers)
        if q.get("list") == ["search"]:
            assert q["srnamespace"] == ["6"] and q["srsearch"][0].endswith(" filetype:bitmap") and q["srlimit"] == ["30"]
            query = q["srsearch"][0][:-len(" filetype:bitmap")]
            self.queries.append(query)
            return 200, {"batchcomplete": True, "query": {"search": [{"ns": 6, "title": p["title"]}
                                                                    for p in self.results.get(query, [])]}}
        assert q["prop"] == ["imageinfo"] and q["iiurlwidth"] == ["1600"] and "extmetadata" in q["iiprop"][0]
        titles = q["titles"][0].split("|")
        self.info.append(titles)
        by_title = {p["title"]: p for ps in self.results.values() for p in ps}
        pages = [{"title": t} if t in self.no_info else by_title[t] for t in titles]
        return 200, {"batchcomplete": True, "query": {"pages": pages}}

    def file(self, url, headers):
        self.files.append(url)
        self.headers.append(headers)
        return 200, JPEG


@pytest.fixture
def photos_on(free_sources, fake_http, monkeypatch):
    slept = []
    monkeypatch.setattr(photos, "sleep", lambda s: slept.append(s))
    fake_http.slept = slept
    return fake_http


def commons(fake_http, results, no_info=()):
    c = Commons(results, no_info)
    fake_http.on("commons", c.api)
    fake_http.on("commons_file", c.file)
    return c


def cut_ok(data):
    assert data == JPEG
    return photos.Cut(True, PNG_CUT, 0.41, 2.2)


COMET_RESULTS = {"2020 Zenith Comet Prime": [
    page("2020 Zenith Comet Prime interior dashboard.jpg"),                              # not the outside of the car
    page("2019 Zenith Comet Prime side.jpg", width=4000),                                 # a year off
    page("2020 Zenith Comet Prime, Front Right, 10-24-2020.jpg"),                         # the one (exact year, front)
    page("2020 Zenith Comet at a show.jpg"),                                              # "Prime" missing, and a show
    page("2020 Zenith Comet Prime NC.jpg", lic="CC BY-NC-SA 4.0"),                       # not a free licence
    page("2020 Zenith Comet Prime small.jpg", width=800),                                 # too small
    page("2020 Zenith Comet Prime logo.svg", mime="image/svg+xml"),                       # not a photo
]}


# --- a car: Wikimedia Commons -------------------------------------------------------------------------------------

def test_a_zenith_gets_its_exact_year_front_photo_cut_out(conn, photos_on, monkeypatch, tmp_path):
    c = commons(photos_on, COMET_RESULTS)
    monkeypatch.setattr(photos, "cutter", cut_ok)
    m = car(conn)
    folder = tmp_path / "images"
    rec = photos.ensure(conn, m["id"], TODAY, folder)
    assert rec["status"] == "ready" and rec["source"] == "commons" and rec["cutout"] == 1
    assert c.queries == ["2020 Zenith Comet Prime"]                                   # found: no second query
    assert c.info == [["File:2020 Zenith Comet Prime, Front Right, 10-24-2020.jpg",   # the named titles, by year, angle
                       "File:2020 Zenith Comet Prime NC.jpg", "File:2020 Zenith Comet Prime small.jpg",
                       "File:2019 Zenith Comet Prime side.jpg"]]
    assert photos_on.slept == [photos.API_PAUSE]                                     # between the two API calls
    assert c.files == ["https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/2020_Zenith_Comet_Prime,_Front_Right,_10-24-2020.jpg/1600px-2020_Zenith_Comet_Prime,_Front_Right,_10-24-2020.jpg"]
    assert all(h["User-Agent"] == photos.USER_AGENT == "hermes-finance/1.0 (self-hosted personal net-worth dashboard)"
               for h in c.headers)                                           # TASK-18: no personal URL in code
    assert (folder / rec["file"]).read_bytes() == PNG_CUT and rec["file"].endswith(".png")
    assert rec["credit"] == "Photo: SsmIntrigue, CC BY-SA 4.0 · Wikimedia Commons"
    assert rec["credit_url"] == "https://commons.wikimedia.org/wiki/File:2020_Zenith_Comet_Prime,_Front_Right,_10-24-2020.jpg"
    v = photos.view(conn, manual.get(conn, m["id"]))
    assert v["status"] == "ready" and v["kind"] == "cutout" and v["url"].startswith(f"/api/images/{rec['file']}?v=")
    assert conn.execute("SELECT calls FROM api_usage WHERE provider = 'commons'").fetchone()[0] == 3   # search, info, file
    assert photos.ensure(conn, m["id"], TODAY, folder) is None                        # fetched once


def test_the_candidates_are_filtered_and_ranked():
    ranked = [photos.candidate(p, 2020, ["zenith"], ["comet", "prime"]) for p in COMET_RESULTS["2020 Zenith Comet Prime"]]
    kept = [c["title"] for c in ranked if c]
    assert kept == ["File:2019 Zenith Comet Prime side.jpg", "File:2020 Zenith Comet Prime, Front Right, 10-24-2020.jpg"]
    best = sorted((c for c in ranked if c), key=lambda c: c["rank"])[0]
    assert best["title"].startswith("File:2020 Zenith Comet Prime, Front Right") and best["rank"][:2] == (0, 0)
    assert photos.model_words("Comet Prime (PHEV)") == ["comet", "prime"]
    for lic, ok in (("CC0", True), ("Public domain", True), ("CC BY 2.0", True), ("CC BY-SA 3.0", True),
                    ("CC BY-SA 4.0 int", True), ("GFDL", False), ("CC BY-NC 2.0", False), ("CC BY-ND 4.0", False),
                    ("All rights reserved", False)):
        assert bool(photos.candidate(page("2020 Zenith Comet Prime front.jpg", lic=lic), 2020, ["zenith"],
                                     ["comet", "prime"])) is ok, lic


def test_a_file_whose_imageinfo_doesnt_come_back_is_skipped_not_fatal(conn, photos_on, monkeypatch, tmp_path):
    """The first real lookup (2026-09-26) asked metadata for 30 files in one request and found nothing usable: pages can
    come back without imageinfo. The picker now asks about a few titles it already chose, and one without imageinfo
    is just skipped."""
    c = commons(photos_on, COMET_RESULTS, no_info={"File:2020 Zenith Comet Prime, Front Right, 10-24-2020.jpg"})
    monkeypatch.setattr(photos, "cutter", cut_ok)
    rec = photos.ensure(conn, car(conn)["id"], TODAY, tmp_path)
    assert rec["status"] == "ready" and rec["note"].startswith("File:2019 Zenith Comet Prime side.jpg")


def test_only_a_few_named_titles_are_asked_about(conn, photos_on, monkeypatch, tmp_path):
    many = [page(f"2020 Zenith Comet Prime front {i}.jpg") for i in range(20)] + [page(f"Zenith Corolla {i}.jpg") for i in range(5)]
    c = commons(photos_on, {"2020 Zenith Comet Prime": many})
    monkeypatch.setattr(photos, "cutter", cut_ok)
    photos.ensure(conn, car(conn)["id"], TODAY, tmp_path)
    assert len(c.info) == 1 and len(c.info[0]) == photos.MAX_INFO == 8 and not any("Corolla" in t for t in c.info[0])


def test_a_honda_is_ranked_by_year_then_angle_then_size(conn, photos_on, monkeypatch, tmp_path):
    c = commons(photos_on, {"2019 Honda Civic": [
        page("Honda Civic Type R 2019 rear.jpg", width=5000),                              # rear: never
        page("2018 Honda Civic sedan.jpg", width=5200),                                    # ±1 year, no angle
        page("2020 Honda Civic front left.jpg", width=3000),                               # ±1 year, front
        page("2019 Honda CR-V front.jpg", width=6000),                                     # another model
        page("Honda Civic.jpg", width=6000),                                               # no year
    ]})
    monkeypatch.setattr(photos, "cutter", cut_ok)
    m = car(conn, 2019, "Honda", "Civic")
    rec = photos.ensure(conn, m["id"], TODAY, tmp_path)
    assert rec["status"] == "ready" and "2020_Honda_Civic_front_left" in c.files[0]
    assert rec["note"].startswith("File:2020 Honda Civic front left.jpg")


def test_a_tesla_model_3_needs_every_model_word(conn, photos_on, monkeypatch, tmp_path):
    c = commons(photos_on, {"2021 Tesla Model 3": [
        page("2021 Tesla Model S front.jpg", width=5000),                                  # "3" missing
        page("2023 Tesla Model Y 3 motors.jpg", width=5000),                               # has "3", but "Model Y"... and a year off
        page("2021 Tesla Model 3 Long Range front.jpg", width=4000),
    ]})
    monkeypatch.setattr(photos, "cutter", cut_ok)
    m = car(conn, 2021, "Tesla", "Model 3")
    photos.ensure(conn, m["id"], TODAY, tmp_path)
    assert "2021_Tesla_Model_3_Long_Range_front" in c.files[0]
    assert photos.candidate(page("2021 Tesla Model S front.jpg"), 2021, ["tesla"], ["model", "3"]) is None


def test_no_result_asks_the_second_query_then_draws_the_silhouette(conn, photos_on, tmp_path):
    c = commons(photos_on, {"2020 Zenith Comet Prime": [], "Zenith Comet Prime": [page("Zenith Comet Prime interior.jpg")]})
    m = car(conn)
    rec = photos.ensure(conn, m["id"], TODAY, tmp_path)
    assert c.queries == ["2020 Zenith Comet Prime", "Zenith Comet Prime"] and photos_on.slept == [photos.API_PAUSE]
    assert rec["status"] == "none" and rec["note"] == ("Wikimedia Commons has no usable photo of a 2020 Zenith Comet "
                                                       "Prime (PHEV)")
    v = photos.view(conn, manual.get(conn, m["id"]))
    assert v == {**v, "status": "none", "kind": "silhouette", "url": None}
    assert photos.ensure(conn, m["id"], TODAY, tmp_path) is None and len(c.queries) == 2   # tried again in 30 days
    with conn:
        conn.execute("UPDATE asset_images SET fetched_at = '2026-01-01T00:00:00Z'")
    assert photos.due(conn, manual.get(conn, m["id"])) is True


def test_a_bad_cutout_tries_the_next_candidate_then_draws_the_silhouette(conn, photos_on, monkeypatch, tmp_path):
    c = commons(photos_on, {"2020 Zenith Comet Prime": [page(f"2020 Zenith Comet Prime front {i}.jpg") for i in range(5)]})
    seen = []

    def bad(data):
        seen.append(1)
        return photos.Cut(False, None, 0.12, 4.1, "the cut-out doesn't look like a whole car (covers 12%, width/height 4.10)")

    monkeypatch.setattr(photos, "cutter", bad)
    m = car(conn)
    rec = photos.ensure(conn, m["id"], TODAY, tmp_path / "images")
    assert len(seen) == 3 and len(c.files) == 3 and photos_on.slept == [photos.API_PAUSE] * 3   # at most 3, paced
    assert rec["status"] == "none" and rec["note"].count("doesn't look like a whole car") == 3
    assert not (tmp_path / "images").exists()                                   # nothing written


@pytest.mark.parametrize("problem", [photos.CutUnavailable("rembg isn't installed"), photos.CutFailed("rembg failed (X)")])
def test_without_a_working_cut_the_photo_is_used_as_it_is(conn, photos_on, monkeypatch, tmp_path, problem):
    commons(photos_on, COMET_RESULTS)

    def cutter(data):
        raise problem

    monkeypatch.setattr(photos, "cutter", cutter)
    m = car(conn)
    rec = photos.ensure(conn, m["id"], TODAY, tmp_path)
    assert rec["status"] == "ready" and rec["cutout"] == 0 and rec["file"].endswith(".jpg")
    assert (tmp_path / rec["file"]).read_bytes() == JPEG and f"not cut out: {problem}" in rec["note"]
    assert photos.view(conn, manual.get(conn, m["id"]))["kind"] == "photo"      # cover-cropped by the page


def test_the_real_cut_says_when_rembg_isnt_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "rembg", None)                              # `import rembg` fails
    with pytest.raises(photos.CutUnavailable, match="uv sync --extra photos"):
        photos.cut(JPEG)


def test_the_cleanup_keeps_the_car_and_trims_a_scrap_under_it():
    """clean() on synthetic cut-outs (needs the photos extra's numpy / Pillow / scipy)."""
    np = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    pytest.importorskip("scipy.ndimage")

    def png(arr):
        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, "PNG")
        return buf.getvalue()

    a = np.zeros((300, 600, 4), dtype=np.uint8)
    a[100:230, 60:540] = (200, 200, 200, 255)                                   # the body
    for cx in (150, 450):                                                       # the tyres
        yy, xx = np.ogrid[:300, :600]
        a[(yy - 230) ** 2 + (xx - cx) ** 2 < 40 ** 2] = (20, 20, 20, 255)
    a[270:273, 40:560] = (90, 90, 90, 255)                                      # a thin strip of pavement...
    a[240:273, 298:302] = (90, 90, 90, 255)                                     # ...joined to the car by a sliver
    a[20:40, 20:40] = (255, 255, 255, 255)                                      # a stray blob (not the largest)
    out = photos.clean(png(a))
    # the body is 480 px wide (60..540), the strip 520 (40..560): 480 / 173 = 2.77 means the strip was trimmed
    assert out.ok and 0.3 < out.coverage < 0.45 and 2.7 < out.aspect < 2.85, out
    got = np.array(Image.open(io.BytesIO(out.png)))
    xs = np.nonzero(got[..., 3] > 40)[1]
    assert xs.max() - xs.min() + 1 == 480                                       # the car's width: no strip, no blob
    assert got.shape[1] < 600 and got.shape[0] < 300                            # cropped to the car (with a margin)
    small = np.zeros((300, 600, 4), dtype=np.uint8)
    small[140:170, 250:330] = (200, 200, 200, 255)
    assert photos.clean(png(small)).ok is False                                 # covers far under 20%
    tall = np.zeros((300, 600, 4), dtype=np.uint8)
    tall[10:290, 200:400] = (200, 200, 200, 255)
    r = photos.clean(png(tall))
    assert r.ok is False and "width/height 0.71" in r.reason                   # 200 / 280: not car-shaped


def test_the_picture_is_shared_by_cars_of_the_same_year_make_and_model(conn, photos_on, monkeypatch, tmp_path):
    c = commons(photos_on, COMET_RESULTS)
    monkeypatch.setattr(photos, "cutter", cut_ok)
    a = car(conn)
    b = car(conn, model="Comet Prime", name="The other Comet")
    assert photos.key_for(manual.get(conn, a["id"])) == photos.key_for(manual.get(conn, b["id"])) == \
        "vehicle:2020|zenith|comet prime"
    photos.ensure(conn, a["id"], TODAY, tmp_path)
    assert photos.ensure(conn, b["id"], TODAY, tmp_path) is None and len(c.queries) == 1
    assert photos.view(conn, manual.get(conn, b["id"]))["status"] == "ready"


def test_a_failed_fetch_is_recorded_and_tried_again_the_next_day(conn, photos_on, tmp_path):
    photos_on.on("commons", (503, b"down"))
    m = car(conn)
    rec = photos.ensure(conn, m["id"], TODAY, tmp_path)
    assert rec["status"] == "error" and "server error (HTTP 503)" in rec["note"]
    assert photos.due(conn, manual.get(conn, m["id"])) is False
    with conn:
        conn.execute("UPDATE asset_images SET fetched_at = ?", ((date.today() - timedelta(days=2)).isoformat() + "T12:00:00Z",))
    assert photos.due(conn, manual.get(conn, m["id"])) is True


def test_the_commons_cap_holds(conn, photos_on, monkeypatch, tmp_path):
    commons(photos_on, COMET_RESULTS)
    monkeypatch.setitem(config.API_CAPS, "commons", 1)
    monkeypatch.setattr(photos, "cutter", cut_ok)
    rec = photos.ensure(conn, car(conn)["id"], TODAY, tmp_path)                  # the search takes the only call
    assert rec["status"] == "error" and rec["note"].startswith("Wikimedia Commons's monthly limit is reached (1 of 1")


# --- a home: the Redfin listing photo, else OpenStreetMap, else the outline -----------------------------------------

@pytest.fixture
def redfin(free_sources, fake_http, fake_browser):
    rf = Redfin()
    fake_http.on("redfin", rf.http)
    fake_http.on("redfin_photo", lambda url, headers: (200, JPEG))
    fake_http.on("osm", lambda url, headers: (200, b"\x89PNG\r\n\x1a\n" + b"tile" * 50))
    return rf


def home(conn, address=ALDER, **kw):
    return valuation.create(conn, {"kind": "property", "name": "Home", "address": address, **kw}, TODAY)


def test_a_home_gets_its_redfin_listing_photo_from_the_same_lookup(conn, redfin, fake_http, tmp_path):
    h = home(conn)
    d = json.loads(conn.execute("SELECT details_json FROM asset_valuations").fetchone()[0])
    assert d["photo_url"] == "https://ssl.cdn-redfin.com/photo/40/bigphoto/975/31415975_0.jpg"
    assert redfin.asked == [f"autocomplete {ALDER}", "avm 5555501", "above 5555501"]
    assert conn.execute("SELECT calls FROM api_usage WHERE provider = 'redfin'").fetchone()[0] == 1   # one lookup
    rec = photos.ensure(conn, h["id"], TODAY, tmp_path)
    assert rec == {**rec, "status": "ready", "source": "redfin", "credit": "Photo: listing via Redfin", "cutout": 0}
    assert (tmp_path / rec["file"]).read_bytes() == JPEG
    photo = [(u, hd) for u, hd in fake_http.calls if "cdn-redfin" in u]
    assert len(photo) == 1 and photo[0][1]["User-Agent"].startswith("Mozilla/5.0") and photo[0][1]["Accept"] == "image/*"
    assert not [u for u, _ in fake_http.calls if "google" in u]                 # never the Street View URL
    assert redfin.asked[3:] == []                                               # the photo needed no second lookup
    assert photos.view(conn, manual.get(conn, h["id"]))["kind"] == "photo"


def test_a_street_view_url_is_never_taken_for_a_photo():
    above = {"a": {"fullScreenPhotoUrl": "https://maps.googleapis.com/maps/api/streetview?key=REDFINKEY"},
             "b": {"fullScreenPhotoUrl": "http://ssl.cdn-redfin.com/photo/1.jpg"},
             "c": {"fullScreenPhotoUrl": "https://evil.example/cdn-redfin.com/photo.jpg"}}
    assert valuation.redfin_photo_url(above) is None
    above["d"] = {"fullScreenPhotoUrl": "https://ssl.cdn-redfin.com/photo/40/bigphoto/975/31415975_0.jpg"}
    assert valuation.redfin_photo_url(above) == "https://ssl.cdn-redfin.com/photo/40/bigphoto/975/31415975_0.jpg"


def test_an_estimate_from_before_the_photo_asks_redfin_once_for_it(conn, redfin, tmp_path):
    h = home(conn)
    with conn:   # as the staging Alder row: valued before TASK-13b noted the photo
        conn.execute("UPDATE asset_valuations SET details_json = json_remove(details_json, '$.photo_url')")
    rec = photos.ensure(conn, h["id"], TODAY, tmp_path)
    assert rec["source"] == "redfin" and redfin.asked[-1] == "above 5555501"
    calls = conn.execute("SELECT account_id, note FROM api_calls WHERE provider = 'redfin' ORDER BY id").fetchall()
    assert [tuple(r) for r in calls] == [(h["id"], "via https"), (None, "via https · photo · location"), (None, "image")]


def test_no_listing_photo_gives_an_openstreetmap_tile_with_a_pin(conn, redfin, fake_http, tmp_path):
    redfin.above = {}                                                           # the listing has no photo
    h = home(conn)
    rec = photos.ensure(conn, h["id"], TODAY, tmp_path)
    x, y, px, py = photos.tile_for(38.4411, -122.7193)
    tile = [(u, hd) for u, hd in fake_http.calls if "openstreetmap" in u]
    assert tile[0][0] == f"https://tile.openstreetmap.org/17/{x}/{y}.png" and len(tile) == 1
    assert tile[0][1]["User-Agent"] == photos.USER_AGENT
    assert rec == {**rec, "status": "ready", "source": "osm", "credit": "© OpenStreetMap contributors",
                   "credit_url": "https://www.openstreetmap.org/copyright", "pin_x": px, "pin_y": py}
    v = photos.view(conn, manual.get(conn, h["id"]))
    assert v["kind"] == "map" and v["pin"] == {"x": px, "y": py}


def test_the_tile_math():
    assert photos.tile_for(0.0, 0.0, 1) == (1, 1, 0.0, 0.0)
    x, y, px, py = photos.tile_for(34.17982, -118.43144)
    assert (x, y) == (22416, 52280) and 0 <= px < 1 and 0 <= py < 1   # by hand: 61.5686 / 360 * 2^17 = 22416.4


def test_a_unit_redfin_has_no_row_for_gets_the_map_at_its_building(conn, redfin, fake_http, tmp_path):
    redfin.avm["300004120"] = {"predictedValue": 1031933.0, "latLong": {"latitude": 34.17982, "longitude": -118.43144}}
    h = home(conn, "4120 Birch Ave #303, Santa Rosa, CA 95404", balance=500000)
    assert h["estimate_result"]["reason"] == "no_estimate"
    rec = photos.ensure(conn, h["id"], TODAY, tmp_path)
    assert rec["source"] == "osm" and (rec["lat"], rec["lng"]) == (34.17982, -118.43144)
    assert redfin.asked[-2:] == ["autocomplete 4120 Birch Ave, Santa Rosa, CA 95404", "avm 300004120"]
    assert conn.execute("SELECT COUNT(*) FROM asset_valuations").fetchone()[0] == 0   # the building's value: never kept
    assert manual.get(conn, h["id"])["balance"] == 500000.0


def test_no_location_draws_the_house(conn, redfin, tmp_path):
    redfin.homes = {}
    h = home(conn, "1 Nowhere Rd, Springfield, IL 62701", balance=300000)
    rec = photos.ensure(conn, h["id"], TODAY, tmp_path)
    assert rec["status"] == "none" and rec["note"] == "no listing photo and no location on Redfin"
    assert photos.view(conn, manual.get(conn, h["id"]))["kind"] == "outline"



def test_the_value_table_says_not_checked_yet_until_redfin_is_asked(conn, redfin):
    typed = manual.create(conn, {"kind": "property", "name": "Typed", "address": ALDER, "balance": 500000}, TODAY)
    assert valuation.detail(conn, typed["id"], TODAY)["table"] == [
        {"label": "Estimate", "source": "Redfin", "value": None, "primary": True, "empty": "not checked yet"}]
    unit = home(conn, "4120 Birch Ave #303, Santa Rosa, CA 95404", balance=500000)   # asked: none for this unit
    assert valuation.detail(conn, unit["id"], TODAY)["table"][0]["empty"] == "no estimate"
    assert "empty" not in valuation.detail(conn, home(conn)["id"], TODAY)["table"][0]

# --- the page: the images route, the background fetch, the switch -----------------------------------------------------

@pytest.fixture
def app(conn):
    a = App(config.db_path(), lambda: None)
    a.today = lambda: TODAY
    return a


def call(app, method, path, body=None):
    status, ctype, raw = app.handle(method, path, POST if method == "POST" else LOCAL,
                                    json.dumps(body or {}).encode() if method == "POST" else b"")
    return status, ctype, raw


def test_pictures_are_served_from_the_images_folder_and_nothing_else(app, conn, photos_on, monkeypatch):
    commons(photos_on, COMET_RESULTS)
    monkeypatch.setattr(photos, "cutter", cut_ok)
    m = car(conn)
    rec = photos.ensure(conn, m["id"], TODAY, photos.images_dir(config.db_path()))
    status, ctype, raw = call(app, "GET", f"/api/images/{rec['file']}")
    assert status == 200 and ctype == "image/png" and raw == PNG_CUT
    assert App.cache_control(f"/api/images/{rec['file']}?v=1", 200) == "private, max-age=86400"
    for bad in ("../finance.db", "..%2Ffinance.db", rec["file"].upper(), rec["file"].replace(".png", ".db"),
                "0123456789abcdef.png"):
        assert call(app, "GET", f"/api/images/{bad}")[0] == 404


def test_the_first_view_and_a_save_fetch_the_picture_in_the_background(app, conn, photos_on, monkeypatch):
    m = car(conn)
    status, _, raw = call(app, "GET", f"/api/manual/{m['id']}/detail")
    assert status == 200 and json.loads(raw)["photo"]["status"] == "pending" and SCHEDULED == [m["id"]]
    status, _, raw = call(app, "POST", "/api/manual", {"kind": "vehicle", "vehicle_year": 2019, "vehicle_make": "Honda",
                                                       "vehicle_model": "Civic", "balance": 9000})
    assert status == 200 and SCHEDULED == [m["id"], json.loads(raw)["id"]]
    photos._save(conn, photos.key_for(manual.get(conn, m["id"])), "vehicle", "none", note="nothing")
    call(app, "GET", f"/api/manual/{m['id']}/detail")
    assert SCHEDULED.count(m["id"]) == 1                                         # tried: not asked again


def test_photos_switched_off_are_never_fetched(app, conn, data_dir, tmp_path):
    (tmp_path / "hermes.env").write_text((tmp_path / "hermes.env").read_text() + "PHOTOS_ENABLED=0\n")
    m = car(conn)
    status, _, raw = call(app, "GET", f"/api/manual/{m['id']}/detail")
    assert json.loads(raw)["photo"] == {**json.loads(raw)["photo"], "status": "none", "kind": "silhouette"}
    assert SCHEDULED == [] and photos.ensure(conn, m["id"], TODAY, tmp_path) is None


def test_the_background_run_is_one_thread_per_picture(conn, monkeypatch):
    started, gate = [], threading.Event()

    def slow(c, account_id, today, folder, force=False, trigger="refresh"):
        started.append(account_id)
        gate.wait(5)

    monkeypatch.setattr(photos, "ensure", slow)
    path = config.db_path()
    t = photos.run_in_background(path, 7)
    assert photos.run_in_background(path, 7) is None                             # already running
    gate.set()
    t.join(5)
    assert started == [7] and photos.run_in_background(path, 7).join(5) is None and started == [7, 7]


# --- the loan link --------------------------------------------------------------------------------------------------

def test_a_loan_link_is_validated(conn):
    loan = manual.create(conn, {"kind": "loan", "name": "Car Loan", "institution": "TFS", "subtype": "auto",
                                "original_amount": 30000, "balance": 5907.88, "monthly_payment": 465.1}, TODAY)
    bank = manual.create(conn, {"kind": "bank", "name": "Cash", "institution": "X", "subtype": "checking", "balance": 1}, TODAY)
    m = car(conn, linked_loan_id=loan["id"])
    assert m["linked_loan_id"] == loan["id"]
    for bad, msg in ((bank["id"], "Loan must be one of your loans"), (999, "Loan must be one of your loans"),
                     ("2", "Loan must be one of your loans"), (True, "Loan must be one of your loans")):
        with pytest.raises(manual.ManualError, match=msg) as e:
            manual.edit(conn, m["id"], {"linked_loan_id": bad}, TODAY)
        assert e.value.field == "linked_loan_id"
    manual.archive(conn, loan["id"], TODAY)
    with pytest.raises(manual.ManualError, match="That loan is archived"):
        car(conn, linked_loan_id=loan["id"], name="x")
    with pytest.raises(manual.ManualError, match="doesn't apply to bank"):
        manual.validate("bank", {"linked_loan_id": 1}, TODAY, partial=True)


V8_SQL = (Path(__file__).parent / "fixtures" / "schema_v8.sql").read_text()


def make_v8(path, vehicles=1, autos=1):
    raw = sqlite3.connect(path)
    raw.executescript(V8_SQL)
    ts = "2026-09-26T20:40:46Z"
    for i in range(autos):
        raw.execute("INSERT INTO manual_accounts (kind, name, institution, subtype, balance, original_amount, monthly_payment, "
                    "balance_as_of, created_at, updated_at) VALUES ('loan', ?, 'TFS', 'auto', 5907.88, 30567, 465.1, "
                    "'2026-09-26', ?, ?)", (f"Car Loan {i}", ts, ts))
    for i in range(vehicles):
        raw.execute("INSERT INTO manual_accounts (kind, name, balance, vehicle_make, vehicle_model, vehicle_year, balance_as_of, "
                    "created_at, updated_at, vin, value_source, vehicle_condition) VALUES ('vehicle', ?, 14090, 'Zenith', "
                    "'Comet Prime', 2020, '2026-09-26', ?, ?, ?, 'user', 'good')", (f"Comet {i}", ts, ts, VIN))
    raw.execute("INSERT INTO manual_accounts (kind, name, balance, balance_as_of, created_at, updated_at) "
                "VALUES ('credit_card', 'Card', 1110.66, '2026-09-26', ?, ?)", (ts, ts))
    raw.execute("INSERT INTO asset_valuations (account_id, value, source, miles, subject, fetched_at, raw_json) "
                "VALUES (?, 20198, 'marketcheck', 108000, ?, ?, '{}')", (autos + 1, VIN, ts))
    raw.commit()
    rows = [tuple(r) for r in raw.execute("SELECT * FROM manual_accounts ORDER BY id")]
    raw.close()
    return rows


def test_upgrade_from_v8_links_the_only_auto_loan_and_adds_the_image_cache(data_dir):
    path = config.db_path()
    config.ensure_data_dir(path.parent)
    before = make_v8(path)
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
    after = [tuple(r) for r in conn.execute("SELECT * FROM manual_accounts ORDER BY id")]
    assert [a[:-1] for a in after] == before                                    # every old value kept
    assert [(r["kind"], r["linked_loan_id"]) for r in conn.execute("SELECT * FROM manual_accounts ORDER BY id")] == \
        [("loan", None), ("vehicle", 1), ("credit_card", None)]                  # the car -> the only auto loan
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(db.SCHEMA)
    for table in ("manual_accounts", "asset_images"):
        assert [r[1:3] for r in conn.execute(f"PRAGMA table_info({table})")] == \
            [r[1:3] for r in fresh.execute(f"PRAGMA table_info({table})")]
    assert conn.execute("SELECT source, value FROM asset_valuations").fetchall()[0][:] == ("marketcheck", 20198.0)   # kept
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("INSERT INTO asset_images (key, kind, status, fetched_at) VALUES ('k', 'boat', 'ready', 'x')")
    with conn:  # unlinked by hand, then the app restarts: the migration doesn't run again
        conn.execute("UPDATE manual_accounts SET linked_loan_id = NULL WHERE kind = 'vehicle'")
    conn.close()
    again = db.connect(path)
    assert again.execute("SELECT linked_loan_id FROM manual_accounts WHERE kind = 'vehicle'").fetchone()[0] is None
    again.close()


@pytest.mark.parametrize("vehicles, autos", [(2, 1), (1, 2), (1, 0)])
def test_an_ambiguous_loan_is_left_unlinked(data_dir, vehicles, autos):
    path = config.db_path()
    config.ensure_data_dir(path.parent)
    make_v8(path, vehicles, autos)
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM manual_accounts WHERE linked_loan_id IS NOT NULL").fetchone()[0] == 0
    conn.close()


def test_cli_photos_fetches_whats_due_and_the_forced_one(conn, photos_on, monkeypatch, capsys):
    from hermes_finance import cli
    c = commons(photos_on, COMET_RESULTS)
    monkeypatch.setattr(photos, "cutter", cut_ok)
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    m = car(conn)
    assert cli.main(["photos"]) == 0
    out = capsys.readouterr().out
    assert (f"#{m['id']} 2020 Zenith Comet Prime (PHEV): fetched: ready, shows a photo, cut out "
            "(Photo: SsmIntrigue, CC BY-SA 4.0 · Wikimedia Commons)") in out
    assert "calls this month: Redfin 0/20, Wikimedia Commons 3/60, OpenStreetMap 0/20" in out
    assert cli.main(["photos"]) == 0 and "kept: ready" in capsys.readouterr().out and len(c.queries) == 1
    assert cli.main(["photos", "--account", str(m["id"]), "--force", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["fetched"] is True and len(c.queries) == 2
    assert (photos.images_dir(config.db_path()) / photos.file_name(photos.key_for(manual.get(conn, m["id"])), "png")).exists()
    loan = manual.create(conn, {"kind": "loan", "name": "L", "institution": "X", "subtype": "auto", "original_amount": 10,
                                "balance": 5, "monthly_payment": 1}, TODAY)
    assert cli.main(["photos", "--account", str(loan["id"])]) == 2


def test_a_thumbnail_on_thumb_wikimedia_is_accepted_but_other_hosts_are_not():
    """Live 2026-09-27: Commons returned the Comet thumbnail from thumb.wikimedia.org, and the picker (which only
    allowed upload.wikimedia.org) rejected every photo, so the page fell back to the drawing."""
    from hermes_finance import photos
    title = "2020 Zenith Comet Prime, Front Right, 10-24-2020.jpg"
    make, model = photos._words("Zenith"), photos.model_words("Comet Prime (PHEV)")
    live = page(title)
    live["imageinfo"][0]["thumburl"] = ("https://thumb.wikimedia.org/wikipedia/commons/thumb/3/3e/"
                                        "2020_Zenith_Comet_Prime%2C_Front_Right%2C_10-24-2020.jpg/1920px-x.jpg")
    assert photos.candidate(live, 2020, make, model) is not None
    for bad in ("https://evil.example/x.jpg", "https://thumb.wikimedia.org.evil.example/x.jpg", "http://thumb.wikimedia.org/x.jpg"):
        other = page(title)
        other["imageinfo"][0]["thumburl"] = bad
        other["imageinfo"][0]["url"] = bad
        assert photos.candidate(other, 2020, make, model) is None, bad


def test_a_factory_series_code_is_never_used_as_the_trim():
    """Real data 2026-09-27: NHTSA gave the owner's Comet no Trim but Series "CMT2L-AHXEBA / CMT2L-AHXGBA"; that
    became the trim and overflowed the edit form's Trim field (the layout gate caught it)."""
    from hermes_finance import valuation
    base = {"ModelYear": "2020", "Make": "ZENITH", "Model": "Comet Prime", "ElectrificationLevel": "PHEV"}
    out = valuation._decoded({"Results": [{**base, "Trim": "", "Series": "CMT2L-AHXEBA / CMT2L-AHXGBA"}]}, TODAY)
    assert "vehicle_trim" not in out
    out = valuation._decoded({"Results": [{**base, "Trim": "Limited", "Series": "CMT2L-AHXEBA"}]}, TODAY)
    assert out["vehicle_trim"] == "Limited"

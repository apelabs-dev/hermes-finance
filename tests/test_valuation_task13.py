"""TASK-13: Carfax car values (trade-in, in the local Chrome), Redfin home values, the condo-unit rules, the value
precedence and the page data.

No real Carfax / Redfin / MarketCheck / RentCast / NHTSA call and no real Chrome: HTTP is conftest.FakeHttp, the browser
is conftest.FakeBrowser (the response shapes recorded in the TASK-13 brief), and the CDP tests drive a throwaway
headless Chrome (tests/cdp.py, random port) against a fake Carfax on loopback. conftest also refuses port 9222 at the
socket level and switches the DevTools client off for every process a test starts (HERMES_CHROME_DEVTOOLS=off)."""

from __future__ import annotations

import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import (CARFAX_CAPTCHA, CARFAX_PRICES, CARFAX_TRIMS, CARFAX_VIN, ALDER, ALDER_AVM, NOT_AVAILABLE,
                      PRODUCTION_FREE_SOURCES_DEFAULT, BIRCH_BUILDING, OfflineBrowser, Redfin)
from conftest import redfin_row as row
from hermes_finance import cdp_client, cli, config, db, manual, reads, usage, valuation
from hermes_finance.web import App

TODAY = date(2026, 11, 2)          # outside the September seed's 32-day window
LOCAL = {"Host": "127.0.0.1:8765"}
POST = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
COMET = {"kind": "vehicle", "vin": CARFAX_VIN, "mileage": 143210, "zip_code": "95404"}
# carfax.com/value's prices request as recorded in the brief (its condition was "excellent"; ours defaults to good)
RECORDED_BODY = {"vin": "SYNTHCARXL3012345", "zip": "95404", "condition": "excellent", "odometer": 143210,
                 "trimOptions": {"options": [{"componentId": 0, "component": "Black", "featureCategory": "EXTERIOR_COLOR",
                                              "standardFrequency": "", "componentDefault": True}],
                                 "subtrim": "", "trim": "LIMITED", "trimDefault": True, "trimDisplay": "LIMITED"}}
# NHTSA's extended decode of the same VIN (the fields the page uses, as the real decode returned them on 2026-09-26)
NHTSA_COMET = {"Count": 1, "Results": [{
    "ModelYear": "2020", "Make": "ZENITH", "Model": "Comet Prime (PHEV)", "Trim": "", "Series": "CMT2L-AHXEBA / CMT2L-AHXGBA",
    "BodyClass": "Sedan/Saloon", "Doors": "5", "Seats": "5", "DisplacementL": "1.8", "EngineCylinders": "4",
    "EngineConfiguration": "In-Line", "ElectrificationLevel": "PHEV (Plug-in Hybrid Electric Vehicle)",
    "TransmissionStyle": "Electronic Continuously Variable (e-CVT)", "DriveType": "4x2", "CurbWeightLB": "3375",
    "WheelBaseShort": "106.30", "WheelSizeFront": "15", "PlantCity": "ZENITH CITY", "PlantCountry": "JAPAN",
    "AdaptiveCruiseControl": "Standard", "LaneKeepSystem": "Standard", "LaneDepartureWarning": "Standard",
    "ForwardCollisionWarning": "Standard", "CIB": "Standard", "PedestrianAutomaticEmergencyBraking": "Standard",
    "BlindSpotMon": "", "TPMS": "Direct", "ABS": "Standard", "ErrorCode": "0"}]}


def day(n):
    return TODAY + timedelta(days=n)


def calls(conn, provider, month=None):
    r = conn.execute("SELECT calls FROM api_usage WHERE provider = ? AND month = ?",
                     (provider, (month or TODAY.isoformat()[:7]))).fetchone()
    return r[0] if r else 0


def vin_with(serial: str) -> str:
    """A valid VIN (check digit computed) for the Comet's first 8 characters and a 6-digit serial."""
    head, tail = "SYNTHCAR", f"L3{serial}"
    probe = head + "0" + tail
    return head + valuation.vin_check_digit(probe) + tail


@pytest.fixture
def clock(monkeypatch):
    """Estimates are stamped with the real time (fetched_at); a test that moves `today` moves this clock too, so the
    days the precedence compares are the test's days."""
    def at(d: date):
        monkeypatch.setattr(valuation, "now", lambda: f"{d.isoformat()}T18:00:00Z")
    at(TODAY)
    return at


@pytest.fixture
def comet_http(fake_http):
    fake_http.on("nhtsa", (200, NHTSA_COMET))
    return fake_http


def set_env(path: Path, **values):
    path.write_text(path.read_text() + "".join(f"{k}={v}\n" for k, v in values.items()))


# --- switches, caps --------------------------------------------------------------------------------------------------

def test_carfax_and_redfin_are_off_on_a_new_install_and_the_env_file_switches_them(tmp_path):
    # TASK-18 addendum 3: a new install starts with Carfax and Redfin off (free public pages, not APIs); an install from
    # before keeps them on through the v11 migration (test_settings_task18); an env-file flag still wins either way
    assert PRODUCTION_FREE_SOURCES_DEFAULT == {"carfax": False, "redfin": False, "photos": True}
    env = tmp_path / "e.env"
    env.write_text("")
    prog = ("import json\nfrom hermes_finance import config, cdp_client\n"
            "print(json.dumps([config.free_sources(), cdp_client.Chrome().endpoint()]))")
    for text, want in (("", {"carfax": False, "redfin": False, "photos": True}),
                       ("CARFAX_ENABLED=0\nREDFIN_ENABLED=1\n", {"carfax": False, "redfin": True, "photos": True}),
                       ("REDFIN_ENABLED=off\nCARFAX_ENABLED=1\nPHOTOS_ENABLED=no\n",
                        {"carfax": True, "redfin": False, "photos": False})):
        env.write_text(text)
        # a fresh process: no conftest patch there, only what production would read (and the inherited DevTools switch)
        r = subprocess.run([sys.executable, "-c", prog], env={**os.environ, "HERMES_ENV_FILE": str(env)},
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        sources, endpoint = json.loads(r.stdout)
        assert sources == want and endpoint is None       # and even there, the real Chrome is out of reach


def test_the_new_caps_and_the_seven_day_block():
    assert config.api_caps() == {"nhtsa": 300, "carfax": 6, "redfin": 20, "commons": 60, "osm": 20}
    assert config.BLOCK_DAYS == {"carfax": 7, "redfin": 7, "commons": 1, "osm": 7}
    assert usage.block_end("carfax", TODAY) == day(7) and usage.block_end("redfin", TODAY) == day(7)
    assert usage.block_end("nhtsa", TODAY) == date(2026, 12, 1)         # NHTSA: the rest of the month


# --- Carfax -------------------------------------------------------------------------------------------------------

def test_carfax_sends_exactly_the_recorded_requests_and_keeps_the_trade_in(conn, free_sources, comet_http, fake_browser):
    m = valuation.create(conn, COMET, TODAY)
    # one background tab on carfax.com/value, closed; two in-page requests, the second exactly as the page sends it
    assert fake_browser.opened == ["https://www.carfax.com/value/"] and fake_browser.closed == fake_browser.opened
    first, second = fake_browser.fetches
    assert (first["method"], first["url"], first["body"]) == (
        "GET", f"https://consumerapi.carfax.com/hbv/cow/trims/{CARFAX_VIN}", None)
    assert (second["method"], second["url"], second["headers"]) == (
        "POST", "https://consumerapi.carfax.com/hbv/cow/prices", {"Content-Type": "application/json"})
    expected = {**RECORDED_BODY, "condition": "good"}                 # the account's condition, default good
    assert second["body"] == json.dumps(expected)                     # same keys, same order
    # NHTSA has no Trim for this VIN (only a factory Series code, which is no longer used as the trim), so the
    # account has no trim: Carfax's default (LIMITED) and its default colour (Black)
    assert m["vehicle_trim"] is None
    # trade-in (wholesalePrice) is the value; private sale and dealer retail beside it
    assert m["value_source"] == "estimate" and m["balance"] == 14090.0
    v = m["valuation"]
    est = v["estimate"]
    assert (est["value"], est["source"], est["basis"], est["basis_label"]) == (14090.0, "carfax", "trade_in", "Trade-in")
    assert est["details"] == {"trade_in": 14090.0, "private_sale": 17160.0, "dealer_retail": 19360.0, "records": 25,
                              "carfax_odometer": 143210, "trim": "LIMITED", "condition": "good", "zip": "95404",
                              "colour": "Black", "display_status": "Displayable",
                              "specs": {"bodyType": "4 DOOR HATCHBACK", "driveType": "FWD",
                                        "engineName": "1.8L Plug-in Hybrid I4 121hp", "engineCylinders": 4,
                                        "engineSize": 1.8, "fuelType": "PLUG-IN GAS/ELECTRIC HYBRID",
                                        "transType": "CONTINUOUSLY VARIABLE", "segmentationDesc": "COMPACT CAR",
                                        "trim": "LIMITED", "year": 2020}}
    assert m["estimate_result"]["message"] == "Trade-in $14,090 by Carfax (private sale $17,160, dealer retail $19,360)"
    # counted once (a lookup = its two requests), as a Carfax call for this account
    assert calls(conn, "carfax") == 1
    assert [tuple(r) for r in conn.execute("SELECT provider, account_id, trigger, outcome FROM api_calls "
                                           "WHERE provider = 'carfax'")] == [("carfax", m["id"], "save", "ok")]
    saved = conn.execute("SELECT * FROM asset_valuations WHERE source = 'carfax'").fetchone()
    assert json.loads(saved["raw_json"]) == {"displayStatus": "Displayable", "numberOfRecords": 25, "odometer": 143210,
                                             "vehiclePrice": {"consumerRetailPrice": 19360, "fsboPrice": 17160,
                                                              "wholesalePrice": 14090}}
    assert saved["miles"] == 143210 and saved["subject"] == CARFAX_VIN and saved["low"] is None
    assert v["odometer_hint"] is None                                 # Carfax's odometer isn't higher


def test_carfax_uses_the_accounts_trim_when_it_is_one_of_carfaxs(conn, free_sources, comet_http, fake_browser):
    valuation.create(conn, {**COMET, "vehicle_make": "Zenith", "vehicle_model": "Comet Prime", "vehicle_year": 2020,
                            "vehicle_trim": "xle", "vehicle_condition": "Excellent"}, TODAY)
    body = json.loads(fake_browser.fetches[1]["body"])
    assert body["condition"] == "excellent"
    assert body["trimOptions"] == {"options": [{"componentId": 0, "component": "Classic Silver Metallic",
                                                "featureCategory": "EXTERIOR_COLOR", "standardFrequency": "",
                                                "componentDefault": True}],
                                   "subtrim": "", "trim": "XLE", "trimDefault": False, "trimDisplay": "XLE"}


def test_the_condition_is_one_of_carfaxs_own_values():
    assert manual.VEHICLE_CONDITIONS == ("fair", "good", "excellent")   # carfax.com/value's bundle: {fair, good, excellent}
    assert manual.validate("vehicle", {"vehicle_condition": " FAIR "}, TODAY, partial=True) == {"vehicle_condition": "fair"}
    with pytest.raises(manual.ManualError, match="Condition must be one of fair, good, excellent") as e:
        manual.validate("vehicle", {"vehicle_condition": "poor"}, TODAY, partial=True)
    assert e.value.field == "vehicle_condition"
    with pytest.raises(manual.ManualError, match="doesn't apply to property"):
        manual.validate("property", {"vehicle_condition": "good"}, TODAY, partial=True)


def test_editing_the_condition_asks_carfax_again_within_the_guard(conn, free_sources, comet_http, fake_browser):
    m = valuation.create(conn, COMET, TODAY)
    assert len(fake_browser.opened) == 1
    same_day = valuation.edit(conn, m["id"], {"vehicle_condition": "excellent"}, TODAY)
    assert same_day["vehicle_condition"] == "excellent" and same_day["estimate_result"]["reason"] == "today"
    assert len(fake_browser.opened) == 1                              # saved; Carfax is asked again tomorrow at most
    e = valuation.edit(conn, m["id"], {"vehicle_condition": "fair"}, day(1))
    assert e["vehicle_condition"] == "fair" and json.loads(fake_browser.fetches[-1]["body"])["condition"] == "fair"
    assert len(fake_browser.opened) == 2 and e["estimate_result"]["provider"] == "carfax"
    cleared = valuation.edit(conn, m["id"], {"vehicle_condition": None}, day(2))
    assert cleared["vehicle_condition"] == "good"                    # cleared: back to the default


@pytest.mark.parametrize("response, why", [
    (CARFAX_CAPTCHA, "a captcha page"),                                               # the 403 + captcha seen live
    ((403, '{"message": "Forbidden"}'), "HTTP 403"),
    ((200, {"url": "https://geo.captcha-delivery.com/captcha/?initialCid=AHrlqAAAAAMA"}), "a captcha page"),
    ((200, "<!doctype html><html><body>Please wait...</body></html>"), "a web page"),
])
def test_a_bot_wall_blocks_carfax_for_seven_days(conn, free_sources, comet_http, fake_browser, response, why):
    fake_browser.on("GET", "consumerapi.carfax.com/hbv/cow/trims/", response)
    m = valuation.create(conn, {**COMET, "balance": 14090}, TODAY)
    r = m["estimate_result"]
    assert r["ok"] is False and r["called"] is True and r["reason"] == "blocked"
    assert r["blocked_until"] == day(7).isoformat() and f"Carfax blocked the check (trims: {why})" in r["message"]
    assert len(fake_browser.fetches) == 1 and fake_browser.closed == fake_browser.opened   # no prices request after it
    assert calls(conn, "carfax") == 1
    assert conn.execute("SELECT outcome FROM api_calls WHERE provider = 'carfax'").fetchone()[0] == "blocked"
    # while blocked: nothing sent, nothing counted, whoever asks
    for d in (0, 1, 6):
        again = valuation.refresh(conn, m["id"], day(d))["estimate_result"]
        assert again["called"] is False and again["reason"] in ("blocked", "today")
    assert valuation.revalue(conn, day(6))["results"][0]["action"] == "skipped"
    assert len(fake_browser.opened) == 1 and calls(conn, "carfax") == 1
    # a week later it may ask again
    fake_browser.routes.pop(("GET", "consumerapi.carfax.com/hbv/cow/trims/"))
    fake_browser.on("GET", "consumerapi.carfax.com/hbv/cow/trims/", (200, CARFAX_TRIMS))
    assert valuation.refresh(conn, m["id"], day(7))["estimate_result"]["ok"] is True


def test_no_vehicle_price_in_the_answer_blocks_carfax_too(conn, free_sources, comet_http, fake_browser):
    fake_browser.on("POST", "consumerapi.carfax.com/hbv/cow/prices",
                    (200, {"atomInfo": CARFAX_PRICES["atomInfo"], "displayStatus": "NotDisplayable"}))
    r = valuation.create(conn, {**COMET, "balance": 14090}, TODAY)["estimate_result"]
    assert r["reason"] == "blocked" and r["blocked_until"] == day(7).isoformat()
    assert r["message"].startswith("Carfax gave no price (vehiclePrice missing, displayStatus NotDisplayable)")
    assert conn.execute("SELECT COUNT(*) FROM asset_valuations").fetchone()[0] == 0


def test_the_block_survives_a_restart(conn, free_sources, comet_http, fake_browser):
    fake_browser.on("GET", "consumerapi.carfax.com/hbv/cow/trims/", CARFAX_CAPTCHA)
    m = valuation.create(conn, {**COMET, "balance": 14090}, TODAY)
    prog = f"""
import json
from datetime import date
from hermes_finance import db, usage, valuation
class NoTab:
    def available(self): return None          # Chrome "is there": only the saved block may stop the lookup
    def tab(self, url): raise AssertionError("a blocked Carfax was asked")
valuation.browser = NoTab()
conn = db.connect()
try:
    usage.reserve(conn, "carfax", date(2026, 11, 8), {m['id']}, "refresh")
    reserve = "allowed"
except usage.Refused as e:
    reserve = e.reason
r = valuation.refresh(conn, {m['id']}, date(2026, 11, 8))["estimate_result"]
print(json.dumps({{"reserve": reserve, "reason": r["reason"], "called": r["called"], "pid": __import__("os").getpid()}}))
"""
    env = {**os.environ, "FINANCE_DATA_DIR": str(config.data_dir()), "HERMES_ENV_FILE": str(free_sources)}
    r = subprocess.run([sys.executable, "-c", prog], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["pid"] != os.getpid() and out["reserve"] == "blocked"
    assert out["reason"] == "blocked" and out["called"] is False and calls(conn, "carfax") == 1


def test_chrome_not_running_fails_soft_and_counts_nothing(conn, free_sources, comet_http, fake_browser):
    fake_browser.down = "Chrome not running (nothing answers on 127.0.0.1:9222)"
    with pytest.raises(manual.ManualError) as e:                      # nothing to fall back on: the value is needed
        valuation.create(conn, COMET, TODAY)
    assert str(e.value) == ("Your value is required for now: Carfax skipped: Chrome not running (nothing answers on "
                            "127.0.0.1:9222)")
    m = valuation.create(conn, {**COMET, "balance": 14090}, TODAY)
    r = m["estimate_result"]
    assert r == {**r, "ok": False, "called": False, "reason": "unavailable",
                 "message": "Carfax skipped: Chrome not running (nothing answers on 127.0.0.1:9222)"}
    assert fake_browser.opened == [] and calls(conn, "carfax") == 0
    assert conn.execute("SELECT COUNT(*) FROM api_calls WHERE provider = 'carfax'").fetchone()[0] == 0
    # not counted, so not "asked today" either: once Chrome is back, the same day works
    fake_browser.down = None
    assert valuation.refresh(conn, m["id"], TODAY)["estimate_result"]["ok"] is True


def test_the_tab_is_closed_when_chrome_fails_mid_lookup(conn, free_sources, comet_http, fake_browser):
    fake_browser.on("POST", "consumerapi.carfax.com/hbv/cow/prices", cdp_client.CDPError("the tab crashed"))
    r = valuation.create(conn, {**COMET, "balance": 14090}, TODAY)["estimate_result"]
    assert r["reason"] == "browser" and r["called"] is True and r["blocked_until"] is None
    assert "Chrome failed during the lookup (the tab crashed)" in r["message"]
    assert fake_browser.closed == fake_browser.opened == ["https://www.carfax.com/value/"]
    assert calls(conn, "carfax") == 1                                 # it was sent: it counts
    fake_browser.open_error = cdp_client.CDPError("Chrome not running")
    r = valuation.refresh(conn, 1, day(1))["estimate_result"]
    assert r["reason"] == "browser" and fake_browser.closed == fake_browser.opened


def test_an_unexpected_carfax_answer_is_counted_and_explained(conn, free_sources, comet_http, fake_browser):
    fake_browser.on("GET", "consumerapi.carfax.com/hbv/cow/trims/", (200, {**CARFAX_TRIMS, "trimOptions": 5}))
    r = valuation.create(conn, {**COMET, "balance": 14090}, TODAY)["estimate_result"]
    assert r["reason"] == "bad_response" and r["message"] == "Carfax sent an unexpected answer"
    assert fake_browser.closed == fake_browser.opened and calls(conn, "carfax") == 1


def test_carfax_cap_is_six_a_month_and_one_a_day_per_car(conn, free_sources, comet_http, fake_browser):
    ids = [valuation.create(conn, {**COMET, "vin": vin_with(f"{n:06d}")}, TODAY)["id"] for n in range(1, 7)]
    assert calls(conn, "carfax") == 6 and len(fake_browser.opened) == 6
    seventh = valuation.create(conn, {**COMET, "vin": vin_with("000007"), "balance": 9000}, TODAY)["estimate_result"]
    assert seventh["reason"] == "cap" and seventh["called"] is False
    assert seventh["message"] == "Carfax's monthly limit is reached (6 of 6 calls this month); it resumes on 2026-12-01"
    assert len(fake_browser.opened) == 6
    # one a day per car: a Refresh the day it was saved is refused before anything is sent
    r = valuation.refresh(conn, ids[0], TODAY)["estimate_result"]
    assert r["reason"] in ("today", "cap") and len(fake_browser.opened) == 6


def test_carfax_asks_once_a_day_per_car_and_monthly_in_the_daily_run(conn, free_sources, comet_http, fake_browser):
    m = valuation.create(conn, COMET, TODAY)
    assert valuation.refresh(conn, m["id"], TODAY)["estimate_result"]["reason"] == "today"
    for n in (1, 15, 29):
        x = valuation.revalue(conn, day(n))["results"][0]
        assert x["action"] == "skipped" and "next monthly check on 2026-12-02" in x["message"]
    assert valuation.revalue(conn, day(30))["results"][0]["action"] == "valued"
    assert len(fake_browser.opened) == 2 and calls(conn, "carfax") == 1 and calls(conn, "carfax", "2026-12") == 1


def test_carfax_says_more_miles_quietly_and_never_changes_the_mileage(conn, free_sources, comet_http, fake_browser):
    m = valuation.create(conn, {**COMET, "mileage": 100000}, TODAY)
    assert json.loads(fake_browser.fetches[1]["body"])["odometer"] == 100000        # we send ours, not Carfax's
    assert m["mileage"] == 100000
    assert m["valuation"]["odometer_hint"] == {"miles": 143210, "message": "Carfax records show 143,210 mi, update?"}
    assert "Carfax records show 143,210 mi, update? (finance manual edit 1 --mileage N)" in cli._valuation_text(m)
    assert manual.get(conn, m["id"])["mileage"] == 100000
    m = valuation.edit(conn, m["id"], {"mileage": 143210}, day(1))
    assert m["valuation"]["odometer_hint"] is None


# --- which value counts (TASK-13 precedence) ------------------------------------------------------------------------

# --- Redfin -------------------------------------------------------------------------------------------------------

def home(address=ALDER, **kw):
    return {"kind": "property", "name": "Home", "address": address, **kw}


def test_stingray_prefix_is_stripped_and_a_unit_is_read_the_same_every_way():
    assert valuation.stingray('{}&&{"payload": {"a": 1}}') == {"payload": {"a": 1}}
    assert valuation.stingray('{"payload": 2}') == {"payload": 2} and valuation.stingray("<html>") is None
    for text in ("4120 Birch Ave #303, Santa Rosa, CA 95404", "4120 Birch Ave Unit 303, Santa Rosa, CA 95404",
                 "4120 Birch Ave, Apt 303, Santa Rosa, CA 95404", "4120 Birch Ave Ste 303, Santa Rosa, CA 95404",
                 "4120 Birch Ave, Apt. 303, Santa Rosa, CA 95404", "4120 Birch Ave Suite 303", "4120 Birch Ave # 303",
                 "4120 N Birch Ave APT #303"):
        a = valuation.parse_address(text)
        assert (a.number, a.street, a.unit) == ("4120", "birch", "303"), text
    assert valuation.parse_address("4120 Birch Ave #303, Santa Rosa, CA 95404").building == \
        "4120 Birch Ave, Santa Rosa, CA 95404"
    alder = valuation.parse_address(ALDER)
    assert (alder.number, alder.street, alder.unit, alder.building) == ("1420", "alder", None, ALDER)
    assert valuation.parse_address("12 Unity Ave, Stevens Point, WI").unit is None        # a word, not a unit
    assert valuation.parse_address("4120 Birch Ave Unit 4120-301").unit == "4120-301"


def test_redfin_over_plain_https_keeps_the_estimate_and_the_home_facts(conn, free_sources, fake_http, fake_browser):
    rf = Redfin()
    fake_http.on("redfin", rf.http)
    m = valuation.create(conn, home(), TODAY)
    assert m["balance"] == 1014415.33 and m["value_source"] == "estimate"
    assert rf.asked == [f"autocomplete {ALDER}", "avm 5555501", "above 5555501"] and fake_browser.opened == []
    ua = [h["User-Agent"] for u, h in fake_http.calls if "redfin" in u]
    assert len(ua) == 3 and all(x.startswith("Mozilla/5.0 (Macintosh") for x in ua)
    est = m["valuation"]["estimate"]
    assert (est["source"], est["basis"], est["value"]) == ("redfin", "estimate", 1014415.33)
    assert est["details"] == {"property_id": "5555501", "name": "1420 Alder Ave", "sub_name": "Santa Rosa, CA, USA",
                              "url": "/CA/Santa-Rosa/1420-Alder-Ave/home/5555501",
                              "preview": "$1,014,415 (+$194K since last sold)", "beds": 3.0, "baths": 2.0,
                              "sqft": 1560.0, "last_sold": {"amount": 820000.0, "label": "Last Sold Price"},
                              "lat": 38.4411, "lng": -122.7193, "street_address": "1420 Alder Ave", "via": "https",
                              "photo_url": "https://ssl.cdn-redfin.com/photo/40/bigphoto/975/31415975_0.jpg"}
    assert calls(conn, "redfin") == 1
    assert conn.execute("SELECT outcome, note FROM api_calls WHERE provider = 'redfin'").fetchone()[:] == ("ok", "via https")
    assert m["estimate_result"]["message"] == "Estimated $1,014,415.33 by Redfin"


def test_redfin_refused_over_https_asks_from_a_redfin_tab_and_starts_there_next_time(conn, free_sources, fake_http,
                                                                                    fake_browser):
    rf = Redfin()
    fake_http.on("redfin", (403, b"<html><body>Access to this page has been denied.</body></html>"))
    fake_browser.on("GET", "www.redfin.com/stingray/", rf.tab)
    m = valuation.create(conn, home(), TODAY)
    assert m["balance"] == 1014415.33 and m["valuation"]["estimate"]["details"]["via"] == "chrome"
    assert fake_http.count("redfin") == 1                              # the refused plain request, then Chrome
    assert fake_browser.opened == ["https://www.redfin.com/"] and fake_browser.closed == fake_browser.opened
    assert [f["url"].split("?")[0] for f in fake_browser.fetches] == [
        "https://www.redfin.com/stingray/do/location-autocomplete", "https://www.redfin.com/stingray/api/home/details/avm",
        "https://www.redfin.com/stingray/api/home/details/aboveTheFold"]
    assert calls(conn, "redfin") == 1                                  # one lookup, counted once
    assert conn.execute("SELECT note FROM api_calls WHERE provider = 'redfin'").fetchone()[0] == \
        "via chrome (plain HTTPS got HTTP 403)"
    valuation.revalue(conn, day(30))                                   # the monthly one: straight to Chrome
    assert fake_http.count("redfin") == 1 and len(fake_browser.opened) == 2


def test_redfin_refused_with_no_chrome_to_fall_back_on_counts_but_does_not_block(conn, free_sources, fake_http,
                                                                                fake_browser):
    fake_http.on("redfin", (403, b"<html>denied</html>"))
    fake_browser.down = "Chrome not running (nothing answers on 127.0.0.1:9222)"
    r = valuation.create(conn, home(balance=900000), TODAY)["estimate_result"]
    assert r["called"] is True and r["blocked_until"] is None and r["reason"] == "http"
    assert r["message"] == ("Redfin refused the plain request (HTTP 403) and chrome not running (nothing answers on "
                            "127.0.0.1:9222); not retried")
    assert calls(conn, "redfin") == 1 and usage.blocked(conn, "redfin", TODAY) is None


def test_redfin_refused_both_ways_blocks_it_for_seven_days(conn, free_sources, fake_http, fake_browser):
    fake_http.on("redfin", (403, b"<html>denied</html>"))
    fake_browser.on("GET", "www.redfin.com/stingray/", (403, "<html>captcha-delivery</html>"))
    r = valuation.create(conn, home(balance=900000), TODAY)["estimate_result"]
    assert r["reason"] == "blocked" and r["blocked_until"] == day(7).isoformat()
    assert fake_browser.closed == fake_browser.opened and calls(conn, "redfin") == 1
    assert valuation.refresh(conn, 1, day(3))["estimate_result"]["reason"] == "blocked"


def test_estimate_not_available_is_a_clear_no_estimate_not_an_error(conn, free_sources, fake_http, fake_browser):
    rf = Redfin(avm={"5555501": NOT_AVAILABLE})
    fake_http.on("redfin", rf.http)
    m = valuation.create(conn, home(balance=900000), TODAY)
    r = m["estimate_result"]
    assert r["ok"] is False and r["reason"] == "no_estimate" and r["blocked_until"] is None
    assert r["message"] == "Redfin has no estimate for 1420 Alder Ave (Estimate Not Available)"
    assert usage.blocked(conn, "redfin", TODAY) is None
    assert conn.execute("SELECT outcome FROM api_calls WHERE provider = 'redfin'").fetchone()[0] == "none"
    assert m["valuation"]["sources"]["redfin"]["note"] == r["message"] and m["balance"] == 900000.0


def test_a_condo_unit_with_no_row_of_its_own_gets_no_estimate(conn, free_sources, fake_http, fake_browser):
    """2b: "#303" finds only the free-text row, the building search only the building and other units: no estimate,
    never the building's $1,031,933 nor unit 4120-301's $579,436."""
    rf = Redfin()
    fake_http.on("redfin", rf.http)
    address = "4120 Birch Ave #303, Santa Rosa, CA 95404"
    m = valuation.create(conn, home(address, balance=500000), TODAY)
    r = m["estimate_result"]
    assert rf.asked == [f"autocomplete {address}", "autocomplete 4120 Birch Ave, Santa Rosa, CA 95404"]   # no AVM asked
    assert r["reason"] == "no_estimate" and r["message"] == ("Redfin has no record for unit 303 (only the building or "
                                                              "other units): no estimate for this unit")
    assert conn.execute("SELECT COUNT(*) FROM asset_valuations").fetchone()[0] == 0
    assert m["balance"] == 500000.0 and calls(conn, "redfin") == 1


def test_a_unit_is_only_valued_from_its_own_row(conn, free_sources, fake_http, fake_browser):
    """Unit-mismatch rules: unit 301 takes the "#301" row (whose estimate isn't available), never "Unit 4120-301" nor
    the building; a unit with a row and an estimate of its own is valued."""
    rf = Redfin()
    fake_http.on("redfin", rf.http)
    r = valuation.create(conn, home("4120 Birch Ave, Unit 301, Santa Rosa, CA 95404", balance=500000), TODAY)
    assert rf.asked[-1] == "avm 201000301"
    assert r["estimate_result"]["message"] == "Redfin has no estimate for 4120 Birch Ave #301 (Estimate Not Available)"
    rows = BIRCH_BUILDING["sections"][0]["rows"]
    want = valuation.parse_address("4120 Birch Ave Apt 303")
    assert valuation.redfin_pick(rows, want) is None
    assert valuation.redfin_pick(rows, valuation.parse_address("4120 Birch Ave"))["id"] == "1_300004120"
    assert valuation.redfin_pick(rows, valuation.parse_address("4120 Birch Ave Unit 4120-301"))["id"] == "1_300004301"
    assert valuation.redfin_pick(rows, valuation.parse_address("4122 Birch Ave #301")) is None       # another number
    own = Redfin(homes={"4120 birch ave": {"sections": [{"rows": [row(300000305, "4120 Birch Ave #305")]}]}},
                 avm={"300000305": {"predictedValue": 612000.0, "numBeds": 2, "numBaths": 2, "sqFt": {"value": 1100}}})
    fake_http.on("redfin", own.http)
    m = valuation.create(conn, home("4120 Birch Ave Apt 305, Santa Rosa, CA 95404"), TODAY)
    assert m["balance"] == 612000.0 and m["valuation"]["estimate"]["details"]["property_id"] == "300000305"


def test_redfin_cap_is_twenty_a_month(conn, free_sources, fake_http, fake_browser):
    rf = Redfin()
    fake_http.on("redfin", rf.http)
    for n in range(20):
        valuation.create(conn, home(balance=1, name=f"H{n}"), TODAY)
    assert calls(conn, "redfin") == 20
    r = valuation.create(conn, home(balance=1, name="H21"), TODAY)["estimate_result"]
    assert r["reason"] == "cap" and r["message"].startswith("Redfin's monthly limit is reached (20 of 20")


# --- the page's data, the API, the CLI -------------------------------------------------------------------------------

@pytest.fixture
def app(conn):
    a = App(config.db_path(), lambda: None)
    a.today = lambda: TODAY
    return a


def call(app, method, path, body=None):
    status, _, raw = app.handle(method, path, POST if method == "POST" else LOCAL,
                                json.dumps(body or {}).encode() if method == "POST" else b"")
    return status, json.loads(raw)


def test_the_car_page_data(app, conn, free_sources, comet_http, fake_browser):
    loan = manual.create(conn, {"kind": "loan", "name": "Car Loan", "institution": "Zenith Finances", "subtype": "auto",
                                "interest_rate": 3.81, "original_amount": 30567, "balance": 5907.88,
                                "monthly_payment": 465.10}, TODAY)
    car = valuation.create(conn, {**COMET, "linked_loan_id": loan["id"]}, TODAY)
    status, d = call(app, "GET", f"/api/manual/{car['id']}/detail")
    assert status == 200
    assert d["title"] == "2020 Zenith Comet Prime Limited"
    assert d["value"]["amount"] == 14090.0 and d["value"]["line"] == f"Trade-in · Carfax · {valuation.short_day(car['valuation']['estimate']['date'])} · at 143,210 mi"
    assert d["value"]["first_check"] is True and d["value"]["change"] is None
    assert [(x["label"], x["source"], x["value"]) for x in d["table"]] == [    # Carfax's three, nothing else
        ("Trade-in", "Carfax", 14090.0), ("Private sale", "Carfax", 17160.0), ("Dealer retail", "Carfax", 19360.0)]
    assert d["sell"] == {"label": "Get a Carfax cash offer", "url": "https://www.carfax.com/value/"}
    ln = d["loan"]
    assert (ln["id"], ln["name"], ln["balance"], ln["monthly_payment"], ln["equity"]) == (
        loan["id"], "Car Loan", 5907.88, 465.1, round(14090 - 5907.88, 2))
    assert ln["loan_share"] == round(5907.88 / 14090, 4) and ln["payoff_month"] and d["linked_loan_id"] == loan["id"]
    # one engine row (Carfax's name), Carfax's drive and body, "Plug-in hybrid"; no unverified feature
    assert dict(d["specs"]) == {"Engine": "1.8L Plug-in Hybrid I4 121hp", "Transmission": "e-CVT", "Drive": "FWD",
                                "Fuel": "Plug-in hybrid", "Body": "Hatchback", "Doors": "5", "Seats": "5",
                                "Curb weight": "3,375 lb", "Wheelbase": "106.3 in", "Wheels": "15\"",
                                "Segment": "Compact car", "Built in": "Zenith City, Japan"}
    assert [x[0] for x in d["specs"]].count("Engine") == 1
    assert [(s["label"], s["value"]) for s in d["safety"]][:7] == [
        ("Adaptive cruise control", "Standard"), ("Lane keeping assist", "Standard"),
        ("Lane departure warning", "Standard"), ("Forward collision warning", "Standard"),
        ("Automatic emergency braking", "Standard"), ("Pedestrian emergency braking", "Standard"),
        ("Tire pressure monitoring", "Direct")]
    assert not [s for s in d["safety"] if "blind" in s["label"].lower()]    # NHTSA doesn't list it for this VIN
    assert d["safety_state"] == "fetched"
    assert d["vin_masked"] == "•" * 11 + "012345" and d["records"] == 25
    assert d["details"]["mileage"] == 143210 and d["details"]["condition"] == "good" and d["details"]["zip_code"] == "95404"
    assert d["details"]["next_check"] == day(30).isoformat() and len(d["history"]) == 1
    assert d["photo"] == {**d["photo"], "status": "pending", "kind": "silhouette", "url": None}
    assert call(app, "GET", "/api/manual/999/detail")[0] == 404
    assert call(app, "GET", f"/api/manual/{loan['id']}/detail")[0] == 400


def test_equity_uses_only_the_linked_loan(app, conn, free_sources, comet_http, fake_browser):
    car = valuation.create(conn, COMET, TODAY)
    one = manual.create(conn, {"kind": "loan", "name": "Car Loan", "institution": "TFS", "subtype": "auto",
                               "original_amount": 30000, "balance": 5907.88, "monthly_payment": 465.1}, TODAY)
    assert valuation.detail(conn, car["id"], TODAY)["loan"] is None          # never guessed: not linked, no equity
    manual.create(conn, {"kind": "loan", "name": "Other", "institution": "Bank", "subtype": "auto",
                         "original_amount": 9000, "balance": 2500, "monthly_payment": 300}, TODAY)
    status, e = call(app, "POST", f"/api/manual/{car['id']}", {"linked_loan_id": one["id"]})
    assert status == 200 and e["linked_loan_id"] == one["id"]
    d = valuation.detail(conn, car["id"], TODAY)
    assert d["loan"]["id"] == one["id"] and d["loan"]["equity"] == round(14090 - 5907.88, 2)
    assert call(app, "POST", f"/api/manual/{car['id']}", {"linked_loan_id": car["id"]}) == (
        400, {"error": "Loan must be one of your loans (or none)", "field": "linked_loan_id"})
    manual.archive(conn, one["id"], TODAY)
    assert valuation.detail(conn, car["id"], TODAY)["loan"] is None          # an archived loan no longer counts
    status, e = call(app, "POST", f"/api/manual/{car['id']}", {"linked_loan_id": None})
    assert status == 200 and e["linked_loan_id"] is None


def test_the_property_page_data(app, conn, free_sources, fake_http, fake_browser):
    fake_http.on("redfin", Redfin().http)
    status, m = call(app, "POST", "/api/manual", home())
    assert status == 200 and m["balance"] == 1014415.33
    status, d = call(app, "GET", f"/api/manual/{m['id']}/detail")
    assert status == 200 and d["details"]["unit"] is None
    assert d["table"] == [{"label": "Estimate", "source": "Redfin", "value": 1014415.33, "primary": True},
                          {"label": "Last sold price", "source": "Redfin", "value": 820000.0, "primary": False}]
    assert d["facts"] == {**d["facts"], "beds": 3.0, "baths": 2.0, "sqft": 1560.0}
    assert d["source_line"].startswith("Redfin ") and "RentCast" not in json.dumps(d)
    status, u = call(app, "POST", "/api/manual", home("4120 Birch Ave #303, Santa Rosa, CA 95404", balance=500000))
    d = valuation.detail(conn, u["id"], TODAY)
    assert [x["value"] for x in d["table"]] == [None] and d["notes"]["no_estimate"] == (
        "Redfin has no record for unit 303 (only the building or other units): no estimate for this unit")


def test_cli_condition_and_loan_flags(conn, capsys):
    loan = manual.create(conn, {"kind": "loan", "name": "L", "institution": "X", "subtype": "auto", "original_amount": 10,
                                "balance": 5, "monthly_payment": 1}, TODAY)
    code = cli.main(["manual", "add", "--kind", "vehicle", "--vehicle-make", "Zenith", "--vehicle-model", "Comet",
                     "--vehicle-year", "2020", "--balance", "14000", "--condition", "fair", "--loan", str(loan["id"]),
                     "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["vehicle_condition"] == "fair" and out["linked_loan_id"] == loan["id"]
    assert cli.main(["manual", "edit", str(out["id"]), "--clear", "linked_loan_id", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["linked_loan_id"] is None
    assert cli.main(["manual", "add", "--kind", "vehicle", "--vehicle-make", "T", "--vehicle-model", "P",
                     "--vehicle-year", "2020", "--balance", "1", "--condition", "poor"]) == 2
    assert cli.main(["manual", "add", "--kind", "vehicle", "--vehicle-make", "T", "--vehicle-model", "P",
                     "--vehicle-year", "2020", "--balance", "1", "--loan", "999"]) == 2


# --- the CDP client against a real (throwaway, headless) Chrome and a fake Carfax on loopback -------------------------

class FakeCarfax(BaseHTTPRequestHandler):
    """carfax.com/value (the page, on 127.0.0.1) and consumerapi (the API, on localhost: another origin, so CORS
    applies exactly as between www.carfax.com and consumerapi.carfax.com)."""
    seen: list = []
    blocked = False
    page_origin = ""

    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", self.page_origin)
        self.send_header("Access-Control-Allow-Credentials", "true")

    def _send(self, status, body, ctype="application/json", cors=True):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if cors:
            self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.seen.append(("OPTIONS", self.path, None))
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.end_headers()

    def do_GET(self):
        self.seen.append(("GET", self.path, None))
        if self.path == "/value/":
            return self._send(200, "<!doctype html><title>value</title><p>fake carfax.com/value</p>", "text/html", False)
        if self.path.startswith("/hbv/cow/trims/"):
            if self.blocked:   # DataDome: a 403 captcha page without CORS headers (the page's fetch() can't read it)
                return self._send(403, CARFAX_CAPTCHA[1], "text/html", cors=False)
            return self._send(200, json.dumps(CARFAX_TRIMS))
        self._send(404, "{}")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        self.seen.append(("POST", self.path, body))
        self._send(200, json.dumps(CARFAX_PRICES))


@pytest.fixture
def headless(monkeypatch):
    from cdp import Chrome, find_chrome
    if find_chrome() is None:
        pytest.skip("Chrome is not installed")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeCarfax)
    port = srv.server_address[1]
    FakeCarfax.seen, FakeCarfax.blocked, FakeCarfax.page_origin = [], False, f"http://127.0.0.1:{port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(valuation, "CARFAX_PAGE", f"http://127.0.0.1:{port}/value/")
    monkeypatch.setattr(valuation, "CARFAX_TRIMS_URL", f"http://localhost:{port}/hbv/cow/trims/{{vin}}")
    monkeypatch.setattr(valuation, "CARFAX_PRICES_URL", f"http://localhost:{port}/hbv/cow/prices")
    with Chrome() as b:
        devtools = int((Path(b.profile) / "DevToolsActivePort").read_text().split()[0])
        monkeypatch.setattr(valuation, "browser", cdp_client.Chrome(port=devtools))
        yield b, devtools
    srv.shutdown()
    srv.server_close()


def targets(port):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/json/list")
    out = [(t["id"], t["type"], t["url"]) for t in json.loads(c.getresponse().read()) if t["type"] == "page"]
    c.close()
    return out


def test_a_real_chrome_runs_the_lookup_in_a_new_tab_and_closes_it(conn, free_sources, headless):
    b, port = headless
    before = targets(port)
    car = manual.create(conn, {**COMET, "vehicle_make": "Zenith", "vehicle_model": "Comet Prime", "vehicle_year": 2020},
                        TODAY, estimate=True)
    r = valuation.refresh(conn, car["id"], TODAY)
    assert r["estimate_result"]["ok"] is True and r["balance"] == 14090.0
    assert [(m, p) for m, p, _ in FakeCarfax.seen if p != "/favicon.ico"] == [
        ("GET", "/value/"), ("GET", f"/hbv/cow/trims/{CARFAX_VIN}"), ("OPTIONS", "/hbv/cow/prices"),
        ("POST", "/hbv/cow/prices")]
    assert json.loads(FakeCarfax.seen[-1][2]) == {**RECORDED_BODY, "condition": "good"}
    assert targets(port) == before                                     # the tab it opened is gone, the other untouched
    assert calls(conn, "carfax") == 1


def test_a_real_chrome_reads_a_cors_blocked_403_as_a_block(conn, free_sources, headless):
    b, port = headless
    FakeCarfax.blocked = True
    before = targets(port)
    car = manual.create(conn, {**COMET, "vehicle_make": "Zenith", "vehicle_model": "Comet Prime", "vehicle_year": 2020,
                               "balance": 14090}, TODAY)
    r = valuation.refresh(conn, car["id"], TODAY)["estimate_result"]
    assert r["reason"] == "blocked" and r["blocked_until"] == day(7).isoformat()
    # the page's fetch() can't read a 403 without CORS headers (it fails as a TypeError); the tab's network log can
    assert r["message"].split(". Paused")[0] in ("Carfax blocked the check (trims: a captcha page)",
                                                 "Carfax blocked the check (trims: HTTP 403)")
    assert targets(port) == before


def test_the_devtools_client_says_when_chrome_is_not_there(monkeypatch):
    with socket.socket() as s:                                         # a loopback port nothing listens on
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    c = cdp_client.Chrome(port=free)
    assert c.available() == f"Chrome not running (nothing answers on 127.0.0.1:{free})"
    with pytest.raises(cdp_client.CDPError, match="Chrome not running"):
        with c.tab("http://127.0.0.1/"):
            pass
    # switched off (as in every test): no connection is even attempted
    monkeypatch.setenv("HERMES_CHROME_DEVTOOLS", "off")
    monkeypatch.setattr(socket.socket, "connect", lambda *a: (_ for _ in ()).throw(AssertionError("connected")))
    off = cdp_client.Chrome()
    assert off.endpoint() is None and off.available() == "Chrome not running here (HERMES_CHROME_DEVTOOLS=off)"
    with pytest.raises(cdp_client.CDPError):
        with off.tab("https://www.carfax.com/value/"):
            pass
    monkeypatch.setenv("HERMES_CHROME_DEVTOOLS", "127.0.0.1:9333")
    assert cdp_client.Chrome().endpoint() == ("127.0.0.1", 9333)


def test_no_test_can_reach_the_real_chrome():
    """How "no real Carfax / Redfin call, no real Chrome" is enforced (conftest): the browser is an offline stub, the
    DevTools client is switched off for this process and the ones it starts, and a socket to port 9222 is refused."""
    assert isinstance(valuation.browser, OfflineBrowser)
    assert valuation.browser.available().startswith("Chrome not running")
    with pytest.raises(AssertionError, match="real Chrome tab"):
        valuation.browser.tab("https://www.carfax.com/value/")
    assert os.environ["HERMES_CHROME_DEVTOOLS"] == "off" and cdp_client.Chrome().endpoint() is None
    for host in ("127.0.0.1", "localhost"):
        with socket.socket() as s, pytest.raises(AssertionError, match="real Chrome"):
            s.connect((host, 9222))
    with pytest.raises(AssertionError, match="real network lookup"):
        socket.getaddrinfo("consumerapi.carfax.com", 443)
    with pytest.raises(AssertionError, match="real network call attempted"):
        valuation.transport("https://www.redfin.com/stingray/do/location-autocomplete?location=x&v=2", {}, 1.0)
    assert config.free_sources() == {"carfax": False, "redfin": False, "photos": False}   # on where a test turns them on


# --- schema v8 ------------------------------------------------------------------------------------------------------

V7_SQL = (Path(__file__).parent / "fixtures" / "schema_v7.sql").read_text()


def test_upgrade_from_v7_rebuilds_asset_valuations_and_keeps_every_row(data_dir):
    path = config.db_path()
    config.ensure_data_dir(path.parent)
    raw = sqlite3.connect(path)
    raw.executescript(V7_SQL)
    ts = "2026-09-27T03:16:40Z"
    raw.execute("INSERT INTO manual_accounts (kind, name, balance, vehicle_make, vehicle_model, vehicle_year, balance_as_of, "
                "created_at, updated_at, vin, mileage, zip_code, value_source) VALUES ('vehicle', '2020 Zenith Comet Prime', "
                "14090, 'Zenith', 'Comet Prime', 2020, '2026-09-26', ?, ?, ?, 143210, '95404', 'user')", (ts, ts, CARFAX_VIN))
    raw.execute("INSERT INTO manual_accounts (kind, name, balance, balance_as_of, created_at, updated_at, address, "
                "value_source) VALUES ('property', 'Condo', 1302000, '2026-09-26', ?, ?, ?, 'estimate')", (ts, ts, "4120 Birch Ave, Unit 303, Santa Rosa, CA 95404"))
    raw.execute("INSERT INTO asset_valuations (account_id, value, source, miles, subject, fetched_at, raw_json) VALUES "
                "(1, 20198, 'marketcheck', 108000, ?, ?, '{\"marketcheck_price\": 20198, \"msrp\": 35014}')", (CARFAX_VIN, ts))
    raw.execute("INSERT INTO asset_valuations (account_id, value, low, high, source, subject, fetched_at, raw_json) VALUES "
                "(2, 1302000, 1028000, 1575000, 'rentcast', '4120 birch ave, unit 303, santa rosa, ca 95404', ?, '{}')", (ts,))
    raw.execute("INSERT INTO vin_decodes VALUES (?, 2020, 'Zenith', 'Comet Prime (PHEV)', 'CMT2L', ?)", (CARFAX_VIN, ts))
    raw.commit()
    before = [tuple(r) for r in raw.execute("SELECT * FROM asset_valuations ORDER BY id")]
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 7
    with pytest.raises(sqlite3.IntegrityError):                      # v7 can't hold a Carfax value
        raw.execute("INSERT INTO asset_valuations (account_id, value, source, fetched_at) VALUES (1, 5, 'carfax', 'x')")
    raw.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
    after = [tuple(r)[:10] for r in conn.execute("SELECT * FROM asset_valuations ORDER BY id")]
    assert after == before                                             # every row, every value
    assert [tuple(r)[10:] for r in conn.execute("SELECT * FROM asset_valuations")] == [(None, None, None)] * 2
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(db.SCHEMA)
    for table in ("asset_valuations", "manual_accounts", "vin_decodes"):
        assert [r[1:3] for r in conn.execute(f"PRAGMA table_info({table})")] == \
            [r[1:3] for r in fresh.execute(f"PRAGMA table_info({table})")]
    assert conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'asset_valuations'"
                        ).fetchall()[0][0] == "idx_asset_valuations_account"
    assert [tuple(r) for r in conn.execute("SELECT kind, vehicle_condition FROM manual_accounts ORDER BY id")] == \
        [("vehicle", "good"), ("property", None)]
    assert conn.execute("SELECT specs_json FROM vin_decodes").fetchone()[0] is None   # decoded again once, for specs
    with conn:
        conn.execute("INSERT INTO asset_valuations (account_id, value, source, fetched_at, details_json) "
                     "VALUES (1, 14090, 'carfax', 'x', '{}')")
        conn.execute("INSERT INTO asset_valuations (account_id, value, source, fetched_at, flag) "
                     "VALUES (2, 1, 'redfin', 'x', NULL)")
    for bad in ("INSERT INTO asset_valuations (account_id, value, source, fetched_at) VALUES (1, 5, 'kbb', 'x')",
                "INSERT INTO asset_valuations (account_id, value, source, fetched_at, flag) VALUES (1, 5, 'rentcast', 'x', 'odd')",
                "UPDATE manual_accounts SET vehicle_condition = 'poor' WHERE id = 1"):
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(bad)
    snapshot = [tuple(r) for r in conn.execute("SELECT * FROM asset_valuations ORDER BY id")]
    conn.close()
    again = db.connect(path)                                           # idempotent
    assert [tuple(r) for r in again.execute("SELECT * FROM asset_valuations ORDER BY id")] == snapshot
    assert again.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'asset_valuations_v8'").fetchone()[0] == 0
    again.close()


def test_a_v7_decode_is_redone_once_for_the_specs(conn, free_sources, comet_http, fake_browser):
    m = manual.create(conn, {**COMET, "vehicle_make": "Zenith", "vehicle_model": "Comet Prime", "vehicle_year": 2020,
                             "balance": 14090}, TODAY)
    with conn:
        conn.execute("INSERT INTO vin_decodes VALUES (?, 2020, 'Zenith', 'Comet Prime (PHEV)', 'CMT2L', 'x', NULL)",
                     (CARFAX_VIN,))
        conn.execute("UPDATE manual_accounts SET vin_decoded = ? WHERE id = ?", (CARFAX_VIN, m["id"]))
    before = valuation.detail(conn, m["id"], TODAY)
    assert before["safety"] == [] and before["safety_state"] == "not_fetched"   # not "NHTSA lists none" (2026-09-27)
    valuation.revalue(conn, TODAY, m["id"])
    assert comet_http.count("nhtsa") == 1 and valuation.vin_specs(conn, CARFAX_VIN)["safety"]["CIB"] == "Standard"
    assert valuation.detail(conn, m["id"], TODAY)["safety_state"] == "fetched"
    valuation.revalue(conn, day(30), m["id"])
    assert comet_http.count("nhtsa") == 1                              # once

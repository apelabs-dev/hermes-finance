"""TASK-12's guarantees after TASK-13b removed MarketCheck and RentCast: VINs, the NHTSA decode, the value sources
(Carfax for a car, Redfin for a home), "Your value" always wins, the monthly cadence, and the call guard (counted before
it's sent, capped, never retried; a refusal stops the provider, and the stop survives a restart) for Carfax, Redfin and
NHTSA. Plus the removal itself: no code path can build a MarketCheck or RentCast request.

Mocked only (conftest): FakeHttp for NHTSA / Redfin, FakeBrowser for Carfax; real network, the real Chrome and the real
~/.hermes/.env are out of reach."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from conftest import (CARFAX_CAPTCHA, CARFAX_PRICES, ALDER, NHTSA_OK, VIN, FakeHttp, Redfin)
from hermes_finance import cdp_client, cli, config, db, manual, reads, usage, valuation
from hermes_finance.web import App

TODAY = date(2026, 11, 2)
LOCAL = {"Host": "127.0.0.1:8765"}
POST = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
VIN2 = "1M8GDM9AXKP042788"         # another valid VIN (its check digit is X)
CAR = {"kind": "vehicle", "vin": VIN, "mileage": 120000, "zip_code": "94110"}
HOME = {"kind": "property", "name": "Home", "address": ALDER}
SRC = Path(valuation.__file__).parent


def calls(conn, provider):
    r = conn.execute("SELECT calls FROM api_usage WHERE provider = ? AND month = ?", (provider, TODAY.isoformat()[:7]))
    row = r.fetchone()
    return row[0] if row else 0


def row(conn, account_id):
    return conn.execute("SELECT * FROM manual_accounts WHERE id = ?", (account_id,)).fetchone()


def day(n):
    return TODAY + timedelta(days=n)


def set_env(path: Path, **values):
    text = path.read_text() if path.exists() else ""
    path.write_text(text + "".join(f"{k}={v}\n" for k, v in values.items()))


@pytest.fixture
def on(free_sources, fake_http, fake_browser):
    """Carfax (a FakeBrowser answering the recorded shapes), Redfin (conftest.Redfin over HTTPS) and NHTSA (FakeHttp)."""
    rf = Redfin()
    fake_http.on("redfin", rf.http)
    return fake_http, fake_browser, rf


def prices(value):
    return (200, {**CARFAX_PRICES, "vehiclePrice": {"consumerRetailPrice": value + 5000, "fsboPrice": value + 3000,
                                                    "wholesalePrice": value}})


def set_price(fake_browser, value):
    fake_browser.on("POST", "consumerapi.carfax.com/hbv/cow/prices", prices(value))


# --- VIN ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("vin", [VIN, VIN2, "5YJ3E1EA6KF317002", "JTMBFREVXKD012345"])
def test_valid_vins_pass_the_check_digit(vin):
    assert valuation.vin_problem(vin) is None


@pytest.mark.parametrize("vin, needle", [
    ("1HGCM82643A004352", "check digit"),        # one digit off (the 9th should be 3)
    ("1HGCM82633A00435", "17 characters"),
    ("1HGCM82633A0043521", "17 characters"),
    ("1HGCM82633AO04352", "I, O or Q"),          # the letter O
    ("1HGCM8263#A004352", "only letters and digits"),
])
def test_bad_vins_are_explained(vin, needle):
    assert needle in valuation.vin_problem(vin)


def test_a_vin_is_normalised_and_its_check_digit_validated_on_save(conn):
    clean = manual.validate("vehicle", {"vin": " 1hgcm 82633a004352 "}, TODAY, partial=True)
    assert clean["vin"] == VIN
    with pytest.raises(manual.ManualError, match="check digit") as e:
        manual.validate("vehicle", {"vin": "1HGCM82643A004352"}, TODAY, partial=True)
    assert e.value.field == "vin"


def test_bad_vin_check_digit_is_refused_before_any_call(conn, on):
    fake_http, browser, _ = on
    with pytest.raises(manual.ManualError, match="check digit") as e:
        valuation.create(conn, {**CAR, "vin": "1HGCM82643A004352"}, TODAY)
    assert e.value.field == "vin"
    assert fake_http.calls == [] and browser.opened == []
    assert conn.execute("SELECT COUNT(*) FROM manual_accounts").fetchone()[0] == 0
    assert calls(conn, "nhtsa") == calls(conn, "carfax") == 0


def test_js_vin_check_matches_the_server(tmp_path):
    """app.js's vinProblem() gives the same answer (and words) as valuation.vin_problem() for the same VINs."""
    import shutil
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    js = (SRC / "static" / "app.js").read_text()
    start = js.index("  const VIN_VALUES =")
    end = js.index("  // Validate one field like the server does")
    vins = [VIN, VIN2, "1HGCM82643A004352", "1HGCM82633A00435", "1HGCM82633AO04352", "1HGCM8263#A004352",
            "JTMBFREVXKD012345", "JTMBFREV0KD012345"]
    prog = js[start:end] + f"\nconsole.log(JSON.stringify({json.dumps(vins)}.map(vinProblem)));"
    out = json.loads(subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=60, check=True).stdout)
    assert out == [valuation.vin_problem(v) for v in vins]


# --- saving a vehicle: decode, estimate -------------------------------------------------------------------------

def test_save_decodes_the_vin_prefills_and_fetches_the_carfax_trade_in(conn, on):
    fake_http, browser, _ = on
    m = valuation.create(conn, CAR, TODAY)
    assert (m["vehicle_year"], m["vehicle_make"], m["vehicle_model"], m["vehicle_trim"]) == (2003, "Honda", "Accord", "EX-V6")
    assert m["name"] == "2003 Honda Accord"
    assert m["decoded"] == {"vehicle_year": 2003, "vehicle_make": "Honda", "vehicle_model": "Accord", "vehicle_trim": "EX-V6"}
    # one decode, then one Carfax lookup (in a tab); nothing else is asked
    assert [u.split("?")[0] for u, _ in fake_http.calls] == [
        f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValuesExtended/{VIN}"]
    assert fake_http.params(0) == {"format": "json"}
    assert browser.opened == browser.closed == ["https://www.carfax.com/value/"]
    assert m["value_source"] == "estimate" and m["balance"] == 14090.0 and m["signed"] == 14090.0
    v = m["valuation"]
    assert v["source"] == "estimate" and v["estimate"]["value"] == 14090.0 and v["estimate"]["source_label"] == "Carfax"
    assert v["estimate"]["basis"] == "trade_in" and v["estimate"]["miles"] == 120000
    assert m["estimate_result"]["ok"] and m["estimate_result"]["applied"]
    assert manual.history(conn, m["id"])[-1] == {"date": TODAY.isoformat(), "balance": 14090.0}
    assert row(conn, m["id"])["vin_decoded"] == VIN
    assert calls(conn, "nhtsa") == 1 and calls(conn, "carfax") == 1
    assert conn.execute("SELECT account_id FROM api_calls WHERE provider = 'nhtsa'").fetchone()[0] == m["id"]


def test_typed_fields_win_over_the_decode(conn, on):
    m = valuation.create(conn, {**CAR, "vehicle_make": "Honda Motor", "vehicle_model": "Accord Coupe",
                                "vehicle_year": 2004, "balance": 6100.0}, TODAY)
    assert (m["vehicle_year"], m["vehicle_make"], m["vehicle_model"], m["vehicle_trim"]) == (2004, "Honda Motor",
                                                                                           "Accord Coupe", "EX-V6")
    assert m["value_source"] == "user" and m["balance"] == 6100.0       # typed: "Your value", the estimate is beside it
    assert m["valuation"]["estimate"]["value"] == 14090.0


def test_a_vin_nhtsa_cant_read_needs_the_fields_typed(conn, on):
    fake_http, browser, _ = on
    fake_http.on("nhtsa", (200, {"Count": 1, "Results": [{"ModelYear": "", "Make": "", "Model": "",
                                                          "ErrorText": "11 - Incorrect Model Year"}]}))
    with pytest.raises(manual.ManualError, match="NHTSA couldn't read this VIN.*Enter the make, model and year"):
        valuation.create(conn, CAR, TODAY)
    assert conn.execute("SELECT COUNT(*) FROM manual_accounts").fetchone()[0] == 0 and browser.opened == []
    m = valuation.create(conn, {**CAR, "vehicle_make": "Honda", "vehicle_model": "Accord", "vehicle_year": 2003,
                                "balance": 6000}, TODAY)
    assert m["decode_problem"].startswith("NHTSA couldn't read this VIN") and m["vehicle_make"] == "Honda"


def test_everything_else_is_validated_before_any_lookup(conn, on):
    fake_http, browser, _ = on
    with pytest.raises(manual.ManualError, match="ZIP code must be 5 digits"):
        valuation.create(conn, {**CAR, "zip_code": "9411"}, TODAY)
    with pytest.raises(manual.ManualError, match="Mileage must be a whole number"):
        valuation.create(conn, {**CAR, "mileage": 12.5}, TODAY)
    with pytest.raises(manual.ManualError, match="Your value is required"):
        valuation.create(conn, {"kind": "vehicle", "vehicle_make": "Honda", "vehicle_model": "Accord",
                                "vehicle_year": 2003}, TODAY)
    assert fake_http.calls == [] and browser.opened == []


def test_without_the_mileage_or_zip_the_value_is_needed_and_nothing_is_priced(conn, on):
    fake_http, browser, _ = on
    with pytest.raises(manual.ManualError, match="VIN, mileage and ZIP code to use the Carfax estimate"):
        valuation.create(conn, {"kind": "vehicle", "vin": VIN}, TODAY)
    m = valuation.create(conn, {"kind": "vehicle", "vin": VIN, "balance": 6000}, TODAY)
    assert browser.opened == [] and m["estimate_result"] is None
    assert m["valuation"]["missing"] == ["Mileage", "ZIP code"]
    assert valuation._needs(["VIN", "Mileage", "ZIP code"]) == "VIN, mileage and ZIP code"
    assert m["valuation"]["refresh_note"] == "Add the mileage and ZIP code to get an estimate"


def test_a_source_switched_off_is_simply_off(conn, fake_http, fake_browser, data_dir, tmp_path):
    set_env(tmp_path / "hermes.env", CARFAX_ENABLED="0", REDFIN_ENABLED="off")
    with pytest.raises(manual.ManualError) as e:                         # no estimate to fall back on
        valuation.create(conn, CAR, TODAY)
    assert str(e.value) == "Your value is required" and e.value.field == "balance"
    m = valuation.create(conn, {**CAR, "balance": 6000}, TODAY)
    assert m["value_source"] == "user" and m["estimate_result"] is None and fake_browser.opened == []
    assert fake_http.count("nhtsa") == 1                                 # the free decode still fills the car
    v = m["valuation"]
    assert v["enabled"] is False and v["can_refresh"] is False
    assert v["note"] == v["refresh_note"] == "Carfax is switched off (CARFAX_ENABLED=0 in the .env)"
    r = valuation.refresh(conn, m["id"], TODAY)["estimate_result"]
    assert r == {**r, "ok": False, "called": False, "reason": "off"}
    assert valuation.revalue(conn, TODAY)["results"][0]["action"] == "skipped"
    home = valuation.create(conn, {**HOME, "balance": 250000}, TODAY)
    assert home["valuation"]["note"] == "Redfin is switched off (REDFIN_ENABLED=0 in the .env)"
    app = App(config.db_path(), lambda: None)
    app.today = lambda: TODAY
    status, _, body = app.handle("GET", "/api/accounts", LOCAL, b"")
    p = json.loads(body)["valuation"]
    assert status == 200 and set(p) == {"carfax", "redfin"}
    assert p["carfax"]["enabled"] is False and p["redfin"]["enabled"] is False


# --- NHTSA refusals, timeouts ------------------------------------------------------------------------------------

def test_an_nhtsa_refusal_is_counted_explained_and_stops_it_for_the_month(conn, on):
    fake_http, _, _ = on
    fake_http.on("nhtsa", (403, {"Message": "Access denied"}))
    m = valuation.create(conn, {**CAR, "vehicle_make": "Honda", "vehicle_model": "Accord", "vehicle_year": 2003}, TODAY)
    assert m["decode_problem"] == "NHTSA refused the request (HTTP 403): Access denied. Paused until 2026-12-01"
    assert fake_http.count("nhtsa") == 1 and calls(conn, "nhtsa") == 1
    # never retried: not by another car's save, not by the daily run, until next month
    other = valuation.create(conn, {**CAR, "vin": VIN2, "vehicle_make": "Mack", "vehicle_model": "GR", "vehicle_year": 2019,
                                    "balance": 9000}, day(2))
    assert other["decode_problem"].startswith("NHTSA is paused until 2026-12-01")
    valuation.revalue(conn, day(40 - 12))
    assert fake_http.count("nhtsa") == 1
    fake_http.on("nhtsa", (200, NHTSA_OK))
    assert valuation.decode_vin(conn, "5YJ3E1EA6KF317002", date(2026, 12, 1), None, "save")["vehicle_make"] == "Honda"


def test_a_plain_429_counts_is_not_retried_and_does_not_block(conn, on):
    fake_http, _, _ = on
    fake_http.on("nhtsa", (429, {"Message": "Too many requests"}))
    m = valuation.create(conn, {**CAR, "vehicle_make": "Honda", "vehicle_model": "Accord", "vehicle_year": 2003}, TODAY)
    assert m["decode_problem"] == "NHTSA is rate-limiting (HTTP 429): Too many requests; not retried"
    assert usage.blocked(conn, "nhtsa", TODAY) is None and calls(conn, "nhtsa") == 1


def test_a_429_that_says_quota_stops_nhtsa_for_the_month(conn, on):
    fake_http, _, _ = on
    fake_http.on("nhtsa", (429, {"Message": "Daily quota exceeded"}))
    m = valuation.create(conn, {**CAR, "vehicle_make": "Honda", "vehicle_model": "Accord", "vehicle_year": 2003}, TODAY)
    assert "quota is used up" in m["decode_problem"] and usage.blocked(conn, "nhtsa", TODAY)["blocked_until"] == "2026-12-01"


@pytest.mark.parametrize("error, needle", [(TimeoutError("timed out"), "timed out"),
                                           (OSError("connection refused"), "unreachable (OSError)")])
def test_timeouts_and_network_errors_count_and_are_not_retried(conn, on, error, needle):
    fake_http, _, _ = on
    fake_http.on("nhtsa", error)
    m = valuation.create(conn, {**CAR, "vehicle_make": "Honda", "vehicle_model": "Accord", "vehicle_year": 2003}, TODAY)
    assert needle in m["decode_problem"] and calls(conn, "nhtsa") == 1
    assert conn.execute("SELECT outcome FROM api_calls WHERE provider = 'nhtsa'").fetchone()[0] == \
        ("timeout" if isinstance(error, TimeoutError) else "error")


# --- manual override, cadence ------------------------------------------------------------------------------------

def test_a_typed_value_always_wins_until_use_estimate_again(conn, on):
    _, browser, _ = on
    m = valuation.create(conn, CAR, TODAY)
    assert m["balance"] == 14090.0 and m["value_source"] == "estimate"
    m = valuation.edit(conn, m["id"], {"balance": 15000}, TODAY)         # typed: "Your value"
    assert m["balance"] == 15000.0 and m["value_source"] == "user" and m["estimate_result"] is None
    set_price(browser, 13900)
    r = valuation.refresh(conn, m["id"], day(1))
    assert r["estimate_result"]["ok"] and r["estimate_result"]["applied"] is False
    assert r["balance"] == 15000.0 and r["value_source"] == "user"      # a new estimate never overrides your value
    assert r["valuation"]["estimate"]["value"] == 13900.0
    assert valuation.revalue(conn, day(31))["results"][0]["applied"] is False
    assert row(conn, m["id"])["balance"] == 15000.0
    u = valuation.use_estimate(conn, m["id"], day(31))
    assert u["balance"] == 13900.0 and u["value_source"] == "estimate" and u["balance_as_of"] == day(31).isoformat()
    e = valuation.edit(conn, m["id"], {"name": "The Accord"}, day(32))   # a rename is not a new value
    assert e["value_source"] == "estimate" and e["balance"] == 13900.0


def test_use_estimate_needs_an_estimate_and_a_vehicle_or_property(conn, on):
    m = valuation.create(conn, {"kind": "vehicle", "vehicle_make": "Honda", "vehicle_model": "Fit", "vehicle_year": 2015,
                                "balance": 4000}, TODAY)
    with pytest.raises(manual.ManualError, match="No estimate") as e:
        valuation.use_estimate(conn, m["id"], TODAY)
    assert e.value.status == 409
    loan = manual.create(conn, {"kind": "loan", "name": "L", "institution": "X", "subtype": "auto", "original_amount": 10,
                                "balance": 5, "monthly_payment": 1}, TODAY)
    with pytest.raises(manual.ManualError, match="only vehicles and properties"):
        valuation.use_estimate(conn, loan["id"], TODAY)
    with pytest.raises(manual.ManualError, match="only vehicles and properties"):
        valuation.refresh(conn, loan["id"], TODAY)


def test_the_daily_run_asks_once_a_month_per_account(conn, on):
    fake_http, browser, rf = on
    car = valuation.create(conn, CAR, TODAY)
    home = valuation.create(conn, HOME, TODAY)
    for n in (0, 1, 29):
        for x in valuation.revalue(conn, day(n))["results"]:
            assert x["action"] == "skipped" and "next monthly check on 2026-12-02" in x["message"]
    set_price(browser, 13600)
    first = valuation.revalue(conn, day(30))
    again = valuation.revalue(conn, day(30))
    assert [x["action"] for x in first["results"]] == ["valued", "valued"] and first["valued"] == 2
    assert [x["action"] for x in again["results"]] == ["skipped", "skipped"]
    assert len(browser.opened) == 2 and fake_http.count("nhtsa") == 1      # the VIN was decoded once
    assert row(conn, car["id"])["balance"] == 13600.0 and row(conn, home["id"])["balance"] == 1014415.33


def test_the_daily_run_decodes_a_vin_it_never_could(conn, on):
    fake_http, _, _ = on
    fake_http.on("nhtsa", TimeoutError())
    m = valuation.create(conn, {**CAR, "vehicle_make": "Honda", "vehicle_model": "Accord", "vehicle_year": 2003}, TODAY)
    assert m["vehicle_trim"] is None and row(conn, m["id"])["vin_decoded"] is None
    fake_http.on("nhtsa", (200, NHTSA_OK))
    valuation.revalue(conn, day(30))
    assert row(conn, m["id"])["vehicle_trim"] == "EX-V6" and row(conn, m["id"])["vin_decoded"] == VIN
    assert fake_http.count("nhtsa") == 2


def test_refresh_is_limited_to_one_per_account_per_day(conn, on):
    _, browser, rf = on
    m = valuation.create(conn, CAR, TODAY)                               # the save's call is today's
    r = valuation.refresh(conn, m["id"], TODAY)["estimate_result"]
    assert r["called"] is False and r["reason"] == "today" and "again tomorrow" in r["message"]
    assert valuation.refresh(conn, m["id"], day(1))["estimate_result"]["ok"]
    assert valuation.refresh(conn, m["id"], day(1))["estimate_result"]["reason"] == "today"
    assert len(browser.opened) == 2 and calls(conn, "carfax") == 2
    home = valuation.create(conn, HOME, day(1))                          # each account has its own day
    assert home["estimate_result"]["ok"] and calls(conn, "redfin") == 1
    assert valuation.refresh(conn, home["id"], day(1))["estimate_result"]["reason"] == "today"


def test_editing_what_carfax_looks_at_revalues_once_a_day(conn, on):
    _, browser, _ = on
    m = valuation.create(conn, CAR, TODAY)
    e = valuation.edit(conn, m["id"], {"mileage": 125000}, TODAY)        # same day: the save already asked
    assert e["estimate_result"]["reason"] == "today" and e["mileage"] == 125000
    e = valuation.edit(conn, m["id"], {"mileage": 126000}, day(1))
    assert e["estimate_result"]["ok"] and json.loads(browser.fetches[-1]["body"])["odometer"] == 126000
    e = valuation.edit(conn, m["id"], {"name": "Accord"}, day(2))
    assert e["estimate_result"] is None                                  # nothing Carfax sees changed


def test_a_new_vin_is_decoded_and_replaces_the_old_car(conn, on):
    fake_http, _, _ = on
    m = valuation.create(conn, CAR, TODAY)
    fake_http.on("nhtsa", (200, {"Results": [{"ModelYear": "2019", "Make": "ZENITH", "Model": "Mesa", "Trim": "LE"}]}))
    e = valuation.edit(conn, m["id"], {"vin": VIN2}, day(3))
    assert (e["vehicle_year"], e["vehicle_make"], e["vehicle_model"], e["vehicle_trim"]) == (2019, "Zenith", "Mesa", "LE")
    assert e["name"] == "2019 Zenith Mesa" and e["estimate_result"]["ok"] and row(conn, m["id"])["vin_decoded"] == VIN2
    e = valuation.edit(conn, m["id"], {"vehicle_model": "", "vehicle_make": "Zenith Motor"}, day(4))
    assert e["vehicle_model"] == "Mesa" and e["vehicle_make"] == "Zenith Motor"
    decodes = fake_http.count("nhtsa")
    e = valuation.edit(conn, m["id"], {"vin": VIN}, day(4))              # once per VIN: the kept decode, no call
    assert e["vehicle_make"] == "Honda" and fake_http.count("nhtsa") == decodes


# --- the guard: Carfax, Redfin, NHTSA ----------------------------------------------------------------------------

def test_the_cap_is_enforced_at_exactly_n(conn, on, monkeypatch):
    _, browser, _ = on
    monkeypatch.setitem(config.API_CAPS, "carfax", 3)
    for v in (VIN, VIN2, "5YJ3E1EA6KF317002"):
        valuation.create(conn, {**CAR, "vin": v}, TODAY)
    assert calls(conn, "carfax") == 3 and len(browser.opened) == 3
    with pytest.raises(manual.ManualError, match="Your value is required for now: Carfax's monthly limit") as e:
        valuation.create(conn, {**CAR, "vin": "JTMBFREVXKD012345"}, TODAY)   # nothing to fall back on: not a $0 car
    assert e.value.field == "balance"
    fourth = valuation.create(conn, {**CAR, "vin": "JTMBFREVXKD012345", "balance": 8000}, TODAY)["estimate_result"]
    assert fourth["called"] is False and fourth["reason"] == "cap"
    assert fourth["message"] == "Carfax's monthly limit is reached (3 of 3 calls this month); it resumes on 2026-12-01"
    assert len(browser.opened) == 3 and calls(conn, "carfax") == 3
    with pytest.raises(usage.Refused) as r:                             # the rolling window: December starts at 0 in
        usage.reserve(conn, "carfax", date(2026, 12, 1))                 # api_usage, but those 3 were 29 days ago
    assert r.value.reason == "window"
    assert usage.reserve(conn, "carfax", day(32)).calls == 1             # 32 days on, they've aged out


def test_concurrent_callers_cannot_go_past_the_cap(conn, monkeypatch):
    monkeypatch.setitem(config.API_CAPS, "redfin", 5)
    path = config.db_path()
    start = threading.Barrier(16)
    won, refused = [], []

    def caller():
        c = db.connect(path)
        try:
            start.wait()
            won.append(usage.reserve(c, "redfin", TODAY).call_id)
        except usage.Refused as e:
            refused.append(e.reason)
        finally:
            c.close()

    threads = [threading.Thread(target=caller) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 5 and refused == ["cap"] * 11
    assert calls(conn, "redfin") == 5 and conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0] == 5


def test_concurrent_processes_cannot_go_past_the_cap(conn):
    """Separate processes (the daily run and the dashboard) on the same DB file, at Carfax's real cap (6)."""
    go = time.time() + 1.5
    prog = ("import sys, time; from datetime import date; from hermes_finance import db, usage\n"
            f"c = db.connect(); time.sleep(max(0, {go} - time.time()))\n"
            f"try:\n    usage.reserve(c, 'carfax', date({TODAY.year}, {TODAY.month}, {TODAY.day})); print('won')\n"
            "except usage.Refused as e:\n    print(e.reason)\n")
    env = {**os.environ, "FINANCE_DATA_DIR": str(config.data_dir())}
    procs = [subprocess.Popen([sys.executable, "-c", prog], env=env, stdout=subprocess.PIPE, text=True) for _ in range(10)]
    outs = sorted(p.communicate(timeout=60)[0].strip() for p in procs)
    assert outs == ["cap"] * 4 + ["won"] * 6
    assert calls(conn, "carfax") == 6


def test_every_failed_call_counts(conn, on, monkeypatch):
    _, browser, _ = on
    monkeypatch.setitem(config.API_CAPS, "carfax", 3)
    car = valuation.create(conn, {**CAR, "balance": 1, "vehicle_make": "Honda", "vehicle_model": "Accord",
                                  "vehicle_year": 2003}, TODAY)
    assert calls(conn, "carfax") == 1                                    # the save's lookup
    for n, response in enumerate([(500, "oops"), cdp_client.CDPError("tab crashed")], start=1):
        browser.on("GET", "consumerapi.carfax.com/hbv/cow/trims/", response)
        assert valuation.refresh(conn, car["id"], day(n))["estimate_result"]["called"] is True
    assert calls(conn, "carfax") == 3
    browser.routes.pop(("GET", "consumerapi.carfax.com/hbv/cow/trims/"))
    assert valuation.refresh(conn, car["id"], day(3))["estimate_result"]["reason"] == "cap"


def _as_seen_elsewhere(provider, seen):
    """What another connection (i.e. committed) sees at the moment the first request of a lookup goes out."""
    other = db.connect(config.db_path())
    try:
        seen.setdefault(provider, {
            "calls": other.execute("SELECT calls FROM api_usage WHERE provider = ?", (provider,)).fetchone()[0],
            "outcome": other.execute("SELECT outcome FROM api_calls WHERE provider = ? ORDER BY id DESC",
                                     (provider,)).fetchone()[0]})
    finally:
        other.close()


@pytest.mark.parametrize("provider", ["carfax", "redfin", "nhtsa"])
def test_the_call_is_counted_before_it_is_sent(conn, on, provider):
    """At the moment the request goes out, api_usage already has it and api_calls holds it as pending (read from another
    connection, i.e. committed): a crash or a hang mid-call can't leave a call uncounted. Carfax (the tab), Redfin and
    NHTSA (HTTPS)."""
    fake_http, browser, rf = on
    seen = {}
    if provider == "carfax":
        from conftest import CARFAX_TRIMS

        def trims(url, method, headers, body):
            _as_seen_elsewhere("carfax", seen)
            return 200, CARFAX_TRIMS
        browser.on("GET", "consumerapi.carfax.com/hbv/cow/trims/", trims)
    elif provider == "redfin":
        def redfin(url, headers):
            _as_seen_elsewhere("redfin", seen)
            return rf.http(url, headers)
        fake_http.on("redfin", redfin)
    else:
        def nhtsa(url, headers):
            _as_seen_elsewhere("nhtsa", seen)
            return 200, NHTSA_OK
        fake_http.on("nhtsa", nhtsa)
    valuation.create(conn, HOME if provider == "redfin" else CAR, TODAY)
    assert seen == {provider: {"calls": 1, "outcome": "pending"}}
    assert conn.execute("SELECT outcome FROM api_calls WHERE provider = ?", (provider,)).fetchone()[0] == "ok"


def test_a_redfin_block_persists_across_restarts(data_dir, free_sources, fake_http, fake_browser):
    conn = db.connect()
    fake_http.on("redfin", (403, b"<html>captcha-delivery</html>"))
    fake_browser.on("GET", "www.redfin.com/stingray/", (403, "<html>captcha-delivery</html>"))
    home = valuation.create(conn, {**HOME, "balance": 900000}, TODAY)
    assert home["estimate_result"]["blocked_until"] == day(7).isoformat()
    conn.close()
    conn = db.connect()                                                  # a restart (or the next day's run)
    with pytest.raises(usage.Refused) as e:
        usage.reserve(conn, "redfin", day(3), home["id"], "daily")
    assert e.value.reason == "blocked"
    assert valuation.refresh(conn, home["id"], day(4))["estimate_result"]["reason"] == "blocked"
    assert calls(conn, "redfin") == 1
    conn.close()


def test_the_nhtsa_stop_survives_a_new_process(conn, on):
    """An NHTSA refusal is written to the DB: a brand-new Python process (the next daily run) sees the block and sends
    nothing (its transport would fail it if it were ever called; its Chrome is switched off like every test's)."""
    fake_http, _, _ = on
    fake_http.on("nhtsa", (403, {"Message": "denied"}))
    car = valuation.create(conn, {**CAR, "vehicle_make": "Honda", "vehicle_model": "Accord", "vehicle_year": 2003,
                                  "balance": 6000}, TODAY)
    assert car["decode_problem"].endswith("Paused until 2026-12-01")
    prog = f"""
import json
from datetime import date
from hermes_finance import db, usage, valuation
sent = []
def transport(url, headers, timeout, max_bytes=None):
    sent.append(url.split("?")[0])
    raise AssertionError("a blocked provider was called")
valuation.transport = transport
conn = db.connect()
try:
    usage.reserve(conn, "nhtsa", date(2026, 11, 20), {car['id']}, "daily")
    reserve = "allowed"
except usage.Refused as e:
    reserve = e.reason
decode = valuation._decode(conn, "{VIN2}", date(2026, 11, 21), {car['id']})[1]
print(json.dumps({{"reserve": reserve, "decode": decode, "sent": sent, "pid": __import__("os").getpid()}}))
"""
    env = {**os.environ, "FINANCE_DATA_DIR": str(config.data_dir()), "HERMES_ENV_FILE": str(config.env_file_path())}
    r = subprocess.run([sys.executable, "-c", prog], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["pid"] != os.getpid() and out["reserve"] == "blocked" and out["sent"] == []
    assert out["decode"].startswith("NHTSA is paused until 2026-12-01")
    assert calls(conn, "nhtsa") == 1


def test_a_bad_cap_config_calls_nothing(conn, on, monkeypatch):
    _, browser, _ = on
    monkeypatch.setitem(config.API_CAPS, "carfax", -1)
    with pytest.raises(config.ConfigError):
        config.api_caps()
    m = valuation.create(conn, {**CAR, "vehicle_make": "Honda", "vehicle_model": "Accord", "vehicle_year": 2003,
                                "balance": 300}, TODAY)
    assert m["decode_problem"].startswith("NHTSA is off: the carfax monthly cap must be a whole number")
    assert m["estimate_result"]["reason"] == "config" and browser.opened == [] and calls(conn, "carfax") == 0


def test_reserve_needs_a_connection_outside_a_transaction(conn):
    conn.execute("INSERT INTO settings VALUES ('x', '1', 'now')")        # an open write
    with pytest.raises(RuntimeError):
        usage.reserve(conn, "carfax", TODAY)
    conn.rollback()


def test_a_block_that_cant_be_saved_still_stops_this_process(conn, on, monkeypatch):
    import sqlite3
    fake_http, _, _ = on
    monkeypatch.setattr(valuation, "_process_blocks", {})
    real = usage.finish

    def locked(c, res, outcome, status=None, note=None, block_reason=None, today=None):
        if block_reason:
            raise sqlite3.OperationalError("database is locked")
        return real(c, res, outcome, status, note, block_reason, today)

    monkeypatch.setattr(usage, "finish", locked)
    fake_http.on("nhtsa", (403, {"Message": "denied"}))
    first = valuation._decode(conn, VIN, TODAY, None)[1]
    assert first.endswith("Paused until 2026-12-01")
    again = valuation._decode(conn, VIN2, TODAY, None)[1]
    assert again == "NHTSA is paused until 2026-12-01" and fake_http.count("nhtsa") == 1


# --- property, mileage nudge ---------------------------------------------------------------------------------------

def test_a_property_gets_the_redfin_estimate(conn, on):
    fake_http, _, rf = on
    m = valuation.create(conn, HOME, TODAY)
    assert m["balance"] == 1014415.33 and m["value_source"] == "estimate"
    assert m["valuation"]["estimate"]["source_label"] == "Redfin" and m["valuation"]["estimate"]["basis"] == "estimate"
    assert rf.asked == [f"autocomplete {ALDER}", "avm 5555501", "above 5555501"] and calls(conn, "redfin") == 1


def test_property_address_is_validated(conn):
    with pytest.raises(manual.ManualError, match="street, city and state"):
        manual.validate("property", {"address": "Home"}, TODAY, partial=True)
    with pytest.raises(manual.ManualError, match="doesn't apply to vehicle"):
        manual.validate("vehicle", {"address": "1 Main St, Springfield"}, TODAY, partial=True)


def test_the_mileage_nudge_is_quiet_and_comes_every_90_days(conn, on):
    m = valuation.create(conn, CAR, TODAY)
    r = lambda d: valuation.state(conn, row(conn, m["id"]), d)  # noqa: E731
    assert r(day(89))["mileage_nudge"] is False and r(day(90))["mileage_nudge"] is True
    manual.edit(conn, m["id"], {"mileage": 131000}, day(90))
    assert r(day(90))["mileage_nudge"] is False and row(conn, m["id"])["mileage_as_of"] == day(90).isoformat()


# --- the CLI -------------------------------------------------------------------------------------------------------

def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_revalue_dry_run_calls_and_counts_nothing(conn, on, capsys, monkeypatch):
    fake_http, _, rf = on
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    home = manual.create(conn, {**HOME, "balance": 300000}, TODAY)
    code, out, _ = run(capsys, "revalue", "--dry-run")
    assert code == 0 and f"#{home['id']} Home (redfin): would call: would ask Redfin (no estimate yet)" in out
    assert "dry run: nothing called or counted" in out and fake_http.calls == [] and calls(conn, "redfin") == 0
    code, out, _ = run(capsys, "revalue")
    assert code == 0 and "valued: Estimated $1,014,415.33 by Redfin; kept your value" in out
    assert "calls this month: Carfax 0/6, Redfin 1/20" in out
    code, out, _ = run(capsys, "revalue", "--account", str(home["id"]))            # a Refresh: once a day
    assert code == 0 and "already asked Redfin today" in out
    fake_http.on("redfin", TimeoutError())
    monkeypatch.setattr(cli, "_today", lambda: day(1))
    code, out, _ = run(capsys, "revalue", "--account", str(home["id"]))
    assert code == 1 and "failed: Redfin timed out; not retried" in out           # non-fatal: reported, exit 1


def test_cli_api_usage_prints_used_and_cap(conn, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 9, 26))
    code, out, _ = run(capsys, "api-usage")
    assert code == 0 and "API usage for 2026-09 (resets 2026-10-01" in out
    assert "NHTSA                0 used / cap 300 / free, no key" in out
    assert "Carfax               0 used / cap 6 / free page, no key; each lookup counts once" in out
    assert "Redfin               0 used / cap 20 / free page, no key; each lookup counts once" in out
    assert "Wikimedia Commons    0 used / cap 60 / free API, no key (car photos)" in out
    assert "OpenStreetMap        0 used / cap 20 / free map tiles, no key (home maps)" in out
    assert "MarketCheck" not in out and "RentCast" not in out
    usage.block(conn, "redfin", date(2026, 9, 26), "Redfin blocked the check (autocomplete: HTTP 403)")
    code, out, _ = run(capsys, "api-usage", "--json")
    p = {x["provider"]: x for x in json.loads(out)["providers"]}
    assert set(p) == {"nhtsa", "carfax", "redfin", "commons", "osm"} and p["redfin"]["blocked_until"] == "2026-10-03"
    code, out, _ = run(capsys, "api-usage", "--unblock", "redfin")
    assert code == 0 and "block lifted" in out and usage.blocked(conn, "redfin", date(2026, 9, 26)) is None
    with pytest.raises(SystemExit):
        cli.main(["api-usage", "--unblock", "marketcheck"])                       # not a provider any more


def test_cli_manual_add_and_use_estimate(conn, on, capsys):
    code, out, _ = run(capsys, "manual", "add", "--kind", "vehicle", "--vin", VIN, "--mileage", "120000", "--zip", "94110",
                       "--balance", "6000")
    assert code == 0 and "2003 Honda Accord  $6,000" in out and "Your value (Carfax trade-in $14,090 on" in out
    assert "VIN decoded by NHTSA: 2003 Honda Accord EX-V6" in out and "estimate: Trade-in $14,090 by Carfax" in out
    code, out, _ = run(capsys, "manual", "edit", "1", "--use-estimate")
    assert code == 0 and "$14,090" in out and "Trade-in · Carfax ·" in out
    code, _, err = run(capsys, "manual", "edit", "1", "--use-estimate", "--balance", "1")
    assert code == 2 and "--use-estimate takes no other field" in err


def test_networth_shows_value_source_and_as_of_date(conn, on, capsys, monkeypatch):
    car = valuation.create(conn, CAR, TODAY)
    valuation.create(conn, {**HOME, "balance": 450000}, TODAY)
    p = reads.networth(conn, TODAY)
    groups = {g["key"]: g for g in p["groups"]}
    v = groups["vehicles"]["accounts"][0]["valuation"]
    est_day = car["valuation"]["estimate"]["date"]
    assert v == {"value": 14090.0, "source": "estimate", "label": "Carfax", "provider": "carfax", "basis": "trade_in",
                 "as_of": est_day, "estimate": {"value": 14090.0, "source": "carfax", "date": est_day, "miles": 120000,
                                                "basis": "trade_in"}}
    h = groups["property"]["accounts"][0]["valuation"]
    assert h["source"] == "user" and h["label"] == "Your value" and h["as_of"] == TODAY.isoformat() and h["value"] == 450000.0
    assert h["estimate"]["value"] == 1014415.33
    text = reads.networth_text(p)
    # TASK-15: the migrated Custom pick (the Cash & debts preset) leaves the car and the home out, and the All listing says so
    assert f"2003 Honda Accord [manual] [not counted in Custom]  $14,090 (Trade-in · Carfax · as of {est_day})" in text
    assert f"Home [manual] [not counted in Custom]  $450,000 (Your value · as of {TODAY.isoformat()}; Redfin estimate $1,014,415.33 on {est_day})" in text
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    code, out, _ = run(capsys, "networth", "--json")
    assert code == 0 and json.loads(out)["groups"] == p["groups"]


# --- the dashboard ---------------------------------------------------------------------------------------------------

@pytest.fixture
def app(conn):
    a = App(config.db_path(), lambda: None)
    a.today = lambda: TODAY
    return a


def call(app, method, path, body=None):
    status, _, raw = app.handle(method, path, POST if method == "POST" else LOCAL,
                                json.dumps(body or {}).encode() if method == "POST" else b"")
    return status, json.loads(raw)


def test_the_form_body_with_nulls_and_blanks_saves(app, conn, on):
    """Exactly what the page posts for a car left to its estimate: nulls for the value and the empty optional fields,
    the year / make / model left for the VIN to fill."""
    body = {"kind": "vehicle", "vin": VIN, "vehicle_make": None, "vehicle_model": None, "vehicle_year": None,
            "vehicle_trim": None, "mileage": 120000, "zip_code": "94110", "balance": None}
    status, m = call(app, "POST", "/api/manual", body)
    assert status == 200 and m["name"] == "2003 Honda Accord" and m["balance"] == 14090.0 and m["value_source"] == "estimate"
    status, m = call(app, "POST", "/api/manual", {"kind": "property", "name": "Home", "address": None, "balance": None})
    assert status == 400 and m == {"error": "Your value is required (or add the address to use the Redfin estimate)",
                                   "field": "balance"}


def test_dashboard_save_refresh_and_use_estimate(app, conn, on):
    _, browser, _ = on
    status, m = call(app, "POST", "/api/manual", CAR)
    assert status == 200 and m["balance"] == 14090.0 and m["estimate_result"]["ok"]
    status, m = call(app, "POST", f"/api/manual/{m['id']}", {"balance": 16200})
    assert status == 200 and m["value_source"] == "user"
    status, r = call(app, "POST", f"/api/manual/{m['id']}/refresh")
    assert status == 200 and r["estimate_result"]["reason"] == "today"   # the save used today's
    app.today = lambda: day(1)
    set_price(browser, 13800)
    status, r = call(app, "POST", f"/api/manual/{m['id']}/refresh")
    assert status == 200 and r["estimate_result"]["ok"] and r["balance"] == 16200.0
    status, u = call(app, "POST", f"/api/manual/{m['id']}/use-estimate")
    assert status == 200 and u["balance"] == 13800.0 and u["value_source"] == "estimate"
    status, a = call(app, "GET", "/api/accounts")
    assert a["valuation"]["carfax"] == {**a["valuation"]["carfax"], "enabled": True, "calls": 2, "cap": 6,
                                        "blocked_until": None}
    assert a["valuation"]["carfax"]["privacy_note"] == ("Your VIN, mileage and ZIP code are sent to NHTSA (the free VIN "
                                                        "lookup) and to Carfax (through your Chrome) to get an estimate")
    assert a["valuation"]["redfin"]["privacy_note"] == "Your address is sent to Redfin to get an estimate"
    car = next(x for x in a["manual"] if x["kind"] == "vehicle")
    assert car["valuation"]["estimate"]["value"] == 13800.0 and "vin_decoded" not in car
    status, err = call(app, "POST", "/api/manual", {**CAR, "vin": "1HGCM82643A004352"})
    assert status == 400 and err["field"] == "vin" and "check digit" in err["error"]
    assert call(app, "POST", "/api/manual/999/refresh")[0] == 404
    assert call(app, "POST", f"/api/manual/{m['id']}/confirm-estimate")[0] == 404   # RentCast's confirm step is gone


def test_archived_accounts_are_never_valued(conn, on):
    _, browser, _ = on
    m = valuation.create(conn, CAR, TODAY)
    manual.archive(conn, m["id"], TODAY)
    assert valuation.revalue(conn, day(40))["results"] == [] and len(browser.opened) == 1


def test_a_value_typed_while_the_lookup_is_out_wins(conn, on):
    _, browser, _ = on
    m = valuation.create(conn, CAR, TODAY)

    def slow(url, method, headers, body):   # the user saves 9999 in the dashboard while Carfax answers
        other = db.connect(config.db_path())
        manual.edit(other, m["id"], {"balance": 9999}, day(1))
        other.close()
        return prices(13100)

    browser.on("POST", "consumerapi.carfax.com/hbv/cow/prices", slow)
    r = valuation.refresh(conn, m["id"], day(1))
    assert r["estimate_result"]["ok"] and r["estimate_result"]["applied"] is False
    assert r["balance"] == 9999.0 and r["value_source"] == "user" and r["valuation"]["estimate"]["value"] == 13100.0


def test_an_estimate_counts_only_for_the_vin_or_address_it_was_for(conn, on):
    fake_http, browser, rf = on
    m = valuation.create(conn, CAR, TODAY)
    fake_http.on("nhtsa", (200, {"Results": [{"ModelYear": "2019", "Make": "FORD", "Model": "Transit"}]}))
    e = valuation.edit(conn, m["id"], {"vin": VIN2}, TODAY)             # corrected the same day: Carfax waits
    assert e["estimate_result"]["reason"] == "today"
    assert e["balance"] == 0.0 and e["valuation"]["estimate"] is None  # the Accord's price doesn't value the Transit
    with pytest.raises(manual.ManualError, match="No estimate for 2019 Ford Transit yet \\(for this VIN\\)"):
        valuation.use_estimate(conn, m["id"], TODAY)
    set_price(browser, 31000)
    assert valuation.refresh(conn, m["id"], day(1))["balance"] == 31000.0
    home = valuation.create(conn, HOME, TODAY)
    e = valuation.edit(conn, home["id"], {"address": "  1420   ALDER AVE, Santa Rosa, CA 95405 "}, day(1))
    assert e["balance"] == 1014415.33 and e["valuation"]["estimate"]["value"] == 1014415.33   # the same address


def test_each_vin_is_decoded_once(conn, on):
    fake_http, _, _ = on
    valuation.create(conn, {**CAR, "balance": 1}, TODAY)
    valuation.create(conn, {**CAR, "balance": 2}, TODAY)                # another account, same VIN
    assert fake_http.count("nhtsa") == 1
    assert conn.execute("SELECT vehicle_make, vehicle_trim FROM vin_decodes WHERE vin = ?", (VIN,)).fetchone()[:] == \
        ("Honda", "EX-V6")


def test_an_unexpected_nhtsa_answer_is_an_explained_counted_failure(conn, on):
    fake_http, _, _ = on
    fake_http.on("nhtsa", (200, {"Results": "not a list"}))
    problem = valuation._decode(conn, VIN, TODAY, None)[1]
    assert problem == "NHTSA couldn't read this VIN" and calls(conn, "nhtsa") == 1
    fake_http.on("nhtsa", (200, b"<html>maintenance</html>"))
    assert valuation._decode(conn, VIN2, TODAY, None)[1] == "NHTSA sent something unreadable"


# --- the real transport's error mapping (a fake opener: still no network) ---------------------------------------

class _Resp:
    status = 200

    def __init__(self, body=b'{"Results": []}', exc=None):
        self.body, self.exc = body, exc

    def read(self, n):
        if self.exc:
            raise self.exc
        return self.body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    def __init__(self, outcome):
        self.outcome, self.requests = outcome, []

    def open(self, req, timeout):
        self.requests.append((req.full_url, dict(req.header_items()), timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def test_urllib_transport_maps_errors_without_the_url(monkeypatch):
    import http.client
    import io
    import urllib.error
    url = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValuesExtended/X?format=json"
    ok = _Opener(_Resp())
    monkeypatch.setattr(valuation, "_OPENER", ok)
    assert valuation.urllib_transport(url, {}, 10.0) == (200, b'{"Results": []}')
    assert ok.requests[0][2] == 10.0 and ok.requests[0][1]["User-agent"].startswith("hermes-finance/")
    big = _Opener(_Resp(body=b"x" * 2_000_000))
    monkeypatch.setattr(valuation, "_OPENER", big)
    assert len(valuation.urllib_transport(url, {}, 10.0)[1]) == valuation.MAX_BYTES            # an API answer: 512 KB
    assert len(valuation.urllib_transport(url, {}, 10.0, max_bytes=1_500_000)[1]) == 1_500_000  # an image: more
    err = urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b'{"Message": "denied"}'))
    monkeypatch.setattr(valuation, "_OPENER", _Opener(err))
    assert valuation.urllib_transport(url, {}, 10.0) == (403, b'{"Message": "denied"}')
    for outcome, exc in ((urllib.error.URLError(TimeoutError()), TimeoutError),
                         (urllib.error.URLError(ConnectionRefusedError()), OSError),
                         (_Resp(exc=http.client.IncompleteRead(b"x")), OSError),
                         (_Resp(exc=TimeoutError()), TimeoutError)):
        monkeypatch.setattr(valuation, "_OPENER", _Opener(outcome))
        with pytest.raises(exc) as e:
            valuation.urllib_transport(url, {}, 10.0)
        assert "vpic" not in str(e.value)
    with pytest.raises(ValueError):
        valuation.urllib_transport("http://vpic.nhtsa.dot.gov/x", {}, 10.0)       # https only
    assert valuation._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere") is None


def test_no_test_can_reach_the_network():
    """How "no real network call" is enforced in every test (conftest.no_network, autouse): each layer refuses."""
    import socket
    import urllib.request
    with pytest.raises(AssertionError, match="real network call attempted"):
        valuation.transport("https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/x?format=json", {}, 1.0)
    with pytest.raises(AssertionError, match="real network call attempted"):
        valuation.urllib_transport("https://commons.wikimedia.org/w/api.php", {}, 1.0)
    with pytest.raises(AssertionError, match="real network call attempted"):
        urllib.request.urlopen("https://tile.openstreetmap.org/17/1/1.png")
    for address in (("93.184.216.34", 443), ("www.redfin.com", 443), ("2606:2800:220:1::", 443)):
        family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as s, pytest.raises(AssertionError, match="real network connection"):
            s.connect(address)
    with pytest.raises(AssertionError, match="real network lookup"):     # refused before any DNS query
        socket.create_connection(("upload.wikimedia.org", 443), timeout=1)
    with socket.socket() as srv:                                         # loopback is allowed (test servers, headless
        srv.bind(("127.0.0.1", 0))                                       # Chrome's DevTools)
        srv.listen(1)
        with socket.create_connection(srv.getsockname(), timeout=2):
            pass
    assert not config.env_file_path().exists()                           # and no real .env is in reach


# --- TASK-13b: MarketCheck and RentCast are gone ------------------------------------------------------------------

GONE = ("api.marketcheck.com", "api.rentcast.io", "MARKETCHECK_API_KEY", "RENTCAST_API_KEY", "X-Api-Key",
        "marketcheck_value", "rentcast_value", "rentcast_check", "confirm_estimate", "confirm-estimate",
        "MARKETCHECK_MONTHLY_CAP", "RENTCAST_MONTHLY_CAP", "api_keys(")


def test_no_code_can_build_a_marketcheck_or_rentcast_request():
    """Grep over everything the app runs (Python, the page, the daily script copy): none of their hosts, key names,
    functions or the RentCast-only confirm step remain. The only mentions left are the words in comments explaining
    that their old rows are kept as history (and the source CHECK that keeps those rows valid)."""
    files = [*SRC.rglob("*.py"), SRC / "static" / "app.js", SRC / "static" / "app.css",
             SRC.parents[1] / "scripts" / "finance_daily_prerun.sh"]
    hits = [f"{f.name}: {needle}" for f in files for needle in GONE if needle in f.read_text()]
    assert hits == []
    js = (SRC / "static" / "app.js").read_text() + (SRC / "static" / "app.css").read_text()
    assert not re.search(r"marketcheck|rentcast", js, re.I)              # no label, class or comment on the page
    assert set(usage.PROVIDERS) == {"nhtsa", "carfax", "redfin", "commons", "osm"} == set(config.API_CAPS)
    assert valuation.SOURCE_OF == {"vehicle": "carfax", "property": "redfin"}
    assert not hasattr(config, "api_keys") and not hasattr(valuation, "marketcheck_value")
    helps = subprocess.run([sys.executable, "-m", "hermes_finance.cli", "manual", "edit", "--help"], capture_output=True,
                           text=True, timeout=60).stdout
    assert "--confirm-estimate" not in helps and "--use-estimate" in helps


def test_revalue_never_asks_them_even_with_old_keys_in_the_env(conn, on, tmp_path):
    """The old keys are still in the owner's .env: they're ignored. Every request of a full daily run goes to NHTSA,
    Carfax (the tab) or Redfin; FakeHttp has no route for their hosts, so a request there would fail the test."""
    fake_http, browser, rf = on
    set_env(tmp_path / "hermes.env", MARKETCHECK_API_KEY="mc-old-key-000000000000000000000001",
            RENTCAST_API_KEY="rc-old-key-000000000000000000000002")
    car = manual.create(conn, {**CAR, "vehicle_make": "Honda", "vehicle_model": "Accord", "vehicle_year": 2003,
                               "balance": 1}, TODAY)
    manual.create(conn, {**HOME, "balance": 1}, TODAY)
    out = valuation.revalue(conn, TODAY)
    assert [x["provider"] for x in out["results"]] == ["carfax", "redfin"] and out["valued"] == 2
    hosts = {u.split("/")[2] for u, _ in fake_http.calls} | {f["url"].split("/")[2] for f in browser.fetches}
    assert hosts <= {"vpic.nhtsa.dot.gov", "www.redfin.com", "consumerapi.carfax.com"}
    assert {r[0] for r in conn.execute("SELECT DISTINCT provider FROM api_calls")} <= {"nhtsa", "carfax", "redfin"}
    assert "mc-old-key" not in json.dumps(manual.describe(conn, row(conn, car["id"]), TODAY))


def test_their_old_rows_are_kept_but_never_shown_or_used(conn, on):
    """History from before TASK-13b (the live DB has one MarketCheck row for the Comet and one RentCast row for the
    archived test condo) stays in asset_valuations, but no value, list, history or page uses it."""
    car = manual.create(conn, {**CAR, "vehicle_make": "Zenith", "vehicle_model": "Comet Prime", "vehicle_year": 2020,
                               "balance": 14090}, TODAY)
    home = manual.create(conn, {**HOME, "balance": 900000}, TODAY)
    with conn:
        conn.execute("INSERT INTO asset_valuations (account_id, value, source, miles, subject, fetched_at, raw_json) "
                     "VALUES (?, 20198, 'marketcheck', 108000, ?, '2026-09-27T03:16:40Z', '{}')", (car["id"], VIN))
        conn.execute("INSERT INTO asset_valuations (account_id, value, low, high, source, subject, fetched_at, raw_json) "
                     "VALUES (?, 1302000, 1028000, 1575000, 'rentcast', ?, '2026-09-27T03:53:19Z', '{}')",
                     (home["id"], valuation.subject(row(conn, home["id"]))))
    for acct in (car, home):
        r = row(conn, acct["id"])
        assert valuation.latest(conn, r) is None and valuation.effective(conn, r) is None
        assert valuation.history(conn, acct["id"]) == []
        assert manual.describe(conn, r, TODAY)["valuation"]["estimate"] is None
        with pytest.raises(manual.ManualError, match="No estimate"):
            valuation.use_estimate(conn, acct["id"], TODAY)
        d = valuation.detail(conn, acct["id"], TODAY)
        assert d["history"] == [] and all(x["value"] is None for x in d["table"])
        assert "MarketCheck" not in json.dumps(d) and "RentCast" not in json.dumps(d)
    assert conn.execute("SELECT COUNT(*) FROM asset_valuations WHERE source IN ('marketcheck', 'rentcast')").fetchone()[0] == 2

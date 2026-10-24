"""TASK-22e: a failed logo fetch must not mean initials forever.

Before, Alpaca's first sync stored its icon row even when the fetch failed (logo_png NULL, source 'none'), and every
later sync saw that row and returned: one network blip, no Alpaca logo ever. A Plaid bank was looked up only when it was
connected (and by hand with `finance refresh-institutions`), so a blip then meant initials for good too. Now a row
without a logo is tried again once its last try is logos.INSTITUTION_RETRY_DAYS (1) old, by date: the next day's run
always retries, a second run the same day never does, and a logo once found is kept. Every fetch here is a fake; the
clock is pinned through alpaca.now / logos.now (what fetched_at is written with)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from conftest import add_item, err, page
from pngtools import logo_png
from hermes_finance import alpaca, cli, logos, syncnow
from hermes_finance.logos import FetchError, Response

HOME = "https://alpaca.markets"
TOUCH = "https://alpaca.markets/touch.png"
FAVICON = "https://alpaca.markets/favicon.ico"
PNG = logo_png(64)
KEY_ID, SECRET = "AKTESTKEY0000000001", "sEcReT0000000000000000000000000000000000ab"
ACCOUNT = {"status": "ACTIVE", "cash": "125.34", "equity": "1230.00", "currency": "USD", "account_number": "PA0012344321"}
GET_BY_ID = "/institutions/get_by_id"
CHASE = {"institution_id": "ins_56", "name": "Chase", "logo": None, "primary_color": "#095aa6",
         "url": "https://www.chase.com"}
CHASE_HOME, CHASE_TOUCH = "https://www.chase.com", "https://www.chase.com/touch.png"


class Clock:
    """What db.now() says in alpaca.py and logos.py (fetched_at, and the day a retry is judged by)."""

    def __init__(self, monkeypatch):
        self.stamp = "2026-09-29T07:00:05Z"
        monkeypatch.setattr(alpaca, "now", lambda: self.stamp)
        monkeypatch.setattr(logos, "now", lambda: self.stamp)

    def at(self, stamp: str) -> "Clock":
        self.stamp = stamp
        return self


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


class Site:
    """A bank's (or Alpaca's) web site for logos.site_icon: its home page names an apple-touch-icon; `down` is a network
    blip (every request raises FetchError, as logos.http_get does on a timeout). Every request is recorded."""

    def __init__(self, home: str, touch: str):
        self.home, self.touch = home, touch
        self.down = False
        self.calls: list[str] = []

    def __call__(self, url):
        self.calls.append(url)
        if self.down:
            raise FetchError(f"{url}: TimeoutError")
        if url == self.home:
            return Response(200, "text/html", b'<link rel="apple-touch-icon" href="/touch.png">', url)
        if url == self.touch:
            return Response(200, "image/png", PNG, url)
        return Response(404, "text/html", b"", url)


def tries(site: Site) -> int:
    """How many times the logo was looked for: each try starts at the home page."""
    return site.calls.count(site.home)


# --- Alpaca ------------------------------------------------------------------------------------------------------------

class FakeAlpaca:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, host, target, headers):
        self.calls.append(target)
        return 200, json.dumps(ACCOUNT if target == "/v2/account" else []).encode()


@pytest.fixture
def alpaca_on(tmp_path, monkeypatch):
    """A live key file and Alpaca answered by a fake, the switch lifted (conftest sets HERMES_ALPACA=off)."""
    path = tmp_path / "cfg" / "alpaca-live.env"
    path.parent.mkdir(parents=True)
    path.write_text(f"APCA_API_KEY_ID={KEY_ID}\nAPCA_API_SECRET_KEY={SECRET}\nAPCA_ENV=live\n")
    monkeypatch.setenv("HERMES_ALPACA_KEY_FILE", str(path))
    monkeypatch.delenv("HERMES_ALPACA", raising=False)
    fake = FakeAlpaca()
    monkeypatch.setattr(alpaca, "transport", fake)
    return fake


@pytest.fixture
def alpaca_site():
    return Site(HOME, TOUCH)


def alpaca_row(conn):
    r = conn.execute("SELECT logo_png, logo_source, fetched_at, name, url FROM institutions WHERE institution_id = ?",
                     (alpaca.INSTITUTION_ID,)).fetchone()
    return None if r is None else (r["logo_png"], r["logo_source"], r["fetched_at"], r["name"], r["url"])


def sync_at(conn, clock, stamp, site):
    clock.at(stamp)
    out = alpaca.sync(conn, date.fromisoformat(stamp[:10]), fetch_logo=site)
    assert out["action"] == "saved", out
    return out


def test_alpaca_a_failed_first_fetch_is_retried_the_next_day_once_then_kept_for_good(alpaca_on, alpaca_site, conn,
                                                                                      clock):
    site = alpaca_site
    # 1. the first sync, during a network blip: the row is stored with no logo (the page draws initials)
    site.down = True
    sync_at(conn, clock, "2026-09-29T07:00:05Z", site)
    assert site.calls == [HOME, FAVICON]                                 # one try: the home page, then /favicon.ico
    assert alpaca_row(conn) == (None, "none", "2026-09-29T07:00:05Z", "Alpaca", HOME)
    assert logos.institution_logo(conn, alpaca.INSTITUTION_ID) is None

    # 2. more syncs the same day (the dashboard's Connect, a second run): no fetch at all, even with the site back
    site.down = False
    for stamp in ("2026-09-29T07:30:00Z", "2026-09-29T23:59:59Z"):
        sync_at(conn, clock, stamp, site)
    assert site.calls == [HOME, FAVICON]
    assert alpaca_row(conn)[:3] == (None, "none", "2026-09-29T07:00:05Z")

    # 3. the next morning's daily sync (under 24 h later, still a new day): exactly one try, and the logo is stored
    sync_at(conn, clock, "2026-09-30T06:59:00Z", site)
    assert site.calls[2:] == [HOME, TOUCH] and tries(site) == 2
    assert alpaca_row(conn) == (PNG, "site", "2026-09-30T06:59:00Z", "Alpaca", HOME)
    assert logos.institution_logo(conn, alpaca.INSTITUTION_ID) == ("image/png", PNG)

    # 4. after that success: never fetched again (the same day, the next day, weeks later, with the site down)
    site.down = True
    for stamp in ("2026-09-30T12:00:00Z", "2026-10-01T07:00:00Z", "2026-11-15T07:00:00Z"):
        sync_at(conn, clock, stamp, site)
    assert len(site.calls) == 4 and tries(site) == 2
    assert alpaca_row(conn) == (PNG, "site", "2026-09-30T06:59:00Z", "Alpaca", HOME)


def test_alpaca_a_retry_that_fails_again_leaves_initials_and_waits_another_day(alpaca_on, alpaca_site, conn, clock):
    site = alpaca_site
    site.down = True
    sync_at(conn, clock, "2026-09-29T07:00:00Z", site)
    sync_at(conn, clock, "2026-09-30T07:00:00Z", site)                   # the next day: tried once more, fails again
    assert tries(site) == 2
    assert alpaca_row(conn)[:3] == (None, "none", "2026-09-30T07:00:00Z")  # its try date moved: the wait starts again
    sync_at(conn, clock, "2026-09-30T20:00:00Z", site)
    assert tries(site) == 2                                              # not again that day
    site.down = False
    sync_at(conn, clock, "2026-10-03T07:00:00Z", site)                   # days without a sync: the next one tries
    assert tries(site) == 3 and alpaca_row(conn)[:2] == (PNG, "site")


def test_alpaca_the_kill_switch_means_no_logo_fetch_even_when_one_is_due(alpaca_on, alpaca_site, conn, clock,
                                                                         monkeypatch):
    site = alpaca_site
    with conn:   # a miss from last week: due a retry
        conn.execute("INSERT INTO institutions VALUES (?, 'Alpaca', NULL, 'none', NULL, ?, '2026-09-20T07:00:00Z')",
                     (alpaca.INSTITUTION_ID, HOME))
    clock.at("2026-09-29T07:00:00Z")
    monkeypatch.setenv("HERMES_ALPACA", "off")
    alpaca._store_logo(conn, site)
    out = alpaca.sync(conn, date(2026, 9, 29), fetch_logo=site)
    assert out["action"] == "failed" and "switched off" in out["message"]
    assert site.calls == [] and alpaca_on.calls == []                    # zero fetches, zero Alpaca requests
    assert alpaca_row(conn)[:3] == (None, "none", "2026-09-20T07:00:00Z")
    # the same row with the switch lifted: the retry is due, so exactly one try
    monkeypatch.delenv("HERMES_ALPACA")
    sync_at(conn, clock, "2026-09-29T07:00:00Z", site)
    assert site.calls == [HOME, TOUCH] and alpaca_row(conn)[:2] == (PNG, "site")


# --- Plaid banks -------------------------------------------------------------------------------------------------------

def inst_row(conn, institution_id):
    r = conn.execute("SELECT logo_png, logo_source, primary_color, fetched_at FROM institutions WHERE institution_id = ?",
                     (institution_id,)).fetchone()
    return None if r is None else tuple(r)


@pytest.fixture
def chase_site():
    return Site(CHASE_HOME, CHASE_TOUCH)


@pytest.fixture
def banks(conn, fake):
    """Chase (Plaid has no logo for it: its site's icon), and Nova, whose logo is already stored."""
    add_item(conn, "item_a", "ins_56", "Chase")
    add_item(conn, "item_b", "ins_126339", "Nova")
    with conn:
        conn.execute("INSERT INTO institutions VALUES ('ins_126339', 'Nova', ?, 'plaid', '#00a9ce', "
                     "'https://www.novabank.example', '2026-09-01T00:00:00Z')", (PNG,))
    fake.on(GET_BY_ID, lambda b: {"institution": CHASE} if b["institution_id"] == "ins_56" else
            err("INVALID_INSTITUTION", "INVALID_INPUT"))
    return fake


def lookups(fake) -> list[str]:
    return [b["institution_id"] for b in fake.bodies(GET_BY_ID)]


def test_plaid_a_bank_whose_icon_failed_at_connect_is_looked_up_again_the_next_day_once_then_kept(
        conn, client, banks, chase_site, clock):
    site = chase_site
    # connect time, during a network blip (link.py): the row keeps the colour but no logo
    site.down = True
    clock.at("2026-09-29T07:00:00Z")
    logos.refresh_institution(conn, client, "ins_56", site)
    assert inst_row(conn, "ins_56") == (None, "none", "#095aa6", "2026-09-29T07:00:00Z") and tries(site) == 1

    # the same day: nothing is asked (neither Plaid nor the site), whatever the network does
    site.down = False
    for stamp in ("2026-09-29T09:00:00Z", "2026-09-29T23:59:59Z"):
        clock.at(stamp)
        assert logos.refresh_institutions(conn, client, site, missing_only=True) == []
    assert lookups(banks) == ["ins_56"] and tries(site) == 1

    # the next morning (under 24 h later): Chase, and only Chase, is looked up once; its icon is stored
    clock.at("2026-09-30T06:59:00Z")
    (out,) = logos.refresh_institutions(conn, client, site, missing_only=True)
    assert out["source"] == "site" and not out["error"]
    assert lookups(banks) == ["ins_56", "ins_56"] and site.calls[-2:] == [CHASE_HOME, CHASE_TOUCH] and tries(site) == 2
    assert inst_row(conn, "ins_56") == (PNG, "site", "#095aa6", "2026-09-30T06:59:00Z")

    # after that: never again, and Nova's stored logo was never looked up
    site.down = True
    for stamp in ("2026-10-01T07:00:00Z", "2026-11-15T07:00:00Z"):
        clock.at(stamp)
        assert logos.refresh_institutions(conn, client, site, missing_only=True) == []
    assert lookups(banks) == ["ins_56", "ins_56"] and tries(site) == 2
    assert inst_row(conn, "ins_126339") == (PNG, "plaid", "#00a9ce", "2026-09-01T00:00:00Z")


def test_plaid_a_bank_whose_lookup_failed_is_asked_once_a_day_too(conn, client, fake, chase_site, clock):
    add_item(conn, "item_a", "ins_56", "Chase")          # no institutions row: the lookup failed when it was connected
    fake.on(GET_BY_ID, err("INTERNAL_SERVER_ERROR", "API_ERROR", 500))
    clock.at("2026-09-29T07:00:00Z")
    (out,) = logos.refresh_institutions(conn, client, chase_site, missing_only=True)
    assert out["error"] and inst_row(conn, "ins_56") == (None, "none", None, "2026-09-29T07:00:00Z")   # the try counts
    clock.at("2026-09-29T20:00:00Z")
    assert logos.refresh_institutions(conn, client, chase_site, missing_only=True) == []
    assert lookups(fake) == ["ins_56"]                                   # not asked twice the same day
    fake.on(GET_BY_ID, {"institution": CHASE})
    clock.at("2026-09-30T07:00:00Z")
    (out,) = logos.refresh_institutions(conn, client, chase_site, missing_only=True)
    assert out["source"] == "site" and lookups(fake) == ["ins_56", "ins_56"]
    assert inst_row(conn, "ins_56") == (PNG, "site", "#095aa6", "2026-09-30T07:00:00Z")


def test_plaid_finance_sync_runs_the_retry_but_sync_now_asks_plaid_nothing_more(conn, cli_client, banks, chase_site,
                                                                                clock, capsys, monkeypatch):
    with conn:   # Chase's icon failed yesterday
        conn.execute("INSERT INTO institutions VALUES ('ins_56', 'Chase', NULL, 'none', '#095aa6', ?, "
                     "'2026-09-28T07:00:00Z')", (CHASE_HOME,))
    monkeypatch.setattr(logos, "http_get", chase_site)
    clock.at("2026-09-29T07:00:00Z")
    # Sync now (the page, finance_sync): exactly the calls TASK-22 lists, no lookup
    out = syncnow.run(conn, lambda: cli_client)
    assert out["ok"] and GET_BY_ID not in banks.paths() and chase_site.calls == []
    # the daily `finance sync`: the one lookup (Chase only), silently
    assert cli.main(["sync"]) == 0
    assert lookups(banks) == ["ins_56"] and tries(chase_site) == 1
    assert inst_row(conn, "ins_56")[:2] == (PNG, "site")
    assert capsys.readouterr().out.strip().splitlines()[:3] == ["Chase: +0 ~0 -0 | balances ok",
                                                               "Nova: +0 ~0 -0 | balances ok", "synced 2/2 item(s)"]


def test_plaid_finance_sync_asks_nothing_when_no_bank_synced_and_the_next_good_run_does(conn, cli_client, banks,
                                                                                       chase_site, clock, monkeypatch):
    banks.on("/transactions/sync", err("ITEM_LOGIN_REQUIRED"))
    monkeypatch.setattr(logos, "http_get", chase_site)
    clock.at("2026-09-29T07:00:00Z")
    assert cli.main(["sync"]) == 1                                       # every bank failed: no lookup, no try spent
    assert GET_BY_ID not in banks.paths() and chase_site.calls == [] and inst_row(conn, "ins_56") is None
    banks.on("/transactions/sync", page(next_cursor="c1"))
    clock.at("2026-09-29T08:00:00Z")
    assert cli.main(["sync"]) == 0                                       # the same day, once a bank syncs: asked
    assert lookups(banks) == ["ins_56"] and inst_row(conn, "ins_56")[:2] == (PNG, "site")


# --- holdings: the 30-day retry the retry_due helper now shares --------------------------------------------------------

def test_holdings_keep_their_30_day_retry_through_the_shared_helper():
    miss = "2026-09-26T00:00:00Z"
    assert not logos.retry_due(miss, logos.MISS_RETRY_DAYS, date(2026, 10, 25))      # 29 days: not yet
    assert logos.retry_due(miss, logos.MISS_RETRY_DAYS, date(2026, 10, 26))          # 30 days: due
    assert not logos.retry_due(miss, logos.INSTITUTION_RETRY_DAYS, date(2026, 9, 26))  # a bank: not the same day
    assert logos.retry_due(miss, logos.INSTITUTION_RETRY_DAYS, date(2026, 9, 27))    # the next day
    assert logos.retry_due(None, logos.INSTITUTION_RETRY_DAYS, date(2026, 9, 26))    # never tried: due

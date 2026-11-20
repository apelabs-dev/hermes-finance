"""SimpleFIN's protocol v2 client (stdlib urllib): the setup-token claim, the access URL (a secret: never in a URL,
a log, an exception or the database), the connect-time SSRF check, /accounts and the v2 errlist. Every test uses an
injected transport and resolver: no test resolves or reaches a real host.
"""

from __future__ import annotations

import base64
import json
import logging
import socket
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from hermes_finance import redact, simplefin

FIXTURES = Path(__file__).parent / "fixtures"
V2 = json.loads((FIXTURES / "simplefin_v2.json").read_text())
ACCESS = "https://bridge-user:bridge-pass@bridge.example/simplefin"
TOKEN_URL = "https://bridge.example/simplefin/claim/synthetic-token"
TOKEN = base64.urlsafe_b64encode(TOKEN_URL.encode()).decode().rstrip("=")


def token_for(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def public(host, port, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class Fake:
    """simplefin.Transport: (method, url, headers, body, timeout) -> Response; every call recorded."""

    def __init__(self, status=200, body=b"", headers=None):
        self.status, self.body, self.headers = status, body if isinstance(body, bytes) else body.encode(), headers or {}
        self.calls: list[tuple[str, str, dict, bytes | None, float]] = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        return simplefin.Response(self.status, self.body)

    def json(self, payload, status=200):
        self.status, self.body = status, json.dumps(payload).encode()
        return self


def claim_ok(fake=None):
    fake = fake or Fake(200, ACCESS)
    url = simplefin.claim(TOKEN, transport=fake, resolve=public)
    return fake, url


# --- claim -------------------------------------------------------------------------------------------

def test_claim_happy_path_posts_the_decoded_url_and_returns_the_access_link():
    fake, url = claim_ok()
    assert url == ACCESS
    (method, called, headers, body, timeout), = fake.calls
    assert method == "POST" and called == TOKEN_URL
    assert body is None and timeout == simplefin.TIMEOUT
    assert "authorization" not in {k.lower() for k in headers}
    assert "bridge-pass" not in called


def test_claim_403_says_the_token_was_already_used_or_revoked():
    fake = Fake(403, "Forbidden (was it already claimed?)")
    with pytest.raises(simplefin.InvalidToken) as e:
        simplefin.claim(TOKEN, transport=fake, resolve=public)
    text = str(e.value)
    assert "already used" in text and "SimpleFIN Bridge" in text
    assert TOKEN not in text and ACCESS not in text


@pytest.mark.parametrize("token", ["", "   ", "not base64!", "a" * 4097, None, 42])
def test_claim_rejects_a_malformed_token_before_any_request(token):
    fake = Fake()
    with pytest.raises(simplefin.InvalidToken):
        simplefin.claim(token, transport=fake, resolve=public)
    assert fake.calls == []


@pytest.mark.parametrize("decoded", [
    "http://bridge.example/claim/x",                       # not https
    "https://bridge.example/claim/x?next=elsewhere",       # query
    "https://bridge.example/claim/x#fragment",             # fragment
    "https://user:pass@bridge.example/claim/x",            # credentials in the setup URL
    "ftp://bridge.example/claim/x",
    "not a url",
])
def test_claim_rejects_a_decoded_url_that_is_not_an_https_claim_url(decoded):
    fake = Fake()
    with pytest.raises(simplefin.InvalidToken):
        simplefin.claim(token_for(decoded), transport=fake, resolve=public)
    assert fake.calls == []


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "10.0.0.7", "169.254.169.254", "[::1]"])
def test_claim_refuses_a_host_that_is_not_public(host):
    fake = Fake()
    with pytest.raises(simplefin.InvalidToken):
        simplefin.claim(token_for(f"https://{host}/claim/x"), transport=fake, resolve=public)
    assert fake.calls == []


def test_claim_resolves_at_connect_time_and_every_address_must_be_global():
    def mixed(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))]
    fake = Fake(200, ACCESS)
    with pytest.raises(simplefin.InvalidToken):
        simplefin.claim(TOKEN, transport=fake, resolve=mixed)
    assert fake.calls == []


def test_claim_refuses_to_follow_a_redirect():
    fake = Fake(302, "https://elsewhere.example/claim/x")
    with pytest.raises(simplefin.SimpleFinError) as e:
        simplefin.claim(TOKEN, transport=fake, resolve=public)
    assert "redirect" in str(e.value).lower() and ACCESS not in str(e.value)


def test_claim_caps_the_response_and_never_echoes_it():
    fake = Fake(200, "x" * 5000)
    with pytest.raises(simplefin.InvalidToken) as e:
        simplefin.claim(TOKEN, transport=fake, resolve=public)
    assert "5000" not in str(e.value)


def test_claim_network_failure_is_a_transport_error_without_the_url():
    def boom(*a, **k):
        raise OSError(f"failed to reach {TOKEN_URL}")
    with pytest.raises(simplefin.TransportError) as e:
        simplefin.claim(TOKEN, transport=boom, resolve=public)
    assert TOKEN_URL not in str(e.value) and "bridge.example" not in str(e.value)


# --- the access URL -----------------------------------------------------------------------------------

def test_parse_access_url_splits_the_credentials_out_of_the_url():
    access = simplefin.parse_access_url(ACCESS, resolve=public)
    assert access.base == "https://bridge.example/simplefin"
    assert access.base.count("@") == 0 and "bridge-pass" not in access.base
    assert access.auth.startswith("Basic ")
    assert base64.b64decode(access.auth.split()[1]).decode() == "bridge-user:bridge-pass"
    assert "bridge-pass" not in repr(access) and "bridge-user" not in repr(access)


@pytest.mark.parametrize("url", [
    "https://bridge.example/simplefin",                     # no credentials
    "https://bridge-user@bridge.example/simplefin",         # no password
    "http://bridge-user:pass@bridge.example/simplefin",     # not https
    "https://bridge-user:pass@bridge.example/simplefin?x=1",
    "https://bridge-user:pass@bridge.example/simplefin#x",
    "https://bridge-user:pass@127.0.0.1/simplefin",
    "https://bridge-user:pass@localhost/simplefin",
    "https://bridge-user:pass@169.254.169.254/latest/meta-data",
    "https://bridge-user:pass@[::1]/simplefin",
])
def test_parse_access_url_refuses_anything_that_is_not_a_public_https_url(url):
    with pytest.raises(simplefin.InvalidToken):
        simplefin.parse_access_url(url, resolve=public)


def test_parse_access_url_refuses_a_name_that_resolves_to_a_private_address():
    def private(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", port))]
    with pytest.raises(simplefin.InvalidToken):
        simplefin.parse_access_url(ACCESS, resolve=private)


# --- fetching -----------------------------------------------------------------------------------------

def client(fake=None, resolve=public):
    fake = fake or Fake(200, json.dumps(V2))
    return simplefin.Client(ACCESS, transport=fake, resolve=resolve), fake


def test_accounts_asks_for_version_2_with_basic_auth_and_no_credentials_in_the_url():
    c, fake = client()
    c.accounts(date(2026, 8, 1), date(2026, 10, 29), pending=True)
    (method, url, headers, body, timeout), = fake.calls
    assert method == "GET" and body is None
    assert url.startswith("https://bridge.example/simplefin/accounts?")
    assert "bridge-pass" not in url and "@" not in url
    assert "version=2" in url and "pending=1" in url and "start-date=" in url and "end-date=" in url
    assert headers["Authorization"].startswith("Basic ")
    assert timeout == simplefin.TIMEOUT


def test_accounts_402_and_403_are_disconnected_never_empty_data():
    for status, reason in ((402, "subscription"), (403, "revoked")):
        fake = Fake(status, json.dumps({"errlist": [{"code": "gen.auth", "msg": "Forbidden"}]}))
        c, _ = client(fake)
        with pytest.raises(simplefin.Disconnected) as e:
            c.accounts(date(2026, 8, 1), date(2026, 10, 29))
        assert e.value.reason == reason
        assert e.value.errors and e.value.errors[0].message == "Forbidden"


def test_a_403_errlist_is_parsed_and_sanitized():
    fake = Fake(403, json.dumps({"errlist": [
        {"code": "gen.auth", "msg": "Forbidden: bad credentials at https://bridge.example/simplefin?user=secret"},
        {"code": "gen.api", "msg": "developer-only notice"}]}))
    c, _ = client(fake)
    with pytest.raises(simplefin.Disconnected) as e:
        c.accounts(date(2026, 8, 1), None)
    joined = " ".join(m.message for m in e.value.errors)
    assert "https://" not in joined and "bridge.example" not in joined
    assert any(m.code == "gen.api" and m.user is False for m in e.value.errors)


def test_partial_errlist_is_sanitized_and_gen_api_is_not_a_connection_break():
    payload = {"errlist": [{"code": "act.failed", "msg": "Account\nneeds\ta look at https://bank.example/x"},
                           {"code": "gen.api", "msg": "Requester error " + "x" * 500}],
               "connections": V2["connections"], "accounts": V2["accounts"]}
    fake = Fake(200, json.dumps(payload))
    c, _ = client(fake)
    got = c.accounts(date(2026, 8, 1), None)
    assert [e.code for e in got.errors] == ["act.failed", "gen.api"]
    assert "https://" not in got.errors[0].message and "\n" not in got.errors[0].message
    assert len(got.errors[1].message) <= simplefin.MAX_ERROR_CHARS
    assert got.errors[1].user is False and got.errors[0].user is True
    assert len(got.accounts) == 3                      # a partial errlist never throws the data away


def test_a_quota_errlist_stops_the_sync_instead_of_retrying():
    payload = {"errlist": [{"code": "gen", "msg": "Request rate limit exceeded; try again tomorrow"}],
               "connections": [], "accounts": []}
    c, _ = client(Fake(200, json.dumps(payload)))
    with pytest.raises(simplefin.Disconnected) as e:
        c.accounts(date(2026, 8, 1), None)
    assert e.value.reason == "quota"


def test_invalid_json_is_an_error_not_a_silent_empty_set():
    c, _ = client(Fake(200, b"<html>nope</html>"))
    with pytest.raises(simplefin.SimpleFinError):
        c.accounts(date(2026, 8, 1), None)


# --- normalizing --------------------------------------------------------------------------------------

def test_normalize_joins_connections_and_keeps_the_v2_shape():
    got = simplefin.normalize(V2)
    assert set(got.connections) == {"CON-SYNTH-1", "CON-SYNTH-2"}
    assert got.connections["CON-SYNTH-1"].name == "Synthetic Bank"
    by_id = {a.id: a for a in got.accounts}
    chk = by_id["ACT-SYNTH-CHK"]
    assert (chk.conn_id, chk.connection_name, chk.currency) == ("CON-SYNTH-1", "Synthetic Bank", "USD")
    assert chk.balance_cents == 120025 and chk.available_cents == 115025
    assert [t.id for t in chk.transactions] == ["TX-SYNTH-1", "TX-SYNTH-2", "TX-SYNTH-PENDING"]
    # SimpleFIN: positive = money in. Our Plaid sign: positive = money out: negate.
    assert [t.amount_cents for t in chk.transactions] == [-250000, 4000, 1234]
    assert [t.pending for t in chk.transactions] == [False, False, True]
    assert by_id["ACT-SYNTH-SAV"].has_holdings is True and by_id["ACT-SYNTH-CHK"].has_holdings is False


def test_the_fixture_uses_simplefins_sign_and_the_client_negates_every_row():
    """The fixture must read like the protocol (positive = money in), not like the app's Plaid sign."""
    raw = {t["id"]: t["amount"] for account in V2["accounts"] for t in account["transactions"]}
    assert raw == {"TX-SYNTH-1": "2500.00", "TX-SYNTH-2": "-40.00", "TX-SYNTH-PENDING": "-12.34",
                   "TX-SYNTH-3": "-75.50"}
    got = simplefin.normalize(V2)
    for account in got.accounts:
        for t in account.transactions:
            assert t.amount_cents == -simplefin.cents(raw[t.id])
    by_id = {t.id: t.amount_cents for account in got.accounts for t in account.transactions}
    assert by_id["TX-SYNTH-1"] == -250000          # a paycheck is money in, so the stored sign is negative
    assert by_id["TX-SYNTH-2"] == 4000             # a grocery charge is money out


def test_unknown_extra_keys_are_ignored():
    payload = json.loads(json.dumps(V2))
    payload["accounts"][0]["extra"] = {"account-open-date": "2020-01-01", "anything": [1, 2]}
    payload["accounts"][0]["transactions"][0]["mcc"] = "5812"
    payload["accounts"][0]["transactions"][0]["memo"] = "note"
    got = simplefin.normalize(payload)
    assert {a.id for a in got.accounts} == {"ACT-SYNTH-CHK", "ACT-SYNTH-SAV", "ACT-SYNTH-CARD"}


def test_a_missing_accounts_list_is_an_unexpected_format():
    with pytest.raises(simplefin.SimpleFinError):
        simplefin.normalize({"connections": []})


# --- amounts ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,cents", [
    ("0", 0), ("0.00", 0), ("-0.01", -1), ("0.01", 1), ("12.3", 1230), ("12.34", 1234),
    ("-1234.56", -123456), ("115385.51", 11538551), ("-95.50", -9550), ("1e2", 10000),
])
def test_amounts_convert_from_decimal_strings_without_float_drift(text, cents):
    assert simplefin.cents(text) == cents


def test_amounts_property_random_decimal_strings_round_trip_exactly():
    import random
    rng = random.Random(20261115)
    for _ in range(500):
        whole = rng.randrange(0, 10**9)
        frac = rng.choice(["", ".0", ".00", ".5", ".05", ".50", ".99", ".1", ".9"])
        sign = rng.choice(["", "-"])
        text = f"{sign}{whole}{frac}"
        d = Decimal(text)
        assert simplefin.cents(text) == int(d * 100)
        assert simplefin.cents(text) == simplefin.cents(f"{text}")   # deterministic
        assert simplefin.money(simplefin.cents(text)) == str(d.quantize(Decimal("0.01")))


def test_money_renders_cents_as_a_plain_two_decimal_string():
    assert simplefin.money(0) == "0.00"
    assert simplefin.money(-1) == "-0.01"
    assert simplefin.money(123456) == "1234.56"
    assert simplefin.money(-123456) == "-1234.56"


# --- type guesses -------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,holdings,balance,available,want", [
    ("Synthetic Savings", False, 100000, 100000, ("depository", "savings")),
    ("Everyday Checking", False, 100000, 100000, ("depository", "checking")),
    ("Synthetic Visa", False, -45075, 454925, ("credit", "credit card")),
    ("Home Mortgage", False, -25000000, None, ("loan", "mortgage")),
    ("Student Loan", False, -1000000, None, ("loan", "student")),
    ("Auto Loan", False, -1500000, None, ("loan", "auto")),
    ("Brokerage Account", True, 500000, None, ("investment", "brokerage")),
    ("Mystery Account", False, 100000, None, ("other", None)),
])
def test_guess_type_is_conservative_and_uses_holdings_name_and_sign(name, holdings, balance, available, want):
    kind, subtype, confidence = simplefin.guess_type(name, holdings, balance, available)
    assert (kind, subtype) == want
    assert confidence in ("high", "medium", "low")
    if want == ("other", None):
        assert confidence == "low"


def test_holdings_make_an_investment_high_confidence_and_unknown_names_low():
    assert simplefin.guess_type("Robinhood", True, 1, None)[2] == "high"
    assert simplefin.guess_type("Account 9942", False, 1, None) == ("other", None, "low")


# --- secrets ------------------------------------------------------------------------------------------

def test_token_and_access_url_never_reach_logs_or_exceptions(caplog):
    caplog.set_level(logging.DEBUG)
    redact.register(ACCESS)
    fake = Fake(200, ACCESS)
    url = simplefin.claim(TOKEN, transport=fake, resolve=public)
    c = simplefin.Client(url, transport=Fake(403, json.dumps({"errlist": [{"code": "gen.auth",
                                                                          "msg": f"Forbidden {ACCESS}"}]})),
                         resolve=public)
    with pytest.raises(simplefin.Disconnected) as e:
        c.accounts(date(2026, 8, 1), None)
    haystack = "\n".join([caplog.text, str(e.value), repr(e.value), repr(c)])
    assert "bridge-pass" not in haystack
    assert "bridge-pass" not in caplog.text
    assert TOKEN not in haystack and url not in haystack    # the access URL is a secret even in a repr

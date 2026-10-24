"""Env parsing, redaction, and the Plaid client request/response contract."""

from __future__ import annotations

import json

import pytest

from conftest import FAKE_CLIENT_ID, FAKE_SECRET, err
from hermes_finance import config, redact
from hermes_finance.plaid import PlaidClient, PlaidError


def test_parse_env_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# comment\n\nPLAID_CLIENT_ID=abc\nexport PLAID_SECRET=\"s3cr3t=x\"\nPLAID_ENV='production'\n"
                 "OTHER=1\nnot a line\n")
    values = config.parse_env_file(f)
    assert values["PLAID_CLIENT_ID"] == "abc"
    assert values["PLAID_SECRET"] == "s3cr3t=x"
    assert values["PLAID_ENV"] == "production"


def test_load_settings_errors(tmp_path):
    with pytest.raises(config.ConfigError, match="not found"):
        config.load_settings(tmp_path / "missing")
    f = tmp_path / ".env"
    f.write_text("PLAID_CLIENT_ID=abc\n")
    with pytest.raises(config.ConfigError, match="PLAID_SECRET"):
        config.load_settings(f)


def test_redact_scrub():
    redact.register("my-registered-secret-value")
    text = "x my-registered-secret-value y access-production-1234-abcd z public-sandbox-99"
    out = redact.scrub(text)
    assert "my-registered-secret-value" not in out
    assert "access-production-1234" not in out and "public-sandbox-99" not in out


def test_client_sends_credentials_in_body_and_parses_errors(fake):
    client = PlaidClient(config.Settings(FAKE_CLIENT_ID, FAKE_SECRET, "production"), transport=fake)
    fake.on("/accounts/get", err("ITEM_LOGIN_REQUIRED"))
    with pytest.raises(PlaidError) as e:
        client.accounts_get("access-production-x")
    assert e.value.error_code == "ITEM_LOGIN_REQUIRED" and e.value.is_item_error and e.value.status == 400
    # FakePlaid itself asserts client_id/secret were in the body and strips them from the record
    assert fake.calls == [("/accounts/get", {"access_token": "access-production-x"})]


def test_sandbox_endpoints_are_forbidden(fake):
    client = PlaidClient(config.Settings(FAKE_CLIENT_ID, FAKE_SECRET, "production"), transport=fake)
    with pytest.raises(RuntimeError, match="forbidden"):
        client.post("/sandbox/public_token/create", {})
    assert fake.calls == []


def test_non_json_error_response():
    client = PlaidClient(config.Settings(FAKE_CLIENT_ID, FAKE_SECRET, "production"),
                         transport=lambda *a: (502, b"<html>bad gateway</html>"))
    with pytest.raises(PlaidError, match="HTTP_502"):
        client.institutions_get()


def test_transactions_sync_request_shape(fake):
    client = PlaidClient(config.Settings(FAKE_CLIENT_ID, FAKE_SECRET, "production"), transport=fake)
    client.transactions_sync("access-production-x", None)
    client.transactions_sync("access-production-x", "cur")
    first, second = fake.bodies("/transactions/sync")
    assert "cursor" not in first and second["cursor"] == "cur"
    assert first["count"] == 500 and json.dumps(first["options"]) == '{"include_original_description": true}'

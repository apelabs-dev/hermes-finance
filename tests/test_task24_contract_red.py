"""Standalone route-contract regression: collects on deb0246 too."""
import json
from hermes_finance import config
from hermes_finance.web import App


def test_rules_get_exists_on_an_initialized_database(conn):
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    status, _, body, _ = app.respond("GET", "/api/rules", {"Host": "127.0.0.1:8765"}, b"")
    assert status == 200
    assert json.loads(body) == {"rules": [], "counts": {"total": 0, "user": 0, "ai": 0}, "suggestions": []}

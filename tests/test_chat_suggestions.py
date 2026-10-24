"""Deterministic Ask suggestion regression: synthetic API fixtures in Node, no browser, DB or model calls.

Also pins the /api/overview shape the chips read (month, category spend/cap/due/status, upcoming bills), so a
backend change fails here instead of silently blanking the suggestions in a browser nobody runs in this test."""
from pathlib import Path
import shutil
import subprocess

from agent_tools import TODAY, build
from hermes_finance import api, config, db


def test_contextual_chat_suggestions():
    node = shutil.which("node")
    assert node, "Node is required for the no-browser Ask UI regression"
    script = Path(__file__).with_name("chat_suggestions.cjs")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "8 scenario groups passed" in result.stdout


def test_overview_payload_contract_for_ask_chips(data_dir):
    build("full", config.db_path())
    conn = db.connect(config.db_path())
    month = "2026-09"
    payload = api.overview(conn, {"month": month}, TODAY)
    conn.close()
    assert payload["month"] == month
    assert payload["today"] == TODAY.isoformat()
    assert payload["categories"]
    for c in payload["categories"]:
        assert isinstance(c["category"], str) and c["category"]
        assert isinstance(c["spend"], (int, float))
        assert c["cap"] is None or isinstance(c["cap"], (int, float))
        assert isinstance(c["due"], (int, float))
        assert c["status"] in {"ok", "warn", "over", "nocap"}
    assert isinstance(payload["upcoming"], list)
    for b in payload["upcoming"]:
        assert b["kind"] in {"bill", "subscription"}
        assert isinstance(b["next_expected"], str) and len(b["next_expected"]) == 10

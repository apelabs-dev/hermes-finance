"""Shared currency formatting and amount-column layout on synthetic scenes."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

import demo_data
from cdp import Chrome, Server, find_chrome
from hermes_finance import db

APP = Path(__file__).resolve().parents[1] / "src/hermes_finance/static/app.js"


def formatted(locale, currency, expression):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = APP.read_text()
    source = source[source.index("  const LOC ="):source.index("  const pct =")]
    harness = "const document = {documentElement: {dataset: " + json.dumps({"locale": locale, "currency": currency}) + "}};\n"
    harness += "const h = (tag, props, ...kids) => kids.join('');\n" + source
    result = subprocess.run([node, "-e", harness + f"\nconsole.log(JSON.stringify({expression}));"],
                            capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


def test_money_uses_whole_dollars_cents_ascii_signs_and_rounded_zero():
    values = [12, 12.5, -12, -12.5, 0, -0.001, 12.999, -12.999, "1234.50", None]
    expected = ["$12", "$12.50", "-$12", "-$12.50", "$0", "$0", "$13", "-$13", "$1,234.50", "$0"]
    expression = json.dumps(values) + ".map(v => [money2(v), splitMoney(v).join('')])"
    assert formatted("en-US", "USD", expression) == [[s, s] for s in expected]
    assert formatted("en-US", "USD", "[money(-0.1), signed(-12), signed(12), splitMoney(12, true).join(''), kf(-1200), axisMoney(-12.5, 0, [{value: 0}, {value: 0.25}])]") == [
        "$0", "+$12", "$12", "-$12", "-$1.2k", "-$12.50"]
    assert formatted("en-US", "USD", "[billAmt({amount: 12}), usualText({low: -12, high: -12}), usualText({low: 100, high: 150})]") == ["$12", "-$12", "$100–150"]


@pytest.mark.parametrize("locale,currency,expected", [
    ("de-DE", "EUR", ["12\u00a0€", "-12,50\u00a0€", "0\u00a0€"]),
    ("ja-JP", "JPY", ["￥12", "-￥13", "￥0"]),
    ("en-US", "KWD", ["KWD\u00a012", "-KWD\u00a012.500", "KWD\u00a00"]),
])
def test_currency_position_precision_and_hero_match(locale, currency, expected):
    assert formatted(locale, currency, "[12, -12.5, 0].map(v => [money2(v), splitMoney(v).join('')])") == [[s, s] for s in expected]


@pytest.fixture(scope="module")
def number_site(tmp_path_factory):
    path = tmp_path_factory.mktemp("number-layout") / "finance.db"
    conn = db.connect(path)
    demo_data.build(conn, today=date.today())
    demo_data.extras(conn, today=date.today())
    conn.close()
    site = Server(path, today=date.today())
    try:
        yield site
    finally:
        site.close()


@pytest.fixture(scope="module")
def number_chrome():
    if find_chrome() is None:
        pytest.skip("Chrome is not installed")
    with Chrome() as chrome:
        yield chrome


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("page", ["overview", "transactions", "budgets", "recurring", "accounts", "investments", "debt"])
def test_amount_columns_align_without_page_overflow(number_site, number_chrome, width, page):
    b = number_chrome
    b.size(width, 900 if width == 1280 else 844)
    b.goto(f"{number_site.url}/?numbers={width}#/{page}")
    b.settle()
    rows = b.js("""[...document.querySelectorAll('#page .amt, #page .num, #page .gtot, #page .bal, #page .li > .v')]
      .filter(e => e.getClientRects().length && e.textContent.includes('$'))
      .map(e => ({text: e.textContent, align: getComputedStyle(e).textAlign,
        numeric: getComputedStyle(e).fontVariantNumeric}))""")
    if page != "budgets":
        assert rows, page
    assert all(r["align"] == "right" and "tabular-nums" in r["numeric"] for r in rows), rows
    assert not any("−$" in r["text"] or ".00" in r["text"] for r in rows), rows
    assert b.js("document.documentElement.scrollWidth <= innerWidth + 1"), (width, page)
    assert b.problems() == []

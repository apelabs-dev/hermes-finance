"""The daily AI classification prompt (the only categorise instruction text) tells the agent a refund is never Income."""

from __future__ import annotations

from pathlib import Path

DAILY = Path(__file__).resolve().parents[1] / "hermes_plugin" / "crons" / "prompts" / "daily.md"


def test_the_daily_prompt_says_a_refund_is_never_income():
    text = " ".join(DAILY.read_text().lower().split())
    for words in ("refund, return, reversal or card credit", "that purchase's category, or other", "never income"):
        assert words in text, words

"""Optional installed WebKit smoke check; missing binaries skip, layout regressions fail."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from webkit_shots import WebKitUnavailable, capture


@pytest.mark.webkit
def test_webkit_phone_budgets_and_transactions(tmp_path):
    try:
        reports = capture(tmp_path)
    except WebKitUnavailable as exc:
        pytest.skip(str(exc))
    assert len(reports) == 4
    assert len(list(tmp_path.glob("webkit-*.png"))) == 4

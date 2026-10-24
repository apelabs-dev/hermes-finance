"""Release identity and a stranger's explicit, key-free checkout prerequisite."""

import importlib.util
from pathlib import Path
import re
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("release_identity", ["package", "mcp"])
def test_beta_release_versions_agree(tmp_path, release_identity):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    package = next(p for p in lock["package"] if p["name"] == project["name"])
    manifest = (ROOT / "hermes_plugin" / "plugin.yaml").read_text()
    version = re.search(r"^version: ([^\s]+)", manifest, re.M).group(1)
    assert project["version"] == package["version"] == version
    if release_identity == "package":
        import hermes_finance

        assert hermes_finance.__version__ == project["version"]
    else:
        from hermes_finance.mcp_server import build_server

        assert build_server(tmp_path / "finance.db", "stdio").version == project["version"]
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert re.search(r"^## 0\.2\.0\b", changelog, re.M)
    assert "beta" in changelog.lower()


def test_missing_app_explains_checkout_and_doctor(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("finance_plugin", ROOT / "hermes_plugin" / "__init__.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    monkeypatch.setattr(plugin, "PLUGIN_DIR", tmp_path / "installed-plugin")
    monkeypatch.delenv("HERMES_FINANCE_APP_DIR", raising=False)

    def missing(name):
        assert name == "hermes_finance.tooldefs"
        raise ImportError("no app package in this profile")

    monkeypatch.setattr(plugin.importlib, "import_module", missing)
    class Context:
        def get_config(self, key):
            return None

    with pytest.raises(ImportError) as error:
        plugin.register(Context())
    message = str(error.value)
    assert "checkout" in message and "HERMES_FINANCE_APP_DIR" in message
    assert "uv sync" in message and "finance doctor" in message
    assert "Install hermes-finance in Hermes's Python" not in message


def test_readme_plugin_install_is_explicit_and_key_free():
    text = (ROOT / "README.md").read_text()
    section = text.split("## Install as a Hermes plugin\n", 1)[1].split("\n## ", 1)[0]
    for command in ('export HERMES_FINANCE_APP_DIR="$PWD"', "uv sync", "uv run finance doctor",
                    "hermes plugins validate hermes_plugin", "hermes plugins doctor --ci hermes_plugin",
                    "hermes plugins enable hermes-finance", "#hermes_plugin"):
        assert command in section
    assert "No bank or API key" in section
    assert "checkout" in section and "approval" in section

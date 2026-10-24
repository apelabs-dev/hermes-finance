# Changelog

## 0.2.0

Beta release. The standalone Hermes plugin requires a trusted app checkout prepared
with Python 3.12 and `uv sync`; set `HERMES_FINANCE_APP_DIR` to its root. Installation
and local reads need no bank or API key. `uv run finance doctor` explains optional
integration setup. The plugin adapter remains compatible with Python 3.11.

Release versions now agree across the app, plugin manifest and lockfile. Catalog
preparation remains gated on a public export, an immutable pin and policy review.

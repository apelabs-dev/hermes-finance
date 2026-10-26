# Changelog

## [0.2.2](https://github.com/arspesk/hermes-finance/compare/v0.2.1...v0.2.2) (2026-10-26)


### Bug Fixes

* macOS Ask/lookup crash fixes, plugin credentials through Hermes, setup and card-payment fixes, motion and alert layout ([2ffc430](https://github.com/arspesk/hermes-finance/commit/2ffc43095765debb1e389eae1299903fd1da0e3b))
* **release:** mask the repository's own URL in the release scan ([2c3c130](https://github.com/arspesk/hermes-finance/commit/2c3c130d9958b9221bc587d7150c3458ec62508e))

## [0.2.1](https://github.com/arspesk/hermes-finance/compare/v0.2.0...v0.2.1) (2026-10-25)


### Bug Fixes

* first-run fixes from the clean-machine acceptance run ([df2aafb](https://github.com/arspesk/hermes-finance/commit/df2aafb55c059a8f0b2c71a3a02f1fb1d3b0e47e))
* let categories under a group budget get a budget of their own ([5c1e5b7](https://github.com/arspesk/hermes-finance/commit/5c1e5b744a18b2f6538cec69de7fea0d634a01d3))

## 0.2.0

Beta release. The standalone Hermes plugin requires a trusted app checkout prepared
with Python 3.12 and `uv sync`; set `HERMES_FINANCE_APP_DIR` to its root. Installation
and local reads need no bank or API key. `uv run finance doctor` explains optional
integration setup. The plugin adapter remains compatible with Python 3.11.

Release versions now agree across the app, plugin manifest and lockfile. Catalog
preparation remains gated on a public export, an immutable pin and policy review.

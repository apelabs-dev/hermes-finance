# Changelog

## [0.2.4](https://github.com/arspesk/hermes-finance/compare/v0.2.3...v0.2.4) (2026-10-26)


### Bug Fixes

* **check:** a nested layout gate cannot deadlock the browser workers ([2c4ea5e](https://github.com/arspesk/hermes-finance/commit/2c4ea5ee2c25fae55d69445f2c0db85e825e73fd))
* **demo:** car and landmark pages show a real photo and their estimates ([3194312](https://github.com/arspesk/hermes-finance/commit/3194312e70562ece006679082c86e002d597ab8c))
* **demo:** photo file names match the parity check ([6dedaf7](https://github.com/arspesk/hermes-finance/commit/6dedaf733c1d3007d14e957d82bb0eb56bb58168))
* **demo:** real stock holdings show their logo ([5245008](https://github.com/arspesk/hermes-finance/commit/5245008380f716f55a41dd342afa0d41d3b010e7))
* **test:** the trickle deadline test gives its first step time ([efa0a19](https://github.com/arspesk/hermes-finance/commit/efa0a199ca164275aea343b4e98ea67d44e0f53f))
* **test:** two timing assumptions a slow CI runner broke ([debe8c1](https://github.com/arspesk/hermes-finance/commit/debe8c1464814bcd7394dcea159e78c6c26c8c1a))
* **test:** unknown-state wait test fits a slow runner ([436c5d4](https://github.com/arspesk/hermes-finance/commit/436c5d417630239c415c734b39b40e423465ad60))
* Transactions month label on phones matches Overview and Budgets ([181930e](https://github.com/arspesk/hermes-finance/commit/181930eaa99da504a0a9383c2de0358eeee9b890))
* user-filed transaction rows, Nightly suite on the macOS runner ([8dbca13](https://github.com/arspesk/hermes-finance/commit/8dbca13fa97ca540226720d24b7a6ea38e36b33d))

## [0.2.3](https://github.com/arspesk/hermes-finance/compare/v0.2.2...v0.2.3) (2026-10-26)


### Bug Fixes

* Transactions month pill matches the other pages; demo v2 build, live-AI worker and weekly rebuild ([e31162d](https://github.com/arspesk/hermes-finance/commit/e31162d9945d72fa31608a1e3bcc3b3d9be4fc8f))

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

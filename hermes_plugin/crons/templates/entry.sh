#!/usr/bin/env bash
# hermes-finance cron entry script: @@ROLE@@ (version @@VERSION@@)
# Installed by `hermes finance crons apply` into this Hermes profile's scripts directory. A later apply replaces it
# (keeping this copy in .backup/) and records its sha256 in the plugin's state; edit the plugin's template instead.
# It runs the finance app's own pre-run steps (hermes_finance.cronpipe) with the app's own Python and prints a short
# summary for the @@ROLE@@ report job. Its artifacts (queue, alerts, snapshots) are private to this user.
set -uo pipefail
APP_DIR=@@APP_DIR@@
APP_PYTHON=@@APP_PYTHON@@
ARTIFACTS=@@ARTIFACTS@@
@@DATA_DIR_LINE@@
unset PYTHONHOME
# Credentials come from the environment Hermes runs this job with; the app reads no key file.
export HERMES_FINANCE_CREDENTIAL_SOURCE=hermes
export PYTHONPATH="$APP_DIR/src" PYTHONDONTWRITEBYTECODE=1
cd "$APP_DIR" || { echo "PRE-RUN ERROR: the finance app directory is missing: $APP_DIR"; exit 0; }
if [ ! -x "$APP_PYTHON" ]; then
  echo "PRE-RUN ERROR: the finance app's Python is missing: $APP_PYTHON (run uv sync in $APP_DIR)"
  exit 0
fi
exec "$APP_PYTHON" -m hermes_finance.cronpipe @@ROLE@@ --artifacts "$ARTIFACTS"

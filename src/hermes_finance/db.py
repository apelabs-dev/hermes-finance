"""SQLite schema + connections. Designed to be queried directly by an AI agent.

Sign convention (Plaid): transactions.amount > 0 means money LEFT the account (purchase, fee,
payment out); amount < 0 means money CAME IN (paycheck, refund, deposit). This holds for every
account type, including credit cards (a purchase is positive, a card payment is negative).
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA_VERSION = 19  # 2: balance_snapshots; 3: ai_runs, ai_run_groups, ai_reviews;
                     # 4: manual_accounts, manual_balance_history, loan_payments, holding_snapshots;
                     # 5: institutions, security_logos; 6: settings;
                     # 7: manual_accounts vin..value_source, asset_valuations, vin_decodes, api_usage, api_calls;
                     # 8: asset_valuations carfax/redfin + details_json, flag, confirmed_at (rebuilt);
                     #    manual_accounts.vehicle_condition; vin_decodes.specs_json;
                     # 9: manual_accounts.linked_loan_id, asset_images; 10: pending_actions;
                     # 11 (TASK-18, Batch A): auth_users, auth_recovery_codes, auth_sessions, auth_failures, mcp_tokens,
                     #    imports; transactions.source / import_id / fitid (_migrate_task18)
                     # 12 (TASK-19, Batch C): transaction_splits, transaction_notes, tags, transaction_tags,
                     #    saved_filters, category_rollover, goals, goal_entries, alpaca_snapshots,
                     #    view v_spend_lines (_task19_schema)
                     # 13 (TASK-21): tracked_bills (_task21_schema)
                     # 14 (TASK-23 review): pending_actions.origin, held_by (ADDED_COLUMNS)
                     # 15 (TASK-24 fix): rule_deletions, classifications.detached_by and its trigger (_task24_schema).
                     #    Both lanes had stamped 14 with different DDL, so 15 is the cumulative step: a database at 14
                     #    from either branch converges here (ADDED_COLUMNS guards + an idempotent _task24_schema).
                     # 16 (polish P0): category_groups; categories.group_id / icon / sort (ADDED_COLUMNS); the 34-category
                     #    taxonomy (CATEGORY_GROUPS) and finer Plaid detailed-code defaults (_v16_schema)
                     # 17 (polish P2d): goals.icon / colour, preserving existing appearance (_v17_schema)
                     # 18 (polish P2e): group_budgets (a monthly budget on a category group), group_budget_kept (the
                     #    groups whose category budgets were kept in the one-time move) (_v18_schema)
                     # 19 (category names): Transport / Shopping / Health -> Other Transport / Other Shopping / Other
                     #    Health and the group Other -> Misc, by id (_v19_rename); new icons, catch-alls last, and the
                     #    Plaid defaults re-filed by today's map (_v19_schema)

# Estimated values of manual vehicles (the Carfax trade-in) and properties (the Redfin Estimate): one row per successful
# lookup, never updated. Rows 'marketcheck' / 'rentcast' (before TASK-13b) stay as history, never shown or used.
ASSET_VALUATIONS = """-- Estimated values of manual vehicles (Carfax trade-in) and properties (Redfin Estimate): one row per lookup. Rows
-- of the providers used before TASK-13b (marketcheck, rentcast) are history: kept, never shown, never a value.
CREATE TABLE IF NOT EXISTS asset_valuations (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES manual_accounts(id),
    value        REAL NOT NULL CHECK (value > 0),   -- Carfax: the trade-in (wholesalePrice)
    low          REAL,                        -- low / high: a range (only the old RentCast rows have one)
    high         REAL,
    source       TEXT NOT NULL CHECK (source IN ('marketcheck', 'rentcast', 'carfax', 'redfin')),
    miles        INTEGER,
    subject      TEXT,                        -- what was valued: the VIN, or the address (lowercase, spaces collapsed);
                                              -- only an estimate for the account's current one is shown or followed
    fetched_at   TEXT NOT NULL,               -- UTC timestamp
    raw_json     TEXT,                        -- the response, trimmed (never a key)
    details_json TEXT,                        -- v8: what the page shows beside the value (Carfax private sale / dealer
                                              -- retail / records / odometer / specs; Redfin beds, baths, sqft, last sold,
                                              -- location, listing photo)
    flag         TEXT CHECK (flag IN ('building_level')),
                                              -- v8, unused since TASK-13b (it flagged RentCast answers)
    confirmed_at TEXT                         -- v8, unused since TASK-13b
);"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id           TEXT PRIMARY KEY,
    institution_id    TEXT,
    institution_name  TEXT,
    access_token      TEXT NOT NULL,          -- secret; reads as NULL through `finance query`
    cursor            TEXT,                   -- /transactions/sync cursor (NULL = never synced)
    status            TEXT NOT NULL DEFAULT 'active',  -- active | login_required | error
    error_code        TEXT,                   -- last Plaid error_code, NULL when healthy
    error_message     TEXT,
    products          TEXT,                   -- JSON array of products enabled on the Item
    txn_update_status TEXT,                   -- Plaid transactions_update_status
    created_at        TEXT NOT NULL,          -- when we exchanged it (= a Trial slot was used)
    last_sync_at      TEXT,
    removed_at        TEXT                    -- rows are NEVER deleted; removed Items keep their slot
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id        TEXT PRIMARY KEY,
    item_id           TEXT NOT NULL REFERENCES items(item_id),
    name              TEXT,
    official_name     TEXT,
    mask              TEXT,                   -- last 2-4 digits
    type              TEXT,                   -- depository | credit | loan | investment | other
    subtype           TEXT,                   -- checking | savings | credit card | ...
    current_balance   REAL,
    available_balance REAL,
    credit_limit      REAL,
    iso_currency      TEXT,
    updated_at        TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id         TEXT PRIMARY KEY,
    account_id             TEXT NOT NULL REFERENCES accounts(account_id),
    item_id                TEXT NOT NULL REFERENCES items(item_id),
    date                   TEXT NOT NULL,     -- YYYY-MM-DD (posted date, or pending date)
    authorized_date        TEXT,
    name                   TEXT,
    merchant_name          TEXT,
    amount                 REAL NOT NULL,     -- Plaid sign: POSITIVE = money OUT, NEGATIVE = money IN
    iso_currency           TEXT,
    pending                INTEGER NOT NULL DEFAULT 0,
    pending_transaction_id TEXT,
    payment_channel        TEXT,              -- online | in store | other
    pfc_primary            TEXT,              -- Plaid personal_finance_category.primary
    pfc_detailed           TEXT,
    pfc_confidence         TEXT,              -- VERY_HIGH | HIGH | MEDIUM | LOW | UNKNOWN
    logo_url               TEXT,
    website                TEXT,
    location_json          TEXT,
    counterparties_json    TEXT,
    raw_json               TEXT,              -- full Plaid transaction object
    removed                INTEGER NOT NULL DEFAULT 0,  -- 1 = Plaid removed it; row kept
    first_seen_at          TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_transactions_item ON transactions(item_id);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_merchant ON transactions(merchant_name);

CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    parent      INTEGER REFERENCES categories(id),
    monthly_cap REAL,                         -- budget cap in account currency; NULL = no cap
    is_income   INTEGER NOT NULL DEFAULT 0,
    is_transfer INTEGER NOT NULL DEFAULT 0    -- transfers/card payments: exclude from spend totals
);

-- Plaid personal_finance_category code (detailed or primary) -> our category.
-- Lookup order: detailed code, then primary code, then 'Other'.
CREATE TABLE IF NOT EXISTS plaid_category_map (
    pfc_code    TEXT PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id)
);

CREATE TABLE IF NOT EXISTS rules (
    id           INTEGER PRIMARY KEY,
    match_field  TEXT NOT NULL CHECK (match_field IN ('merchant_name', 'name')),
    match_type   TEXT NOT NULL DEFAULT 'exact' CHECK (match_type IN ('exact', 'regex')),
    pattern      TEXT NOT NULL,               -- exact: case-insensitive equality; regex: Python re.search, case-insensitive
    category_id  INTEGER NOT NULL REFERENCES categories(id),
    priority     INTEGER NOT NULL DEFAULT 100, -- higher wins; ties -> lower id
    created_from TEXT,                        -- user | ai | <free text>
    created_at   TEXT NOT NULL,
    UNIQUE (match_field, match_type, pattern)
);

-- One row per transaction. Precedence is enforced on write: user > user rule > ai > ai rule > plaid
-- (source 'rule' covers both kinds of rule; rules.created_from tells them apart).
CREATE TABLE IF NOT EXISTS classifications (
    transaction_id TEXT PRIMARY KEY REFERENCES transactions(transaction_id),
    category_id    INTEGER NOT NULL REFERENCES categories(id),
    source         TEXT NOT NULL CHECK (source IN ('plaid', 'rule', 'ai', 'user')),
    confidence     REAL,
    note           TEXT,
    rule_id        INTEGER REFERENCES rules(id),
    updated_at     TEXT NOT NULL,
    detached_by    INTEGER REFERENCES rule_deletions(id)  -- v15: filed by a rule since deleted (TASK24_SCHEMA)
);
CREATE TRIGGER IF NOT EXISTS trg_classifications_protect_user
BEFORE UPDATE ON classifications
WHEN OLD.source = 'user' AND NEW.source <> 'user'
BEGIN
    SELECT RAISE(ABORT, 'user classifications are never overwritten');
END;

CREATE TABLE IF NOT EXISTS holdings (
    account_id        TEXT NOT NULL,
    security_id       TEXT NOT NULL,
    item_id           TEXT NOT NULL,
    ticker_symbol     TEXT,
    security_name     TEXT,
    security_type     TEXT,
    quantity          REAL,
    institution_price REAL,
    institution_value REAL,
    cost_basis        REAL,
    iso_currency      TEXT,
    raw_json          TEXT,                   -- {"holding": ..., "security": ...}
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (account_id, security_id)
);

CREATE TABLE IF NOT EXISTS liabilities (
    account_id             TEXT NOT NULL,
    kind                   TEXT NOT NULL,     -- credit | mortgage | student
    item_id                TEXT NOT NULL,
    last_statement_balance REAL,
    minimum_payment_amount REAL,
    next_payment_due_date  TEXT,
    last_payment_amount    REAL,
    last_payment_date      TEXT,
    is_overdue             INTEGER,
    interest_rate          REAL,              -- percent (purchase APR for credit cards)
    raw_json               TEXT,
    updated_at             TEXT NOT NULL,
    PRIMARY KEY (account_id, kind)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY,
    item_id     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    added       INTEGER NOT NULL DEFAULT 0,
    modified    INTEGER NOT NULL DEFAULT 0,
    removed     INTEGER NOT NULL DEFAULT 0,
    error       TEXT,                         -- NULL = transactions sync succeeded
    details     TEXT                          -- JSON: balances / liabilities / investments outcome
);

-- One balance row per account per local day, written at the end of every successful sync.
-- Net worth history comes only from these real snapshots (never back-filled).
CREATE TABLE IF NOT EXISTS balance_snapshots (
    account_id   TEXT NOT NULL,
    date         TEXT NOT NULL,               -- local YYYY-MM-DD
    current      REAL,
    available    REAL,
    iso_currency TEXT,
    PRIMARY KEY (account_id, date)
);

-- Daily AI classification (`finance ai-queue` / `finance ai-apply`). The app never calls an LLM:
-- Hermes reads a queue, decides, and hands the decisions back. One ai_runs row per queue export.
CREATE TABLE IF NOT EXISTS ai_runs (
    run_id        TEXT PRIMARY KEY,           -- ai-YYYYmmdd-HHMMSS (UTC, same instant as created_at)
    created_at    TEXT NOT NULL,
    applied_at    TEXT,                       -- NULL until ai-apply succeeds; a run applies once
    queued_groups INTEGER,
    queued_txns   INTEGER,
    summary_json  TEXT                        -- the ai-apply output
);

-- The queue snapshot: ai-apply may only touch transactions listed here for its run.
CREATE TABLE IF NOT EXISTS ai_run_groups (
    run_id               TEXT REFERENCES ai_runs(run_id),
    group_id             TEXT,                -- g1, g2, ... within the run
    merchant             TEXT,
    rule_field           TEXT,                -- merchant_name | name (what a `rule` decision matches on)
    rule_pattern         TEXT,
    transaction_ids_json TEXT NOT NULL,
    PRIMARY KEY (run_id, group_id)
);

-- One row per transaction the AI has looked at (so each is queued once), from its run's decision.
CREATE TABLE IF NOT EXISTS ai_reviews (
    transaction_id TEXT PRIMARY KEY REFERENCES transactions(transaction_id),
    run_id         TEXT NOT NULL,
    reviewed_at    TEXT NOT NULL,
    verdict        TEXT NOT NULL CHECK (verdict IN ('changed', 'confirmed', 'flagged')),
    category_id    INTEGER REFERENCES categories(id),  -- the category after the review
    confidence     REAL,
    note           TEXT
);

-- Things Plaid can't see, entered by hand (dashboard "Add account" or `finance manual add`).
-- balance is always >= 0: liabilities (credit_card, loan, other_liability) are subtracted by kind.
-- Rows are archived, never deleted (archived rows leave every total).
CREATE TABLE IF NOT EXISTS manual_accounts (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('bank', 'credit_card', 'investment', 'loan', 'vehicle',
                                                  'property', 'other_asset', 'other_liability')),
    name            TEXT NOT NULL,
    institution     TEXT,
    subtype         TEXT,                     -- bank: checking|savings|money_market|cd; investment: brokerage|401k|
                                              -- roth_ira|traditional_ira|hsa|crypto|other; loan: auto|student|
                                              -- personal|mortgage|bnpl|other; free text for other_*
    balance         REAL NOT NULL CHECK (balance >= 0),
    credit_limit    REAL,
    interest_rate   REAL,                     -- APR in percent; NULL = not set
    original_amount REAL,
    monthly_payment REAL,
    match_pattern   TEXT,                     -- loans: case-insensitive substring of transactions name/merchant_name
    vehicle_make    TEXT,
    vehicle_model   TEXT,
    vehicle_year    INTEGER,
    balance_as_of   TEXT NOT NULL,            -- YYYY-MM-DD the entered balance was true
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT,
    -- v7 (TASK-12, automatic valuation). Added at the end so an upgraded table matches a new one.
    vin             TEXT,                     -- vehicles: 17 characters, check digit verified
    vehicle_trim    TEXT,
    mileage         INTEGER,
    mileage_as_of   TEXT,                     -- YYYY-MM-DD the mileage was entered (the 90-day nudge)
    zip_code        TEXT,                     -- vehicles: where the car is (Carfax prices by location)
    address         TEXT,                     -- property: what Redfin looks up
    vin_decoded     TEXT,                     -- the VIN last decoded by NHTSA (decoded once per VIN)
    value_source    TEXT CHECK (value_source IN ('user', 'estimate')),
                                              -- vehicle/property: 'user' = the typed value wins ("Your value"),
                                              -- 'estimate' = balance follows the estimate (valuation.effective())
    -- v8 (TASK-13)
    vehicle_condition TEXT CHECK (vehicle_condition IN ('fair', 'good', 'excellent')),
                                              -- vehicles: what Carfax prices (carfax.com/value's own list); 'good'
    -- v9 (TASK-13b)
    linked_loan_id  INTEGER REFERENCES manual_accounts(id)
                                              -- vehicle / property: the manual loan it's financed by (its equity)
);

-- Balance of a manual account over time: the entered balance on its balance_as_of day, plus (loans) the
-- balance after each applied payment on the payment's day. Net worth history reads these.
CREATE TABLE IF NOT EXISTS manual_balance_history (
    manual_account_id INTEGER NOT NULL REFERENCES manual_accounts(id),
    date              TEXT NOT NULL,
    balance           REAL NOT NULL,
    PRIMARY KEY (manual_account_id, date)
);

-- A bank transaction applied to a manual loan or credit card (loan_id: the manual account; at most one, once). Only
-- transactions dated after its balance_as_of move its balance. interest/principal: the split at that point (APR not
-- set, or a card: all principal).
CREATE TABLE IF NOT EXISTS loan_payments (
    transaction_id TEXT PRIMARY KEY REFERENCES transactions(transaction_id),
    loan_id        INTEGER NOT NULL REFERENCES manual_accounts(id),
    amount         REAL NOT NULL,
    interest       REAL NOT NULL,
    principal      REAL NOT NULL,
    applied_at     TEXT NOT NULL,
    source         TEXT NOT NULL CHECK (source IN ('auto', 'ai', 'user'))
);
CREATE INDEX IF NOT EXISTS idx_loan_payments_loan ON loan_payments(loan_id);

-- One row per account/security per local day, written after a sync whose holdings call succeeded
-- (the same way as balance_snapshots). The portfolio chart comes only from these (never back-filled).
CREATE TABLE IF NOT EXISTS holding_snapshots (
    date        TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    security_id TEXT NOT NULL,
    ticker      TEXT,
    quantity    REAL,
    price       REAL,
    value       REAL,
    PRIMARY KEY (account_id, security_id, date)
);

-- Bank logos and colours (`finance refresh-institutions`, and at connect time). Plaid's free
-- /institutions/get_by_id first; when it has no logo, the bank's own icon fetched from its site; else
-- nothing (the page draws initials in primary_color) and the daily `finance sync` tries again once a day (TASK-22e;
-- Alpaca's icon too). Served from here at /api/institutions/<id>/logo, never hotlinked. logo_png holds the image
-- bytes (PNG, or the site's JPEG/GIF/WebP/ICO), checked before storing.
CREATE TABLE IF NOT EXISTS institutions (
    institution_id TEXT PRIMARY KEY,
    name           TEXT,
    logo_png       BLOB,
    logo_source    TEXT CHECK (logo_source IN ('plaid', 'site', 'none')),
    primary_color  TEXT,                      -- '#rrggbb' from Plaid
    url            TEXT,
    fetched_at     TEXT
);

-- Company / fund logos for holdings (`finance sync` after holdings, `finance refresh-logos`), fetched once per
-- ticker and served at /api/securities/<ticker>/logo. A miss (png NULL, source 'none') is retried after 30 days.
CREATE TABLE IF NOT EXISTS security_logos (
    ticker     TEXT PRIMARY KEY,
    png        BLOB,
    source     TEXT NOT NULL CHECK (source IN ('fmp', 'none')),
    fetched_at TEXT NOT NULL
);

-- Dashboard preferences that phone and desktop share (api.py SETTINGS: each key's JSON value is validated there).
-- chart_excluded_categories: categories hidden from the "Where it went" chart only; no total ever uses it.
-- networth_excluded (TASK-15): the accounts the Custom net worth leaves out (Plaid account_id / manual:<id>).
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,                 -- JSON
    updated_at TEXT NOT NULL
);

{asset_valuations}
CREATE INDEX IF NOT EXISTS idx_asset_valuations_account ON asset_valuations(account_id, fetched_at);

-- NHTSA VIN decodes, so each VIN is decoded once (by any account, on any save or daily run).
CREATE TABLE IF NOT EXISTS vin_decodes (
    vin           TEXT PRIMARY KEY,
    vehicle_year  INTEGER,
    vehicle_make  TEXT,
    vehicle_model TEXT,
    vehicle_trim  TEXT,
    fetched_at    TEXT NOT NULL,
    specs_json    TEXT                        -- v8: the extended decode's specs and safety equipment ({} = none)
);

-- The call guard (usage.py). Calls per provider per local calendar month, counted BEFORE each outbound call in one
-- BEGIN IMMEDIATE transaction (failed, 429 and timed-out calls count). blocked_until: a refusal stops that provider
-- until this local day. Rows of providers no longer used (marketcheck, rentcast before TASK-13b) are kept as history.
CREATE TABLE IF NOT EXISTS api_usage (
    provider       TEXT NOT NULL,             -- nhtsa | carfax | redfin | commons | osm
    month          TEXT NOT NULL,             -- local YYYY-MM
    calls          INTEGER NOT NULL DEFAULT 0,
    blocked_until  TEXT,                      -- local YYYY-MM-DD, NULL = not blocked
    blocked_reason TEXT,
    updated_at     TEXT,
    PRIMARY KEY (provider, month)
);

-- One row per outbound call (reserved before it's made), for the per-account limits (1 on-demand call per account per
-- day, 1 scheduled call per account per 30 days) and the rolling 32-day window, whatever the provider's billing day.
CREATE TABLE IF NOT EXISTS api_calls (
    id         INTEGER PRIMARY KEY,
    provider   TEXT NOT NULL,
    account_id INTEGER,                       -- manual_accounts.id (NULL: the seed rows)
    trigger    TEXT NOT NULL CHECK (trigger IN ('save', 'refresh', 'daily', 'seed')),
    at         TEXT NOT NULL,                 -- UTC timestamp
    day        TEXT NOT NULL,                 -- local YYYY-MM-DD
    status     INTEGER,                       -- HTTP status (NULL: no response)
    outcome    TEXT NOT NULL DEFAULT 'pending',  -- pending | ok | error | timeout | blocked | seed
    note       TEXT                           -- what went wrong, in words (never a key)
);
CREATE INDEX IF NOT EXISTS idx_api_calls_provider ON api_calls(provider, day);
CREATE INDEX IF NOT EXISTS idx_api_calls_account ON api_calls(account_id, provider, day);

-- The picture on a car / property page (photos.py), fetched once in the background, never uploaded: a Wikimedia
-- Commons photo of that year / make / model (background removed when rembg is installed), the home's Redfin listing
-- photo, or an OpenStreetMap tile with a pin. The file is in data/images/ (served at /api/images/<file>).
CREATE TABLE IF NOT EXISTS asset_images (
    key        TEXT PRIMARY KEY,              -- vehicle:<year>|<make>|<model> (shared by every account with that car),
                                              -- or property:<address> (lowercase, spaces collapsed)
    kind       TEXT NOT NULL CHECK (kind IN ('vehicle', 'property')),
    status     TEXT NOT NULL CHECK (status IN ('ready', 'none', 'error')),
                                              -- none: nothing usable (tried again after 30 days); error: tomorrow
    source     TEXT CHECK (source IN ('commons', 'redfin', 'osm')),
    file       TEXT,                          -- in data/images/; NULL: the page draws the silhouette / outline
    cutout     INTEGER NOT NULL DEFAULT 0,    -- 1: background removed (a transparent PNG)
    width      INTEGER,
    height     INTEGER,
    credit     TEXT,                          -- the line under the picture ("Photo: X, CC BY-SA 4.0 · Wikimedia Commons")
    credit_url TEXT,                          -- where it links (the Commons file page, the OSM copyright page)
    artist     TEXT,
    license    TEXT,
    lat        REAL,                          -- osm: the home; pin_x / pin_y: where it is on the tile (0..1)
    lng        REAL,
    pin_x      REAL,
    pin_y      REAL,
    note       TEXT,                          -- what was tried, why there's nothing
    fetched_at TEXT NOT NULL                  -- UTC timestamp
);

-- Changes an MCP client asked for (TASK-17; the write tools are only listed while the mcp.allow_writes setting is on).
-- Recorded, never applied here: each waits for the owner's decision. Rows are never deleted.
CREATE TABLE IF NOT EXISTS pending_actions (
    id         INTEGER PRIMARY KEY,
    tool       TEXT NOT NULL,                 -- finance_set_category | finance_add_rule | finance_set_cap
    arguments  TEXT NOT NULL,                 -- JSON, as validated
    summary    TEXT NOT NULL,                 -- the change in plain words (what the owner approves or rejects)
    client     TEXT,                          -- the MCP client's name (its clientInfo) when the call carries it: over
                                              -- stdio, or the 2026 protocol over HTTP (a stateless 2025 POST doesn't).
                                              -- A display name only, whatever the client says: never who recorded it
    status     TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'approved', 'rejected', 'applied', 'failed')),
    created_at TEXT NOT NULL,                 -- UTC timestamp
    decided_at TEXT,
    result     TEXT,                          -- JSON: what applying it did (or why it failed)
    origin     TEXT NOT NULL DEFAULT 'mcp',   -- v14: who recorded it, set by the code path that did (mcp_state.ORIGINS:
                                              -- mcp the MCP server, ask the Ask panel's turn); rows from before are mcp
    held_by    TEXT                           -- v14: the Ask turn that recorded it, while that turn runs: not listed, not
                                              -- decidable (mcp_state.decide claims only rows with none) until released
);

-- Every Link completion the server saw, including duplicates it refused to exchange.
CREATE TABLE IF NOT EXISTS link_attempts (
    id               INTEGER PRIMARY KEY,
    created_at       TEXT NOT NULL,
    institution_id   TEXT,
    institution_name TEXT,
    outcome          TEXT NOT NULL,           -- exchanged | duplicate_blocked | duplicate_counted | rejected | error
    item_id          TEXT,
    error            TEXT
);
"""

SCHEMA = SCHEMA.replace("{asset_valuations}", ASSET_VALUATIONS)

# TASK-18 (v11). Built-in login (auth.py), MCP bearer tokens and file imports (importer.py). Secrets are never stored
# here in the clear: passwords and recovery codes are argon2id hashes, session and MCP tokens SHA-256 hashes (the
# tokens are 256-bit random). `finance query` reads every one of these columns as NULL (_readonly_authorizer).
TASK18_SCHEMA = """
-- The built-in login's one account (Settings -> Access & security -> Built-in login). 2FA is required: a row exists
-- only once its authenticator app has been confirmed with a code.
CREATE TABLE IF NOT EXISTS auth_users (
    id                  INTEGER PRIMARY KEY,
    username            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash       TEXT NOT NULL,        -- argon2id (PHC string)
    totp_secret         TEXT NOT NULL,        -- base32 (RFC 6238, SHA-1, 30 s, 6 digits)
    totp_last_step      INTEGER NOT NULL DEFAULT 0,  -- the last 30 s step a code was accepted for: a replay is refused
    created_at          TEXT NOT NULL,
    password_changed_at TEXT NOT NULL
);

-- One-time recovery codes (8 per set; a new set replaces the old one).
CREATE TABLE IF NOT EXISTS auth_recovery_codes (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES auth_users(id),
    code_hash  TEXT NOT NULL,                 -- argon2id
    created_at TEXT NOT NULL,
    used_at    TEXT
);

-- Signed-in browsers. The cookie carries a random 256-bit token; only its SHA-256 is kept.
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES auth_users(id),
    csrf         TEXT NOT NULL,               -- sent back in X-CSRF-Token on every state-changing request
    created_at   TEXT NOT NULL,               -- absolute expiry counts from here
    last_seen_at TEXT NOT NULL,               -- idle expiry counts from here
    ip           TEXT,
    user_agent   TEXT
);

-- Wrong sign-in tries: 5 in a row lock that user (key user:<name>) or that address (ip:<address>) for 15 minutes.
CREATE TABLE IF NOT EXISTS auth_failures (
    key          TEXT PRIMARY KEY,
    failures     INTEGER NOT NULL,
    first_at     TEXT NOT NULL,
    locked_until TEXT
);

-- Per-client bearer tokens for /mcp (Settings -> Connect other agents): hfm_ + 32 random bytes, shown once.
CREATE TABLE IF NOT EXISTS mcp_tokens (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,        -- SHA-256 of the token
    last4        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);

-- One row per imported file (Add account -> Import a file). Its rows are in transactions with source 'import'.
CREATE TABLE IF NOT EXISTS imports (
    id                INTEGER PRIMARY KEY,
    created_at        TEXT NOT NULL,
    filename          TEXT,
    kind              TEXT NOT NULL CHECK (kind IN ('csv', 'ofx')),
    manual_account_id INTEGER NOT NULL REFERENCES manual_accounts(id),
    rows_new          INTEGER NOT NULL,
    rows_skipped      INTEGER NOT NULL
);
"""
# transactions columns added in v11: where a row came from, and for an imported one its file and (OFX) FITID. An
# imported row's account_id is 'manual:<manual_accounts.id>' and its item_id 'import' (no items row: no Plaid slot).
TASK18_COLUMNS = [
    ("transactions", "source", "TEXT NOT NULL DEFAULT 'plaid' CHECK (source IN ('plaid', 'import'))"),
    ("transactions", "import_id", "INTEGER REFERENCES imports(id)"),
    ("transactions", "fitid", "TEXT"),
]
IMPORT_ITEM = "import"


def _migrate_task18(conn: sqlite3.Connection, before: int) -> None:
    """v10 -> v11 (TASK-18): the login / MCP token / import tables and the transactions columns. Idempotent: every
    table is CREATE IF NOT EXISTS and every column is added only when it's missing, so an interrupted run is finished
    by the next one. Existing rows keep their values (a synced transaction's source is 'plaid'). An install from
    before (`before` > 0) keeps Carfax and Redfin on: a new install starts with them off (config.FREE_SOURCES_DEFAULT),
    so an upgraded one gets its switches saved as they were (an env-file flag still wins over either)."""
    conn.executescript(TASK18_SCHEMA)
    if 0 < before < 11:
        conn.executemany("INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, 'true', ?)",
                         [("valuations.carfax", now()), ("valuations.redfin", now())])
    for table, column, decl in TASK18_COLUMNS:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_fitid ON transactions(account_id, fitid) "
                 "WHERE fitid IS NOT NULL")

VIEWS = """
DROP VIEW IF EXISTS v_monthly_category_spend;
DROP VIEW IF EXISTS v_transactions;
DROP VIEW IF EXISTS v_items;

-- Items without the access token.
CREATE VIEW v_items AS
SELECT item_id, institution_id, institution_name, status, error_code, error_message, products,
       txn_update_status, created_at, last_sync_at, removed_at
FROM items;

-- Effective category: classification row (user > user rule > ai > ai rule > plaid, materialized by
-- `finance classify-apply`) -> else Plaid-mapped default (detailed, then primary) -> else Other.
-- spend = amount (Plaid sign: positive = spend/money out, negative = refund/income).
CREATE VIEW v_transactions AS
SELECT
    t.transaction_id,
    t.date,
    substr(t.date, 1, 7)                                    AS month,
    t.authorized_date,
    t.name,
    t.merchant_name,
    t.amount,
    t.amount                                                AS spend,
    t.iso_currency,
    t.pending,
    t.payment_channel,
    t.pfc_primary,
    t.pfc_detailed,
    t.pfc_confidence,
    cat.id                                                  AS category_id,
    cat.name                                                AS category,
    CASE
        WHEN c.category_id IS NOT NULL THEN c.source
        WHEN COALESCE(dm.category_id, pm.category_id) IS NOT NULL THEN 'plaid'
        ELSE 'default'
    END                                                     AS category_source,
    c.confidence                                            AS category_confidence,
    c.note                                                  AS category_note,
    cat.is_income,
    cat.is_transfer,
    t.account_id,
    COALESCE(a.name, ma.name)                               AS account_name,  -- ma: an imported row's manual account
    a.mask                                                  AS account_mask,
    a.type                                                  AS account_type,
    a.subtype                                               AS account_subtype,
    t.item_id,
    COALESCE(i.institution_name, ma.institution)            AS institution_name,
    t.website,
    t.logo_url,
    r.verdict                                               AS ai_verdict,
    r.note                                                  AS ai_note,
    -- flagged by the AI and not decided by the user since (a user classification, or a user rule)
    CASE WHEN r.verdict = 'flagged'
          AND NOT (COALESCE(c.source = 'user' OR (c.source = 'rule' AND COALESCE(ru.created_from, dl.created_from) = 'user'), 0)
                   AND c.updated_at >= r.reviewed_at)
         THEN 1 ELSE 0 END                                  AS ai_flagged,
    t.source                                                AS source   -- plaid | import (TASK-18)
FROM transactions t
LEFT JOIN accounts a            ON a.account_id = t.account_id
LEFT JOIN items i               ON i.item_id = t.item_id
LEFT JOIN manual_accounts ma    ON t.source = 'import' AND ma.id = CAST(substr(t.account_id, 8) AS INTEGER)
LEFT JOIN classifications c     ON c.transaction_id = t.transaction_id
LEFT JOIN rules ru              ON ru.id = c.rule_id
LEFT JOIN rule_deletions dl     ON dl.id = c.detached_by AND c.rule_id IS NULL   -- v15: filed by a rule since deleted
LEFT JOIN ai_reviews r          ON r.transaction_id = t.transaction_id
LEFT JOIN plaid_category_map dm ON dm.pfc_code = t.pfc_detailed
LEFT JOIN plaid_category_map pm ON pm.pfc_code = t.pfc_primary
LEFT JOIN categories cat        ON cat.id = COALESCE(
    c.category_id, dm.category_id, pm.category_id,
    (SELECT id FROM categories WHERE name = 'Other'))
WHERE t.removed = 0;

CREATE VIEW v_monthly_category_spend AS
SELECT
    v.month,
    v.category,
    ROUND(SUM(v.spend), 2)                                          AS spend,
    c.monthly_cap                                                   AS cap,
    CASE WHEN c.monthly_cap IS NOT NULL AND SUM(v.spend) > c.monthly_cap THEN 1 ELSE 0 END
                                                                    AS over_cap,
    COUNT(*)                                                        AS txn_count,
    c.is_income,
    c.is_transfer
FROM v_transactions v
JOIN categories c ON c.id = v.category_id
GROUP BY v.month, v.category_id;
"""

# Spending categories in groups (polish P0, approved 2026-10-20): one tile colour per group ("r,g,b", the `--rgb` of a
# `.cb` tile) and a Lucide icon per category, all different: 34, the 11 original spending categories among them, named
# byte for byte as before (insights.py and api.py name some of them), and 23 new; "Fun" became the Leisure group. Income
# and Transfers stay outside the groups. static/app.js carries the same table for its tiles (test_taxonomy_p0 checks they agree).
# v19 (design review 2026-10-21): the catch-alls that shared their group's name are "Other Transport", "Other Shopping"
# and "Other Health", the group "Other" is "Misc" (the category "Other" keeps its name: the review queue, classify's
# fallback and the Plaid map key on it), and each group's catch-all comes last in it.
CATEGORY_GROUPS = [
    ("Home", "142,168,255", [("Rent & Utilities", "house"), ("Internet & Phone", "wifi"), ("Home Goods", "armchair"),
                             ("Home Repair", "paint-roller")]),
    ("Food", "245,180,84", [("Groceries", "shopping-cart"), ("Eating Out", "utensils"), ("Coffee", "coffee"),
                            ("Alcohol & Bars", "wine")]),
    ("Transport", "95,198,232", [("Gas", "fuel"), ("Parking", "square-parking"), ("Rideshare", "car-taxi-front"),
                                 ("Car Payment", "hand-coins"), ("Car Maintenance", "wrench"),
                                 ("Public Transit", "train-front"), ("Other Transport", "route")]),
    ("Shopping", "245,158,139", [("Clothing", "shirt"), ("Electronics", "laptop"), ("Other Shopping", "shopping-bag")]),
    ("Bills", "163,168,255", [("Subscriptions", "repeat"), ("Insurance", "shield-check"), ("Fees", "receipt")]),
    ("Health", "199,146,234", [("Personal Care", "scissors"), ("Gym", "dumbbell"), ("Pharmacy", "pill"),
                               ("Doctor & Dentist", "stethoscope"), ("Other Health", "heart-pulse")]),
    ("Leisure", "255,143,177", [("Entertainment", "clapperboard"), ("Travel", "plane"), ("Hobbies", "palette"),
                                ("Hotels", "bed-double")]),
    ("Misc", "155,162,182", [("Gifts & Charity", "gift"), ("Pets", "paw-print"), ("Education", "graduation-cap"),
                             ("Other", "ellipsis")]),
]
UNGROUPED_ICONS = {"Income": "arrow-down-left", "Transfers": "arrow-left-right"}
# A category outside the taxonomy (one made by hand) is never dropped: "Fun" files under Leisure, any other spending
# one under Misc, with no icon of its own (the page shows a tag).
LEGACY_GROUPS = {"Fun": "Leisure"}

# (name, is_income, is_transfer)
DEFAULT_CATEGORIES = [
    ("Rent & Utilities", 0, 0),
    ("Groceries", 0, 0),
    ("Eating Out", 0, 0),
    ("Other Transport", 0, 0),   # v19: was Transport (renamed by id, so ids 1..13 stay v1's)
    ("Other Shopping", 0, 0),    # v19: was Shopping
    ("Subscriptions", 0, 0),  # Plaid has no subscription category: set by rules / AI / user
    ("Entertainment", 0, 0),
    ("Other Health", 0, 0),      # v19: was Health
    ("Travel", 0, 0),
    ("Income", 1, 0),
    ("Transfers", 0, 1),
    ("Fees", 0, 0),
    ("Other", 0, 0),
]
DEFAULT_CATEGORIES += [(name, 0, 0) for _, _, cats in CATEGORY_GROUPS for name, _ in cats
                       if name not in {c[0] for c in DEFAULT_CATEGORIES}]   # v16: the 23 new ones

# Plaid personal_finance_category (primary or detailed) -> category name.
DEFAULT_PFC_MAP = {
    "INCOME": "Income",
    "TRANSFER_IN": "Transfers",
    "TRANSFER_OUT": "Transfers",
    "LOAN_PAYMENTS": "Transfers",  # credit card / loan payments move money between own accounts
    "LOAN_PAYMENTS_MORTGAGE_PAYMENT": "Rent & Utilities",
    "LOAN_PAYMENTS_CAR_PAYMENT": "Car Payment",   # Transport before v16 (init_schema moves the old row)
    "LOAN_DISBURSEMENTS": "Transfers",
    "BANK_FEES": "Fees",
    "ENTERTAINMENT": "Entertainment",
    "FOOD_AND_DRINK": "Eating Out",
    "FOOD_AND_DRINK_GROCERIES": "Groceries",
    "GENERAL_MERCHANDISE": "Other Shopping",
    "HOME_IMPROVEMENT": "Other Shopping",
    "MEDICAL": "Other Health",
    "PERSONAL_CARE": "Other Health",
    "GENERAL_SERVICES": "Other",
    "GOVERNMENT_AND_NON_PROFIT": "Other",
    "TRANSPORTATION": "Other Transport",
    "TRAVEL": "Travel",
    "RENT_AND_UTILITIES": "Rent & Utilities",
}
# v16: Plaid detailed codes that name one of the new categories outright. Codes that could mean two of them (hardware
# stores, eye care, video games, bike shares, tolls) keep their primary code's category.
DEFAULT_PFC_MAP.update({
    "FOOD_AND_DRINK_COFFEE": "Coffee",
    "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR": "Alcohol & Bars",
    "TRANSPORTATION_GAS": "Gas",
    "TRANSPORTATION_PARKING": "Parking",
    "TRANSPORTATION_TAXIS_AND_RIDE_SHARES": "Rideshare",
    "TRANSPORTATION_PUBLIC_TRANSIT": "Public Transit",
    "GENERAL_SERVICES_AUTOMOTIVE": "Car Maintenance",
    "GENERAL_SERVICES_INSURANCE": "Insurance",
    "GENERAL_SERVICES_EDUCATION": "Education",
    "GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES": "Clothing",
    "GENERAL_MERCHANDISE_ELECTRONICS": "Electronics",
    "GENERAL_MERCHANDISE_PET_SUPPLIES": "Pets",
    "GENERAL_MERCHANDISE_GIFTS_AND_NOVELTIES": "Gifts & Charity",
    "GENERAL_MERCHANDISE_PHARMACIES_AND_SUPPLEMENTS": "Pharmacy",
    "MEDICAL_PHARMACIES_AND_SUPPLEMENTS": "Pharmacy",
    "MEDICAL_DENTAL_CARE": "Doctor & Dentist",
    "MEDICAL_PRIMARY_CARE": "Doctor & Dentist",
    "MEDICAL_VETERINARY_SERVICES": "Pets",
    "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS": "Gym",
    "PERSONAL_CARE_HAIR_AND_BEAUTY": "Personal Care",
    "PERSONAL_CARE_LAUNDRY_AND_DRY_CLEANING": "Personal Care",
    "PERSONAL_CARE_OTHER_PERSONAL_CARE": "Personal Care",
    "HOME_IMPROVEMENT_FURNITURE": "Home Goods",
    "HOME_IMPROVEMENT_REPAIR_AND_MAINTENANCE": "Home Repair",
    "RENT_AND_UTILITIES_INTERNET_AND_CABLE": "Internet & Phone",
    "RENT_AND_UTILITIES_TELEPHONE": "Internet & Phone",
    "TRAVEL_LODGING": "Hotels",
    "GOVERNMENT_AND_NON_PROFIT_DONATIONS": "Gifts & Charity",
})


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | None = None, timeout: float = 30) -> sqlite3.Connection:
    """Read-write connection. Creates data dir (0700) and DB (0600) and applies the schema. `timeout`: seconds each
    statement waits for another connection's lock (SQLite's busy timeout), from the first one on (the Ask turn's: 0)."""
    path = Path(path or config.db_path())
    config.ensure_data_dir(path.parent)
    old_umask = os.umask(0o077)
    try:
        conn = sqlite3.connect(path, timeout=timeout)
    finally:
        os.umask(old_umask)
    os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        init_schema(conn)
    return conn


# Columns added to existing tables after their first version: (table, column, declaration), in table order.
ADDED_COLUMNS = [
    ("manual_accounts", "vin", "TEXT"),
    ("manual_accounts", "vehicle_trim", "TEXT"),
    ("manual_accounts", "mileage", "INTEGER"),
    ("manual_accounts", "mileage_as_of", "TEXT"),
    ("manual_accounts", "zip_code", "TEXT"),
    ("manual_accounts", "address", "TEXT"),
    ("manual_accounts", "vin_decoded", "TEXT"),
    ("manual_accounts", "value_source", "TEXT CHECK (value_source IN ('user', 'estimate'))"),
    ("manual_accounts", "vehicle_condition", "TEXT CHECK (vehicle_condition IN ('fair', 'good', 'excellent'))"),
    ("vin_decodes", "specs_json", "TEXT"),
    ("manual_accounts", "linked_loan_id", "INTEGER REFERENCES manual_accounts(id)"),
    ("pending_actions", "origin", "TEXT NOT NULL DEFAULT 'mcp'"),   # v14 (TASK-23 review): every earlier row is an MCP
    ("pending_actions", "held_by", "TEXT"),                          # client's, and none is held
    ("classifications", "detached_by", "INTEGER REFERENCES rule_deletions(id)"),   # v15 (TASK-24 fix)
    ("categories", "group_id", "INTEGER REFERENCES category_groups(id)"),           # v16 (polish P0): NULL = Income,
    ("categories", "icon", "TEXT"),                                                 # Transfers; a Lucide icon name
    ("categories", "sort", "INTEGER"),                                              # order within its group
]


def init_schema(conn: sqlite3.Connection) -> None:
    before = conn.execute("PRAGMA user_version").fetchone()[0]
    with conn:
        conn.executescript(SCHEMA)
        for table, column, decl in ADDED_COLUMNS:
            if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        if before < 7:  # the first migration to v7: typed vehicle/property values are "Your value"
            conn.execute("UPDATE manual_accounts SET value_source = 'user' "
                         "WHERE kind IN ('vehicle', 'property') AND value_source IS NULL")
        if before < 8:  # TASK-13: vehicles are priced in 'good' condition unless set otherwise
            conn.execute("UPDATE manual_accounts SET vehicle_condition = 'good' "
                         "WHERE kind = 'vehicle' AND vehicle_condition IS NULL")
        _rebuild_asset_valuations(conn)
        if before < 9:
            _link_loans(conn)
        _migrate_task18(conn, before)
        _task24_schema(conn)   # before VIEWS: v_transactions reads rule_deletions
        conn.executescript(VIEWS)
        _task19_schema(conn)
        _task21_schema(conn)
        if before < 19:   # before the defaults go in: they carry the new names, and the old rows keep their ids
            _v19_rename(conn)
        conn.executemany(
            "INSERT OR IGNORE INTO categories (name, is_income, is_transfer) VALUES (?, ?, ?)",
            DEFAULT_CATEGORIES,
        )
        if before < 16:   # before the new defaults go in: only a map row still at its v15 default moves
            conn.execute("UPDATE plaid_category_map SET category_id = (SELECT id FROM categories WHERE name = 'Car Payment') "
                         "WHERE pfc_code = 'LOAN_PAYMENTS_CAR_PAYMENT' "
                         "AND category_id = (SELECT id FROM categories WHERE name = 'Other Transport')")   # v15's Transport
        conn.executemany(
            "INSERT OR IGNORE INTO plaid_category_map (pfc_code, category_id) "
            "SELECT ?, id FROM categories WHERE name = ?",
            DEFAULT_PFC_MAP.items(),
        )
        _v16_schema(conn)
        _v17_schema(conn)
        _v18_schema(conn)
        if before < 19:
            _v19_schema(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


V18_SCHEMA = """
-- A monthly budget on a whole category group (polish P2e, groupbudget.py): it counts every spending category in the group,
-- and the category caps inside it are shown but not counted in the page's totals. No row = no group budget (the group's
-- Planned is the sum of its caps). Never rolls over.
CREATE TABLE IF NOT EXISTS group_budgets (
    group_id   INTEGER PRIMARY KEY REFERENCES category_groups(id),
    amount     REAL NOT NULL CHECK (amount >= 0),
    created_at TEXT NOT NULL,                  -- UTC timestamp
    updated_at TEXT NOT NULL
);
-- The one-time "move budgets to groups" review: a group whose category budgets were kept. Its caps stay as they are and
-- the review never offers it again.
CREATE TABLE IF NOT EXISTS group_budget_kept (
    group_id INTEGER PRIMARY KEY REFERENCES category_groups(id),
    kept_at  TEXT NOT NULL                     -- UTC timestamp
);
"""


def _v18_schema(conn: sqlite3.Connection) -> None:
    """v17 -> v18 (polish P2e): the group budgets and the kept groups. Additive and idempotent (CREATE ... IF NOT EXISTS
    only): no cap, category or rollover row is touched, so a database stopped half way resumes and a rerun changes
    nothing."""
    conn.executescript(V18_SCHEMA)


def _v17_schema(conn: sqlite3.Connection) -> None:
    """Add goal appearance after v16, preserving the old kind/track defaults, including archived goals.

    NULL marks an unfilled column so an interrupted upgrade can safely resume. Re-running never resets a choice.
    Legacy pencil/split/card/handcoins icons are retained; the picker offers the twelve new selectable icons.
    """
    for column in ("icon", "colour"):
        if column not in {r[1] for r in conn.execute("PRAGMA table_info(goals)")}:
            conn.execute(f"ALTER TABLE goals ADD COLUMN {column} TEXT")
    conn.execute("""UPDATE goals SET icon = CASE
        WHEN kind = 'payoff' THEN CASE WHEN
            account_id IN (SELECT account_id FROM accounts WHERE type = 'credit') OR
            account_id IN (SELECT 'manual:' || id FROM manual_accounts WHERE kind = 'credit_card')
            THEN 'card' ELSE 'handcoins' END
        WHEN kind = 'monthly' THEN 'piggy-bank'
        WHEN track = 'manual' THEN 'pencil'
        WHEN track = 'earmark' THEN 'split'
        ELSE 'piggy-bank' END WHERE icon IS NULL""")
    conn.execute("""UPDATE goals SET colour = CASE WHEN kind = 'payoff' THEN '#f5b454'
        ELSE '#4fd1a5' END WHERE colour IS NULL""")


# --- TASK-19 (Batch C): splits, notes and tags, saved filters, budget rollover, goals, the Alpaca live account ---------
TASK19_SCHEMA = """
-- A purchase split into lines (txmeta.py): two or more, each a spending category and an amount (Plaid sign, like
-- transactions.amount), adding up to the transaction's amount to the cent. The transaction itself becomes the user's
-- decision (classifications.source = 'user', its category kept), so no rule or AI run ever touches it. Spending by
-- category (v_spend_lines) uses the lines in place of the transaction's category; totals don't change.
CREATE TABLE IF NOT EXISTS transaction_splits (
    id             INTEGER PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
    line           INTEGER NOT NULL,           -- 1, 2, ... in the order shown
    category_id    INTEGER NOT NULL REFERENCES categories(id),
    amount         REAL NOT NULL,              -- whole cents
    created_at     TEXT NOT NULL,
    UNIQUE (transaction_id, line)
);
CREATE INDEX IF NOT EXISTS idx_transaction_splits_txn ON transaction_splits(transaction_id);

-- A note on any transaction (free text), and tags (many-to-many, made as they're first used).
CREATE TABLE IF NOT EXISTS transaction_notes (
    transaction_id TEXT PRIMARY KEY REFERENCES transactions(transaction_id),
    note           TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transaction_tags (
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
    tag_id         INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (transaction_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_transaction_tags_tag ON transaction_tags(tag_id);

-- A named Transactions filter set, shown as a chip above the table.
CREATE TABLE IF NOT EXISTS saved_filters (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    filters    TEXT NOT NULL,                  -- JSON {q, category, bank, kind, tag} (empty ones left out)
    created_at TEXT NOT NULL
);

-- Budget rollover (rollover.py), off by default: a row here turns it on for that category. Then last month's unused
-- cap (cap - spent, never below 0) adds to this month's cap.
CREATE TABLE IF NOT EXISTS category_rollover (
    category_id INTEGER PRIMARY KEY REFERENCES categories(id),
    enabled_at  TEXT NOT NULL
);

-- Goals (goals.py). They never change net worth, budgets, alerts or any other total. kind: save (Save up: a target,
-- optional date; progress from a whole account, part of one (an earmark) or entries logged by hand), payoff (Pay off:
-- a loan or card, paid down from its balance when the goal was made), monthly (Monthly savings: income - spending
-- each month vs a target). Archived goals are kept.
CREATE TABLE IF NOT EXISTS goals (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('save', 'payoff', 'monthly')),
    name          TEXT NOT NULL,
    target        REAL CHECK (target IS NULL OR target > 0),
    target_month  TEXT,                        -- save: YYYY-MM to hit it by (optional)
    track         TEXT CHECK (track IN ('account', 'earmark', 'manual')),
    account_id    TEXT,                        -- a Plaid account_id or manual:<id> (save: account / earmark; payoff)
    earmark       REAL CHECK (earmark IS NULL OR earmark >= 0),
    start_balance REAL,                        -- payoff: the balance owed when the goal was made
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    archived_at   TEXT
);
-- Money added to (+) or taken out of (-) a goal logged by hand, and each change of an earmark: the goal's history.
CREATE TABLE IF NOT EXISTS goal_entries (
    id         INTEGER PRIMARY KEY,
    goal_id    INTEGER NOT NULL REFERENCES goals(id),
    date       TEXT NOT NULL,                  -- local YYYY-MM-DD
    amount     REAL NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goal_entries_goal ON goal_entries(goal_id, date);

-- The Alpaca live brokerage account, read-only through its API (alpaca.py, `finance alpaca-sync`, and the key the
-- dashboard's Connect form saves): one row per local day, replaced by each sync that day. Net worth counts the latest
-- counted row, only while its status is ACTIVE. counted 0: the key was removed (or its file is gone), so those rows are
-- left out of every page, total and history point.
CREATE TABLE IF NOT EXISTS alpaca_snapshots (
    date            TEXT PRIMARY KEY,          -- local YYYY-MM-DD
    fetched_at      TEXT NOT NULL,             -- UTC timestamp
    status          TEXT NOT NULL,             -- Alpaca's account status (ACTIVE, ACCOUNT_CLOSED, ...)
    account_mask    TEXT,                      -- last 4 of the account number
    currency        TEXT,
    cash            REAL NOT NULL DEFAULT 0,
    equity          REAL NOT NULL DEFAULT 0,   -- cash + the positions' market value: what net worth counts
    reported_equity REAL,                      -- Alpaca's own equity figure, kept for reference
    positions       INTEGER NOT NULL DEFAULT 0,
    positions_json  TEXT NOT NULL DEFAULT '[]', -- [{symbol, asset_class, qty, price, value, cost_basis}], value = market value
    counted         INTEGER NOT NULL DEFAULT 1  -- 0 once the key is removed
);

DROP VIEW IF EXISTS v_spend_lines;
-- v_transactions with a split transaction's lines in place of the transaction: one row per line (its category and
-- amount), every other transaction as it is. The spending totals and everything by category (budgets, rings, the
-- chart, cash flow, alerts, `finance spend` / budget-status) read this; a split never changes a total.
CREATE VIEW v_spend_lines AS
SELECT v.transaction_id, NULL AS split_line, v.date, v.month, v.name, v.merchant_name, v.amount, v.spend, v.pending,
       v.category_id, v.category, v.category_source, v.is_income, v.is_transfer, v.account_id, v.account_name,
       v.account_mask, v.account_type, v.item_id, v.institution_name, v.pfc_primary, v.pfc_detailed
FROM v_transactions v
WHERE NOT EXISTS (SELECT 1 FROM transaction_splits s WHERE s.transaction_id = v.transaction_id)
UNION ALL
SELECT v.transaction_id, s.line, v.date, v.month, v.name, v.merchant_name, s.amount, s.amount, v.pending,
       cat.id, cat.name, 'split', cat.is_income, cat.is_transfer, v.account_id, v.account_name,
       v.account_mask, v.account_type, v.item_id, v.institution_name, v.pfc_primary, v.pfc_detailed
FROM transaction_splits s
JOIN v_transactions v ON v.transaction_id = s.transaction_id
JOIN categories cat   ON cat.id = s.category_id;
"""


def _task19_schema(conn: sqlite3.Connection) -> None:
    """v11 -> v12 (TASK-19): the tables and the v_spend_lines view (after VIEWS: it reads v_transactions). Idempotent:
    every table is CREATE IF NOT EXISTS and the view is dropped and made again."""
    conn.executescript(TASK19_SCHEMA)


# --- TASK-21: bills tracked by hand ------------------------------------------------------------------------------------
TASK21_SCHEMA = """
-- A bill tracked by hand (bills.py; "Track as a bill" on a transaction, `finance bills track`, finance_track_bill), or a
-- detected one whose schedule was edited. A bill is the merchant (insights.merchant_key), not the account: every charge
-- from it, on any account, confirms it. One row per merchant; stopped_at set = stopped: a bill tracked by hand stops, a
-- detected one is hidden from bills (tracking it again clears it). Rows are never deleted.
CREATE TABLE IF NOT EXISTS tracked_bills (
    id                    INTEGER PRIMARY KEY,
    merchant_key          TEXT NOT NULL UNIQUE,   -- insights.merchant_key(merchant_name, name): the bill
    merchant              TEXT NOT NULL,          -- its name as shown when it was tracked
    cadence               TEXT NOT NULL CHECK (cadence IN ('weekly', 'biweekly', 'monthly', 'bimonthly', 'quarterly',
                                                           'semiannual', 'yearly')),
    variable              INTEGER NOT NULL CHECK (variable IN (0, 1)),   -- 1: the amount changes each time
    usual_low             REAL CHECK (usual_low IS NULL OR usual_low > 0),  -- "Usually about": one amount (low = high)
    usual_high            REAL CHECK (usual_high IS NULL OR (usual_low IS NOT NULL AND usual_high >= usual_low)),
    anchor_date           TEXT NOT NULL,          -- the charge it starts from: local YYYY-MM-DD
    anchor_amount         REAL NOT NULL,
    anchor_transaction_id TEXT,
    created_at            TEXT NOT NULL,          -- UTC timestamps
    updated_at            TEXT NOT NULL,
    stopped_at            TEXT
);
"""


def _task21_schema(conn: sqlite3.Connection) -> None:
    """v12 -> v13 (TASK-21): the tracked_bills table. Idempotent (CREATE IF NOT EXISTS)."""
    conn.executescript(TASK21_SCHEMA)


# --- TASK-24 fix: rule deletion receipts (the rules manager's Undo) -------------------------------------------------------
TASK24_SCHEMA = """
-- A deleted rule as it was (classify.delete_rule: Settings -> Categorization rules, `finance rules delete`) and the Undo
-- for it. The rows it had filed keep their category and source 'rule', with rule_id NULL, and point here
-- (classifications.detached_by). created_from says whose rule it was, so an AI decision never overwrites what a deleted
-- user rule filed (classify.set_category, ai._held, v_transactions.ai_flagged), while a deleted AI rule's rows stay open
-- to the AI as before. Undo (classify.restore_rule) takes only the receipt: the rule comes back from this row, and only
-- the rows still pointing here are linked again. The caller gets the receipt once; only its SHA-256 is stored (and hidden
-- from read-only connections). A receipt restores once (restored_at). Rows are never deleted.
CREATE TABLE IF NOT EXISTS rule_deletions (
    id               INTEGER PRIMARY KEY,
    receipt_hash     TEXT NOT NULL UNIQUE,   -- sha256 hex of the receipt handed to the caller
    rule_id          INTEGER NOT NULL,       -- the rule as it was (rules.*)
    match_field      TEXT NOT NULL,
    match_type       TEXT NOT NULL,
    pattern          TEXT NOT NULL,
    category_id      INTEGER NOT NULL REFERENCES categories(id),
    priority         INTEGER NOT NULL,
    created_from     TEXT,
    created_at       TEXT NOT NULL,
    filed            INTEGER NOT NULL,       -- rows it had filed: the ones this delete detached
    deleted_at       TEXT NOT NULL,          -- UTC timestamps
    restored_at      TEXT,
    restored_rule_id INTEGER
);
-- A detached row that changes at all (its category, source or rule) is no longer as the delete left it: it stops pointing
-- at the delete, so Undo never links a row that was changed, even one changed away and back.
CREATE TRIGGER IF NOT EXISTS trg_classifications_detached_changed
AFTER UPDATE OF category_id, source, rule_id ON classifications
WHEN NEW.detached_by IS NOT NULL AND NEW.detached_by IS OLD.detached_by
BEGIN
    UPDATE classifications SET detached_by = NULL WHERE transaction_id = NEW.transaction_id;
END;
"""


def _task24_schema(conn: sqlite3.Connection) -> None:
    """v14/v13 -> v15 (TASK-24 fix): rule_deletions and the trigger (classifications.detached_by is in ADDED_COLUMNS, added
    before this runs). Idempotent (CREATE IF NOT EXISTS). Rows a deleted rule filed before v15 have no receipt: they keep
    no user protection and no Undo."""
    conn.executescript(TASK24_SCHEMA)


# --- polish P0: category groups --------------------------------------------------------------------------------------
V16_SCHEMA = """
-- The groups spending categories file under (CATEGORY_GROUPS): a group only organises categories (Budgets, Where it
-- went, the category picker); a transaction is always filed to a category, never to a group. Rows are never deleted.
CREATE TABLE IF NOT EXISTS category_groups (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    sort INTEGER NOT NULL,                     -- display order, 1 = first
    rgb  TEXT NOT NULL                         -- the tile colour every category in it shares: "r,g,b"
);
"""


def _v16_schema(conn: sqlite3.Connection) -> None:
    """v15 -> v16 (polish P0): category_groups and their rows, and each category's group, icon and order (the new
    categories are in DEFAULT_CATEGORIES, inserted before this runs). Additive and idempotent: a category's group, icon
    and order are only filled while empty, so nothing set since is changed, and no category or classification is
    touched otherwise. A category outside the taxonomy keeps its rows: "Fun" files under Leisure, any other spending
    one under Misc (Other before v19)."""
    conn.executescript(V16_SCHEMA)
    for sort, (group, rgb, cats) in enumerate(CATEGORY_GROUPS, 1):
        conn.execute("INSERT OR IGNORE INTO category_groups (name, sort, rgb) VALUES (?, ?, ?)", (group, sort, rgb))
        gid = conn.execute("SELECT id FROM category_groups WHERE name = ?", (group,)).fetchone()[0]
        for i, (name, icon) in enumerate(cats, 1):
            conn.execute("UPDATE categories SET group_id = COALESCE(group_id, ?), icon = COALESCE(icon, ?), "
                         "sort = COALESCE(sort, ?) WHERE name = ?", (gid, icon, i, name))
    for name, icon in UNGROUPED_ICONS.items():
        conn.execute("UPDATE categories SET icon = COALESCE(icon, ?) WHERE name = ?", (icon, name))
    for name, group in LEGACY_GROUPS.items():
        conn.execute("UPDATE categories SET group_id = (SELECT id FROM category_groups WHERE name = ?) "
                     "WHERE name = ? AND group_id IS NULL", (group, name))
    conn.execute("UPDATE categories SET group_id = (SELECT id FROM category_groups WHERE name = 'Misc') "
                 "WHERE group_id IS NULL AND is_income = 0 AND is_transfer = 0")
    conn.execute("UPDATE categories SET sort = 1000 + id WHERE sort IS NULL")   # hand-made ones: after the taxonomy's


# --- v19: category names (design review 2026-10-21) -------------------------------------------------------------------
# The catch-alls that shared their group's name, and the group that shared the category Other's name: old -> new.
V19_RENAMES = {"Transport": "Other Transport", "Shopping": "Other Shopping", "Health": "Other Health"}
V19_GROUP_RENAMES = {"Other": "Misc"}
# A category's new icon, set only while it still has the old one: name -> (old, new)
V19_ICONS = {"Other Transport": ("car-front", "route"), "Home Repair": ("hammer", "paint-roller")}


class SchemaError(RuntimeError):
    """An upgrade that can't go ahead on this database's rows. Nothing it would write is written."""


def _v19_rename(conn: sqlite3.Connection) -> None:
    """v18 -> v19, the names (before DEFAULT_CATEGORIES goes in, which carries the new ones): each renamed category and
    group keeps its id, so every classification, rule, cap, split, rollover, map row and group budget follows it. Fails
    closed: when a category (or group) already has the new name, in any case, made by hand, it raises SchemaError and
    writes nothing (the caller's transaction rolls back and user_version stays where it was), since merging two
    categories the owner kept apart is theirs to decide. Idempotent: an old name that's gone is skipped. Saved filters
    and the chart's hidden categories, which store names, are renamed too."""
    plans = [("categories", "category", V19_RENAMES)]
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'category_groups'").fetchone():
        plans.append(("category_groups", "category group", V19_GROUP_RENAMES))   # none before v16: made with the new name
    for table, what, renames in plans:
        for old, new in renames.items():
            row = conn.execute(f"SELECT id FROM {table} WHERE name = ?", (old,)).fetchone()
            if row is None:
                continue
            clash = conn.execute(f"SELECT id, name FROM {table} WHERE name = ? COLLATE NOCASE AND id <> ?",
                                 (new, row[0])).fetchone()
            if clash:
                raise SchemaError(
                    f"can't upgrade the database to v{SCHEMA_VERSION}: it renames the {what} {old!r} (id {row[0]}) to "
                    f"{new!r}, and a {what} {clash[1]!r} (id {clash[0]}) already exists. Rename or merge that one first, "
                    f"then start again; nothing was changed.")
            conn.execute(f"UPDATE {table} SET name = ? WHERE id = ?", (new, row[0]))

    def swap(value, names):
        if isinstance(value, list):
            return [names.get(v, v) if isinstance(v, str) else v for v in value]
        return names.get(value, value) if isinstance(value, str) else value

    for fid, raw in conn.execute("SELECT id, filters FROM saved_filters").fetchall():
        try:
            f = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(f, dict):
            continue
        new = {k: swap(v, V19_RENAMES if k == "category" else V19_GROUP_RENAMES if k == "group" else {})
               for k, v in f.items()}
        if new != f:
            conn.execute("UPDATE saved_filters SET filters = ? WHERE id = ?", (json.dumps(new), fid))
    row = conn.execute("SELECT value FROM settings WHERE key = 'chart_excluded_categories'").fetchone()
    try:
        hidden = json.loads(row[0]) if row else None
    except ValueError:
        hidden = None
    if isinstance(hidden, list) and swap(hidden, V19_RENAMES) != hidden:
        conn.execute("UPDATE settings SET value = ?, updated_at = ? WHERE key = 'chart_excluded_categories'",
                     (json.dumps(swap(hidden, V19_RENAMES)), now()))


def _v19_schema(conn: sqlite3.Connection) -> None:
    """v18 -> v19, after _v16_schema: the new icons (only over the old one, so an icon set since stays), each group's
    order with its catch-all last (a category moved to another group by hand keeps its place), then the Plaid defaults
    re-filed. Idempotent: a rerun finds nothing left to change."""
    for name, (old, new) in V19_ICONS.items():
        conn.execute("UPDATE categories SET icon = ? WHERE name = ? AND (icon IS NULL OR icon = ?)", (new, name, old))
    for group, _, cats in CATEGORY_GROUPS:
        for i, (name, _) in enumerate(cats, 1):
            conn.execute("UPDATE categories SET sort = ? WHERE name = ? "
                         "AND group_id = (SELECT id FROM category_groups WHERE name = ?)", (i, name, group))
    refile_plaid_defaults(conn)


def refile_plaid_defaults(conn: sqlite3.Connection) -> int:
    """Every Plaid default (classifications.source 'plaid') moved to the category today's plaid_category_map gives its
    transaction: the detailed code's row, else the primary's (classify.plaid_default's lookup). A row filed before a
    finer default existed (a gas station left in Transport by v15) lands where a new sync would put it. Only 'plaid'
    rows: a user's, a rule's or the AI's pick is never touched, and a code with no map row at all stays where it is.
    Returns the rows moved. Runs in the caller's transaction."""
    moves = conn.execute(
        """SELECT c.transaction_id, COALESCE(dm.category_id, pm.category_id) AS target
           FROM classifications c
           JOIN transactions t             ON t.transaction_id = c.transaction_id
           LEFT JOIN plaid_category_map dm ON dm.pfc_code = t.pfc_detailed
           LEFT JOIN plaid_category_map pm ON pm.pfc_code = t.pfc_primary
           WHERE c.source = 'plaid' AND COALESCE(dm.category_id, pm.category_id) IS NOT NULL
             AND COALESCE(dm.category_id, pm.category_id) <> c.category_id""").fetchall()
    ts = now()
    conn.executemany("UPDATE classifications SET category_id = ?, updated_at = ? "
                     "WHERE transaction_id = ? AND source = 'plaid'", [(target, ts, tid) for tid, target in moves])
    return len(moves)


def _link_loans(conn: sqlite3.Connection) -> None:
    """v9: a vehicle (property) is linked to its loan when that's unambiguous: exactly one active vehicle and exactly
    one active auto loan (one property and one mortgage). Otherwise nothing is linked; the edit form's picker does it.
    Only an unlinked account is touched, so running it again changes nothing."""
    for kind, subtype in (("vehicle", "auto"), ("property", "mortgage")):
        assets = conn.execute("SELECT id, linked_loan_id FROM manual_accounts WHERE kind = ? AND archived_at IS NULL",
                              (kind,)).fetchall()
        loans = conn.execute("SELECT id FROM manual_accounts WHERE kind = 'loan' AND subtype = ? AND archived_at IS NULL",
                             (subtype,)).fetchall()
        if len(assets) == 1 and len(loans) == 1 and assets[0][1] is None:
            conn.execute("UPDATE manual_accounts SET linked_loan_id = ? WHERE id = ?", (loans[0][0], assets[0][0]))


def _rebuild_asset_valuations(conn: sqlite3.Connection) -> None:
    """v7 -> v8: SQLite can't change a CHECK constraint, so a v7 asset_valuations (sources marketcheck / rentcast only)
    is copied into a new table with every row kept, then swapped in. A no-op once rebuilt. The copy, the drop and the
    rename share one transaction (the INSERT opens it); a leftover half-made table from an interrupted run is dropped."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'asset_valuations'").fetchone()
    if row is None or "'carfax'" in row[0]:
        return
    keep = [r[1] for r in conn.execute("PRAGMA table_info(asset_valuations)")]
    conn.execute("DROP TABLE IF EXISTS asset_valuations_v8")
    conn.execute(ASSET_VALUATIONS.split("\n", 2)[2].replace("CREATE TABLE IF NOT EXISTS asset_valuations",
                                                            "CREATE TABLE asset_valuations_v8", 1))
    cols = ", ".join(keep)
    conn.execute(f"INSERT INTO asset_valuations_v8 ({cols}) SELECT {cols} FROM asset_valuations")
    conn.execute("DROP TABLE asset_valuations")
    conn.execute("ALTER TABLE asset_valuations_v8 RENAME TO asset_valuations")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_asset_valuations_account ON asset_valuations(account_id, fetched_at)")


# --- read-only access for `finance query` ------------------------------------------------------

# Introspection pragmas (any argument is a table/index name, never a new value).
_READ_PRAGMAS = {
    "table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo",
    "foreign_key_list", "database_list", "collation_list", "function_list",
}
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                    sqlite3.SQLITE_RECURSIVE}


# Columns that read as NULL through `finance query` (and every other read-only connection): bank tokens, and (TASK-18)
# the login's hashes, 2FA key, session / CSRF tokens and MCP token hashes.
HIDDEN_COLUMNS = frozenset({("items", "access_token"), ("auth_users", "password_hash"), ("auth_users", "totp_secret"),
                            ("auth_recovery_codes", "code_hash"), ("auth_sessions", "token_hash"),
                            ("auth_sessions", "csrf"), ("mcp_tokens", "token_hash"),
                            ("rule_deletions", "receipt_hash")})


def _readonly_authorizer(action, arg1, arg2, _db, _trigger):
    if action == sqlite3.SQLITE_READ and (arg1, arg2) in HIDDEN_COLUMNS:
        return sqlite3.SQLITE_IGNORE  # column reads as NULL
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and (arg1 in _READ_PRAGMAS or (arg1 == "user_version" and arg2 is None)):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def connect_readonly(path: Path | None = None, timeout: float = 30) -> sqlite3.Connection:
    """mode=ro URI + query_only + an authorizer that denies anything but reads and hides tokens. `timeout`: as
    connect()'s."""
    path = Path(path or config.db_path())
    if not path.exists():
        raise FileNotFoundError(f"database not found: {path} (run `finance serve` or `finance sync` first)")
    uri = f"file:{urllib.parse.quote(str(path.resolve()))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_readonly_authorizer)
    return conn


def slots_used(conn: sqlite3.Connection) -> int:
    """Every Item ever exchanged counts, including removed ones (Plaid Trial never frees slots), plus every blocked
    duplicate the owner confirmed Plaid counted anyway (outcome 'duplicate_counted': an Item with no access_token)."""
    return conn.execute("SELECT (SELECT COUNT(*) FROM items) + "
                        "(SELECT COUNT(*) FROM link_attempts WHERE outcome = 'duplicate_counted')").fetchone()[0]


def blocked_duplicates(conn: sqlite3.Connection) -> int:
    """Blocked duplicates not yet confirmed either way: Plaid *may* have counted them (the worst-case warning)."""
    return conn.execute(
        "SELECT COUNT(*) FROM link_attempts WHERE outcome = 'duplicate_blocked'").fetchone()[0]

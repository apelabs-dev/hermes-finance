-- Schema v12 as TASK-20 (the Batch A + Batch C merge) left it: hermes_finance/db.py at 90bad74 (SCHEMA_VERSION = 12:
-- TASK-17's pending_actions, TASK-18's login / token / import tables, TASK-19's tables and v_spend_lines), with its
-- default rows, dumped with sqlite3 iterdump. Used by test_schema_upgrade.py (TASK-21: v12 -> v13); do not edit.
PRAGMA user_version = 12;
BEGIN TRANSACTION;
CREATE TABLE accounts (
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
CREATE TABLE ai_reviews (
    transaction_id TEXT PRIMARY KEY REFERENCES transactions(transaction_id),
    run_id         TEXT NOT NULL,
    reviewed_at    TEXT NOT NULL,
    verdict        TEXT NOT NULL CHECK (verdict IN ('changed', 'confirmed', 'flagged')),
    category_id    INTEGER REFERENCES categories(id),  -- the category after the review
    confidence     REAL,
    note           TEXT
);
CREATE TABLE ai_run_groups (
    run_id               TEXT REFERENCES ai_runs(run_id),
    group_id             TEXT,                -- g1, g2, ... within the run
    merchant             TEXT,
    rule_field           TEXT,                -- merchant_name | name (what a `rule` decision matches on)
    rule_pattern         TEXT,
    transaction_ids_json TEXT NOT NULL,
    PRIMARY KEY (run_id, group_id)
);
CREATE TABLE ai_runs (
    run_id        TEXT PRIMARY KEY,           -- ai-YYYYmmdd-HHMMSS (UTC, same instant as created_at)
    created_at    TEXT NOT NULL,
    applied_at    TEXT,                       -- NULL until ai-apply succeeds; a run applies once
    queued_groups INTEGER,
    queued_txns   INTEGER,
    summary_json  TEXT                        -- the ai-apply output
);
CREATE TABLE alpaca_snapshots (
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
CREATE TABLE api_calls (
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
CREATE TABLE api_usage (
    provider       TEXT NOT NULL,             -- nhtsa | carfax | redfin | commons | osm
    month          TEXT NOT NULL,             -- local YYYY-MM
    calls          INTEGER NOT NULL DEFAULT 0,
    blocked_until  TEXT,                      -- local YYYY-MM-DD, NULL = not blocked
    blocked_reason TEXT,
    updated_at     TEXT,
    PRIMARY KEY (provider, month)
);
CREATE TABLE asset_images (
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
CREATE TABLE asset_valuations (
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
);
CREATE TABLE auth_failures (
    key          TEXT PRIMARY KEY,
    failures     INTEGER NOT NULL,
    first_at     TEXT NOT NULL,
    locked_until TEXT
);
CREATE TABLE auth_recovery_codes (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES auth_users(id),
    code_hash  TEXT NOT NULL,                 -- argon2id
    created_at TEXT NOT NULL,
    used_at    TEXT
);
CREATE TABLE auth_sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES auth_users(id),
    csrf         TEXT NOT NULL,               -- sent back in X-CSRF-Token on every state-changing request
    created_at   TEXT NOT NULL,               -- absolute expiry counts from here
    last_seen_at TEXT NOT NULL,               -- idle expiry counts from here
    ip           TEXT,
    user_agent   TEXT
);
CREATE TABLE auth_users (
    id                  INTEGER PRIMARY KEY,
    username            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash       TEXT NOT NULL,        -- argon2id (PHC string)
    totp_secret         TEXT NOT NULL,        -- base32 (RFC 6238, SHA-1, 30 s, 6 digits)
    totp_last_step      INTEGER NOT NULL DEFAULT 0,  -- the last 30 s step a code was accepted for: a replay is refused
    created_at          TEXT NOT NULL,
    password_changed_at TEXT NOT NULL
);
CREATE TABLE balance_snapshots (
    account_id   TEXT NOT NULL,
    date         TEXT NOT NULL,               -- local YYYY-MM-DD
    current      REAL,
    available    REAL,
    iso_currency TEXT,
    PRIMARY KEY (account_id, date)
);
CREATE TABLE categories (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    parent      INTEGER REFERENCES categories(id),
    monthly_cap REAL,                         -- budget cap in account currency; NULL = no cap
    is_income   INTEGER NOT NULL DEFAULT 0,
    is_transfer INTEGER NOT NULL DEFAULT 0    -- transfers/card payments: exclude from spend totals
);
INSERT INTO "categories" VALUES(1,'Rent & Utilities',NULL,NULL,0,0);
INSERT INTO "categories" VALUES(2,'Groceries',NULL,NULL,0,0);
INSERT INTO "categories" VALUES(3,'Eating Out',NULL,NULL,0,0);
INSERT INTO "categories" VALUES(4,'Transport',NULL,NULL,0,0);
INSERT INTO "categories" VALUES(5,'Shopping',NULL,NULL,0,0);
INSERT INTO "categories" VALUES(6,'Subscriptions',NULL,NULL,0,0);
INSERT INTO "categories" VALUES(7,'Entertainment',NULL,NULL,0,0);
INSERT INTO "categories" VALUES(8,'Health',NULL,NULL,0,0);
INSERT INTO "categories" VALUES(9,'Travel',NULL,NULL,0,0);
INSERT INTO "categories" VALUES(10,'Income',NULL,NULL,1,0);
INSERT INTO "categories" VALUES(11,'Transfers',NULL,NULL,0,1);
INSERT INTO "categories" VALUES(12,'Fees',NULL,NULL,0,0);
INSERT INTO "categories" VALUES(13,'Other',NULL,NULL,0,0);
CREATE TABLE category_rollover (
    category_id INTEGER PRIMARY KEY REFERENCES categories(id),
    enabled_at  TEXT NOT NULL
);
CREATE TABLE classifications (
    transaction_id TEXT PRIMARY KEY REFERENCES transactions(transaction_id),
    category_id    INTEGER NOT NULL REFERENCES categories(id),
    source         TEXT NOT NULL CHECK (source IN ('plaid', 'rule', 'ai', 'user')),
    confidence     REAL,
    note           TEXT,
    rule_id        INTEGER REFERENCES rules(id),
    updated_at     TEXT NOT NULL
);
CREATE TABLE goal_entries (
    id         INTEGER PRIMARY KEY,
    goal_id    INTEGER NOT NULL REFERENCES goals(id),
    date       TEXT NOT NULL,                  -- local YYYY-MM-DD
    amount     REAL NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE goals (
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
CREATE TABLE holding_snapshots (
    date        TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    security_id TEXT NOT NULL,
    ticker      TEXT,
    quantity    REAL,
    price       REAL,
    value       REAL,
    PRIMARY KEY (account_id, security_id, date)
);
CREATE TABLE holdings (
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
CREATE TABLE imports (
    id                INTEGER PRIMARY KEY,
    created_at        TEXT NOT NULL,
    filename          TEXT,
    kind              TEXT NOT NULL CHECK (kind IN ('csv', 'ofx')),
    manual_account_id INTEGER NOT NULL REFERENCES manual_accounts(id),
    rows_new          INTEGER NOT NULL,
    rows_skipped      INTEGER NOT NULL
);
CREATE TABLE institutions (
    institution_id TEXT PRIMARY KEY,
    name           TEXT,
    logo_png       BLOB,
    logo_source    TEXT CHECK (logo_source IN ('plaid', 'site', 'none')),
    primary_color  TEXT,                      -- '#rrggbb' from Plaid
    url            TEXT,
    fetched_at     TEXT
);
CREATE TABLE items (
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
CREATE TABLE liabilities (
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
CREATE TABLE link_attempts (
    id               INTEGER PRIMARY KEY,
    created_at       TEXT NOT NULL,
    institution_id   TEXT,
    institution_name TEXT,
    outcome          TEXT NOT NULL,           -- exchanged | duplicate_blocked | rejected | error
    item_id          TEXT,
    error            TEXT
);
CREATE TABLE loan_payments (
    transaction_id TEXT PRIMARY KEY REFERENCES transactions(transaction_id),
    loan_id        INTEGER NOT NULL REFERENCES manual_accounts(id),
    amount         REAL NOT NULL,
    interest       REAL NOT NULL,
    principal      REAL NOT NULL,
    applied_at     TEXT NOT NULL,
    source         TEXT NOT NULL CHECK (source IN ('auto', 'ai', 'user'))
);
CREATE TABLE manual_accounts (
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
CREATE TABLE manual_balance_history (
    manual_account_id INTEGER NOT NULL REFERENCES manual_accounts(id),
    date              TEXT NOT NULL,
    balance           REAL NOT NULL,
    PRIMARY KEY (manual_account_id, date)
);
CREATE TABLE mcp_tokens (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,        -- SHA-256 of the token
    last4        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE TABLE pending_actions (
    id         INTEGER PRIMARY KEY,
    tool       TEXT NOT NULL,                 -- finance_set_category | finance_add_rule | finance_set_cap
    arguments  TEXT NOT NULL,                 -- JSON, as validated
    summary    TEXT NOT NULL,                 -- the change in plain words (what the owner approves or rejects)
    client     TEXT,                          -- the MCP client's name (its clientInfo) when the call carries it: over
                                              -- stdio, or the 2026 protocol over HTTP (a stateless 2025 POST doesn't)
    status     TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'approved', 'rejected', 'applied', 'failed')),
    created_at TEXT NOT NULL,                 -- UTC timestamp
    decided_at TEXT,
    result     TEXT                           -- JSON: what applying it did (or why it failed)
);
CREATE TABLE plaid_category_map (
    pfc_code    TEXT PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id)
);
INSERT INTO "plaid_category_map" VALUES('INCOME',10);
INSERT INTO "plaid_category_map" VALUES('TRANSFER_IN',11);
INSERT INTO "plaid_category_map" VALUES('TRANSFER_OUT',11);
INSERT INTO "plaid_category_map" VALUES('LOAN_PAYMENTS',11);
INSERT INTO "plaid_category_map" VALUES('LOAN_PAYMENTS_MORTGAGE_PAYMENT',1);
INSERT INTO "plaid_category_map" VALUES('LOAN_PAYMENTS_CAR_PAYMENT',4);
INSERT INTO "plaid_category_map" VALUES('LOAN_DISBURSEMENTS',11);
INSERT INTO "plaid_category_map" VALUES('BANK_FEES',12);
INSERT INTO "plaid_category_map" VALUES('ENTERTAINMENT',7);
INSERT INTO "plaid_category_map" VALUES('FOOD_AND_DRINK',3);
INSERT INTO "plaid_category_map" VALUES('FOOD_AND_DRINK_GROCERIES',2);
INSERT INTO "plaid_category_map" VALUES('GENERAL_MERCHANDISE',5);
INSERT INTO "plaid_category_map" VALUES('HOME_IMPROVEMENT',5);
INSERT INTO "plaid_category_map" VALUES('MEDICAL',8);
INSERT INTO "plaid_category_map" VALUES('PERSONAL_CARE',8);
INSERT INTO "plaid_category_map" VALUES('GENERAL_SERVICES',13);
INSERT INTO "plaid_category_map" VALUES('GOVERNMENT_AND_NON_PROFIT',13);
INSERT INTO "plaid_category_map" VALUES('TRANSPORTATION',4);
INSERT INTO "plaid_category_map" VALUES('TRAVEL',9);
INSERT INTO "plaid_category_map" VALUES('RENT_AND_UTILITIES',1);
CREATE TABLE rules (
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
CREATE TABLE saved_filters (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    filters    TEXT NOT NULL,                  -- JSON {q, category, bank, kind, tag} (empty ones left out)
    created_at TEXT NOT NULL
);
CREATE TABLE security_logos (
    ticker     TEXT PRIMARY KEY,
    png        BLOB,
    source     TEXT NOT NULL CHECK (source IN ('fmp', 'none')),
    fetched_at TEXT NOT NULL
);
CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,                 -- JSON
    updated_at TEXT NOT NULL
);
CREATE TABLE sync_log (
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
CREATE TABLE tags (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);
CREATE TABLE transaction_notes (
    transaction_id TEXT PRIMARY KEY REFERENCES transactions(transaction_id),
    note           TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE TABLE transaction_splits (
    id             INTEGER PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
    line           INTEGER NOT NULL,           -- 1, 2, ... in the order shown
    category_id    INTEGER NOT NULL REFERENCES categories(id),
    amount         REAL NOT NULL,              -- whole cents
    created_at     TEXT NOT NULL,
    UNIQUE (transaction_id, line)
);
CREATE TABLE transaction_tags (
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
    tag_id         INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (transaction_id, tag_id)
);
CREATE TABLE transactions (
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
, source TEXT NOT NULL DEFAULT 'plaid' CHECK (source IN ('plaid', 'import')), import_id INTEGER REFERENCES imports(id), fitid TEXT);
CREATE TABLE vin_decodes (
    vin           TEXT PRIMARY KEY,
    vehicle_year  INTEGER,
    vehicle_make  TEXT,
    vehicle_model TEXT,
    vehicle_trim  TEXT,
    fetched_at    TEXT NOT NULL,
    specs_json    TEXT                        -- v8: the extended decode's specs and safety equipment ({} = none)
);
CREATE INDEX idx_transactions_date ON transactions(date);
CREATE INDEX idx_transactions_item ON transactions(item_id);
CREATE INDEX idx_transactions_account ON transactions(account_id);
CREATE INDEX idx_transactions_merchant ON transactions(merchant_name);
CREATE TRIGGER trg_classifications_protect_user
BEFORE UPDATE ON classifications
WHEN OLD.source = 'user' AND NEW.source <> 'user'
BEGIN
    SELECT RAISE(ABORT, 'user classifications are never overwritten');
END;
CREATE INDEX idx_loan_payments_loan ON loan_payments(loan_id);
CREATE INDEX idx_asset_valuations_account ON asset_valuations(account_id, fetched_at);
CREATE INDEX idx_api_calls_provider ON api_calls(provider, day);
CREATE INDEX idx_api_calls_account ON api_calls(account_id, provider, day);
CREATE UNIQUE INDEX idx_transactions_fitid ON transactions(account_id, fitid) WHERE fitid IS NOT NULL;
CREATE VIEW v_items AS
SELECT item_id, institution_id, institution_name, status, error_code, error_message, products,
       txn_update_status, created_at, last_sync_at, removed_at
FROM items;
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
          AND NOT (COALESCE(c.source = 'user' OR (c.source = 'rule' AND ru.created_from = 'user'), 0)
                   AND c.updated_at >= r.reviewed_at)
         THEN 1 ELSE 0 END                                  AS ai_flagged,
    t.source                                                AS source   -- plaid | import (TASK-18)
FROM transactions t
LEFT JOIN accounts a            ON a.account_id = t.account_id
LEFT JOIN items i               ON i.item_id = t.item_id
LEFT JOIN manual_accounts ma    ON t.source = 'import' AND ma.id = CAST(substr(t.account_id, 8) AS INTEGER)
LEFT JOIN classifications c     ON c.transaction_id = t.transaction_id
LEFT JOIN rules ru              ON ru.id = c.rule_id
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
CREATE INDEX idx_transaction_splits_txn ON transaction_splits(transaction_id);
CREATE INDEX idx_transaction_tags_tag ON transaction_tags(tag_id);
CREATE INDEX idx_goal_entries_goal ON goal_entries(goal_id, date);
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
COMMIT;

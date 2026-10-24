-- Schema v6 exactly as created by hermes_finance/db.py at commit c35fdba (SCHEMA_VERSION = 6),
-- dumped with sqlite3 iterdump. Used by test_schema_upgrade.py; do not edit.
PRAGMA user_version = 6;
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
CREATE TABLE classifications (
    transaction_id TEXT PRIMARY KEY REFERENCES transactions(transaction_id),
    category_id    INTEGER NOT NULL REFERENCES categories(id),
    source         TEXT NOT NULL CHECK (source IN ('plaid', 'rule', 'ai', 'user')),
    confidence     REAL,
    note           TEXT,
    rule_id        INTEGER REFERENCES rules(id),
    updated_at     TEXT NOT NULL
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
    archived_at     TEXT
);
CREATE TABLE manual_balance_history (
    manual_account_id INTEGER NOT NULL REFERENCES manual_accounts(id),
    date              TEXT NOT NULL,
    balance           REAL NOT NULL,
    PRIMARY KEY (manual_account_id, date)
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
    a.name                                                  AS account_name,
    a.mask                                                  AS account_mask,
    a.type                                                  AS account_type,
    a.subtype                                               AS account_subtype,
    t.item_id,
    i.institution_name,
    t.website,
    t.logo_url,
    r.verdict                                               AS ai_verdict,
    r.note                                                  AS ai_note,
    -- flagged by the AI and not decided by the user since (a user classification, or a user rule)
    CASE WHEN r.verdict = 'flagged'
          AND NOT (COALESCE(c.source = 'user' OR (c.source = 'rule' AND ru.created_from = 'user'), 0)
                   AND c.updated_at >= r.reviewed_at)
         THEN 1 ELSE 0 END                                  AS ai_flagged
FROM transactions t
LEFT JOIN accounts a            ON a.account_id = t.account_id
LEFT JOIN items i               ON i.item_id = t.item_id
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
COMMIT;

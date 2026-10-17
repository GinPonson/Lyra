-- Lyra DuckDB bootstrap schema, version 1.
--
-- This file defines only durable ledger state required by Phase 1. Gmail OAuth,
-- Agent sessions and device pairing receive separate migrations when their
-- application flows are implemented; keeping them out avoids speculative tables.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version       INTEGER PRIMARY KEY,
    description   VARCHAR NOT NULL,
    applied_at    TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE SEQUENCE IF NOT EXISTS ledger_version_seq START 1;
CREATE SEQUENCE IF NOT EXISTS lot_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS realized_id_seq START 1;

-- A Lyra account is one Schwab brokerage account, not an application user.
CREATE TABLE IF NOT EXISTS accounts (
    id                  UUID PRIMARY KEY,
    display_name        VARCHAR NOT NULL CHECK (length(trim(display_name)) > 0),
    legacy_account_key  VARCHAR NOT NULL UNIQUE CHECK (length(trim(legacy_account_key)) > 0),
    broker              VARCHAR NOT NULL DEFAULT 'schwab',
    masked_suffix       VARCHAR NOT NULL CHECK (length(trim(masked_suffix)) > 0),
    base_currency       VARCHAR NOT NULL DEFAULT 'USD' CHECK (length(base_currency) = 3),
    status              VARCHAR NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'archived')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    CHECK (updated_at >= created_at)
);

-- Every successful mutation creates one immutable, monotonically increasing version.
CREATE TABLE IF NOT EXISTS ledger_versions (
    version           BIGINT PRIMARY KEY DEFAULT nextval('ledger_version_seq'),
    reason            VARCHAR NOT NULL
                      CHECK (reason IN ('file_import', 'gmail_import', 'fifo_rebuild')),
    source_reference  VARCHAR,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    CHECK (version > 0)
);

-- A version can affect multiple accounts, while FIFO remains isolated per account.
CREATE TABLE IF NOT EXISTS ledger_version_accounts (
    ledger_version  BIGINT NOT NULL REFERENCES ledger_versions(version),
    account_id      UUID NOT NULL REFERENCES accounts(id),
    PRIMARY KEY (ledger_version, account_id)
);

-- Raw uploads are temporary. This table retains only metadata, validation state and audit totals.
CREATE TABLE IF NOT EXISTS import_files (
    id                       UUID PRIMARY KEY,
    file_hash                VARCHAR NOT NULL CHECK (length(file_hash) = 64),
    original_file_name       VARCHAR NOT NULL CHECK (length(trim(original_file_name)) > 0),
    file_type                VARCHAR NOT NULL CHECK (file_type IN ('csv', 'xlsx')),
    file_size_bytes          BIGINT NOT NULL CHECK (file_size_bytes >= 0),
    parser_version           VARCHAR NOT NULL,
    status                   VARCHAR NOT NULL CHECK (
                                 status IN (
                                     'uploaded', 'parsing', 'preview_ready', 'committing',
                                     'committed', 'rejected', 'failed', 'cancelled'
                                 )
                             ),
    target_account_id        UUID REFERENCES accounts(id),
    preview_token_hash       VARCHAR,
    preview_ledger_version   BIGINT REFERENCES ledger_versions(version),
    total_rows               INTEGER CHECK (total_rows IS NULL OR total_rows >= 0),
    inserted_rows            INTEGER CHECK (inserted_rows IS NULL OR inserted_rows >= 0),
    skipped_rows             INTEGER CHECK (skipped_rows IS NULL OR skipped_rows >= 0),
    error_code               VARCHAR,
    error_summary            VARCHAR,
    committed_ledger_version BIGINT REFERENCES ledger_versions(version),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    CHECK (updated_at >= created_at)
);

-- Canonical transaction fact table. CSV, Excel and Gmail all enter through this table.
CREATE TABLE IF NOT EXISTS transactions (
    account_id          UUID NOT NULL REFERENCES accounts(id),
    record_date         DATE NOT NULL,
    transaction_date    DATE NOT NULL,
    action              VARCHAR NOT NULL CHECK (
                            action IN (
                                'Buy', 'Sell', 'Buy to Open', 'Sell to Open',
                                'Buy to Close', 'Sell to Close', 'Expired', 'Assigned',
                                'MoneyLink Transfer', 'Credit Interest',
                                'Qualified Dividend', 'Non-Qualified Div',
                                'Pr Yr Non-Qual Div', 'NRA Tax Adj', 'Pr Yr NRA Tax',
                                'Foreign Tax Paid'
                            )
                        ),
    raw_symbol          VARCHAR,
    description         VARCHAR,
    asset_type          VARCHAR NOT NULL CHECK (asset_type IN ('stock', 'option', 'cash')),
    underlying          VARCHAR,
    expiry              DATE,
    strike              DECIMAL(18, 4),
    option_type         VARCHAR CHECK (option_type IN ('C', 'P')),
    quantity            DECIMAL(18, 6),
    price               DECIMAL(18, 6),
    fees                DECIMAL(18, 6),
    amount              DECIMAL(18, 4),
    source_type         VARCHAR NOT NULL CHECK (source_type IN ('csv', 'excel', 'gmail')),
    source_reference    VARCHAR NOT NULL,
    import_file_id      UUID REFERENCES import_files(id),
    source_hash         VARCHAR NOT NULL CHECK (length(source_hash) = 64),
    sequence            INTEGER NOT NULL CHECK (sequence > 0),
    occurrence          INTEGER NOT NULL CHECK (occurrence > 0),
    business_hash       VARCHAR NOT NULL CHECK (length(business_hash) = 64),
    transaction_hash    VARCHAR NOT NULL CHECK (length(transaction_hash) = 64),
    parser_version      VARCHAR NOT NULL,
    ledger_version      BIGINT NOT NULL REFERENCES ledger_versions(version),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (account_id, transaction_hash),
    CHECK (
        asset_type = 'cash'
        OR (raw_symbol IS NOT NULL AND underlying IS NOT NULL AND quantity > 0)
    ),
    CHECK (
        asset_type <> 'option'
        OR (expiry IS NOT NULL AND strike IS NOT NULL AND option_type IS NOT NULL)
    ),
    CHECK (
        asset_type = 'option'
        OR (expiry IS NULL AND strike IS NULL AND option_type IS NULL)
    )
);

-- Only currently open FIFO lots are persisted; rebuild atomically replaces this table's rows.
CREATE TABLE IF NOT EXISTS lots (
    lot_id                 BIGINT PRIMARY KEY DEFAULT nextval('lot_id_seq'),
    account_id             UUID NOT NULL REFERENCES accounts(id),
    symbol_key             VARCHAR NOT NULL,
    asset_type             VARCHAR NOT NULL CHECK (asset_type IN ('stock', 'option')),
    underlying             VARCHAR NOT NULL,
    expiry                 DATE,
    strike                 DECIMAL(18, 4),
    option_type            VARCHAR CHECK (option_type IN ('C', 'P')),
    direction              VARCHAR NOT NULL CHECK (direction IN ('long', 'short')),
    open_date              DATE NOT NULL,
    open_price             DECIMAL(18, 6) NOT NULL,
    open_quantity          DECIMAL(18, 6) NOT NULL CHECK (open_quantity > 0),
    remaining_quantity     DECIMAL(18, 6) NOT NULL CHECK (remaining_quantity > 0),
    open_fees              DECIMAL(18, 6) NOT NULL DEFAULT 0,
    fees_allocated         DECIMAL(18, 6) NOT NULL DEFAULT 0,
    open_transaction_hash  VARCHAR NOT NULL,
    ledger_version         BIGINT NOT NULL REFERENCES ledger_versions(version),
    FOREIGN KEY (account_id, open_transaction_hash)
        REFERENCES transactions(account_id, transaction_hash),
    CHECK (remaining_quantity <= open_quantity),
    CHECK (fees_allocated >= 0),
    CHECK (
        asset_type <> 'option'
        OR (expiry IS NOT NULL AND strike IS NOT NULL AND option_type IS NOT NULL)
    ),
    CHECK (
        asset_type = 'option'
        OR (expiry IS NULL AND strike IS NULL AND option_type IS NULL)
    )
);

-- Each realized row links the exact opening and closing transaction used by FIFO.
CREATE TABLE IF NOT EXISTS realized (
    realized_id            BIGINT PRIMARY KEY DEFAULT nextval('realized_id_seq'),
    account_id             UUID NOT NULL REFERENCES accounts(id),
    symbol_key             VARCHAR NOT NULL,
    asset_type             VARCHAR NOT NULL CHECK (asset_type IN ('stock', 'option')),
    underlying             VARCHAR NOT NULL,
    direction              VARCHAR NOT NULL CHECK (direction IN ('long', 'short')),
    open_date              DATE NOT NULL,
    close_date             DATE NOT NULL,
    quantity               DECIMAL(18, 6) NOT NULL CHECK (quantity > 0),
    open_price             DECIMAL(18, 6) NOT NULL,
    close_price            DECIMAL(18, 6) NOT NULL,
    open_fees              DECIMAL(18, 6) NOT NULL DEFAULT 0,
    close_fees             DECIMAL(18, 6) NOT NULL DEFAULT 0,
    pnl                    DECIMAL(18, 4) NOT NULL,
    close_action           VARCHAR NOT NULL,
    open_transaction_hash  VARCHAR NOT NULL,
    close_transaction_hash VARCHAR NOT NULL,
    ledger_version         BIGINT NOT NULL REFERENCES ledger_versions(version),
    FOREIGN KEY (account_id, open_transaction_hash)
        REFERENCES transactions(account_id, transaction_hash),
    FOREIGN KEY (account_id, close_transaction_hash)
        REFERENCES transactions(account_id, transaction_hash),
    CHECK (close_date >= open_date)
);

-- Current-position view retains account boundaries; cross-account grouping happens explicitly above it.
CREATE OR REPLACE VIEW v_positions AS
SELECT
    account_id,
    symbol_key,
    asset_type,
    underlying,
    expiry,
    strike,
    option_type,
    direction,
    sum(remaining_quantity) AS quantity,
    sum(remaining_quantity * open_price) AS cost,
    min(open_date) AS first_open_date,
    max(ledger_version) AS ledger_version
FROM lots
GROUP BY
    account_id, symbol_key, asset_type, underlying, expiry, strike, option_type, direction;

CREATE OR REPLACE VIEW v_realized_summary AS
SELECT
    account_id,
    underlying,
    asset_type,
    strftime(close_date, '%Y-%m') AS month,
    count(*) AS closed_lots,
    count(DISTINCT close_transaction_hash) AS close_transactions,
    sum(pnl) AS pnl,
    sum(open_fees + close_fees) AS fees,
    max(ledger_version) AS ledger_version
FROM realized
GROUP BY account_id, underlying, asset_type, month;

CREATE OR REPLACE VIEW v_cashflows AS
SELECT
    account_id,
    transaction_date,
    action,
    description,
    amount,
    transaction_hash,
    ledger_version
FROM transactions
WHERE asset_type = 'cash';

INSERT INTO schema_migrations (version, description)
VALUES (1, 'phase 1 ledger core')
ON CONFLICT (version) DO NOTHING;

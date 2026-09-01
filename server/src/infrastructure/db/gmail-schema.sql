-- Lyra DuckDB migration 2: Gmail connection and deterministic message metadata.
--
-- Apply after schema.sql. Full message bodies, OAuth authorization codes, access
-- tokens and plaintext refresh tokens are intentionally absent from this schema.

CREATE TABLE IF NOT EXISTS gmail_connections (
    id                       UUID PRIMARY KEY,
    google_subject           VARCHAR NOT NULL UNIQUE CHECK (length(trim(google_subject)) > 0),
    google_email             VARCHAR NOT NULL CHECK (length(trim(google_email)) > 0),
    encrypted_refresh_token  VARCHAR,
    granted_scopes           VARCHAR NOT NULL,
    history_id               VARCHAR,
    status                   VARCHAR NOT NULL CHECK (
                                 status IN (
                                     'pending_initial_scan', 'active', 'needs_resync',
                                     'disconnected', 'error'
                                 )
                             ),
    last_error_code          VARCHAR,
    last_sync_at             TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    CHECK (updated_at >= created_at),
    CHECK (status = 'disconnected' OR encrypted_refresh_token IS NOT NULL)
);

-- A suffix is only a clue. One connection/suffix becomes usable after the user
-- explicitly chooses one Lyra account; two accounts are never selected implicitly.
CREATE TABLE IF NOT EXISTS gmail_account_mappings (
    id               UUID PRIMARY KEY,
    connection_id    UUID NOT NULL REFERENCES gmail_connections(id),
    account_suffix   VARCHAR NOT NULL CHECK (length(trim(account_suffix)) > 0),
    account_id       UUID NOT NULL REFERENCES accounts(id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    UNIQUE (connection_id, account_suffix),
    CHECK (updated_at >= created_at)
);

-- Store only identifiers, hashes and deterministic parsing outcomes. The body is
-- discarded after parsing and must never enter Agent context or durable logs.
CREATE TABLE IF NOT EXISTS gmail_messages (
    connection_id     UUID NOT NULL REFERENCES gmail_connections(id),
    message_id        VARCHAR NOT NULL,
    thread_id         VARCHAR,
    received_at       TIMESTAMPTZ,
    content_hash      VARCHAR,
    account_id        UUID REFERENCES accounts(id),
    parse_status      VARCHAR NOT NULL CHECK (
                          parse_status IN ('discovered', 'parsed', 'imported', 'rejected', 'conflict')
                      ),
    error_code        VARCHAR,
    transaction_count INTEGER NOT NULL DEFAULT 0 CHECK (transaction_count >= 0),
    history_id        VARCHAR,
    synced_at         TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (connection_id, message_id),
    CHECK (
        parse_status = 'discovered'
        OR (content_hash IS NOT NULL AND length(content_hash) = 64)
    )
);

-- Preserve which canonical transactions came from each message without copying
-- transaction fields or email content into a second ledger model.
CREATE TABLE IF NOT EXISTS gmail_message_transactions (
    connection_id    UUID NOT NULL,
    message_id       VARCHAR NOT NULL,
    account_id       UUID NOT NULL,
    transaction_hash VARCHAR NOT NULL,
    ordinal          INTEGER NOT NULL CHECK (ordinal > 0),
    PRIMARY KEY (connection_id, message_id, ordinal),
    UNIQUE (connection_id, message_id, account_id, transaction_hash),
    FOREIGN KEY (connection_id, message_id)
        REFERENCES gmail_messages(connection_id, message_id),
    FOREIGN KEY (account_id, transaction_hash)
        REFERENCES transactions(account_id, transaction_hash)
);

INSERT INTO schema_migrations (version, description)
VALUES (2, 'gmail connections and message metadata')
ON CONFLICT (version) DO NOTHING;

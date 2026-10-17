-- Lyra DuckDB migration 14: owner-wide market source preferences.
-- This row contains public source identifiers only. Credentials remain in the
-- server environment, and market changes never create a ledger version.
CREATE TABLE IF NOT EXISTS market_source_settings (
    singleton_key INTEGER PRIMARY KEY CHECK (singleton_key = 1),
    revision      BIGINT NOT NULL CHECK (revision BETWEEN 1 AND 9007199254740991),
    routes_json   JSON NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL
);

INSERT INTO schema_migrations (version, description)
VALUES (14, 'owner-wide market source preferences')
ON CONFLICT (version) DO NOTHING;

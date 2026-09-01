-- Lyra DuckDB migration 7: short-lived import preview authorization metadata.
--
-- The plaintext confirmation token is returned once and never persisted. The
-- fingerprint binds the token to the exact parsed preview and ledger baseline.

ALTER TABLE import_files
    ADD COLUMN IF NOT EXISTS preview_fingerprint VARCHAR;

ALTER TABLE import_files
    ADD COLUMN IF NOT EXISTS preview_expires_at TIMESTAMPTZ;

INSERT INTO schema_migrations (version, description)
VALUES (7, 'short-lived import preview authorization metadata')
ON CONFLICT (version) DO NOTHING;

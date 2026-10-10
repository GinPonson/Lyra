-- Lyra DuckDB migration 13: one private owner and OAuth-only device enrollment.
-- Provider subject, never email alone, is the immutable identity after enrollment.
CREATE TABLE IF NOT EXISTS server_owner_identity (
    singleton_key INTEGER PRIMARY KEY CHECK (singleton_key = 1),
    provider      VARCHAR NOT NULL CHECK (provider = 'google'),
    subject       VARCHAR NOT NULL CHECK (length(subject) BETWEEN 1 AND 255),
    email         VARCHAR NOT NULL,
    display_name  VARCHAR,
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    UNIQUE (provider, subject)
);

-- Credentials issued by the previous unauthenticated pairing endpoints must not
-- remain a login bypass. Users sign in once after this upgrade; ledgers remain.
UPDATE device_access_tokens
SET status = 'revoked', revoked_at = current_timestamp
WHERE status = 'active';

UPDATE device_refresh_credentials
SET status = 'revoked', revoked_at = current_timestamp
WHERE status <> 'revoked';

UPDATE paired_devices
SET status = 'revoked', revoked_at = current_timestamp,
    updated_at = greatest(updated_at, current_timestamp)
WHERE status = 'active';

INSERT INTO schema_migrations (version, description)
VALUES (13, 'single owner Google login and credential reauthorization')
ON CONFLICT (version) DO NOTHING;

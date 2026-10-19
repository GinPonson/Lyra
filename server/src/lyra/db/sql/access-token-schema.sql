-- Lyra DuckDB migration 6: short-lived paired-device access tokens.
--
-- Apply after schema.sql and auth-schema.sql. Only SHA-256 token hashes are
-- durable; plaintext Bearer tokens are returned once and never logged.

CREATE TABLE IF NOT EXISTS device_access_tokens (
    id            UUID PRIMARY KEY,
    device_id     UUID NOT NULL REFERENCES paired_devices(id),
    token_hash    VARCHAR NOT NULL UNIQUE CHECK (length(token_hash) = 64),
    status        VARCHAR NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'revoked')),
    expires_at    TIMESTAMPTZ NOT NULL,
    last_seen_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    issued_at     TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    CHECK (expires_at > issued_at),
    CHECK (last_seen_at IS NULL OR last_seen_at >= issued_at),
    CHECK (
        (status = 'active' AND revoked_at IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL)
    )
);

INSERT INTO schema_migrations (version, description)
VALUES (6, 'short-lived device access tokens')
ON CONFLICT (version) DO NOTHING;

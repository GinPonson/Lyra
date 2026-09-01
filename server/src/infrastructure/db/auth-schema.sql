-- Lyra DuckDB migration 3: paired Android devices and refresh credentials.
--
-- Apply after schema.sql. Pairing challenges intentionally remain short-lived
-- process state, so a server restart invalidates every outstanding pairing code.
-- Plaintext access tokens, refresh credentials and device private keys must never
-- enter this schema.

CREATE TABLE IF NOT EXISTS paired_devices (
    id                      UUID PRIMARY KEY,
    display_name            VARCHAR NOT NULL CHECK (length(trim(display_name)) > 0),
    public_key_pem          VARCHAR NOT NULL CHECK (length(trim(public_key_pem)) > 0),
    public_key_fingerprint  VARCHAR NOT NULL UNIQUE
                            CHECK (length(public_key_fingerprint) = 64),
    key_algorithm           VARCHAR NOT NULL CHECK (length(trim(key_algorithm)) > 0),
    status                  VARCHAR NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'revoked')),
    paired_at               TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    last_seen_at            TIMESTAMPTZ,
    revoked_at              TIMESTAMPTZ,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    CHECK (last_seen_at IS NULL OR last_seen_at >= paired_at),
    CHECK (updated_at >= paired_at),
    CHECK (
        (status = 'active' AND revoked_at IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL)
    )
);

-- Each family supports rotation without storing a plaintext credential. Reuse of
-- a rotated token can revoke all generations in the same family transactionally.
CREATE TABLE IF NOT EXISTS device_refresh_credentials (
    id             UUID PRIMARY KEY,
    device_id      UUID NOT NULL REFERENCES paired_devices(id),
    family_id      UUID NOT NULL,
    generation     INTEGER NOT NULL CHECK (generation > 0),
    token_hash     VARCHAR NOT NULL UNIQUE CHECK (length(token_hash) = 64),
    status         VARCHAR NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'rotated', 'revoked')),
    expires_at     TIMESTAMPTZ NOT NULL,
    last_used_at   TIMESTAMPTZ,
    rotated_at     TIMESTAMPTZ,
    revoked_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    UNIQUE (device_id, family_id, generation),
    CHECK (expires_at > created_at),
    CHECK (last_used_at IS NULL OR last_used_at >= created_at),
    CHECK (
        (status = 'active' AND rotated_at IS NULL AND revoked_at IS NULL)
        OR (status = 'rotated' AND rotated_at IS NOT NULL AND revoked_at IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL)
    )
);

INSERT INTO schema_migrations (version, description)
VALUES (3, 'paired devices and refresh credentials')
ON CONFLICT (version) DO NOTHING;

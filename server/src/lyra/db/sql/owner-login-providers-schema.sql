-- Lyra DuckDB migration 15: explicitly configured identities for one private owner.
-- Each provider has one immutable subject. Email/name never links two identities.
CREATE TABLE server_owner_identity_v15 (
    singleton_key INTEGER NOT NULL CHECK (singleton_key = 1),
    provider      VARCHAR PRIMARY KEY CHECK (provider IN ('google', 'linuxdo', 'github')),
    subject       VARCHAR NOT NULL CHECK (length(subject) BETWEEN 1 AND 255),
    email         VARCHAR,
    display_name  VARCHAR,
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    UNIQUE (provider, subject)
);

INSERT INTO server_owner_identity_v15
    (singleton_key, provider, subject, email, display_name, created_at, updated_at)
SELECT singleton_key, provider, subject, email, display_name, created_at, updated_at
FROM server_owner_identity;

DROP TABLE server_owner_identity;
ALTER TABLE server_owner_identity_v15 RENAME TO server_owner_identity;

-- Existing Google owner, device sessions, ledgers and source settings remain valid.
INSERT INTO schema_migrations (version, description)
VALUES (15, 'multiple configured OAuth identities for the private owner')
ON CONFLICT (version) DO NOTHING;

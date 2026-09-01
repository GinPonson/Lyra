-- Lyra DuckDB migration 4: append-only security and business audit events.
--
-- Apply after schema.sql and auth-schema.sql. Application repositories expose
-- inserts and reads only; update/delete operations are intentionally absent.

CREATE TABLE IF NOT EXISTS audit_events (
    id              UUID PRIMARY KEY,
    occurred_at     TIMESTAMPTZ NOT NULL,
    event_type      VARCHAR NOT NULL CHECK (length(trim(event_type)) > 0),
    outcome         VARCHAR NOT NULL CHECK (outcome IN ('success', 'denied', 'failure')),
    actor_type      VARCHAR NOT NULL CHECK (actor_type IN ('system', 'device')),
    actor_id        UUID REFERENCES paired_devices(id),
    request_id      VARCHAR,
    target_type     VARCHAR,
    target_id       VARCHAR,
    account_id      UUID REFERENCES accounts(id),
    ledger_version  BIGINT REFERENCES ledger_versions(version),
    error_code      VARCHAR,
    metadata_json   JSON NOT NULL DEFAULT '{}',
    CHECK (
        (actor_type = 'system' AND actor_id IS NULL)
        OR (actor_type = 'device' AND actor_id IS NOT NULL)
    ),
    CHECK ((target_type IS NULL) = (target_id IS NULL)),
    CHECK (
        (outcome = 'success' AND error_code IS NULL)
        OR (outcome IN ('denied', 'failure') AND error_code IS NOT NULL)
    )
);

INSERT INTO schema_migrations (version, description)
VALUES (4, 'security and business audit events')
ON CONFLICT (version) DO NOTHING;

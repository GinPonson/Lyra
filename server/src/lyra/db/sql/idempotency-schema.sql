-- Lyra DuckDB migration 10: device-scoped mutation idempotency records.
--
-- Plaintext Idempotency-Key values and response bodies must never enter this
-- table. Completed rows only retain a resource reference so routes can rebuild
-- a current safe response without persisting access tokens or OAuth secrets.

CREATE TABLE IF NOT EXISTS mutation_idempotency_records (
    id                    UUID PRIMARY KEY,
    device_id             UUID NOT NULL REFERENCES paired_devices(id),
    operation             VARCHAR NOT NULL CHECK (length(trim(operation)) > 1),
    key_hash              VARCHAR NOT NULL CHECK (length(key_hash) = 64),
    request_hash          VARCHAR NOT NULL CHECK (length(request_hash) = 64),
    status                VARCHAR NOT NULL DEFAULT 'in_progress'
                          CHECK (status IN ('in_progress', 'completed')),
    result_resource_id    VARCHAR,
    response_status_code  INTEGER,
    created_at            TIMESTAMPTZ NOT NULL,
    expires_at            TIMESTAMPTZ NOT NULL,
    completed_at          TIMESTAMPTZ,
    UNIQUE (device_id, operation, key_hash),
    CHECK (expires_at > created_at),
    CHECK (
        (
            status = 'in_progress'
            AND result_resource_id IS NULL
            AND response_status_code IS NULL
            AND completed_at IS NULL
        )
        OR (
            status = 'completed'
            AND length(trim(result_resource_id)) > 0
            AND response_status_code BETWEEN 200 AND 299
            AND completed_at IS NOT NULL
            AND completed_at >= created_at
        )
    )
);

INSERT INTO schema_migrations (version, description)
VALUES (10, 'device-scoped mutation idempotency records')
ON CONFLICT (version) DO NOTHING;

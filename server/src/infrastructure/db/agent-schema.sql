-- Lyra DuckDB migration 5: normalized Agent sessions, messages and tool calls.
--
-- Apply after schema.sql and auth-schema.sql. Provider-native caches and hidden
-- reasoning are intentionally absent. A reconnect reads the normalized snapshot;
-- event sequence gaps are never reconstructed from guessed deltas.

CREATE TABLE IF NOT EXISTS agent_sessions (
    id                   UUID PRIMARY KEY,
    device_id            UUID NOT NULL REFERENCES paired_devices(id),
    provider             VARCHAR NOT NULL CHECK (provider IN ('claude', 'pi', 'opencode')),
    status               VARCHAR NOT NULL CHECK (
                             status IN ('idle', 'running', 'cancelling', 'closed')
                         ),
    account_scope_kind   VARCHAR NOT NULL CHECK (account_scope_kind IN ('all', 'account')),
    account_id           UUID REFERENCES accounts(id),
    ledger_version       BIGINT NOT NULL CHECK (ledger_version >= 0),
    as_of                TIMESTAMPTZ NOT NULL,
    revision             INTEGER NOT NULL CHECK (revision > 0),
    last_event_sequence  BIGINT NOT NULL DEFAULT 0 CHECK (last_event_sequence >= 0),
    summary              VARCHAR,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    closed_at            TIMESTAMPTZ,
    CHECK (
        (account_scope_kind = 'all' AND account_id IS NULL)
        OR (account_scope_kind = 'account' AND account_id IS NOT NULL)
    ),
    CHECK (updated_at >= created_at),
    CHECK (
        (status = 'closed' AND closed_at IS NOT NULL)
        OR (status <> 'closed' AND closed_at IS NULL)
    )
);

-- Only normalized visible content is durable. Provider-internal messages, hidden
-- reasoning and raw protocol frames must not be copied into this table.
CREATE TABLE IF NOT EXISTS agent_messages (
    id            UUID PRIMARY KEY,
    session_id    UUID NOT NULL REFERENCES agent_sessions(id),
    ordinal       INTEGER NOT NULL CHECK (ordinal > 0),
    role          VARCHAR NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    provider      VARCHAR CHECK (provider IN ('claude', 'pi', 'opencode')),
    status        VARCHAR NOT NULL CHECK (
                      status IN ('streaming', 'completed', 'failed', 'cancelled')
                  ),
    content       VARCHAR NOT NULL,
    error_code    VARCHAR,
    created_at    TIMESTAMPTZ NOT NULL,
    completed_at  TIMESTAMPTZ,
    UNIQUE (session_id, id),
    UNIQUE (session_id, ordinal),
    CHECK (
        (status = 'streaming' AND completed_at IS NULL AND error_code IS NULL)
        OR (status = 'completed' AND completed_at IS NOT NULL AND error_code IS NULL)
        OR (status IN ('failed', 'cancelled') AND completed_at IS NOT NULL)
    )
);

-- Arguments and results are deliberately summaries produced after policy checks,
-- not raw CLI frames. Every call retains the authoritative ledger version used.
CREATE TABLE IF NOT EXISTS agent_tool_calls (
    id                 UUID PRIMARY KEY,
    session_id         UUID NOT NULL REFERENCES agent_sessions(id),
    message_id         UUID NOT NULL,
    ordinal            INTEGER NOT NULL CHECK (ordinal > 0),
    tool_name          VARCHAR NOT NULL CHECK (length(trim(tool_name)) > 0),
    authorization_decision VARCHAR NOT NULL
                           CHECK (authorization_decision IN ('allowed', 'denied')),
    status             VARCHAR NOT NULL CHECK (
                           status IN ('started', 'completed', 'failed', 'cancelled', 'rejected')
                       ),
    arguments_summary  JSON NOT NULL DEFAULT '{}',
    result_summary     JSON,
    ledger_version     BIGINT NOT NULL CHECK (ledger_version >= 0),
    duration_ms        BIGINT CHECK (duration_ms IS NULL OR duration_ms >= 0),
    error_code         VARCHAR,
    started_at         TIMESTAMPTZ NOT NULL,
    completed_at       TIMESTAMPTZ,
    UNIQUE (session_id, message_id, ordinal),
    FOREIGN KEY (session_id, message_id)
        REFERENCES agent_messages(session_id, id),
    CHECK (
        (status = 'started' AND completed_at IS NULL AND error_code IS NULL)
        OR (status = 'completed' AND completed_at IS NOT NULL AND error_code IS NULL)
        OR (status IN ('failed', 'cancelled', 'rejected') AND completed_at IS NOT NULL)
    ),
    CHECK (
        (authorization_decision = 'allowed' AND status <> 'rejected')
        OR (authorization_decision = 'denied' AND status = 'rejected')
    )
);

INSERT INTO schema_migrations (version, description)
VALUES (5, 'normalized agent sessions and tool calls')
ON CONFLICT (version) DO NOTHING;

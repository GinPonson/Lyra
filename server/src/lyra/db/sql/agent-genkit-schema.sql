-- Lyra migration 12: one Genkit runtime, retaining legacy conversation provenance.
-- Old CLI sessions cannot resume in another engine. Close them explicitly while
-- retaining every visible message and tool audit row; financial tables are untouched.
-- DuckDB cannot alter CHECK constraints, so rebuild through constraint-free staging.

CREATE TABLE agent_genkit_sessions_staging AS SELECT * FROM agent_sessions;
CREATE TABLE agent_genkit_messages_staging AS SELECT * FROM agent_messages;
CREATE TABLE agent_genkit_tools_staging AS SELECT * FROM agent_tool_calls;
DROP TABLE agent_tool_calls;
DROP TABLE agent_messages;
DROP TABLE agent_sessions;

CREATE TABLE agent_sessions (
    id                   UUID PRIMARY KEY,
    device_id            UUID NOT NULL REFERENCES paired_devices(id),
    provider             VARCHAR NOT NULL CHECK (provider = 'genkit'),
    original_provider    VARCHAR CHECK (original_provider IN ('claude', 'pi', 'opencode')),
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


INSERT INTO agent_sessions (
    id, device_id, provider, original_provider, status, account_scope_kind, account_id,
    ledger_version, as_of, revision, last_event_sequence, summary,
    created_at, updated_at, closed_at
)
SELECT id, device_id, 'genkit', provider, 'closed', account_scope_kind, account_id,
       ledger_version, as_of, revision + 1, last_event_sequence + 1, summary,
       created_at, greatest(updated_at, current_timestamp),
       coalesce(closed_at, greatest(updated_at, current_timestamp))
FROM agent_genkit_sessions_staging;

CREATE TABLE agent_messages (
    id            UUID PRIMARY KEY,
    session_id    UUID NOT NULL REFERENCES agent_session_identities(id),
    ordinal       INTEGER NOT NULL CHECK (ordinal > 0),
    role          VARCHAR NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    provider      VARCHAR CHECK (provider IN ('genkit', 'claude', 'pi', 'opencode')),
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


INSERT INTO agent_messages (
    id, session_id, ordinal, role, provider, status, content, error_code,
    created_at, completed_at
)
SELECT id, session_id, ordinal, role, provider,
       CASE WHEN status = 'streaming' THEN 'failed' ELSE status END,
       content,
       CASE WHEN status = 'streaming' THEN 'AGENT_TURN_FAILED' ELSE error_code END,
       created_at,
       CASE WHEN status = 'streaming' THEN greatest(created_at, current_timestamp) ELSE completed_at END
FROM agent_genkit_messages_staging;

INSERT INTO agent_messages (
    id, session_id, ordinal, role, provider, status, content, error_code,
    created_at, completed_at
)
SELECT uuid(), s.id, coalesce(m.last_ordinal, 0) + 1, 'system', NULL, 'completed',
       '此会话原先使用 ' || s.original_provider || '，现已归档。Lyra 已迁移到 Genkit，请新建会话继续。',
       NULL, s.updated_at, s.updated_at
FROM agent_sessions s
LEFT JOIN (
    SELECT session_id, max(ordinal) AS last_ordinal
    FROM agent_genkit_messages_staging GROUP BY session_id
) m ON m.session_id = s.id;

CREATE TABLE agent_tool_calls (
    id                 UUID PRIMARY KEY,
    session_id         UUID NOT NULL,
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


INSERT INTO agent_tool_calls (
    id, session_id, message_id, ordinal, tool_name, authorization_decision,
    status, arguments_summary, result_summary, ledger_version, duration_ms,
    error_code, started_at, completed_at
)
SELECT id, session_id, message_id, ordinal, tool_name, authorization_decision,
       CASE WHEN status = 'started' THEN 'cancelled' ELSE status END,
       CAST(arguments_summary AS JSON), CAST(result_summary AS JSON), ledger_version,
       CASE WHEN status = 'started' THEN coalesce(duration_ms, 0) ELSE duration_ms END,
       CASE WHEN status = 'started' THEN 'AGENT_TOOL_CANCELLED' ELSE error_code END,
       started_at,
       CASE WHEN status = 'started' THEN greatest(started_at, current_timestamp) ELSE completed_at END
FROM agent_genkit_tools_staging;

DROP TABLE agent_genkit_tools_staging;
DROP TABLE agent_genkit_messages_staging;
DROP TABLE agent_genkit_sessions_staging;

INSERT INTO schema_migrations (version, description)
VALUES (12, 'single Genkit runtime and archived legacy sessions')
ON CONFLICT (version) DO NOTHING;

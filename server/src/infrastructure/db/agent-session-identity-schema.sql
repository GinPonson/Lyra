-- Lyra DuckDB migration 11: separate immutable Agent session identity from mutable state.
--
-- DuckDB 1.5.5 rejects updates to any column of a parent row while a child foreign key
-- references that row. Agent messages therefore reference an immutable identity table,
-- while agent_sessions remains the mutable lifecycle record. Existing normalized data is
-- copied without changing message ordinals, tool summaries or financial baselines.

CREATE TABLE agent_session_identities (
    id UUID PRIMARY KEY
);

INSERT INTO agent_session_identities (id)
SELECT id FROM agent_sessions;

CREATE TABLE agent_messages_v11 (
    id            UUID PRIMARY KEY,
    session_id    UUID NOT NULL REFERENCES agent_session_identities(id),
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

INSERT INTO agent_messages_v11
SELECT * FROM agent_messages;

CREATE TABLE agent_tool_calls_v11 (
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
        REFERENCES agent_messages_v11(session_id, id),
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

INSERT INTO agent_tool_calls_v11
SELECT * FROM agent_tool_calls;

DROP TABLE agent_tool_calls;
DROP TABLE agent_messages;
ALTER TABLE agent_messages_v11 RENAME TO agent_messages;
ALTER TABLE agent_tool_calls_v11 RENAME TO agent_tool_calls;

INSERT INTO schema_migrations (version, description)
VALUES (11, 'immutable agent session identity for mutable lifecycle')
ON CONFLICT (version) DO NOTHING;

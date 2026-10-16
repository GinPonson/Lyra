-- Lyra DuckDB migration 16: 多用户账本归属（users / user_identities 与各表的 user_id）。
--
-- **为什么必须整库重建**：DuckDB 1.5.6 上「加列 → 回填 → 加约束 → 改名」这条常规路径
-- 完全走不通，以下均为实测结论（复现脚本见 docs/plans/assets/p0/）：
--   F1 非空表上 `ADD COLUMN ... NOT NULL` 不支持（ParserException）；
--   F2 `ALTER TABLE ... ADD/DROP CONSTRAINT` 未实现（NotImplementedException）；
--   F3 复合外键要求父表在 CREATE 时就有对应的 UNIQUE/PK（BinderException）；
--   F4 被外键引用的父表不能 RENAME，所以不能用「建 _v16 → 拷贝 → 删旧 → 改名」；
--   F5 有子引用的父表不能 DROP，`CASCADE` 也没有级联语义。
-- accounts 与 paired_devices 被几乎整张图引用（见下方 DROP 顺序），因此本轮把**全部**
-- 业务表整体重建，而不是逐表挑改：挑改会在 F5 上撞墙，且留下一半新一半旧的约束图。
--
-- 阶段划分（全部在 apply_migrations 的同一个事务内执行，失败即整体回滚到旧库）：
--   阶段 1  每张业务表 CTAS 到无外键暂存表 _stage_<t>；CTAS 完整保留 UUID/DECIMAL/DATE
--   阶段 2  先 DROP 全部视图（DuckDB 允许 DROP 掉视图的基表，但会把视图留成查询即报错的
--           悬空对象，所以视图必须先删），再按依赖倒序 DROP 表（子先父后）
--   阶段 3  以最终名字从零 CREATE schema 16 全部表与视图（约束必须在 CREATE 时给全）
--   阶段 4  回填归属并装载（归属来自调用方预先建好的 _migration_context 临时表）
--   阶段 5  DROP 全部 _stage_<t>
--
-- **前置条件**：调用方 `apply_migrations` 必须先建好并填充 `_migration_context`。
-- 该表一行一条已确认身份，用户级字段在每行重复（见 migrations.py 的 MigrationContext）。
-- 库非空却没有上下文时，`apply_migrations` 会在执行本脚本之前明确失败，绝不把旧数据
-- 分配给某个默认用户或首次登录者（计划 §9.3）。
--
-- 归属列一律写成 `(SELECT DISTINCT owner_user_id FROM _migration_context)`：上下文为空时
-- 这个标量子查询返回 NULL，于是**非空旧库会在 NOT NULL 约束上硬失败**。这里刻意不用
-- `CROSS JOIN (SELECT DISTINCT ...)`：后者在上下文为空时会静默插 0 行，把数据丢失伪装成
-- 迁移成功。空库（全新安装）下暂存表为空，0 行插入是正确结果，不受影响。
--
-- **不在本轮的职责**：迁移上下文的具体输入、离线初始化入口与 owner 身份表的删除属于 P1b；
-- 业务仓储的 user_id 调用方改造属于 P2–P6。本文件只落物理结构。

-- =====================================================================
-- 阶段 1：全部业务表 CTAS 到无外键暂存表
-- =====================================================================
-- 顺序无关紧要：此时还没有任何表被删除，CTAS 只读不写。
CREATE TABLE _stage_accounts AS SELECT * FROM accounts;
CREATE TABLE _stage_ledger_versions AS SELECT * FROM ledger_versions;
CREATE TABLE _stage_ledger_version_accounts AS SELECT * FROM ledger_version_accounts;
CREATE TABLE _stage_import_files AS SELECT * FROM import_files;
CREATE TABLE _stage_transactions AS SELECT * FROM transactions;
CREATE TABLE _stage_lots AS SELECT * FROM lots;
CREATE TABLE _stage_realized AS SELECT * FROM realized;
CREATE TABLE _stage_account_import_reminder_settings AS SELECT * FROM account_import_reminder_settings;
CREATE TABLE _stage_paired_devices AS SELECT * FROM paired_devices;
CREATE TABLE _stage_device_refresh_credentials AS SELECT * FROM device_refresh_credentials;
CREATE TABLE _stage_device_access_tokens AS SELECT * FROM device_access_tokens;
CREATE TABLE _stage_mutation_idempotency_records AS SELECT * FROM mutation_idempotency_records;
CREATE TABLE _stage_audit_events AS SELECT * FROM audit_events;
CREATE TABLE _stage_agent_session_identities AS SELECT * FROM agent_session_identities;
CREATE TABLE _stage_agent_sessions AS SELECT * FROM agent_sessions;
CREATE TABLE _stage_agent_messages AS SELECT * FROM agent_messages;
CREATE TABLE _stage_agent_tool_calls AS SELECT * FROM agent_tool_calls;
CREATE TABLE _stage_gmail_connections AS SELECT * FROM gmail_connections;
CREATE TABLE _stage_gmail_account_mappings AS SELECT * FROM gmail_account_mappings;
CREATE TABLE _stage_gmail_messages AS SELECT * FROM gmail_messages;
CREATE TABLE _stage_gmail_message_transactions AS SELECT * FROM gmail_message_transactions;
CREATE TABLE _stage_market_source_settings AS SELECT * FROM market_source_settings;

-- =====================================================================
-- 阶段 2：拆除旧图（视图优先，表按依赖倒序：子先父后）
-- =====================================================================
DROP VIEW v_positions;
DROP VIEW v_realized_summary;
DROP VIEW v_cashflows;
DROP VIEW v_official_import_reminders;

-- 叶子表：没有任何表引用它们。
DROP TABLE gmail_message_transactions;
DROP TABLE agent_tool_calls;
DROP TABLE agent_messages;
DROP TABLE lots;
DROP TABLE realized;
-- transactions 被 lots/realized/gmail_message_transactions 引用，故在其之后删除。
DROP TABLE transactions;
-- ledger_version_accounts / account_import_reminder_settings / audit_events 同时引用
-- accounts 与 ledger_versions，必须早于这两个父表。
DROP TABLE ledger_version_accounts;
DROP TABLE account_import_reminder_settings;
DROP TABLE audit_events;
-- import_files 被 transactions 引用，故在其之后删除。
DROP TABLE import_files;
DROP TABLE gmail_account_mappings;
DROP TABLE gmail_messages;
DROP TABLE gmail_connections;
DROP TABLE device_refresh_credentials;
DROP TABLE device_access_tokens;
DROP TABLE mutation_idempotency_records;
-- agent_sessions 引用 paired_devices 与 accounts；agent_messages 引用 agent_session_identities。
DROP TABLE agent_sessions;
DROP TABLE agent_session_identities;
DROP TABLE paired_devices;
DROP TABLE accounts;
DROP TABLE ledger_versions;
DROP TABLE market_source_settings;

-- =====================================================================
-- 阶段 3：以最终名字从零创建 schema 16（约束必须在 CREATE 时给全，见 F1–F3）
-- =====================================================================

-- 用户是一张**普通可变表**：实测当前 DuckDB 版本允许在有子引用时更新父表的非键列，
-- 所以停用用户就是 `UPDATE users SET status = 'disabled'`，不需要拆成
-- 「不可变身份表 + 可变状态表」。用户 UUID 一经创建不再变更，F4/F6 对 users 无影响。
CREATE TABLE users (
    id            UUID PRIMARY KEY,
    display_name  VARCHAR NOT NULL CHECK (length(trim(display_name)) > 0),
    status        VARCHAR NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'disabled')),
    role          VARCHAR NOT NULL DEFAULT 'member'
                  CHECK (role IN ('admin', 'member')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    CHECK (updated_at >= created_at)
);

-- 身份键始终是已验证的 (provider, subject)：邮箱与昵称只作展示，永不用于关联两个身份。
-- `UNIQUE (user_id, id)` 不是冗余：F3 要求复合外键的父列组合有唯一约束，
-- paired_devices 的 `(user_id, identity_id)` 复合外键正是靠它才建得起来。
CREATE TABLE user_identities (
    id            UUID PRIMARY KEY,
    user_id       UUID NOT NULL REFERENCES users(id),
    provider      VARCHAR NOT NULL CHECK (provider IN ('google', 'linuxdo', 'github')),
    subject       VARCHAR NOT NULL CHECK (length(subject) BETWEEN 1 AND 255),
    email         VARCHAR,
    display_name  VARCHAR,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    UNIQUE (provider, subject),
    UNIQUE (user_id, id),
    CHECK (updated_at >= created_at)
);

-- 账户是某个用户的券商账户，不再是一台设备或整个部署的账户。
-- `legacy_account_key` 由全局唯一改为**用户内唯一**：A/B 可以各自拥有同键账户。
-- `UNIQUE (user_id, id)` 供 import_files 的复合外键使用（同样受 F3 约束）。
CREATE TABLE accounts (
    id                  UUID PRIMARY KEY,
    user_id             UUID NOT NULL REFERENCES users(id),
    display_name        VARCHAR NOT NULL CHECK (length(trim(display_name)) > 0),
    legacy_account_key  VARCHAR NOT NULL CHECK (length(trim(legacy_account_key)) > 0),
    broker              VARCHAR NOT NULL DEFAULT 'schwab',
    masked_suffix       VARCHAR NOT NULL CHECK (length(trim(masked_suffix)) > 0),
    base_currency       VARCHAR NOT NULL DEFAULT 'USD' CHECK (length(base_currency) = 3),
    status              VARCHAR NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'archived')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    UNIQUE (user_id, legacy_account_key),
    UNIQUE (user_id, id),
    CHECK (updated_at >= created_at)
);

-- 版本号仍来自**全局**序列，因此单个用户看到的版本号不连续（A 可能看到 1 → 3）。
-- 「用户当前账本版本」的唯一定义是 max(version) WHERE user_id = ?，空账本为 0；
-- 任何模块都不得改回全局 max(version)（P0 契约 §3.3）。
CREATE TABLE ledger_versions (
    version           BIGINT PRIMARY KEY DEFAULT nextval('ledger_version_seq'),
    user_id           UUID NOT NULL REFERENCES users(id),
    reason            VARCHAR NOT NULL
                      CHECK (reason IN ('file_import', 'gmail_import', 'fifo_rebuild')),
    source_reference  VARCHAR,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    CHECK (version > 0)
);

-- 版本与账户的关联表。它经 account 归属隔离，因此**不加** user_id：
-- 跨用户保护由应用层查询范围保证（P0 契约 §3.2）。
CREATE TABLE ledger_version_accounts (
    ledger_version  BIGINT NOT NULL REFERENCES ledger_versions(version),
    account_id      UUID NOT NULL REFERENCES accounts(id),
    PRIMARY KEY (ledger_version, account_id)
);

-- 设备一旦归属用户就不再改 owner；唯一键从全局指纹改为 (user_id, 指纹)，
-- 使两台设备在不同用户下可以持有同一把公钥而不互相顶掉。
-- `(user_id, identity_id)` 复合外键保证「设备本次登录的身份」与设备属于同一用户
-- （实测该约束在 INSERT 与 UPDATE 上都生效），GET /v1/me 的身份来源由此无需猜测。
CREATE TABLE paired_devices (
    id                      UUID PRIMARY KEY,
    user_id                 UUID NOT NULL REFERENCES users(id),
    identity_id             UUID NOT NULL,
    display_name            VARCHAR NOT NULL CHECK (length(trim(display_name)) > 0),
    public_key_pem          VARCHAR NOT NULL CHECK (length(trim(public_key_pem)) > 0),
    public_key_fingerprint  VARCHAR NOT NULL
                            CHECK (length(public_key_fingerprint) = 64),
    key_algorithm           VARCHAR NOT NULL CHECK (length(trim(key_algorithm)) > 0),
    status                  VARCHAR NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'revoked')),
    paired_at               TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    last_seen_at            TIMESTAMPTZ,
    revoked_at              TIMESTAMPTZ,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    UNIQUE (user_id, public_key_fingerprint),
    FOREIGN KEY (user_id, identity_id) REFERENCES user_identities(user_id, id),
    CHECK (last_seen_at IS NULL OR last_seen_at >= paired_at),
    CHECK (updated_at >= paired_at),
    CHECK (
        (status = 'active' AND revoked_at IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL)
    )
);

-- 导入作业必须**直接**关联 user：上传阶段可能还没有目标账户，仅靠 target_account_id
-- 无法确定归属。target_account_id 可空，复合外键在 NULL 上按 MATCH SIMPLE 放行，
-- 一旦填值就要求该账户与作业同属一个用户（实测 INSERT 与 UPDATE 都生效）。
CREATE TABLE import_files (
    id                       UUID PRIMARY KEY,
    user_id                  UUID NOT NULL REFERENCES users(id),
    file_hash                VARCHAR NOT NULL CHECK (length(file_hash) = 64),
    original_file_name       VARCHAR NOT NULL CHECK (length(trim(original_file_name)) > 0),
    file_type                VARCHAR NOT NULL CHECK (file_type IN ('csv', 'xlsx')),
    file_size_bytes          BIGINT NOT NULL CHECK (file_size_bytes >= 0),
    parser_version           VARCHAR NOT NULL,
    status                   VARCHAR NOT NULL CHECK (
                                 status IN (
                                     'uploaded', 'parsing', 'preview_ready', 'committing',
                                     'committed', 'rejected', 'failed', 'cancelled'
                                 )
                             ),
    target_account_id        UUID,
    preview_token_hash       VARCHAR,
    preview_ledger_version   BIGINT REFERENCES ledger_versions(version),
    total_rows               INTEGER CHECK (total_rows IS NULL OR total_rows >= 0),
    inserted_rows            INTEGER CHECK (inserted_rows IS NULL OR inserted_rows >= 0),
    skipped_rows             INTEGER CHECK (skipped_rows IS NULL OR skipped_rows >= 0),
    error_code               VARCHAR,
    error_summary            VARCHAR,
    committed_ledger_version BIGINT REFERENCES ledger_versions(version),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    preview_fingerprint      VARCHAR,
    preview_expires_at       TIMESTAMPTZ,
    FOREIGN KEY (user_id, target_account_id) REFERENCES accounts(user_id, id),
    CHECK (updated_at >= created_at)
);

-- 规范交易事实表：经 account 归属隔离，**不加** user_id（P0 契约 §3.2）。
CREATE TABLE transactions (
    account_id          UUID NOT NULL REFERENCES accounts(id),
    record_date         DATE NOT NULL,
    transaction_date    DATE NOT NULL,
    action              VARCHAR NOT NULL CHECK (
                            action IN (
                                'Buy', 'Sell', 'Buy to Open', 'Sell to Open',
                                'Buy to Close', 'Sell to Close', 'Expired', 'Assigned',
                                'MoneyLink Transfer', 'Credit Interest',
                                'Qualified Dividend', 'Non-Qualified Div',
                                'Pr Yr Non-Qual Div', 'NRA Tax Adj', 'Pr Yr NRA Tax',
                                'Foreign Tax Paid'
                            )
                        ),
    raw_symbol          VARCHAR,
    description         VARCHAR,
    asset_type          VARCHAR NOT NULL CHECK (asset_type IN ('stock', 'option', 'cash')),
    underlying          VARCHAR,
    expiry              DATE,
    strike              DECIMAL(18, 4),
    option_type         VARCHAR CHECK (option_type IN ('C', 'P')),
    quantity            DECIMAL(18, 6),
    price               DECIMAL(18, 6),
    fees                DECIMAL(18, 6),
    amount              DECIMAL(18, 4),
    source_type         VARCHAR NOT NULL CHECK (source_type IN ('csv', 'excel', 'gmail')),
    source_reference    VARCHAR NOT NULL,
    import_file_id      UUID REFERENCES import_files(id),
    source_hash         VARCHAR NOT NULL CHECK (length(source_hash) = 64),
    sequence            INTEGER NOT NULL CHECK (sequence > 0),
    occurrence          INTEGER NOT NULL CHECK (occurrence > 0),
    business_hash       VARCHAR NOT NULL CHECK (length(business_hash) = 64),
    transaction_hash    VARCHAR NOT NULL CHECK (length(transaction_hash) = 64),
    parser_version      VARCHAR NOT NULL,
    ledger_version      BIGINT NOT NULL REFERENCES ledger_versions(version),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (account_id, transaction_hash),
    CHECK (
        asset_type = 'cash'
        OR (raw_symbol IS NOT NULL AND underlying IS NOT NULL AND quantity > 0)
    ),
    CHECK (
        asset_type <> 'option'
        OR (expiry IS NOT NULL AND strike IS NOT NULL AND option_type IS NOT NULL)
    ),
    CHECK (
        asset_type = 'option'
        OR (expiry IS NULL AND strike IS NULL AND option_type IS NULL)
    )
);

-- FIFO 未平仓批次：经 account 归属隔离，**不加** user_id。
CREATE TABLE lots (
    lot_id                 BIGINT PRIMARY KEY DEFAULT nextval('lot_id_seq'),
    account_id             UUID NOT NULL REFERENCES accounts(id),
    symbol_key             VARCHAR NOT NULL,
    asset_type             VARCHAR NOT NULL CHECK (asset_type IN ('stock', 'option')),
    underlying             VARCHAR NOT NULL,
    expiry                 DATE,
    strike                 DECIMAL(18, 4),
    option_type            VARCHAR CHECK (option_type IN ('C', 'P')),
    direction              VARCHAR NOT NULL CHECK (direction IN ('long', 'short')),
    open_date              DATE NOT NULL,
    open_price             DECIMAL(18, 6) NOT NULL,
    open_quantity          DECIMAL(18, 6) NOT NULL CHECK (open_quantity > 0),
    remaining_quantity     DECIMAL(18, 6) NOT NULL CHECK (remaining_quantity > 0),
    open_fees              DECIMAL(18, 6) NOT NULL DEFAULT 0,
    fees_allocated         DECIMAL(18, 6) NOT NULL DEFAULT 0,
    open_transaction_hash  VARCHAR NOT NULL,
    ledger_version         BIGINT NOT NULL REFERENCES ledger_versions(version),
    FOREIGN KEY (account_id, open_transaction_hash)
        REFERENCES transactions(account_id, transaction_hash),
    CHECK (remaining_quantity <= open_quantity),
    CHECK (fees_allocated >= 0),
    CHECK (
        asset_type <> 'option'
        OR (expiry IS NOT NULL AND strike IS NOT NULL AND option_type IS NOT NULL)
    ),
    CHECK (
        asset_type = 'option'
        OR (expiry IS NULL AND strike IS NULL AND option_type IS NULL)
    )
);

-- 已实现记录：经 account 归属隔离，**不加** user_id。
CREATE TABLE realized (
    realized_id            BIGINT PRIMARY KEY DEFAULT nextval('realized_id_seq'),
    account_id             UUID NOT NULL REFERENCES accounts(id),
    symbol_key             VARCHAR NOT NULL,
    asset_type             VARCHAR NOT NULL CHECK (asset_type IN ('stock', 'option')),
    underlying             VARCHAR NOT NULL,
    direction              VARCHAR NOT NULL CHECK (direction IN ('long', 'short')),
    open_date              DATE NOT NULL,
    close_date             DATE NOT NULL,
    quantity               DECIMAL(18, 6) NOT NULL CHECK (quantity > 0),
    open_price             DECIMAL(18, 6) NOT NULL,
    close_price            DECIMAL(18, 6) NOT NULL,
    open_fees              DECIMAL(18, 6) NOT NULL DEFAULT 0,
    close_fees             DECIMAL(18, 6) NOT NULL DEFAULT 0,
    pnl                    DECIMAL(18, 4) NOT NULL,
    close_action           VARCHAR NOT NULL,
    open_transaction_hash  VARCHAR NOT NULL,
    close_transaction_hash VARCHAR NOT NULL,
    ledger_version         BIGINT NOT NULL REFERENCES ledger_versions(version),
    FOREIGN KEY (account_id, open_transaction_hash)
        REFERENCES transactions(account_id, transaction_hash),
    FOREIGN KEY (account_id, close_transaction_hash)
        REFERENCES transactions(account_id, transaction_hash),
    CHECK (close_date >= open_date)
);

-- 对账单提醒设置：经 account 归属隔离，**不加** user_id。
CREATE TABLE account_import_reminder_settings (
    account_id       UUID PRIMARY KEY REFERENCES accounts(id),
    enabled          BOOLEAN NOT NULL DEFAULT true,
    interval_months  UTINYINT NOT NULL DEFAULT 1
                     CHECK (interval_months BETWEEN 1 AND 24),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

-- Gmail 连接必须关联 user；同一 Google subject 在不同用户下独立存储，
-- 不得通过全局 upsert 接管他人连接。唯一键因此按用户划分。
CREATE TABLE gmail_connections (
    id                       UUID PRIMARY KEY,
    user_id                  UUID NOT NULL REFERENCES users(id),
    google_subject           VARCHAR NOT NULL CHECK (length(trim(google_subject)) > 0),
    google_email             VARCHAR NOT NULL CHECK (length(trim(google_email)) > 0),
    encrypted_refresh_token  VARCHAR,
    granted_scopes           VARCHAR NOT NULL,
    history_id               VARCHAR,
    status                   VARCHAR NOT NULL CHECK (
                                 status IN (
                                     'pending_initial_scan', 'active', 'needs_resync',
                                     'disconnected', 'error'
                                 )
                             ),
    last_error_code          VARCHAR,
    last_sync_at             TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    UNIQUE (user_id, google_subject),
    -- 与 user_identities / accounts 同理，这里是「子资源必须与父资源同属一个用户」
    -- 所需复合外键的父侧前提；P4/P5 不得再改 schema，因此父侧唯一键必须在本次给全。
    UNIQUE (user_id, id),
    CHECK (updated_at >= created_at),
    CHECK (status = 'disconnected' OR encrypted_refresh_token IS NOT NULL)
);

-- 映射与消息经 connection 隔离，**不加** user_id；account/transaction 的同属校验在应用层。
CREATE TABLE gmail_account_mappings (
    id               UUID PRIMARY KEY,
    connection_id    UUID NOT NULL REFERENCES gmail_connections(id),
    account_suffix   VARCHAR NOT NULL CHECK (length(trim(account_suffix)) > 0),
    account_id       UUID NOT NULL REFERENCES accounts(id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    UNIQUE (connection_id, account_suffix),
    CHECK (updated_at >= created_at)
);

CREATE TABLE gmail_messages (
    connection_id     UUID NOT NULL REFERENCES gmail_connections(id),
    message_id        VARCHAR NOT NULL,
    thread_id         VARCHAR,
    received_at       TIMESTAMPTZ,
    content_hash      VARCHAR,
    account_id        UUID REFERENCES accounts(id),
    parse_status      VARCHAR NOT NULL CHECK (
                          parse_status IN ('discovered', 'parsed', 'imported', 'rejected', 'conflict')
                      ),
    error_code        VARCHAR,
    transaction_count INTEGER NOT NULL DEFAULT 0 CHECK (transaction_count >= 0),
    history_id        VARCHAR,
    synced_at         TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (connection_id, message_id),
    CHECK (
        parse_status = 'discovered'
        OR (content_hash IS NOT NULL AND length(content_hash) = 64)
    )
);

CREATE TABLE gmail_message_transactions (
    connection_id    UUID NOT NULL,
    message_id       VARCHAR NOT NULL,
    account_id       UUID NOT NULL,
    transaction_hash VARCHAR NOT NULL,
    ordinal          INTEGER NOT NULL CHECK (ordinal > 0),
    PRIMARY KEY (connection_id, message_id, ordinal),
    UNIQUE (connection_id, message_id, account_id, transaction_hash),
    FOREIGN KEY (connection_id, message_id)
        REFERENCES gmail_messages(connection_id, message_id),
    FOREIGN KEY (account_id, transaction_hash)
        REFERENCES transactions(account_id, transaction_hash)
);

-- 设备凭据与幂等记录经**不可变的设备归属**隔离，不加 user_id；
-- 保留设备级幂等唯一键，返回缓存结果前仍须检查当前授权。
CREATE TABLE device_refresh_credentials (
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

CREATE TABLE device_access_tokens (
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

CREATE TABLE mutation_idempotency_records (
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

-- 业务审计区分「操作者」与「资源所属」两个用户维度：设备代用户操作时二者相同，
-- 后台任务必须保留资源所属用户，部署级 system 事件可以都没有用户（故两列可空）。
-- 这两列是可加性扩展：既有写入方沿用显式列名，未提供时保持 NULL，由 P2–P6 补齐。
CREATE TABLE audit_events (
    id              UUID PRIMARY KEY,
    occurred_at     TIMESTAMPTZ NOT NULL,
    event_type      VARCHAR NOT NULL CHECK (length(trim(event_type)) > 0),
    outcome         VARCHAR NOT NULL CHECK (outcome IN ('success', 'denied', 'failure')),
    actor_type      VARCHAR NOT NULL CHECK (actor_type IN ('system', 'device')),
    actor_id        UUID REFERENCES paired_devices(id),
    actor_user_id   UUID REFERENCES users(id),
    subject_user_id UUID REFERENCES users(id),
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

-- 会话身份是**不可变**父表，其上记录 user：会话仓储通过该身份校验归属，
-- 创建设备与账户必须同属该用户。agent_sessions 保持可变，故不直接挂 user_id。
CREATE TABLE agent_session_identities (
    id       UUID PRIMARY KEY,
    user_id  UUID NOT NULL REFERENCES users(id),
    -- 同上：父侧唯一键必须在 CREATE 时给全，供 P4 用复合外键约束会话子资源同属一个用户。
    UNIQUE (user_id, id)
);

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

-- 行情来源设置从「全部署单例」改为「按用户一行」：revision 与来源偏好各自独立，
-- 启动不再把某个用户的设置装成全局运行时偏好。公开报价缓存仍按公开报价身份共享。
CREATE TABLE market_source_settings (
    user_id      UUID PRIMARY KEY REFERENCES users(id),
    revision     BIGINT NOT NULL CHECK (revision BETWEEN 1 AND 9007199254740991),
    routes_json  JSON NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL
);

-- 视图定义与 schema 15 逐字一致：这些表本身没有 user_id（经 account 隔离），
-- 视图也不额外暴露归属，用户范围由应用层查询条件提供（P3 负责）。
CREATE OR REPLACE VIEW v_positions AS
SELECT
    account_id,
    symbol_key,
    asset_type,
    underlying,
    expiry,
    strike,
    option_type,
    direction,
    sum(remaining_quantity) AS quantity,
    sum(remaining_quantity * open_price) AS cost,
    min(open_date) AS first_open_date,
    max(ledger_version) AS ledger_version
FROM lots
GROUP BY
    account_id, symbol_key, asset_type, underlying, expiry, strike, option_type, direction;

CREATE OR REPLACE VIEW v_realized_summary AS
SELECT
    account_id,
    underlying,
    asset_type,
    strftime(close_date, '%Y-%m') AS month,
    count(*) AS closed_lots,
    count(DISTINCT close_transaction_hash) AS close_transactions,
    sum(pnl) AS pnl,
    sum(open_fees + close_fees) AS fees,
    max(ledger_version) AS ledger_version
FROM realized
GROUP BY account_id, underlying, asset_type, month;

CREATE OR REPLACE VIEW v_cashflows AS
SELECT
    account_id,
    transaction_date,
    action,
    description,
    amount,
    source_type,
    source_reference,
    transaction_hash,
    ledger_version
FROM transactions
WHERE asset_type = 'cash';

CREATE OR REPLACE VIEW v_official_import_reminders AS
SELECT
    accounts.id AS account_id,
    coalesce(settings.enabled, true) AS enabled,
    coalesce(settings.interval_months, 1) AS interval_months,
    max(CASE
        WHEN imports.status = 'committed' THEN CAST(imports.updated_at AS DATE)
        ELSE NULL
    END) AS last_successful_import_date
FROM accounts
LEFT JOIN account_import_reminder_settings AS settings
    ON settings.account_id = accounts.id
LEFT JOIN import_files AS imports
    ON imports.target_account_id = accounts.id
GROUP BY accounts.id, settings.enabled, settings.interval_months;

-- =====================================================================
-- 阶段 4：回填归属并装载
-- =====================================================================
-- 先建用户与身份：其余表的 user_id 外键都指向这里。身份 id 由 uuid() 现场生成，
-- 迁移不需要调用方提供身份 UUID（设备归属按 (provider, subject) 反查）。
-- 时间戳统一取 current_timestamp：schema 15 没有记录用户创建时间，不存在可回填的原值。
INSERT INTO users (id, display_name, status, role, created_at, updated_at)
SELECT DISTINCT
    owner_user_id, owner_display_name, owner_status, owner_role,
    current_timestamp, current_timestamp
FROM _migration_context;

INSERT INTO user_identities
    (id, user_id, provider, subject, email, display_name, created_at, updated_at)
SELECT
    uuid(), owner_user_id, provider, subject, email, display_name,
    current_timestamp, current_timestamp
FROM _migration_context;

-- 旧库的全部资源归给上下文中明确确认的原用户；空库下暂存表为空，这里插 0 行。
INSERT INTO accounts
    (id, user_id, display_name, legacy_account_key, broker, masked_suffix,
     base_currency, status, created_at, updated_at)
SELECT
    id,
    (SELECT DISTINCT owner_user_id FROM _migration_context),
    display_name, legacy_account_key, broker, masked_suffix, base_currency, status,
    created_at, updated_at
FROM _stage_accounts;

-- 版本号显式插入，不走序列默认值。重建不会删除序列对象，但**显式插入**意味着必须重新
-- 建立主键前提「nextval('ledger_version_seq') > max(version)」：正常路径下序列本就领先
-- （这里只是多跳掉若干未使用的号段，而 §3.3 已确认单用户版本号本就不连续），
-- 异常路径下（历史上有人显式写过版本号）序列会落后，不推进就会在迁移后的第一次写入
-- 撞上主键冲突。序列是非事务对象，回滚只会让它多跳号，不会破坏旧库。
INSERT INTO ledger_versions (version, user_id, reason, source_reference, created_at)
SELECT
    version,
    (SELECT DISTINCT owner_user_id FROM _migration_context),
    reason, source_reference, created_at
FROM _stage_ledger_versions;

SELECT nextval('ledger_version_seq') FROM range(
    (SELECT coalesce(max(version), 0) FROM ledger_versions)
);

INSERT INTO ledger_version_accounts (ledger_version, account_id)
SELECT ledger_version, account_id FROM _stage_ledger_version_accounts;

-- 旧设备没有记录「本次登录用的是哪个身份」，因此上下文必须显式指定一条身份作为
-- 既有设备的身份来源（MigrationContext 的 is_device_identity）。未指定时下面的
-- 标量子查询返回 NULL，identity_id 的 NOT NULL 会让迁移硬失败，而不是随便挑一条。
INSERT INTO paired_devices
    (id, user_id, identity_id, display_name, public_key_pem, public_key_fingerprint,
     key_algorithm, status, paired_at, last_seen_at, revoked_at, updated_at)
SELECT
    id,
    (SELECT DISTINCT owner_user_id FROM _migration_context),
    (
        SELECT identities.id
        FROM user_identities AS identities
        WHERE identities.user_id = (SELECT DISTINCT owner_user_id FROM _migration_context)
          AND identities.provider = (SELECT provider FROM _migration_context WHERE is_device_identity)
          AND identities.subject = (SELECT subject FROM _migration_context WHERE is_device_identity)
    ),
    display_name, public_key_pem, public_key_fingerprint, key_algorithm, status,
    paired_at, last_seen_at, revoked_at, updated_at
FROM _stage_paired_devices;

INSERT INTO import_files
    (id, user_id, file_hash, original_file_name, file_type, file_size_bytes,
     parser_version, status, target_account_id, preview_token_hash,
     preview_ledger_version, total_rows, inserted_rows, skipped_rows, error_code,
     error_summary, committed_ledger_version, created_at, updated_at,
     preview_fingerprint, preview_expires_at)
SELECT
    id,
    (SELECT DISTINCT owner_user_id FROM _migration_context),
    file_hash, original_file_name, file_type, file_size_bytes, parser_version, status,
    target_account_id, preview_token_hash, preview_ledger_version, total_rows,
    inserted_rows, skipped_rows, error_code, error_summary, committed_ledger_version,
    created_at, updated_at, preview_fingerprint, preview_expires_at
FROM _stage_import_files;

INSERT INTO transactions
    (account_id, record_date, transaction_date, action, raw_symbol, description,
     asset_type, underlying, expiry, strike, option_type, quantity, price, fees,
     amount, source_type, source_reference, import_file_id, source_hash, sequence,
     occurrence, business_hash, transaction_hash, parser_version, ledger_version,
     created_at)
SELECT
    account_id, record_date, transaction_date, action, raw_symbol, description,
    asset_type, underlying, expiry, strike, option_type, quantity, price, fees,
    amount, source_type, source_reference, import_file_id, source_hash, sequence,
    occurrence, business_hash, transaction_hash, parser_version, ledger_version,
    created_at
FROM _stage_transactions;

INSERT INTO lots
    (lot_id, account_id, symbol_key, asset_type, underlying, expiry, strike, option_type,
     direction, open_date, open_price, open_quantity, remaining_quantity, open_fees,
     fees_allocated, open_transaction_hash, ledger_version)
SELECT
    lot_id, account_id, symbol_key, asset_type, underlying, expiry, strike, option_type,
    direction, open_date, open_price, open_quantity, remaining_quantity, open_fees,
    fees_allocated, open_transaction_hash, ledger_version
FROM _stage_lots;

-- 与 ledger_version_seq 同理：lot_id / realized_id 也是显式装载的，必须把序列推过
-- 已装载的最大 id，否则迁移后的第一次 FIFO 重建会撞主键。
SELECT nextval('lot_id_seq') FROM range(
    (SELECT coalesce(max(lot_id), 0) FROM lots)
);

INSERT INTO realized
    (realized_id, account_id, symbol_key, asset_type, underlying, direction, open_date,
     close_date, quantity, open_price, close_price, open_fees, close_fees, pnl,
     close_action, open_transaction_hash, close_transaction_hash, ledger_version)
SELECT
    realized_id, account_id, symbol_key, asset_type, underlying, direction, open_date,
    close_date, quantity, open_price, close_price, open_fees, close_fees, pnl,
    close_action, open_transaction_hash, close_transaction_hash, ledger_version
FROM _stage_realized;

SELECT nextval('realized_id_seq') FROM range(
    (SELECT coalesce(max(realized_id), 0) FROM realized)
);

INSERT INTO account_import_reminder_settings
    (account_id, enabled, interval_months, updated_at)
SELECT account_id, enabled, interval_months, updated_at
FROM _stage_account_import_reminder_settings;

INSERT INTO gmail_connections
    (id, user_id, google_subject, google_email, encrypted_refresh_token, granted_scopes,
     history_id, status, last_error_code, last_sync_at, created_at, updated_at)
SELECT
    id,
    (SELECT DISTINCT owner_user_id FROM _migration_context),
    google_subject, google_email, encrypted_refresh_token, granted_scopes, history_id,
    status, last_error_code, last_sync_at, created_at, updated_at
FROM _stage_gmail_connections;

INSERT INTO gmail_account_mappings
    (id, connection_id, account_suffix, account_id, created_at, updated_at)
SELECT id, connection_id, account_suffix, account_id, created_at, updated_at
FROM _stage_gmail_account_mappings;

INSERT INTO gmail_messages
    (connection_id, message_id, thread_id, received_at, content_hash, account_id,
     parse_status, error_code, transaction_count, history_id, synced_at)
SELECT
    connection_id, message_id, thread_id, received_at, content_hash, account_id,
    parse_status, error_code, transaction_count, history_id, synced_at
FROM _stage_gmail_messages;

INSERT INTO gmail_message_transactions
    (connection_id, message_id, account_id, transaction_hash, ordinal)
SELECT connection_id, message_id, account_id, transaction_hash, ordinal
FROM _stage_gmail_message_transactions;

-- =====================================================================
-- 旧设备凭据一律撤销（计划 §1.8「发布包含数据迁移和重新登录」、§9.4「撤销旧凭据」）。
--
-- schema 16 引入了授权模型（用户 → 设备），而 schema 15 的凭据是在**没有用户概念**时
-- 签发的。让它们继续有效，等于让一个前授权模型时期建立的会话悄悄穿过一次授权模型的
-- 引入——即使当前库里只有原 owner，这也不是该默认的方向。
--
-- 撤销只动状态，**不删行**：设备与凭据的历史要保留，重放检测与审计都需要它们。
-- 用户随后重新 OAuth 登录会签发出新凭据（§9.6 的运行手册本来就有这一步）。
--
-- status='rotated' 的行同样置为 'revoked'；'revoked' 的行保留原有 revoked_at，
-- 不把历史撤销时间改写成今天。
INSERT INTO device_refresh_credentials
    (id, device_id, family_id, generation, token_hash, status, expires_at, last_used_at,
     rotated_at, revoked_at, created_at)
SELECT
    id, device_id, family_id, generation, token_hash, 'revoked', expires_at, last_used_at,
    rotated_at, coalesce(revoked_at, current_timestamp), created_at
FROM _stage_device_refresh_credentials;

INSERT INTO device_access_tokens
    (id, device_id, token_hash, status, expires_at, last_seen_at, revoked_at, issued_at)
SELECT
    id, device_id, token_hash, 'revoked', expires_at, last_seen_at,
    coalesce(revoked_at, current_timestamp), issued_at
FROM _stage_device_access_tokens;

INSERT INTO mutation_idempotency_records
    (id, device_id, operation, key_hash, request_hash, status, result_resource_id,
     response_status_code, created_at, expires_at, completed_at)
SELECT
    id, device_id, operation, key_hash, request_hash, status, result_resource_id,
    response_status_code, created_at, expires_at, completed_at
FROM _stage_mutation_idempotency_records;

-- 历史审计事件全部属于原用户的资源，因此 subject_user_id 回填原用户；
-- 操作者维度只在「设备代用户操作」时回填，system 事件按定义没有用户。
INSERT INTO audit_events
    (id, occurred_at, event_type, outcome, actor_type, actor_id, actor_user_id,
     subject_user_id, request_id, target_type, target_id, account_id, ledger_version,
     error_code, metadata_json)
SELECT
    id, occurred_at, event_type, outcome, actor_type, actor_id,
    CASE WHEN actor_type = 'device'
         THEN (SELECT DISTINCT owner_user_id FROM _migration_context)
         ELSE NULL
    END,
    (SELECT DISTINCT owner_user_id FROM _migration_context),
    request_id, target_type, target_id, account_id, ledger_version,
    error_code, metadata_json
FROM _stage_audit_events;

INSERT INTO agent_session_identities (id, user_id)
SELECT id, (SELECT DISTINCT owner_user_id FROM _migration_context)
FROM _stage_agent_session_identities;

INSERT INTO agent_sessions
    (id, device_id, provider, original_provider, status, account_scope_kind, account_id,
     ledger_version, as_of, revision, last_event_sequence, summary, created_at,
     updated_at, closed_at)
SELECT
    id, device_id, provider, original_provider, status, account_scope_kind, account_id,
    ledger_version, as_of, revision, last_event_sequence, summary, created_at,
    updated_at, closed_at
FROM _stage_agent_sessions;

INSERT INTO agent_messages
    (id, session_id, ordinal, role, provider, status, content, error_code, created_at,
     completed_at)
SELECT
    id, session_id, ordinal, role, provider, status, content, error_code, created_at,
    completed_at
FROM _stage_agent_messages;

INSERT INTO agent_tool_calls
    (id, session_id, message_id, ordinal, tool_name, authorization_decision, status,
     arguments_summary, result_summary, ledger_version, duration_ms, error_code,
     started_at, completed_at)
SELECT
    id, session_id, message_id, ordinal, tool_name, authorization_decision, status,
    CAST(arguments_summary AS JSON), CAST(result_summary AS JSON), ledger_version,
    duration_ms, error_code, started_at, completed_at
FROM _stage_agent_tool_calls;

-- 单例设置行改挂到原用户主键上；单例行在而上下文为空时会因主键 NOT NULL 硬失败。
INSERT INTO market_source_settings (user_id, revision, routes_json, updated_at)
SELECT
    (SELECT DISTINCT owner_user_id FROM _migration_context),
    revision, routes_json, updated_at
FROM _stage_market_source_settings;

-- =====================================================================
-- 阶段 5：清理暂存表
-- =====================================================================
DROP TABLE _stage_accounts;
DROP TABLE _stage_ledger_versions;
DROP TABLE _stage_ledger_version_accounts;
DROP TABLE _stage_import_files;
DROP TABLE _stage_transactions;
DROP TABLE _stage_lots;
DROP TABLE _stage_realized;
DROP TABLE _stage_account_import_reminder_settings;
DROP TABLE _stage_paired_devices;
DROP TABLE _stage_device_refresh_credentials;
DROP TABLE _stage_device_access_tokens;
DROP TABLE _stage_mutation_idempotency_records;
DROP TABLE _stage_audit_events;
DROP TABLE _stage_agent_session_identities;
DROP TABLE _stage_agent_sessions;
DROP TABLE _stage_agent_messages;
DROP TABLE _stage_agent_tool_calls;
DROP TABLE _stage_gmail_connections;
DROP TABLE _stage_gmail_account_mappings;
DROP TABLE _stage_gmail_messages;
DROP TABLE _stage_gmail_message_transactions;
DROP TABLE _stage_market_source_settings;

-- 运行时不再使用的旧 owner 身份表（v13 建、v15 重建）。
--
-- **在这里删而不是只让离线入口删**：离线入口只覆盖升级路径，直接 `open_lyra_database`
-- 建出来的新库会留着这张空表，于是「全新库」与「升级库」的表集合不一致——那种差异
-- 会在排查时被当成真问题。放在迁移里，两条路径的结果完全相同。
--
-- 删除时机在归属迁移之后：`_migration_context` 里确认过的身份已经写进 `user_identities`，
-- 旧表不再承担任何职责。它没有外键依赖（v15 是一次整体重建），因此可以直接 DROP。
-- 离线入口若要向操作者报告「哪些历史身份未被确认」，必须在**迁移前**读取该表。
DROP TABLE IF EXISTS server_owner_identity;

INSERT INTO schema_migrations (version, description)
VALUES (16, 'per-user ledger ownership and login identities')
ON CONFLICT (version) DO NOTHING;

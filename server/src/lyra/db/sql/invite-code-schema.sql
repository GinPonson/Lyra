-- Lyra DuckDB migration 17: 邀请码。
--
-- **只存哈希**，与设备凭据同一策略：明文字段一旦进库，任何一次备份、日志采样或
-- 只读排查都会把它变成长期有效的钥匙。明文只在创建响应里返回一次。
--
-- 语义（产品决定，2026-10-17）：**单次有效、不过期、可手动撤销**。
-- 因此没有 expires_at 列——"不过期"是刻意的，不是遗漏；撤销是唯一的失效手段。
--
-- ``created_by`` 允许为空：**离线初始化入口**在还没有任何管理员时就要能签发第一枚
-- 邀请码，否则"新用户必须有码、管理员只能由用户创建"会形成死锁。为空即表示该码来自
-- 服务器终端而非某个管理员账号，正好可审计。

CREATE TABLE IF NOT EXISTS invite_codes (
    id           UUID PRIMARY KEY,
    code_hash    VARCHAR NOT NULL UNIQUE CHECK (length(code_hash) = 64),
    -- 备注：这枚码准备发给谁。只作运维记录，不参与任何判定。
    label        VARCHAR,
    status       VARCHAR NOT NULL CHECK (status IN ('active', 'used', 'revoked')),
    created_by   UUID REFERENCES users(id),
    created_at   TIMESTAMPTZ NOT NULL,
    used_by      UUID REFERENCES users(id),
    used_at      TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    -- 三种状态各自的字段组合，与设备凭据表同一写法：状态与时间戳不允许自相矛盾。
    CHECK (
        (status = 'active' AND used_by IS NULL AND used_at IS NULL AND revoked_at IS NULL)
        OR (status = 'used' AND used_by IS NOT NULL AND used_at IS NOT NULL AND revoked_at IS NULL)
        OR (status = 'revoked' AND used_at IS NULL AND revoked_at IS NOT NULL)
    )
);

-- 管理列表按创建时间倒序；状态过滤是唯一的查询条件。
CREATE INDEX IF NOT EXISTS invite_codes_status_idx ON invite_codes (status);

INSERT INTO schema_migrations (version, description)
VALUES (17, 'single-use invite codes gating new registrations')
ON CONFLICT (version) DO NOTHING;

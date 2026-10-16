# Lyra 多用户独立账本 P0 冻结契约

日期：2026-10-16。对应计划：[lyra-multi-user-ledger-20261015.md](lyra-multi-user-ledger-20261015.md) 第 5 节 P0。

状态：**P0 已完成契约冻结与 DuckDB 定向验证**；本文件是 P1a 建表、P2 认证、P3–P7 隔离实施的共同依据。本文件只记录在合成库与内存库上实测得到的事实，不含任何真实凭据或真实账本数据。

---

## 1 基线确认

| 项 | 实测值 | 依据 |
| --- | --- | --- |
| 工作区状态 | 干净，无未提交改动 | `git status --porcelain` 为空；最后一次提交 `120e290` 即本计划 |
| 当前分支 | `feat/genkit-oauth` | `git branch --show-current` |
| 当前 schema | 15（15 条迁移） | [migrations.py:24](/Users/Gin/Workspace/Lyra/server/src/lyra/db/migrations.py#L24) |
| 当前协议 | 2，最低支持 2 | [protocol.py:12](/Users/Gin/Workspace/Lyra/server/src/lyra/protocol.py#L12) |
| 锁定 DuckDB | 1.5.6 | `uv.lock`；运行时实测 `duckdb 1.5.6` |
| 冻结夹具 | schema 15、无业务数据 | [typescript-ledger-schema15.duckdb](/Users/Gin/Workspace/Lyra/server/tests/fixtures/typescript-ledger-schema15.duckdb) |
| 服务端测试文件 | 119 个 | `server/tests/test_*.py` |
| 服务端基线测试 | **2453 passed, 4 skipped, 0 failed**，退出码 0，耗时 140.11s | 见 §5 |
| Flutter 基线 | `flutter analyze` 无问题；`flutter test` **262 passed, 5 skipped**，退出码 0 | 见 §5 |
| 路由契约 | 48 项（47 REST + 1 WS） | [route-manifest.json](/Users/Gin/Workspace/Lyra/server/tests/fixtures/route-manifest.json) |

目标版本不变：schema **16**、协议 **3**、最低支持协议 **3**。

---

## 2 DuckDB 定向验证结论（决定性）

本节全部结论来自在当前锁定版本 DuckDB 1.5.6 上对内存库的实测，复现脚本见 [assets/p0/](assets/p0)。这些结论直接决定迁移写法与 users 的物理结构，**不是推断**。

### 2.1 硬约束（必须绕开）

| 编号 | 约束 | 实测报错 |
| --- | --- | --- |
| F1 | 非空表上 `ADD COLUMN ... NOT NULL` 不支持 | `ParserException: Adding columns with constraints not yet supported` |
| F2 | `ALTER TABLE ... ADD CONSTRAINT` / `DROP CONSTRAINT` 完全未实现 | `NotImplementedException: No support for that ALTER TABLE option yet!` |
| F3 | 复合外键要求父表在 CREATE 时就有对应的 PK/UNIQUE | `BinderException: Failed to create foreign key: referenced table ... does not have a primary key or unique constraint on the columns user_id,id` |
| F4 | **被外键引用的父表不能 RENAME** | `DependencyException: Cannot alter entry "accounts_v16" because there are entries that depend on it` |
| F5 | 有子引用的父表不能 DROP，且 `CASCADE` 无级联语义 | `CatalogException: Could not drop the table because this table is main key table of the table "transactions"` |
| F6 | 被引用表上不能 `ALTER COLUMN ... TYPE` | `DependencyException: Cannot alter entry "users" because there are entries that depend on it` |
| F7 | 视图必须先于其基表删除 | `CatalogException: Could not drop the table because ...` |
| F8 | **被其他表外键引用的行，其「参与外键的列」不能更新** | `ConstraintException: Violates foreign key constraint because key "device_id: ..." is still referenced by a foreign key in a different table` |

**F8 的精确边界**（P2 实施时发现，主代理独立复验确认；这是对 P0 首轮探针的补充）：

| 操作 | 结果 |
| --- | --- |
| `UPDATE users SET status='disabled'`，users 被多方引用 | **允许**——`status` 不参与任何外键 |
| `UPDATE paired_devices SET identity_id=?`，该设备被 `device_refresh_credentials` 引用 | **拒绝**——`identity_id` 参与指向 `user_identities` 的复合外键 |
| 同上，但先删除该设备的凭据行 | **允许** |

因此判据是「**该列是否参与外键**」，而不是「该行是否被引用」。计划 §2.3 关于 `users` 单表可变的结论不受影响（`status`/`role` 都不参与外键）。

**对设备身份绑定的后果**：`paired_devices` 一旦签发了凭据（正常登录必然如此），它的 `identity_id` 就不可改写。所以计划 §4.3「设备应保留本次登录对应的 identity 关联」在实践中是**写一次**：同一设备用同一用户的**另一个身份**再次登录时，设备保留首次绑定的身份。登录响应改为**回读设备实际绑定的身份**，保证 `exchange` 与 `/me` 报出同一个身份。这是被数据库约束逼出的真实行为，不是实现取巧；复合外键仍保证身份与设备同属一个用户。

若产品上确实需要「同一设备切换身份」，唯一可行路径是先撤销该设备凭据再重新签发（会丢失重放检测所需的凭据行），因此**首版不提供**。

F1+F2+F3+F4 合起来意味着：**schema 16 的每一张需要新约束的表都必须以最终名字从零创建，旧表必须先整体拆除**。"加列 → 回填 → 加约束 → 改名"这条常规路径在当前版本上完全走不通。

### 2.2 可用能力（可以依赖）

| 能力 | 实测结果 |
| --- | --- |
| FK 在 INSERT 上生效 | 子行指向不存在的父被拒绝 |
| **FK 在 UPDATE 上生效** | 改子表 FK 列为不存在的父、或改成与复合父键不匹配的组合，均被拒绝 |
| 复合外键保证同属一个用户 | `(user_id, account_id) REFERENCES accounts(user_id, id)` 拒绝跨用户组合 |
| 删除被引用的父行 | 被拒绝 |
| **父表非键列更新（有子引用）** | **允许**——`UPDATE users SET status='disabled'` 在存在设备/账户/交易引用时成功 |
| **父表参与外键的列更新（有子引用）** | **拒绝**——见下方 F8 |
| 用户内唯一 + 跨用户可同键 | `UNIQUE (user_id, legacy_account_key)` 允许 A/B 同键，拒绝同用户重复 |
| 全局唯一身份键 | `UNIQUE (provider, subject)` 生效；`ON CONFLICT DO NOTHING` 收敛为一行 |
| CTAS 保真 | `CREATE TABLE ... AS SELECT` 完整保留 `UUID` / `DECIMAL(18,4)` / `DATE` |
| 事务回滚 | 回滚后旧表、旧数据、旧视图全部可用，无暂存表残留 |

### 2.3 结论一：users 保持单表可变，不拆表

计划 §4.1 曾预留「若版本限制被引用父行更新，则复用不可变身份表 + 可变状态表模式」。实测（2.2 第 5 行）表明**当前版本允许在有子引用时更新父表非键列**，因此：

- `users` 就是**一张普通可变表**，`status` 与 `role` 直接放在上面，停用用户即 `UPDATE users SET status='disabled'`。
- 用户 UUID 一经创建不再变更，因此 F4/F6 对 `users` 无影响。
- **不引入**不可变身份表 + 可变状态表的拆分。该模式在会话身份上已有先例（迁移 11），但它解决的是"会话可变生命周期"问题，与用户停用无关，不要机械照搬。

### 2.4 结论二：schema 15 → 16 的唯一可行迁移配方

```
阶段 1  把每张业务表用 CTAS 复制到无外键的暂存表      CREATE TABLE _stage_<t> AS SELECT * FROM <t>
阶段 2  拆除旧图：先 DROP 全部视图，再按依赖倒序 DROP 表（子先父后）
阶段 3  以最终名字从零 CREATE schema 16 全部表与视图（约束必须在 CREATE 时给全）
阶段 4  回填归属并装载：INSERT INTO <t> SELECT ..., <owner_user_id> FROM _stage_<t>
阶段 5  DROP 全部 _stage_<t>
```

整个序列在**一个事务**内执行。已实测：成功路径数据与 Decimal 保真、复合外键生效、视图可用；失败注入后 `ROLLBACK` 使旧库完全可用且无 `_stage_*` 残留。

注意：**不要**采用"建 `_v16` 表 → 拷贝 → 删旧表 → 改名"的写法，F4 会让被引用的父表改名失败。

---

## 3 冻结决策

### 3.1 用户与身份物理结构（P1a 据此建表）

| 表 | 关键约束 |
| --- | --- |
| `users` | `id UUID PRIMARY KEY`；`display_name`；`status`（`active`/`disabled`）；`role`（`admin`/`member`）；创建、更新时间。普通可变表 |
| `user_identities` | `id UUID PRIMARY KEY`；`user_id UUID NOT NULL REFERENCES users(id)`；`provider`；`subject`；可选 `email`、`display_name`；`UNIQUE (provider, subject)` |
| `paired_devices` | 增 `user_id UUID NOT NULL REFERENCES users(id)`；唯一键改为 `(user_id, public_key_fingerprint)` |
| `accounts` | 增 `user_id UUID NOT NULL REFERENCES users(id)`；`UNIQUE (user_id, legacy_account_key)`；**必须同时有 `UNIQUE (user_id, id)`** 供子表建复合外键 |

`UNIQUE (user_id, id)` 不是冗余：F3 要求复合外键的父列组合有唯一约束，缺它子表建不起来。这是 P1a 最容易漏掉的一条。

### 3.2 跨用户写入的数据库级保护范围

复合外键 `(user_id, X_id) REFERENCES parent(user_id, id)` 是当前唯一能由数据库保证"子资源与父资源同属一个用户"的机制，且实测在 INSERT 与 UPDATE 上都生效。

但计划 §4.1 明确「交易、FIFO、已实现记录、提醒经 account 归属隔离，**不机械地给所有子表重复添加 user 字段**」。这些表因此**没有** `user_id` 列，也就**无法**建复合外键，其跨用户保护**完全依赖应用层查询范围**。

这一点必须显式记录：P8 在验收时**不得**声称这些表有数据库级跨用户保护；P3 必须为它们逐条提供用户范围断言的测试证据（对应 V03/V04/V06）。

### 3.3 账本版本语义

- `ledger_version_seq` 仍是**全局序列**（保持既有实现）。
- 「用户当前账本版本」的显式定义：`SELECT coalesce(max(version), 0) FROM ledger_versions WHERE user_id = ?`。
- 因此**单个用户的版本号不连续**（A 可能看到 1 → 3）。
- 唯一需要改动的实现点：[import_commit_repository.py:223](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/import_commit_repository.py#L223) `_current_ledger_version()` 当前是全局 `max(version)`，必须加用户条件。`LEDGER_VERSION_CONFLICT` 判定（[同文件:148](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/import_commit_repository.py#L148)）随之自动获得用户范围。
- 已核实 Flutter 只用等值比较（[market_workspace.dart:170](/Users/Gin/Workspace/Lyra/apps/mobile/lib/features/market/market_workspace.dart#L170)），无 `+1` 或计数假设，不连续是安全的。
- **禁止**任何模块改回全局 `max(version)`。

### 3.4 公开健康响应

当前 `/v1/health` 七段中，下列字段在多用户下是用户活动信息，必须处理：

| 字段 | 现状 | 处置 |
| --- | --- | --- |
| `gmail.totalConnections` | 全库连接数 | 移除（用户活动计数） |
| `gmail.attentionRequired` | 全库待处理连接数 | 移除（用户活动计数） |
| `gmail.status` | 由上面两个推导 | 改为部署级：有任一连接异常即 `degraded`，不暴露数量 |
| `writeQueue.pendingOperations` | 部署级队列深度 | 保留（部署可用性指标，非用户数据） |
| `agents.*` | provider 可用性计数 | 保留（部署级供应商状态，非用户数据） |
| `process` / `database` | 部署可用性 | 保留 |

原则不变：缺失段不填占位值，不暴露用户数据与用户活动计数。客户端解析与测试同步更新，**不得**填伪造的 0 值维持旧形状。

### 3.5 无后台 worker 的事实（影响停用级联范围）

实测当前服务端**没有任何后台 worker、线程池或定时任务**：

- 唯一的 `asyncio.create_task` 是 [agent_events.py:126](/Users/Gin/Workspace/Lyra/server/src/lyra/api/agent_events.py#L126) 的 WS 事件发送任务。
- Gmail 同步（[gmail_routes.py:133](/Users/Gin/Workspace/Lyra/server/src/lyra/api/gmail_routes.py#L133)）与导入提交（[import_routes.py:207](/Users/Gin/Workspace/Lyra/server/src/lyra/api/import_routes.py#L207)）都是**请求内联执行**。
- 全部写操作经 [WriteCoordinator](/Users/Gin/Workspace/Lyra/server/src/lyra/application/write_coordinator.py) 的 `asyncio.Lock` 串行，带 `accepting` 与 `pending_operations`。

因此计划 §4.4 与 V11 的「排队任务」实际指：**已在 `WriteCoordinator` 锁上排队等待的请求**，以及**在飞 Agent 回合**。P4/P5 的停用级联范围据此收窄，且 V11 可确定性测试（持有协调器锁 → 入队 → 停用 → 释放 → 断言拒绝），不需要等待真实后台调度。

`AgentTurnOrchestrator` 已有设备级取消 [cancel_device_turns](/Users/Gin/Workspace/Lyra/server/src/lyra/application/agent_turn_orchestrator.py#L291)，P4 需要按用户维度提供对应能力。

### 3.6 `UserRepository` 冻结签名

文件归属：**P1a 独占** `/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/user_repository.py`。P2 及后续模块只调用不修改。

领域类型（放同一文件或 `security/` 下，由 P1a 择一，但 P2 必须能直接导入）：

```python
UserRole = Literal["admin", "member"]
UserStatus = Literal["active", "disabled"]

@dataclass(frozen=True)
class UserRecord:
    id: str; display_name: str; status: UserStatus; role: UserRole
    created_at: str; updated_at: str          # ISO UTC 毫秒 + Z 文本，与既有仓储同形

@dataclass(frozen=True)
class UserIdentityRecord:
    id: str; user_id: str; provider: LoginProviderId; subject: str
    email: str | None; display_name: str | None
    created_at: str; updated_at: str

@dataclass(frozen=True)
class ResolvedUser:
    """一次登录解析的结果。"""
    user: UserRecord
    identity: UserIdentityRecord
    created: bool        # 本次是否新建了用户；重放/重复登录为 False
```

方法签名：

| 方法 | 语义 |
| --- | --- |
| `find_or_create_user_for_identity(identity: LoginIdentity, *, now: datetime) -> ResolvedUser` | 按 `(provider, subject)` 查找；未命中则原子创建 `member` 用户 + 身份。必须收敛并发，见下方算法 |
| `find_user_by_id(user_id: str) -> UserRecord \| None` | 读取用户；不存在返回 `None` |
| `find_identity(provider, subject) -> UserIdentityRecord \| None` | 读取身份 |
| `set_user_status(user_id: str, status: UserStatus, *, now: datetime) -> UserRecord` | 停用/恢复。用户不存在时抛 `ApplicationError`（404 语义） |
| `list_users(*, limit: int, offset: int) -> tuple[UserRecord, ...]` | 受限分页；`limit` 有上界，越界抛错而非静默截断 |
| `count_active_admins() -> int` | 「最后管理员保护」判据 |
| `create_admin(identity: LoginIdentity, *, now: datetime) -> ResolvedUser` | 离线初始化入口用；固定 `role='admin'`。已存在同身份时返回既有用户并**明确失败**，不静默降级 |

**并发收敛算法**（对应 V02「两个独立登录事务并发完成」）。所需 SQL 原语已实测：

- `INSERT ... ON CONFLICT (provider, subject) DO NOTHING RETURNING id` → 冲突时返回**空结果集**，插入成功时返回一行。可据此判断本次是否抢到。
- 无子引用的用户可以 `DELETE`；有身份引用的用户删除被 FK 拒绝。

```
1. SELECT ... WHERE provider = ? AND subject = ?        -- 快路径
   命中 → 直接返回，created=False
2. 未命中：
   a. INSERT INTO users (新 UUID, role='member', status='active')
   b. INSERT INTO user_identities (...) ON CONFLICT (provider, subject) DO NOTHING RETURNING id
   c. 有返回行 → created=True
   d. 无返回行 → 并发被抢先：DELETE 刚建的候选用户，重新查询既有归属，created=False
```

**调用方必须把 2a–2d 包在同一个事务里**（复用 [device_repository.py:40](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/device_repository.py#L40) 的 `transaction()` 写法），保证失败时不残留「只有用户、没有身份」的半成品。禁止用 `DO UPDATE` 覆盖他人归属。

### 3.7 设备与身份的关联

计划 §4.3 要求「设备应保留本次登录对应的 identity 关联，身份必须与设备的 user 一致」。落地方式：`paired_devices` 增 `identity_id UUID NOT NULL REFERENCES user_identities(id)`。

这样 `GET /v1/me` 的身份来源可由 `paired_devices → user_identities` 直接取得，无需猜测。`user_id` 与 `identity_id` 的一致性由 §3.2 的复合外键手段保证：`paired_devices` 同时带 `user_id`，可加 `FOREIGN KEY (user_id, identity_id) REFERENCES user_identities(user_id, id)`，因此 `user_identities` 也必须建 `UNIQUE (user_id, id)`。

**P1a 必须建的三处 `UNIQUE (user_id, id)`**：`user_identities`、`accounts`，以及任何需要被复合外键引用的父表。遗漏任何一处都会让子表建不起来（F3）。

### 3.8 `AuthContext` 冻结接口

现状：`require_device`（[dependencies.py:39](/Users/Gin/Workspace/Lyra/server/src/lyra/api/dependencies.py#L39)）是 6 个业务路由的**路由级依赖**（`APIRouter(dependencies=[Depends(require_device)])`），把设备写入 `request.state.device`，处理器通过 `request.state.device.id` 取设备。

目标（P2 落地）：

| 项 | 冻结值 |
| --- | --- |
| 文件 | 新增 `server/src/lyra/security/auth_context.py`，**P2 独占创建** |
| 类型 | `@dataclass(frozen=True) class AuthContext: user_id: str; device_id: str; role: UserRole` |
| 生成 | 仅由服务端在「校验访问令牌 → 设备状态 → 用户状态」全部通过后构造，任何调用方不得自行拼装 |
| 依赖 | `require_user(request, authorization) -> AuthContext`，放 [dependencies.py](/Users/Gin/Workspace/Lyra/server/src/lyra/api/dependencies.py)；同时写 `request.state.auth` |
| 管理依赖 | `require_admin(...) -> AuthContext`，非 `admin` 抛 403；同样写 `request.state.auth` |
| 移除 | **`require_device` 直接删除**，不留兼容路径。6 个业务路由改用 `require_user`；需要设备 id 处改读 `AuthContext.device_id` |

`request.state.device` 一并删除：设备 id 已在 `AuthContext` 里，保留两条来源会让归属判定分叉。

错误码（沿用既有 401 语义，新增仅一条）：

| 情形 | 码 | 状态 |
| --- | --- | --- |
| 令牌缺失/非法/设备不存在 | `DEVICE_CREDENTIAL_INVALID` | 401（保持现状，不区分原因以免枚举令牌） |
| 设备已撤销 / 令牌已过期 | 保留领域层原始码 | 401（保持现状，客户端据此决定重登还是刷新） |
| **用户已停用** | **`USER_DISABLED`** | **403**，`retryable=false`；客户端据此结束会话，不进入刷新重试 |

「已验证但未注册」不引入新错误码——直接自动注册（计划 §4.2）。

### 3.9 登录响应与 `/me` 的 `user` 对象形状

计划 §4.3 要求登录完成响应与 `GET /v1/me` 返回**同一个** `user` 对象。冻结为：

```json
{
  "user": {
    "userId": "<uuid>",
    "displayName": "<string>",
    "status": "active | disabled",
    "role": "admin | member",
    "identity": {
      "provider": "google | github | linuxdo",
      "subject": "<string>",
      "email": "<string | null>"
    }
  }
}
```

把身份字段收进 `identity` 子对象而不是平铺，是为了避免与用户自身的 `displayName` 语义相撞——身份上的 `display_name` 是第三方昵称，不是用户在 Lyra 的显示名。身份**必须**来自当前设备会话的已验证关联（`paired_devices.identity_id`，见 §3.7），不得由请求参数指定。

登录响应的其余部分（设备与令牌字段）保持现有形状不变。

### 3.10 离线初始化入口的接口

计划 §9 要求「新安装同样要求显式指定首个管理员的 `(provider, subject)`」，且「不得新建公开 bootstrap HTTP 入口」。既有 owner 配置位于 [google_login_client.py:78](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/auth/google_login_client.py#L78) 与 [oauth_login_client.py:144](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/auth/oauth_login_client.py#L144)，均为**运行时鉴权配置**，本次要整体退出。

冻结：`server/src/lyra/admin.py`（P1b 新增）是一个**离线 CLI**，管理员与 owner 归属通过**命令行参数**显式给出，不新增长期环境变量。

| 参数 | 用途 |
| --- | --- |
| `--database` | 目标库路径；缺省取既有 `Settings.lyra_database_path` |
| `--provider` / `--subject` | 首个管理员的 `(provider, subject)`，新安装必填 |
| `--owner-identity provider:subject` | 可重复；旧库归属时**显式确认**映射到原用户的身份 |
| `--display-name` | 可选显示名 |

理由：这是一次性离线操作，不是运行时配置。用环境变量会让一个只该用一次的授权长期驻留在部署配置里，容易被误认成运行时鉴权开关。`config.py` 因此**不需要新增字段**。

入口不得打印用户 token，不得读取隐含的「首个请求」作为管理员凭据（计划 §9 末段）。

### 3.11 `resolve_legacy_account_key` 的签名变更（跨 P3/P4/P6）

现状：[account_lookup.py:15](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/account_lookup.py#L15) 只按 `account_id` 全库查找，返回脱敏账户键。该模块自己的 docstring 已经指出这是「最容易静默出错的一类转换」。

多用户下它变成**越权入口**：A 传入 B 的账户 UUID 会解析出 B 的 legacy key，随后账本查询若也按 legacy key 无范围匹配，就会返回 B 的数据。计划 §4.1 已点名此点。

**冻结新签名**：

```python
def resolve_legacy_account_key(
    connection: duckdb.DuckDBPyConnection, user_id: str, account_id: str
) -> str | None: ...
```

`user_id` 放在 `account_id` **之前**，避免调用方漏传时被位置参数静默错配。不属于该用户的账户返回 `None`（与「不存在」不可区分，符合计划 §4.2）。

生产调用点共 5 处，归属如下：

| 调用点 | 归属 |
| --- | --- |
| [account_lookup.py](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/account_lookup.py#L15) 定义 | **P3** 改签名并加用户条件 |
| [ledger_routes.py:99](/Users/Gin/Workspace/Lyra/server/src/lyra/api/ledger_routes.py#L99) | P3 |
| [market_routes.py:231](/Users/Gin/Workspace/Lyra/server/src/lyra/api/market_routes.py#L231) | **P6**（与 P3 同波次，按本冻结签名直接写） |
| [financial_tool_definitions.py:150](/Users/Gin/Workspace/Lyra/server/src/lyra/application/financial_tool_definitions.py#L150) | P4 |
| [agent_session_repository.py:211](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/agent_session_repository.py#L211)（包装方法 `resolve_legacy_account_key(account_id)`） | P4，包装方法同样加 `user_id` |

**P3 与 P6 同波次**，P6 从开工起就按新签名写，不要等 P3 落地后再改。

同类需要带用户范围的还有账本 SQL 的 `_scope_filter`（[ledger_repository.py:86](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/ledger_repository.py#L86)）：`scope.kind == "all"` 当前**不加任何条件**，账户分支只按 `a.legacy_account_key = ?` 匹配。两处都必须叠加 `a.user_id = ?`。

### 3.12 仓储层无用户范围的入口清单（P3–P6 逐条收口）

§4 的路由清单是 HTTP 层盘点；下面是从代码里实际找出的**仓储/服务层**无用户范围入口。它们是越权的真实落点，P8 核对时不能只看是否出现 `user_id` 字样。

| 入口 | 现状 | 归属 |
| --- | --- | --- |
| [account_lookup.py:15](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/account_lookup.py#L15) `resolve_legacy_account_key` | 只按 `account_id` 全库查 | P3（见 §3.11） |
| [ledger_repository.py:86](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/ledger_repository.py#L86) `_scope_filter` | `all` 不加条件；账户分支只按 legacy key | P3 |
| [account_repository.py:33](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/account_repository.py#L33) `load_brokerage_account` | 只按 `account_id` | P3 |
| [account_repository.py:66](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/account_repository.py#L66) `persist_created_account` | `account` 字典无 `user_id` | P3 |
| [import_commit_repository.py:220](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/import_commit_repository.py#L220) `_current_ledger_version` | `max(version)` 全库 | P3（见 §3.3） |
| [import_routes.py:389](/Users/Gin/Workspace/Lyra/server/src/lyra/api/import_routes.py#L389) `_active_accounts` | 全库账户候选 | P3 |
| [gmail_connection_repository.py:111](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/gmail_connection_repository.py#L111) `load_gmail_connections` | 全库连接；**同时被健康端点使用** | P5 加用户范围；健康端点改为部署级（§3.4） |
| [gmail_connection_repository.py:81](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/gmail_connection_repository.py#L81) 插入依赖全局 `google_subject UNIQUE` | 唯一键已由 P1a 改为 `(user_id, google_subject)`，报错文案「该 Google 账号已有连接」需按用户表述 | P5 |
| [agent_session_repository.py:202](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/agent_session_repository.py#L202) `resolve_legacy_account_key` 包装方法 | 无用户范围 | P4 |
| [agent_events.py:97](/Users/Gin/Workspace/Lyra/server/src/lyra/api/agent_events.py#L97) WS 订阅 | 认证设备后只按 `sessionId` 订阅，无归属校验 | P4 |
| [runtime.py:51](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/market_data/runtime.py#L51) `MarketDataRuntime._routes` / `_settings_revision` | 单例可变状态；`service.set_routes` 改共享服务 | P6 |
| [main.py:177](/Users/Gin/Workspace/Lyra/server/src/lyra/main.py#L177) `load_market_settings` | 启动装载全局偏好 | P6 |

`MarketDataService` 的公开报价缓存与供应商预算**保持部署级共享**（计划 §4.4），不要因为加了用户就把它按用户复制——那会击穿缓存复用与全局预算。

---

## 4 入口归属清单（P8 覆盖用）

共 48 项（47 REST + 1 WS），全部来自签入的路由清单。归属列为**实施任务**。

| 路由组 | 数量 | 归属 | 隔离要点 |
| --- | --- | --- | --- |
| `/v1/auth/*` | 5 | P2 | `start`/`callback`/`exchange`/`logout`/`providers`；按 `(provider, subject)` 注册与登录 |
| `/v1/devices/tokens` | 1 | P2 | 刷新必须校验用户状态 |
| `/v1/me`（新增） | 1 | P2 | 身份来自当前设备会话的已验证关联 |
| `/v1/admin/users*`（新增） | 2 | P2 | 仅 admin；不得停用最后一个活动管理员 |
| `/v1/ledger/*` | 9 | P3 | 账户 CRUD、summary、positions、trades、cashflow、realized、expiring、audit |
| `/v1/analytics/*` | 9 | P3 | 全部经账户归属隔离 |
| `/v1/imports/*` | 4 | P3 | 作业先按用户查，再校验预览令牌与幂等 |
| `/v1/agent/*` | 6 | P4 | 创建、读取、历史、工具审计、发送、取消 |
| `/v1/agents` | 1 | P4 | provider 可用性，部署级 |
| `/v1/events`（WS） | 1 | P4 | 订阅/重放校验会话 user；停用后关闭 |
| `/v1/google/*` | 7 | P5 | 连接、映射、同步、断开；禁止按 subject 全局 upsert |
| `/v1/market/*` | 3 | P6 | 设置按用户；共享报价缓存按公开报价身份 |
| `/v1/health` | 1 | 主代理 | 见 §3.4 |
| `/v1/system/info` | 1 | 主代理 | 协议与 schema 版本门槛 |

后台写入口：**无独立 worker**（见 §3.5）。写路径为 `WriteCoordinator` + 请求内联。

---

## 5 证据与复现

复现脚本（在 `server/` 下执行，使用项目既有 UV 约定）：

```bash
UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \
  uv run python ../docs/plans/assets/p0/01-fk-basics.py
# 依次 02 / 03 / 04
```

| 脚本 | 覆盖 |
| --- | --- |
| [01-fk-basics.py](assets/p0/01-fk-basics.py) | FK 在 INSERT/UPDATE/DELETE 上的强度、父表非键列更新、事务回滚、唯一约束 |
| [02-constraints-and-migration.py](assets/p0/02-constraints-and-migration.py) | F1/F2 约束变更限制、F3 复合外键前提、用户内唯一键 |
| [03-rebuild-roundtrip.py](assets/p0/03-rebuild-roundtrip.py) | 表重建往返、F4 改名限制、复合外键在 UPDATE 上的强度 |
| [04-staged-rebuild-and-rollback.py](assets/p0/04-staged-rebuild-and-rollback.py) | 唯一可行迁移配方、CTAS 保真、故障注入回滚 |

服务端基线测试（改动前，2026-10-16）：

```bash
cd server
UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python uv run pytest -q
# 2453 passed, 4 skipped, 1 warning in 140.11s  → 退出码 0
```

唯一警告是 Starlette 关于 `httpx` 的弃用提示，与本次改动无关。本基线是后续所有波次的回归对照。

Flutter 基线（改动前，2026-10-16）：

```bash
cd apps/mobile
flutter analyze --no-pub     # No issues found!
flutter test --no-pub --reporter expanded --timeout 60s --concurrency=2
# 262 passed, 5 skipped  → 退出码 0
```

**沙箱前提（已实测）**：Flutter 工具链每次运行都会执行 `bin/internal/update_engine_version.sh`，该脚本无条件写入 `$FLUTTER_ROOT/bin/cache/engine.stamp` 与 `engine.realm`。在只允许写工作区的文件策略下这两个写入被拒绝，`flutter analyze` 直接以退出码 1 失败且**只打印两行 `Operation not permitted`**，看起来像分析失败，实际是权限问题。本次已在放宽文件策略后跑通。**P7 与 P8 必须在能写 Flutter SDK 缓存目录的策略下运行 Flutter 命令**，否则会误判为代码问题。

环境限制：`uv run` 在沙箱下需 `UV_CACHE_DIR`，项目既有约定见 [preflight_cutover.py:9](/Users/Gin/Workspace/Lyra/server/scripts/preflight_cutover.py#L9)；该问题会伪装成「接管既有账本未通过」，见 [preflight_cutover.py:78](/Users/Gin/Workspace/Lyra/server/scripts/preflight_cutover.py#L78)。

---

## 6 P0 完成判定

| P0 条目 | 状态 |
| --- | --- |
| 确认未提交改动与基线一致 | 完成：工作区干净，无待保留改动 |
| 冻结第 4 节字段、错误码、仓储构造参数、工具上下文与响应形状 | 部分：物理结构与关键语义已冻结（§3）；错误码与响应形状沿用计划 §4.2/§4.3 既有契约，未新增待定字段 |
| `UserRepository` 完整方法签名与文件归属 | 完成：归属 P1a 独占，签名见 §3.6（含并发收敛算法） |
| 共享 `AuthContext` 文件与接口 | 完成：§3.8，P2 落地 |
| 登录响应与 `/me` 的 `user` 对象形状 | 完成：§3.9 |
| 冻结「用户当前账本版本」定义 | 完成：§3.3 |
| 冻结公开健康响应字段清单 | 完成：§3.4 |
| DuckDB 父子外键更新、表重建与事务回滚定向验证 | 完成：§2，含唯一可行迁移配方 |
| 确定 users 最终物理结构与身份/设备关联方式 | 完成：§3.1、§2.3 |
| 列出 REST/WS 入口、后台写入口与公开健康字段 | 完成：§4 |
| 确认 schema/协议、测试入口与可用 SDK | 完成：§1、§5 |

**P1a 可以开工**。P2/P3/P7 可依据本文件独立开发。

---

## 7 主代理波次集成待办（Lead 独占文件）

以下变更**必须等子代理的测试跑完再执行**——它们改的是所有测试共用的常量，中途修改会让子代理的测试结果不可复现。记在此处以免遗漏。

### 7.1 协议升到 3

`MIN_SUPPORTED_PROTOCOL_VERSION` 必须同步升到 3（计划 §1.7「最低支持协议同步设为 3」）。已核实的完整影响面：

| 文件 | 位置 | 改动 |
| --- | --- | --- |
| [protocol.py](/Users/Gin/Workspace/Lyra/server/src/lyra/protocol.py#L12) | L12、L14 | `LYRA_PROTOCOL_VERSION = 3`；`MIN_SUPPORTED_PROTOCOL_VERSION = 3`；模块 docstring 的「迁移期间不得变更」需改写 |
| [preflight_cutover.py](/Users/Gin/Workspace/Lyra/server/scripts/preflight_cutover.py#L137) | L137–L139 | 检查标签「协议版本为 2」→ 3，断言 `value == "3"`，docstring 同步 |
| [test_api.py](/Users/Gin/Workspace/Lyra/server/tests/test_api.py#L120) | L120–L131、L150–L157 | 测试名 `..._protocol_two` 与三处 `2` → `3` |
| [test_client_protocol_gate.py](/Users/Gin/Workspace/Lyra/server/tests/test_client_protocol_gate.py#L88) | L88–L89 | 期望的 `min/max_supported_protocol_version` → `3` |
| [test_preflight_script.py](/Users/Gin/Workspace/Lyra/server/tests/test_preflight_script.py#L89) | L89 | 断言消息「协议版本应为 2」→ 3 |

**不需要改**：所有测试都从 `lyra.protocol` 导入 `CLIENT_PROTOCOL_HEADERS`，常量一改自动跟随。`error_envelope.py` 的门槛逻辑直接读常量，无需改动。

### 7.2 schema 版本相关的 Lead 文件

- [test_api.py:147](/Users/Gin/Workspace/Lyra/server/tests/test_api.py#L147) 断言 `CURRENT_SCHEMA_VERSION == 15`，P1a 提到 16 后必然失败——属预期破坏，集成时改为 16。
- [preflight_cutover.py:106](/Users/Gin/Workspace/Lyra/server/scripts/preflight_cutover.py#L106) `check_schema_version_is_self_consistent` 只校验清单与常量自洽，不需要改。
- [preflight_cutover.py:72](/Users/Gin/Workspace/Lyra/server/scripts/preflight_cutover.py#L72) 的「接管既有账本」检查读冻结夹具的 `max(version)` 并断言能打开，**schema 15 夹具本身不变**，该检查保持原样。

### 7.3 路由清单

P2 落地后新增 `GET /v1/me`、`GET /v1/admin/users`、`PATCH /v1/admin/users/{}`，须同步追加到 [route-manifest.json](/Users/Gin/Workspace/Lyra/server/tests/fixtures/route-manifest.json)（48 → 51 项），并更新其 `note` 字段的计数描述。`tests/test_route_manifest.py` 会守住。

### 7.4 健康响应收口

按 §3.4 的字段清单改 [health.py](/Users/Gin/Workspace/Lyra/server/src/lyra/api/health.py) 与 [main.py:93](/Users/Gin/Workspace/Lyra/server/src/lyra/main.py#L93) 的 `gmail_connections` 装配，并同步 `tests/test_health.py` 的断言。**不得填伪造的 0 值维持旧形状。**

### 7.5 `main.py` 接线

已核实的待改点（详见 §3.8、§3.10）：

- `_authorize_owner_device`（L32）→ 已改为 `_authorize_user_device`：用户解析 + 设备签发，返回体加入 `user`/`identity`（§3.9）。
- `build_owner_login_service`（L111）→ 已改名 `build_login_service`，内部换成自动注册（模块同时由 `owner_login_service.py` 改名为 `login_service.py`）。
- `build_authenticator`（L143）→ 用户感知。
- `load_market_settings`（L177）→ 启动不再装载全局偏好（P6）。
- `build_tool_definitions`（L160）→ 工具上下文注入 userId（P4）。
- `create_ledger_router()` / `create_import_router()` 等无参构造 → 按 P3 的接口说明改为注入用户范围仓储。

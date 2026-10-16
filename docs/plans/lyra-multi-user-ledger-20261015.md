# Lyra 多用户独立账本执行计划

日期：2026-10-15；2026-10-16 评审补缺（修订内容见第 11 节）。状态：待实施。本次交付仅保存计划，不代表账号系统已实现或已部署。

目标是在同一个 Server 上支持少量用户，各自拥有独立账本，使用现有 Google、GitHub、LINUX DO OAuth 登录，首次登录自动注册普通用户。本计划供主代理协调、子代理按模块实施；主代理负责设计决策、公共接口、集成与最终审核。

仓库根目录：`/Users/Gin/Workspace/Lyra`。本计划以当前 Python/FastAPI 与 Flutter 实现为基线。历史语言迁移文档中的“固定 schema 15、协议 2”属于此前迁移约束；本功能需要同步升级服务端和客户端。

## 1 交付范围与固定决策

1. 复用三种 OAuth 的授权、回调、state、PKCE 和身份验证实现。改造登录成功后的用户映射与自动注册，不重新开发 OAuth，也不新增密码登录。
2. Server 自己管理用户。身份关系为 `Server → userId → 设备会话、证券账户、导入、Agent 会话、Gmail 连接`。一个用户可以有多个设备、多个证券账户；现有账户汇总就是该用户的账本，不另建共享账本或成员关系模型。
3. 保留单进程 DuckDB、现有连接锁与写协调器。所有用户共用数据库，通过服务端强制用户范围隔离。保持现有 Decimal、FIFO、交易解析和去重语义。
4. Server 上已配置且可用的三种 OAuth 来源均允许首次登录自动注册，无需管理员审批或 owner 白名单。新身份获得 `member` 角色和空账本，已有身份登录原用户。管理功能保留用户列表、停用与恢复；管理端先交付受保护的 API，不新增完整管理后台。
5. 用户角色仅 `admin`、`member`；角色只控制用户管理功能。管理员调用账本接口时也只能访问自己的数据。首次管理员由离线初始化入口显式指定，不允许首位访问者自动认领。
6. 首版每个 App 保持一个当前登录用户，通过退出再登录切换。身份以 `(provider, subject)` 唯一识别，未经明确关联的不同来源身份分别注册独立用户，即使邮箱相同也不自动合并。内部身份表支持原 owner 多来源身份迁移，自助绑定额外登录方式的界面不在首版范围。
7. 目标 schema 为 16，客户端协议为 3，最低支持协议同步设为 3。若执行时版本已被其他工作占用，由主代理顺延版本并统一更新本计划和相关契约，禁止覆盖历史迁移。
8. 发布包含数据迁移和重新登录，不保留无用户归属的业务访问路径。全部隔离验收通过后才对用户开放自动注册；通过部署顺序和网络访问范围控制发布，不另建长期注册开关或审批流程。

## 2 已核实的实现入口

| 入口 | 当前事实 | 实施用途 |
| --- | --- | --- |
| [OAuth 登录服务](/Users/Gin/Workspace/Lyra/server/src/lyra/application/owner_login_service.py) | OAuth 验证后调用 `provider.assert_owner` | 替换为用户查找与自动注册，保留授权安全检查 |
| [设备仓储](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/device_repository.py) | owner 身份按 provider 更新，设备按全库公钥指纹查找 | 建立稳定用户映射和用户内设备归属 |
| [鉴权依赖](/Users/Gin/Workspace/Lyra/server/src/lyra/api/dependencies.py) | `require_device` 返回设备，没有内部用户 | 统一注入认证上下文 |
| [账本仓储](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/ledger_repository.py) | `scope=all` 不增加账户条件 | 将全部查询限制到当前用户 |
| [导入路由](/Users/Gin/Workspace/Lyra/server/src/lyra/api/import_routes.py) | 账户候选与最新账本版本从全库读取 | 隔离账户匹配、作业和版本 |
| [Agent 事件入口](/Users/Gin/Workspace/Lyra/server/src/lyra/api/agent_events.py) | 认证设备后按 session ID 订阅 | 增加会话归属和停用处理 |
| [行情运行时](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/market_data/runtime.py) | 来源路由和设置修订号是单例状态 | 将用户偏好与共享报价服务分开 |
| [本地偏好](/Users/Gin/Workspace/Lyra/apps/mobile/lib/core/preferences/app_preferences.dart) | 偏好键使用 Server 与 provider/subject | 改为 Server 与内部 userId |
| [迁移框架](/Users/Gin/Workspace/Lyra/server/src/lyra/db/migrations.py) | 当前 schema 15，迁移事务提交后 CHECKPOINT | 增量升级并验证重启 |
| [会话身份迁移](/Users/Gin/Workspace/Lyra/server/src/lyra/db/sql/agent-session-identity-schema.sql) | 已因 DuckDB 外键更新约束拆分不可变身份和可变状态 | 新用户与设备状态设计的实测依据 |

这些事实足以开始实施。只对当前依赖版本下的迁移约束做定向验证，不再开展一轮全面架构调查。

## 3 开工约束

- 执行前读取适用的 AGENTS 指令，检查当前工作区。已有 review 修复可能尚未提交，必须保留；不得 reset、覆盖、自动提交或推送其他改动。
- 不读取、列出或提交 `application-gin.yml`。搜索与 Git 文件集合必须排除该文件；禁止 `git add .` 或读取真实凭据、真实账本作为测试输入。
- 只使用合成数据和仓库中的冻结测试夹具。生产迁移、创建真实用户、对外发信、部署不属于本计划文件的执行授权。
- 子代理先说明所改模块的原逻辑与保持的行为，再实施。方法注释写清参数、返回和约束；行间注释解释非直观原因。
- 不通过可选 `user_id`、默认 owner、全库回退、吞异常或空结果伪装失败来让旧测试通过。调用方和测试夹具应显式迁移到新契约。
- 新增依赖前检查已有库。现有 UUID、opaque token 哈希、OAuth、加密器、事务和写协调器优先复用。
- Android 构建、调试或打包时先使用 `android` 或 android-cli skill。普通文件修改遵守既有 Flutter 风格；不顺带升级依赖或 Gradle。

## 4 公共数据与权限契约

### 4.1 用户与资源归属

| 实体 | 目标约束 |
| --- | --- |
| `users`，新增 | 稳定 UUID；显示名称；`active/disabled` 状态；`admin/member` 角色；创建、更新时间 |
| `user_identities`，新增 | `user_id`、provider、subject、可选 email/display name；全局唯一 `(provider, subject)`；用户与身份映射不因再次登录而转移 |
| `paired_devices` | 必须关联 user；按 `(user_id, public_key_fingerprint)` 查找和约束唯一；设备一旦归属用户就不改 owner |
| `accounts` | 必须关联 user；`legacy_account_key` 改为用户内唯一；UUID 仍全局唯一 |
| `import_files` | 必须直接关联 user，不能仅依靠可能为空的目标账户；目标账户必须同属该用户 |
| `ledger_versions` | 增加 user；物理 version 可继续使用全局序列，所有业务查询取当前用户自己的最新版本，空账本为 0 |
| `agent_session_identities` | 在不可变会话身份上记录 user；会话仓储通过该身份校验归属，创建设备与账户必须同属该用户 |
| `gmail_connections` | 必须关联 user；同一 Google subject 在不同用户下独立存储、不得通过 upsert 接管他人连接；唯一键按用户划分 |
| `market_source_settings` | 用 user 替代 singleton；revision 和来源偏好各自独立 |
| 交易、FIFO、已实现记录、提醒 | 经 account 归属隔离，不机械地给所有子表重复添加 user 字段 |
| Agent 消息与工具审计 | 经不可变 session 身份隔离，包括历史会话和关闭会话 |
| Gmail 映射与消息 | 经 connection 隔离；关联的 account、transaction 必须与 connection 同属一个用户 |
| 设备凭据与幂等记录 | 经不可变的设备归属隔离；保留设备级幂等唯一键，返回缓存结果前仍检查当前授权 |
| 业务审计 | 区分操作者 `actor_user_id` 与资源所属 `subject_user_id`；部署级 system 事件可无用户，用户后台任务必须保留资源所属用户 |

DuckDB 定向验证是 P0/P1a 门槛：建立真实父子外键后，测试用户停用、设备撤销、账户归档和 Gmail 状态更新。**P0 已实测完成**（DuckDB 1.5.6）：父表非键列在有子引用时可以更新，因此 `users` 保持单表可变，**不采用**不可变身份表 + 可变状态表模式；同时实测 `ADD COLUMN ... NOT NULL`、`ADD/DROP CONSTRAINT` 均不支持且被引用父表不可改名，故 schema 16 必须整体重建。完整结论与迁移配方见 [P0 冻结契约](lyra-multi-user-ledger-p0-contract-20261016.md) §2。子代理不得自行移除完整性校验或另选一种模型。

所有跨资源写入必须在事务中确认归属。外键能保证记录存在，不代表记录属于同一用户。对重复账户标识的支持不能只改唯一索引：`account_lookup`、账本 SQL、导入匹配及 Agent 工具的 legacy key 解析都必须带用户范围。

### 4.2 认证上下文与仓储

- 新增不可变 `AuthContext(user_id, device_id, role)`，仅由服务端验证访问令牌、设备状态、用户状态后生成。
- 普通业务路由使用统一的 `require_user` 依赖；管理路由再要求 admin。请求正文和模型参数不能指定或覆盖业务访问的 userId。
- 用户业务仓储在构造时要求 `user_id`，单个方法继续接收账户或资源 ID；禁止 `user_id=None` 表示全部用户。共享基础设施仓储与管理员用户目录单独明确职责。
- 所有业务资源按“当前用户 + 资源 ID”查询。有效但不属于当前用户的资源与不存在资源返回相同 404；格式非法沿用 400；禁止用空账本响应掩盖越权。
- 禁用账号返回明确的 `USER_DISABLED`；已验证的新身份进入自动注册，不引入“未开通”错误。其他既有凭据错误码保留。客户端对禁用状态结束会话，不进入刷新重试循环。
- Agent 工具的 userId 来自服务端会话上下文。后台导入、邮件同步、模型回调同样传递明确 userId，不从某个全局“当前用户”变量读取。
- 同一用户的不同设备可以访问自己的账本和 Agent 历史；设备字段继续用于凭据撤销与审计，不能代替用户数据归属。

### 4.3 登录与用户管理 API

以下为待实现契约，沿用现有 JSON 命名和错误信封，不表示这些新增接口已经存在。

| 接口 | 契约 |
| --- | --- |
| `POST /v1/auth/{provider}/start` | 保留现有字段与来源可用性检查；不增加注册表单字段，不在此阶段创建用户 |
| 既有 callback 与 exchange | 保留 provider、state、PKCE、nonce 校验；callback 只验证来源身份，exchange 验证客户端持有的 verifier 后，按 `(provider, subject)` 查找用户或自动创建普通用户；用户、身份、设备与凭据签发在一个事务中完成 |
| 登录完成响应 | 保留现有设备和令牌字段；`user` 对象包含 `userId`、`displayName`、`status`、`role` 及当前已验证身份的 `provider`、`subject`、可选 `email` |
| `GET /v1/me`，新增 | 返回与登录响应一致的 `user` 对象，身份来源必须来自当前设备会话的已验证关联 |
| `GET /v1/admin/users`，新增 | 仅 admin 可调用，返回用户管理所需字段与受限分页，不返回账本或第三方令牌 |
| `PATCH /v1/admin/users/{userId}`，新增 | 首版仅接受状态变更；禁止通过该接口修改角色或身份；不得停用最后一个活动管理员 |

自动注册只在完整 OAuth 验证和客户端兑换验证成功后发生。回调未完成、授权取消、verifier 无效或事务过期时不创建用户。OAuth 未提供邮箱不阻止注册，身份键始终是已验证的 `(provider, subject)`。

同一身份的两个独立登录事务并发完成时，必须通过既有写协调器和唯一约束收敛到同一 userId，可以各自签发所属设备会话；同一个登录事务仍保持现有单次兑换与重放保护。事务失败不得残留只有用户、没有身份的半成品，重试不重复注册。

命中已存在且被停用的用户时，直接拒绝登录，保留身份映射以防同一身份重新注册。停用约束作用于该用户及其已绑定身份，不推断不同 OAuth 身份是否属于同一自然人。未经明确关联的新身份按自动注册规则获得全新空账本，不能访问任何旧用户的数据，也不能自行获得管理员角色。

设备应保留本次登录对应的 identity 关联，身份必须与设备的 user 一致。刷新凭据沿用该关联。新库管理员初始化和旧库归属输入由离线入口接收，复用既有部署配置；不得新建公开 bootstrap HTTP 入口。

### 4.4 长连接、后台任务和共享状态

- 停用操作与业务写入通过同一写协调器排序。停用提交后，排队任务实际开始执行及后续工具调用必须拒绝该用户；此前已提交的账本事务不回滚。
- 撤销该用户全部访问与刷新凭据，关闭其活动 WebSocket，终止或标记取消该用户的 Agent 回合。已发出的外部请求不能保证撤回，但返回后不得继续投递数据或触发下一次写入。
- 恢复用户后要求重新 OAuth 登录，不复活旧令牌；已有身份仍映射到原 userId，不新建用户或重置账本。
- 行情 provider 凭据、公开报价缓存和供应商预算保持部署级共享；按当前用户读取来源设置快照，显式传给报价调用，禁止每个请求覆盖同一 runtime 的全局路由。
- 用户数据的缓存、幂等返回、事件重放、游标、设置 revision 和前端缓存必须保持归属边界。任何用户数据缓存键都要包含用户或可证明归属于用户的不可变资源标识。
- 公开健康检查只返回部署可用性与版本等必要信息，不暴露全库 ledgerVersion、用户 Gmail 数量或用户活动计数。现有 App 对健康响应的解析和测试同步更新，不填伪造的 0 值维持旧形状。

## 5 代理任务与文件所有权

所有任务状态初始为“待开始”。主代理在派发时复制任务目标、允许改动路径、禁止范围、冻结契约和验收条目；不转交整段聊天。子代理只在自己负责的文件和对应测试中写入，需要修改其他模块时先向主代理报告。

主代理独占公共接线与契约文件：[main.py](/Users/Gin/Workspace/Lyra/server/src/lyra/main.py)、[config.py](/Users/Gin/Workspace/Lyra/server/src/lyra/config.py)、[protocol.py](/Users/Gin/Workspace/Lyra/server/src/lyra/protocol.py)、[pyproject.toml](/Users/Gin/Workspace/Lyra/server/pyproject.toml)、[路由清单](/Users/Gin/Workspace/Lyra/server/tests/fixtures/route-manifest.json)、共享测试夹具、本计划和部署文档。子代理提供精确接线需求，主代理在对应波次集成。

用户与身份仓储（`users` / `user_identities`）由 **P1a 独占**实现，P0 冻结其完整方法签名；P2 及后续模块只调用不修改。P2 需要新增方法时向主代理报告，由主代理在 P1a 的文件上增补，禁止 P2 自行改写该仓储——这是 P1a 与 P2 之间唯一的写冲突点，必须在派发前钉死。迁移 SQL 只由 P1a 修改，迁移执行代码与离线入口只由 P1b 修改；新增共享 AuthContext 的具体文件和接口在 P0 指定并由 P2 落地。

### P0 主代理冻结契约和基线

依赖：无。权限：只读检查；可创建临时合成数据库做定向验证，不打开真实数据。

- 确认当前未提交改动与本计划基线是否一致，记录需保留的修改，避免重做上一轮 review 修复。
- 冻结第 4 节的字段、错误码、仓储构造参数、工具上下文和响应形状；生成供服务端与 Flutter 共用的合成契约样本。
- 冻结用户与身份仓储 `UserRepository` 的完整方法签名（按 `(provider, subject)` 查找、原子创建用户与身份、读取用户、更新用户状态、受限分页列表、初始化首个管理员），并确认其文件归属为 P1a。签名冻结前不得放行 P1a 或 P2。
- 冻结「用户当前账本版本」的显式定义：`max(version) WHERE user_id = ?`，空账本为 0。明确 `ledger_version_seq` 仍为全局序列，因此单个用户的版本号**不连续**（A 可能看到 1→3）。已核实 Flutter 只用等值比较（[market_workspace.dart:170](/Users/Gin/Workspace/Lyra/apps/mobile/lib/features/market/market_workspace.dart#L170)），无 `+1` 或计数假设，故不连续是安全的；契约中须写明禁止任何模块改回全局 `max(version)`。
- 冻结公开健康响应的目标字段清单。当前 `/v1/health` 返回 `gmail.totalConnections`、`attentionRequired` 与 `agents` 计数（[health.py:108](/Users/Gin/Workspace/Lyra/server/src/lyra/api/health.py#L108)），§4.4 只规定"不暴露用户 Gmail 数量、不填伪造 0 值"，未给出保留清单。须逐段确认 `process` / `database` / `writeQueue` / `gmail` / `agents` 各字段在多用户下的取舍（保留、移除或改为部署级聚合），形成 P8 的可执行改造清单。
- 定向验证 DuckDB 父子外键更新、表重建与事务回滚；确定 users 的最终物理结构以及身份与设备关联方式。
- 列出所有 REST/WS 入口、后台写入口和公开健康字段，标记归属要求；作为 P8 的覆盖清单。
- 确认当前 schema/协议、Python/Flutter 测试入口及可用 SDK；仅在需要时重跑当前基线，保留真实结果。沙箱下 `uv` 与 `flutter` 的已知限制见第 8 节。

完成条件：公共契约无待定字段，P1a 能据此建表并落地用户仓储，P2/P3/P7 能据此独立实现。

### P1a 物理 schema 与用户仓储代理

依赖：P0。可改：新增迁移脚本 `/Users/Gin/Workspace/Lyra/server/src/lyra/db/sql/multi-user-schema.sql`、[migrations.py](/Users/Gin/Workspace/Lyra/server/src/lyra/db/migrations.py) 的迁移清单追加（只追加第 16 条及其常量，不动历史条目）、新增用户与身份仓储 `/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/user_repository.py`、约束验证测试。不得修改历史 SQL 内容、既有业务仓储或生产数据。

本代理是 P2 的唯一上游，必须先于 P2 完成：P2 的自动注册与登录事务直接依赖这里落地的 `UserRepository`。

- 实现第 4 节全部表归属、唯一约束、外键与索引；按外键依赖重建所需表和视图，保留既有金额、顺序、交易哈希、消息和来源记录。
- 实现 P0 冻结的 `UserRepository` 完整接口，含原子创建「用户 + 身份」与首个管理员初始化所需的方法。仓储在构造时要求明确 `user_id` 或明确的身份参数，禁止以 `None` 表示全部用户。
- 在有子记录的情况下实际更新用户、设备、账户与 Gmail 连接的父行，给出当前锁定 DuckDB 版本下的实测结论；若受外键约束限制，采用 P0 冻结的「不可变身份表 + 可变状态表」模型，不得自行移除完整性校验或另选一种模型。
- 为全新库与冻结 schema 15 库副本提供建表、约束与状态更新测试。
- 校验每个子资源与父资源归属一致；重启不得重复迁移或重新生成用户。

完成条件：schema 16 物理结构冻结，约束与状态更新测试通过，`UserRepository` 接口可用且签名与 P0 冻结结果一致。P2 据此即可开始，无需等待 P1b。

### P1b 数据迁移与离线入口代理

依赖：P1a。可改：新增离线入口 `/Users/Gin/Workspace/Lyra/server/src/lyra/admin.py`、新增离线入口测试。**不得修改 `migrations.py`、`multi-user-schema.sql`、`user_repository.py`、历史 SQL 内容、业务仓储或生产数据**——迁移清单、迁移 SQL 与「缺少归属即明确失败」的校验都在 P1a 的文件里，P1b 只构造迁移输入并调用既有入口；需要改动 P1a 的文件时向主代理报告，由主代理在 P1a 文件上增补。

- 实现显式管理员初始化及原 owner 迁移输入；必要的迁移上下文只服务于一次数据升级，不成为长期 owner 兼容模式。初始化入口不得打印用户 token，不得读取隐含的「首个请求」作为管理员凭据。
- 为全新库、冻结 schema 15 库、缺 owner 信息的旧库、故障注入回滚、迁移后再次启动提供测试。
- 迁移成功后删除运行时不再使用的 owner 身份表；保留历史迁移文件。缺少明确归属时启动必须明确失败，不能分配给首次登录者。

完成条件：新库与旧库副本迁移通过；记录数、Decimal 汇总、去重键、FIFO、消息及 Gmail 密文完整；用户和设备状态可更新；失败后旧数据可重新打开。

### P2 身份与自动注册代理

依赖：P1a；按 P0 冻结接口与 P1b、P7 并行（P3 在其后一波次，其验收依赖本代理完成）。可改：[auth_routes.py](/Users/Gin/Workspace/Lyra/server/src/lyra/api/auth_routes.py)、[dependencies.py](/Users/Gin/Workspace/Lyra/server/src/lyra/api/dependencies.py)、[device_auth.py](/Users/Gin/Workspace/Lyra/server/src/lyra/security/device_auth.py)、[设备认证器](/Users/Gin/Workspace/Lyra/server/src/lyra/application/device_authenticator.py)、[设备签发服务](/Users/Gin/Workspace/Lyra/server/src/lyra/application/device_pairing_service.py)、[设备仓储](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/device_repository.py)、现有 owner 登录服务及来源配置模块、对应测试。可新增自动注册服务和管理路由。**不得修改 `user_repository.py`**——该文件由 P1a 独占，P2 只调用；需要新方法时向主代理报告。重命名旧 owner 模块时列出所有调用方，由主代理协调其他模块引用。

- 实现按 `(provider, subject)` 查找或原子创建 user/identity、用户状态管理、`require_user` 与 `require_admin`；所有自动注册用户固定为 `member`。
- 将 owner 白名单替换为首次登录自动注册与已有用户状态校验；provider 可用性取决于 OAuth 配置，不再要求单 owner 的 subject 配置。旧配置退出运行时鉴权，迁移输入单独显式处理。
- 令牌认证、刷新和设备签发都验证用户；设备不能通过公钥或重复登录转移归属。
- 提供停用后的设备列表与「该用户已停用」查询、通知接口；WS 与 Agent 侧的级联由 P4 实现，Gmail 后台同步侧的级联由 P5 实现，主代理只做接线。不得仅更新状态字段就报告停用完成。
- 保持 OAuth state/PKCE/nonce、刷新轮换、重放检测、撤销和 no-store 行为；更新相关正向与反向测试。

完成条件：三来源的新身份均能自动注册并获得空账本；再次登录和同身份并发登录保持同一 userId；未完成验证时不创建用户；同邮箱不合并；停用身份不能重新注册，登录和刷新均被拒；最后管理员保护有效。

### P3 账本与导入代理

依赖：P1a、P0 契约；**验收依赖 P2 完成**。可改：[ledger_routes.py](/Users/Gin/Workspace/Lyra/server/src/lyra/api/ledger_routes.py)、[import_routes.py](/Users/Gin/Workspace/Lyra/server/src/lyra/api/import_routes.py)、账户服务与账户查找/仓储、账本仓储、导入应用服务及导入仓储、相应测试。`ledger_routes.py` 中认证器装配的改动也由本代理接收 P2 的接口说明后完成，避免多人写同一文件。

- 所有账户 CRUD、分析、分页、来源详情、提醒与导入操作使用显式用户范围。
- 先按用户查导入作业，再校验预览令牌和幂等；上传尚未绑定账户时也有 owner。暂存文件的读取和删除必须经过作业授权。
- 账本版本统一按当前用户读取，快照与版本在同一短事务中获取；写入版本关联正确用户，B 的写入不得使 A 预览失效。
- 账户键变为用户内唯一后，逐个修复 SQL/映射查询，验证同键跨用户不会混入解析、去重、FIFO 或汇总。
- 同用户并发确认同一预览仍沿用既有冲突/幂等语义；账本写入和用户停用遵循同一写协调器排序。

完成条件：A/B 使用相同账户标识与相同文件可各自导入；各自 `scope=all`、版本、明细和分页正确；串改 importId/accountId/token 不能读写他人资源；现有财务计算回归通过。

验收门槛：本代理与 P2 同波次启动，但 P2 的真实鉴权与 `require_user` 集成完成前，只能以桩验证推进开发，**不得报告任何隔离验收通过**，也不得以桩测试充当 V03–V06 的证据。第 6 节波次表已按此顺序排列。

### P4 Agent 隔离代理

依赖：P2/P3 接口已集成。可改：[Agent 路由](/Users/Gin/Workspace/Lyra/server/src/lyra/api/agent_routes.py)、[事件路由](/Users/Gin/Workspace/Lyra/server/src/lyra/api/agent_events.py)、[编排器](/Users/Gin/Workspace/Lyra/server/src/lyra/application/agent_turn_orchestrator.py)、[金融工具装配](/Users/Gin/Workspace/Lyra/server/src/lyra/application/financial_tool_definitions.py)、[Agent 领域代码](/Users/Gin/Workspace/Lyra/server/src/lyra/domain/agent)、Agent 仓储、运行时接入及对应测试。不得修改账本仓储、schema 或行情模块。

- 创建、读取、历史、发送、取消、关闭、工具审计和 WS 订阅/重放均验证会话 user。
- userId 由服务端 session 身份注入工具上下文；禁止模型指定用户，`all` 限于该用户所有账户。
- 保留历史上下文、线程池模型执行、事件顺序、原子订阅重放与取消语义。
- **本代理负责停用级联中 Agent 与 WS 侧的完整实现**：停用后禁止继续工具执行与事件投递，关闭该用户的活动 WebSocket、清理其订阅与在飞回合、标记取消正在执行的模型回调，并保证已发出的外部请求返回后不再投递数据或触发下一次写入。不要因原创建设备撤销而把用户历史变成他人可访问资源。P2 只提供「该用户已停用」的查询接口与通知，P8 只做接线与验收，不承担级联逻辑。停用与业务写入经同一写协调器排序，停用提交后排队任务开始执行即拒绝。

完成条件：A 不能读取、取消或订阅 B 的 session；伪造工具参数不能扩大范围；同用户另一设备可读自己历史；停用中的长连接和模型回调不继续泄露数据。

### P5 Gmail 隔离代理

依赖：P2/P3 接口已集成。可改：[Gmail 路由](/Users/Gin/Workspace/Lyra/server/src/lyra/api/gmail_routes.py)、Gmail 应用服务、[Gmail 基础设施](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/gmail)、Gmail 连接与消息仓储及对应测试。不得改 schema、共享 AuthContext 或账本仓储。

- OAuth 事务、连接列表、映射、同步、断开全部保留用户归属；连接不能通过 Google subject 的全局 upsert 被其他用户覆盖。
- 映射和消息入账时同时检查 connection/account/transaction 归属，复用 P3 的用户账本写入和版本接口。
- 后台增量同步携带 userId，入队与实际执行检查活动状态；用户停用后停止该用户任务。
- **本代理负责停用级联中 Gmail 后台同步侧的完整实现**：停用提交后，该用户已入队的同步任务在实际开始执行时必须拒绝，不得入账，也不得继续拉取或写回消息。P8 只做接线与验收。
- 保持既有密文存储、邮件解析与丢弃正文原则，不向日志或 Agent 泄露 Gmail token 和邮件正文。

完成条件：A 无法列出、同步、断开 B 的连接；跨用户账户映射失败且无部分写入；用户间相同邮件标识不互相去重；停用后的排队同步不能入账。

### P6 行情设置隔离代理

依赖：P2/P3 接口已集成；与 P3 同波次时先按 P0 冻结签名开发，账户 UUID 映射的最终验证在 P3 集成后进行。可改：[行情路由](/Users/Gin/Workspace/Lyra/server/src/lyra/api/market_routes.py)、[行情服务](/Users/Gin/Workspace/Lyra/server/src/lyra/application/market_data_service.py)、[行情运行时](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/market_data/runtime.py)、[设置仓储](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/market_settings_repository.py)及对应测试。不得改 Agent 工具文件；向 P4 提供显式用户设置快照调用契约。

- 设置保存、读取、revision 冲突检测均按用户处理，启动不再把某个用户设置装成全局运行时偏好。
- HTTP 和 Agent 使用同一套“当前用户设置快照 + 共享报价服务”接口；缓存仍按公开报价身份复用。
- 保留已有币种分组、缺价/过期表达、账户 UUID 映射、短事务快照及网络请求在事务外的行为。

完成条件：A/B 并发采用不同来源时 HTTP 与 Agent 各自得到正确来源；A 设置更新不改变 B revision；供应商全局预算仍生效；用户持仓不会出现在共享缓存值中。

### P7 Flutter 账号适配代理

依赖：P0 响应契约，可与 P2、P1b 并行（P3 在其后一波次）；真实服务端契约验收依赖 P2 至 P6。可改：[移动端 lib](/Users/Gin/Workspace/Lyra/apps/mobile/lib)、[移动端 test](/Users/Gin/Workspace/Lyra/apps/mobile/test)。不得修改服务端、依赖锁文件或重新设计现有登录页面。

- 在登录响应、安全存储和会话模型中加入稳定 userId；协议更新为 3，严格解析新身份字段。
- 复用现有三种 OAuth 按钮，登录页说明“首次登录将自动创建账号”；新用户完成授权后直接进入空账本与既有首次导入流程，无独立注册表单。补充禁用或注册事务失败的明确反馈，保留取消、超时、重试和浏览器回跳行为。
- 偏好统一按 `normalizedServerUrl + userId`；账户范围、Agent session 与缓存按该身份隔离。旧凭据缺 userId 时清理会话并要求登录，不能按邮箱猜归属。
- 切换账号时取消旧请求、关闭 WS、失效旧 client、清理财务页面和 Agent session；迟到响应不能恢复上一用户状态。
- `/me` 用于核对当前用户身份；保持现有明确的离线/网络错误处理，不把网络失败伪装成用户已被停用。
- 普通用户设置和资料不展示管理员专用能力；首版管理 API 不需要完整 Flutter 管理后台。

完成条件：新身份首次登录直接进入独立空账本；同一身份再次登录返回原账本；A→退出→B 的缓存、页面、偏好和迟到响应隔离；同用户不同 OAuth 身份在明确关联后使用同一偏好键；旧协议被清楚拒绝；真实后端 JSON 可由生产解析器读取。

### P8 主代理集成与最终审核

依赖：P1a/P1b 与 P2 至 P7 完成。主代理接入所有公共文件、管理路由、初始化入口、停用回调接线和健康响应；更新路由清单、预检脚本及部署文档中的当前版本约束，保留历史执行记录的历史含义。

**停用级联的实现责任在 P4（WS 与 Agent）与 P5（Gmail 后台同步）**，P2 提供停用查询与通知接口。P8 只负责把三者的接口接起来并验证端到端行为，不在此重写级联逻辑——避免把最复杂的跨模块时序压到同时承担全量验收与审核的波次上。

- 逐项对照 P0 入口清单，覆盖 REST、WS、后台任务、审计、设置、游标和幂等重放；不能仅检查是否出现 `user_id` 字符串。
- 检查没有绕开用户范围的生产调用方，没有测试专用的默认 owner，也没有移除原有隔离/精度断言来通过测试。
- 核对各代理差异和测试证据；可将独立事实核对委派为只读审核，最终结论由主代理作出。
- 执行第 7 节矩阵与第 8 节验证；完成升级和恢复手册，记录真实结果与未完成的外部联调。

完成条件：所有发布门槛有证据，所有相关检查通过，真实环境未验证事项单独列明；未执行生产部署时不得写“已上线”。

## 6 调度与交接规则

| 波次 | 任务 | 放行条件 |
| --- | --- | --- |
| 0 | P0 主代理 | 契约、归属设计与迁移约束确定 |
| 1 | P1a 物理 schema 与用户仓储 | 约束与状态更新测试通过，schema 16 冻结，`UserRepository` 可用 |
| 2 | P1b 迁移与离线入口、P2 身份与自动注册、P7 Flutter 并行 | 每个代理只写自己的文件；P2 只调用 P1a 的用户仓储，不改写该文件 |
| 3 | P3 账本导入、P6 行情并行 | P2 的真实鉴权已集成；P3 的验收不得早于 P2 完成 |
| 4 | P4 Agent、P5 Gmail 并行 | 依赖 P3 的用户账本写入与版本接口、P6 的设置快照契约 |
| 5 | P8 主代理 | 全链路集成与验收完成 |

P1 拆为 P1a/P1b 的理由：原 P1 独占 schema 重建、离线初始化、五类迁移测试与 owner 表删除，而 P2–P6 全部依赖它，是整条关键路径上唯一的单点。拆开后 P1a 一完成即放行 P2/P7，P1b 的数据迁移与离线入口不在 P2–P6 的开发关键路径上。原「波次 2 三路并行」并不成立——P3 的验收被同波次的 P2 卡住，故 P3 移到波次 3；P4/P5 均依赖 P3 的账本写入与版本接口，故移到波次 4。

最多同时运行三个子代理，为主代理保留协调槽位。公共文件修改顺序由主代理管理；下游可以使用冻结接口开发，但不能把桩测试当作真实鉴权集成通过。

每个代理交付必须包含：任务状态、实际改动文件、保持的原业务行为、接口变化、执行过的测试命令与结果、未完成事项，以及需要主代理接线的精确位置。失败或阻塞必须明确报告；主代理接手或重新分配，不声称未执行的验证已经通过。

## 7 必须通过的验收矩阵

使用合成管理员 M、普通用户 A/B，A 的两台设备 A1/A2，以及两组相同证券账户标识和相同文件。所有“拒绝”断言同时检查响应、数据库和后台副作用。

| 编号 | 场景 | 必须结果 |
| --- | --- | --- |
| V01 | 三种 OAuth 首次登录、无邮箱、state/PKCE/nonce 错误、授权取消、同邮箱不同身份 | 有效新身份自动注册 member 并获得空账本；无邮箱可注册；验证失败或取消不创建用户；不同身份不自动合并 |
| V02 | 同身份再次登录、两个独立登录事务并发完成、同登录事务重放、注册事务失败后重试 | 同身份始终对应一个 userId；设备各自归属正确；重放被拒；失败事务无残留且重试不重复注册 |
| V03 | A/B `scope=all`、相同账户标识、直接传他人 accountId | 汇总仅含本人；标识可共存；越权不可读写 |
| V04 | 同文件分别导入、跨用户 importId/预览 token/幂等重放 | 各自正确去重；他人作业不可读取、提交或取消 |
| V05 | A 生成预览后 B 提交；A 自己同时提交 | B 不影响 A 版本；A 自身版本冲突按既有规则检测 |
| V06 | 分页、来源追溯、提醒、审计及其他资源 ID 替换 | 不出现其他用户内容；错误与无资源一致 |
| V07 | A 访问/发送/取消 B Agent，会话 WS 订阅与重放 | HTTP 和 WS 均拒绝，历史与实时事件都不泄露 |
| V08 | 模型伪造 userId/account scope，A2 访问 A1 历史 | 模型不能扩大授权；同用户跨设备正常 |
| V09 | Gmail 跨用户列表/映射/同步/断开、同邮箱再次连接 | 不覆盖他人连接或 token；跨归属映射不落库 |
| V10 | A/B 不同行情来源，并发保存与 Agent 查询 | 各自来源与 revision 正确，公共报价缓存可复用 |
| V11 | 停用时刷新、排队导入、邮件同步、模型回调和 WS 推送 | 停用提交后不再授权新操作；凭据和活动连接失效 |
| V12 | 自动注册权限、停用身份再次 OAuth、普通用户调用管理 API、最后管理员停用、用户恢复 | 自动注册只得 member；停用身份不创建新用户；权限拒绝或冲突明确；恢复沿用原 userId 且不复活旧凭据 |
| V13 | Flutter 首次 OAuth、同身份再次登录、A→B、迟到响应、App 重启、旧凭据 | 首次进入空账本，再次进入原账本；身份与缓存不混用；旧凭据要求重新登录 |
| V14 | 全新库、schema 15 副本升级、第二次启动、中途失败 | 归属明确、数据不变、迁移不重做、失败可恢复 |
| V15 | 原 owner 多来源身份、迁移后的陌生 OAuth 身份、无 owner 的非空旧库 | 已确认身份映射到原用户；新身份只获得空账本；旧库归属不明确时明确失败 |
| V16 | 协议 2 请求新服务、协议 3 新客户端、公开健康接口 | 版本门槛生效；公开接口不泄露用户数据与活动计数 |
| V17 | A/B 并发读账本、导入和长时间外部请求 | 无连接结果串线/死锁；网络请求不占长数据库事务；记录排队耗时 |

## 8 验证命令与证据

每个代理先运行负责模块的定向测试；主代理在最终接线完成后运行全量。下列命令是实施阶段入口，不代表本次计划编写时已执行。

服务端，在 `/Users/Gin/Workspace/Lyra/server` 执行：

```bash
uv run pytest -q
uv run ruff check .
```

Flutter，在 `/Users/Gin/Workspace/Lyra/apps/mobile` 执行：

```bash
flutter analyze --no-pub
flutter test --no-pub --reporter expanded --timeout 60s --concurrency=2
```

如果 SDK 不在 PATH，使用环境中已确认的 SDK 路径，不猜安装目录。需要 Android 构建时先按 android-cli 指令准备工具。依赖缺失或外网失败必须按项目代理规范处理并记录，不能省略检查或编造通过结果。

2026-10-16 评审时实测的环境限制（执行前先按此处理，不要当成失败结论）：

- `uv run` 默认缓存目录 `~/.cache/uv` 在沙箱下不可写，报 `Failed to initialize cache at /Users/Gin/.cache/uv`。**项目已有约定**：`server/scripts/` 下的脚本统一使用 `UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python`（见 [preflight_cutover.py:9](/Users/Gin/Workspace/Lyra/server/scripts/preflight_cutover.py#L9)），且 [preflight_cutover.py:78](/Users/Gin/Workspace/Lyra/server/scripts/preflight_cutover.py#L78) 已记录该问题会伪装成「接管既有账本未通过」。按既有约定设置即可，实测解析到 `duckdb 1.5.6`，与 `uv.lock` 一致；**不要另建缓存目录**。P0 的 DuckDB 定向验证必须使用这一设置。
- `flutter` 位于 `/Users/Gin/.flutter-sdk/bin/flutter`。**已实测确认**：它每次运行都执行 `bin/internal/update_engine_version.sh`，无条件写 `$FLUTTER_ROOT/bin/cache/engine.stamp` 与 `engine.realm`。在只允许写工作区的文件策略下，`flutter analyze` 会以退出码 1 失败且**只打印两行 `Operation not permitted`**，看起来像代码问题，实际是权限问题。P7/P8 必须在能写 Flutter SDK 缓存目录的策略下运行 Flutter 命令。放宽后实测基线：`flutter analyze` 无问题，`flutter test` **262 passed, 5 skipped**，退出码 0。
- 本机没有 `timeout` 命令（macOS 默认），脚本中不要依赖它。

另外必须完成：

- 通过真实 FastAPI 测试客户端生成登录、`/me`、账本、导入、行情响应，再交给 Flutter 的生产模型解析；避免两边分别手写样本掩盖协议漂移。
- 用冻结 schema 15 测试数据库的临时副本验证迁移，原夹具保持不变。重启再读取；比较记录数、金额/数量汇总、FIFO 与消息内容及归属。
- 对有子记录的用户、设备、账户和 Gmail 连接实际更新状态，覆盖 DuckDB 外键行为。
- 更新 [预检脚本](/Users/Gin/Workspace/Lyra/server/scripts/preflight_cutover.py) 的当前版本/路由检查与对应测试，清楚区分语言切换历史事实与本次账号升级门槛。包含真实部署配置检查的步骤仅在部署阶段运行。
- 记录实际命令、工作目录、退出码、通过/失败/跳过数量和日志位置。上一轮测试通过数量不是本次验收证据。

## 9 数据升级与恢复

实施阶段只在合成库和测试夹具副本操作。实际部署按以下顺序执行，并记录原 owner 的明确映射及目标用户 UUID：

1. 完成全量验收，准备协议 3 客户端和新服务端；停止接受写入，排空或明确取消后台任务，停止唯一数据库写进程。
2. 使用既有部署方式备份数据库、必要的 WAL/相关持久文件和加密所需配置，保护密钥；在副本上验证可以恢复，不输出敏感内容。
3. 对旧 owner 的各来源身份做显式确认后映射到同一原用户。owner 身份表只是已登录记录，不能不经核对就把每个历史身份都当成旧账本的永久授权；未确认的身份不关联原用户，后续按自动注册规则只能得到独立空账本。非空库无法确定 owner 时拒绝迁移。
4. 用离线升级入口在一个受控迁移事务中升级 schema，将旧资源全部归给明确的原用户，撤销旧凭据。未完成的导入预览和运行中的 Agent 回合明确终止并要求重试，历史记录保留。
5. 运行数据完整性和归属校验，提交后 CHECKPOINT；重新打开库验证，再启动新服务。
6. 在受限访问范围内让原用户重新 OAuth 登录，验证账本、Gmail 和历史；使用合成或明确授权的测试用户验证自动注册与隔离后，再对用户开放服务。
7. 升级失败保持服务关闭并报告原因。回退使用旧二进制与完整迁移前备份，不让旧程序打开 schema 16，也不只恢复部分表。新版本已接收写入后不能直接覆盖旧备份，须先保存新增数据并制定恢复方案。

新安装同样要求显式指定首个管理员的 `(provider, subject)`；第三方 OAuth 仍负责验证实际登录身份。初始化入口不应打印用户 token，不读取隐含的“首个请求”作为管理员凭据。

## 10 完成定义与执行记录

全部 P0、P1a、P1b、P2 至 P8 完成、V01 至 V17 有测试或验收证据、协议与迁移同步、无未解决的越权路径，才可标记实现完成。生产 OAuth、真实模型/Gmail、Android 真机及生产部署若未执行，分别记录为待联调或待部署，不影响对本地实现事实的准确报告。

工作量参考：一名熟悉项目的工程师约 20–30 人日，已按复用现有 OAuth 计算，包含迁移、客户端与隔离回归；这是规划估算，不是测试结果或完成承诺。

2026-10-16 评审将原估算 12–20 人日上调：原估算未充分计入 P1 的 schema 重建与外键约束验证（实测 DuckDB 版本 1.5.6，父子外键更新行为需先定向确认）、6 个服务端模块的隔离穿透加真实鉴权集成，以及 P8 同时承担全量验收与 17 条矩阵的成本。P1a/P1b 与 P8 是最可能超期的三项。

| 任务 | 状态 | 交付与证据 |
| --- | --- | --- |
| P0 公共契约与基线 | 已完成（2026-10-16） | 契约冻结与 DuckDB 定向验证见 [P0 冻结契约](lyra-multi-user-ledger-p0-contract-20261016.md)；复现脚本 [assets/p0/](assets/p0)。基线：工作区干净、schema 15、协议 2、DuckDB 1.5.6、48 项路由。决定性结论：`ADD COLUMN NOT NULL` 与 `ADD/DROP CONSTRAINT` 在当前版本均不支持、被引用父表不可改名，故 schema 16 必须整体重建；`users` 保持单表可变（父表非键列更新在 4 类子引用下实测通过），无需拆表 |
| P1a 物理 schema 与用户仓储 | 已完成（2026-10-16） | 交付：新增 [multi-user-schema.sql](/Users/Gin/Workspace/Lyra/server/src/lyra/db/sql/multi-user-schema.sql)（迁移 16，整体重建）、[user_repository.py](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/user_repository.py)、[migrations.py](/Users/Gin/Workspace/Lyra/server/src/lyra/db/migrations.py) 追加 `MigrationContext` 与清单第 16 条、新增 `tests/test_multi_user_schema.py`（30 用例）与 `tests/test_user_repository.py`、扩展 `tests/test_migrations.py`。验证：`pytest tests/test_multi_user_schema.py tests/test_user_repository.py tests/test_migrations.py -q` → **63 passed**；`ruff check .` → 全部通过；主代理独立验证全新库迁移后 schema 16、30 张表、6 处 `UNIQUE (user_id, id)` 父侧键齐备、重复迁移幂等。**子代理在交付报告阶段失败，成果由主代理接手核验** |
| P1b 数据迁移与离线入口 | 已完成（2026-10-16） | 交付：新增 [admin.py](/Users/Gin/Workspace/Lyra/server/src/lyra/admin.py)（离线 CLI，`python -m lyra.admin`）与 `tests/test_admin_bootstrap.py`（20 用例）。CLI 参数见 P0 契约 §3.10；旧库升级用 `--owner-identity` 显式确认归属，未列出的历史身份不关联原用户。验证：`pytest tests/test_admin_bootstrap.py tests/test_migrations.py tests/test_multi_user_schema.py tests/test_user_repository.py -q` → **83 passed**；`ruff check .` 通过；子代理另跑了真实 CLI 冒烟（新安装/重复执行/旧库升级/重复升级四条路径的退出码与输出均已记录）。**主代理修正一处与计划的偏差**：迁移 16 原本不删旧 owner 身份表，只有离线入口的升级路径删，导致「直接 `open_lyra_database` 建的新库」与「升级库」表集合不一致。已把 `DROP TABLE IF EXISTS server_owner_identity` 放进迁移 16，并把离线入口的「未确认身份」读取提到迁移**之前**（否则删表后读不回来，操作者会失去核对依据） |
| P2 身份与自动注册 | 已完成（2026-10-16） | 交付：新增 [auth_context.py](/Users/Gin/Workspace/Lyra/server/src/lyra/security/auth_context.py) 与 `tests/test_auth_context.py`；`dependencies.py` 加 `require_user`/`require_admin` 并**删除** `require_device` 与 `request.state.device`（无兼容路径）；`auth_routes.py` 加 `/v1/me`、`GET /v1/admin/users`、`PATCH /v1/admin/users/{userId}` 与停用级联；`device_auth.py`/`device_authenticator.py`/`device_pairing_service.py`/`owner_login_service.py`/`owner_login_providers.py`/`device_repository.py` 与 3 个来源配置模块改造。验证：`pytest test_auth_routes test_auth_context test_device_repository -q` → **116 passed**；`ruff check .` 通过。P2 在实施中发现并上报了 `main.py` 的**基线启动期崩溃**（见 10.2）与 F8 契约细节 |
| P3 账本与导入 | 已完成（2026-10-16） | 交付：生产 8 个文件（`account_lookup` / `account_repository` / `ledger_repository` / `import_job_repository` / `import_commit_repository` / `account_service` / `api/ledger_routes` / `api/import_routes`）+ 测试 9 个（含**新增** [test_ledger_import_isolation.py](/Users/Gin/Workspace/Lyra/server/tests/test_ledger_import_isolation.py)，9 条 A/B 端到端隔离）。`resolve_legacy_account_key`、`load_brokerage_account`、`DuckDbLedgerRepository`/`DuckDbImportCommitRepository` 构造期均改为 **keyword-only 必填 `user_id`**——漏传是 `TypeError` 而不是静默全库。账本版本判据改为 `coalesce(max(version),0) WHERE user_id=?`。验证（主代理独立复现）：`pytest` 上述 9 个文件 → **152 passed**（退出码 0）；`ruff check .` 通过。**P3 顺带修掉 P2 遗留的第二处静默回归**：`import_routes._device()` 仍读已删除的 `request.state.device`，恒返回 `None` 导致 `_plan_idempotency` 返回 `None`——**提交幂等被静默关闭**。主代理已全仓排查 `state.device`/`require_device`，确认无其它功能性残留 |
| P4 Agent | 已完成（2026-10-16） | 交付：`domain/agent/tool_call.py`（`AgentToolContext` 新增必填 `user_id`）、`api/agent_routes.py`、`api/agent_events.py`、`agent_turn_orchestrator.py`、`agent_event_buffer.py`、`financial_tool_definitions.py`、`financial_tool_handlers.py`、`public_projection.py`（仅 `_market_status` 一处）、`agent_session_repository.py` + 9 个测试文件。验证（主代理独立复现）：`pytest <10 个 P4 文件> -q` → **181 passed**；`ruff check src tests` 通过。主代理另核了两处关键实现：**WS 订阅归属校验**在 `subscribe_with_resume` **之前**执行、复用 `load_session_for_user`（避免 HTTP 拒而 WS 放的分叉）、拒绝帧与"不可续传"**完全相同**（不能当存在性探针）；**模型伪造防护**为两层（工具 schema 不含保留字段 + `_authorized_arguments` 使用前再删一次），注释写明"授权边界不能只靠一层 schema" |
| P5 Gmail | 已完成（2026-10-16） | 交付：`gmail_connection_repository.py`、`gmail_message_repository.py`、`gmail_service.py`、**新增** `gmail_user_scope.py`、`gmail/service_factory.py`、`api/gmail_routes.py` + 3 个测试文件（17→27、10→21、43→64）。用户范围经 `service.for_user(user_id)` 注入——**不是给每个方法加参数**，因为服务是单例而"当前用户"是每请求量，做成参数后任一处漏传就**静默**退回全库语义。验证（主代理独立复现）：`pytest test_gmail_connection_repository test_gmail_message_repository test_gmail_routes -q` → **112 passed**（改动前同命令为 43 passed + 29 errors）；`ruff check .` 通过。**P5 修掉了同类第三处潜伏缺陷**：`gmail_routes.py` 的 `_call(_service(request).sync, ...)` 在请求期解析属性，而 `GmailService.sync` 在生产中**根本不存在** → 每次同步 500。主代理已用 `git show HEAD` 确认基线即如此 |
| P6 行情 | 已完成（2026-10-16） | 交付：`api/market_routes.py`、`infrastructure/market_data/runtime.py`、`infrastructure/db/market_settings_repository.py`、`application/market_data_service.py`、`market_budget.py`（仅 docstring）+ 3 个测试文件。**删除** `MarketDataRuntime._routes`/`_settings_revision`/`_settings_lock`/`initialize_settings` 与 `MarketDataService.set_routes`；新增 `settings_snapshot(connection, user_id)`，每次请求读库、不缓存不写回；`read_quote(instrument, route)` 把路由变成调用参数。验证（主代理独立复现）：`pytest tests/test_market_routes.py tests/test_market_runtime.py tests/test_market_data_service.py -q` → **59 passed**；`pytest tests/test_market*.py -q` → **222 passed**；`ruff check .` 通过。**主代理逐行核对两处越权测试改写**（`test_market_runtime.py`、`test_market_data_service.py`）：均为**更强**的断言，非削弱——前者新增按用户隔离验证与 `hasattr(runtime, "initialize_settings") is False` 的回归守卫，后者新增「第二次调用确实打到新来源」（`catalog.calls == ["primary","secondary"]`） |
| P7 Flutter | 已完成（2026-10-16） | 交付：`apps/mobile/lib` 7 改 1 新（新增 `features/auth/user_profile.dart`）、`apps/mobile/test` 19 个文件。协议升 3；`CredentialSnapshot.userId` 持久化；`preferencesIdentityKey` 只按 `normalizedServerUrl + userId` 且**删除** authMethod/subject/device 全部兜底分支；旧凭据在建立 client 前即清理会话并要求重登，不发网络请求。验证（主代理独立重跑）：`flutter analyze --no-pub` → No issues found；`flutter test --no-pub --reporter expanded --timeout 60s --concurrency=2` → **273 passed, 5 skipped, 0 failed**（基线 262/5，净增 11 个用例）。未改 `pubspec.yaml`/`pubspec.lock`，未动服务端。遗留：`agent_live_events_test.dart:689` 的时序断言在多次运行中出现过 1 次偶发失败（单文件连跑 5/5 通过，两次全量全绿），未修改该断言 |
| P8 集成与审核 | 已完成（2026-10-16） | 主代理交付：协议升 3、`main.py` 用户解析与设备签发接线、健康响应两轮收口、迁移删旧 owner 表与撤销旧凭据、路由清单 48→51、预检脚本、部署文档（含多用户升级与恢复手册、新安装管理员创建）、[契约样本流水线](/Users/Gin/Workspace/Lyra/contracts/README.md)、`docs/progress.md` 的进度与验证记录、以及**过期命名重命名**。终局证据：服务端 `pytest -q` **2663 passed / 4 skipped / 0 failed**、`ruff check .` 通过；Flutter `analyze` 无问题、`test` **282 passed / 5 skipped**。**未执行**：生产 OAuth、真实 Gmail/模型、Android 真机、生产部署——不得写「已上线」 |

### 10.1 验收矩阵覆盖状态（主代理逐条核对，2026-10-16）

核对方式：直接读测试文件找对应断言，不采信代理自述。**只记已有证据的条目**，其余待对应任务完成。

| 编号 | 状态 | 证据（文件:行） |
| --- | --- | --- |
| V01 | 已覆盖 | `test_auth_routes.py` 的 `test_a_new_identity_registers_as_a_member_with_an_empty_ledger`（自动注册为 member + 空账本）、`test_a_pending_exchange_does_not_create_a_user`、`test_a_denied_authorization_does_not_create_a_user`（未完成验证不建用户）、`test_the_same_email_in_two_providers_does_not_merge_users`（同邮箱不合并） |
| V02 | 已覆盖 | `test_the_same_identity_logs_in_as_the_same_user`（再登录同 userId）、`test_two_independent_logins_converge_to_one_user`（两个独立登录事务收敛）、`test_callback_replay_is_rejected` + `test_exchange_is_single_use`（重放被拒）、`test_transaction_rolls_back_user_and_identity_together` + `test_concurrent_create_converges_to_existing_owner`（失败无残留、并发收敛） |
| V12 | 已覆盖 | `test_a_disabled_identity_cannot_log_in_or_refresh`（停用身份不建新用户且登录/刷新被拒）、`test_admin_users_requires_an_admin` + `test_admin_patch_refuses_role_changes`（权限与角色保护）、`test_admin_patch_protects_the_last_active_admin`（最后管理员）、`test_new_user_never_inherits_role_or_ledger`（自动注册只得 member） |
| V13 | 已覆盖（Flutter） | 主代理独立重跑 `flutter test` → **273 passed, 5 skipped**；P7 报告列出新增 11 个用例覆盖首次登录空账本、同身份再登录、A→B 迟到响应、旧凭据要求重登、`USER_DISABLED` 结束会话 |
| V14 | 已覆盖 | `test_opens_and_migrates_fresh_database`（全新库）、`test_takes_over_typescript_created_database`（schema 15 副本）、`test_restart_does_not_remigrate_or_recreate_users`（第二次启动不重做）、`test_failed_upgrade_rolls_back_to_working_schema15`（中途失败可恢复） |
| V15 | 已覆盖 | `test_upgrade_attributes_every_table_to_confirmed_owner`（已确认身份映射到原用户）、`test_upgrade_without_owner_context_fails_loudly`（非空库无归属则明确失败）、`test_upgrade_rejects_duplicate_identities_in_context` / `test_upgrade_rejects_malformed_owner_user_id`（输入校验）、`test_new_user_never_inherits_role_or_ledger`（陌生新身份只得空账本） |
| V16 | 已覆盖 | `test_client_protocol_gate.py` 的拒绝列表含 `"2"`（旧协议被拒）与 `test_the_426_body_carries_the_supported_range`（区间 3–3）、`test_health.py` 的 `test_the_gmail_section_reports_status_without_user_activity_counts`（公开接口不泄露用户活动计数） |
| V03–V06 | 已覆盖 | **新增** [test_ledger_import_isolation.py](/Users/Gin/Workspace/Lyra/server/tests/test_ledger_import_isolation.py) 的 9 条真实端到端用例（非桩）：A/B 相同 legacy key 共存且同用户重复 409、清单与 `scope=all` 只含本人、**他人账户 ID 与不存在 UUID 返回逐字段相同的 404**、同一文件各自导入互不干扰、跨用户 importId 不可读不可取消、B 持 A 的 importId+token 无法提交（且 A 之后仍能用同一 token 提交，证明暂存文件未被 B 动）、B 写入不使 A 预览失效、A 自身版本冲突仍按既有规则 `409 LEDGER_VERSION_CONFLICT` |
| V07–V08 | 已覆盖 | V07：A 读/审计/取消/关闭/发消息/WS 订阅全拒且历史零泄露、**拒绝帧与「不存在」逐字段一致**、被拒订阅未登记订阅者（`subscriber_count` 佐证）。V08：`all` 只含本人账户、他人账户 UUID 不可绑定、伪造 `userId`/`account` 参数被拒、缺 userId 明确失败、**A2 读 A1 历史正常**（同用户跨设备） |
| V09 | 已覆盖 | A 无法列出/同步/断开 B 的连接（`test_real_service_lists_only_the_authenticated_users_connections` 等 3 条）；跨归属映射失败且无部分写入（`test_cross_user_mapping_writes_nothing`、`test_mapping_into_another_users_connection_writes_nothing`、`test_rebinding_onto_another_users_account_is_a_conflict` 等 5 条）——实现上是把归属判定**写进同一条 SQL**（`INSERT … SELECT … WHERE EXISTS(连接与账户同属该用户) RETURNING id`），没有"先查后插"的窗口；用户间相同邮件标识不互相去重（2 条）；停用后的排队同步不能入账（3 条）；同一 Google 账号两用户独立存储（2 条） |
| V10 | 已覆盖 | `test_settings_and_revisions_are_isolated_per_user`（A 改设置不动 B 的 revision）、`test_concurrent_saves_use_each_users_own_revision`、`test_two_users_get_their_own_market_source`（A/B 各自来源，并断言 `runtime.service._routes` 未被保存改写）、`test_provider_budget_is_shared_across_users`（A 用尽额度后 B 得到 `rate_limited`，证明预算跨用户共享）、`test_shared_quote_cache_is_reused_without_leaking_positions`（缓存按公开报价身份复用，键 `provider\0identifier` **不含** user/account UUID，且不含用户持仓）、`test_positions_cannot_resolve_another_users_account` |
| V11 | 已覆盖 | 认证与连接侧：`test_a_disabled_identity_cannot_log_in_or_refresh`、`test_admin_patch_disables_a_member_and_revokes_its_sessions`、`test_admin_patch_disable_closes_the_members_event_stream`、`test_logout_cancels_the_devices_running_turns`；实现 `revoke_user_devices`（`device_pairing_service.py:334`）撤销该用户**全部**设备与凭据并走写协调器排队，`_close_device_sessions` 逐设备收口（主代理核实设备列表含已撤销设备，故覆盖完整）。Agent 侧（P4）：停用后工具执行前复核 → 403、停用时 WS 被 4001 关闭、回合开始前 `_require_active_user` → 403、`cancel_user_turns` 幂等。Gmail 侧（P5）：`test_gmail_sync_dependencies.py` 逐点验证五个动作在停用后各自拒绝。导入侧（P3）：作业按用户查，停用后 `require_user` 直接 403 |
| V17 | 已覆盖 | 结果串线：`test_database_concurrency.py` 的 `test_query_results_do_not_cross_between_threads`、`test_rollback_cannot_include_another_threads_write`；并发认证读：`test_concurrent_authenticated_fastapi_reads_succeed`；事务异常释放等待者：`test_failed_transaction_end_releases_waiters_and_preserves_errors`。**网络请求不占长数据库事务**：`test_network_fetch_runs_outside_any_database_transaction`（`test_market_routes.py`），判据取自 DuckDB 自身行为——已有事务时 `BEGIN` 抛 `TransactionException`，因此抓取回调里能成功开事务即证明调用点在 `COMMIT` 之后。**已做反向验证**：故意把 `COMMIT` 移到抓取之后，该测试变红；还原后恢复。**记录排队耗时**：`test_queue_wait_is_observable_under_concurrent_writes`——在真实协调器上测「A 的长操作把 B 挡了多久」，同时断言等待确实发生与**有界**（死锁会让它变成超时而非一个略大于慢操作的常数）；没有这个数字就无法区分排队与死锁，两者在客户端看来都是"迟迟不返回" |

### 10.2 主代理侧执行记录（2026-10-16）

协议与 schema 门槛已按 §1.7 抬高，影响面在实施前已核实（见 P0 契约 §7.1）：

| 改动 | 位置 | 验证 |
| --- | --- | --- |
| 协议升 3，最低支持同步抬到 3 | `protocol.py`、`preflight_cutover.py`、`test_api.py`、`test_client_protocol_gate.py`、`test_preflight_script.py` | `pytest tests/test_api.py tests/test_preflight_script.py -q` → **13 passed**；`ruff check .` 通过 |
| 旧协议必须被拒（V16 的断言点） | `test_client_protocol_gate.py` 的拒绝列表加入 `"2"` | 同上 |
| schema 版本断言改为引用常量 | `test_api.py`、`test_backup_script.py` | `pytest tests/test_backup_script.py -q` → **10 passed** |
| `main.py` 用户解析与设备签发接线 | `_authorize_user_device` 注入 `UserRepository`，返回体追加 `user`/`identity` | `ruff` 通过；`create_app` 导入成功 |
| 部署文档当前版本约束 | `README.md`、`docs/deployment.md`、`docs/genkit-oauth.md`、`docs/market-data.md` | 版本号与归属语义更新；`docs/progress.md` 为历史执行记录，保留原文另加新条目 |
| 路由清单新增 3 条并保留历史计数 | [route-manifest.json](/Users/Gin/Workspace/Lyra/server/tests/fixtures/route-manifest.json)、`tests/fixtures/README.md`、`preflight_cutover.py` | 47（历史）+ 3 = **50 对 + 1 WS**；`pytest tests/test_route_manifest.py tests/test_preflight_script.py -q` → **9 passed** |
| 健康响应按 §3.4/§4.4 收口 | `api/health.py`、`main.py`、`db/connection.py`、`gmail_connection_repository.py` 新增部署级聚合 `count_gmail_connections_needing_attention` | 移除两处**跨用户活动量**：`gmail.totalConnections`/`attentionRequired`（改为只报 `status`），以及 `database.ledgerVersion`——后者是**全库** `max(version)`，`_HEALTH_SQL` 原先直接查 `ledger_versions`。计划 §4.4 明确禁止公开端点暴露账本版本。实测应用返回 `database={"status":"available","schemaVersion":16}`、`gmail={"status":"available"}`。**未填伪造的 0 维持旧形状**。测试同步更新（`test_health.py` +3 条、`test_api.py` 七段断言改闭集、`test_migrations.py` 改为断言字段不存在）→ `test_api.py + test_health.py + test_migrations.py + test_admin_bootstrap.py + test_multi_user_schema.py` **78 passed** |
| 修复基线潜伏的启动期崩溃 | [main.py](/Users/Gin/Workspace/Lyra/server/src/lyra/main.py#L136) `login_provider_ids` | `_providers` 是 `dict[provider_id, provider]`，`provider.id for provider in ...` 会对**字符串**取 `.id` 抛 `AttributeError`。该 bug 在 HEAD 中就存在，只因测试环境从不配置 OAuth 来源（空 dict 不执行循环体）而一直未显形——**任何配齐一组 OAuth 凭据的真实部署都会在启动时直接崩**。由 P2 的回归测试发现，主代理修复；`pytest tests/test_auth_routes.py tests/test_health.py -q` → **84 passed** |

P0 契约新增 **F8**：被外键引用的行，其**参与外键的列**不能更新（`paired_devices.identity_id` 在设备凭据存在时不可改写），而不参与外键的列可以（`users.status`）。该结论由 P2 实施时发现、主代理独立复验确认，是对 P0 首轮探针的补充，直接决定「同一设备换身份」首版不提供。

后续（同日晚些）主代理侧修复与收口：

| 事项 | 位置 | 验证 |
| --- | --- | --- |
| 迁移 16 删除旧 owner 身份表；离线入口的「未确认身份」读取提到迁移**之前** | `multi-user-schema.sql`、`admin.py` | 原只有升级路径删，新库会留空表导致两条路径表集合不一致；`pytest test_admin_bootstrap + test_migrations + test_multi_user_schema + test_user_repository` → **83 passed** |
| 迁移 16 **撤销全部旧设备凭据**（计划 §1.8「发布包含数据迁移和重新登录」、§9.4「撤销旧凭据」） | `multi-user-schema.sql` | 原样保留旧凭据会让前授权模型时期的会话穿过一次授权模型的引入。只改状态不删行（重放检测与审计需要）。新增测试断言 `active` 凭据迁移后为 `revoked` 且行数不变、已撤销行保留原撤销时间 → `test_multi_user_schema.py` **31 passed** |
| 修复 `agent_events.py` 的 `AuthContext` 回归 | `api/agent_events.py` | P2 把 `authenticator.authenticate()` 的返回改成 `AuthContext` 后，WS 入口仍取 `device.id` → `AttributeError`，**WebSocket 连接会在运行时直接崩**。该文件不在 P2 的清单里。改为 `auth.device_id`；`test_agent_events_route.py` 由全 ERROR 转为全通过 |
| 无人认领的跨模块契约测试 | `test_client_protocol_gate`、`test_error_envelope`、`test_no_store_coverage`、`test_database_concurrency` | 夹具补 `UserRepository` 与共享写协调器、清理死导入 → **40 passed** |
| 幂等记录仓库测试 | `test_mutation_idempotency_repository.py` | 设备夹具补 `user_id`/`identity_id`（schema 16 两列 NOT NULL）→ **19 passed** |
| P4/P5/P6 共享夹具补第三个构造参数 | 6 个测试文件 8 处 | 由 ERROR 转为可运行；`test_agent_routes + test_agent_events_route` → **25 passed** |
| 清除测试里硬编码的 `x-lyra-protocol-version: "2"` | 8 个测试文件 | 协议升 3 后这些请求被 **426** 静默拒绝（症状是 `KeyError`，不像协议问题）。**已修 5 个非 P3 文件**，改用共享常量 `CLIENT_PROTOCOL_HEADERS`；剩余 `test_account_creation`、`test_ledger_routes`、`test_import_routes` 在 P3 完成后处理，避免与其并发写入冲突 |
| 预检脚本四项关键检查通过 | `scripts/preflight_cutover.py` + `test_preflight_script.py` | 修正仍写着「协议版本为 2」的检查标签。实测：`协议版本为 3: LYRA_PROTOCOL_VERSION=3`、`模式版本自洽: 清单 16 条、末条 16、常量 16`、`接管既有账本: schema=15 tables=28`（冻结夹具仍可打开）、`路由契约: 50 条与契约一致`；`pytest test_preflight_script.py` → **5 passed**，`ruff check .` 通过（全量测试项未跑：三个代理正在并行改动） |
| 删除启动期装载全局行情偏好（P6 的阻塞项） | [main.py](/Users/Gin/Workspace/Lyra/server/src/lyra/main.py#L186) | 删掉 `load_market_settings` + `initialize_settings` 那段：它把某一个用户的设置装成全局运行时偏好，多用户下等于让启动时读到的那个用户覆盖所有人的来源选择。保留 `create_market_data_runtime(...)`（启动默认路由 + 部署级共享的报价服务）。`ruff` 通过，`create_app` 导入成功 |

**已记录的跨模块待接（P4 范围，勿遗漏）**：P6 已把 `MarketDataRuntime.settings_snapshot` 的签名改为

```python
settings_snapshot(connection: duckdb.DuckDBPyConnection, user_id: str) -> MarketDataRuntimeSettings
```

每次调用都读数据库、**不做跨请求缓存**（缓存某个用户的设置等价于把它当成运行时"当前值"，多用户下必然互相覆盖）。因此以下**不属于 P6** 的调用方会断，须由 P4 在集成时补上 `(connection, user_id)`：

- [public_projection.py:758](/Users/Gin/Workspace/Lyra/server/src/lyra/application/public_projection.py#L758) `project_market_positions` 里的 `market_runtime.settings_snapshot()`
- [financial_tool_handlers.py:321](/Users/Gin/Workspace/Lyra/server/src/lyra/application/financial_tool_handlers.py#L321) 的 `market_runtime.settings_snapshot()`

`public_projection.py` 是 **P3 与 P4 共用**的投影模块（无单一归属），改动前须确认不与 P3 冲突。

**P3 签名变更造成的待适配调用点（P4/P5/P6 必须逐条收口，否则是 `TypeError`）**。P3 按纪律没有越界修改它们：

| 位置 | 归属 | 所需改动 |
| --- | --- | --- |
| `api/market_routes.py:169` | P6 | `DuckDbLedgerRepository(connection)` → 加 keyword-only `user_id` |
| `api/market_routes.py:231` | P6 | `resolve_legacy_account_key(connection, scope.account or "")` → 插入 `user_id` |
| `application/financial_tool_definitions.py:150` | P4 | 同上，插 `user_id` |
| `application/financial_tool_definitions.py:174`、`:271` | P4 | `DuckDbLedgerRepository(connection_provider())` → 加 `user_id`（**P4 需先给工具上下文补 `userId`**，契约 §4.2 已要求） |
| `application/financial_tool_handlers.py:313` | P4 | 同上 |
| `infrastructure/db/agent_session_repository.py:202`、`:211` | P4 | 包装方法加 `user_id` 形参并转发 |
| `infrastructure/db/gmail_message_repository.py:142` | P5 | `DuckDbImportCommitRepository(connection)` → 加 `user_id` |
| `infrastructure/gmail/service_factory.py:479` | P5 | `DuckDbLedgerRepository(connection)` → 加 `user_id` |
| `tests/test_agent_session_repository.py` | P4 | fixtures 缺 `accounts.user_id`，且 164/170/189/196 行按新签名调整 |
| `tests/test_financial_tool_definitions.py` | P4 | 同上，139/214 行 |

另：`api/market_routes.py:169` 那个事务里还有 `SELECT coalesce(max(version),0) FROM ledger_versions` 与 `load_market_settings(connection)` 两处无用户范围读取，属 P6。

**「提醒」无需新增收口代码**（P3 核实）：`v_official_import_reminders` 没有任何 Python 读取路径，只被 schema 与 `test_multi_user_schema.py` 引用；该视图以 `accounts.id` 为轴，而 `import_files` 受 `(user_id, target_account_id)` 复合外键约束同属一个用户，因此不存在需要收口的无范围入口。

**主代理对各代理「待裁决项」的裁定**（记录理由，避免后人重开）：

| 来源 | 求裁决项 | 裁定与理由 |
| --- | --- | --- |
| P5 | 单次同步上限是否进 `config.py` | **保持模块常量**。`config.py` 只收核心 8 个变量；`LYRA_AGENT_*`/`LYRA_MARKET_*` 都是各模块自己从环境读（如 `market_data/config.py`）。要做成可配置需要一个"有界整数"读取器，而 `market_data/config.py::_bounded` 是行情专属——import 进 gmail 会造出跨层依赖，复制一份又违反 DRY。P5 的常量已带完整理由与升级路径；若将来确需可调，正确做法是把 `_bounded` 提升为共享设置助手（独立重构，不在本计划内） |
| P5 | Gmail 同步未进 `WriteCoordinator` | **接受现状**（改前也没有）。计划 §4.4 要的排序由共享连接的事务锁保证：`BEGIN` 持锁到 `COMMIT`——停用先提交则同步读到 `disabled` 并整体回滚；同步先开始则停用等它提交后再撤销。因此 P0 契约 §3.5 描述的 V11 确定性写法（持协调器锁 → 入队 → 停用 → 释放 → 断言拒绝）**不适用于 Gmail 同步** |
| P5 | 同用户重复授权同一 Google 账号：409 还是 upsert | **保持 409**。P0 契约 §3.12 冻结的正是"报错文案需按用户表述"，迁移文档也把它记为报错分支。改成 upsert 需改写既有断言，属计划外行为变更 |
| P6 | 两处越权测试改写 | **追认**。断言的正是被冻结契约删除的 API；逐行核对确认是**更强**的断言（新增"实例上不再有 `initialize_settings`/`_routes`"的回归守卫、"第二次调用确实打到新来源"） |
| P6 | `test_market_routes.py` 涨到 1030 行 | **暂不拆分**，避免 P8 临近时制造无关改动 |
| P4 | `tests/test_tool_runtime.py` 越界改 1 行 | **追认**。`AgentToolContext` 新增必填 `user_id` 后该文件会全 ERROR，而被测模块 `tool_runtime.py` 属 P4。改动只是夹具补一个 kwarg |
| P4 | `_close_device_sessions` 是否改用 `take_user`/`cancel_user_turns` | **保持现状**。主代理核实 `revoke_user_devices` 返回该用户**全部**设备（含已撤销的），逐设备收口因此完整；而 `_close_device_sessions` 还为**登出**复用（单设备），改成用户维度会让它有双模式却无可观测收益 |
| P4 | `GET .../tool-calls` 新增 UUID（400）与归属（404）校验 | **接受**。原实现连会话 ID 校验都没有就直接返回空列表——那正是计划禁止的"用空结果掩盖失败"；新行为与其它按 ID 路由一致 |
| P5 | `_SyncDependencies` 中 refresh/fetch 的活动检查只能间接覆盖 | **已关闭**：主代理新增 [test_gmail_sync_dependencies.py](/Users/Gin/Workspace/Lyra/server/tests/test_gmail_sync_dependencies.py)，逐点验证 `load_connection` / `refresh_access_token` / `fetch_incremental` / `fetch_initial` / `save_connection` 五个动作在停用后各自以 `USER_DISABLED`（403、不可重试）拒绝，并做反向确认（移除 `save_connection` 的检查后该用例变红）。写这条用例时**又抓到一处缺陷**（见下） |

**主代理在写上述用例时抓到的第四处同族潜伏缺陷**：`service_factory.fetch_initial` 里写的是
`from ...domain.gmail.search_query import build_schwab_econfirm_search`，而该函数一直只存在于
`infrastructure/gmail/search_query.py`（`git show HEAD` 确认**基线即如此**）。

它此前是死代码——`GmailService.sync` 在生产中**根本不存在**（P5 报告的第三处缺陷），因此首次扫描没有生产入口。P5 实现 `sync` 之后 `fetch_initial` 变成**可达路径**，这一行就会抛 `ModuleNotFoundError` → 首次 Gmail 同步 500。已改为同包导入 `from .search_query import ...`。

至此同类"从未被走到的代码路径"共四处，全部在基线中潜伏、全部因为缺少端到端覆盖而未被发现：

| # | 位置 | 症状 | 触发条件 |
| --- | --- | --- | --- |
| 1 | `main.py` `login_provider_ids` | 启动即崩（`AttributeError`） | 配齐任一 OAuth 来源 |
| 2 | `import_routes._device()` | **静默关闭提交幂等** | 永不崩溃，只是失效 |
| 3 | `gmail_routes` 的 `.sync` | 每次同步 500 | 用户点"同步" |
| 4 | `service_factory.fetch_initial` 的错误导入 | 首次扫描 500 | 首次 Gmail 同步 |



**契约样本流水线已落地（计划 §8 的强制项）**：

- 生成器 [export_contract_samples.py](/Users/Gin/Workspace/Lyra/server/scripts/export_contract_samples.py)：在临时空库上装配真实应用，只把 OAuth 来源客户端换成脚本替身，然后走**真实的** HTTP 端点采集响应。自动注册、身份解析、设备与凭据签发、账户创建、导入预览全部走生产代码，因此样本反映的是**协议**而非某个内部函数的返回值。
- 采集的 **7 条**用例覆盖计划 §8 列出的五类：`login`（`POST /v1/auth/{provider}/exchange`）、`me`、`createAccount`、`ledgerAccounts`、`ledgerSummary`、`importPreview`（真实上传一份合成 Schwab CSV）、`marketSettings`。
- 消费测试 [contract_samples_test.dart](/Users/Gin/Workspace/Lyra/apps/mobile/test/contract_samples_test.dart)：**9 条**断言，全部用**生产解析器**解析同一份文件——`LyraUserProfile.fromJson`、`BrokerageAccount.fromJson`、`LedgerSummaryRead.fromJson`、`ImportPreviewView.fromJson`、`MarketSettings.fromJson`。
- 三个设计决定：样本放仓库根的 [contracts/](/Users/Gin/Workspace/Lyra/contracts/README.md)（放任一侧，另一侧就会当「别人的夹具」再手写一份，漂移恰好被掩盖）；生成器走真实端点而非调内部函数；**样本缺失时 `fail` 而不是 `skip`**——跳过会让「没跑生成器」看起来像「协议一致」。
- 验证：`flutter test test/contract_samples_test.dart` → **9 passed**；Flutter 全量 **282 passed, 5 skipped**（基线 262 → 273 → 277 → 282），`flutter analyze` 无问题；服务端 `ruff` 通过；生成器可重复执行。
- 过程中修掉自己两处错误断言：创建类端点返回 **201** 而读取类返回 200，最初写死 200 导致假失败；`createAccount` 的响应是 `{account}`，**不带 `{metadata,data}` 信封**（与账本读取不同）。





**契约 §3.9 实测核对（主代理跑真实应用，不采信自述）**：自动注册一条 GitHub 身份后，

```
登录侧 user 键: displayName, identity, role, status, userId        role=member status=active
identity 键:    email, provider, subject
/v1/me → 200，顶层键: user；user 键与 identity 键同上
两侧 user 对象完全一致: True
```

即登录完成响应与 `GET /v1/me` 返回**同一个** `user` 对象，字段集与契约 §3.9 逐字一致——Flutter 的严格解析器（缺字段即 `FormatException`）据此可解析。

**冻结夹具完整性**：`typescript-ledger-schema15.duckdb` 全程未被改动（`git status` 干净，sha256 `1b430cc0…`），满足计划"原夹具保持不变"；`tests/fixtures/` 下仅 `README.md` 与 `route-manifest.json` 由主代理有意更新。



破坏基线与收敛（P1a 落地后、P2–P6 未修之前）：**444 个失败/错误（25 FAILED + 419 ERROR）横跨 30 个测试文件**，根因单一——`NOT NULL constraint failed: accounts.user_id`。这是预期破坏，随各模块迁移调用方逐步收敛。

**收敛到 0——全部写入者停止后主代理跑的终局证据（2026-10-16）**：

| 命令（工作目录 `server/` 与 `apps/mobile/`） | 结果 |
| --- | --- |
| `uv run pytest -q` | **2663 passed, 4 skipped, 0 failed**，退出码 0，232.45s |
| `uv run ruff check .` | All checks passed |
| `flutter analyze --no-pub` | No issues found |
| `flutter test --no-pub --reporter expanded --timeout 60s --concurrency=2` | **282 passed, 5 skipped, 0 failed**，退出码 0 |

基线是服务端 2453 passed / Flutter 262 passed，本次净增 **210 + 20** 个测试。唯一 warning 是既有的 Starlette `httpx` 弃用提示，与本改造无关。

交给执行主代理的启动指令：

> 按本计划实施 Lyra 多用户独立账本。先完成 P0，保留工作区已有修复；按波次将可独立实现任务交给子代理，主代理负责设计、公共文件接线和最终审核。复用现有 Google、GitHub、LINUX DO OAuth，首次登录自动注册普通用户，不重写登录链路。严格执行用户归属、注册事务与唯一身份约束、数据库迁移和验收矩阵；不读取真实凭据或真实账本，不部署、不提交或推送。每个任务完成后在本节补充真实差异与验证证据，再进入依赖它的阶段。

## 11 修订记录

### 11.1 2026-10-16 评审补缺

评审对第 2 节的九项基线事实逐条回到代码核实，全部属实，无需修正；版本、约束与入口描述与当前实现一致。以下四项为评审发现的计划缺口，已就地修订。

| 编号 | 问题 | 修订 |
| --- | --- | --- |
| G1 | 用户与身份仓储的归属未指定。P1（离线初始化）与 P2（自动注册）都要写 `users`/`user_identities`，原计划分别表述为「新增离线初始化实现」与「可新增专用用户仓储」，会在同一文件上相撞 | §5 明确该仓储由 P1a 独占、P0 冻结其完整方法签名；P2 只调用不修改，需要新方法时经主代理在 P1a 文件上增补。P0 增加签名冻结条目 |
| G2 | P1 是唯一的关键路径单点，独占 schema 重建、离线初始化、五类迁移测试与 owner 表删除，P2–P6 全部依赖它，且原计划未给并行缓解 | P1 拆为 P1a（物理 schema + 用户仓储 + 约束验证）与 P1b（数据迁移 + 离线入口 + 迁移测试），写作用域互斥；P1a 完成即放行 P2/P7。§6 波次表与 §10 任务表同步更新 |
| G3 | 原「波次 2 三路并行」不成立：P3 的验收被同波次的 P2 卡住，而计划又禁止用桩测试冒充真实鉴权集成 | P3 移至波次 3，并在 P3 增加显式验收门槛：P2 集成前只能桩验证，不得报告隔离验收通过，不得以桩测试充当 V03–V06 证据 |
| G4 | P8 负载过重，同时承担公共接线、管理路由、初始化入口、停用级联、健康收口与全量验收审核；停用级联实为 P4/P5 的实现责任 | §4.4 的停用级联实现明确切给 P4（WS 与 Agent 侧）与 P5（Gmail 后台同步侧），P2 只提供停用查询与通知接口，P8 只做接线与验收 |

同时补充两项原计划未覆盖的执行前提：

- **G5 健康响应字段清单**：已核实当前 `/v1/health` 返回 `gmail.totalConnections`、`attentionRequired` 与 agents 计数；§4.4 只规定「不暴露用户 Gmail 数量、不填伪造 0 值」，未给出保留清单。P0 增加逐段确认字段取舍的条目，作为 P8 的改造清单。
- **版本语义显式化**：已核实 `_current_ledger_version()` 为全局 `max(version)`，且 `gmail_connections.google_subject` 为全局 `UNIQUE`，两处均为 §4.1 与 P3 已点名的隔离点。P0 增加条目把「用户当前版本 = `max(version) WHERE user_id`」写成显式定义，并记录单用户版本号不连续（已核实 Flutter 只用等值比较，无 `+1` 或计数假设，故安全）。

另有两项非阻断性调整：§8 补充 2026-10-16 实测的沙箱限制（`uv` 缓存目录、`flutter` SDK 缓存目录、缺少 `timeout`），避免执行者把环境限制误报为失败；§10 将工作量估算由 12–20 人日上调至 20–30 人日，并说明上调依据。

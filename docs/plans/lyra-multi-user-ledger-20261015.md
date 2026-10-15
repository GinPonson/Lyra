# Lyra 多用户独立账本执行计划

日期：2026-10-15。状态：待实施。本次交付仅保存计划，不代表账号系统已实现或已部署。

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

DuckDB 定向验证是 P0/P1 门槛：建立真实父子外键后，测试用户停用、设备撤销、账户归档和 Gmail 状态更新。若当前锁定版本限制被引用父行更新，复用项目已有的“不可变身份表 + 可变状态表”模式；由主代理先更新物理表契约，子代理不得自行移除完整性校验或各选一种模型。

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

主代理独占公共接线与契约文件：[main.py](/Users/Gin/Workspace/Lyra/server/src/lyra/main.py)、[config.py](/Users/Gin/Workspace/Lyra/server/src/lyra/config.py)、[protocol.py](/Users/Gin/Workspace/Lyra/server/src/lyra/protocol.py)、[pyproject.toml](/Users/Gin/Workspace/Lyra/server/pyproject.toml)、[路由清单](/Users/Gin/Workspace/Lyra/server/tests/fixtures/route-manifest.json)、共享测试夹具、本计划和部署文档。子代理提供精确接线需求，主代理在对应波次集成。迁移文件只由 P1 修改；新增共享 AuthContext 的具体文件和接口在 P0 指定并由 P2 落地。

### P0 主代理冻结契约和基线

依赖：无。权限：只读检查；可创建临时合成数据库做定向验证，不打开真实数据。

- 确认当前未提交改动与本计划基线是否一致，记录需保留的修改，避免重做上一轮 review 修复。
- 冻结第 4 节的字段、错误码、仓储构造参数、工具上下文和响应形状；生成供服务端与 Flutter 共用的合成契约样本。
- 定向验证 DuckDB 父子外键更新、表重建与事务回滚；确定 users 的最终物理结构以及身份与设备关联方式。
- 列出所有 REST/WS 入口、后台写入口和公开健康字段，标记归属要求；作为 P8 的覆盖清单。
- 确认当前 schema/协议、Python/Flutter 测试入口及可用 SDK；仅在需要时重跑当前基线，保留真实结果。

完成条件：公共契约无待定字段，P1 能据此建表，P2/P3/P7 能据此独立实现。

### P1 数据模型与迁移代理

依赖：P0。可改：[db 目录](/Users/Gin/Workspace/Lyra/server/src/lyra/db)、迁移测试及新增的离线初始化实现。新增迁移文件拟为 `/Users/Gin/Workspace/Lyra/server/src/lyra/db/sql/multi-user-schema.sql`，离线入口拟为 `/Users/Gin/Workspace/Lyra/server/src/lyra/admin.py`。不得修改历史 SQL 内容、业务仓储或生产数据。

- 实现第 4 节全部表归属、唯一约束与审计字段，保留既有金额、顺序、交易哈希、消息和来源记录。
- 实现显式管理员初始化及原 owner 迁移输入；必要的迁移上下文只服务于一次数据升级，不成为长期 owner 兼容模式。
- 按外键依赖重建所需表和视图，校验每个子资源与父资源归属一致；重启不得重复迁移或重新生成用户。
- 为全新库、冻结 schema 15 库、缺 owner 信息的旧库、故障注入回滚、迁移后再次启动提供测试。
- 迁移成功后删除运行时不再使用的 owner 身份表；保留历史迁移文件。缺少明确归属时启动必须明确失败，不能分配给首次登录者。

完成条件：新库与旧库副本迁移通过；记录数、Decimal 汇总、去重键、FIFO、消息及 Gmail 密文完整；用户和设备状态可更新；失败后旧数据可重新打开。

### P2 身份与自动注册代理

依赖：P1；按 P0 冻结接口与 P3/P7 并行。可改：[auth_routes.py](/Users/Gin/Workspace/Lyra/server/src/lyra/api/auth_routes.py)、[dependencies.py](/Users/Gin/Workspace/Lyra/server/src/lyra/api/dependencies.py)、[device_auth.py](/Users/Gin/Workspace/Lyra/server/src/lyra/security/device_auth.py)、[设备认证器](/Users/Gin/Workspace/Lyra/server/src/lyra/application/device_authenticator.py)、[设备签发服务](/Users/Gin/Workspace/Lyra/server/src/lyra/application/device_pairing_service.py)、[设备仓储](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/device_repository.py)、现有 owner 登录服务及来源配置模块、对应测试。可新增专用用户仓储、自动注册服务和管理路由。重命名旧 owner 模块时列出所有调用方，由主代理协调其他模块引用。

- 实现按 `(provider, subject)` 查找或原子创建 user/identity、用户状态管理、`require_user` 与 `require_admin`；所有自动注册用户固定为 `member`。
- 将 owner 白名单替换为首次登录自动注册与已有用户状态校验；provider 可用性取决于 OAuth 配置，不再要求单 owner 的 subject 配置。旧配置退出运行时鉴权，迁移输入单独显式处理。
- 令牌认证、刷新和设备签发都验证用户；设备不能通过公钥或重复登录转移归属。
- 提供停用后的设备列表与通知接口，交给主代理接入 WS、Agent 和后台任务取消；不得仅更新状态字段就报告停用完成。
- 保持 OAuth state/PKCE/nonce、刷新轮换、重放检测、撤销和 no-store 行为；更新相关正向与反向测试。

完成条件：三来源的新身份均能自动注册并获得空账本；再次登录和同身份并发登录保持同一 userId；未完成验证时不创建用户；同邮箱不合并；停用身份不能重新注册，登录和刷新均被拒；最后管理员保护有效。

### P3 账本与导入代理

依赖：P1、P0 契约；最终集成测试依赖 P2。可改：[ledger_routes.py](/Users/Gin/Workspace/Lyra/server/src/lyra/api/ledger_routes.py)、[import_routes.py](/Users/Gin/Workspace/Lyra/server/src/lyra/api/import_routes.py)、账户服务与账户查找/仓储、账本仓储、导入应用服务及导入仓储、相应测试。`ledger_routes.py` 中认证器装配的改动也由本代理接收 P2 的接口说明后完成，避免多人写同一文件。

- 所有账户 CRUD、分析、分页、来源详情、提醒与导入操作使用显式用户范围。
- 先按用户查导入作业，再校验预览令牌和幂等；上传尚未绑定账户时也有 owner。暂存文件的读取和删除必须经过作业授权。
- 账本版本统一按当前用户读取，快照与版本在同一短事务中获取；写入版本关联正确用户，B 的写入不得使 A 预览失效。
- 账户键变为用户内唯一后，逐个修复 SQL/映射查询，验证同键跨用户不会混入解析、去重、FIFO 或汇总。
- 同用户并发确认同一预览仍沿用既有冲突/幂等语义；账本写入和用户停用遵循同一写协调器排序。

完成条件：A/B 使用相同账户标识与相同文件可各自导入；各自 `scope=all`、版本、明细和分页正确；串改 importId/accountId/token 不能读写他人资源；现有财务计算回归通过。

### P4 Agent 隔离代理

依赖：P2/P3 接口已集成。可改：[Agent 路由](/Users/Gin/Workspace/Lyra/server/src/lyra/api/agent_routes.py)、[事件路由](/Users/Gin/Workspace/Lyra/server/src/lyra/api/agent_events.py)、[编排器](/Users/Gin/Workspace/Lyra/server/src/lyra/application/agent_turn_orchestrator.py)、[金融工具装配](/Users/Gin/Workspace/Lyra/server/src/lyra/application/financial_tool_definitions.py)、[Agent 领域代码](/Users/Gin/Workspace/Lyra/server/src/lyra/domain/agent)、Agent 仓储、运行时接入及对应测试。不得修改账本仓储、schema 或行情模块。

- 创建、读取、历史、发送、取消、关闭、工具审计和 WS 订阅/重放均验证会话 user。
- userId 由服务端 session 身份注入工具上下文；禁止模型指定用户，`all` 限于该用户所有账户。
- 保留历史上下文、线程池模型执行、事件顺序、原子订阅重放与取消语义。
- 停用后禁止继续工具执行与事件投递，清理用户的订阅和活动回合；不要因原创建设备撤销而把用户历史变成他人可访问资源。

完成条件：A 不能读取、取消或订阅 B 的 session；伪造工具参数不能扩大范围；同用户另一设备可读自己历史；停用中的长连接和模型回调不继续泄露数据。

### P5 Gmail 隔离代理

依赖：P2/P3 接口已集成。可改：[Gmail 路由](/Users/Gin/Workspace/Lyra/server/src/lyra/api/gmail_routes.py)、Gmail 应用服务、[Gmail 基础设施](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/gmail)、Gmail 连接与消息仓储及对应测试。不得改 schema、共享 AuthContext 或账本仓储。

- OAuth 事务、连接列表、映射、同步、断开全部保留用户归属；连接不能通过 Google subject 的全局 upsert 被其他用户覆盖。
- 映射和消息入账时同时检查 connection/account/transaction 归属，复用 P3 的用户账本写入和版本接口。
- 后台增量同步携带 userId，入队与实际执行检查活动状态；用户停用后停止该用户任务。
- 保持既有密文存储、邮件解析与丢弃正文原则，不向日志或 Agent 泄露 Gmail token 和邮件正文。

完成条件：A 无法列出、同步、断开 B 的连接；跨用户账户映射失败且无部分写入；用户间相同邮件标识不互相去重；停用后的排队同步不能入账。

### P6 行情设置隔离代理

依赖：P2/P3 接口已集成。可改：[行情路由](/Users/Gin/Workspace/Lyra/server/src/lyra/api/market_routes.py)、[行情服务](/Users/Gin/Workspace/Lyra/server/src/lyra/application/market_data_service.py)、[行情运行时](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/market_data/runtime.py)、[设置仓储](/Users/Gin/Workspace/Lyra/server/src/lyra/infrastructure/db/market_settings_repository.py)及对应测试。不得改 Agent 工具文件；向 P4 提供显式用户设置快照调用契约。

- 设置保存、读取、revision 冲突检测均按用户处理，启动不再把某个用户设置装成全局运行时偏好。
- HTTP 和 Agent 使用同一套“当前用户设置快照 + 共享报价服务”接口；缓存仍按公开报价身份复用。
- 保留已有币种分组、缺价/过期表达、账户 UUID 映射、短事务快照及网络请求在事务外的行为。

完成条件：A/B 并发采用不同来源时 HTTP 与 Agent 各自得到正确来源；A 设置更新不改变 B revision；供应商全局预算仍生效；用户持仓不会出现在共享缓存值中。

### P7 Flutter 账号适配代理

依赖：P0 响应契约，可与 P2/P3 并行；真实服务端契约验收依赖 P2 至 P6。可改：[移动端 lib](/Users/Gin/Workspace/Lyra/apps/mobile/lib)、[移动端 test](/Users/Gin/Workspace/Lyra/apps/mobile/test)。不得修改服务端、依赖锁文件或重新设计现有登录页面。

- 在登录响应、安全存储和会话模型中加入稳定 userId；协议更新为 3，严格解析新身份字段。
- 复用现有三种 OAuth 按钮，登录页说明“首次登录将自动创建账号”；新用户完成授权后直接进入空账本与既有首次导入流程，无独立注册表单。补充禁用或注册事务失败的明确反馈，保留取消、超时、重试和浏览器回跳行为。
- 偏好统一按 `normalizedServerUrl + userId`；账户范围、Agent session 与缓存按该身份隔离。旧凭据缺 userId 时清理会话并要求登录，不能按邮箱猜归属。
- 切换账号时取消旧请求、关闭 WS、失效旧 client、清理财务页面和 Agent session；迟到响应不能恢复上一用户状态。
- `/me` 用于核对当前用户身份；保持现有明确的离线/网络错误处理，不把网络失败伪装成用户已被停用。
- 普通用户设置和资料不展示管理员专用能力；首版管理 API 不需要完整 Flutter 管理后台。

完成条件：新身份首次登录直接进入独立空账本；同一身份再次登录返回原账本；A→退出→B 的缓存、页面、偏好和迟到响应隔离；同用户不同 OAuth 身份在明确关联后使用同一偏好键；旧协议被清楚拒绝；真实后端 JSON 可由生产解析器读取。

### P8 主代理集成与最终审核

依赖：P1 至 P7 完成。主代理接入所有公共文件、管理路由、初始化入口、停用回调和健康响应；更新路由清单、预检脚本及部署文档中的当前版本约束，保留历史执行记录的历史含义。

- 逐项对照 P0 入口清单，覆盖 REST、WS、后台任务、审计、设置、游标和幂等重放；不能仅检查是否出现 `user_id` 字符串。
- 检查没有绕开用户范围的生产调用方，没有测试专用的默认 owner，也没有移除原有隔离/精度断言来通过测试。
- 核对各代理差异和测试证据；可将独立事实核对委派为只读审核，最终结论由主代理作出。
- 执行第 7 节矩阵与第 8 节验证；完成升级和恢复手册，记录真实结果与未完成的外部联调。

完成条件：所有发布门槛有证据，所有相关检查通过，真实环境未验证事项单独列明；未执行生产部署时不得写“已上线”。

## 6 调度与交接规则

| 波次 | 任务 | 放行条件 |
| --- | --- | --- |
| 0 | P0 主代理 | 契约、归属设计与迁移约束确定 |
| 1 | P1 数据模型 | 迁移测试通过，schema 稳定 |
| 2 | P2 身份与自动注册、P3 账本导入、P7 Flutter 并行 | 每个代理只写自己的文件；主代理先完成真实鉴权与账本集成 |
| 3 | P4 Agent、P5 Gmail、P6 行情并行 | 共用已完成的用户上下文和账本接口 |
| 4 | P8 主代理 | 全链路集成与验收完成 |

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

全部 P0 至 P8 完成、V01 至 V17 有测试或验收证据、协议与迁移同步、无未解决的越权路径，才可标记实现完成。生产 OAuth、真实模型/Gmail、Android 真机及生产部署若未执行，分别记录为待联调或待部署，不影响对本地实现事实的准确报告。

工作量参考：一名熟悉项目的工程师约 12–20 人日，已按复用现有 OAuth 计算，包含迁移、客户端与隔离回归；这是规划估算，不是测试结果或完成承诺。

| 任务 | 状态 | 交付与证据 |
| --- | --- | --- |
| P0 公共契约与基线 | 待开始 | 待记录 |
| P1 数据模型与迁移 | 待开始 | 待记录 |
| P2 身份与自动注册 | 待开始 | 待记录 |
| P3 账本与导入 | 待开始 | 待记录 |
| P4 Agent | 待开始 | 待记录 |
| P5 Gmail | 待开始 | 待记录 |
| P6 行情 | 待开始 | 待记录 |
| P7 Flutter | 待开始 | 待记录 |
| P8 集成与审核 | 待开始 | 待记录 |

交给执行主代理的启动指令：

> 按本计划实施 Lyra 多用户独立账本。先完成 P0，保留工作区已有修复；按波次将可独立实现任务交给子代理，主代理负责设计、公共文件接线和最终审核。复用现有 Google、GitHub、LINUX DO OAuth，首次登录自动注册普通用户，不重写登录链路。严格执行用户归属、注册事务与唯一身份约束、数据库迁移和验收矩阵；不读取真实凭据或真实账本，不部署、不提交或推送。每个任务完成后在本节补充真实差异与验证证据，再进入依赖它的阶段。

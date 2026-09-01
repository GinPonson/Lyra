# Lyra 移动资产账本与 AI Agent 应用完整方案

> 文档状态：方案讨论稿，不代表已经进入开发  
> 版本：1.0  
> 日期：2026-08-30  
> 目标平台：Android 优先的 Flutter 手机应用  
> 迁移来源：`/Users/Gin/Workspace/Schwab`  
> 新项目目录：`/Users/Gin/Workspace/Lyra`

## 1. 执行摘要

Lyra 定位为一款私有部署、以数据可信度为第一优先级的多账户资产账本与分析应用。它读取 Schwab 官方 Excel/CSV 交易数据，并在用户完成 Google 授权后，持续从 Gmail 中增量提取 Schwab 交易确认邮件。应用提供持仓、已实现盈亏、现金流、风险、回撤、归因等分析，同时集成可切换的 Claude、Pi、OpenCode AI Agent。

Lyra 不是券商交易终端，不提供下单、改单、撤单，也不在第一阶段接入实时行情。其核心价值是：

- 将 Schwab 历史交易、后续邮件交易确认和多账户数据统一为可审计账本。
- 完整迁移现有 Schwab Python 项目的解析、FIFO 和分析能力到 TypeScript。
- 使用一个 TypeScript 服务完成 API、数据处理、Gmail 同步、定时任务和 Agent 编排。
- 使用 Flutter 构建独立手机端，保持服务端与 App 的职责边界清晰。
- 以 Revolut 的视觉语言为唯一设计参考，在关键体验中达到高端数字产品的视觉完成度。
- 所有金融结果都明确标示账户范围、数据时间、账本版本和计算口径。

第一阶段推荐架构为：

```text
Flutter App
    │
    ├── HTTPS REST：查询、导入、授权、配置、会话控制
    └── WebSocket：导入进度、同步进度、Agent 流式事件
    │
Single TypeScript Server
    ├── Fastify HTTP / WebSocket
    ├── Excel / CSV 解析与标准化
    ├── Google OAuth / Gmail 增量同步
    ├── FIFO 与金融分析
    ├── 多账户与审计
    ├── Agent Orchestrator
    ├── Scheduler / Write Coordinator
    └── DuckDB
```

服务端只有一个业务进程。Claude、Pi、OpenCode 是该进程按需控制的子进程，不是独立微服务。Caddy 或 Tailscale 仅承担安全接入，不属于业务服务。

## 2. 已确认决策

| 领域 | 决策 |
| --- | --- |
| 项目阶段 | 已按本文进入分阶段实现；实际完成度与验证证据见 `docs/progress.md` |
| 客户端 | Flutter，Android 优先，保留未来 iOS 能力 |
| 服务端 | 单一 TypeScript 服务，不拆分 Python、Agent Gateway 或微服务 |
| 数据库 | 服务端 DuckDB；App 本地使用 SQLite 缓存 |
| 初始数据 | Schwab Excel/CSV 手动导入 |
| 后续数据 | Google OAuth 授权后的 Gmail 增量导入 |
| 账户模型 | 一个 Lyra 用户管理多个 Schwab 账户 |
| 交易能力 | 不支持实盘下单、改单、撤单 |
| 行情能力 | 第一阶段不接实时行情，不把历史成交价伪装成当前市价 |
| Agent | 支持 Claude、Pi、OpenCode，允许在回合边界切换 |
| Agent 运行位置 | 第一阶段统一在服务端运行，App 只负责控制与呈现 |
| 设计参考 | 仅采用 Revolut 风格，不混用 Binance、Coinbase、Kraken 的品牌语言 |
| 图标 | Material Icons Outlined，由统一语义映射层管理 |
| 表情符号 | 界面文案、按钮、状态、导航和空状态全程禁止使用表情符号 |
| 默认部署 | 私有、自托管、单用户、多券商账户 |
| 默认安全 | HTTPS、设备配对、短期令牌、系统安全存储、生物识别、令牌加密、审计日志 |

设计参考文档：[Revolut DESIGN.md](https://github.com/VoltAgent/awesome-design-md/blob/main/design-md/revolut/DESIGN.md)。

## 3. 产品定位与边界

### 3.1 产品定位

Lyra 是“交易数据账本与智能分析器”，而不是“券商交易客户端”。用户在手机上完成数据导入、账户切换、结果查看、审计和自然语言分析；所有权威金融计算由服务端执行。

### 3.2 目标用户

第一阶段面向一个私有部署的 Lyra 所有者。这里的“多账户”是多个 Schwab 券商账户，不是多人 SaaS 租户。这个边界可以显著降低权限、合规和数据隔离复杂度，同时仍保留未来多用户扩展所需的主键与审计结构。

### 3.3 核心场景

1. 用户首次安装 App，安全配对自己的 Lyra 服务端。
2. 用户导入一个或多个 Schwab 账户的 Excel/CSV 历史数据。
3. 用户预览解析结果、确认账户映射，并原子提交账本。
4. 用户授权 Google Gmail 只读访问，建立邮箱与 Schwab 账户的明确映射。
5. 服务端定时增量读取交易确认邮件，解析并写入标准交易账本。
6. 用户在单账户或全部账户范围内查看分析，但范围必须始终可见。
7. 用户让 AI Agent 调用只读金融工具解释持仓、盈亏、风险和异常。
8. 用户在 Claude、Pi、OpenCode 之间切换，并保留可迁移的规范化上下文。
9. 用户定期重新导入 Schwab 官方 Excel/CSV，对 Gmail 覆盖不足的数据进行对账。

### 3.4 明确不做

- 不提交或模拟真实证券订单。
- 不保存券商登录密码。
- 不使用浏览器自动化登录 Schwab。
- 不把 Gmail 交易确认视为完整账单来源。
- 不默认接入实时行情、新闻或投资建议数据源。
- 不让 Agent 直接执行 SQL、Shell 或任意文件读写。
- 不让 Agent 修改、删除或导入金融数据。
- 不在 App 中保存 Claude、Pi、OpenCode 的服务端密钥。
- 不在第一阶段建设多租户 SaaS、团队权限或公开云平台。

## 4. 产品原则

### 4.1 正确性优先于便利性

解析、账户匹配或计算出现歧义时必须停止并显示原因，不能猜测、跳过或静默降级。导入采取“全量验证后原子提交”，不能部分成功后让用户误以为账本完整。

### 4.2 账本是唯一事实来源

Flutter 本地数据库只是可重建缓存。所有持仓、FIFO、已实现盈亏和分析结果均以服务端账本版本为准。

### 4.3 所有结果可追溯

每个结果至少关联：

- `account_scope`：单账户或全部账户。
- `as_of`：数据截止时间。
- `ledger_version`：计算所依据的账本版本。
- `basis`：成本、已实现、现金流或其他计算口径。
- `source`：Excel/CSV、Gmail 或重算任务。

### 4.4 视觉表达服务于金融可信度

首页、过渡和 Agent 可以先锋、沉浸、富有物理感；涉及金额确认、账户映射、导入错误、交易流水和删除操作的页面必须克制、稳定、可复核。

### 4.5 不使用隐式回退

- Agent 不可用时显示具体不可用原因，不自动切换到其他 Agent。
- Gmail 游标失效时进入 `needs_resync`，不自动扫描整个邮箱。
- 账户后缀冲突时要求用户映射，不选择第一个匹配项。
- Excel 表头未知时拒绝导入，不做模糊字段猜测。
- 实时价格缺失时明确显示“未接入行情”，不以最后成交价冒充市价。

## 5. 总体技术架构

### 5.1 逻辑分层

```text
Presentation
  Flutter Screens / Motion / Charts / Agent Stream

Client Application
  Riverpod State / API Client / Secure Session / Local Cache

Transport
  REST / WebSocket / Stable Error Contract

TypeScript Application
  Use Cases / Authorization / Validation / Job Coordination

Domain
  Transaction Normalization / FIFO / Analytics / Account Scope / Agent Tools

Infrastructure
  DuckDB / Gmail API / File Parser / Child Processes / Encryption / Scheduler
```

### 5.2 单服务约束

一个 Node.js 进程负责所有业务模块，但模块之间仍必须保持清晰边界。单服务不等于单个巨型文件，也不等于放弃领域分层。

服务端内部约束：

- 所有账本写入通过单一 `WriteCoordinator` 排队执行。
- 查询可并发，但必须绑定明确账本快照或版本。
- 长任务使用进程内作业协调器，不引入 Redis。
- 定时任务、Gmail 同步和 Agent 运行都由同一进程管理。
- 进程退出时必须停止接收新任务、等待安全检查点并关闭子进程。
- 若未来需要水平扩展，必须先把 DuckDB 单写者和进程内任务状态迁出；第一阶段不提前承担这类复杂度。

### 5.3 客户端与服务端边界

Flutter 负责：

- 页面、动效、图表和交互。
- 文件选择与上传。
- Google 授权界面和一次性授权结果传递。
- 本地缓存、会话令牌和生物识别门禁。
- WebSocket 事件消费和状态呈现。

TypeScript 服务负责：

- 所有金融数据校验、标准化和持久化。
- FIFO 与分析计算。
- Gmail 令牌保管和定时同步。
- 多账户隔离与聚合。
- Agent 子进程、工具权限和会话迁移。
- 审计、安全策略和稳定错误码。

App 不复制 FIFO 或财务计算逻辑，以避免两套实现产生不一致。

## 6. 技术选型

### 6.1 Flutter 客户端

| 能力 | 推荐技术 | 选择理由 |
| --- | --- | --- |
| UI | Flutter / Dart | Android 优先且保留跨平台；适合定制绘制和高级动效 |
| 状态管理 | Riverpod | 依赖关系明确，便于异步状态、测试和作用域管理 |
| 路由 | GoRouter | 支持声明式路由、深链和认证重定向 |
| HTTP | Dio | 拦截器、上传进度、取消和统一错误处理成熟 |
| 实时事件 | web_socket_channel | 轻量承载 Agent 与作业事件 |
| 本地缓存 | Drift + SQLite | 类型化查询、迁移明确、适合离线只读缓存 |
| 安全存储 | flutter_secure_storage | 使用 Android Keystore 保存设备令牌和密钥材料 |
| 本地认证 | local_auth | 生物识别或系统凭据门禁 |
| 文件选择 | file_selector | 选择 `.xlsx` 和 `.csv` 文件 |
| 图表 | fl_chart + CustomPainter | 常规图表复用，品牌化图形自绘 |
| 图片缓存 | cached_network_image | 管理视觉素材缓存；金融敏感内容不走此缓存 |
| 测试 | flutter_test、golden test、integration_test | 覆盖逻辑、视觉和关键流程 |

不建议第一阶段引入复杂 Flutter Clean Architecture 模板、代码生成式网络层或多 package 工作区。采用 feature-first 单应用结构即可，在清晰边界与轻量维护之间取得平衡。

### 6.2 TypeScript 服务端

| 能力 | 推荐技术 | 选择理由 |
| --- | --- | --- |
| 运行时 | Node.js 当前 LTS | TypeScript 生态和 Agent CLI 编排友好 |
| 语言 | TypeScript strict | 提升金融模型、API 和事件类型可靠性 |
| HTTP | Fastify | 轻量、性能稳定、插件边界清晰 |
| 校验 | Zod | API、配置、导入中间结构统一运行时校验 |
| 数据库 | DuckDB Node API | 复用既有分析型数据模型，单机查询能力强 |
| 精确数值 | decimal.js | 金额、数量、比例禁止使用 JavaScript 浮点数 |
| CSV | csv-parse | 严格 CSV 解析和错误定位 |
| Excel | ExcelJS | 读取 `.xlsx` 单元格、工作表和元数据 |
| 邮件 | mailparser | RFC MIME、HTML 与文本内容解析 |
| Google | Node 原生 `fetch` + Google REST API | 当前仅需 OAuth、userinfo、Gmail messages/history 少量端点，避免引入整套 `googleapis`；响应在边界严格校验 |
| 定时任务 | Croner | 单进程定时任务，避免额外基础设施 |
| 写入队列 | p-queue | 把所有账本写入限制为并发数 1 |
| WebSocket | @fastify/websocket | 与现有 Fastify 服务集成 |
| 日志 | Pino | 结构化日志、字段脱敏和请求关联 |
| Agent 工具 | MCP TypeScript SDK + 内部 Tool Registry | 统一只读工具定义并适配不同 Agent |
| 测试 | Vitest + fast-check | 单元、集成、属性测试和迁移对照 |

不选择 NestJS、PostgreSQL、Redis、消息队列和独立 Python 服务。若未来产品边界变为公开多租户 SaaS，再依据真实瓶颈重新评估，而不是提前增加运维负担。

### 6.3 精确数值规范

- 金额、数量、成本、价格、收益率在领域层统一使用 `Decimal`。
- DuckDB 使用固定精度 `DECIMAL`，精度和小数位沿用并验证现有 Schwab 项目的实际数据需求。
- REST 和 WebSocket 中的精确数值统一序列化为十进制字符串。
- Flutter 使用十进制定点类型或受控字符串解析，不用 `double` 执行金融计算。
- 仅图表像素坐标可以使用浮点数；图表数值来源必须先由精确值转换并保留展示口径。

## 7. 建议目录结构

```text
Lyra/
├── apps/
│   └── mobile/                 Flutter 应用
├── server/
│   └── src/
│       ├── api/                路由、协议、认证、错误映射
│       ├── application/        用例与事务边界
│       ├── domain/
│       │   ├── accounts/       账户与账户范围
│       │   ├── ledger/         标准交易、去重、账本版本
│       │   ├── fifo/           FIFO 批次与已实现盈亏
│       │   ├── analytics/      报表和风险分析
│       │   └── agents/         工具、会话和事件模型
│       ├── infrastructure/
│       │   ├── db/             DuckDB schema、迁移、仓储
│       │   ├── imports/        CSV、Excel、MIME 适配器
│       │   ├── gmail/          OAuth、history、消息读取
│       │   ├── agents/         Claude、Pi、OpenCode 适配器
│       │   ├── crypto/         令牌加密与密钥加载
│       │   └── jobs/           调度、队列和进度事件
│       ├── config/             严格配置 schema
│       └── bootstrap/          服务启动和安全关闭
├── contracts/                  OpenAPI、事件 schema、错误码
├── fixtures/                   已匿名化的对照样本
├── docs/
│   ├── plans/                  方案文档
│   ├── adr/                    架构决策记录
│   └── runbooks/               部署、备份、恢复、重同步
└── scripts/                    开发和验证脚本
```

目录是目标结构，不表示当前已经创建这些工程。实现时先建立最小骨架，再按迁移阶段增加模块。

## 8. 核心领域模型

### 8.1 账户

`accounts` 表示 Schwab 券商账户，不表示 Lyra 登录用户。

建议字段：

| 字段 | 含义 |
| --- | --- |
| `id` | Lyra 内部不可变 UUID |
| `display_name` | 用户自定义名称 |
| `legacy_account_key` | 迁移自 Python 项目的账户键 |
| `broker` | 第一阶段固定为 Schwab，但不写死在业务分支中 |
| `masked_suffix` | 用于显示和邮件匹配的脱敏后缀 |
| `base_currency` | 账户基础币种 |
| `status` | active、archived |
| `created_at`、`updated_at` | UTC 时间戳 |

关键约束：

- `masked_suffix` 不是全局唯一身份凭据。
- 两个账户后缀相同或不足以确定账户时，导入必须要求用户选择。
- 已有关联交易的账户不可硬删除，只能归档。
- “全部账户”是查询范围，不是一条伪账户记录。

### 8.2 导入文件与作业

`import_files` 保存文件摘要和处理状态，不长期保存原始文件。

建议状态：

```text
uploaded → parsing → preview_ready → committing → committed
                  └→ rejected
         └→ failed
```

建议字段包括文件哈希、原始文件名、类型、大小、解析器版本、目标账户、记录数、错误摘要、账本前后版本和审计时间。

### 8.3 标准交易

`transactions` 是唯一标准交易事实表。Excel、CSV 和 Gmail 只能通过标准化器写入该表，不能各自维护独立交易模型。

核心信息包括：

- 账户 ID。
- 来源类型与来源引用。
- 交易日期和可用的成交时间。
- 原始动作与标准动作。
- 标的、期权合约结构、数量、价格、费用和净额。
- 原始行序号或消息定位信息。
- 规范化哈希。
- 解析器版本与账本版本。

约束：

- 同一账户内规范化哈希唯一，保证重复导入幂等。
- 原始内容与标准内容可追溯，但不长期保存完整原始邮件。
- 日期、时区和交易顺序规则必须与 Python 版本对照验证。
- 未知动作不得映射为通用动作继续计算。

### 8.4 FIFO 批次与已实现结果

`lots` 保存仍开放或已部分消耗的批次，`realized` 保存每次匹配形成的已实现结果。

必须保持以下不变量：

- 每个已实现结果都能定位到开仓交易和闭仓交易。
- 批次数量、成本和费用拆分使用精确十进制。
- 部分平仓后的剩余批次准确可复算。
- 重建前后结果由同一组标准交易确定，不依赖处理时的偶然顺序。
- 期权合约、到期、指派和行权规则必须按现有 Python 行为迁移，不在迁移中自行重定义。

### 8.5 Gmail 连接

建议拆分为：

- `gmail_connections`：Google 账户标识、加密 refresh token、授权范围、同步游标、状态。
- `gmail_account_mappings`：邮箱中识别出的账户线索到 Lyra 账户 ID 的明确映射。
- `gmail_messages`：Google message ID、thread ID、内容哈希、解析状态、错误码和关联交易；不长期存储完整正文。

### 8.6 Agent 会话

建议数据包括：

- `agent_sessions`：当前 Agent、账户范围、账本版本、状态和会话摘要。
- `agent_messages`：规范化后的用户、助手和工具消息。
- `agent_tool_calls`：工具名、参数摘要、结果摘要、授权结论、耗时和账本版本。
- `audit_events`：配置、导入、授权、同步、Agent 工具和敏感操作审计。

## 9. Excel/CSV 导入设计

### 9.1 支持范围

第一阶段支持：

- `.xlsx`：用户要求的初始 Excel 导入格式。
- `.csv`：继续兼容现有 Schwab Python 项目的官方导出格式。

第一阶段拒绝：

- 旧式 `.xls`。
- 含宏的 `.xlsm`。
- 加密或密码保护工作簿。
- 交易区域含公式的单元格。
- 隐藏的交易行或无法明确识别的工作表。
- 未知或模糊表头。

Excel 适配器只负责把工作表转换为规范化输入行。字段校验、动作解析、期权解析、Decimal 转换和哈希逻辑必须与 CSV 共享，禁止产生两套金融规则。

### 9.2 导入流程

1. App 选择文件并进行扩展名、大小和基本 MIME 检查。
2. App 通过上传接口发送文件，显示真实上传进度。
3. 服务端写入受限临时目录，计算文件摘要并创建导入作业。
4. 解析器识别格式和工作表；存在多个候选工作表时停止并要求用户选择。
5. 标准化器对所有行执行完整校验，不写账本。
6. 服务端返回预览：账户线索、日期范围、记录统计、重复统计、动作分布、错误定位和预计影响。
7. 用户明确选择或确认目标账户。
8. 用户点击提交后，服务端在单写队列中再次校验导入版本和账户状态。
9. 所有记录以原子方式写入，生成新账本版本并重建受影响账户的 FIFO 与分析结果。
10. 服务端记录审计事件并删除临时原始文件。

### 9.3 预览原则

预览不是宽松导入。它必须准确展示：

- 可接受记录数。
- 文件内重复数。
- 与账本已有记录重复数。
- 阻断性错误及精确行、列、字段。
- 未知动作或无法解析的期权符号。
- 账户匹配结果和歧义。
- 提交后会更新的账户与日期范围。

只要存在阻断性错误，提交按钮不可用。系统不得提供“忽略错误行继续”作为默认选项。

### 9.4 幂等与版本

- 文件哈希用于识别完全相同的文件。
- 交易规范化哈希用于识别跨文件重复记录。
- 再次提交相同数据应产生明确的“无新增交易”结果，而不是报未知错误。
- 解析器规则变化必须提升 `parser_version`。
- 每次成功账本变更生成递增 `ledger_version`。
- 预览必须绑定文件哈希、解析器版本和预览版本；提交时任一项变化都必须重新预览。

## 10. Gmail 授权与同步

### 10.1 授权边界

Gmail 连接和 Lyra 登录是两个独立概念。用户可以使用 Lyra 而不连接 Gmail，也可以断开 Gmail 而不影响已导入的账本数据。

业务数据的最小可行权限为 `gmail.readonly`，身份确认另需标准 OpenID Connect 的 `openid` 与 `email` scope。`gmail.readonly` 属于 Google Restricted Scope。公开发布前通常需要完成相应 OAuth 应用验证，并根据数据处理方式评估额外安全要求。第一阶段默认用于私有、自托管部署，仍需配置正确的 Google Cloud 项目、OAuth 同意屏幕、测试用户和隐私说明。

Google 官方参考：

- [Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [Google OAuth for native apps](https://developers.google.com/identity/protocols/oauth2/native-app)
- [OAuth best practices](https://developers.google.com/identity/protocols/oauth2/resources/best-practices)

### 10.2 推荐授权流程

1. App 请求服务端创建一次性授权事务，取得 `state`、PKCE 关联信息和授权配置。
2. Android 使用 Google 官方授权能力拉起系统授权体验，不使用嵌入式 WebView。
3. 用户选择 Google 账户并确认 `openid`、`email` 与 Gmail 只读权限；服务端通过 Google userinfo 验证邮箱身份。
4. App 获得一次性 server auth code，并通过已认证 HTTPS 通道发送给服务端。
5. 服务端校验授权事务、`state`、过期时间和设备身份，再交换 access token 与 refresh token。
6. 服务端使用 AES-GCM 加密 refresh token，只保存必要授权元数据。
7. 服务端立即验证实际授予的 scope；若缺少必要权限则失败，不尝试降级到不可用模式。
8. 首次同步只扫描明确的 Schwab 查询范围，并让用户确认账户映射。

离线访问使服务端可以在用户不打开 App 时刷新访问令牌并执行增量同步。授权码、access token 和 refresh token 不写入日志、审计详情或 Agent 上下文。

### 10.3 邮件搜索与解析

同步逻辑分为三层：

1. Gmail 查询层：按已确认的 Schwab 发件人、主题和时间范围定位候选消息。
2. MIME 层：解析 multipart、text/plain、text/html、编码和附件元数据。
3. Schwab eConfirm 层：用确定性规则提取交易字段并生成标准交易候选。

发件人和主题白名单必须通过样本验证后固化为版本化规则。邮件正文属于不可信外部输入，即使来自看似合法的发件人，也不能直接作为 Agent 指令。

### 10.4 增量同步

- 首次同步：用户选择一个有上限的起始日期，执行有界扫描。
- 后续同步：保存 Gmail history 游标，优先读取增量变更。
- 默认计划：每小时同步一次，并支持手动同步。
- 同一 Google message ID 和内容哈希保持幂等。
- 消息解析失败时记录稳定错误码并显示可操作原因。
- history 游标无效或过期时，连接进入 `needs_resync`。
- 用户确认新的日期范围后才能执行有界重扫，不能静默全邮箱扫描。

### 10.5 Gmail 数据覆盖限制

Schwab eConfirm 通常只覆盖成交确认，不能被视为完整券商账单。它可能不完整覆盖：

- 股息和利息。
- 资金转入、转出和内部划转。
- 税费和其他费用调整。
- 期权到期、指派和行权的完整账务结果。
- 券商侧更正或历史调整。

因此，Lyra 必须保留官方 Excel/CSV 定期对账流程。提醒按账户配置，默认每个自然月提示一次用户重新导入官方数据；用户可以关闭或调整周期，但系统不能声称 Gmail 已使账本完全自动化。

### 10.6 断开与删除

断开 Gmail 时提供两个明确动作：

- 断开连接：停止同步并删除本地 refresh token，保留已标准化交易和审计记录。
- 断开并请求撤销：除本地删除外，调用 Google 撤销授权；失败时必须显示失败，不假定远端已经撤销。

完整邮件正文和原始 MIME 仅在受控临时处理期存在，解析完成或失败归档后按策略清除。

## 11. 多账户设计

### 11.1 账户范围

所有查询和 Agent 工具必须显式携带 `account_scope`：

- 单个账户 UUID。
- `all`，表示用户主动选择的全部活跃账户。

服务端不提供“缺省即全部账户”的隐式行为。App 可以记住用户最近选择，但每个页面都要可见展示当前范围。

### 11.2 App 账户切换器

账户切换器位于总览页面的核心视觉区，并同步影响：

- 总览。
- 持仓。
- 分析。
- 流水。
- Agent 新会话上下文。

已有 Agent 会话切换账户范围时，需要创建明确的上下文变更消息并生成新的工具查询基线，不能悄悄改变此前回答的含义。

### 11.3 聚合规则

- 同一标的跨账户可以在 `all` 范围内汇总数量，但批次和已实现结果仍保留账户边界。
- 不在不同账户之间做 FIFO 批次抵消。
- 不把一个账户的现金流用于另一个账户的收益解释。
- 基础币种不同的账户在没有汇率源时不能直接合并为一个金额；第一阶段建议只允许同币种聚合，异币种时分组显示并阻止伪总计。
- 账户归档后历史数据仍可查询，但默认不进入活跃账户聚合。

## 12. FIFO 与分析迁移范围

### 12.1 必须迁移的既有能力

根据 Schwab Python 项目现有 CLI，TypeScript 版本应完整覆盖：

- `summary`
- `positions`
- `expiring`
- `audit`
- `monthly`
- `allocation`
- `attribution`
- `drawdown`
- `risk`
- `holding-period`
- `extremes`
- `streaks`
- `stress`
- `realized`
- `trades`
- `cashflow`

迁移目标不是命令行界面一致，而是输入、计算语义、输出字段、Decimal 精度、排序、错误条件和边界行为一致。

### 12.2 计算口径展示

每个分析页面必须提供简短但明确的“口径”入口，至少说明：

- 数据范围和截止时间。
- 是否只基于已实现结果。
- 是否包含费用。
- 多账户如何聚合。
- 缺失哪些数据源。
- 结果是否依赖外部行情。

例如，没有行情源时不能展示名为“当前总市值”的指标。可以展示持仓数量、历史成本、已实现盈亏和基于导入数据的统计，但名称必须准确。

### 12.3 未来行情扩展点

未来若需要当前市值和未实现盈亏，应增加独立的 `MarketDataProvider` 边界，并明确：

- 价格来源和授权。
- 交易时段、延迟和时间戳。
- 拆股、币种和期权报价处理。
- 缓存与过期策略。
- 价格不可用时的显式状态。

第一阶段不预装具体行情供应商，也不让 Agent 自行联网搜索价格后混入权威账本结果。

## 13. AI Agent 架构

### 13.1 目标

Agent 用于解释和探索账本，不是权威计算引擎。权威数字必须来自确定性金融工具；模型负责提出查询、组织说明和比较结果。

### 13.2 运行模型

```text
Flutter Agent UI
    │
    ├── 创建会话 / 发消息 / 切换 Agent / 取消
    └── 消费统一 WebSocket 事件
    │
Agent Orchestrator
    ├── Normalized Session Store
    ├── Financial Tool Registry
    ├── Permission Policy
    ├── Claude Adapter   → Claude CLI 子进程
    ├── Pi Adapter       → Pi RPC/SDK 子进程
    └── OpenCode Adapter → ACP/Headless 子进程
```

服务端是唯一 Agent 编排位置。Flutter 不直接持有模型供应商密钥，也不在手机端尝试运行完整编码 Agent。这满足“服务器与 App 分开”：App 与 Agent 实现通过统一协议解耦，未来可新增其他运行位置，但第一阶段不增加双运行时复杂度。

### 13.3 统一适配器契约

每个 Agent 适配器提供以下能力：

- `probe`：检查二进制、版本、认证和必要扩展是否可用。
- `start`：创建受限会话。
- `send`：发送规范化消息与工具上下文。
- `cancel`：取消当前回合。
- `resume`：在同一 Agent 能力允许时恢复原生会话。
- `handoff`：接收规范化历史并开始新 Agent 会话。
- `dispose`：终止子进程和清理临时工作目录。

具体适配思路：

| Agent | 首选接口 | Lyra 约束 |
| --- | --- | --- |
| Claude | stream-json、会话能力、MCP | 严格工具允许列表，禁用文件和 Shell 能力 |
| Pi | RPC/SDK + Lyra 扩展 | 扩展只注册 Lyra 工具，关闭写文件、编辑和通用 Bash |
| OpenCode | ACP 或 headless JSON + MCP | 独立会话目录，只提供 Lyra MCP 工具 |

如果某个 Agent 缺少二进制、未登录或扩展版本不匹配，接口返回 `unavailable` 和具体原因。系统不得自动用另一个 Agent 替代。

### 13.4 统一事件模型

WebSocket 事件建议统一为：

- `session.started`
- `message.started`
- `message.delta`
- `reasoning.delta`
- `tool.started`
- `tool.completed`
- `approval.required`
- `usage.updated`
- `message.completed`
- `message.failed`
- `session.completed`

所有事件携带 `session_id`、`message_id`、单调递增序号和时间戳。App 遇到序号间隙时必须重新获取会话快照，而不是猜测缺失内容。

### 13.5 无缝切换的准确含义

“无缝切换”定义为在完整回合边界进行：

1. 当前 Agent 完成或用户取消当前回合。
2. Orchestrator 固化规范化对话、工具调用、账户范围、账本版本和已确认偏好。
3. 新 Agent 接收经过长度控制的上下文、结构化工具结果和会话摘要。
4. 新 Agent 以明确系统消息告知切换来源和当前数据基线。
5. App 在同一会话视觉流中继续展示，但保留 Agent 切换标记。

不能迁移的内容包括模型隐藏推理、供应商内部缓存和原生未公开状态。文案不能承诺这些内容被迁移。

### 13.6 只读金融工具

第一阶段允许 Agent 调用：

- accounts
- summary
- positions
- expiring
- trades
- realized
- cashflow
- monthly
- allocation
- attribution
- drawdown
- risk
- holding-period
- extremes
- streaks
- stress
- audit

工具定义只存在一份，由内部 `Tool Registry` 同时生成 MCP 暴露、直接适配调用和审计元数据，避免为三个 Agent 分别维护逻辑。

### 13.7 禁止能力

Agent 不允许：

- 导入或提交文件。
- 连接、断开或重同步 Gmail。
- 新建、修改、归档或删除账户。
- 修改或删除交易与批次。
- 执行 SQL、Shell、网络抓取或任意文件访问。
- 读取完整原始邮件或 OAuth 令牌。
- 改变全局安全策略。
- 调用任何实盘交易能力。

### 13.8 金融回答协议

只要回答中包含金融数字，Agent 必须同时给出：

- 账户范围。
- 数据截止时间。
- 账本版本。
- 计算口径。
- 使用的工具。

Agent 不得把历史成本称为当前市值，不得把仅基于已实现交易的回撤描述为账户净值回撤。若工具无法支持用户问题，Agent 应说明缺少的数据，而不是估算后伪装为账本结果。

### 13.9 Prompt Injection 防护

- 邮件正文先经确定性解析器转为标准交易，原文不进入 Agent 上下文。
- 工具参数由 schema 校验，账户范围由服务端授权策略覆盖校验。
- 工具输出标记为数据，不作为系统指令再解释。
- Agent 子进程在专用空工作目录运行，不能访问项目、数据库文件或环境变量。
- 日志和错误信息在进入模型前清理路径、令牌、邮箱和内部堆栈。

## 14. API 设计

### 14.1 协议原则

- 所有接口使用 `/v1` 版本前缀。
- 日期使用 ISO 8601；时间戳保存和传输为 UTC。
- 精确数值使用字符串。
- 每个请求带 `request_id`，每个长任务带 `job_id`。
- 分页使用稳定 cursor，不使用易受插入影响的页码。
- 修改接口支持幂等键。
- App 与服务端建立兼容性握手；不兼容版本明确阻断，不静默忽略字段。

### 14.2 路由范围

账户：

- `GET /v1/accounts`
- `POST /v1/accounts`
- `PATCH /v1/accounts/:accountId`
- `POST /v1/accounts/:accountId/archive`

导入：

- `POST /v1/imports/preview`
- `GET /v1/imports/:importId`
- `POST /v1/imports/:importId/commit`
- `POST /v1/imports/:importId/cancel`

Google 与 Gmail：

- `POST /v1/google/authorization-transactions`
- `POST /v1/google/authorization-transactions/:id/complete`
- `GET /v1/google/connections`
- `POST /v1/google/connections/:id/sync`
- `DELETE /v1/google/connections/:id`
- `GET /v1/google/connections/:id/mappings`
- `PUT /v1/google/connections/:id/mappings/:mappingId`

账本：

- `GET /v1/ledger/summary`
- `GET /v1/ledger/positions`
- `GET /v1/ledger/expiring`
- `GET /v1/ledger/trades`
- `GET /v1/ledger/realized`
- `GET /v1/ledger/cashflow`
- `GET /v1/ledger/audit`

分析：

- `GET /v1/analytics/monthly`
- `GET /v1/analytics/allocation`
- `GET /v1/analytics/attribution`
- `GET /v1/analytics/drawdown`
- `GET /v1/analytics/risk`
- `GET /v1/analytics/holding-period`
- `GET /v1/analytics/extremes`
- `GET /v1/analytics/streaks`
- `GET /v1/analytics/stress`

Agent：

- `GET /v1/agents`
- `POST /v1/agent/sessions`
- `GET /v1/agent/sessions/:sessionId`
- `POST /v1/agent/sessions/:sessionId/messages`
- `POST /v1/agent/sessions/:sessionId/switch`
- `POST /v1/agent/sessions/:sessionId/cancel`
- `DELETE /v1/agent/sessions/:sessionId`
- `GET /v1/events`，升级为 WebSocket。

### 14.3 错误模型

错误响应包含：

| 字段 | 含义 |
| --- | --- |
| `request_id` | 关联日志和审计 |
| `code` | 稳定、可供 App 分支处理的错误码 |
| `message` | 面向用户的安全说明 |
| `details` | 结构化字段错误或可操作信息 |
| `retryable` | 是否可在相同条件下重试 |

不返回堆栈、数据库路径、临时目录、Agent 命令行、令牌或服务端环境信息。

建议稳定错误码包括：

- `IMPORT_UNSUPPORTED_FORMAT`
- `IMPORT_UNKNOWN_HEADERS`
- `IMPORT_AMBIGUOUS_SHEET`
- `IMPORT_VALIDATION_FAILED`
- `IMPORT_PREVIEW_STALE`
- `IMPORT_STATE_CONFLICT`
- `ACCOUNT_MAPPING_REQUIRED`
- `ACCOUNT_SCOPE_REQUIRED`
- `GMAIL_SCOPE_MISSING`
- `GMAIL_AUTH_REVOKED`
- `GMAIL_HISTORY_EXPIRED`
- `GMAIL_RESYNC_CONFIRMATION_REQUIRED`
- `GMAIL_CONNECTION_ERROR`
- `GMAIL_MESSAGE_CONFLICT`
- `GMAIL_MESSAGE_REJECTED`
- `GOOGLE_AUTH_TRANSACTION_INVALID`
- `GOOGLE_AUTH_TRANSACTION_EXPIRED`
- `GOOGLE_AUTH_TRANSACTION_USED`
- `LEDGER_VERSION_CONFLICT`
- `AGENT_UNAVAILABLE`
- `AGENT_SWITCH_REQUIRES_TURN_BOUNDARY`
- `AGENT_HANDOFF_CONTEXT_TOO_LARGE`
- `AGENT_SESSION_STATE_CONFLICT`
- `AGENT_TURN_FAILED`
- `AGENT_TURN_CANCELLED`
- `AGENT_TOOL_FORBIDDEN`
- `AGENT_TOOL_EXECUTION_FAILED`
- `AGENT_TOOL_CANCELLED`
- `AGENT_PROTOCOL_ERROR`
- `PAIRING_CODE_INVALID`
- `PAIRING_CODE_EXPIRED`
- `PAIRING_CODE_USED`
- `DEVICE_PUBLIC_KEY_INVALID`
- `DEVICE_CREDENTIAL_INVALID`
- `DEVICE_CREDENTIAL_EXPIRED`
- `DEVICE_CREDENTIAL_REUSED`
- `DEVICE_REVOKED`
- `IDEMPOTENCY_KEY_REUSED`
- `IDEMPOTENCY_REQUEST_IN_PROGRESS`
- `CLIENT_VERSION_INCOMPATIBLE`

## 15. Flutter 信息架构

### 15.1 主导航

底部固定五个一级入口：

1. 总览
2. 持仓
3. 分析
4. 流水
5. Agent

账户管理、数据导入、Gmail 连接、安全、同步状态和服务端信息进入账户中心，不占用底部主导航。

### 15.2 页面职责

#### 总览

- 大型账户范围切换器。
- 已实现结果、现金流、持仓成本等口径明确的核心指标。
- 数据截止时间和账本状态。
- 最近导入、Gmail 同步和需要处理的异常。
- 分析入口和 Agent 快捷提问。

#### 持仓

- 按标的分组的数量、成本和批次信息。
- 期权合约和到期风险。
- 单账户与全部账户切换。
- 无行情时不展示伪市值和伪实时涨跌。

#### 分析

- 月度、配置、归因、回撤、风险、持有期、极值、连胜连亏、压力测试。
- 每张图可查看计算口径、数据范围和缺失数据说明。
- 图表与数值均可进入关联交易明细。

#### 流水

- 交易和现金流时间线。
- 按账户、日期、动作、标的和来源过滤。
- 显示来源为 Excel/CSV 或 Gmail，并可查看审计信息。
- 不提供随意编辑账本记录；纠错通过重新导入、明确更正流程或未来专门机制完成。

#### Agent

- 当前 Agent 和可用状态。
- 当前账户范围、账本版本和数据截止时间。
- 流式回答、工具执行卡片、错误和取消。
- 回合边界切换 Claude、Pi、OpenCode。
- 对每个金融数字提供来源工具与口径入口。

#### 账户中心

- Schwab 账户列表和归档状态。
- Excel/CSV 导入与历史。
- Google 连接、权限、账户映射和同步历史。
- 设备配对、生物识别、服务端证书和会话管理。
- 数据备份、恢复状态和审计导出。

## 16. 视觉设计系统

### 16.1 设计方向

唯一参考采用 Revolut 风格：黑白双画布、钴蓝紫品牌色、超大文字、宽松留白、圆角卡片、产品化摄影或抽象视觉、强烈但克制的过渡。

Lyra 不直接复制 Revolut 商标、页面或素材，而是抽取其设计语法并建立独立品牌：

- 以巨大账户数字和分析标题建立视觉冲击。
- 使用黑色沉浸区与暖白信息区形成节奏。
- 以钴蓝紫表达品牌、交互焦点和 Agent 能量状态。
- 以更克制的语义色表达盈利、亏损、风险和警告。
- 通过空间、动效和字体塑造高端感，而不是堆叠玻璃拟态和装饰。

### 16.2 基础色

| Token | 建议值 | 用途 |
| --- | --- | --- |
| `brand.primary` | `#494FDF` | 主品牌色、主按钮、焦点 |
| `brand.bright` | `#4F55F1` | 高光和 Agent 动态层 |
| `brand.deep` | `#3A40C4` | 按压、深色背景上的层次 |
| `surface.black` | `#000000` | 沉浸式首页和过渡 |
| `surface.deep` | `#0A0A0A` | 深色页面底层 |
| `surface.elevated` | `#16181A` | 深色卡片 |
| `surface.white` | `#FFFFFF` | 主要浅色表面 |
| `surface.warm` | `#F4F4F4` | 柔和信息背景 |
| `text.ink` | `#191C1F` | 浅色主文本 |

金融语义色独立于品牌色：盈利、亏损、警告、风险和中性状态必须通过可访问性对比度验证，不能只依靠红绿区别。

### 16.3 字体

- 品牌与界面：Geist Sans。
- 金额、数量、代码和时间：Geist Mono。
- 首页主数字可在手机断点使用约 56–72sp 的流体尺度。
- 分析章节标题可使用 40–56sp，并根据窗口宽度缩放。
- 正文不低于可访问性建议尺寸，支持系统字体缩放。
- 数字采用等宽或 tabular figures，防止滚动更新时跳动。

### 16.4 图标规范

- 全部使用 Material Icons Outlined。
- 建立 `LyraIcons` 语义映射层，业务代码不得散落直接图标名称。
- 常规图标使用 20 或 24dp。
- 触摸目标至少 48dp。
- 同一语义在全 App 使用同一图标。
- 不混入 Lucide、Font Awesome、Cupertino Icons 或自定义风格不一致的线性图标。
- 不使用表情符号充当图标、状态、提示或装饰。

### 16.5 版式与组件

- 采用不对称但有基线秩序的网格。
- 卡片圆角建议分为 16、24、32 三档，避免每个组件任意取值。
- 主按钮为胶囊或大圆角矩形，次按钮使用轮廓或文本形式。
- 金融表格在手机上转换为分层行卡，不横向塞入过多列。
- 大数字与小型口径标签必须成组出现。
- 导入预览、错误列表和交易明细使用更高密度的编辑式排版。
- 不使用无意义渐变边框、过度玻璃模糊和发光描边。

### 16.6 动效

高表现区域：

- 启动与解锁。
- 首页账户切换。
- 总览主数字更新。
- 分析章节进入。
- Agent 思考与工具调用状态。
- 导入完成和账本版本切换。

克制区域：

- OAuth 同意说明。
- 账户映射。
- 金额和数据提交确认。
- 错误详情。
- 交易流水和审计。
- 删除、断开和归档。

动效原则：

- 使用基于弹簧的位移、缩放和透明度，而不是统一线性缓动。
- 账户切换可使用轻量 Z 轴和共享元素，但不能导致数字含义短暂混淆。
- 数字滚动只在值真正变化时发生。
- 列表滚动期间避免大面积实时模糊。
- 优先使用 transform、opacity 和可控 shader。
- 支持系统“减少动态效果”，关闭视差、粒子和大幅缩放。
- 关键确认使用系统触觉反馈，不使用声音或表情符号替代状态。

### 16.7 可访问性

- 所有文本和关键图形满足对比度要求。
- 盈亏不能只靠红绿，需同时使用符号、标签或图形方向。
- 图表提供文本摘要和可聚焦数据点。
- 支持大字体、屏幕阅读器和横屏。
- 导航顺序与视觉顺序一致。
- 动效、震动和生物识别均尊重系统设置。

## 17. 响应式与 Android 适配

Flutter 虽以手机为主，仍需覆盖：

- 小屏手机。
- 常规手机。
- 大屏手机和折叠屏。
- 横屏。
- Android 状态栏、导航栏和手势区域。

布局策略：

- 小屏使用单列和更紧凑的主数字尺度。
- 大屏手机允许两列分析卡片，但不缩小触摸目标。
- 折叠屏展开态把主导航转为 Navigation Rail，并让详情与列表并列。
- 避免以固定像素定位构建核心页面。
- 所有全屏视觉层尊重安全区域和系统栏对比度。

正式实施 Android 工程时，构建、设备、布局验证和调试应遵循项目约定，通过 `android` CLI 流程执行。

## 18. 本地缓存与离线行为

### 18.1 缓存原则

- Drift/SQLite 只保存最近查询结果、会话列表和必要 UI 状态。
- 缓存数据必须携带 `ledger_version` 和 `fetched_at`。
- App 离线时可以查看已缓存数据，但顶部持续显示离线和数据截止时间。
- 离线时禁止提交导入、Google 授权、同步、Agent 消息和账户变更。
- 网络恢复后先验证服务端身份和账本版本，再刷新数据。

### 18.2 缓存敏感性

- 不缓存原始邮件、OAuth 令牌或 Agent 密钥。
- 可配置隐藏通知预览和最近任务内容。
- 用户退出配对或撤销设备时清除本地数据库密钥和敏感缓存。
- Android 最近任务缩略图默认遮罩金额；用户可在安全设置中调整，但初始值保持保护。

## 19. 安全方案

### 19.1 App 与服务端认证

第一阶段推荐设备配对，而不是用 Gmail 身份兼任 Lyra 登录：

1. 服务端生成短时、一次性配对码。
2. App 在同一安全网络内提交配对码和设备公钥。
3. 服务端登记设备并签发短期访问令牌与可轮换刷新凭据。
4. 刷新凭据存入 Android Keystore 支持的安全存储。
5. App 每次进入敏感页面前可触发生物识别或系统凭据验证。
6. 服务端可以单独撤销某台设备。

配对码不能写入静态配置、镜像或仓库，也不能长期有效。

### 19.2 传输安全

- 正式环境只允许 HTTPS/WSS。
- 推荐通过 Tailscale 私网访问，或使用 Caddy 提供可信 TLS。
- App 默认拒绝无效证书和明文 HTTP。
- 开发模式如需明文，只能通过明确的构建配置启用，不能自动回退。
- 所有敏感修改接口检查认证、设备状态、CSRF/重放风险和幂等键。

### 19.3 服务端密钥与令牌

- Gmail refresh token 使用 AES-GCM 加密。
- 主加密密钥通过环境注入或 Docker secret 提供，不写入数据库或 Git。
- Agent CLI 凭据由服务器主机自身安全配置管理，Lyra 数据库不复制供应商密钥。
- Pino 日志对 Authorization、Cookie、OAuth code、token、邮箱和账户标识脱敏。
- 服务启动时缺少关键密钥立即失败，不生成临时弱密钥继续运行。

### 19.4 文件处理

- 上传大小和解压后尺寸均有限制，防止压缩炸弹。
- Excel ZIP 内容检查条目数量、路径和总体积。
- 临时文件位于专用目录，权限最小化。
- 文件名只用于展示，不直接拼接文件系统路径。
- 解析完成后删除临时文件；删除失败记录阻断性运维告警。
- 不执行宏、公式、嵌入对象或外部链接。

### 19.5 审计

必须审计：

- 设备配对、刷新、撤销和认证失败。
- 账户创建、重命名和归档。
- 文件上传、预览、提交、拒绝和删除。
- Gmail 授权、同步、重同步、映射、断开和撤销。
- 账本重建和版本变化。
- Agent 会话、切换、工具调用、拒绝和异常。
- 备份、恢复和完整性检查。

审计日志不保存完整邮件、令牌、Agent 隐藏推理或不必要的个人数据。

### 19.6 威胁与控制

| 威胁 | 主要控制 |
| --- | --- |
| 恶意 Excel | 类型/大小限制、ZIP 检查、禁公式宏、严格 schema、临时隔离 |
| 重复或冲突交易 | 文件哈希、交易哈希、原子提交、账本版本 |
| 错误账户映射 | 明确映射、歧义阻断、账户范围可见 |
| Gmail token 泄漏 | 服务端加密、日志脱敏、最小 scope、可撤销 |
| 邮件 prompt injection | 确定性解析、原文不进 Agent、工具 schema |
| Agent 越权 | 空工作目录、禁 Shell/文件/网络、只读工具、审计 |
| App 丢失 | 生物识别、Keystore、设备撤销、最近任务遮罩 |
| 服务端损坏 | DuckDB 备份、校验、恢复演练、单写协调 |
| 结果误读 | 账户范围、截止时间、版本和口径常驻显示 |

## 20. 部署与运维

### 20.1 默认部署

第一阶段推荐：

- 一台用户控制的服务器、NAS 或常开电脑。
- 一个 Lyra Docker 容器，内部运行单 Node.js 服务。
- 一个持久卷保存 DuckDB、审计和必要状态。
- 通过 Tailscale 访问；若公开域名接入，则由 Caddy 提供 TLS 和反向代理。
- Claude、Pi、OpenCode CLI 安装和认证位于同一主机，并被 Lyra 以受限子进程调用。

Caddy 不承载业务状态；它是可选边缘组件，不违反“只保留一个服务端”的产品架构要求。

### 20.2 配置

配置由严格 schema 校验，建议分类：

- 服务监听地址和公开基础 URL。
- DuckDB 路径。
- 加密密钥来源。
- Google OAuth client 配置。
- Gmail 同步频率和默认有界扫描范围。
- 上传限制和保留策略。
- Agent 二进制路径、允许版本和超时。
- 日志级别与脱敏策略。

缺少必填配置时启动失败。敏感配置不输出到错误页面，也不写入仓库。任何 Git 操作都必须排除 `application-gin.yml`。

### 20.3 备份与恢复

- 备份前通过写协调器创建一致性检查点。
- 默认执行每日加密备份，并保留可配置的轮换周期。
- 备份包括 DuckDB 和必要的版本化配置元数据，不包括临时上传和 Agent 子进程目录。
- 恢复先在隔离位置校验，再替换运行数据库。
- 恢复后强制执行 schema、账本哈希和 FIFO 一致性验证。
- Gmail token 是否进入备份由明确策略决定；若不备份，恢复后要求重新授权。

### 20.4 可观测性

- 结构化日志包含 `request_id`、`job_id`、`session_id` 和耗时。
- 指标包含 API 延迟、导入行数、Gmail 同步结果、写队列长度、Agent 启动与工具耗时。
- 健康检查区分进程存活、数据库可用、写队列状态和外部集成状态。
- Google 或 Agent 不可用不应让账本只读查询整体失效，但相关功能必须明确显示降级原因。
- 不因外部服务失败自动切换数据源或 Agent。

## 21. 性能目标

### 21.1 Flutter

- 常规设备至少稳定 60fps；支持高刷设备时目标 90/120fps。
- 列表虚拟化，图表只绘制可见数据。
- 大量交易记录使用 cursor 分页。
- 启动时不阻塞等待 Gmail 或 Agent 探测。
- 高级 shader 在支持设备上预热；不支持时采用同一设计语言的静态层，不改变业务含义。
- 动效性能不足时关闭非必要视觉层，并通过设备能力策略明确选择，不在运行中随机改变。

### 21.2 服务端

- 常见只读查询在目标数据规模内争取低于 200ms，不把该数值作为未经基准测试的硬保证。
- 导入预览和提交分离，避免长事务占用上传连接。
- Gmail 同步不阻塞账本读取。
- 所有写任务串行化，读任务使用稳定快照。
- Agent 流式 token 不逐 token 写数据库，按消息片段或完成事件批量持久化。
- 性能验收必须基于匿名化真实规模数据，而不是只用小型单元样本。

## 22. Python 到 TypeScript 的迁移方案

### 22.1 迁移原则

迁移采用“行为等价重写”，不是逐行翻译，也不是借机改变业务口径。Python 项目在迁移完成前保持为只读事实参照和验证 Oracle。

主要来源映射：

| Python 来源 | TypeScript 目标 |
| --- | --- |
| `src/schwab/ingest.py` | `domain/ledger` + `infrastructure/imports` |
| `src/schwab/fifo.py` | `domain/fifo` |
| `src/schwab/email_ingest.py` | `infrastructure/imports/mime` + `infrastructure/gmail` |
| `src/schwab/db.py` | `infrastructure/db` |
| `src/schwab/cli.py` | `domain/analytics` + API use cases |
| `tests/` | Vitest 对照测试、golden fixtures、属性测试 |

### 22.2 分步迁移

#### 阶段 A：冻结行为基线

- 固定 Python 依赖和测试环境。
- 运行全部现有测试。
- 为每个 CLI JSON 输出建立 golden 文件。
- 收集匿名化真实 CSV、期权、部分平仓、重复导入和错误样本。
- 记录错误类型、消息语义、排序和 Decimal 字符串格式。

#### 阶段 B：迁移基础类型与标准化

- 金额、日期、动作、账户、期权符号和哈希。
- CSV 严格解析。
- Excel 适配器复用同一标准化器。
- 对未知动作、空值、非法 Decimal、表头、时区和编码建立对照测试。

#### 阶段 C：迁移 FIFO

- 开仓、加仓、部分平仓、完全平仓。
- 多批次匹配、费用分摊和精度。
- 股票和期权处理。
- 到期、指派、行权等现有行为。
- 批次和已实现结果逐行对比。

#### 阶段 D：迁移全部报表

- 逐个迁移现有 CLI 能力。
- 保持 JSON 字段、排序、空结果和错误行为一致。
- 为多账户增加显式范围，但单账户结果必须与 Python 对照一致。

#### 阶段 E：迁移邮件与 Gmail

- 先迁移现有 RFC MIME 和 eConfirm 确定性解析。
- 再增加 Google OAuth、Gmail API 和 history 增量同步。
- 邮件样本覆盖纯文本、HTML、multipart、编码和重复消息。
- Gmail 层只负责获取消息，不能改变邮件解析语义。

#### 阶段 F：切换与移除 Python

- App 和 API 只调用 TypeScript 实现。
- 运行双轨对照，不做双写生产账本。
- 满足全部迁移门禁后，才删除 Python 运行依赖。
- 保留匿名化 fixtures 和 golden 结果作为长期回归资产。

### 22.3 迁移完成门禁

只有同时满足以下条件，才允许宣布 Python 已被替代：

- 现有 Python 测试全部在 TypeScript 中有等价覆盖。
- 所有 golden JSON 输出匹配。
- 匿名化真实导入的交易记录逐行匹配。
- `lots` 和 `realized` 逐行匹配。
- 重复导入和幂等结果匹配。
- 错误输入的拒绝位置和错误语义匹配。
- Decimal 精度、日期、时区和排序匹配。
- Gmail MIME/eConfirm 样本匹配。
- 性能与内存达到目标规模要求。
- 备份恢复后重算结果一致。

不能因进度压力降低这些门禁，也不能在 Python 版本被删除后才发现缺少对照样本。

## 23. 测试策略

### 23.1 服务端测试

单元测试：

- Decimal、日期、期权符号和动作映射。
- 规范化哈希和幂等。
- FIFO 边界、部分匹配和费用分摊。
- 每个分析器的空数据、单笔和边界结果。
- 账户范围与权限。
- Agent 事件转换和工具策略。

属性测试：

- 导入顺序规范化后结果稳定。
- 重复导入不改变账本。
- FIFO 消耗数量不超过可用批次。
- 开放批次数量加已消耗数量守恒。
- 同一交易集合重建结果一致。

集成测试：

- DuckDB schema 和迁移。
- 导入预览、提交、冲突和取消。
- Gmail OAuth 回调、令牌刷新、history 过期和重同步确认。
- WebSocket 断线重连与事件序号。
- Claude、Pi、OpenCode 适配器使用受控假进程和契约测试。

安全测试：

- Excel 压缩炸弹、路径穿越、公式、宏和超大文件。
- OAuth state、授权码重放和 scope 缺失。
- Agent 工具越权、提示注入和敏感信息脱敏。
- 设备撤销后访问被拒绝。

### 23.2 Flutter 测试

- Riverpod 状态和 API 错误映射单元测试。
- 总览、导入、账户映射、Gmail、Agent 和安全流程 widget 测试。
- 关键页面 golden test，覆盖浅色、深色、小屏、大屏和大字体。
- 导入到分析的端到端测试。
- Agent 流式输出、取消、失败和切换的端到端测试。
- 离线、令牌过期、证书错误和服务端版本不兼容测试。
- Android 真机性能、折叠屏和减少动态效果测试。

### 23.3 不可替代的人工验收

- 对照 Schwab 官方数据核验金额和账户。
- 检查导入错误是否能被非开发者理解。
- 检查 Agent 是否准确表达数据口径。
- 检查页面在金额较大、负数、长标的名和多账户下的布局。
- 使用屏幕阅读器、大字体和减少动态效果完成主要流程。
- 执行一次真实备份和隔离恢复演练。

## 24. 分阶段交付计划

项目已按以下顺序开始实施，每个阶段仍须满足独立退出门禁；实际完成度以 `docs/progress.md` 为准。

### Phase 0：方案冻结与验证基线

交付物：

- 本文档评审通过。
- 产品范围和不做事项确认。
- Python 测试与 golden 基线。
- 匿名化导入和邮件样本。
- API、错误码和账本版本规则草案。

退出门禁：关键开放问题已有决定，Python 事实基线可重复运行。

### Phase 1：TypeScript 账本内核

交付物：

- 单服务骨架、严格配置和 DuckDB。
- CSV/Excel 预览与原子提交。
- FIFO 与核心报表迁移。
- 审计和备份基础。

退出门禁：单账户迁移对照全部通过，导入失败不会产生部分账本。

### Phase 2：Flutter 只读产品骨架

交付物：

- 设备配对和安全存储。
- 五个主导航页面。
- 多账户范围、总览、持仓、分析、流水。
- Revolut 方向设计 token、组件和关键动效。

退出门禁：手机端能安全查看完整账本，所有页面显示数据范围与版本。

### Phase 3：Gmail 自动增量

交付物：

- Google OAuth 授权。
- Gmail 有界首扫和 history 增量同步。
- 明确账户映射、错误处理和重同步流程。
- 每月官方文件对账提醒。

退出门禁：令牌安全、同步幂等、邮件解析对照和断开流程通过。

### Phase 4：AI Agent

交付物：

- 统一 Tool Registry。
- Claude、Pi、OpenCode 适配器。
- 统一流式事件和受限工具权限。
- 回合边界切换和规范化会话迁移。

退出门禁：三个 Agent 契约测试通过，无法越权，数字回答包含完整口径。

### Phase 5：视觉、性能和发布加固

交付物：

- 关键动效、shader、无障碍和多设备适配。
- 性能基准、安全测试、备份恢复和运维手册。
- Android 内部分发包。

退出门禁：功能、视觉、性能、安全和恢复验收全部完成。

### Phase 6：Python 退役

交付物：

- 最终双轨对照报告。
- Python 运行依赖移除。
- 长期 golden fixtures 和迁移记录。

退出门禁：第 22.3 节全部条件满足，并得到明确切换确认。

## 25. 验收标准

### 25.1 数据

- 支持 `.xlsx` 和 `.csv` 严格导入。
- 重复文件和重复交易幂等。
- 任一阻断错误不写入部分数据。
- 多账户映射不存在隐式猜测。
- FIFO、已实现结果和所有既有报表与 Python 基线一致。
- 每次变更生成可追溯账本版本。

### 25.2 Gmail

- 使用系统授权体验和服务端离线令牌交换。
- 只申请必要 scope，并校验实际授权。
- 支持首次有界扫描、增量同步和手动同步。
- history 失效必须由用户确认重扫范围。
- 不长期保存完整原始邮件。
- 明确提示 Gmail 不覆盖完整账务数据。

### 25.3 多账户

- 所有接口、页面和 Agent 工具显式使用账户范围。
- 跨账户不混合 FIFO 批次。
- 异币种在无汇率时不生成伪合计。
- 账户归档不破坏历史审计。

### 25.4 Agent

- Claude、Pi、OpenCode 状态可探测。
- 不可用时显示原因，不自动切换。
- 只在回合边界切换。
- 规范化消息、工具结果、账户范围和账本版本可迁移。
- 不能访问 Shell、SQL、原始邮件、数据库文件和敏感环境。
- 金融数字回答包含范围、时间、版本、口径和工具。

### 25.5 UI

- 视觉语言统一为 Revolut 方向。
- 全部图标来自 Material Icons Outlined 语义映射。
- 界面全程无表情符号。
- 关键金融页面优先可读和可审计。
- 支持大字体、减少动态效果和屏幕阅读器。
- 常规设备达到稳定 60fps。

### 25.6 安全与运维

- 正式环境只允许 HTTPS/WSS。
- 设备令牌保存在系统安全存储。
- Gmail refresh token 加密且可撤销。
- 日志和错误不泄漏敏感信息。
- 备份与恢复经过真实演练。
- 所有关键操作存在审计记录。

## 26. 主要风险与处理

| 风险 | 影响 | 处理策略 |
| --- | --- | --- |
| Gmail scope 验证门槛 | 公开发布可能受阻 | 私有部署优先；尽早建立 Google Cloud 测试项目并评估验证 |
| Gmail 数据不完整 | 账本缺少非成交事件 | 每月官方 Excel/CSV 对账；界面持续显示数据来源与完整性 |
| Python 与 TS 精度差异 | 盈亏和批次不一致 | Decimal 全链路、golden 对照、逐行 lots/realized 比较 |
| DuckDB 单写限制 | 并发写入冲突 | 单一 WriteCoordinator；导入、同步、重建串行化 |
| Agent CLI 协议变化 | 适配器失效 | 版本探测、允许版本范围、契约测试、明确 unavailable |
| Agent 幻觉 | 错误金融解释 | 数字必须来自只读工具，强制口径元数据，禁止自行估价 |
| 高级动效影响性能 | 卡顿和耗电 | 能力分级、shader 预热、减少动态模式、性能门禁 |
| 多账户歧义 | 数据写错账户 | 明确映射、歧义阻断、提交预览和审计 |
| 私有服务证书配置复杂 | App 无法安全连接 | Tailscale 默认路径；提供明确证书诊断，不允许明文回退 |
| 原始数据含敏感信息 | 泄漏风险 | 临时存储、最短保留、加密、日志脱敏、Agent 隔离 |

## 27. 仍需讨论的事项

以下问题不阻塞本文档成立，但在进入实现前应明确：

1. 服务端最终运行位置：家用服务器、NAS、Mac 常开机还是云主机。
2. Android App 分发方式：本地安装、内部测试渠道还是公开商店。
3. Google OAuth 应用类型和发布范围：仅测试用户、内部使用还是公开验证。
4. 首次 Gmail 有界扫描的默认时间范围。
5. 官方文件对账提醒已确定按账户配置，默认每个自然月一次，可关闭或调整为 1–24 个月。
6. 备份是否包含加密后的 Gmail token；若包含，备份密钥如何隔离。
7. Agent CLI 是共用服务器主机登录态，还是每个 Agent 使用独立服务账号。
8. Agent 会话和工具审计的保留周期。
9. 第一阶段是否只支持 USD 账户；如果存在多币种，是否只分组展示。
10. 未来是否需要行情数据；若需要，数据源、许可、延迟和费用另行立项。
11. 手工更正账本是否需要第一阶段支持；推荐首版不允许直接编辑，先以官方文件重导和明确对账为主。
12. App 中是否允许导出审计报告或脱敏分析结果。

## 28. 推荐的最终方案

在当前需求下，推荐保持以下组合不变：

- Flutter 手机端，Android 优先。
- 单一 Fastify + TypeScript 服务。
- DuckDB 作为唯一服务端数据库，所有写入串行协调。
- `.xlsx` 与 `.csv` 使用同一金融标准化内核。
- Gmail 只读授权、服务端离线同步、history 增量和有界重扫。
- 每月官方 Excel/CSV 对账，弥补邮件覆盖限制。
- 多 Schwab 账户、显式账户范围、账户间 FIFO 隔离。
- Claude、Pi、OpenCode 作为受限子进程，通过统一只读工具和事件协议接入。
- Agent 只负责解释，不负责金融计算和数据变更。
- Revolut 作为唯一视觉参考，Material Icons Outlined 作为唯一图标体系，界面无表情符号。
- 私有自托管、设备配对、HTTPS、令牌加密和完整审计作为默认安全基线。
- Python 只有在测试、golden、真实样本和批次结果全部对照通过后才退役。

该方案控制了基础设施重量，同时保留金融正确性、Gmail 自动化、多账户和多 Agent 的完整能力。它适合先做出可靠的私有产品，再依据真实使用数据决定是否增加行情、多币种、公开发布或多用户能力。

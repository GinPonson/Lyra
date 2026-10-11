# Genkit 与 Google 登录

本次迁移将 Lyra 收敛为 **一个 Flutter App、一个 TypeScript 服务、一个 Genkit 助手**。金融工具继续在服务进程内执行，不再启动 Claude、Pi、OpenCode CLI，也不再暴露 `/mcp`。

## 1. 助手如何使用已有方法

`server/src/infrastructure/agents/genkit-runtime.ts` 使用 Genkit 的 `generateStream` 和动态工具定义。每次调用进入 `AgentTurnOrchestrator`，由现有金融注册表执行并保存工具审计。

可用工具为 `accounts`、`summary`、`positions`、`expiring`、`trades`、`realized`、`cashflow`、`monthly`、`allocation`、`attribution`、`drawdown`、`risk`、`holding-period`、`extremes`、`streaks`、`stress`、`audit`、`market-positions`，共 18 个。

`market-positions` 是独立的外部只读行情工具，与 App 的 `GET /v1/market/positions` 共用供应商无关的估值层，以及用户在「我的 → 行情与估值」保存的股票 / 期权主备来源。模型不能指定供应商、任意 URL 或密钥，也不能修改这些设置。工具返回当前持仓的参考估值、来源、报价时间、选价依据、缺失 / 过期状态及本次来源设置版本；不提供历史组合净值或当日盈亏。已有 17 个账本工具的计算口径保持原样。操作与接口见[行情接入与换源](market-data.md)。

账户范围、账本版本与截止时间来自服务端会话。模型只能提交工具公开允许的参数，不能覆盖这些数据边界。无效工具名、非法参数和试图覆盖范围的请求进入拒绝审计；合法工具经过同一执行路径。

金融语义没有随 AI 框架改变：`positions` 仍基于现有 FIFO 与历史成本口径；`stress` 仍是已有的期权压力分析口径，并不自动获得实时行情或任意市场跌幅模拟能力。模型负责选择工具、组织解释，计算结果由金融代码提供。

保留流式回复、工具卡片和取消。账户范围、账本版本与截止时间在新建会话时确定；切换范围或使用最新账本基线需新建对话。每个会话同一时刻只运行一个回合。提供方已固定为 `genkit`，界面显示 Lyra；工具循环有时限与轮次上限。当前累计模型返回的 token 用量，费用保持未知，不估算为零。服务重启时，未完成的 Genkit 回合会明确标记为中断，保留已保存内容与工具记录，并恢复到可继续对话的空闲状态。

## 2. 配置模型

当前实现安装并精确锁定 `genkit` 和官方 `@genkit-ai/compat-oai` 插件。它连接支持 **Chat Completions、流式回复和 tool calling** 的兼容模型端点。

| 变量 | 用途 |
| --- | --- |
| `LYRA_GENKIT_BASE_URL` | 模型 API 基址，例如供应商提供的 `/v1` 地址；不能含用户名、密码、查询参数或片段 |
| `LYRA_GENKIT_MODEL` | 供应商接受的完整模型标识，可包含 `/` |
| `LYRA_GENKIT_API_KEY` | 模型供应商密钥，仅保存在服务端 |
| `LYRA_AGENT_TURN_TIMEOUT_MS` | 回合上限，默认 `120000`；允许 `1000` 至 `600000` 毫秒 |
| `LYRA_AGENT_MAX_TURNS` | Genkit 循环上限，默认 `10`；允许 `1` 至 `32` |

三项模型配置未齐全时，助手公开状态为 `not_configured`，账本功能仍可工作。状态显示可用仅表示本地配置完整，不代表已经向模型供应商完成健康请求。

Google 登录只用于确认 Lyra 用户身份，**不会提供模型额度或转换为模型 API 凭据**。模型服务仍需独立配置。自托管且不验证 API key 的兼容端点也需要填写非空占位 key。

## 3. 配置 Google 登录

### Google 侧

在 Google Cloud 中为本项目创建 OAuth **Web application** 客户端。授权码由服务端交换，所以 client secret 保留在服务端，App 不内置该密钥。

在 Authorized redirect URIs 中登记完整回调，例如：

```text
https://lyra.example.com/v1/auth/google/callback
```

服务端配置必须与之完全一致，包括协议、域名、端口及路径。回调不接受查询参数或片段，正式环境要求 HTTPS。只允许 `localhost`、`127.0.0.1` 或 IPv6 loopback 在本机开发时使用 HTTP。

如果 OAuth 项目仍处于 Google 的测试发布状态，应将用于登录的 Google 账号加入项目的测试用户。具体授权设置按 Google 控制台当前提示完成。

### Lyra 侧

| 变量 | 要求 |
| --- | --- |
| `LYRA_LOGIN_GOOGLE_CLIENT_ID` | Web 客户端 ID |
| `LYRA_LOGIN_GOOGLE_CLIENT_SECRET` | 对应的客户端密钥 |
| `LYRA_LOGIN_GOOGLE_REDIRECT_URI` | 已登记的 `/v1/auth/google/callback` 完整地址 |
| `LYRA_OWNER_EMAIL` | 允许登录的 Google 已验证邮箱 |
| `LYRA_OWNER_GOOGLE_SUBJECT` | 可选的 Google 稳定 subject；可替代邮箱限制 |

至少设置一种所有者限制；同时设置邮箱和 subject 时必须都匹配。部分配置缺失会在启动时失败。全部未配置时登录入口返回 `AUTH_NOT_CONFIGURED`，不会退回匿名配对。

这是自用账本的单所有者模式。首次通过配置限制并成功兑换凭据后，数据库固定记录 Google `sub`。之后即使邮箱相同，不同 `sub` 也不能接管账本。直接更改环境变量不会重新分配已绑定所有者；当前未提供所有者转移界面。

### App 登录过程

1. 输入 HTTPS 服务端地址，点击“使用 Google 登录”。
2. App 生成本地 PKCE verifier，经服务端创建一次登录事务。
3. 系统浏览器打开 Google 授权页，用户选择已允许的账号。
4. 服务端回调验证 Google ID token、state、nonce 与所有者限制，浏览器显示“请返回 Lyra”。
5. App 使用仅保存在自身内存中的 verifier 轮询并领取 Lyra 凭据。

登录事务十分钟过期，只能成功兑换一次；服务端重启会清除未完成事务，App 重新发起即可。Google 的授权码交换与 App 的最终兑换使用两组独立 PKCE，浏览器 URL 和回调页面不携带 Lyra token。

> **安全边界（重要，尚未修复）**：当前登录事务绑定的是**发起方**（持有 App verifier 的一方），而不是**在浏览器里完成授权的人**。`/v1/auth/google/start` 无需凭据，回调只校验 Google 身份是否属于配置的所有者。因此任何能访问该服务的人都可以自行创建事务，把生成好的授权链接发给所有者，诱使其用自己的 Google 账号完成授权，随后用自己持有的 verifier 领取该所有者的设备凭据（access token + 三十天 refresh token）。
>
> 这需要所有者点击他人提供的链接并在 Google 授权页同意，属于钓鱼前提，但确实能在所有者不知情的情况下取得账本访问权。在补齐“只有人类可传递的共享秘密”之前（例如回调页显示确认码并由 App 输入后兑换，或让授权码直接回到 App 而非服务端），**不要把本服务的 Google 授权链接转发给任何人，也不要在不可信网络暴露登录入口**。

Lyra access token 有效期十五分钟，refresh token 有效期三十天并在使用后轮换。退出登录会撤销当前设备的 access/refresh 凭据、关闭该设备的 WebSocket 并取消其正在运行的助手回合；App 清除本地登录信息。若客户端重新使用同一公钥，服务端会复用设备身份并撤销旧凭据；当前 App 每次重新登录生成新密钥，因此新登录会创建新的设备记录。

当前实际接入的身份提供方只有 **Google**。GitHub 和 Linux.do 尚未接入，界面不显示未实现的选项。

## 4. Gmail 授权保持独立

登录只请求 `openid email profile`。已有 Gmail 授权继续使用 `LYRA_GOOGLE_CLIENT_ID`、`LYRA_GOOGLE_CLIENT_SECRET` 和 `LYRA_GOOGLE_REDIRECT_URI`，由已有 Gmail 授权与导入流程处理；它不是新的登录 callback。

不使用 Gmail 导入时，省略这些可选变量即可。已有部署保留原来的 `LYRA_SECRET_MASTER_KEY`，否则原先加密保存的 Gmail refresh token 无法用新密钥解密。新部署可以在本机生成 32 字节随机密钥：

```bash
node -e 'console.log(require("node:crypto").randomBytes(32).toString("hex"))'
```

## 5. 创建证券账户并导入第一份账本

Google 登录成功后，新数据库还没有证券账户。先在「我的 → 证券账户」或总览创建入口添加账户，再导入官方 CSV / XLSX；也可以直接选择文件，在导入面板按提示创建账户，随后继续同一文件的预览。

- **账户名称**：自己识别的名称，例如“长期账户”。
- **账户标识**：Schwab 导出文件名中 `Individual_` 后、下一个下划线前的片段。例如 `Individual_XXX276_Transactions.csv` 填 `XXX276`；可直接使用导出文件中的脱敏标识。
- **基础币种**：默认 USD，用于该账户的币种标记；创建账户不会进行换汇或重新计算交易金额。

添加账户与导入确认是两次明确操作。创建成功只增加账户记录，不添加交易、不创建新的金融账本版本。文件仍须通过既有全批校验、导入预览与确认提交；成功导入后再刷新持仓和分析。

账户写接口为 `POST /v1/ledger/accounts`，要求协议 2 和有效的设备 Bearer token。请求示例：

```json
{
  "displayName": "长期账户",
  "accountIdentifier": "XXX276",
  "baseCurrency": "USD"
}
```

成功返回 `201` 与 `{ "account": ... }`，公开账户对象仅包含 ID、显示名、券商、脱敏后缀、币种、状态与时间。完整导入标识不回传至账户列表或模型工具。标识会进行大小写规范化；同一标识不能重复创建，包括已归档的账户。

App 为一次创建保留 `Idempotency-Key`，请求响应丢失时可重试并取回原账户。同设备、同键、同规范化请求在 24 小时内重放原成功结果；同键改换请求返回 `IDEMPOTENCY_KEY_REUSED`，重复标识返回 `ACCOUNT_ALREADY_EXISTS`。新增账户是 App 的明确写操作，不在 Genkit 的只读工具中。

## 6. 用户页面与设置

App 主导航为「总览、股票、交易、我的」。「我的」统一显示 Google 登录身份、证券账户列表与新增入口、金额隐私、启动账户、涨跌配色、行情与估值、连接管理和版本信息。未登录时仍能访问本机设置。

「启动时隐藏金额」下次启动生效，总览的眼睛按钮临时控制当前会话；金额与原始值提示、辅助阅读、金额图表一并处理，股数、笔数、日期和比例保留。AI 对话正文不在此开关的处理范围。配色只改变金融涨跌的视觉表达，不改正负号或金融计算。

启动账户支持记住上次、全部账户或指定活跃账户。所选账户按服务端与稳定 Google 身份隔离，恢复前校验有效性；已不存在或已归档的账户回退到全部账户。默认值修改只影响后续启动，不改变当前账户选择或已有 AI 对话范围。

公开连接检测通过既有 `/v1/system/info` 和 `/v1/health` 完成，使用独立无凭据客户端，检测失败不会触发登录失效。更换服务端先保存新地址，再退出原设备会话并开始新的 Google 授权；保存失败会保留当前登录。退出登录保留本机使用偏好。详细操作见[移动端说明](../apps/mobile/README.md#我的与常用设置)。

金额隐私、启动账户和配色保存在本机。「行情与估值」中的来源选择则需要有效登录：为股票和期权分别选择主来源、备用来源或关闭，每次选择会自动提交，确认后立即对服务端的新行情请求生效并刷新行情，不需要额外保存或为切源重启。选择通过独立的 `GET / PUT /v1/market/settings` 持久化到 schema 14，同一所有者的全部账户、设备和 Genkit 共用，服务重启后恢复。

行情来源列表由服务端目录提供；未配置来源可见但不可选，已保存来源缺凭据或移除时保留原选择并提示修复。自动提交时显示「正在切换」并暂时禁用选择控件；失败时界面恢复最后已确认的选择并提示重试。请求使用 `expectedRevision` 检查其他设备的修改，冲突返回 `409 / MARKET_SETTINGS_CHANGED`，App 提示重新加载后再选择，不静默覆盖其他设备的修改。设置写操作不进入 Genkit 的只读工具。

模型供应商、密钥和模型名称继续由服务端环境变量管理；行情密钥、MarketData 模式与请求预算也在服务器配置，变更这些内容需要配置预检并重启。推荐行情来源默认环境变量维持 `none`，实际选源通过界面自动提交；env 来源值只在尚无持久记录时作为启动默认。完整行为见[行情接入与换源](market-data.md#4-在界面选择来源)。

## 7. 启动与升级

VPS 部署可使用已提供的 Dockerfile、Compose 与 Caddy 模板，具体命令见 [服务端部署](deployment.md)。其配置预检不打开数据库；下面保留直接运行 Node 的方式。

环境变量模板见 [`server/.env.example`](../server/.env.example)。以下命令均从仓库根目录执行：

```bash
npm ci
npm run check
mkdir -p data
cp server/.env.example server/.env
```

替换模板值，配置好 HTTPS 反向代理与 Google 回调后启动：

```bash
node --env-file=server/.env server/dist/src/main.js
```

默认地址是 `127.0.0.1:3141`。反向代理需要转发常规 HTTP 请求并支持 `/v1/events` WebSocket 升级。App 和系统浏览器都必须能够访问回调使用的域名；跨设备测试建议直接使用 HTTPS 测试域名。

升级已有部署时：

1. 停止旧服务，保存一致的数据库备份与原有密钥配置。
2. 配置新的 Google 登录和模型环境变量；旧 CLI 路径、扩展、工作目录及 MCP 配置不再使用。
3. 启动新版服务，按现有版本自动迁移至 v14。
4. 安装支持界面切源的协议 **2** App。从 v12 或更早版本升级时，重新 Google 登录并新建 Lyra 会话；已使用 v13 OAuth 登录的有效设备会话可以继续使用。

v12 保留旧消息和工具审计，记录旧 provider，将旧会话归档，并终结未完成的旧消息/工具记录。v13 新增所有者绑定，并撤销此前匿名配对签发的凭据。v14 新增行情来源设置表，随账本数据库备份；不重复撤销 v13 的 OAuth 凭据，也不改变金融账本版本。

旧客户端使用协议 1 时收到 `426 / CLIENT_VERSION_INCOMPATIBLE`。当前 schema 为 **14**；旧程序不能直接读取升级后的数据库。若需要回退，使用停服时保存的备份和相匹配的旧 App / 服务端版本。

## 8. 验证与当前边界

`npm run check` 执行干净 TypeScript 构建、复制 SQL 迁移，再运行全部服务端测试。构建前清理 `dist`，防止删除的 CLI/MCP 模块及旧测试继续残留在部署产物中。

Genkit 集成验证使用 Genkit 的真实工具循环、可控假模型、真实 DuckDB，以及运行在本机的兼容 HTTP/SSE 模型端点；不需要外部模型账号。OAuth 测试替换外部 Google 边界，验证状态、身份规则与真实数据库凭据流程。

首次账户与导入阶段的服务端回归为 **117 个套件、279 项测试通过**，0 失败、0 跳过。该用例从真实空数据库开始，经受控 Google 身份验证后签发设备凭据，创建账户、预览并确认 CSV，最后读取真实持仓与已实现盈亏；大小写不同的文件名重复上传仍不会重复入账。此后的部署和行情改动及最新全量统计见[实现进度](progress.md)。

此前用户页面与本机偏好阶段执行了 `flutter pub get` 并保存真实解析的锁文件，新增兼容现有平台依赖的 `package_info_plus 9.0.1`；当时 `flutter analyze --no-pub` 零问题，`flutter test --no-pub --reporter expanded --timeout 60s` **162 项全部通过**。新增 71 项覆盖金额隐私与图表配色、持久偏好、启动账户恢复、用户页面及连接管理；原有 91 项继续通过。检查期间修复了启动读取失败后手动选择仍无法查询，以及偏好保存失败后服务端地址退回默认值的恢复路径。该历史阶段没有修改服务端，117 套件 / 279 项是当时上一轮服务端完整回归结果；行情与界面切源的最新验证见[实现进度](progress.md)。

此前交付的深色界面 Android 调试 APK 已通过 `flutter build apk --debug --no-pub` 构建，版本 `1.0.0` / versionCode **3**，167,049,415 字节；元数据解析与 `apksigner verify` 通过。该 APK 使用开发签名，尚未进行真机安装与运行验收，也不包含后来新增的行情面板和界面切源；体验这些功能需重新构建当前移动端源码，本次未重建 APK 或部署服务。深色界面阶段的 Flutter 组件已完成手机、放大字体与宽屏渲染检查。真实 Google 浏览器登录、真实模型 / 行情供应商及手机完整联调仍需要部署配置，不能用受控测试代替。

最新执行结果记录在 [`docs/progress.md`](progress.md)。这些待验证项完成前，保持迁移 PR 为草稿。

## 官方参考

- [Genkit 工具调用](https://genkit.dev/docs/js/tool-calling/)
- [Genkit JavaScript API](https://js.api.genkit.dev/)
- [Google Web Server OAuth](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Google OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect)

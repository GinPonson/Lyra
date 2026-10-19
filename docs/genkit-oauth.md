# Genkit 与多来源登录

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

Google、Linux.do 和 GitHub 登录只用于确认 Lyra 用户身份，**不会提供模型额度或转换为模型 API 凭据**。模型服务仍需独立配置。自托管且不验证 API key 的兼容端点也需要填写非空占位 key。

## 3. 配置 Google、Linux.do 与 GitHub

独立登录页固定显示 **Google、Linux.do、GitHub** 三个入口，通过公开的 `GET /v1/auth/providers` 查询当前服务器已启用的方式。未配置的来源保留显示并标为不可用；目录只公开来源名称与可用性，不返回 client secret 或任何用户标识。

部署时至少完整启用一种来源。两份环境模板中的登录组默认均为注释：只取消注释并填写要使用的整组，其他组保持未设置。任何来源部分填写都会在账本迁移前报错；回调必须是固定 HTTPS 路径。模板里的 `replace-with-*` 占位值不会被专门识别，务必替换成真实值。填写完整仅代表本地配置可用，实际账号与回调仍须浏览器联调。

### 回调地址

每种来源分别在自己的开发者控制台登记以下完整地址，并与服务端对应的 `REDIRECT_URI` 保持一致：

| 来源 | 固定回调路径 | 公开地址示例 |
| --- | --- | --- |
| Google | `/v1/auth/google/callback` | `https://lyra.example.com/v1/auth/google/callback` |
| Linux.do | `/v1/auth/linuxdo/callback` | `https://lyra.example.com/v1/auth/linuxdo/callback` |
| GitHub | `/v1/auth/github/callback` | `https://lyra.example.com/v1/auth/github/callback` |

将示例域名替换为自己的服务域名，协议、域名、端口及路径须完全一致。回调不能含用户名、密码、查询参数或片段；正式环境要求 HTTPS。只允许 `localhost`、`127.0.0.1` 或 IPv6 loopback 在本机开发时使用 HTTP。服务端启动时会校验每个已启用来源的回调是否符合该来源的固定 HTTPS 路径（部分填写整组失败）；**没有全局 origin 配置项**（不再有 `LYRA_PUBLIC_ORIGIN`，服务端从不读取它），公开地址由反向代理的域名与各自登记的 HTTPS 回调决定。

### Google

在 Google Cloud 创建 OAuth **Web application** 客户端，在 Authorized redirect URIs 登记 Google 回调。授权码由服务器交换，client secret 仅留在服务器。若项目仍处于 Google 的测试发布状态，按控制台要求加入自己的测试账号。配置步骤见 [Google Web Server OAuth](https://developers.google.com/identity/protocols/oauth2/web-server)。

| 变量 | 要求 |
| --- | --- |
| `LYRA_LOGIN_GOOGLE_CLIENT_ID` | Web 客户端 ID |
| `LYRA_LOGIN_GOOGLE_CLIENT_SECRET` | 对应客户端密钥 |
| `LYRA_LOGIN_GOOGLE_REDIRECT_URI` | 已登记的 Google 完整回调地址 |

**不再有 `LYRA_OWNER_*` 变量。** 多用户改造后来源的可用性只取决于 OAuth 配置是否齐全，不再需要预先指定允许登录的邮箱或 subject。首次成功兑换后数据库固定该 `(provider, subject)` 并建立用户；之后相同邮箱的另一个 Google 身份不会被当成同一个用户。

### GitHub

按 [GitHub 创建 OAuth App](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app) 的说明，在个人设置中的 Developer settings → OAuth apps 注册应用；将 Authorization callback URL 填为上表的 GitHub 回调，取得 client ID 和 client secret。

| 变量 | 要求 |
| --- | --- |
| `LYRA_LOGIN_GITHUB_CLIENT_ID` | OAuth App 客户端 ID |
| `LYRA_LOGIN_GITHUB_CLIENT_SECRET` | 对应客户端密钥 |
| `LYRA_LOGIN_GITHUB_REDIRECT_URI` | 已登记的 GitHub 完整回调地址 |

Lyra 在每次登录时用授权所得令牌调用固定的 `GET https://api.github.com/user` 取稳定数字 `id`，请求范围为 `read:user`，不申请仓库权限。该 `id` 是用户名变化后仍可识别用户的稳定标识，见 [Users API](https://docs.github.com/en/rest/users/users)；**昵称、`login` 与邮箱都不参与身份识别**。

### Linux.do

进入 [Linux DO Connect](https://connect.linux.do)，按官方 [Connect 接入说明](https://wiki.linux.do/Community/LinuxDoConnect) 在「我的应用接入 → 申请新接入」登记应用与 Linux.do 回调，取得 client ID 和 client secret。

| 变量 | 要求 |
| --- | --- |
| `LYRA_LOGIN_LINUXDO_CLIENT_ID` | Connect 客户端 ID |
| `LYRA_LOGIN_LINUXDO_CLIENT_SECRET` | 对应客户端密钥 |
| `LYRA_LOGIN_LINUXDO_REDIRECT_URI` | 已登记的 Linux.do 完整回调地址 |

Connect 文档将用户资料中的 `id` 定义为不可变标识。授权范围为 `user`。当前接入只使用稳定 `id` 和显示名，不依赖或返回经验证邮箱，也不根据论坛昵称、信任等级、`external_ids` 等字段识别身份。

### 多用户与自动注册

三种来源都先验证第三方身份；已有身份直接登录，新身份须在授权后填写有效邀请码完成注册。登录首页只显示品牌与底部三个登录按钮，不展示邀请码输入框或按钮下的说明文字。

- 身份键始终是已验证的 **`(provider, subject)`**。Lyra **不**按相同邮箱、昵称或不同来源碰巧相同的数字 ID 自动合并——同一自然人的两个来源身份会得到两个独立用户，除非运维在旧库升级时显式确认它们属于同一人。
- **首版没有自助绑定**：界面不能把另一个来源绑到已有用户上，也不能做所有者转移。
- 新身份通过邀请码注册后，一律成为 `member` 并获得空账本。账号、身份、设备凭据和邀请码核销在同一事务内提交；缺码或错码会整体回滚。`admin` **只能**由离线入口显式创建（见[部署文档](deployment.md#新安装显式创建首个管理员)），任何登录路径都不会产生管理员。
- 用户间完全隔离：有效但不属于当前用户的资源与「不存在」返回相同的 404。
- **停用**用户会立刻撤销其全部设备凭据、关闭其事件流并终止在飞回合；被停用的身份再次 OAuth 登录会被拒绝（身份映射保留，不会因此新建一个用户绕开停用）。恢复用户后需要重新登录，旧令牌不复活。
- 三种来源的邮箱都允许为空，App 显示对应来源与可用的显示名。

新增或修改 client 配置后重启服务器（配置在启动时校验），再在登录页重新检测登录方式。关闭来源阻止该来源继续发起新授权，不应将它视为撤销所有已签发设备凭据的操作；设备退出与凭据撤销另行处理。**注意：环境变量里残留的 `LYRA_OWNER_*` 已不起任何作用**，可以删除。

### App 登录过程

1. 未登录时进入独立登录首页；App 自动恢复或使用构建时配置的 HTTPS 服务端地址。Google、Linux.do、GitHub 三个入口按顺序放在底部安全区上方；失败时在登录入口上方显式反馈。
2. 选择服务器已启用的来源；App 生成本地 PKCE verifier，并向对应 `/v1/auth/{provider}/start` 创建登录事务。该请求只接受设备名、公钥和 PKCE 挑战，不再接收 `inviteCode`。
3. Android 系统浏览器的 Auth Tab 打开所选来源的官方授权页，使用自己的账号完成授权；浏览器阶段 App 不在后台轮询兑换。
4. 服务端通过固定 HTTPS 回调验证 state、来源与身份；Google 另验证 ID token 与 nonce，GitHub / Linux.do 通过固定用户资料端点验证身份。无论成败，服务端都返回相同的 303 重定向到 `com.lyrafinance.lyra://auth/callback`。该地址固定且不携带查询参数、片段、code、state 或用户数据，不能由请求指定。系统捕获该回跳，关闭授权窗口并自动回到 Lyra。
5. App 回到前台后立即使用仅保存在自身内存中的 verifier 向同一来源的 `/exchange` 领取 Lyra 设备凭据，必要时继续轮询。已存在的 `(provider, subject)` 直接登录；新身份收到 `INVITE_CODE_REQUIRED` 后进入「完成注册」页面，此时还没有建立 Lyra 会话。
6. 新用户填写邀请码后，对同一 `/exchange` 提交 `{loginId, codeVerifier, inviteCode}`。缺码或错码保留已授权事务，表单可原地更正，无需再次授权；有效码核销并完成注册后才保存设备凭据、进入工作台。返回登录会撤销本机待注册尝试，过期则必须重新授权。关闭授权窗口即取消登录；迟到回跳或兑换响应不能覆盖新的登录或服务器选择。

登录事务十分钟过期，补填或更正邀请码不会延长时限，只能成功兑换一次，并绑定来源；服务端重启会清除未完成事务，App 重新发起即可。Google 和 GitHub 的上游授权码交换使用 PKCE，App 到 Lyra 的最终兑换使用独立 PKCE。Linux.do 官方接入文档未保证上游 PKCE 支持，当前使用服务端机密客户端授权码流程，仍保留 App 到 Lyra 的 PKCE 与 state 校验。第三方令牌仅在服务端用于本次身份验证，不交给 App；浏览器 URL 和 App 回跳不携带 Lyra token。GitHub 流程见 [Authorizing OAuth apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)。

Auth Tab 需要支持该能力的浏览器（Chrome 137+），不支持时 App 明确提示；它复用系统浏览器的账号状态与自动填充。升级自动回 App 流程时须同时更新服务端与 APK，提供方控制台原有 HTTPS callback 地址无需修改。固定回跳不是登录成功证明，App 仍须校验 PKCE 兑换结果中的用户和来源。

**自动注册只在完整验证之后发生**：回调未完成、授权取消、verifier 无效或事务过期都不会创建用户。用户、身份、设备与凭据在**同一个事务**内提交，失败不会留下「只有用户、没有身份」的半成品。

> **已知安全边界，本轮未修复**：当前登录事务绑定的是**发起方**（持有 App verifier 的一方），不能证明**在浏览器里完成授权的人**持有发起设备。三种来源的 `/start` 都允许未登录创建事务；攻击者仍可能先自行创建事务，再把生成的授权链接发给某个用户，诱导其授权后用攻击者自己持有的 verifier 领取**该用户**的设备凭据。提供方隔离、state 和 PKCE 校验不能解决这个钓鱼前提。
>
> **多用户下影响面更大**：首次登录会自动注册，因此登录入口的暴露范围直接决定谁能在这个部署上获得账号。
>
> 在加入浏览器与 App 之间的人为确认等协议之前，只使用自己在 App 内发起的授权页，不点击或转发他人提供的 Lyra 授权链接；部署侧继续遵守[登录入口的已知安全边界](deployment.md#8-登录入口的已知安全边界)。本次增加登录来源没有声称修复该问题。

Lyra access token 有效期十五分钟，refresh token 有效期三十天并在使用后轮换。退出登录会请求撤销当前设备的 access/refresh 凭据、关闭该设备的 WebSocket 并取消其运行中的助手回合；App 清除本地登录信息，服务端暂时无法撤销时会明确提示。退出当前设备不等于撤销其他设备。若客户端重新使用同一公钥，服务端会复用设备身份并撤销旧凭据；当前 App 每次重新登录生成新密钥，因此新登录会创建新的设备记录。

## 4. Gmail 授权保持独立

Google 登录只请求 `openid email profile`，GitHub 与 Linux.do 登录也不申请 Gmail 权限。「我的 → 证券账户 → 嘉信邮件」单独发起 Gmail 只读授权；Google 授予邮箱只读权限，Lyra 只查询嘉信成交确认邮件，不发送、删除或修改邮件。

Gmail 使用显式配置的 `LYRA_GOOGLE_CLIENT_ID`、`LYRA_GOOGLE_CLIENT_SECRET` 和 `LYRA_GOOGLE_REDIRECT_URI`。可以复用同一 Google Web OAuth 客户端及已经登记的 `/v1/auth/google/callback`；该回调根据服务端保存的短时 state 分派至 Gmail 或身份登录流程，权限请求与凭据保存仍独立。三个 Gmail 变量必须明确填写，不会因 Google 登录可用而隐式启用邮件读取。对应 Google Cloud 项目需要启用 [Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com)，授权范围依据 [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)。

App 创建绑定当前用户与设备的授权事务，使用既有 Android 系统授权窗口完成 Google 同意流程；服务端交换授权码后固定回跳 App。App 只查询短时事务结果，不接收 Google 授权码或令牌。拒绝、取消、过期、重复回调与第三方授权失效均明确反馈；Gmail 授权失效不会登出 Lyra。重新授权保留该用户已有连接 ID、消息归档与账户映射。

首次扫描独立选择「全部嘉信成交邮件」或自定义起始日期，**不从 CSV 或账本日期推导邮件范围**；后续使用 Gmail history 游标增量读取。同步前选择现有证券账户，服务端仍校验邮件尾号与账户归属；不会创建证券账户或将其他尾号强塞进所选账户。主动重新扫描同样需要明确范围。读取消息数有界，截断不推进游标，也不显示为全量完成。

首次历史扫描可能持续数分钟。Google 新项目的用户配额为每分钟 6,000 单位，读取一封完整邮件消耗 60 单位，见 [Gmail 使用限制](https://developers.google.com/workspace/gmail/api/reference/quota)。仅对明确的临时速率限制，服务端在同一个只读 GET 上最多重试六次，采用 1、2、4、8、16、32 秒及少量随机延迟；耗尽仍返回限流错误。日配额、授权、配置、访问拒绝与响应格式错误直接暴露；App 不自动重放同步 POST，网络断开后先刷新连接状态再确认结果。

邮件与 CSV 共用账户、规范化成交事实、业务哈希和 occurrence 去重。已处理的 Gmail message ID 重试不重复入账；分散在不同邮件的相同成交按已关联的 occurrence 匹配已有交易，实际新增与已有跳过分别计数。不同 message ID 的相同内容不会被误当成同一份有顺序保证的原始文件；缺少执行编号或成交时分秒时，不声称能辨别不同投递是否属于同一次执行。

已有 CSV 账本的哈希保持原值。跨来源的证券描述或记账日展示可能不同：只在账户、实际成交日、动作、完整证券身份、数量、价格及原始精度、费用和净额全部一致，且复用既有描述与记录日能精确重建唯一已有业务哈希时，才关联既有交易。期权必须完整匹配正股、到期日、行权价和认购 / 认沽类型。股票代码只接受已核实的 Berkshire B 类股 `BRK.B / BRKB` 及其完整发行人、B 类股描述组合（包含嘉信重复 `CLASS` 的既有表示），并沿用账户唯一使用的代码；不做通用去点或模糊匹配。命中多个不同业务哈希或账户同时存在两种表示时明确报告冲突。

多价格成交支持表头明确声明的 `Quantity / Price / Principal / Charge and/or Interest / Total Amount` 五列模板。逐笔费用只接受明确的 `N/A`、`Commission` 或 `Industry Fee` 标签，并复用既有费用解析与合计校验；逐笔本金和净额、总数量、总费用及总金额均须勾稽。不会将无标签金额猜作费用、按合计分摊费用，或依据平均单价脚注虚构逐笔执行。邮件正文和附件接受合法的带填充或无填充 Base64URL，非法字符、填充和非规范编码仍明确拒绝。

期权邮件的 `Sale / Cash` 不提供开平仓方向，不直接映射为卖出开仓或平仓。只有当前用户已映射账户中的既有期权卖出，在实际成交日、完整合约身份及全部数值精度上相同，且能重建唯一业务哈希时，才借用该已有动作并标记为「仅关联」。该标记在跨邮件 occurrence 接续后再次校验，任何超出已有成交次数的新哈希都会拒绝整封邮件。缺少已确认事实或存在歧义时明确计入解析失败，不参考当前持仓猜测方向；这项核对不改变独立的邮件扫描范围。

同封邮件中，只有一种 MIME 表示已严格命中既有业务哈希时，才允许另一种表示对该 Cash 行的数量、价格采用同值的尾随零书写；本金、费用、净额和完整证券身份仍须一致。先独立验证失败候选中的全部 Cash 块，再重跑完整正文，避免普通格式错误遮住后续未验证的期权。该规范值不跨邮件复用，不改变全局交易哈希规则。

无法证明同日同标的成交先后的新增邮件，明确提示导入覆盖该日全部成交的嘉信 CSV；它不会用收信时间猜测 FIFO 顺序。此类冲突与账户映射冲突、解析失败分别计数，其余可确认的邮件继续处理。正文和明文令牌不持久化，账本写入、消息元数据及交易关联在同一个事务内提交。

授权接口为已鉴权的 `POST /v1/google/authorization-transactions` 和 `GET /v1/google/authorization-transactions/{authorizationId}`，连接状态为 `GET /v1/google/connections`。同步使用 `POST /v1/google/connections/{id}/sync`，App 始终提供已选择的现有账户 `explicitAccountId`；首次或重扫选择 `scanAll=true`，或 `scanAll=false` 加 `confirmedRescanAfterDate=YYYY-MM-DD`，两种范围互斥。正常增量不带范围参数。结果包括新增交易、已有交易、已处理邮件、账户冲突、顺序冲突及解析失败计数；同步完成后刷新现有持仓、交易、总览与行情读模型，保留当前账户范围。

不使用 Gmail 导入时，省略这些可选变量即可。已有部署保留原来的 `LYRA_SECRET_MASTER_KEY`，否则原先加密保存的 Gmail refresh token 无法用新密钥解密。新部署可以在本机生成 32 字节随机密钥：

```bash
node -e 'console.log(require("node:crypto").randomBytes(32).toString("hex"))'
```

## 5. 创建证券账户并导入第一份账本

使用已配置的任一来源登录成功后，新数据库还没有证券账户。先在「我的 → 证券账户」或总览创建入口添加账户，再导入官方 CSV / XLSX；也可以直接选择文件，在导入面板按提示创建账户，随后继续同一文件的预览。

- **账户名称**：自己识别的名称，例如“长期账户”。
- **账户标识**：Schwab 导出文件名中 `Individual_` 后、下一个下划线前的片段。例如 `Individual_XXX276_Transactions.csv` 填 `XXX276`；可直接使用导出文件中的脱敏标识。
- **基础币种**：默认 USD，用于该账户的币种标记；创建账户不会进行换汇或重新计算交易金额。

添加账户与导入确认是两次明确操作。创建成功只增加账户记录，不添加交易、不创建新的金融账本版本。文件仍须通过既有全批校验、导入预览与确认提交；成功导入后再刷新持仓和分析。

账户写接口为 `POST /v1/ledger/accounts`，要求协议 3 和有效的设备 Bearer token。请求示例：

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

登录后，App 主导航为「总览、股票、交易、我的」。「我的」显示实际登录来源和身份，以及证券账户列表与新增入口、金额隐私、启动账户、涨跌配色、行情与估值和 App 版本信息。未登录时显示独立登录页，只需选择账号；后台地址由发布构建预设，页面不展示服务器地址或连接管理。本机偏好会保留，完成登录后继续在「我的」调整。

「启动时隐藏金额」下次启动生效，总览的眼睛按钮临时控制当前会话；金额与原始值提示、辅助阅读、金额图表一并处理，股数、笔数、日期和比例保留。AI 对话正文不在此开关的处理范围。配色只改变金融涨跌的视觉表达，不改正负号或金融计算。

启动账户支持记住上次、全部账户或指定活跃账户。所选账户按服务端、登录来源和稳定 subject 隔离，恢复前校验有效性；已不存在或已归档的账户回退到全部账户。默认值修改只影响后续启动，不改变当前账户选择或已有 AI 对话范围。

新安装由 `LYRA_SERVER_URL` 提供后台地址；已有凭据及历史地址继续优先，启动时先读取本机偏好，避免短暂请求不同后台。普通页面移除连接管理、地址输入和服务端版本，登录失败只显示可操作的用户提示；内部公开系统接口保留用于部署诊断，页面不主动请求健康检测。退出登录保留本机使用偏好。详细操作见[移动端说明](../apps/mobile/README.md#我的与常用设置)。

金额隐私、启动账户和配色保存在本机。「行情与估值」中的来源选择则需要有效登录：为股票和期权分别选择主来源、备用来源或关闭，每次选择会自动提交，确认后立即对服务端的新行情请求生效并刷新行情，不需要额外保存或为切源重启。选择通过独立的 `GET / PUT /v1/market/settings` 持久化到 schema 14 引入的配置表，**按用户保存**：当前用户的全部账户、设备和 Lyra 助手共用，各用户互不影响，服务重启后恢复。供应商凭据、公开报价缓存与供应商预算仍是部署级共享。

行情来源列表由服务端目录提供；未配置来源可见但不可选，已保存来源缺凭据或移除时保留原选择并提示修复。自动提交时显示「正在切换」并暂时禁用选择控件；失败时界面恢复最后已确认的选择并提示重试。请求使用 `expectedRevision` 检查其他设备的修改，冲突返回 `409 / MARKET_SETTINGS_CHANGED`，App 提示重新加载后再选择，不静默覆盖其他设备的修改。设置写操作不进入 Genkit 的只读工具。

模型供应商、密钥和模型名称继续由服务端环境变量管理；行情密钥、MarketData 模式与请求预算也在服务器配置，变更这些内容需要重启（配置在启动时校验）。推荐行情来源默认环境变量维持 `none`，实际选源通过界面自动提交；env 来源值只在尚无持久记录时作为启动默认。完整行为见[行情接入与换源](market-data.md#4-在界面选择来源)。

## 7. 启动与升级

VPS 部署使用仓库里的 `Dockerfile`、`deploy/compose.yaml` 与可选 Caddy 模板，具体命令见 [服务端部署](deployment.md)。

本地直接运行 Python 服务端：需要 Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)。环境变量模板见 [`deploy/server.env.example`](../deploy/server.env.example)（由 `deploy/init-env.py` 生成为 `deploy/server.env`，权限 0600）：

```bash
cd server
uv run pytest          # 测试
uv run ruff check .    # 错误类静态检查
uv run --python 3.12 lyra-serve
```

服务端**只读进程环境变量，不解析 `.env` 文件**——先把变量导出（或交给 systemd / Compose 注入）再启动。**没有独立的配置检查命令**：配置在启动时校验，缺少必填项、主密钥形态不对，或至少一组登录配置不完整，都会直接失败退出，不会带着"看起来启用了但永远登不进去"的配置上线。

默认地址是 `127.0.0.1:3141`。反向代理需要转发常规 HTTP 请求并支持 `/v1/events` WebSocket 升级。App 和系统浏览器都必须能够访问回调使用的域名；跨设备测试建议直接使用 HTTPS 测试域名。

升级已有部署时：

1. 停止旧服务，保存一致的数据库备份与原有密钥配置。
2. 保留原登录配置，按需完整添加 Linux.do / GitHub 组；**不再需要任何所有者 ID**。模型配置独立保留。旧 CLI 路径、扩展、工作目录及 MCP 配置不再使用。
3. 先用原主密钥启动新版服务（**不要换密钥**：换掉会让库里已有的 Gmail 密文再也解不开），按现有版本自动迁移；配置不完整会在启动时直接退出。
4. 安装协议 **3** 的 App。**从 v15 升到 v16 必须先跑离线归属入口并重新登录**——旧设备凭据会被撤销，且原 owner 的身份需要显式确认；不要只完成第 1–3 步就以为升级结束。具体步骤见[部署文档](deployment.md#多用户账本升级schema-16需要重新登录)。

v12 保留旧消息和工具审计，记录旧 provider，将旧会话归档，并终结未完成的旧消息/工具记录。v13 新增所有者绑定，并撤销此前匿名配对签发的凭据。v14 新增行情来源设置表，随账本数据库备份；不重复撤销 v13 的 OAuth 凭据，也不改变金融账本版本。

v15 将所有者绑定扩展为按来源固定 subject，允许 GitHub / Linux.do 身份没有邮箱；原 Google 记录迁入新表，已有设备凭据、财务数据与行情设置保留，不改变金融账本版本。新来源仍须先通过显式的所有者配置，不按邮箱自动关联。

**v16 是多用户账本，取消了 v13 以来的单 owner 模型。** 新增 `users` / `user_identities`，设备、账户、导入、Agent 会话、Gmail 连接与行情设置全部带上用户归属；`(provider, subject)` 成为登录身份键，首次登录自动注册 `member`。由于 DuckDB 1.5.6 不支持给已有表加约束（`ALTER TABLE ADD CONSTRAINT` 未实现）也不支持在被引用时改名，v16 采用**整库重建**：先把每张表 CTAS 到无外键暂存表，拆除旧图，再以最终名字重建并回填归属，全部在一个事务内。**旧设备凭据在此一并撤销，所有人需要重新登录**；旧 owner 身份表被删除，其内容按离线入口显式确认的归属写入 `user_identities`，未确认的身份不会关联原用户。

协议同时升到 **3**，最低支持版本也抬到 3——协议 2 的客户端不理解登录响应里的 `user` 对象，放行它会让它按单账本语义工作。旧客户端会收到 `426 / CLIENT_VERSION_INCOMPATIBLE`。

当前 schema 为 **16**；旧程序不能直接读取升级后的数据库。若需要回退，使用停服时保存的备份和相匹配的旧 App / 服务端版本。完整的升级步骤见[部署文档的多用户账本升级一节](deployment.md#多用户账本升级schema-16需要重新登录)。

## 8. 验证与当前边界

服务端验证入口是 `cd server && uv run --frozen pytest -q`（全量测试）与 `uv run --frozen ruff check .`（错误类静态检查）。迁移 SQL 现在是 `server/src/lyra/db/sql/` 下的唯一来源，不再有 TypeScript 构建或 `dist` 清理步骤。

Genkit 集成验证使用 Genkit 的真实工具循环、可控假模型、真实 DuckDB，以及运行在本机的兼容 HTTP/SSE 模型端点；不需要外部模型账号。OAuth 测试替换外部 Google / GitHub / Linux.do 边界，验证状态、来源隔离、自动注册与真实数据库凭据流程；不能代替真实账号的授权验收。

独立登录页与多来源登录的服务端 Python 全量测试在 HEAD（`ed522c7`）实测 **2307 项通过 / 4 项跳过**（`cd server && uv run --frozen pytest -q`；数字随提交变动，以执行时输出为准），覆盖新来源配置、身份准入与迁移、登录事务；Flutter 当前结果与完整记录见[实现进度](progress.md)。

首次账户与导入阶段的服务端回归为 **117 个套件、279 项测试通过**，0 失败、0 跳过。该用例从真实空数据库开始，经受控 Google 身份验证后签发设备凭据，创建账户、预览并确认 CSV，最后读取真实持仓与已实现盈亏；大小写不同的文件名重复上传仍不会重复入账。此后的部署和行情改动及最新全量统计见[实现进度](progress.md)。

此前用户页面与本机偏好阶段执行了 `flutter pub get` 并保存真实解析的锁文件，新增兼容现有平台依赖的 `package_info_plus 9.0.1`；当时 `flutter analyze --no-pub` 零问题，`flutter test --no-pub --reporter expanded --timeout 60s` **162 项全部通过**。新增 71 项覆盖金额隐私与图表配色、持久偏好、启动账户恢复、用户页面及连接管理；原有 91 项继续通过。检查期间修复了启动读取失败后手动选择仍无法查询，以及偏好保存失败后服务端地址退回默认值的恢复路径。该历史阶段没有修改服务端，117 套件 / 279 项是当时上一轮服务端完整回归结果；行情与界面切源的最新验证见[实现进度](progress.md)。

此前交付的深色界面 Android 调试 APK 已通过 `flutter build apk --debug --no-pub` 构建，版本 `1.0.0` / versionCode **3**，167,049,415 字节；元数据解析与 `apksigner verify` 通过。该 APK 使用开发签名，尚未进行真机安装与运行验收，也不包含后来新增的行情面板和界面切源；体验这些功能需重新构建当前移动端源码，本次未重建 APK 或部署服务。深色界面阶段的 Flutter 组件已完成手机、放大字体与宽屏渲染检查。真实 Google 浏览器登录、真实模型 / 行情供应商及手机完整联调仍需要部署配置，不能用受控测试代替。

独立登录页与新增来源的当前验证结果另记在 [`docs/progress.md`](progress.md)。本轮没有配置真实三方 OAuth 应用、部署 VPS 或重新打包 APK；三种来源的真实浏览器授权、回调与手机完整联调仍待执行。上述历史测试与截图不能视为本轮已完成真实授权。真实环境待验证项完成前，保持迁移 PR 为草稿。

## 官方参考

- [Genkit 工具调用](https://genkit.dev/docs/js/tool-calling/)
- [Genkit JavaScript API](https://js.api.genkit.dev/)
- [Google Web Server OAuth](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Google OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect)
- [GitHub 创建 OAuth App](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app)
- [GitHub OAuth 授权流程](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)
- [GitHub Users API](https://docs.github.com/en/rest/users/users)
- [Linux DO Connect 官方接入说明](https://wiki.linux.do/Community/LinuxDoConnect)

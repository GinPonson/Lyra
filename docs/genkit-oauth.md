# Genkit 与 Google 登录

本次迁移将 Lyra 收敛为 **一个 Flutter App、一个 TypeScript 服务、一个 Genkit 助手**。金融工具继续在服务进程内执行，不再启动 Claude、Pi、OpenCode CLI，也不再暴露 `/mcp`。

## 1. 助手如何使用已有方法

`server/src/infrastructure/agents/genkit-runtime.ts` 使用 Genkit 的 `generateStream` 和动态工具定义。每次调用进入 `AgentTurnOrchestrator`，由现有金融注册表执行并保存工具审计。

可用工具为 `accounts`、`summary`、`positions`、`expiring`、`trades`、`realized`、`cashflow`、`monthly`、`allocation`、`attribution`、`drawdown`、`risk`、`holding-period`、`extremes`、`streaks`、`stress`、`audit`。

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

Lyra access token 有效期十五分钟，refresh token 有效期三十天并在使用后轮换。退出登录会撤销当前设备的 access/refresh 凭据、关闭该设备的 WebSocket 并取消其正在运行的助手回合；App 清除本地登录信息。若客户端重新使用同一公钥，服务端会复用设备身份并撤销旧凭据；当前 App 每次重新登录生成新密钥，因此新登录会创建新的设备记录。

当前实际接入的身份提供方只有 **Google**。GitHub 和 Linux.do 尚未接入，界面不显示未实现的选项。

## 4. Gmail 授权保持独立

登录只请求 `openid email profile`。已有 Gmail 授权继续使用 `LYRA_GOOGLE_CLIENT_ID`、`LYRA_GOOGLE_CLIENT_SECRET` 和 `LYRA_GOOGLE_REDIRECT_URI`，由已有 Gmail 授权与导入流程处理；它不是新的登录 callback。

不使用 Gmail 导入时，省略这些可选变量即可。已有部署保留原来的 `LYRA_SECRET_MASTER_KEY`，否则原先加密保存的 Gmail refresh token 无法用新密钥解密。新部署可以在本机生成 32 字节随机密钥：

```bash
node -e 'console.log(require("node:crypto").randomBytes(32).toString("hex"))'
```

## 5. 启动与升级

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
3. 启动新版服务，自动执行 v12 / v13 迁移。
4. 安装协议 **2** 的 App，重新 Google 登录并新建 Lyra 会话。

v12 保留旧消息和工具审计，记录旧 provider，将旧会话归档，并终结未完成的旧消息/工具记录。v13 新增所有者绑定，并撤销此前匿名配对签发的凭据。金融账本表不在这两项迁移的修改范围内。

旧客户端使用协议 1 时收到 `426 / CLIENT_VERSION_INCOMPATIBLE`。当前 schema 为 **13**；旧程序不能直接读取升级后的数据库。若需要回退，使用停服时保存的备份和相匹配的旧 App / 服务端版本。

## 6. 验证与当前边界

`npm run check` 执行干净 TypeScript 构建、复制 SQL 迁移，再运行全部服务端测试。构建前清理 `dist`，防止删除的 CLI/MCP 模块及旧测试继续残留在部署产物中。

Genkit 集成验证使用 Genkit 的真实工具循环、可控假模型、真实 DuckDB，以及运行在本机的兼容 HTTP/SSE 模型端点；不需要外部模型账号。OAuth 测试替换外部 Google 边界，验证状态、身份规则与真实数据库凭据流程。

真实 Google 浏览器登录、真实模型供应商及手机完整联调仍需要部署配置。当前执行环境的 Flutter 工具链下载因网络审批取消而中断；Dart 格式化与语法解析已执行，`flutter pub get`、`flutter analyze`、`flutter test` 和 APK 构建尚未完成。新加的 `url_launcher` 仍需通过 `flutter pub get` 生成并审查实际 lockfile，现有锁文件没有手工伪造新条目。

最新执行结果记录在 [`docs/progress.md`](progress.md)。这些待验证项完成前，保持迁移 PR 为草稿。

## 官方参考

- [Genkit 工具调用](https://genkit.dev/docs/js/tool-calling/)
- [Genkit JavaScript API](https://js.api.genkit.dev/)
- [Google Web Server OAuth](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Google OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect)

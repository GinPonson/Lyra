# Lyra

自用资产账本与金融分析 App：Flutter 客户端连接单个 TypeScript / Fastify 服务，DuckDB 保存账本、会话和工具审计。

## 当前架构

- **一个 Lyra 助手**：Genkit 负责模型调用与工具循环，直接调用服务端已有的 17 个只读金融工具。
- **Google 登录**：只允许配置的所有者访问；App 使用系统浏览器授权，服务端验证身份并签发 Lyra 设备会话。
- **计算复用**：持仓、FIFO、已实现盈亏、风险和压力分析继续使用同一套金融代码，App 查询和助手共享数据口径。
- **Gmail 单独授权**：身份登录不申请 Gmail 读取权限；已有 Gmail 导入使用独立授权配置。

部署、模型配置、Google OAuth 设置及升级步骤见 **[Genkit 与 Google 登录](docs/genkit-oauth.md)**。

## 服务端开发

需要 Node.js 22 或更高版本。在仓库根目录执行：

```bash
npm ci
npm run check
mkdir -p data
cp server/.env.example server/.env
```

填好 `server/.env` 后，从仓库根目录启动：

```bash
node --env-file=server/.env server/dist/src/main.js
```

默认监听 `127.0.0.1:3141`。正式访问通过 HTTPS 反向代理，代理须支持 `/v1/events` 的 WebSocket 升级。

Flutter 开发入口见 [移动端说明](apps/mobile/README.md)。最新验证状态见 [实现进度](docs/progress.md)。

## 旧版本升级

当前客户端协议为 **2**，数据库 schema 为 **13**。App 与服务端需要一起更新；旧设备凭据会失效，用户需重新 Google 登录。旧 CLI 会话保留内容与工具记录并归档，新建会话使用 Genkit。升级前停服并备份数据库；旧服务端不能直接打开已升级数据库。

`docs/plans/` 与进度表中的早期记录保留设计演变过程；涉及 MCP、多 CLI 和配对码的旧设计已被本次迁移替代。

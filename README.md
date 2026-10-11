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

## 第一次使用

1. 在 App 填写服务端地址，使用获准的 Google 账号登录。
2. 在「我的 → 证券账户」添加账户，或使用总览中的创建入口。填写自定义名称和 Schwab 导出文件名中的账户标识，例如 `Individual_XXX276_...csv` 对应 `XXX276`。
3. 选择官方导出的 CSV 或 XLSX，检查目标账户与导入预览，再确认写入账本。也可以先选文件，再按提示创建账户并继续原文件的预览。
4. 查看持仓、交易与分析；需要 AI 解释时，按当前账户范围新建 Lyra 对话。

创建证券账户仅建立账本归属，不代表连接了券商或自动同步资产。Google 登录与证券账户分别用于身份验证和整理交易。

App 的四个主入口是「总览、股票、交易、我的」。「我的」集中管理登录身份、证券账户、启动时隐藏金额、启动账户、涨跌配色、服务端连接与版本信息。常用设置保存在本机，退出登录后保留；模型配置仍由服务端管理。详细行为见[移动端说明](apps/mobile/README.md#我的与常用设置)。

## 旧版本升级

当前客户端协议为 **2**，数据库 schema 为 **13**。App 与服务端需要一起更新；旧设备凭据会失效，用户需重新 Google 登录。旧 CLI 会话保留内容与工具记录并归档，新建会话使用 Genkit。升级前停服并备份数据库；旧服务端不能直接打开已升级数据库。

`docs/plans/` 与进度表中的早期记录保留设计演变过程；涉及 MCP、多 CLI 和配对码的旧设计已被本次迁移替代。

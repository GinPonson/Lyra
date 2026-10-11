# 在 VPS 部署 Lyra

本方案运行一个 Lyra 服务进程，以 Docker volume 保存 DuckDB，使用可选的 Caddy 提供 HTTPS。适用于当前 `feat/genkit-oauth` 分支、协议 **2**、schema **13** 和 Android **1.0.0+3**。

配置检查、服务探测和真实联调是三个不同步骤：检查命令不会打开数据库或调用第三方；服务探测读取公开状态，并向已移除的 `/mcp` 发送无 RPC 方法的空对象检查 POST 路由；Google 授权、模型工具调用和手机交互需要最后在 App 内验收。

## 1. 先准备这些信息

| 项目 | 填写位置 | 要求 |
| --- | --- | --- |
| 服务域名 | `deploy/.env` 的 `LYRA_DOMAIN` | 纯主机名，例如 `lyra.your-domain.com`；不要带协议、端口或路径 |
| Google Web OAuth client ID / secret | `deploy/server.env` | Web application 类型；密钥只存服务端 |
| Google redirect URI | Google 控制台及 `deploy/server.env` | `https://你的域名/v1/auth/google/callback`，两端完全一致 |
| 允许登录的 Google 邮箱 | `LYRA_OWNER_EMAIL` | 使用自己已验证的 Google 邮箱；也可使用稳定 subject |
| 模型基址、模型名、API key | 三项 `LYRA_GENKIT_*` | 支持流式 Chat Completions 和 tool calling 的兼容服务 |

主机需要 Docker Engine、**Docker Compose 2.30 或更高版本**；`init-env.mjs` 和 `probe.mjs` 使用 Node.js 22 或更高版本。服务本身运行在 Node 24 Debian 容器中。已有其他反向代理时，可以只启动 Lyra 容器。

构建需要访问锁文件中的 `registry.npmjs.org` 和 `mirrors.cloud.tencent.com`；没有通过修改依赖版本来绕过镜像问题。Dockerfile 在目标架构内安装原生 DuckDB 依赖，并在最终运行层执行一次内存数据库 `SELECT 1` 检查。不要将 Mac 的 `node_modules` 复制到服务器。

使用内置 Caddy 时，让域名的有效 A/AAAA 记录指向 VPS，并开放 TCP 80/443；UDP 443 用于 HTTP/3。Caddy 自动申请和续期证书的条件见[官方 HTTPS 文档](https://caddyserver.com/docs/automatic-https)。

## 2. 新安装：生成配置

以下命令均从仓库根目录运行。首次获取源码：

```bash
git clone --branch feat/genkit-oauth https://github.com/GinPonson/Lyra.git
cd Lyra
node deploy/init-env.mjs
```

初始化会创建权限为 `0600` 的 `deploy/.env` 和 `deploy/server.env`，并为**新账本**生成一次 32 字节随机主密钥；已有文件保持原样，不打印密钥。升级已有账本时，将原配置和原 `LYRA_SECRET_MASTER_KEY` 迁入，不能为旧数据换一把新密钥。

在服务器本地用编辑器填好两个文件。不要将真实配置提交到 Git，也不需要把密钥贴进聊天。

`deploy/server.env` 使用 Compose 的 `format: raw`：**一行一个 `KEY=value`，值外面不要加引号，注释单独一行**。美元符号等字符会原样传给服务，避免 API key 被 Compose 变量替换。此格式要求 Compose 2.30+，详见[官方 env_file 说明](https://docs.docker.com/reference/compose-file/services/#format)。它与 `node --env-file=server/.env` 的普通 dotenv 格式分开，不能混用引号规则。

如果暂时只使用账本，完整删除或注释三项 `LYRA_GENKIT_BASE_URL`、`LYRA_GENKIT_MODEL`、`LYRA_GENKIT_API_KEY`，之后使用不带 `--require-ai` 的检查命令。部分填写或保留占位值会被拒绝。未使用 Gmail 导入时，保持其三项变量为注释，不填写空值。

容器监听地址、端口、数据目录和公开 origin 由 Compose 固定注入，覆盖 `server.env` 中的同名值：

| 配置 | 容器实际值 |
| --- | --- |
| 监听 | `0.0.0.0:3141` |
| 数据库 | `/var/lib/lyra/lyra.duckdb` |
| 上传预览暂存 | `/tmp/lyra-import-staging` |
| 公开 origin | `https://${LYRA_DOMAIN}` |

## 3. 构建并先检查配置

```bash
./deploy/compose.sh config --quiet
./deploy/compose.sh build lyra
./deploy/compose.sh run --rm --no-deps lyra \
  node server/dist/src/cli/check-config.js --require-ai
```

检查成功退出码为 `0`，错误为 `1`，参数用法错误为 `2`。检查会复用正式入口的基础配置、Google 和 Genkit 解析，额外拒绝示例值、缺失的所有者约束、不一致的公开 origin / Google 回调和不完整的模型配置。输出只包含检查项及变量名，**不会打开或迁移数据库，也不会验证真实凭据是否有效**。

容器的正常启动入口也会先执行这项检查，通过后才 `exec` 正式服务。不要通过手动运行 `main.js` 来做配置预检，因为它会自动迁移数据库。

`config --quiet` 仅用于语法验证；不要公开粘贴不带 `--quiet` 的完整 Compose 配置，它可能展开密钥。

镜像默认使用 `node:24-bookworm-slim` 与 `caddy:2-alpine`。锁文件固定应用依赖；基础镜像的主版本标签仍会更新。需要固定部署镜像时，在 `deploy/.env` 中将对应镜像变量改成自己验证过的版本或 digest。

## 4. 启动 HTTPS 服务

没有其他反向代理占用 80/443 时：

```bash
./deploy/compose.sh --profile https up -d --wait --wait-timeout 90
./deploy/compose.sh ps
```

然后从能访问该域名的机器运行：

```bash
node deploy/probe.mjs https://你的域名
```

探测检查公开协议与 schema、账本健康、无登录访问被拒绝、缺少协议请求头时被拒绝以及 `/mcp` 的 GET / POST 均为 404。它不会登录、建账户、导入文件或请求模型。首次证书签发可能晚于容器进入运行状态；遇到 TLS 连接失败时，核对 DNS 和 Caddy 日志后重试。

```bash
./deploy/compose.sh logs --tail 100 lyra caddy
```

健康检查接受账本可用的 `degraded` 状态；可选 AI 未配置不应阻止账本服务启动。Genkit 的本地 `available` 仅表示配置完整，不能据此判断模型额度、API key、工具调用或 Google 授权已经验证。

### 已有 Nginx、Caddy 或其他 HTTPS 入口

只启动服务：

```bash
./deploy/compose.sh up -d --wait --wait-timeout 90 lyra
node deploy/probe.mjs http://127.0.0.1:3141
```

Compose 将应用端口只发布到 VPS 的 `127.0.0.1:3141`；将现有反向代理指向它。公开 App 地址和 Google 回调仍使用 HTTPS 域名。代理需要保留 `/v1` 路径、Authorization、`X-Lyra-Protocol-Version`、`Idempotency-Key` 和上传头，并支持 `/v1/events` WebSocket。

Caddy 的 `reverse_proxy` 原生支持 WebSocket，无需额外手写 Upgrade 头，见[官方说明](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。不要给 AI 消息请求设置短于回合上限的代理响应超时。内置 Caddy 没有启用访问日志，并从默认运行日志中移除请求 URI，覆盖上游故障时的错误日志，避免记录 OAuth 回调中的 code/state；过滤语法见[官方日志说明](https://caddyserver.com/docs/caddyfile/directives/log#filter)。

## 5. 数据、停止和升级

- 只运行 **一个 Lyra writer 进程**；不要用多副本或 PM2 cluster 共享这份 DuckDB。
- `ledger_data` 挂载整个数据库目录，包括 WAL 和数据库临时文件。运行用户是 `node`；新 volume 从镜像初始化可写目录。导入既有 volume 时，确认数据属于容器 UID/GID `1000:1000`，不要使用 `chmod 777`。
- `/tmp` 是上限 256 MiB 的临时内存文件系统，用于上传暂存；不是账本备份。服务重启后，尚未确认的预览需要重新选择文件并预览。容器临时目录不会保存历史账本。
- AI 回合默认上限 120 秒、可配置至 600 秒。Compose 停服宽限设为 **11 分钟**，给最长回合和写队列留出退出时间；空闲服务通常立即停止。升级前先在 App 完成或取消当前回合、处理未确认导入。
- `stop`、`down` 默认保留命名数据卷；**不要用 `down -v` 作为升级命令**。

已有进程应先停服，再复制完整数据库目录。下面演示对本 Compose 部署进行一次停服备份，需逐条确认命令成功后再继续：

```bash
mkdir -p deploy/backups
chmod 700 deploy/backups
./deploy/compose.sh stop lyra
backup_file="deploy/backups/lyra-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
(umask 077; ./deploy/compose.sh run --rm --no-deps -T --entrypoint tar lyra \
  -C /var/lib/lyra -czf - . > "$backup_file")
tar -tzf "$backup_file" >/dev/null
./deploy/compose.sh start lyra
```

备份只包含账本目录。`deploy/server.env` 中的原主密钥需要另外安全保管；数据库副本不能替代密钥。检查备份时服务已停止，不复制一份仍在写入的 DuckDB。当前没有自动备份调度，以上是手动备份操作。

完成备份后更新代码，重新构建并预检，再使用与首次部署相同的 `up -d --wait` 命令更新服务。不要同时启动旧程序和新程序访问该数据卷。

回退需要使用**升级前备份 + 相匹配的旧服务和旧 App**；不能直接拿旧程序打开已经迁移到新 schema 的库。具体迁移规则见 [Genkit 与 Google 登录](genkit-oauth.md#7-启动与升级)。

## 6. 手机上完成真实联调

安装深色界面版 `1.0.0+3`，在「我的 → 服务端连接」填入 HTTPS 地址，然后依次验证：

| 操作 | 通过条件 |
| --- | --- |
| Google 登录 | 系统浏览器完成授权，返回 App 后显示允许的邮箱；其他身份不能接管账本 |
| 建证券账户 | 新账户可选；此时没有新增交易 |
| CSV/XLSX 导入 | 预览账户与行数正确；确认后才出现持仓和交易；重复导入不重复入账 |
| 仓位问答 | 新建当前账户范围的 Lyra 对话，真实模型调用 `positions` / `summary` 等工具，界面收到流式结果 |
| 风险／压力分析 | 使用已实现的工具口径；与同范围财务页面核对，不把历史成本称为实时市值 |
| 取消与重连 | 生成中取消能结束回合；手机切网、后台返回后可恢复会话状态 |
| 设置与退出 | 隐藏金额、启动账户、涨跌配色有效；退出后旧凭据不能继续访问账本 |

客户端使用系统浏览器登录，不需要在 APK 中放 Google client secret 或模型 API key。Google 登录不会提供模型额度，Gmail 授权也继续独立。

## 7. 当前验证边界

当前工作环境没有 Docker Engine，因此不能将 Compose/schema 静态校验、Node 本机测试或配置检查当作镜像构建和 VPS 验收。镜像构建、Caddy 真实证书、公开域名、真实 Google/模型及 Android 真机结果应在执行后再更新 [进度记录](progress.md)。

部署前只需确定域名、服务器环境和需要使用的兼容模型服务；凭据在服务器本地配置。真实环境联调完成前，现有迁移 PR 继续保持草稿。

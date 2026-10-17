# 在 VPS 部署 Lyra

本方案运行一个 Lyra 服务进程，以 Docker volume 保存 DuckDB，使用可选的 Caddy 提供 HTTPS。适用于当前 `feat/genkit-oauth` 分支、协议 **2** 和 schema **15**。独立登录页与 Google / Linux.do / GitHub 入口需要构建当前移动端源码；本轮尚未重新打包 Android APK。此前 APK 的版本与验证范围见[进度记录](progress.md)。

配置校验、服务探测和真实联调是三个不同步骤：**配置在服务启动时校验**——不完整就直接退出，没有独立的检查命令；服务探测用 `server/scripts/verify_running_service.py` 读取公开状态、协议门与受保护端点的错误形状（它不会登录、建账户、导入文件或请求模型）；已启用来源的真实 OAuth 授权、模型工具调用和手机交互需要最后在 App 内验收。

## 1. 先准备这些信息

| 项目 | 填写位置 | 要求 |
| --- | --- | --- |
| 服务域名 | `deploy/.env` 的 `LYRA_DOMAIN` | 纯主机名，例如 `lyra.your-domain.com`；不要带协议、端口或路径 |
| 至少一种登录来源的 client ID / secret | `deploy/server.env` 对应的 `LYRA_LOGIN_*` 组 | Google Web application、Linux DO Connect 或 GitHub OAuth App；密钥只存服务端 |
| 各来源的 redirect URI | 对应开发者控制台及 `deploy/server.env` | `https://你的域名/v1/auth/{google\|linuxdo\|github}/callback`，各方分别登记且完全一致 |
| 首个管理员的 `(provider, subject)` | 离线入口命令行参数 | **不再有 `LYRA_OWNER_*` 环境变量**；管理员在服务启动后用 `python -m lyra.admin` 显式创建，见第 4 节 |
| 模型基址、模型名、API key | 三项 `LYRA_GENKIT_*` | 支持流式 Chat Completions 和 tool calling 的兼容服务 |

主机需要 Docker Engine、**Docker Compose 2.30 或更高版本**，以及 **`python3`**（只用于运行 `deploy/init-env.py`，它只用标准库、不装依赖）。服务运行在 **`python:3.12-slim-bookworm`** 容器中，依赖由 `uv` 按 `server/uv.lock` 安装。运行探测脚本的机器需要 `uv`（它会自行取用 Python 3.12，见第 4 节）。已有其他反向代理时，可以只启动 Lyra 容器。

构建需要访问 PyPI（`pypi.org` / `files.pythonhosted.org`）：构建阶段用 `pip` 安装固定版本的 `uv`，再由 `uv sync --frozen` 按 `server/uv.lock` 安装依赖（含按目标架构安装的 DuckDB 轮子）。运行层不再执行内存库 `SELECT 1` 检查——健康检查直接打 `/v1/health`。

使用内置 Caddy 时，让域名的有效 A/AAAA 记录指向 VPS，并开放 TCP 80/443；UDP 443 用于 HTTP/3。Caddy 自动申请和续期证书的条件见[官方 HTTPS 文档](https://caddyserver.com/docs/automatic-https)。

## 2. 新安装：生成配置

以下命令均从仓库根目录运行。首次获取源码：

```bash
git clone --branch feat/genkit-oauth https://github.com/GinPonson/Lyra.git
cd Lyra
python3 deploy/init-env.py
```

初始化会创建权限为 `0600` 的 `deploy/.env` 和 `deploy/server.env`，并为**新账本**生成一次 32 字节随机主密钥；已有文件保持原样，不打印密钥。升级已有账本时，将原配置和原 `LYRA_SECRET_MASTER_KEY` 迁入，不能为旧数据换一把新密钥。

在服务器本地用编辑器填好两个文件。不要将真实配置提交到 Git，也不需要把密钥贴进聊天。

`deploy/server.env` 使用 Compose 的 `format: raw`：**一行一个 `KEY=value`，值外面不要加引号，注释单独一行**。美元符号等字符会原样传给服务，避免 API key 被 Compose 变量替换。此格式要求 Compose 2.30+，详见[官方 env_file 说明](https://docs.docker.com/reference/compose-file/services/#format)。服务端只读进程环境变量、**不解析 `.env` 文件**，所以不存在第二套 dotenv 格式需要对齐。

### 启用登录来源

模板将三种登录全部写成注释，避免未使用来源的占位值被误认为配置。**至少完整启用一组**，也可同时启用多组；不使用的组保持整组未设置。每组只包含 client ID、client secret 与完整回调，任一组部分填写都会失败，不能因另一组可用而忽略错误配置。

| 来源 | 注册应用 | 回调路径 |
| --- | --- | --- |
| Google | Google Cloud 的 Web application OAuth client | `/v1/auth/google/callback` |
| Linux.do | [Linux DO Connect](https://connect.linux.do) 的应用接入 | `/v1/auth/linuxdo/callback` |
| GitHub | GitHub Developer settings → OAuth apps | `/v1/auth/github/callback` |

在相应开发者控制台登记自己的完整 HTTPS 回调，再在服务器本地填写同一地址。具体变量与官方说明见 [Genkit 与多来源登录](genkit-oauth.md#3-配置-googlelinuxdo-与-github)。

**不再有所有者白名单。** 三种来源都是**各自独立的登录入口**，新身份首次登录自动注册为普通用户并获得自己的空账本。身份键是已验证的 `(provider, subject)`，相同邮箱或昵称不会自动合并成一个用户。管理员必须由离线入口显式创建（见第 4 节）——任何登录路径都不会产生管理员，也没有「第一位访问者自动认领」这条路径。

修改登录配置后重启服务器（配置在启动时校验），再在 App 登录页重新检测即可看到来源可用性。配置完整不等于真实凭据已通过提供方验证；关闭来源只阻止新的授权，不代替撤销已经签发的设备会话。

如果暂时只使用账本，完整删除或注释三项 `LYRA_GENKIT_BASE_URL`、`LYRA_GENKIT_MODEL`、`LYRA_GENKIT_API_KEY`：三项全空等于未启用模型，服务照常启动。部分填写或保留占位值会在启动时被拒绝。未使用 Gmail 导入时，保持其三项变量为注释，不填写空值。

### 可选：启用历史或延迟行情

模板默认 `LYRA_MARKET_STOCK_PROVIDERS=none`、`LYRA_MARKET_OPTION_PROVIDERS=none`，推荐保留这两个值。需要行情时，先在服务器填写供应商的完整凭据并重启（凭据不完整或只填一半会在启动时失败）；对应来源随后在 App 中变为可选，填写密钥本身不会启用远程读取。

之后在 **「我的 → 行情与估值」** 分别为股票和期权选择主来源、可选备用来源或关闭；每次选择都会自动提交，服务端确认后立即对新行情查询生效，日常切源无需重启。等待时显示「正在切换」并防止重复提交，失败时界面恢复最后已确认的选择并提示重试。选择通过 schema 14 引入的配置表持久化在服务端，**按用户保存**：该用户的全部设备与 Lyra 助手共用，各用户互不影响，随数据库备份并在重启后恢复。

例如，两类资产都使用 MarketData 历史模式时，服务器填写 `LYRA_MARKETDATA_API_TOKEN`，保留 `LYRA_MARKETDATA_MODE=historical` 和每日本地请求上限 `80`；在 App 内依次将两类主来源选为 MarketData，每次选择会自动提交。该模式显式向上游请求 `mode=historical`，以实际报价时间为准；不保证收盘后立即可取，也没有后台定时任务。数据模式权限以提供方账户为准。

来源环境变量只在数据库尚未保存设置时提供默认值。已有保存记录优先，重启不会用 env 默认顺序覆盖它。保留非 `none` 的旧 env 默认值时，该配置仍需完整有效；移除对应 Key 应同步将旧默认路由清为 `none`，这不会覆盖数据库中的界面选择。已保存来源失去凭据或被移除后，界面会显示不可用状态，允许重新选择或关闭；不会静默替换为新的来源。

密钥、MarketData 模式、预算和超时仍在服务器管理，修改后需重启（配置在启动时校验来源默认值、模式、请求上限与凭据完整性；校验输出不含密钥，也不验证外部账户权限或剩余额度）。密钥不放入 App、Genkit 提示词或请求参数。完整设置契约、时点口径与缓存限制见[行情接入与换源](market-data.md)。

容器监听地址、端口、数据目录和公开 origin 由 Compose 固定注入，覆盖 `server.env` 中的同名值：

| 配置 | 容器实际值 |
| --- | --- |
| 监听 | `0.0.0.0:3141` |
| 数据库 | `/var/lib/lyra/lyra.duckdb` |
| 上传预览暂存 | `/tmp/lyra-import-staging` |
| 公开 origin | **没有这个配置项**：Compose 不再注入 `LYRA_PUBLIC_ORIGIN`，服务端也从不读取它（`Settings` 用 `extra="ignore"`，未知变量被忽略）。公开地址只由反向代理的域名与各来源登记的 HTTPS 回调决定 |

## 3. 构建镜像

```bash
./deploy/compose.sh config --quiet
./deploy/compose.sh build lyra
```

`config --quiet` 仅用于语法验证；不要公开粘贴不带 `--quiet` 的完整 Compose 配置，它可能展开密钥。

**没有独立的配置检查命令。** 配置在服务启动时校验，顺序是：入口 `lyra-serve` 先构造 `Settings`（`LYRA_DATABASE_PATH`、`LYRA_IMPORT_STAGING_DIR`、`LYRA_SECRET_MASTER_KEY` 的 64 位十六进制形态），再打开并迁移账本，最后由 `create_app` 装配登录来源、Gmail 与模型运行时。任何一项不完整都会在**开始监听之前**直接退出，不会进入"半启动"状态。

登录来源的校验规则（`load_owner_login_providers`）：每组四项（client ID / secret / redirect URI / owner subject）要么**整组未设置**（该来源关闭），要么**整组完整有效**；部分填写会直接让启动失败。redirect URI 必须是固定 HTTPS 回调路径（仅本机开发允许 HTTP），owner subject 必须是正整数用户 ID。**模型与行情配置不完整同样会失败**——让服务带着"看起来启用了但永远用不了"的配置上线是被刻意拒绝的。

因此**不要靠"容器起来了"判断配置正确**：用 `up -d --wait` 的退出码、健康状态与日志判断（见下一节）。镜像默认使用 `python:3.12-slim-bookworm` 与 `caddy:2-alpine`；Python 依赖由 `server/uv.lock` 固定，基础镜像的主版本标签与 `LYRA_UV_VERSION` 仍会随上游更新。需要固定部署镜像时，在 `deploy/.env` 里把 `LYRA_PYTHON_IMAGE` / `LYRA_CADDY_IMAGE` / `LYRA_UV_VERSION` 改成自己验证过的版本或 digest。

## 4. 启动 HTTPS 服务

没有其他反向代理占用 80/443 时：

```bash
./deploy/compose.sh --profile https up -d --wait --wait-timeout 300
./deploy/compose.sh ps
```

然后从能访问该域名的机器运行探测（需要 `uv`，在仓库的 `server/` 目录下执行）：

```bash
cd server
UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \
  uv run --python 3.12 python scripts/verify_running_service.py \
  --base-url https://你的域名 --device-token "$DEVICE_TOKEN"
```

`--base-url` 必填，`--device-token` 可选；不传令牌时会跳过带令牌的三项检查，并明确提示**没有验证过真实账本读取**。

探测逐项验证：`/v1/health` 返回 200 且 `status` 为 `healthy` **或 `degraded`**；`/v1/system/info` 的 `schema_version` 为 16、`protocol_version` 为 3；缺少 `x-lyra-protocol-version` 请求头时受保护端点返回 **426**（而不是 401）；未认证访问受保护端点返回 **401 且错误体是扁平信封**；带令牌读取账本返回 200。它不会登录、建账户、导入文件或请求模型。首次证书签发可能晚于容器进入运行状态；遇到 TLS 连接失败时，核对 DNS 和 Caddy 日志后重试。

（原先的 Node 版探测脚本已随 Node 侧整体删除，它的检查项全部并入这个脚本；`/mcp` 端点更早已移除，脚本不探测它。）

```bash
./deploy/compose.sh logs --tail 100 lyra caddy
```

健康检查接受账本可用的 `degraded` 状态；可选 AI 未配置不应阻止账本服务启动。Genkit 的本地 `available` 仅表示配置完整，不能据此判断模型额度、API key、工具调用或任何登录来源的真实授权已经验证。

### 新安装：签发邀请码 → 注册 → 提升管理员

**新身份注册一律需要邀请码**（单次有效、不过期、可撤销）。因此新部署的第一步是离线
签发一枚码；这也避免了「注册要管理员、管理员要先注册」的死锁。

```bash
# 1) 签发第一枚邀请码（明文只打印这一次）
./deploy/compose.sh exec lyra \
  python -m lyra.admin --database /var/lib/lyra/lyra.duckdb \
  --create-invite-code --invite-label 'owner'

# 2) 在 App 登录页填入它，用你的 Google 账号完成首次注册（角色 member）

# 3) 用 /v1/me 或 App「我的」页读出你的 userId，再提升为管理员
./deploy/compose.sh exec lyra \
  python -m lyra.admin --database /var/lib/lyra/lyra.duckdb \
  --promote-user '<userId>'

# 之后用管理员设备签发更多码（明文只返回一次）
curl -s -X POST https://<域名>/v1/admin/invite-codes \
  -H "Authorization: Bearer <管理员 accessToken>" \
  -H "x-lyra-protocol-version: 3" \
  -H 'content-type: application/json' -d '{"label":"给谁"}'
```

- 邀请码**只存哈希**，创建响应是唯一一次能拿到明文的地方；丢了只能重签。
- `--create-invite-code` 不需要管理员已存在（`created_by` 记为空，表示来自服务器终端）。
- 管理接口：`POST/GET /v1/admin/invite-codes`、`DELETE /v1/admin/invite-codes/{id}`（仅 admin）。
- 撤销只对未使用的码生效；已用与已撤销对客户端返回**同一个** `INVITE_CODE_INVALID`，
  避免靠错误码枚举别人的码状态。

**已有用户登录不受影响**：邀请码只在新建用户时核销，老用户既不需要它、也不会消耗它。
管理员调用账本接口时同样只能访问自己的数据——角色只影响用户管理与邀请码签发。

### 已有 Nginx、Caddy 或其他 HTTPS 入口

只启动服务：

```bash
./deploy/compose.sh up -d --wait --wait-timeout 300 lyra
cd server
UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \
  uv run --python 3.12 python scripts/verify_running_service.py \
  --base-url http://127.0.0.1:3141
```

Compose 将应用端口只发布到 VPS 的 `127.0.0.1:3141`；将现有反向代理指向它。公开 App 地址和每个已启用来源的回调仍使用同一个 HTTPS origin。代理需要保留 `/v1` 路径、Authorization、`X-Lyra-Protocol-Version`、`Idempotency-Key` 和上传头，并支持 `/v1/events` WebSocket。

Caddy 的 `reverse_proxy` 原生支持 WebSocket，无需额外手写 Upgrade 头，见[官方说明](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。不要给 AI 消息请求设置短于回合上限的代理响应超时。内置 Caddy 没有启用访问日志，并从默认运行日志中移除请求 URI，覆盖上游故障时的错误日志，避免记录 OAuth 回调中的 code/state；过滤语法见[官方日志说明](https://caddyserver.com/docs/caddyfile/directives/log#filter)。

## 5. 数据、停止和升级

- 只运行 **一个 Lyra writer 进程**；不要用多副本或 PM2 cluster 共享这份 DuckDB。
- `ledger_data` 挂载整个数据库目录，包括 WAL 和数据库临时文件。运行用户是 `lyra`（容器内 UID/GID **10001:10001**）；新建的命名卷按镜像内的属主初始化，所以 `up` 起来即可写。**导入既有 volume 或改用 bind mount 时**，确认数据属于 `10001:10001`（例如 `chown -R 10001:10001 <目录>`），不要使用 `chmod 777`——属主不对时 DuckDB 建不了库、也写不了 WAL，而症状要到首次启动才暴露。
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

备份包含账本目录及数据库中的行情来源选择。`deploy/server.env` 中的原主密钥和供应商凭据需要另外安全保管；数据库副本不能替代密钥。检查备份时服务已停止，不复制一份仍在写入的 DuckDB。当前没有自动备份调度，以上是手动备份操作。

完成备份后更新代码并重新构建，再用与首次部署相同的 `up -d --wait` 命令更新服务（配置在启动时校验，没有独立的预检命令）。不要同时启动旧程序和新程序访问该数据卷。

schema 14 只新增行情选择的持久化，不撤销 schema 13 的有效 OAuth 设备凭据。schema 15 将所有者绑定扩展为按来源固定 subject，并允许新来源没有邮箱；已有 Google 绑定、有效 OAuth 设备会话、账本及行情选择保留。若从 schema 12 或更早版本升级，则仍会经过 schema 13 的旧匿名配对凭据撤销，需要使用已配置的来源重新登录。

回退需要使用**升级前备份 + 相匹配的旧服务和旧 App**；不能直接拿旧程序打开已经迁移到新 schema 的库。具体迁移规则见 [Genkit 与多来源登录](genkit-oauth.md#7-启动与升级)。TS → Python 切流的回退（TS 源码只存在于 git 历史、镜像 `lyra-server:local` 无固定 tag/digest）见[切流运行手册](plans/lyra-cutover-runbook-20261013.md)第 6 阶段。

### 多用户账本升级（schema 16，需要重新登录）

schema 16 引入**多用户独立账本**：同一个 Server 支持少量用户各自拥有独立账本，设备、证券账户、导入作业、Agent 会话、Gmail 连接与行情设置都带上用户归属。协议同时升到 **3**，服务端**不再接受协议 2 的客户端**。

**这次升级与以往不同：旧设备凭据会被全部撤销，所有人都要重新 OAuth 登录。** 这是有意的——schema 15 的凭据是在没有用户概念时签发的，让它们穿过一次授权模型的引入是反方向。凭据只改状态不删行，重放检测与审计需要保留它们。

升级步骤（每一步成功后再继续）：

1. **先完成本地验收**：`uv run pytest -q` 与 `flutter analyze/test` 全绿，且已准备好协议 3 的 App。App 与服务端必须同时升级——协议 2 的 App 会被 426 拒绝，那是它唯一能据此提示升级的信号。
2. **停服并备份**（见上一节的备份命令）。除数据库目录外，`deploy/server.env` 里的主密钥与供应商凭据要另外保管——**数据库副本不能替代密钥**，Gmail token 是加密存储的。
3. **显式确认原 owner 的每一来源身份**。旧库的 `server_owner_identity` 只是「曾经登录过」的记录，**不是原账本的永久授权**。逐个核对后把它们作为 `--owner-identity` 传入；**没有确认的身份不会被关联到原用户**，它们之后按自动注册规则各自得到独立空账本。非空库若无法确定归属，迁移会明确失败——这是刻意的，不要绕过。
4. **跑离线升级入口**（容器内执行，路径按实际挂载调整）：

   ```bash
   python -m lyra.admin --database /var/lib/lyra/lyra.duckdb \
     --owner-identity google:<原 Google subject> \
     --owner-identity github:<原 GitHub subject> \
     --provider google --subject <既有设备使用的那个身份> \
     --display-name '<原用户显示名>'
   ```

   `--provider/--subject` 在旧库升级时表示**既有设备的身份来源**，它必须已经列在 `--owner-identity` 里。设备原有的「本次登录用了哪个身份」没有被记录，因此必须显式指定；不指定而旧库又有设备时，迁移会明确失败而不是替谁挑一条。

   入口会打印归属摘要、已确认身份，以及**未被确认的历史身份警告**——那些身份对应的用户升级后会登录到空账本，漏看这条警告是本次升级最主要的操作风险。

5. **核对结果并重启**：入口完成后重新打开库确认 `users` / `user_identities` / 各表的 `user_id` 归属，再启动新服务。所有设备此时凭据已撤销，用户需要重新登录；重新登录会按 `(provider, subject)` 关联回原用户，账本、Gmail 与历史都会回来。
6. **在受限访问范围内验证后再开放**：先用合成或明确授权的测试账号验证自动注册与用户间隔离，确认无误后再对真实用户开放。

成员关系、共享账本、自助绑定额外登录方式、管理后台都**不在首版范围**：身份键始终是已验证的 `(provider, subject)`，**同邮箱的不同来源身份不会自动合并**。

**失败与回退**：升级失败时保持服务关闭并报告原因，不要带着半迁移状态启动。回退使用**升级前的完整备份 + 相匹配的旧服务与旧 App**；旧程序不能打开 schema 16 的库，也不能只恢复部分表。新版本一旦接收过写入，就不能直接用旧备份覆盖——要先单独保存新增数据并制定恢复方案。

## 6. 手机上完成真实联调

构建与当前服务端匹配的 App，并通过 `--dart-define=LYRA_SERVER_URL=实际部署的HTTPS地址` 注入后台地址。新安装在独立登录页直接选择账号，用户不填写地址或管理连接；旧安装继续使用已有凭据绑定或历史记住的原地址。此前版本的 APK 不包含本轮简化登录页与新增登录方式，当前验收需要重新构建移动端源码。

| 操作 | 通过条件 |
| --- | --- |
| 独立登录页 | 未登录只显示 Google / Linux.do / GitHub 三个入口，不显示地址、连接配置或服务端版本；不可用方式禁用，读取失败可重试，完成授权后进入四 Tab 工作台 |
| Google 登录（已启用时） | 系统浏览器完成授权，返回 App 后显示 Google 身份；原有绑定和有效会话在升级后保留，其他 Google 身份不能接管账本 |
| Linux.do 登录（已启用时） | 仅配置的稳定用户 ID 可访问；无邮箱仍可显示 Linux.do 来源与显示名，取消或授权失败可重新发起 |
| GitHub 登录（已启用时） | 仅配置的稳定用户 ID 可访问；使用昵称或相同邮箱不能绕过限制，不要求用户公开邮箱 |
| 多来源与已绑定身份 | 显式配置的各来源访问同一账本；改变某来源 owner ID 不能覆盖该来源已绑定的身份，跨来源事务和兑换不能混用 |
| 建证券账户 | 新账户可选；此时没有新增交易 |
| CSV/XLSX 导入 | 预览账户与行数正确；确认后才出现持仓和交易；重复导入不重复入账 |
| 仓位问答 | 新建当前账户范围的 Lyra 对话，真实模型调用 `positions` / `summary` 等工具，界面收到流式结果 |
| 风险／压力分析 | 使用已实现的工具口径；与同范围财务页面核对，不把历史成本称为实时市值 |
| 行情与参考估值 | 启用已具备权限的来源后，总览 / 股票显示实际报价时间、来源和选价依据；缺失或过期报价不补零、不计入完整合计；多空期权按合约乘数核对 |
| 界面切源 | 「我的 → 行情与估值」每次选择股票 / 期权主备来源或关闭即自动提交，确认后刷新行情；等待时防重复、失败恢复已确认选择；重启与其他设备可读回，未配置源不可选，冲突提示重新加载再选择 |
| 取消与重连 | 生成中取消能结束回合；手机切网、后台返回后可恢复会话状态 |
| 设置与退出 | 隐藏金额、启动账户、涨跌配色有效；成功退出后返回独立登录页，服务端已撤销的本设备凭据不能继续访问；撤销失败有明确提示 |

客户端使用系统浏览器登录，三种来源的 client secret 和模型 API key 都只保存在服务端。身份登录不会提供模型额度，Gmail 授权继续独立；GitHub / Linux.do 不需要经验证邮箱来访问显式获准的账本。

## 7. 当前验证边界

当前工作环境没有 Docker Engine，因此不能将 Compose/schema 静态校验或本机 Python 测试当作镜像构建和 VPS 验收。镜像构建、Caddy 真实证书、公开域名、Google / Linux.do / GitHub 真实授权、真实模型及 Android 真机结果应在执行后再更新 [进度记录](progress.md)。

服务端验证入口是 `cd server && uv run --frozen pytest -q`（全量测试）与 `uv run --frozen ruff check .`（错误类静态检查）；HEAD（`ed522c7`）实测 **2307 项通过 / 4 项跳过**（数字随提交变动，以执行时输出为准）。该结果来自受控身份提供方与本地测试，不代表真实 OAuth 已上线。本轮未部署到 VPS、未重新构建 APK，也未用真实行情 Key 完成联调。

部署前需要确定域名、服务器环境、至少一个登录来源，以及按需使用的兼容模型服务和首个管理员的 `(provider, subject)`；凭据在服务器本地配置。真实环境联调完成前，现有迁移 PR 继续保持草稿。

## 8. 登录入口的已知安全边界

当前登录流程的**事务绑定的是发起方，而不是在浏览器里完成授权的人**（完整说明见 [多来源登录的安全边界](genkit-oauth.md#app-登录过程)）。Google、Linux.do 和 GitHub 均沿用这一流程；任何能访问登录入口的人都可以自行创建登录事务，把生成好的授权链接发给**某个用户**，诱导其完成授权后领取**该用户**的设备凭据。来源隔离、state 与 PKCE 不能证明浏览器授权者持有发起设备。

**多用户下这条边界的影响面更大**：首次登录会自动注册，未登录过的第三方也能自建事务，因此登录入口的暴露范围直接决定谁能在这个部署上获得账号。

该问题本轮未修复（需要引入只有人类可传递的共享秘密，属协议与交互改动）。在该改动落地前，运维必须遵守：

- 只使用自己在 App 内选择 Google、Linux.do 或 GitHub 后生成的授权页，**不要点击或转发他人提供的 Lyra 授权链接**；
- 不要把登录入口暴露在不受信任的网络或公开索引中；公网部署应限制来源范围，并确保只有你信任的用户能访问该域名；
- 管理员账号用离线入口显式登记（见「新安装：显式创建首个管理员」），不要依赖任何自动认领路径；
- 「我的 → 确认退出」只请求撤销当前设备凭据；不能将当前设备退出视为其他设备已撤销，服务端撤销失败时也不能视为凭据已失效。

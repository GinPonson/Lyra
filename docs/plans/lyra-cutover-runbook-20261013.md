# Lyra 切流运行手册（TS → Python）

> **状态：待执行。** 本手册列出切流所需的每一步与其验收方式。
> **手册的存在不等于切流已完成**——执行结果必须逐项记录在文末。
>
> **部署形态**：本手册的数据库步骤（阶段 0/1/6）假定**宿主机能直接按路径访问账本文件**——
> 即裸机 / systemd 部署：`$LYRA_DATABASE_PATH`、配置目录 `/etc/lyra/`，停服用 `systemctl`。
> 若线上是 **Docker Compose 部署**（见[服务端部署](../deployment.md)），账本在 `ledger_data`
> 命名卷里、宿主机没有直接路径：按 deployment.md 第 5 节的 `compose run --entrypoint tar`
> 写法先把卷备份出来，再对取出的文件执行阶段 6；容器属主是 `lyra`（UID/GID 10001）。
> 阶段 2 的启动命令与部署形态无关，阶段 3/4/5 也不受影响。

## 为什么不能"两边同时跑一段时间"

**DuckDB 是单进程文件锁，第二个进程连只读都打不开。** 因此 TS 与 Python **不能共存**：
不存在"灰度期"或"双写期"。切流是一次原子切换，回退也只能整体回退。

这决定了本手册的形状：**没有逐步放量的阶段，只有"切换前把一切验证完"和"一次切换"。**

## 阶段 0：切换前必须已成立的条件

**不要靠读表格判断——跑脚本：**

```bash
cd server
UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \
  uv run --python 3.12 python scripts/preflight_cutover.py
```

脚本逐项打印「通过 / 未通过」并在有未通过项时**拒绝放行**。

### 自动检查的七项

| # | 条件（与脚本输出同名） | 判定方式 |
| --- | --- | --- |
| 0.1 | Python 全量测试 | `uv run --python 3.12 pytest -q` 退出码为 0 |
| 0.2 | 错误类静态检查 | `uv run --python 3.12 ruff check .` 通过——**项目级扫描**，含 `scripts/`、`tests/` |
| 0.3 | 接管既有账本 | 在当前解释器里只读打开 `tests/fixtures/typescript-ledger-schema15.duckdb`：迁移前 TS 服务端建的 schema 15 夹具，能读出版本与表数才算通过 |
| 0.4 | 模式版本自洽 | 迁移清单末条 == `CURRENT_SCHEMA_VERSION` |
| 0.5 | 协议版本为 2 | `LYRA_PROTOCOL_VERSION == 2`（Flutter 无需改动的依据） |
| 0.6 | 路由契约 | 实际路由与签入的 `tests/fixtures/route-manifest.json` 完全一致——见下方说明 |
| 0.7 | LYRA_SECRET_MASTER_KEY 形态 | 64 位十六进制**且能正常加解密**；"这把密钥是否就是原部署使用的那把"脚本无法判定——**必须在阶段 1 备份完成后离线验证（强制步骤，片段见下）** |

**原先的第 3 项是「TS 基线仍可用（`npm run check`）」，已删除。** TS 服务端连同根
`package.json`/`package-lock.json` 一起从仓库移除了，这条条件**永远不可能再满足**；
把它留在表里，执行者要么卡在一个无法达成的门槛上，要么学会忽略这张表。它原本承担的
作用改由 0.3 的**冻结夹具**承担——「能打开一个不是本实现造的、迁移前 TS 建的库」是
切换后**唯一不能重试**的能力，它的证据必须来自夹具，而不是那个正在被退役的实现。

### 为什么 0.6 必须比「路径」而不是「路径集合」

**按路径求差集只能发现"整条路径缺失"，发现不了"某条路径上少了一个方法"。**

这一点曾真实造成误判：`POST /v1/ledger/accounts` 整块缺失，而路径级对账把它算作一致
（因为 `GET /v1/ledger/accounts` 存在）。**该缺口已补齐**，现在的对账是 (方法, 路径) 对。

对账的对手方已从 TS 的路由注册源码换成签入的 `tests/fixtures/route-manifest.json`
（迁移期间逐项对账的结果）：保护没有消失，只是从「比对另一个实现」变成了「回归检测」。

### 逐项状态（**历史记录**：TS 与 Python 并存时期，2026-10-13 复核）

下表是**切换前两家并存时**的复核结果，只作历史对照，**不能当作当前状态**：

- 「TS 基线」一行指向已从仓库删除的 TS 服务端，**无法再复现**；
- 「全量测试」的数字是当时 Python 侧的快照，此后仍在变动；
- 当前状态**必须**以本阶段实跑 `preflight_cutover.py` 的输出为准，并记录到文末。

| 条件 | 当时状态 |
| --- | --- |
| 全量测试 | ✅ 2275 通过 / 4 跳过（当时） |
| 错误类静态检查 | ✅ 通过 |
| TS 基线 | ✅ 139 suites / 514 tests / 0 fail（**TS 已删除，不可复现**） |
| 模式版本 | ✅ 两侧均为 15 |
| 协议版本 | ✅ 2 |
| 路由（方法, 路径）对 | ✅ TS 47 / Python 47 |
| 主密钥形态 | ⏳ 由执行者设置 |

### 已闭合的旧缺口（**此表曾列出四项，全部已完成**）

| 旧缺口 | 现状 |
| --- | --- |
| Excel 解析未迁移 | ✅ 已实现（xlsx 走真实解析，不再是 501） |
| 行情传输层是占位 | ✅ 已接线 httpx |
| Agent 模型绑定为自定义 HTTP | ✅ 确认行为等价（生态无对应插件） |
| 幂等记录仓储未建 | ✅ 已实现（`mutation_idempotency_records` + 完整编排） |

**这张表曾经是"切换前必须处理"的清单，而四项都已处理完却仍标着未处理**——一份过时的
阻塞清单比没有更危险：它会让执行者去做已经不必做的事，或据此怀疑整个切换是否成熟。

### 预检**不覆盖**、但切换前**必须做**的三件事

- **阶段 1 的备份**——它必须在真实数据库上执行；
- **阶段 4 的真实设备联调**——它是验收标准，脚本无法替代；
- **主密钥必须能解开备份里的既有密文（0.7 的后半）——阶段 1 备份完成后的强制验证，
  未通过不得进入阶段 2**。预检不能打开生产库（DuckDB 单进程锁：TS 还在跑时连只读都
  打不开），所以这件事只能在**阶段 1 的冷备份**上离线做。备份完成、尚未切换时执行
  （`LYRA_SECRET_MASTER_KEY` 需已在当前 shell 中导出）：

  ```bash
  cd server
  UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \
    uv run --python 3.12 python - "<备份文件路径>" <<'PY'
  import os
  import sys

  import duckdb

  from lyra.security.secret_cipher import SecretCipher, master_key_from_hex

  connection = duckdb.connect(sys.argv[1], read_only=True)
  try:
      rows = connection.execute(
          "SELECT id, encrypted_refresh_token FROM gmail_connections "
          "WHERE encrypted_refresh_token IS NOT NULL"
      ).fetchall()
  finally:
      connection.close()

  cipher = SecretCipher(master_key_from_hex(os.environ["LYRA_SECRET_MASTER_KEY"]))
  for connection_id, envelope in rows:
      # AAD 是 `gmail-connection:<连接 ID>`（见 connection_credential.py）。
      cipher.decrypt(envelope, f"gmail-connection:{connection_id}")
  print(f"{len(rows)} 条已存密文全部解开")
  PY
  ```

  解不开即说明**这把密钥不是原部署使用的那把**——不要开始切换。若一条密文都没有
  （`0 条已存密文全部解开`），这项验证是空转，必须在文末如实记录，不能算作通过。

## 阶段 1：备份（不可跳过）

```bash
# 1) 停掉 TS 服务，确保没有写入者
#    必须先停——带着活跃写入者复制 DuckDB 文件会得到不一致的快照
systemctl stop lyra-server

# 2) 冷备份数据库（含 WAL）
cp -a "$LYRA_DATABASE_PATH" "$LYRA_DATABASE_PATH.bak-$(date +%Y%m%d%H%M%S)"
cp -a "$LYRA_DATABASE_PATH.wal" "$LYRA_DATABASE_PATH.wal.bak-$(date +%Y%m%d%H%M%S)" 2>/dev/null || true

# 3) 备份部署配置
#    **注意：`application-gin.yml` 含敏感凭据，不得进入任何 git 操作或归档上传。**
tar czf "/tmp/lyra-config-$(date +%Y%m%d%H%M%S).tgz" /etc/lyra/

# 4) **验证备份可用**——未验证的备份等于没有备份
python3 -c "
import duckdb
con = duckdb.connect('$LYRA_DATABASE_PATH.bak-...', read_only=True)
print('schema:', con.execute('SELECT max(version) FROM schema_migrations').fetchone())
print('accounts:', con.execute('SELECT count(*) FROM accounts').fetchone())
print('transactions:', con.execute('SELECT count(*) FROM transactions').fetchone())
"
```

**备份验证的记录必须写进文末**：一个打不开的备份在需要回退时才会被发现。

**以上步骤已脚本化——用它，不要手工执行：**

```bash
cd server
UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \
  uv run --python 3.12 python scripts/backup_ledger.py \
  --database "$LYRA_DATABASE_PATH" --output-dir /var/backups/lyra \
  --i-have-stopped-the-server
```

脚本的三个性质使它与手工步骤不同：

| 性质 | 为什么重要 |
| --- | --- |
| **不确认服务已停止就拒绝执行** | 手动 `cp` 不会拦你；带写入者复制得到的快照**看起来正常但不一致**，而验证查询很可能仍然通过 |
| **备份与验证绑成一步** | 分成两步时，验证是那个在紧张时被跳过的步骤 |
| **只读打开并查询关键表** | 覆盖账本主体、凭据（配对设备）与迁移状态三类；只查一张表可能漏掉"主体可读但凭据表损坏" |

脚本**只处理数据库文件，不触碰配置目录**——`application-gin.yml` 含敏感凭据，不得进入任何归档。

### 阶段 1 的收尾（**强制**）：验证主密钥能解开备份里的既有密文

`backup_ledger.py` 只证明"文件可读、关键表存在"，**证明不了"这把密钥就是当初写入 Gmail
密文的那把"**——那个错误要到用户点开 Gmail 时才暴露，而那时已经切完流了。

所以在进入阶段 2 之前，**必须**拿**刚生成的备份文件**跑一次上文阶段 0「预检不覆盖、但切换前必须做的三件事」一节里的主密钥验证片段：

- 全部解开 → 在文末执行记录的「1 备份 + 验证」一行记下条数与结果；
- 解不开 → **停止，不要进入阶段 2**，先找回原部署使用的主密钥；
- 输出 `0 条已存密文全部解开` → 说明库里没有既有密文，这项验证是**空转**，如实记录，
  不能当作通过。

## 阶段 2：启动 Python 服务

```bash
cd server
uv sync --python 3.12
uv run --python 3.12 lyra-serve
```

**入口是 `lyra-serve`**（`pyproject.toml` 的 `[project.scripts]`）。**启动方式与部署形态
无关**——systemd / 容器 / 裸进程都只是调用这条命令，因此阶段 2 的脚本不预设任何一种。

`python -m lyra.main` 等价可用。

**启动即失败的情形**（不会进入"半启动"状态）：配置缺失、主密钥形态不对、数据库打不开
或迁移失败——`run()` 在这些情况下直接退出。

**启动后跑自检，不要靠肉眼看日志：**

```bash
cd server
UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \
  uv run --python 3.12 python scripts/verify_running_service.py \
  --base-url http://127.0.0.1:3141 --device-token "$DEVICE_TOKEN"
```

它逐项验证并给出可判定的结论：

| 检查 | 判定 |
| --- | --- |
| `/v1/health` | 200，且 `status` 为 `healthy` **或 `degraded`** |
| `/v1/system/info` | `schema_version` 为 15、`protocol_version` 为 2 |
| 缺少协议头访问受保护端点 | **426**（而不是 401）——401 说"凭据不对"，426 说"App 版本不兼容" |
| 受保护端点未认证 | **401 且错误体是扁平信封**（`code` / `message` / `retryable` / `request_id`） |
| 带令牌读取账本 | 200（**"能服务"的唯一直接证据**） |
| `?scope=all` | 200 |
| 非法 `scope` | **400**（而不是静默返回全部账户） |

**`degraded` 算通过**——它表示服务能工作、只是部分能力受损；把 degraded 当失败会让一个
可用的服务被误判为不可用。脚本会额外打印 degraded 的原因。

**过程"起来了"与"能服务"是两件事**：uvicorn 打印 "Application startup complete" 之后，
服务仍可能因为数据库不可读或路由装配不全而无法真正工作。

## 阶段 3：探针切换

新服务用的探针就是阶段 2 的自检脚本（原先的 Node 版探针脚本已随迁移删除，它的检查项
全部并入此脚本）。**用同一个脚本对新地址再跑一次**，不要凭肉眼看日志：

```bash
cd server
UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \
  uv run --python 3.12 python scripts/verify_running_service.py \
  --base-url http://127.0.0.1:3141 --device-token "$DEVICE_TOKEN"
```

脚本参数只有两个：`--base-url`（**必填**）与 `--device-token`（可选）。不传
`--device-token` 时会跳过「带令牌读账本」「`?scope=all`」「非法 `scope`」三项，并在
结尾明确打印**没有验证过真实账本读取**——那不是通过。

要手工确认单条请求时注意：受保护端点必须带协议头，否则得到的是 **426**（App 版本不兼容）
而不是账本内容。脚本自己会带上它，手写 `curl` 时要自己加：

```bash
curl -sS http://127.0.0.1:3141/v1/health
curl -sS -H "x-lyra-protocol-version: 2" -H "Authorization: Bearer $DEVICE_TOKEN" \
  http://127.0.0.1:3141/v1/ledger/summary
```

把 Flutter 客户端的 base URL 指向 Python 服务。**协议版本仍为 2，客户端无需重新构建。**

## 阶段 4：真实设备联调（**验收标准，不可跳过**）

| # | 场景 | 通过标准 |
| --- | --- | --- |
| 4.1 | 冷启动 | 已配对设备无需重新登录即可读取账本 |
| 4.2 | 账本读取 | 账户 / 持仓 / 摘要与切换前逐字段一致 |
| 4.3 | CSV 导入 | 上传 → 预览 → 提交全链路成功，笔数与切换前一致 |
| 4.4 | 重复导入 | 同一文件二次提交不产生重复交易 |
| 4.5 | 令牌刷新 | access token 过期后自动刷新，无需重新登录 |
| 4.6 | 退出登录 | 撤销后旧令牌立即失效 |
| 4.7 | Gmail 同步 | 授权 → 同步 → 交易入账（若用户使用） |
| 4.8 | Agent 会话 | 模型已配置时收发消息（若用户使用） |

**任何一项不通过都进入阶段 6（回退），不试图在切换后修补。**

## 阶段 5：观察

- 连续运行 **至少一个完整的同步与导入周期**；
- 确认没有"交易存在但消息未归档"的中间态（§107.1）；
- 确认日志里没有未预期的 5xx。

## 阶段 6：回退（预演过才算数）

**停掉 Python 服务**——继续持有账本文件锁的话，回退脚本会拒绝执行：

```bash
systemctl stop lyra-python
```

**然后跑脚本，不要手工 `cp`：**

```bash
cd server
UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \
  uv run --python 3.12 python scripts/rollback_cutover.py \
  --database "$LYRA_DATABASE_PATH" \
  --backup /var/backups/lyra/lyra.duckdb.bak-20261013T120000Z \
  --i-have-stopped-python
```

`--backup` 指向阶段 1 由 `backup_ledger.py` 产出的文件，命名形如
`<数据库文件名>.bak-<UTC 时间戳>`（`LYRA_DATABASE_PATH=/var/lib/lyra/lyra.duckdb` 时即
`lyra.duckdb.bak-…`）；用真实时间戳替换示例中的值。

不确定时先加 `--dry-run`：它只验证备份并打印将要执行的动作，**不改动任何文件**。

### 回退的最后一步：把 TS 服务端找回来（**源码已不在仓库里**）

回退脚本**只还原数据库**。它在结尾打印的「下一步：启动 TS 服务，并按阶段 3 重新对探针
做一次确认」是迁移前的遗留文本——脚本无法判断 TS 服务端从哪里来，所以**必须由执行者
完成下面这一步**。

TS 服务端（`server/src/**`、根 `package.json`/`package-lock.json`）已随本次提交删除，
「启动 TS 服务」只有两个来源。**先确认来源，再停服回退**：

1. **本地保留的旧镜像**（仅容器形态）。确认标签里装的仍是 TS 镜像，而不是被 Python 构建
   覆盖过的同名镜像：

   ```bash
   docker image inspect lyra-server:local --format 'user={{.Config.User}} cmd={{.Config.Cmd}}'
   # Python 镜像 → user=lyra   cmd=[lyra-serve]
   # 旧 TS 镜像 → user=node   cmd=[/bin/sh …]（指向当时的容器启动脚本）
   ```

   `deploy/compose.yaml` 的镜像是 `lyra-server:local`，**没有固定 tag 或 digest**，因此
   只要在切换后又构建过一次 `lyra`，旧镜像就已经被覆盖、这条路走不通。

2. **git 历史里的最后一个 TS 提交 `6887bec`**（`ed522c7^`，即迁移提交的父提交）。
   **需要在服务器上有外网**（`npm ci` 与可能的镜像构建都要拉依赖）：

   ```bash
   git fetch --all --tags          # 服务器上的克隆未必已有该提交
   git worktree add --detach /tmp/lyra-ts 6887bec
   cd /tmp/lyra-ts
   npm ci
   ```

   接着按部署形态二选一：

   - **裸机 / systemd**：构建后把原来的 systemd 单元指向这个 worktree 的入口
     （`/tmp/lyra-ts/server`，入口 `node dist/src/main.js`，环境变量按原来的方式注入）：

     ```bash
     npm run build
     cd server && npm start        # = node dist/src/main.js
     ```

   - **Docker Compose**：用旧 compose 重建镜像并启动。旧 compose 需要 `.env` /
     `server.env`，从正式部署目录复制过去（权限 0600，用完删除）：

     ```bash
     cp -a <正式部署目录>/deploy/.env <正式部署目录>/deploy/server.env /tmp/lyra-ts/deploy/
     ./deploy/compose.sh build lyra
     ./deploy/compose.sh up -d --wait --wait-timeout 300
     ```

     **`build` 会用 TS 源码重建并覆盖 `lyra-server:local` 标签**——这正是回退想要的结果，
     但要清楚该标签从此代表 TS 镜像；再切回 Python 时必须重新构建。

   新旧版本使用同一组 `LYRA_*` 环境变量名与同一份 `deploy/server.env` 格式，所以配置不需要改。

   回退稳定后清理（容器形态还要删掉复制过去的密钥文件）：

   ```bash
   rm -f /tmp/lyra-ts/deploy/.env /tmp/lyra-ts/deploy/server.env
   git worktree remove /tmp/lyra-ts
   ```

**前置条件（切流前就要确认，而不是回退时才发现）**：至少一条来源可用——容器形态要确认
旧镜像仍在（或在服务器上能拉依赖重建），裸机形态要确认服务器能拉 npm 依赖并完成构建。
若都不满足，回退后**没有任何可运行的 TS 服务端**——此时还原一个切换前的库也没有意义。
因此回退预演必须在切换前完成，并记录在文末。

### 脚本的三道拒绝

回退是**整个切换里唯一"一旦做错就同时失去两边"的操作**，因此脚本的核心不是"能还原
文件"，而是三道拒绝：

| 拒绝 | 后果 |
| --- | --- |
| **不确认 Python 已停止** | 两个进程争同一个文件锁只会都失败——**而失败发生在回退过程中** |
| **备份自身验证不通过** | 先覆盖再验证的话，一次损坏的备份会让**两份都没了** |
| **不把当前库另存** | 见下方"代价" |

### 一处比手工步骤更好的地方

**切换窗口内的数据被保留成 `.post-cutover-<时间>` 文件。**

下面说"这些写入会丢失"——但**它们不必不可逆地消失**。保留一份文件几乎不花代价，而在
回退后想捞回那段时间的写入时，它是唯一的东西。手工 `cp` 覆盖不会保留它。

**还原方式**：整个文件还原，**不做增量修补**——增量修补需要理解两侧的模式差异，而回退
时刻没有做那种判断的余裕。

### 代价（仍然成立）

切换后到回退前的所有写入**不在活动库里**（但已保留成文件，见上）。因此阶段 5 的观察期
应尽量短，或先确认系统稳定再让真实用户写入。

## 阶段 7：TS 退役（**没有待执行的步骤**）

**TS 服务端只存在于 git 历史里**：`ed522c7` 已经删除 TS 服务端（`server/src/**`、根
`package.json`/`package-lock.json`），并把 `server/` 让给 Python 实现。因此：

- 旧版手册"打 tag → 归档 → 删除 `server/`"的步骤**已失效，此处不再保留**。特别地，
  删除 `server/` 的命令绝不能执行：那个路径现在就是**现役的 Python 服务端**；
- 迁移前的 TS 状态需要时从 git 历史取：最后一个 TS 提交是 **`6887bec`**（`ed522c7^`），
  检出方式见阶段 6；
- `git tag -l` 目前为空。是否补一个 `lyra-ts-final` 标签由执行者决定，它不影响切流。

**清账前的确认项**（保留原"删除前必须确认"清单的意图）：

- 阶段 4 全部通过并有记录；
- 阶段 5 观察期内无异常；
- 需要归档时，归档只取自 git 历史——`deploy/.env`、`deploy/server.env` 与
  `application-gin.yml` 从未被 git 跟踪，因此不会进入任何归档。

## 执行记录（由执行者填写）

| 阶段 | 时间 | 结果 | 备注 |
| --- | --- | --- | --- |
| 0 前置 | | | |
| 1 备份 + 验证 | | | |
| 2 启动 | | | |
| 3 探针 | | | |
| 4 设备联调 | | | |
| 5 观察 | | | |
| 6 回退（如需要） | | | |
| 7 TS 退役 | 已完成（`ed522c7`） | 无待执行步骤 | TS 只存在于 git 历史：`6887bec` |

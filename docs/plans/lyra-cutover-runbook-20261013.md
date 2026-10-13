# Lyra 切流运行手册（TS → Python）

> **状态：待执行。** 本手册列出切流所需的每一步与其验收方式。
> **手册的存在不等于切流已完成**——执行结果必须逐项记录在文末。

## 为什么不能"两边同时跑一段时间"

**DuckDB 是单进程文件锁，第二个进程连只读都打不开。** 因此 TS 与 Python **不能共存**：
不存在"灰度期"或"双写期"。切流是一次原子切换，回退也只能整体回退。

这决定了本手册的形状：**没有逐步放量的阶段，只有"切换前把一切验证完"和"一次切换"。**

## 阶段 0：切换前必须已成立的条件

**不要靠读表格判断——跑脚本：**

```bash
cd server-py
UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \
  uv run --python 3.12 python scripts/preflight_cutover.py
```

脚本逐项打印「通过 / 未通过」并在有未通过项时**拒绝放行**。

### 自动检查的七项

| # | 条件 | 判定方式 |
| --- | --- | --- |
| 0.1 | Python 全量测试 | `pytest` 退出码为 0 |
| 0.2 | 错误类静态检查 | `ruff check src/` 通过 |
| 0.3 | **TS 基线仍可用** | `npm run check` 全绿——它既是差分 oracle，也是回退的退路 |
| 0.4 | 模式版本一致 | 两侧 `CURRENT_SCHEMA_VERSION` 相同 |
| 0.5 | 协议版本为 2 | `LYRA_PROTOCOL_VERSION == 2`（Flutter 无需改动的依据） |
| 0.6 | **路由（方法, 路径）对一致** | 见下方说明 |
| 0.7 | `LYRA_SECRET_MASTER_KEY` 形态 | 64 位十六进制——形态不对会读不出 TS 写入的密文 |

### 为什么 0.6 必须比「路径」而不是「路径集合」

**按路径求差集只能发现"整条路径缺失"，发现不了"某条路径上少了一个方法"。**

这一点曾真实造成误判：`POST /v1/ledger/accounts` 整块缺失，而路径级对账把它算作一致
（因为 `GET /v1/ledger/accounts` 存在）。**该缺口已补齐**，现在的对账是 (方法, 路径) 对。

### 逐项状态（2026-10-13 复核）

| # | 条件 | 状态 |
| --- | --- | --- |
| 0.1 | 全量测试 | ✅ **2275 通过 / 4 跳过** |
| 0.2 | 错误类静态检查 | ✅ 通过 |
| 0.3 | TS 基线 | ✅ 139 suites / 514 tests / 0 fail |
| 0.4 | 模式版本 | ✅ 两侧均为 15 |
| 0.5 | 协议版本 | ✅ 2 |
| 0.6 | 路由（方法, 路径）对 | ✅ **TS 47 / Python 47** |
| 0.7 | 主密钥形态 | ⏳ **由执行者设置** |

### 已闭合的旧缺口（**此表曾列出四项，全部已完成**）

| 旧缺口 | 现状 |
| --- | --- |
| Excel 解析未迁移 | ✅ 已实现（xlsx 走真实解析，不再是 501） |
| 行情传输层是占位 | ✅ 已接线 httpx |
| Agent 模型绑定为自定义 HTTP | ✅ 确认行为等价（生态无对应插件） |
| 幂等记录仓储未建 | ✅ 已实现（`mutation_idempotency_records` + 完整编排） |

**这张表曾经是"切换前必须处理"的清单，而四项都已处理完却仍标着未处理**——一份过时的
阻塞清单比没有更危险：它会让执行者去做已经不必做的事，或据此怀疑整个切换是否成熟。

### 预检**不覆盖**的两件事

- **阶段 1 的备份**——它必须在真实数据库上执行；
- **阶段 4 的真实设备联调**——它是验收标准，脚本无法替代。

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
cd server-py
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

## 阶段 2：启动 Python 服务

```bash
cd server-py
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
cd server-py
UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \
  uv run --python 3.12 python scripts/verify_running_service.py \
  --base-url http://127.0.0.1:8080 --device-token "$DEVICE_TOKEN"
```

它逐项验证并给出可判定的结论：

| 检查 | 判定 |
| --- | --- |
| `/v1/health` | 200，且 `status` 为 `healthy` **或 `degraded`** |
| `/v1/system/info` | `schema_version` 为 15、`protocol_version` 为 2 |
| 受保护端点未认证 | **401 且错误体是扁平信封**（`code` / `message` / `retryable` / `request_id`） |
| 带令牌读取账本 | 200（**"能服务"的唯一直接证据**） |
| `?scope=all` | 200 |
| 非法 `scope` | **400**（而不是静默返回全部账户） |

**`degraded` 算通过**——它表示服务能工作、只是部分能力受损；把 degraded 当失败会让一个
可用的服务被误判为不可用。脚本会额外打印 degraded 的原因。

**过程"起来了"与"能服务"是两件事**：uvicorn 打印 "Application startup complete" 之后，
服务仍可能因为数据库不可读或路由装配不全而无法真正工作。

## 阶段 3：探针切换

```bash
curl -sS http://127.0.0.1:PORT/v1/health
curl -sS -H "Authorization: Bearer $DEVICE_TOKEN" http://127.0.0.1:PORT/v1/ledger/summary
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

```bash
systemctl stop lyra-python
```

**然后跑脚本，不要手工 `cp`：**

```bash
cd server-py
UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \
  uv run --python 3.12 python scripts/rollback_cutover.py \
  --database "$LYRA_DATABASE_PATH" --backup /var/backups/lyra/ledger.duckdb.bak-... \
  --i-have-stopped-python
```

不确定时先加 `--dry-run`：它只验证备份并打印将要执行的动作，**不改动任何文件**。

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

## 阶段 7：退役 TS（**仅在阶段 4、5 全部通过后**）

```bash
# 1) 归档而不是直接删除：保留可回退的原文
git tag lyra-ts-final
git archive --format=tar.gz -o /tmp/lyra-ts-final.tgz HEAD:server

# 2) 确认归档可用（能解出文件、能跑一次 check）后再删除
# 3) 删除
git rm -r server/
```

**删除前必须确认**：

- 阶段 4 全部通过并有记录；
- 阶段 5 观察期内无异常；
- TS 的最终状态已打 tag 且归档可解压；
- **`application-gin.yml` 不在归档内**。

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
| 7 退役 TS | | | |

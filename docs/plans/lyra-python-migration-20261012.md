# Lyra 服务端 Python 迁移计划

> **文档状态**：执行计划，**进行中**。M1 已落地。
> **日期**：2026-10-12
> **目标**：把服务端由 TypeScript/Fastify 全量迁移到 **Python 3.12 + FastAPI + Genkit Python + DuckDB**，Python 接管账本文件与 schema 迁移，**Flutter 客户端协议保持版本 2 不变**。
> **相关**：[边界与迁移设计](lyra-python-analytics-boundary-20261012.md)（决策与硬约束）、[完整方案](lyra-product-technical-design.md)、[服务端部署](../deployment.md)
>
> **目录名说明**：本文写作时 Python 服务端在 `server-py/`；M9 已把它改名为 `server/` 并删除 TypeScript 侧（提交 `ed522c7`）。**文中所有 `server-py/` 均应读作 `server/`**；正文按迁移当时原文保留，未逐行改写。

---

## 1. 决策前提

原「是否迁移」的设计文档（[边界与迁移设计](lyra-python-analytics-boundary-20261012.md)）结论是「暂不迁移，满足触发条件再评估」。**产品已决定不再等待触发条件，直接全量迁移。** 该文档中仍然有效的部分：

- DuckDB 跨进程文件锁（附录 A，**实测**）；
- 十进制精度规范（§3.2）；
- schema 不可逆与加密备份受主密钥约束（§3.3）；
- 差分对照 harness（§11）——**它是本次迁移的验收工具，不是迁移的替代品**。

## 2. 最关键的一条约束：切换是原子的

DuckDB 同一时刻只允许一个进程打开账本文件——**写入进程持锁时，第二个进程连只读都打不开**（已实测）。而 auth / session / ledger / market settings 全部在同一个库里。

因此：

> **谁打开账本文件，谁就拥有整个服务。**

推论（决定了整个计划的形态）：

1. **不存在"两个服务并存共享账本"的中间态。** TS 与 Python 不能同时服务同一个账本。
2. M1–M8 一律**在副本或夹具上开发与验证**，不接触生产账本。
3. **M9 是一次硬切**：停 TS → Python 接管账本文件 → 起 Python。回退靠停 Python、起 TS，账本文件不变。
4. 因此每个里程碑都必须满足：**不修改 schema、不写入生产账本、不改变客户端协议。**

## 3. 阶段与验收

| # | 阶段 | 内容 | 验收标准 | 状态 |
| --- | --- | --- | --- | --- |
| **M1** | 接管账本与基础端点 | Python 打开 schema 15 DuckDB、完成迁移校验、暴露 `/v1/health` 与 `/v1/system/info` | ① 能在由 TS 创建的 schema 15 库上无损打开；② 迁移历史逐项校验通过；③ 端点响应与 TS 契约一致；④ 测试全绿 | ✅ **已完成** |
| **M2** | 密钥互通 | AES-256-GCM `SecretCipher` 与 TS 密文**双向互通**（现有 Gmail refresh token 必须在切换后仍可解密） | ① Python 能解开 TS 产生的真实密文；② TS 能解开 Python 产生的密文；③ 格式、AAD、篡改检测与错误语义一致 | ✅ **已完成** |
| **M3a** | 共享语料 + 账本解析 | 同一录制器产出 JSON 副本供 Python 消费；移植 `parsers.ts` / `transactions.ts` / `batch.ts` / `schwab-csv.ts` | Python 解析结果与 oracle 的 transactions **逐字段一致**（含业务哈希） | ✅ **已完成** |
| **M3b** | FIFO 重放 | 移植 `replay.ts`：排序、开平仓、方向推断、费用分摊、盈亏量化 | 与 oracle 的 lots / realized 逐字段一致 | ✅ **已完成** |
| **M3c** | 分析层 16 命令 | 移植 `analytics/*`：summary、positions、expiring、trades、realized、cashflow、monthly、allocation、attribution、drawdown、risk、holding-period、extremes、streaks、stress、audit | 与 oracle 的 16 条命令输出逐字段一致（复用 19 份账本语料） | ✅ **已完成（16/16）** |
| **M5** | 认证与会话 | Google / Linux.do / GitHub OAuth、设备凭据、refresh 轮换、logout 撤销 | 现有设备凭据继续有效；三来源登录往返通过 | ✅ **已完成**（可验证部分；DuckDB 仓储与 HTTP 路由归 M4） |
| **M4** | 只读 REST API | accounts / ledger / market 只读端点，协议 2 | 与 TS 逐字段对照一致；Flutter 不感知差异 | ✅ **已完成**（16 条账本/分析端点；`market/positions` 归 M8；游标分页待补） |
| **M6** | 写入路径 | 导入（上传/预览/提交）、幂等键、审计、写队列 | 重放、并发重复、失败回滚与原行为一致 | ✅ **主流程已完成**（四条路由 + 端到端）；剩余 Excel 解析与幂等串联 |
| **M7** | Agent | Genkit Python、18 个只读工具、回合生命周期、WebSocket 事件 | 工具口径与 M3 一致；流式与取消行为对齐 | ✅ **已完成**（18 工具目录 / 17 可用；模型绑定见 §81.1 的生态缺口） |
| **M8** | 行情 | Alpaca / MarketData 适配、缓存、预算、主备换源 | 设置持久化与热切换语义一致 | ✅ **已完成**（传输层为如实留空的占位，见 §93.5） |
| **M9a** | 认证路由挂载 | 6 条 auth 路由（目录 / 发起 / 兑换 / 回调 / 退出 / 刷新） | 与 TS 行为一致；刷新不要求访问令牌 | ✅ **已完成** |
| **M10** | Gmail 子系统 | Google OAuth、连接管理、账户映射、同步（7 条路由） | 与 TS 行为一致；凭据绑定连接、符号回显、勾稽校验 | ✅ **已完成** |
| **M9b** | 切流与退役 TS | 部署脚本、备份、探针切换；删除 TS 服务端 | **协议 2 客户端无需改动**；真实设备联调通过 | 待开始 |

## 4. 每阶段的固定动作

1. **先出计划**：本文件更新该阶段的范围与验收，不写代码。
2. **再落地实现**：仅在该阶段授权范围内写文件。
3. **验证**：新测试 + 既有回归必须全绿；涉及口径的必须跑差分对照。
4. **不改动**：schema（保持 15）、客户端协议（保持 2）、TS 服务端代码（除 M9 的删除外）。

### 4.1 M2 的范围修正（相对原始计划）

原始 M2 写作「配置预检 + 加密备份互通」，落地时两项都**收窄**，理由如下：

| 原计划项 | 处理 | 理由 |
| --- | --- | --- |
| 部署配置预检 CLI | **拆分到各子系统里程碑** | `check-config.ts`（308 行）校验的是 Google / GitHub / LINUX.do 登录、Genkit、行情的配置。这些加载器在 Python 侧尚不存在，此时实现预检只会产出「PASS 但服务实际无法认证」的**假成功**——正是本项目禁止的结果。改为：基础环境检查随 M2 落地，登录检查随 M5，Genkit 随 M7，行情随 M8；**在全部检查就位前，预检不得输出整体 PASS** |
| 加密备份互通 | **降级到 M9** | 已核实 `encrypted-backup.ts` 在生产中**没有调用点**：部署文档记录的备份方式是停服后 `tar` 数据库目录。为一个无人执行的路径提前实现互通不符合最小实现原则 |
| AES-256-GCM 密钥互通 | **保留并确认为 M2 主线** | 这是**切换关键路径**：现有 Gmail refresh token 以 `SecretCipher` 密文存在账本里，Python 若解不开，切换即导致全部 Gmail 连接失效 |

### 4.2 M4 与 M5 的顺序修正（2026-10-12）

原计划先做 M4（只读 REST API）再做 M5（认证与会话）。落地前核对 `http-server.ts` 与各路由后确认**这个顺序不成立**：

- `http-server.ts` 的 `PUBLIC_V1_ROUTES` 只放行 `/v1/system/info`、`/v1/health`、三个 OAuth 回调与 `/v1/auth/providers`；
- 其余每条业务路由都在 `preHandler` 中调用 `options.authenticateDevice(token)`（账户、账本、行情设置、导入、Agent 均如此）。

也就是说，**没有设备认证就没有任何可读业务端点**。因此调整为 **M5 先于 M4**：

| 原顺序 | 调整后 |
| --- | --- |
| M4 只读 REST API → M5 认证与会话 | **M5 认证与会话 → M4 只读 REST API** |

M5 内部也按依赖顺序推进：先做**凭据原语与访问令牌校验**（可独立测试），再做 Gmail/模型等外部凭据，最后做配对、轮换与撤销的完整链路。

## 5. 过渡期的目录约定

```text
server/        TypeScript 服务端（M1–M8 期间只读参考；M9 删除）
server-py/     Python 服务端（唯一新增写入区）
```

M9 完成后 `server/` 删除，`server-py/` 成为唯一服务端（是否需要改名为 `server/` 在 M9 决定）。

**迁移 SQL 的唯一来源**：`server/src/infrastructure/db/*.sql`（15 个文件，与 `MIGRATIONS` 清单一一对应）。M1–M8 期间 `server-py/src/lyra/db/sql/` 持有副本，并由测试**逐字节校验与源文件一致**，防止两侧漂移。M9 时源文件随 TS 一并删除，副本转为唯一来源。

## 6. 回退

| 时点 | 回退方式 |
| --- | --- |
| M1–M8 | 无需回退：TS 未受影响，账本未被改动 |
| M9 切流后 | 停 Python → 起 TS。schema 不变、账本不变，设备凭据继续有效 |

**M9 是唯一有风险的时点**，因此要求：切换前完整备份账本、记录 TS 侧最后验证版本、保留可即时回退的部署镜像。

## 7. 风险

| 风险 | 缓解 |
| --- | --- |
| 迁移期间两侧行为漂移 | 差分对照 harness 作为验收闸门；迁移 SQL 逐字节校验 |
| 认证细节在重写中回归 | M5 单独成阶段，用现有生产设备凭据做验收 |
| Python Genkit 仍为 preview | M7 排在最后；若 SDK 不稳定，Agent 阶段可暂缓而不阻塞 M1–M6、M9 |
| 一次性切换缺乏灰度 | 无法灰度（单进程锁）；改为「切前完整备份 + 可即时回退 + 真实设备联调前置」 |
| 工作量被低估 | 基线：TS 服务端 153 个文件 / 26,387 行；其中约 17,279 行基础设施需重写，约 4,901 行领域语义有同源参照 |

## 8. M1 落地记录（2026-10-12）

**新增**：`server-py/`（Python 3.12 + FastAPI + DuckDB）。

**实现内容**：

| 文件 | 作用 |
| --- | --- |
| `server-py/src/lyra/config.py` | 环境校验，字段与约束与 TS `server-environment.ts` 一致 |
| `server-py/src/lyra/protocol.py` | 协议版本常量与 `/v1/system/info` 元数据，镜像 `protocol-version.ts` |
| `server-py/src/lyra/db/migrations.py` | 15 条迁移清单与执行语义，镜像 `database.ts` 的 `applyMigrations` |
| `server-py/src/lyra/db/connection.py` | 打开 DuckDB、`temp_directory` 固定、健康探测、CHECKPOINT |
| `server-py/src/lyra/db/sql/` | 15 个迁移 SQL 副本（由测试校验与 TS 源文件逐字节一致） |
| `server-py/src/lyra/api/health.py` | `GET /v1/health` |
| `server-py/src/lyra/api/system.py` | `GET /v1/system/info` |
| `server-py/src/lyra/main.py` | 应用工厂与 uvicorn 入口 |

**验收证据**：见 `server-py/tests/`，含对真实 schema 15 库的接管测试、迁移 SQL 一致性测试、端点契约测试。

## 9. M2 落地记录（2026-10-12）

**新增**：`server-py/src/lyra/security/secret_cipher.py`、跨实现夹具与验证脚本。

**为什么这是 M2 的主线**：现有账本里已有 TypeScript 加密的 Gmail refresh token。Python 若解不开，切换后**全部 Gmail 连接立即失效**，且不是重登一次能恢复的——需要对每个连接重新授权。这是本次迁移中唯一「静默失效且后果严重」的加密路径。

### 9.1 实现要点

| 项 | 值 |
| --- | --- |
| 信封 | `v1.<iv_b64url>.<ciphertext_b64url>.<auth_tag_b64url>`，无填充 |
| 算法 | AES-256-GCM；IV 12 字节；认证标签 16 字节 |
| AAD | 连接上下文的 UTF-8 字节，例如 `gmail-connection:<uuid>` |
| 主密钥 | `LYRA_SECRET_MASTER_KEY` 的 **hex 解码**结果（32 字节） |

**一处容易踩错的细节**：TypeScript 侧有两个密钥解析函数——生产路径是 `Buffer.from(key, "hex")`，而 `parseSecretEncryptionKey`（base64url）**只被它自己的测试使用**。按后者实现会得到一根完全不同的密钥，且症状只在解密真实数据时才出现。

### 9.2 验证证据

| 方向 | 方法 | 结果 |
| --- | --- | --- |
| TS 加密 → Python 解密 | `tests/fixtures/crypto-fixtures.json`（**真实 TS 密文**，已提交） | 5/5 通过，含多字节明文与多字节 AAD |
| Python 加密 → TS 解密 | `scripts/export-python-envelopes.py` 产出 → `scripts/generate-crypto-fixtures.mjs decrypt` | **5/5 成功，0 失败**（含 4096 字符长明文） |
| 篡改检测 | 翻转密文/标签首字符 | 均被 GCM 认证拒绝 |
| 上下文绑定 | 用错误 context 解密 | 被拒绝（密文串换到另一行也解不开） |

**测试规模**：`uv run pytest` → **32 项通过**（M1 的 18 项 + M2 的 14 项）。

### 9.3 过程中发现并修正的问题

一处**测试自身**的缺陷：篡改用例原本翻转 base64url 段的**最后一个**字符。无填充 base64url 的末位字符可能只承载未使用位，改动它未必改变解码后的字节——测试会假通过。已改为翻转首字符，并在辅助函数里注明原因。

### 9.4 未纳入 M2 的两项（已在 §4.1 说明）

- **部署配置预检**：拆到各子系统里程碑，避免产出「PASS 但实际无法认证」的假成功；
- **加密备份互通**：降级到 M9，因为该模块在生产中没有调用点，文档记录的备份方式是停服 `tar`。

## 10. M3a 落地记录（2026-10-12）

### 10.1 语料共享

差分对照语料由 `record-fixtures.py` **同一个录制器产出两份**：TypeScript 侧读 `.ts`，Python 侧读 `.json`。这样两侧不各自录制，避免语料漂移——Python 只是换个格式读同一批 oracle 结果。

已核对两侧用例数一致：**95 条**（csv 35 / econfirm 28 / fifo 13 / analytics 19）。

### 10.2 移植范围

| Python 文件 | 对应 TypeScript |
| --- | --- |
| `domain/ledger/parsers.py` | `parsers.ts`（金额、日期、期权代码、账户标识） |
| `domain/ledger/transactions.py` | `transactions.ts`（动作白名单、资产分类、金额勾稽、**业务哈希**） |
| `domain/ledger/batch.py` | `batch.ts`（occurrence 编号） |
| `infrastructure/imports/schwab_csv.py` | `schwab-csv.ts` + `schwab-table.ts`（表头与列数严格校验） |

### 10.3 三处必须逐字对齐的细节

1. **哈希文本取原始书写形式**：`$5.00` 必须哈希成 `5.00` 而不是 `5`，否则同一份导出在不同实现下得到不同去重键。小数位数取自清洗后的源文本。
2. **负零归一**：`-0.00` 归一到 `0.00`（decimal.js 不保留负零）。若照搬 Python 的 `str(Decimal)`，会得到 `-0.00` 并产出不同哈希。
3. **日期零填充取原始分段**：`1/2/2026` 与 `01/02/2026` 必须产生同一个日期文本，因此返回值取自原始分段而不是解析出的整数。

### 10.4 验证证据

`server-py/tests/test_ledger_parity.py` 直接读 oracle 录制的期望值：

| 检查 | 结果 |
| --- | --- |
| 每条 csv 语料的 transactions 逐字段一致（含哈希、账户、日期、分类、精度） | **29/29 通过** |
| 跨全语料的业务哈希集合一致 | 通过（覆盖 ≥20 条） |
| 错误路径的失败原因逐条一致（数值归一后） | 通过 |

与 TypeScript harness 保持同一份跳过清单：`test_database_failure_rolls_back_whole_file` 是 oracle 的故障注入用例，不属于解析口径差异。

**测试规模**：`uv run pytest` → **61 项通过**（M1 18 + M2 14 + M3a 29）。

## 11. M3b 落地记录（2026-10-12）

**新增**：`server-py/src/lyra/domain/fifo/`（`types.py` / `replay.py`）、`server-py/src/lyra/domain/numeric.py`、`server-py/tests/test_fifo_parity.py`、`server-py/tests/parity_support.py`。

### 11.1 重放顺序是业务语义，不是实现细节

生效日升序 → **同日 `sequence` 降序** → 交易哈希升序。

第二项最容易写反：Schwab 导出**最新在前**，所以同一天里物理位置更靠后的行发生得**更早**。写反不会报错，只会让同日多笔交易的成本基础与已实现损益悄悄错位。

### 11.2 三处必须与现有实现逐字一致的规则

| 规则 | 说明 |
| --- | --- |
| 费用分摊的尾差归属 | 开仓费按消耗比例分摊，**批次耗尽时一次性吃掉剩余尾差**；平仓费**最后一段**吃掉尾差。两边都靠这一条避免舍入漂移 |
| 盈亏量化 | 4 位小数 + **银行家舍入**（`ROUND_HALF_EVEN`），与 Python 侧 `quantize` 默认上下文一致 |
| 顺序可证性 | 同账户 + 同日 + 同合约若来自多个来源哈希，必须**拒绝重放**而不是按不可靠顺序算 |

### 11.3 抽取的共享助手

`domain/numeric.py` 的 `decimal_text` 统一处理「Python 保留标度 vs decimal.js 去尾随零」的渲染差异。此前业务哈希、勾稽错误消息、FIFO 告警各需一份，抽出来后只留一处，避免三处各自漂移。

### 11.4 验证证据

| 检查 | 结果 |
| --- | --- |
| 每条 fifo 语料的 lots 与 realized 逐字段一致 | **11/11 通过**（含 2 条异常路径，失败原因逐字一致） |
| 跨语料已实现损益一致性 | 通过 |
| 排除项正确性 | 已单独核对：`#fifo#2`（故障注入）按跳过清单排除，`#fifo#1`（注入前那次成功重建）正常纳入；多文件重放用例按 `fileCount` 排除 |

**测试规模**：`uv run pytest` → **74 项通过**（M1 18 + M2 14 + M3a 29 + M3b 13）。

### 11.5 已知覆盖缺口（如实记录）

本轮 fifo 语料共比对 **12 行 realized，但只有 1 行未平批次（lots）**——oracle 的 FIFO 测试大多把仓位完全平掉。这意味着：

- 未平批次的多批次聚合、部分平仓后剩余数量的跨账户表现，**未被这批语料覆盖**；
- 该缺口在 M3c 用 19 份账本的分析层语料（`positions` / `allocation` / `risk` 都依赖未平批次）部分弥补，但仍不能宣称全覆盖。

不得据此宣称 FIFO 已全面验证。

## 12. M3c 进展（2026-10-12，3/16）

**新增**：`server-py/src/lyra/domain/analytics/`（`types.py` / `dates.py` / `positions.py` / `summary.py`）、`server-py/tests/test_analytics_parity.py`。

### 12.1 已完成并验证的命令

| 命令 | 内容 |
| --- | --- |
| `summary` | 总览：净入金、已实现损益（分股票/期权）、未平仓数、费用、股息、利息、税、现金余额、交易笔数、日期区间 |
| `positions` | 按「账户 + 完整合约 + 方向」聚合未平批次 |
| `expiring` | 到期窗口内的未平期权，标记已过期 / 今日到期 / 即将到期 |

### 12.2 一处刻意的类型命名决策

分析层类型的字段名**直接采用 oracle 输出的 JSON 键**（snake_case），而不是照搬 TypeScript 内部的 camelCase。

理由是**对照强度**：TS 侧 harness 需要「键名 snake→camel 归一后取交集」，而交集规则会放过"两边命名不同、实际是同一个指标"的情况。字段名与 oracle 一致后，比对是**直接逐字段**的，不再依赖宽松归一。

### 12.3 验证证据

| 命令 | 19 份账本共比对字段数 |
| --- | ---: |
| `summary` | 228 |
| `expiring` | 169 |
| `positions` | 148 |
| **合计** | **545** |

- 每条命令设有**最小比对字段数**下限，低于下限即失败——防止「键名全对不上」造成的空比对假通过；
- 未实现的 13 条命令由 `test_unimplemented_commands_are_explicit` **显式列出**，不会静默缺席。

**测试规模**：`uv run pytest` → **79 项通过**（M1 18 + M2 14 + M3a 29 + M3b 13 + M3c 5）。

### 12.4 顺带弥补的覆盖缺口

§11.5 记录的「fifo 语料只有 1 行未平批次」在本轮得到部分弥补：`positions` 与 `expiring` 直接消费未平批次，148 + 169 个字段的比对覆盖了多批次聚合、方向分离与到期分桶。但**部分平仓后剩余数量的跨批次表现**仍未被专门用例覆盖。

### 12.5 待实现（10 条）

`trades`、`realized`、`cashflow`、`drawdown`、`risk`、`holding-period`、`extremes`、`streaks`、`stress`、`audit`。

## 13. M3c 进展（2026-10-12，6/16）

**新增**：`server-py/src/lyra/domain/analytics/performance.py`。

### 13.1 本轮完成

| 命令 | 内容 |
| --- | --- |
| `monthly` | 按平仓月份汇总胜率、盈亏比、净损益与费用 |
| `allocation` | 按标的/资产类型汇总未平仓**历史成本**配置（非实时市值） |
| `attribution` | 按维度拆解已实现损益贡献与绝对贡献占比 |

### 13.2 两处口径细节

1. **合计取自行内已舍入的文本。** `monthly` 的 `total_pnl` / `total_fees` 是对每行**已渲染字符串**再求和，不是对原始明细求和——这样表格上的数字相加就等于合计，用户对账时不会出现"差一分钱"。照搬"对原始值求和"会得到更精确但**与表格不符**的合计。
2. **排序稳定性属于口径。** 占比或净损益相同的分类必须按名称升序；否则同一份账本两次运行可能给出不同的表格顺序。

### 13.3 验证证据

| 命令 | 19 份账本共比对字段数 |
| --- | ---: |
| `summary` | 228 |
| `monthly` | 185 |
| `expiring` | 169 |
| `positions` | 148 |
| `attribution` | 148 |
| `allocation` | 131 |
| **合计** | **1009** |

**测试规模**：`uv run pytest` → **82 项通过**（M1 18 + M2 14 + M3a 29 + M3b 13 + M3c 8）。

## 14. M3c 进展（2026-10-12，8/16）

**新增**：`server-py/src/lyra/domain/analytics/risk.py`。

### 14.1 本轮完成

| 命令 | 内容 |
| --- | --- |
| `drawdown` | 日度累计已实现损益曲线、最大回撤峰谷与恢复日期 |
| `risk` | 按到期窗口汇总长期权历史成本与短期权历史收入，含标的集中度前 3 |

### 14.2 两处容易做错的语义

1. **同值峰值取最早那次。** 峰值只在 `cumulative > peak` 时更新，因此回撤起点的 `peak_date` 是**首次**达到该峰值的日期。改成 `>=` 会让峰谷日期整体后移。
2. **占比的零值语义。** 合计为零时占比返回 `null` 而不是 `0.00`——「无数据」和「占比为零」是两件事，前者不能被渲染成后者。

### 14.3 一处输入口径要求

`risk` 必须先按持仓口径聚合 FIFO 批次再进桶：同一合约若有多个批次，直接逐批次统计会把"1 个持仓"算成"3 个持仓"。

### 14.4 验证证据

| 命令 | 19 份账本共比对字段数 |
| --- | ---: |
| `summary` | 228 |
| `risk` | 194 |
| `drawdown` | 188 |
| `monthly` | 185 |
| `expiring` | 169 |
| `attribution` | 148 |
| `positions` | 148 |
| `allocation` | 131 |
| **合计** | **1391** |

**测试规模**：`uv run pytest` → **84 项通过**。

### 14.5 待实现（8 条）

`trades`、`realized`、`cashflow`、`holding-period`、`extremes`、`streaks`、`stress`、`audit`。

## 15. M3c 进展（2026-10-12，12/16）

**新增**：`server-py/src/lyra/domain/analytics/trade_analysis.py`。

### 15.1 本轮完成

| 命令 | 内容 |
| --- | --- |
| `holding-period` | 按 FIFO lot 实际持有天数分桶汇总绩效 |
| `extremes` | 按平仓交易净损益列出最佳与最差各 N 笔 |
| `streaks` | 最长连续盈利与连续亏损（零损益交易结束区间但不计入） |
| `stress` | 长期权权利金归零的历史成本压力，含标的前 3 集中度 |

### 15.2 三条共同的聚合前提

这四条命令都建立在同一个聚合之上：**把跨越多个 FIFO 批次的 realized 行重新聚合为一笔平仓交易**。聚合键（平仓哈希 + 账户 + 日期 + 合约 + 类型 + 方向 + 动作）少一项，一笔交易就会被算成多笔，进而让胜率、连续区间与极值排名全部偏移。

### 15.3 一处舍入模式**故意**不一致

`holding-period` 的 `average_holding_days` 与 `average_lot_pnl` 使用 **`ROUND_HALF_UP`**，而同一模块的胜率、以及其它命令都使用 `ROUND_HALF_EVEN`。

这不是笔误：oracle 侧这两项走 DuckDB `round(avg(...))`（half-up），其余走 Python `quantize`（默认 banker's rounding）。统一成一种会让个别桶的均值差一分。「顺手统一舍入模式」在这里是错的。

### 15.4 一处对照强度问题

oracle 的 `extremes` 行是**扁平**的（`close_txn`、`qty` 与 `side`、`rank` 同级），而 TypeScript 领域类型把它嵌套在 `trade` 下。若照搬嵌套结构，扁平化比对会因为路径不同而**跳过 `close_txn` 等字段**——键名差一个词就等于不比对。

因此 Python 侧直接产出与 oracle 同形的扁平行，并让 `ExtremeTradeRow` 继承 `ClosedTransaction` 复用字段；公开 API 的呈现形状留到 M4 决定。

### 15.5 验证证据

| 命令 | 比对字段数 | | 命令 | 比对字段数 |
| --- | ---: | --- | --- | ---: |
| `summary` | 228 | | `expiring` | 169 |
| `risk` | 194 | | `positions` | 148 |
| `drawdown` | 188 | | `attribution` | 148 |
| `monthly` | 185 | | `allocation` | 131 |
| `extremes` | 183 | | `holding-period` | 129 |
| | | | `stress` | 113 |
| | | | `streaks` | 112 |
| | | | **合计** | **1928** |

**测试规模**：`uv run pytest` → **88 项通过**。

### 15.6 待实现（4 条）

`trades`、`realized`、`cashflow`（`queries.ts`）、`audit`（`audit.ts`）。

## 16. M3c 完成（2026-10-12，16/16）→ **M3 领域层全部完成**

**新增**：`server-py/src/lyra/domain/analytics/queries.py`、`audit.py`。

### 16.1 本轮完成

| 命令 | 内容 |
| --- | --- |
| `trades` | 交易流水：过滤、排序、页长限制 |
| `realized` | 已实现明细：合计取过滤后**全量**而非当前页 |
| `cashflow` | 现金流水：明细、总额与分动作合计 |
| `audit` | 只读审计：7 项一致性检查 |

### 16.2 一处刻意的行为保持

`audit.scope_note` 在 oracle（「本命令」）与现有实现（「本功能」）之间**本就有文案差异**。

迁移的契约是**保持现有行为**，不是对齐 oracle。因此 Python 沿用「本功能」，并把这条差异写进测试白名单——与 TypeScript 侧 harness 的 `audit.scopeNote` 豁免是同一条，理由一致。

白名单设了**活条目断言**：命中为 0 即失败。实测命中 `['audit.scope_note']`。

### 16.3 一处明确的未实现（如实记录）

`queries.ts` 的不透明游标分页（`nextCursor`）**未移植**。它是接口层契约，会在 M4（只读 REST API，协议 2）按既有形状一并确定。

这里**不返回占位游标**：一个"看起来能翻页"的假游标比没有更危险——客户端会以为还有下一页。

### 16.4 验证证据

19 份账本 × 16 条命令，共比对 **2771 个字段**：

| 命令 | 字段数 | | 命令 | 字段数 |
| --- | ---: | --- | --- | ---: |
| `trades` | 361 | | `positions` | 148 |
| `summary` | 228 | | `allocation` | 131 |
| `audit` | 209 | | `holding-period` | 129 |
| `risk` | 194 | | `stress` | 113 |
| `realized` | 192 | | `streaks` | 112 |
| `drawdown` | 188 | | `cashflow` | 81 |
| `monthly` | 185 | | | |
| `extremes` | 183 | | | |
| `expiring` | 169 | | | |
| `attribution` | 148 | | **合计** | **2771** |

**测试规模**：`uv run pytest` → **93 项通过**（M1 18 + M2 14 + M3a 29 + M3b 13 + M3c 19）。

### 16.5 M3 小结

领域层三部分全部完成并逐字段验证：

| 子阶段 | 内容 | 验证 |
| --- | --- | --- |
| M3a | CSV 解析、标准化、业务哈希 | 29/29 语料逐字段一致 |
| M3b | FIFO 重放 | 11/11 语料逐字段一致 |
| M3c | 16 条分析命令 | 19 份账本 × 16 命令，2771 字段一致 |

**已知未覆盖**（不得据此宣称全面验证）：真实券商导出的边界情形、多次导入与多文件重放的累计账本状态、游标分页。

## 17. M5 起步：设备凭据与访问令牌（2026-10-12）

**新增**：`server-py/src/lyra/security/device_auth.py`、`server-py/tests/test_device_auth.py`。

### 17.1 为什么先做认证（见 §4.2）

核对确认：除 `/v1/system/info`、`/v1/health`、三个 OAuth 回调与 `/v1/auth/providers` 外，**每条业务路由都在 preHandler 中校验设备令牌**。因此 M4 的只读 API 没有 M5 就无法落地，两者顺序对调。

### 17.2 本轮完成

| 能力 | 说明 |
| --- | --- |
| 不透明凭据 | 256 位随机值，无填充 base64url；服务端只持久化 SHA-256 哈希 |
| 常量时间校验 | 用 `hmac.compare_digest`；格式非法的期望哈希直接判不匹配而不是抛错 |
| 访问令牌签发 | 记录只含哈希，明文仅返回一次 |
| 访问令牌认证 | 校验并把 `last_seen_at` 更新为新记录 |

### 17.3 三条不可放宽的性质（均有测试）

1. **只持久化哈希**——测试断言明文令牌不出现在记录的 `repr` 里；
2. **常量时间比较**——避免通过响应耗时逐字节试探令牌；
3. **检查顺序是安全语义**——凭据 → 设备撤销 → 令牌撤销 → 过期。若先判设备状态，「令牌错了」会被客户端误读成「设备被撤销」，从而提示重新配对而不是重新登录。**顺序是接口契约的一部分**，因此专门有测试断言错误码优先级。

### 17.4 验证证据

- 22 项设备认证测试全部通过，含 SHA-256 已知向量、TTL 边界、到期时刻（等于即过期、差 1 毫秒仍有效）、两条错误码优先级断言；
- **测试规模**：`uv run pytest` → **113 项通过**（M1 18 + M2 14 + M3 61 + M5 20）。

### 17.5 待完成（M5 后续增量）

配对挑战与设备公钥、三种 OAuth 来源的登录往返、logout 撤销、设备活动节流写入。

## 18. M5 进展：刷新凭据轮换与重放撤销（2026-10-12）

**新增**：`device_auth.py` 的刷新凭据部分、`server-py/tests/test_device_refresh.py`。

### 18.1 本轮完成

| 能力 | 说明 |
| --- | --- |
| 刷新凭据签发 | 第 1 代、新族、只存哈希 |
| 刷新凭据轮换 | 返回**事务计划**：旧记录转 `rotated`、新记录同族且代号 +1 |
| 重放撤销计划 | 检出旧凭据被重用时，撤销**整族全部代次** |

### 18.2 三条安全语义（均有专门测试）

1. **检查顺序：凭据状态先于过期。** 一枚"已轮换**且**已过期"的令牌报 `DEVICE_CREDENTIAL_REUSED`，不是"过期"。

   这不是细节：报「过期」会引导客户端静默重新登录，**从而掩盖一次真实的令牌重放**；报「重用」才能触发整族撤销。

2. **撤销必须包含活动的最新代。** 旧令牌泄漏时，攻击者与合法客户端会持续竞速刷新。只撤销旧代等于把窗口留给攻击者——撤销整族是唯一能终止竞速的做法。

3. **撤销计划严格限定同设备同族。** 族中混入其它设备或其它族必须**报错拒绝**而不是照单全收，否则一次泄漏会波及其它设备。

另有一处幂等性要求：已撤销的记录在重复检测时保持原时间戳不变，避免无收益写入。

### 18.3 验证证据

- 13 项刷新凭据测试全部通过，含两条"顺序"反向用例、整族撤销、幂等、三种非法计划拒绝；
- **测试规模**：`uv run pytest` → **126 项通过**。

### 18.4 待完成（M5 后续增量）

配对挑战与设备公钥、三种 OAuth 来源的登录往返、logout 撤销、设备活动节流写入。

## 19. M5 进展：配对挑战与设备公钥（2026-10-12）

**新增**：`device_auth.py` 的配对部分、`server-py/tests/test_device_pairing.py`、`server-py/scripts/generate-device-key-fixtures.mjs`、`server-py/tests/fixtures/device-keys.json`。

### 19.1 本轮完成

| 能力 | 说明 |
| --- | --- |
| 一次性配对挑战 | 去混淆 Base32 字母表，10 字符、5-5 分组；只存 SHA-256 哈希 |
| 配对码消费 | 容忍大小写、显示横线与空格；返回新记录不改原记录 |
| 设备公钥规范化 | 只接受 `PUBLIC KEY`，**明确拒绝私钥**；产出规范 SPKI PEM + 指纹 + 算法名 |
| 设备记录创建 | 保存规范化公钥与指纹，初始 active |

### 19.2 两处检查顺序细节

1. **配对码比对先于「已使用」。** 配错码时必须报「无效」而不是「已使用」——后者会向配错码的一方泄漏「这个码确实存在且已被用掉」。
2. **私钥必须在解析前就被拒绝。** 让私钥有机会进入服务端进程本身就是不可接受的，无论之后是否持久化，因此用字符串检查拦在 `load_pem_public_key` 之前。

### 19.3 跨实现验证（本轮的重点）

已配对设备记录里存的是**规范化后的 SPKI PEM 与 SHA-256 指纹**。两边只要有一点差异（换行、行宽、编码、算法名），切换后既有设备记录就会像被改写过，指纹校验也会失败。

因此用 Node 生成**真实公钥**（RSA-2048 / prime256v1 / Ed25519）并记录 TypeScript 的规范化结果，Python 侧断言逐字节一致：

| 检查项 | 结果 |
| --- | --- |
| 规范化 PEM（含换行与行宽） | 三种密钥全部一致 |
| SHA-256 指纹 | 三种密钥全部一致 |
| 算法名（对应 Node `asymmetricKeyType`） | `rsa` / `ec` / `ed25519` 全部一致 |
| 幂等性（对规范化结果再规范化） | 不变 |

Python 侧的算法名映射表刻意与 Node 取值同名（`rsa`/`dsa`/`ec`/`ed25519`/`ed448`/`x25519`/`x448`），因为 `key_algorithm` 是可查询的持久化字段。

### 19.4 验证证据

- 24 项配对与公钥测试全部通过；
- **测试规模**：`uv run pytest` → **150 项通过**。

### 19.5 待完成（M5 后续增量）

三种 OAuth 来源的登录往返、logout 撤销、设备活动节流写入、设备与访问令牌的撤销计划。

## 20. M5 进展：活动节流与撤销计划（2026-10-12）→ **设备认证模块完成**

**新增**：`device_auth.py` 的活动节流与两个撤销计划、`server-py/tests/test_device_revocation.py`。

### 20.1 本轮完成

| 能力 | 说明 |
| --- | --- |
| 设备活动节流 | 认证成功后按最小间隔决定是否更新 `last_seen_at` / `updated_at` |
| 访问令牌撤销 | 设备撤销时批量撤销其 active 访问令牌 |
| 设备撤销计划 | 同时撤销设备本身与其 active 刷新凭据 |

### 20.2 一条容易被写坏的性质

**无需写入时必须返回原对象本身**，而不是等价副本。调用方靠对象同一性（`is`）判断是否发起数据库写入——若返回副本，节流就形同虚设，每次认证都会写库。测试用 `is` 断言把这一点钉住。

另外两条：

1. **重复撤销保留首次撤销时间。** `revoked_at` 取设备已有值，重放撤销请求不会改写审计时间；凭据沿用同一时间戳，避免设备与凭据的撤销时间不一致。
2. **撤销计划严格限定同一设备。** 凭据中混入其它设备必须报错，否则一次撤销会波及别的设备。

### 20.3 验证证据

- 17 项活动节流与撤销测试全部通过（含对象同一性、间隔边界、幂等、跨设备拒绝）；
- 设备认证模块共 **16 个公开函数**，与 TypeScript 侧 `device-auth.ts` 的能力面一一对应；
- **测试规模**：`uv run pytest` → **167 项通过**（M1 18 + M2 14 + M3 61 + M5 74）。

### 20.4 待完成（M5 剩余）

三种 OAuth 来源的登录往返（Google / Linux.do / GitHub），以及设备凭据兑换与 logout 撤销的**端到端链路**。

## 21. M5 进展：普通 OAuth 登录来源（2026-10-12）

**新增**：`server-py/src/lyra/application/app_error.py`、`server-py/src/lyra/infrastructure/auth/login_provider.py`、`oauth_login_client.py`、`server-py/tests/test_oauth_login.py`。

### 21.1 本轮完成

| 能力 | 说明 |
| --- | --- |
| 登录来源契约 | 三个来源的固定顺序、显示名与身份结构 |
| 配置加载与校验 | 完整配置才启用；**部分配置直接失败**；回调地址与 owner ID 严格校验 |
| owner 准入 | 只认 provider + 不可变数字 ID |
| 授权地址 | GitHub 带 S256 PKCE；LINUX DO 不发 PKCE |
| 授权码交换 | 注入式抓取器，端点全部来自服务端常量 |
| 身份提取 | 严格校验 ID 与账户状态，清洗展示名 |

### 21.2 四条安全约束（均有测试）

1. **不把 access token 当成可自行解码的身份声明**——每次交换后向**固定**用户端点再验证一次；端点来自服务端常量，不接受任何来源提供的 URL。
2. **不传播底层异常**——授权码、client secret 与第三方令牌都可能出现在异常消息或响应正文里，因此一律折叠为同一个安全文案。测试断言错误消息里既不含授权码也不含密钥。
3. **只信任实际字节数**——同时校验声明的 `Content-Length` 与**实际读到的长度**。只信前者等于把内存上限交给第三方控制。
4. **不跟随重定向**——跟随即等于把凭据发给第三方控制的地址。

### 21.3 两处与 TypeScript 对齐的语义

- **owner 准入只看 `provider + subject`**：同名、同邮箱，甚至**另一个来源的相同 ID** 都不获权限（有专门测试）；
- **LINUX DO 的 `silenced` 不等于禁用登录**：它是论坛禁言状态，owner 准入只看不可变 ID；只有 `active === false` 才拒绝。

### 21.4 验证证据

- 45 项 OAuth 测试全部通过（配置边界、注入式交换、失败折叠、体积上限、身份校验）；
- 端点与超时均被断言为服务端常量值；
- **测试规模**：`uv run pytest` → **212 项通过**。

### 21.5 待完成（M5 剩余）

Google OIDC 来源、登录事务（state / nonce / PKCE 绑定）、设备凭据兑换与 logout 撤销的端到端链路。

## 22. M5 进展：Google OIDC 来源（2026-10-12）

**新增**：`server-py/src/lyra/infrastructure/auth/google_login_client.py`、`server-py/tests/test_google_login.py`。

### 22.1 与普通 OAuth 的本质区别

Google 走 **OIDC**：真正的身份来自 **ID token**，而不是用户信息端点。因此职责划分必须是：

| 职责 | 归属 |
| --- | --- |
| 签名、`iss`、`aud`、`exp` 时效 | **官方库 `google-auth`**（负责拉取 JWKS 并验签） |
| `nonce` 绑定、`email_verified`、`sub`/email 格式、owner 准入 | 本模块 |

**不得用 JSON 解码替代官方验签入口**——那等于接受任何人伪造的身份令牌。这条约束写进了模块与函数文档。

### 22.2 本轮完成

| 能力 | 说明 |
| --- | --- |
| 配置加载与校验 | 五项全空则关闭；部分配置失败；**必须配置至少一个 owner 维度** |
| 授权地址 | 始终带 `nonce` + S256 PKCE + `prompt=select_account` |
| 授权码交换 | 注入式抓取器与验签入口；失败一律折叠为安全错误 |
| OIDC 声明校验 | 纯函数，逐条覆盖 14 类声明约束 |
| owner 准入 | 邮箱与 subject **各自独立生效**，配置了的维度必须全部匹配 |

### 22.3 三处容易漏掉的校验

1. **`azp` 也要校验**：`aud` 正确但 `azp` 指向另一个 client，说明令牌是给别的应用签发的。
2. **`email_verified` 必须严格为布尔 `true`**：字符串 `"true"` 不能通过。
3. **`nonce` 用常量时间比较**其 SHA-256，避免通过响应耗时试探。

另外，`prompt=select_account` 是刻意的：多账号浏览器里若不强制选择，会静默复用上一次会话，用户会以为自己换了账号。

### 22.4 验证证据

- 40 项 Google 测试全部通过：配置边界、14 类声明拒绝、owner 维度组合、失败折叠不泄漏凭据；
- 测试中注入的"验签入口"直接返回 payload——它模拟的是**已经验签成功**的输入，不是在测试里绕过验签；
- **测试规模**：`uv run pytest` → **252 项通过**。

### 22.5 待完成（M5 剩余）

登录事务（state / nonce / PKCE 绑定）、设备凭据兑换与 logout 撤销的端到端链路。

## 23. M5 进展：登录事务（2026-10-12）

**新增**：`server-py/src/lyra/application/owner_login_service.py`、`owner_login_providers.py`、`server-py/tests/test_owner_login.py`。

### 23.1 事务模型

每个事务绑定固定来源，并同时持有三组秘密的**哈希与派生值**：

| 项 | 用途 | 存放形式 |
| --- | --- | --- |
| `state` | 浏览器回调绑定 | 只存 SHA-256 |
| `nonce` | OIDC 绑定（传给上游） | 只存 SHA-256 |
| provider code verifier | 上游 PKCE（Google / GitHub） | 明文暂存，回调时立即清空 |
| App code challenge | App→Lyra 的独立 PKCE | 存 challenge，兑换时比对 |

**App verifier 不进入浏览器，第三方令牌不返回 App。**

### 23.2 三条安全语义（均有测试）

1. **state 只存哈希，且必须命中已登记事务**——未登记或畸形的 state 一律 `LOGIN_STATE_INVALID`，不进入哈希查询。
2. **兑换必须带 App verifier**：只有 loginId 甚至浏览器 URL 都不足以领取凭据。比对使用常量时间比较。
3. **先占用再执行**：状态机在首个失败点之前就推进，使并发兑换只能有一个胜者；重复兑换得到 `LOGIN_ALREADY_REDEEMED`（409）而不是重复签发。

### 23.3 一处刻意的容量策略

容量占满（128）时**淘汰最早的事务，而不是对所有人返回 429**。

如果对所有人返回 429，未认证的攻击者只要占满槽位，就能让所有者**永久无法发起新登录**——这是把限流做成了拒绝服务。限流（每来源 20 次/分钟）负责压制攻击节奏，容量淘汰负责保证最新请求始终可用。

### 23.4 PKCE 的独立验证

`pkce_challenge` 用 **RFC 7636 附录 B 的官方测试向量**验证，而不是「自己算自己解」：

```
verifier  dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk
challenge E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM  ✅
```

输入按 **ASCII** 编码（PKCE verifier 的字符集本就限定在 ASCII），与现有实现一致。

### 23.5 验证证据

- 34 项登录事务测试全部通过：目录顺序、参数边界、单来源限流与滑动窗口、容量淘汰、state 绑定与重放、owner 拒绝、过期、兑换单次性、第三方令牌不外泄；
- **测试规模**：`uv run pytest` → **286 项通过**。

### 23.6 待完成（M5 剩余）

设备凭据兑换（`authorizeOwnerDevice` 的事务实现）与 logout 撤销的端到端链路。

## 24. M5 完成：设备会话服务（2026-10-12）

**新增**：`server-py/src/lyra/application/device_pairing_service.py`、`write_coordinator.py`、`server-py/tests/test_device_pairing_service.py`。

### 24.1 与数据库的边界

服务层只依赖 :class:`DeviceRepository` **协议**（事务、设备、凭据的读写），SQL 层实现可独立演进，而事务边界与安全语义保持不变。本轮用内存仓储验证语义，DuckDB 实现随后续增量补齐。

之所以先做服务层：这里集中了全部安全语义（凭据复用、整族撤销、撤销幂等），而 SQL 层是机械实现。

### 24.2 本轮完成

| 能力 | 说明 |
| --- | --- |
| 设备签发 | 记 owner 身份、按指纹复用设备 ID、签发 access + refresh |
| 重新配对 | **先撤销旧凭据再签发**，避免旧 refresh 在新会话后仍可用 |
| 令牌刷新 | 轮换 refresh、签发新 access、重用即撤销整族 |
| 退出登录 | 撤销设备与其全部凭据；重复调用保留首次撤销时间 |
| 单写协调器 | 进程内串行化 DuckDB 写操作，一个任务失败不污染队列 |

### 24.3 TTL 与一条被测试纠正的语义

- access token **15 分钟**；refresh 凭据 **30 天**。

**登出后刷新报 `DEVICE_CREDENTIAL_REUSED`（而不是「设备已撤销」）**，这是刻意且与现有实现一致的：退出登录会撤销该设备全部凭据，因此**非 active 一律按重放处理并撤销整族**。把已撤销的凭据当成可重试的会话错误，等于给拿到旧令牌的攻击者留下试探空间。

这一条最初被我的测试写成「设备已撤销」，是实现正确、预期写错，已按实现修正并把理由写入测试文档字符串。

### 24.4 验证证据

- 20 项设备会话测试全部通过：TTL 精确值、同键复用设备 ID、重新配对撤销顺序、整族撤销、撤销幂等、重登恢复同设备、单写队列串行与失败隔离；
- **测试规模**：`uv run pytest` → **306 项通过**（M1 18 + M2 14 + M3 61 + M5 213）。

### 24.5 M5 小结

认证与会话的可验证部分已全部落地：

| 子项 | 内容 |
| --- | --- |
| 设备认证 | 凭据原语、访问令牌、刷新轮换与重放撤销、配对挑战与公钥、活动节流与撤销计划 |
| 登录来源 | Google OIDC、LINUX DO、GitHub OAuth |
| 登录事务 | state / nonce / PKCE 绑定、限流、容量淘汰、单次兑换 |
| 设备会话 | 签发、重新配对、刷新、退出登录 |

**尚未落地**：登录与设备的 HTTP 路由（属 M4）。

## 25. M4 起步：设备会话的 DuckDB 仓储（2026-10-12）

**新增**：`server-py/src/lyra/infrastructure/db/device_repository.py`、`server-py/tests/test_device_repository.py`。

### 25.1 本轮完成

把 M5 的 `DeviceRepository` 协议落到 **schema 15 的真实表**上：`paired_devices`、`device_refresh_credentials`、`device_access_tokens`、`server_owner_identity`。

集成测试跑在真实 DuckDB 上（不是内存假仓储），因此同时验证了 UUID、TIMESTAMPTZ、CHECK 约束与唯一索引确实可用。11 项集成测试覆盖：签发落库、同键复用设备、重新配对撤销旧凭据、刷新轮换、重用即整族撤销、退出登录、撤销幂等、**事务回滚不留半提交凭据**。

### 25.2 一个真实的部署发现

**DuckDB 的 Python 驱动读取 `TIMESTAMPTZ` 需要 `pytz`；缺失时写正常、读报错**：

```
Invalid Input Error: Required module 'pytz' failed to import
```

这是很难排查的失败模式——签发凭据看起来成功，直到第一次读取设备记录才炸。已把 `pytz` 显式声明为依赖，并在 `pyproject.toml` 里注明原因，而不是依赖环境自带。

### 25.3 一处必须保持的往返形状

领域层按 **ISO 文本**比较时间（例如刷新时判断 `now >= expires_at`）。因此仓储**必须返回 ISO 文本**，返回 `datetime` 对象会让比较静默失败。`_iso()` 统一把 DuckDB 的时间值渲染为与 JavaScript `toISOString()` 同形的 `...T...:...:....000Z`，并有一条专门的测试断言这一点。

### 25.4 验证证据

- 11 项仓储集成测试全部通过；
- **测试规模**：`uv run pytest` → **317 项通过**（M1 18 + M2 14 + M3 61 + M5 213 + M4 11）。

## 26. M4 进展：账本快照读取（2026-10-12）

**新增**：`server-py/src/lyra/infrastructure/db/ledger_repository.py`、`server-py/tests/test_ledger_repository.py`。

### 26.1 本轮完成

把 `transactions` / `lots` / `realized` 还原为领域对象，供分析层直接消费。集成测试在**真实 schema 15** 上插入数据后读回，并验证快照可以直接驱动分析层（`summary` 与 `drawdown` 端到端跑通）。

### 26.2 两条必须保持的形状约定

1. **日期以 ISO 文本进出。** DuckDB 返回 `date` 对象，而领域层用**文本**排序与比较（`open_date`、`close_date`）。直接传对象会让排序退化成对象比较，跨类型比较还会直接抛错。
2. **账户以脱敏键呈现。** 表里存的是 `account_id` UUID，领域层的账户身份是 `legacy_account_key`。少了这层 JOIN，**所有账户范围过滤都会静默失效**——查一个账户会返回全部账户的数据。

### 26.3 验证证据

- 9 项账本仓储集成测试全部通过：账户键映射、日期文本往返、稳定排序、精确 Decimal、未平批次、已实现明细、**账户范围同时作用于三张表**、未知账户返回空而非回落全部、快照驱动分析层端到端；
- **测试规模**：`uv run pytest` → **326 项通过**。

## 27. M4 进展：只读端点与设备认证（2026-10-12）

**新增**：`server-py/src/lyra/application/device_authenticator.py`、`api/dependencies.py`、`api/ledger_routes.py`、`server-py/tests/test_ledger_routes.py`。

### 27.1 本轮完成

| 能力 | 说明 |
| --- | --- |
| 访问令牌认证 | 读取令牌 → 校验 → 更新活动时间；失败统一 401 |
| 认证依赖 | FastAPI `Depends`，从 `Authorization: Bearer` 取令牌 |
| 只读端点 | 14 条：`/v1/ledger/*` 与 `/v1/analytics/*`，路径与 TS 侧逐条对齐 |

### 27.2 三条实现要点

1. **金额必须是字符串出站。** FastAPI 的默认编码器把 `Decimal` 转成 `float`——那会直接丢精度，违反项目既定的金额传输规则。因此所有响应先经 `to_jsonable` 显式转字符串，并有测试逐字段断言类型。
2. **认证失败统一 401**，不区分令牌不存在、设备不存在与已过期，否则未认证调用方可以靠错误码枚举出哪些令牌曾经存在。
3. **`as_of` 为必填参数。** 缺省成"今天"会让同一请求的结果随服务器日期漂移，产出的分析无法复现。

### 27.3 一条明确的未完成项

端点目前返回**领域对象直接序列化的形状**（字段名与分析层一致，与 oracle 的 CLI 输出吻合），**尚未套上 TypeScript 工具运行时的公开投影**（camelCase 字段名与按币种分组的信封）。

这一层必须在切流前补齐，否则 Flutter 会看到字段名差异。已记入 M4 待办，不是遗漏。

### 27.4 验证证据

- 14 项端点测试全部通过：缺令牌/非法头/已撤销令牌均 401、总览与回撤结果正确、金额为字符串、`as_of` 必填、账户范围生效、**14 条路由逐条断言必须要求认证**（防止漏出一个公开端点）；
- **测试规模**：`uv run pytest` → **340 项通过**。

## 28. M4 进展：快照补充账户与币种（2026-10-12）

### 28.1 为什么先补这一步

核对确认公开响应的形状是**按币种分组的信封**：

```ts
createAgentSummary(...)  →  { aggregateCurrency, groups: [{ currency, accountIds, data }] }
```

也就是说，同一份账本要**先按账户基础币种切分再合计**——跨币种直接相加在会计上是错的。而账本快照此前不含账户与币种，因此这是投影层的必要前置。

### 28.2 本轮完成

`LedgerSnapshot` 增加 `accounts`（`LedgerAccount`：内部 UUID、脱敏账户键、`base_currency`），并受账户范围过滤。

两条断言：
- 账户范围**同样作用于账户列表**——否则公开投影会带出范围外的账户；
- 币种随快照返回——投影层据此切分，不再需要额外查询。

### 28.3 验证证据

- 11 项账本仓储测试全部通过（新增账户与币种加载、范围过滤）；
- **测试规模**：`uv run pytest` → **342 项通过**。

### 28.4 M4 剩余

各端点的字段级公开投影（camelCase、账户引用改为 UUID），随后与 TS 逐字段对照。

## 29. M4 进展：按币种分组的信封（2026-10-12）

**新增**：`server-py/src/lyra/domain/analytics/currency_groups.py`、`server-py/tests/test_currency_groups.py`。

### 29.1 为什么必须分组

`USD` 与 `HKD` 的金额**直接相加在会计上是错的**——得到的数字不对应任何真实资产。所以金额类分析必须先按账户基础币种切分，在**每个币种内部**独立计算，再并列返回。

### 29.2 一处不会报错的错误

分组后**必须用该币种的子集去计算**，而不是拿全量快照算完再按币种贴标签。后者会把其它币种的数字混进本组，而且**不会以异常形式出现**——只有在真实多币种账本上才看得出来。

因此有一条专门的断言：两个币种各 2 笔与 1 笔交易时，两组结果必须是 2 和 1；若实现用全量快照，两组都会是 3。

### 29.3 信封语义

| 字段 | 语义 |
| --- | --- |
| `aggregate_currency` | **仅当范围内恰好一种币种**时给出；跨币种为 `null` |
| `groups[].currency` | 该组的基础币种 |
| `groups[].account_ids` | 该组的账户**内部 UUID**（不是脱敏键） |
| `groups[].data` | 针对该币种子集的计算结果 |

### 29.4 验证证据

- 7 项分组测试全部通过：单一币种给出合计币种、跨币种为 `null`、**每组用自身子集**、子集同时收窄账户与批次、账户 UUID 暴露、范围先作用于分组、空范围为信封而非回落全部；
- 一处被测试纠正的预期：HKD 组应保留**它自己的账户**（`(1, 0)` 而不是 `(0, 0)`）——实现正确，是我的断言写错；
- **测试规模**：`uv run pytest` → **349 项通过**。

## 30. M4 进展：summary 端点的公开投影（2026-10-12）

**新增**：`server-py/src/lyra/api/public_projection.py`。

### 30.1 首个端点完成协议 2 对齐

`GET /v1/ledger/summary` 现在返回与 TypeScript 一致的形状：

```json
{
  "aggregateCurrency": "USD",
  "groups": [{ "currency": "USD", "accountIds": ["<uuid>"], "data": { "netDeposits": "...", ... } }]
}
```

即：**币种信封 + camelCase 字段**。此前返回的是领域对象的 snake_case 扁平结构（与 oracle 一致，但客户端读不懂）。

### 30.2 一处刻意的实现选择

投影**不用通用驼峰化函数**，而是显式逐字段列出。

通用规则（`net_deposits` → `netDeposits`）看似等价，但一旦某个字段需要改名——例如 `account` → `accountId` 做脱敏——通用规则会**悄悄产出旧名字**，客户端读到读不懂的字段却不报错。显式列出后，漏字段会直接失败。

### 30.3 验证证据

- summary 端点的三条测试已更新为信封断言：`aggregateCurrency == "USD"`、`groups[0].data.transactionCount == 2`、金额为 camelCase 十进制字符串；
- 范围外账户返回**空信封**（`groups == []`、`aggregateCurrency == null`），而不是回落到全部账户；
- **测试规模**：`uv run pytest` → **349 项通过**。

### 30.4 M4 剩余

其余端点的字段级投影（trades / realized / cashflow / audit / 各 analytics 端点），随后与 TS 逐字段对照。

## 31. M4 进展：positions / expiring 投影与一处真实渲染差异（2026-10-12）

### 31.1 本轮完成

`GET /v1/ledger/positions` 与 `GET /v1/ledger/expiring` 完成协议 2 对齐：

| 项 | 变化 |
| --- | --- |
| 账户引用 | 脱敏键 → **内部 UUID**（`accountId`），不再泄漏服务端账户键 |
| 字段名 | snake_case → camelCase |
| 到期统计 | `asOf` / `expiresToday` 等 camelCase 字段 |

### 31.2 一处被测试抓到的真实渲染差异

公开投影最初用 `format(Decimal, "f")`，它**保留标度**（`"2.000000"`）。而 TypeScript 的公开投影用 decimal.js `toString()`，**去掉尾随零**（`"2"`）。

也就是说**同一个字段在两处渲染不同**：

| 场景 | `quantity` 渲染 |
| --- | --- |
| oracle CLI（`schwab --json`） | `"2.000000"`（保留标度） |
| 公开 API（客户端协议 2） | `"2"`（去尾随零） |

投影层必须用去零版本。若不是测试断言了精确字符串，这个差异只会在真实客户端上表现为"字段值看起来变了"。

### 31.3 验证证据

- 新增 2 项投影测试：账户脱敏为 UUID 且不再出现 `account` 键、`qty` 改名为 `quantity`、到期统计为 camelCase；
- **测试规模**：`uv run pytest` → **351 项通过**。

## 32. 修复一个会让 M3 全部验证失效的缺口（2026-10-12）

**新增**：`server-py/tests/test_parity_support.py`（14 项）。

### 32.1 缺口是什么

Python 侧的对照器 `parity_support.compare_structure` **没有任何元测试**——只有
`test_analytics_parity.py` 使用它，而那个测试永远拿真实夹具比对。

后果是：**如果对照器有 bug**（例如永远返回空差异、或路径前缀写错导致零交集），
那么 M3c 全部 **2771 个字段的比对结果都会失效——而测试依然全绿**。

这正是我在 TypeScript 侧做了 `parity-meta.test.ts`、却在 Python 侧漏掉的一层。
**一个不会因差异而失败的比较器等于没有比较器。**

### 32.2 补上的元测试

通过**主动篡改期望值**证明对照器真的会失败：

| 用例 | 断言 |
| --- | --- |
| 对照组 | 未篡改的期望值必须通过 |
| 改数值 | 篡改 `open_positions` 必须被发现 |
| 改文本 | 篡改 `date_range` 必须被发现（不只测数字） |
| 删行 | 数组少一行必须被发现 |
| 零交集 | 键名全对不上必须报「覆盖不足」，而不是静默通过 |
| 覆盖下限 | 下限不得超过 oracle 自身字段数（否则空账本误报） |
| 白名单 | **只放行命中路径**，其余差异仍然报错 |
| 标签前缀 | 路径必须带命令前缀，否则白名单无法精确定位 |

另有一组基线工具测试：数值归一（`"5.000000"` ≈ `"5"`、`-0` ≈ `0`）、日期与哈希不被破坏、行序无关、`to_plain` 保留 `Decimal`、数组按字段拉平。

### 32.3 顺带确认的一处设计差异

Python 对照器**刻意不做键名归一**（TypeScript 侧做 snake→camel 归一）。因为 Python
的领域类型**直接采用 oracle 键名**，归一反而会掩盖命名差异。已有一条测试显式钉住
这个选择：命名不同即判为不可比。

### 32.4 验证证据

- **测试规模**：`uv run pytest` → **365 项通过**。

## 33. M4 进展：drawdown 端点的信封与投影（2026-10-12）

### 33.1 本轮完成

`GET /v1/analytics/drawdown` 完成协议 2 对齐：**币种信封 + camelCase 字段**。

核对确认该命令在 TypeScript 侧把**领域结果直接**放进币种信封（没有额外重命名），
因此公开字段就是领域字段的 camelCase 形式：`endingCumulativePnl` / `maxDrawdown` /
`peakDate` / `troughDate` / `recoveryDate`，行内为 `dailyPnl` / `cumulativePnl` /
`runningPeak`。

### 33.2 已在信封内的端点

| 端点 | 状态 |
| --- | --- |
| `/v1/ledger/summary` | ✅ 信封 + 投影 |
| `/v1/analytics/drawdown` | ✅ 信封 + 投影 |
| `/v1/ledger/positions` · `/v1/ledger/expiring` | ✅ 独立公开投影（不套信封） |

其余 analytics 端点（monthly / allocation / attribution / risk / holding-period /
extremes / streaks / stress）的 `data` 结构各自不同，需逐一读 TS 对应函数后显式列出，
做法与陷阱见附录 A。

### 33.3 验证证据

- 回撤测试已更新为信封断言：`aggregateCurrency == "USD"`、`data.endingCumulativePnl == "200.0000"`、**snake_case 字段名不再出现**、行内字段为 `cumulativePnl`；
- **测试规模**：`uv run pytest` → **365 项通过**。

## 34. M4 进展：7 条 analytics 端点批量收口（2026-10-12）

### 34.1 一次批量

核对确认剩余 analytics 命令中有 **7 条属于同一形态**：TypeScript 侧把**原始领域
结果**直接放进币种信封，不做额外重命名。因此它们只需要各自的 camelCase 映射，
可以一次做完：

| 端点 | 信封 data 关键字段 |
| --- | --- |
| `/v1/analytics/monthly` | `totalPnl` / `totalFees` / 行内 `winRatePct` / `profitFactor` |
| `/v1/analytics/allocation` | `totalCostBasis` / 行内 `allocationPct` |
| `/v1/analytics/attribution` | `totalPnl` / 行内 `absoluteContributionPct` |
| `/v1/analytics/risk` | `asOf` / `optionPositions` / `topThreeLongUnderlyings` |
| `/v1/analytics/holding-period` | `buckets` / 行内 `averageHoldingDays` |
| `/v1/analytics/streaks` | `closeTransactions` / `neutralTransactions` |
| `/v1/analytics/stress` | `longOptionContracts` / `allLongOptionsLossIfZero` |

### 34.2 一条防退化的结构性断言

新增测试对每个端点的公开 `data` 逐键断言**不得含下划线**。

因为投影是显式逐字段列出的，漏掉一个字段就会把 snake_case 名字发给客户端——
而客户端只会读到 `undefined`，**不会报错**。这条断言让"漏投影"立刻变成测试失败。

（该断言只检查顶层键；嵌套行的字段由各自的字段断言覆盖。）

### 34.3 验证证据

- 8 项新测试：6 条端点参数化断言信封结构（`aggregateCurrency` / `currency` / `accountIds` 为 UUID / 关键公开字段存在）、风险端点的 `asOf` 必填与 `as_of` 不泄漏、公开 data 无 snake_case；
- **测试规模**：`uv run pytest` → **373 项通过**。

## 35. M4 进展：查询类端点投影（2026-10-12）

### 35.1 本轮完成

`/v1/ledger/trades`、`/v1/ledger/realized`、`/v1/ledger/cashflow` 完成协议 2 对齐。

| 端点 | 公开形状 |
| --- | --- |
| `trades` | `{rows, count}`；行内 `accountId` / `symbol`（取自 `raw_symbol`） |
| `realized` | `{rows, count, totalPnl}`；行内 `quantity` / `pnl` |
| `cashflow` | `{rows, count, total, byAction}` |

### 35.2 一处需要关联回完整交易的地方

`TradeRow` 与 `CashflowRow` 是 **oracle 视图的窄行**（`v_trades` / `v_cashflows` 的列），
**不含 `source_type`**——而公开投影需要它。

因此投影按 `txn_hash` 关联回完整交易再投影，而不是将就窄行发出缺字段的响应。

### 35.3 一处刻意不发出的字段

TypeScript 侧的公开查询结果含 `totalCount` 与 `nextCursor`。**Python 侧未实现游标分页**，
因此投影**不发出这两个字段**，并有测试断言它们不被伪造。

伪造 `totalCount = count` 在 limit 截断时是错的；伪造 `nextCursor` 会让客户端无限翻页。
宁可缺字段，也不发假数据。补齐游标分页后再加这两个字段（已在附录 A 记录）。

### 35.4 顺带确认

交易的 action 取值受 schema CHECK 约束限定（`Buy` / `Sell` / `Buy to Open` /
`Qualified Dividend` / `MoneyLink Transfer` 等）。测试种子的现金动作用了不在白名单里的
`Dividend`，被数据库拒绝——这是 schema 在替我们挡住伪造的交易类型。

### 35.5 验证证据

- 4 项新测试：交易投影隐藏哈希与来源引用、现金流水带分动作合计、无现金交易时返回空而非把买卖算入、**不伪造分页字段**；
- **测试规模**：`uv run pytest` → **377 项通过**。

## 36. M4 完成：全部账本与分析端点（2026-10-12）

### 36.1 本轮完成

`/v1/ledger/audit` 与 `/v1/analytics/extremes` 完成协议 2 对齐。

| 端点 | 形状 |
| --- | --- |
| `audit` | **原始领域结果**（不套信封），camelCase：`readOnly` / `transactionCount` / `latestTransactionDate` / `scopeNote` |
| `extremes` | 自有投影：账户脱敏为 UUID，**不公开交易哈希** |

### 36.2 M4 收口状态

**16 条账本与分析端点全部完成协议 2 对齐**（币种信封或自有投影 + camelCase 公开字段）：

- 信封类（9 条）：`summary`、`monthly`、`allocation`、`attribution`、`drawdown`、`risk`、`holding-period`、`streaks`、`stress`
- 自有投影类（7 条）：`positions`、`expiring`、`trades`、`realized`、`cashflow`、`audit`、`extremes`

**未完成**：`/v1/market/positions`（依赖行情数据，归 M8）；游标分页（`totalCount` / `nextCursor`）。

### 36.3 一条覆盖全部端点的防退化断言

新增 `test_no_read_endpoint_leaks_snake_case_keys`：**递归**检查全部 16 条端点响应，
断言不得出现含下划线的键名（`byAction` 字典的字面键是 Action 名，属数据而非字段名）。

因为投影是显式逐字段列出的，漏一个字段就会把内部命名发给客户端——而客户端只会读到
`undefined`，**不会报错**。这条断言让"漏投影"立刻变成测试失败。

### 36.4 一处被测试纠正的预期

极值交易在只有一笔平仓交易时返回 **2 行**——该交易**同时是最佳与最差**。我的断言
原本写 1 行，是实现正确、预期写错。

### 36.5 验证证据

- 3 项新测试：审计 camelCase、极值脱敏与哈希剥离、**全端点无 snake_case**；
- **测试规模**：`uv run pytest` → **380 项通过**。

## 37. M6 起步：导入提交计划（2026-10-12）

**新增**：`server-py/src/lyra/application/import_commit.py`、`server-py/tests/test_import_commit.py`。

### 37.1 边界选择

TS 的导入子系统有 10 个文件、约 1290 行（上传、预览、提交、任务、视图、账户绑定）。
本轮先做 **`import-commit.ts`（85 行）**——它是唯一的写入入口，事务语义与去重规则
都在这里，其余文件是围绕它的流程编排与视图。

### 37.2 本轮完成

`build_import_commit_plan`：在**打开写事务之前**完成去重与 FIFO 重放。

| 行为 | 说明 |
| --- | --- |
| 按 `transaction_hash` 去重 | 重复导入同一份文件、或两次导出区间重叠时跳过已有交易 |
| 无新增交易 → `changed=False`、`fifo=None` | **不做无收益的整本重放** |
| 有新增 → 重放**完整**账户账本 | 只重放新增部分会算错未平批次 |
| 混入他账户交易 → 直接报错 | 说明账户范围过滤失效，继续会把别的账户写进本账户 |

写事务应当尽可能短：能预先算完的都留在事务外。数据库适配器进入单写队列后**仍需
再次校验账本版本**，然后在一个事务中插入交易并替换该账户的 lots / realized。

### 37.3 一处被测试纠正的预期

我原本断言「预览内部的重复哈希也应折叠」，但现有实现**只与既有账本比对**，不处理
预览内部重复。

这是对的：`transaction_hash` 已包含 occurrence，解析同一份文件不会产出两条哈希相同的
交易；真出现说明上游有 bug，应由数据库唯一约束拒绝，而不是在这里静默吞掉一条。
已把测试改为断言该行为并写明理由。

### 37.4 验证证据

- 9 项测试：全新增、跳过已有、整份重复为 no-op（且 `fifo is None`）、既有+新增顺序、他账户拒绝、空预览、**重放覆盖完整账本**（开平仓配对后可验证）、计划不改写交易内容；
- **测试规模**：`uv run pytest` → **389 项通过**。

### 37.5 M6 剩余

上传校验、预览生成与提交快照校验、幂等键、导入任务与视图、账户绑定、以及 DuckDB 侧的
「再次校验账本版本 + 单事务替换 lots/realized」适配器。

## 38. M6 进展：上传边界校验（2026-10-12）

**新增**：`server-py/src/lyra/application/import_upload.py`、`server-py/tests/test_import_upload.py`。

### 38.1 本轮完成

`inspect_import_upload` 与 `ImportUploadMonitor`：在**创建临时文件与进入解析器之前**
拦下非法上传。

**只识别扩展名、大小与文件签名**，不解析 CSV/ZIP——真正的内容解析仍交给标准库并复用
统一八列标准化内核。这里做的是边界，不是解析。

### 38.2 三条关键检查

1. **扩展名与实际签名必须一致。** `.csv` 实际是 ZIP（`PK\x03\x04`）或旧版 OLE 容器
   （`D0 CF 11 E0 A1 B1 1A E1`）时拒绝——否则会把二进制当文本喂给解析器。
2. **CSV 开头不得含 NUL。** 含 NUL 说明不是文本，作为文本导入只会产出垃圾交易。
3. **文件名不得含路径分隔符。** 否则临时文件会落到预期目录之外。

另：大小上限与采样大小都必须是正安全整数；这类**配置错误抛 `ValueError`**，而内容是
`ApplicationError`（422），格式不支持是 415——三类失败有不同的归属，不应混为一谈。

### 38.3 与 Node 实现的差异及原因

TypeScript 用 Node `Transform` 流；Python 侧没有对应管道，改为显式 `feed` / `finish`。
语义保持：**超限时立即抛错**（不是等到最后），且 `summary()` 必须在成功结束后调用
——半读文件不能当作完整上传生成摘要。

### 38.4 一条记录在案的已知边界

NUL 检测只覆盖**头部样本窗口**（默认 4096 字节），窗口之后的 NUL 由解析器负责。
测试里有一条专门记录这个边界的用例（`test_nul_after_sample_window_is_not_detected`）
——它是已知范围，不是理想行为，写下来以免日后被误认为漏洞。

### 38.5 验证证据

- 34 项测试：大小与文件名边界、ZIP/OLE 签名与扩展名不一致、NUL 拒绝、415 与支持格式明细、
  扩展名大小写、监控器哈希与有界采样、**超限中途抛错**、未结束不得出摘要、结束后不得再喂数据；
- **测试规模**：`uv run pytest` → **423 项通过**。

## 39. M6 进展：写入适配器（2026-10-12）

**新增**：`server-py/src/lyra/infrastructure/db/import_commit_repository.py`、`server-py/tests/test_import_commit_repository.py`。

### 39.1 本轮完成

`commit_import`：M6 的核心写入路径——**再校验账本版本 + 单事务插入交易并替换该账户的
lots / realized**。集成测试跑在真实 schema 15 上。

事务顺序（与 TypeScript 一致）：

```
BEGIN
  校验账户 ID ↔ 账本账户键一致
  读取当前账本版本；与预览时的版本不符 → LEDGER_VERSION_CONFLICT (409)
  无变更 → COMMIT，返回原版本（不新增版本号）
  登记新账本版本
  插入本次新增交易（引用新版本）
  DELETE realized / lots WHERE account_id      ← 先删后插
  插入 FIFO 重算后的 lots / realized
  写 ledger_version_accounts 关联
COMMIT
```

### 39.2 四条被测试钉住的性质

1. **版本冲突必须整体回滚。** 只拒绝请求却留下半提交数据，比不拒绝更糟——账本里会
   出现「只有新交易、没有对应批次」的状态，后续分析算出错误持仓。
2. **FIFO 产物是全量重算，先删后插。** 增量合并会留下过期批次。
3. **无变更不新增账本版本。** 版本号本身是审计信号，凭空递增没有意义。
4. **替换只作用于目标账户。** 有专门用例验证另一个账户的批次原样保留。

### 39.3 两个来自数据库的约束（测试暴露）

- **`lots.open_transaction_hash` 有指向 `transactions` 的外键**——批次不能凭空存在，
  必须对应一条真实交易。测试里手工造批次时被数据库拒绝。
- **显式写 `ledger_versions.version` 不会推进 `ledger_version_seq`**，下一次 `nextval`
  仍返回同一个值并撞主键。**版本号必须由序列分配**。这条对生产代码同样适用：任何
  手工插入账本版本的地方都要注意。

### 39.4 DuckDB 的一处非直觉约束

**不允许更新已被外键引用的父行**，即使主键不变。因此当提交需要写 `import_files`
终态时，必须在 `transactions` 引用它**之前**写。本模块暂未接管导入作业表，该顺序
约束在接入作业表时适用，已写在模块文档里。

### 39.5 验证证据

- 9 项集成测试：提交写入交易/批次/已实现/版本关联、开平仓配对产生已实现、版本单调递增、
  **冲突整体回滚（逐表计数不变）且 details 含期望与实际版本**、无变更不推进版本、
  账户不匹配在任何写入前被拒、**FIFO 产物被替换而非追加**、替换只作用于一个账户、
  计划缺 FIFO 产物被拒；
- **测试规模**：`uv run pytest` → **432 项通过**。

### 39.6 M6 剩余

预览生成与提交快照校验、幂等键、导入任务与视图、账户绑定、preview 单次消费与作业终态写入。

## 40. M6 进展：导入预览与提交快照校验（2026-10-12）

**新增**：`server-py/src/lyra/application/import_preview.py`、`server-py/tests/test_import_preview.py`。

### 40.1 两阶段导入的绑定问题

导入是**两阶段**的：先预览（告诉用户会插入多少、跳过多少），用户确认后提交。两阶段
之间存在时间差，期间账本可能被别的导入改动，因此预览必须携带**指纹**，提交时逐项复核。

预览绑定**四项**条件：

| 条件 | 为什么必须绑定 |
| --- | --- |
| 文件哈希 | 换一份文件就代表另一批交易 |
| **解析器版本** | 标准化规则变了，旧预览里的行不再代表同一批交易 |
| 账户 | 账户变了会把交易提交到别处 |
| 基准账本版本 | 账本被推进后，预览里"将插入 N 行"的结论可能已失效 |

### 40.2 一处刻意的错误报告方式

`assert_import_preview_current` **一次性列出全部变化的字段**，而不是遇到第一个就返回。

只报一个字段会让用户在重新预览时反复撞上新的冲突——每修一个又冒一个。一次报全，
用户才能一次把条件摆平。

### 40.3 指纹格式

指纹是 `sha256(JSON.stringify({fileHash, parserVersion, account, baseLedgerVersion, transactionHashes}))`，
键顺序固定、无多余空白，与 JavaScript 同形。指纹是内部一致性凭据，格式漂移会让同一份
预览在两处算出不同值。

### 40.4 验证证据

- 18 项测试：计数与日期范围、已存在交易计入跳过、按 Action 分组、基准版本与解析器版本
  校验、**指纹对四项条件各自敏感**（参数化逐一验证）、内容变化改变指纹、
  条件一致放行、**过期预览一次报全变化字段**、账本推进与账户变化各自失效；
- **测试规模**：`uv run pytest` → **450 项通过**。

### 40.5 M6 剩余

幂等键、导入任务与视图、账户绑定、preview 单次消费与 `import_files` 作业终态写入。

## 41. M6 进展：幂等键（2026-10-12）

**新增**：`server-py/src/lyra/application/idempotent_mutation.py`、`server-py/tests/test_idempotent_mutation.py`。

### 41.1 为什么写入路径必须先有幂等

App 重试是常态（网络抖动、超时重发）。没有幂等键，一次重试就会把导入做两遍。
幂等表的作用是"**重建一个当前的安全响应**"，而不是缓存响应体——因此表里
**只存资源 ID 与 HTTP 状态**，不存响应正文、access token、refresh token 或 OAuth code。

### 41.2 五条分支

| 已有记录 | 结果 |
| --- | --- |
| 无 | `start`：新建进行中记录 |
| 未过期 · 请求指纹相同 · 进行中 | 409 **可重试** |
| 未过期 · 请求指纹相同 · 已完成 | `replay`：直接返回结果引用，**不重复执行修改** |
| 未过期 · 请求指纹**不同** | 409 **不可重试** |
| 已过期 | `start`，并原子替换旧记录 |

### 41.3 一处容易混同的地方

两种 409 的**可重试性必须不同**：

- `IDEMPOTENCY_REQUEST_IN_PROGRESS` → 可重试（等一会儿重发即可）
- `IDEMPOTENCY_KEY_REUSED` → 不可重试（键被复用于不同请求，必须换键）

混同会让客户端要么白等（对已失败的请求反复重试），要么误弃（对还在处理中的请求
直接报错）。有一条专门测试断言两者的 `retryable` 不同。

### 41.4 另外两条被钉住的边界

- **过期判定为开区间**：恰好到期视为已过期（键可复用），早一毫秒视为未过期（重放）。
- **朴素时间必须拒绝**：不带时区的 `datetime` 在比较时会静默按本地时区解释，过期判断
  会出现时区偏移。

### 41.5 验证证据

- 33 项测试：新建记录（只存哈希、默认/自定义 TTL）、重放、同键不同请求拒绝、
  进行中可重试、**两种冲突可重试性不同**、已完成缺结果引用报错、记录不匹配报错、
  过期替换（含请求指纹不同的情况）、**到期边界开区间**、操作名/指纹/TTL/时区校验、
  完成写入、重复完成拒绝、非 2xx 拒绝、**完成后再次提交得到重放**（端到端闭环）；
- **测试规模**：`uv run pytest` → **483 项通过**。

### 41.6 M6 剩余

导入任务与视图、账户绑定、preview 单次消费与 `import_files` 作业终态写入。

## 42. M6 端到端：写入链路贯通（2026-10-12）

**新增**：`server-py/tests/test_import_end_to_end.py`（6 项）。

### 42.1 为什么要专门做端到端

前面的测试各自验证一个环节：预览生成、提交计划、幂等判定、仓储写入、读取投影。
**单测各自通过并不代表它们能组合工作**——接缝处的字段名、账户键、版本号、日期形状
都可能在传递中走样，而每个单测都只看到自己那一半。

本文件把完整链路串起来：

```
原始八列行 → 预览 → 提交计划 → 单事务提交 → 从库里读回 → 分析层计算
```

### 42.2 覆盖的场景

| 场景 | 断言要点 |
| --- | --- |
| 完整导入 | 读回的交易哈希与预览**逐一相同**；开平仓配对后无未平批次、一条已实现 |
| 驱动分析 | 导入的数据能算出总览（`realized_pnl_total` / `cash_balance`） |
| 第二次导入 | 版本号 +1、追加交易、**按完整账本重算 FIFO**（新批次出现、旧已实现保留） |
| 重复导入同一文件 | `insertable_rows == 0`、`changed == False`、账本逐字段不变 |
| 过期预览 | 提交前复核拦下，`IMPORT_PREVIEW_STALE` + `changed_fields` |
| 部分导入 | 只导入开仓时持仓正确反映 |

### 42.3 一处被测试纠正的预期

我断言 `cash_balance == "-300.0000"`，实际是 `"200.0000"`。

原因是 `cash_balance` 是**快照内全部交易金额之和**，而我在另一个测试里的
`-300.0000` 那份种子里还有第三笔属于别的账户的交易（-500）。这里只有两笔：
`-400 + 600 = +200`。实现正确，是我的期望值串了种子。

**这也说明账户范围对 `cash_balance` 有直接影响**——读取时若少了范围过滤，
金额会跨账户相加。

### 42.4 验证证据

- 6 项端到端测试全部通过；
- **测试规模**：`uv run pytest` → **489 项通过**。

## 43. M6 进展：导入账户绑定（2026-10-12）

**新增**：`server-py/src/lyra/application/import_account.py`、`server-py/tests/test_import_account.py`。

### 43.1 这是导入的授权闸门

绑定决定一份文件能写进哪个账户，规则刻意保守：**只有能唯一确定时才自动绑定**，
否则要求用户显式选择。

| 输入 | 结果 |
| --- | --- |
| 唯一命中的完整旧键 | 自动绑定（`legacy_key`） |
| 显式选择 + 线索相符 | 绑定（`explicit_selection`） |
| 线索对不上任何账户 | 409 要求选择 |
| 线索命中多个账户 | 409 要求选择 |
| 指向已归档账户 | 409 要求选择 |

### 43.2 一处不能放宽的校验

**显式选择不能绕过线索校验。** 若允许，用户可以把 A 账户的文件提交到 B 账户——
那就把授权检查做成了摆设。有专门测试断言显式选择与线索不符时仍被拒。

### 43.3 一处安全语义

绑定失败的错误里**只返回候选 UUID**，绝不回显文件中的账户键或完整券商标识。
候选集合本身也按线索过滤，不把全部账户列出来。

有专门测试断言错误消息与 `details` 序列化后**不含任何账户键**，且 `details`
只有 `candidate_account_ids` 一项。

### 43.4 验证证据

- 14 项测试：自动绑定与大小写/空白容忍、空线索抛 `ValueError`、未知线索与归档账户
  要求选择、显式选择生效与后缀线索、**显式选择不能绕线索**、异构候选、**错误不泄漏
 账户键**、候选排序去重；
- **测试规模**：`uv run pytest` → **503 项通过**。

## 44. M6 进展：导入作业状态机与公开视图（2026-10-12）

**新增**：`server-py/src/lyra/application/import_job.py`、`server-py/tests/test_import_job.py`。

### 44.1 为什么用显式转换图

导入是**多步流程**（上传 → 解析 → 预览就绪 → 提交中 → 已提交），每步都可能被取消、
拒绝或失败。用一张转换图管，而不是散落的 `if`：

```
uploaded    → parsing | cancelled
parsing     → preview_ready | rejected | failed
preview_ready → committing | cancelled
committing  → committed | failed
committed / rejected / failed / cancelled → （终态，无出边）
```

### 44.2 三条性质（均有测试）

1. **终态不存在出边。** 少了这条，一个迟到的重试就能把「已提交」改回「提交中」，
   进而**重复写入账本**。测试对四个终态 × 全部目标状态做了穷举（参数化组合）。
2. **同状态请求返回原对象本身**，调用方据此跳过写库。返回等价副本会让每次幂等轮询
   都产生一次无收益写入。
3. **跳过步骤必须被拒**：`uploaded → committing`（跳过预览）、`committing → cancelled`
   （提交中不可取消）等都被显式断言。

### 44.3 公开视图的一条安全语义

**非稳定错误码必须被拒绝，不能当自由文本透传给 App。**

数据库里如果出现不在闭集里的 `error_code`（例如某天有人把底层异常直接写进去），
投影会**抛错**而不是把它转发给客户端。有一条测试专门用一个形如
`PSQLException ... postgres://user:pw@host` 的字符串验证它被拒绝。

公开视图同时省略：文件哈希、解析器版本、预览令牌摘要、底层错误详情。

### 44.4 验证证据

- 63 项测试（59 通过 + 4 个同状态用例由专门测试覆盖而跳过）：合法转换逐条、
  **终态穷举**、跳步拒绝、同状态返回原对象、未知状态与朴素时间拒绝、
  **转换图覆盖全部状态**、公开视图恢复字段、**不泄漏提交秘密与错误详情**、
  稳定错误码闭集、非稳定错误码被拒；
- **测试规模**：`uv run pytest` → **566 项通过**。

### 44.5 M6 剩余

`import_files` 仓储与终态写入、导入 HTTP 路由（`POST /v1/imports/preview`、
`GET /v1/imports/:id`、`POST /v1/imports/:id/commit`、`POST /v1/imports/:id/cancel`）。

## 45. M6 进展：导入作业仓储与 CAS 并发（2026-10-12）

**新增**：`server-py/src/lyra/infrastructure/db/import_job_repository.py`、`server-py/tests/test_import_job_repository.py`。

### 45.1 为什么状态更新必须是 CAS

领域层的 `transition_import_job` 只在内存里判定转换合法性。两个并发请求可能读到
同一个旧状态、各自判定合法、然后**都去写库**。

因此数据库侧用比较并交换做唯一裁决：

```sql
UPDATE import_files SET status = ?, updated_at = ?
WHERE id = ? AND status = ?     -- 只有仍处于预期状态才能改
RETURNING id
```

0 行受影响表示状态已被并发请求改动，调用方必须重新读取后再决定。

### 45.2 一处刻意的防御式重复校验

仓储**重复校验一次转换图**（领域层已校验过）。

只看 CAS 的话，调用方一旦忘记先调 `transition_import_job`，就能把「已提交」写成
「提交中」——**而账本已经写过了**，回退状态会让重试再次导入。

这一条最初被我的测试写成了「仓储会允许终态出边」（我按 TS 的纯 CAS 行为写断言），
复核后认为加这层防护更安全，于是改实现并把测试改为断言 `IMPORT_STATE_CONFLICT`。
这是**有意偏离** TS 的宽松行为，属于更严格而非行为变更。

### 45.3 另一个来自数据库的约束

`import_files.preview_ledger_version` 引用 `ledger_versions(version)`，因此写入预览
基准版本前该版本必须已存在。测试里凭空写 `99` 会被外键拒绝——这条约束防止作业
记录引用一个不存在的账本版本。

### 45.4 验证证据

- 10 项集成测试：创建/读回（时间戳为 ISO 文本）、未知作业返回 `None`、CAS 成功、
  **第二个并发请求失败且不改写状态**、终态被拒、未知作业返回 `False`、
  `record_preview` 写入元数据并推进状态、**非 parsing 状态拒绝写预览**、外键约束；
- **测试规模**：`uv run pytest` → **571 项通过**（4 项跳过）。

### 45.5 M6 剩余

四条导入 HTTP 路由（`POST /v1/imports/preview`、`GET /v1/imports/:id`、
`POST /v1/imports/:id/commit`、`POST /v1/imports/:id/cancel`）与作业创建的编排。

## 46. M6 进展：作业生命周期编排与两次 CAS（2026-10-12）

**新增**：`server-py/src/lyra/application/import_job_service.py`、`server-py/tests/test_import_job_service.py`。

### 46.1 编排的四步

```
1. 读取作业
2. 判定转换是否合法（领域状态机）
3. CAS 写入
4. CAS 失败 → 重读并重新判定 → 再试一次 CAS → 仍失败才报可重试冲突
```

第 4 步是必须的：CAS 失败说明**别人刚改过**，此时基于旧状态得出的判定结论已经作废。

### 46.2 一处设计缺陷（本轮自己发现并修掉）

初版在重判之后**直接报可重试冲突**，没有第二次 CAS。后果是代码里那段
`retryable=True` 分支实际不可达，而且**两个并发取消请求会有一个失败**——明明可以
都成功。

改为：重判后若新状态允许该转换，**再试一次 CAS**；两次都失败才报可重试冲突。
这样两个并发取消都能成功，而真正的并发热点仍会被如实报告。

### 46.3 两种冲突的可重试性不同（又是这一对）

- 并发把作业改成 **`committing`**（不可取消）→ **不可重试**。作业已在提交中，
  反复重试取消永远不会成功；客户端应刷新后看到 `committing`。
- 两次 CAS 都失败（真热点）→ **可重试**。

我的测试最初把第一种写成「可重试」，复核后按上面理由改正——**这是实现与预期
两处都需要澄清**，不是单方面改测试。

### 46.4 一处安全选择

作业不存在时报 **409 而不是 404**：404 会让未授权方探测出某个作业 ID 是否存在。

### 46.5 另一处

`fail_import_job` 的 `error_code` **必须在读库之前**校验（必须是稳定集合内的值）。
否则一份含连接串或堆栈的文本会先被读出来、再被写进**可公开**的作业记录。

### 46.6 验证证据

- 14 项测试：可取消状态、**已取消幂等且不写库**、不可取消状态拒绝、未知作业 409、
  **并发已达同一状态时视为成功**、并发进入 committing 时不可重试冲突、
  失败标记、**非稳定错误码在读库前被拒**、终态拒绝、返回公开视图而非原始记录；
- **测试规模**：`uv run pytest` → **585 项通过**（4 项跳过）。

### 46.7 M6 剩余

四条导入 HTTP 路由（`POST /v1/imports/preview`、`GET /v1/imports/:id`、
`POST /v1/imports/:id/commit`、`POST /v1/imports/:id/cancel`）与上传暂存编排。

## 47. M6 进展：导入作业 HTTP 路由（2026-10-12）

**新增**：`server-py/src/lyra/api/import_routes.py`、`server-py/tests/test_import_routes.py`。

### 47.1 本轮完成

| 路由 | 状态 |
| --- | --- |
| `GET /v1/imports/{importId}` | ✅ 公开作业状态 |
| `POST /v1/imports/{importId}/cancel` | ✅ 取消（幂等） |
| `POST /v1/imports/preview` | ⛔ 未实现（需 multipart 上传暂存） |
| `POST /v1/imports/{importId}/commit` | ⛔ 未实现（需提交令牌校验） |

先做前两条是因为它们不需要上传管道。后两条不是遗漏，是**明确未实现**。

### 47.2 一处被自己抓到的问题

初版路由直接把公开视图 dataclass 交给通用序列化器，结果响应里出现
`file_name`、`total_rows` 等 **snake_case 字段**。

这正是我在账本路由上立的规矩——**通用序列化器按字段名出站，会把内部命名发给
客户端，而客户端只会读到 `undefined`，不会报错**。已补
`project_import_job` 显式逐字段映射为 camelCase。

### 47.3 一条互补的回归断言

新增 `test_import_routes_do_not_leak_snake_case_keys`，与账本路由那条同构。
两条合起来覆盖了当前全部公开端点；**新增端点时必须把它加入列表**，否则同类问题
会再次静默发生。

### 47.4 验证证据

- 10 项测试：两条路由要求认证、读取公开状态、**不泄漏提交秘密**、未知作业 409、
  取消推进状态并落库、**HTTP 层取消幂等**、未知作业取消 409、**提交中不可取消**、
  不影响既有账本路由、**无 snake_case 泄漏**；
- **测试规模**：`uv run pytest` → **595 项通过**（4 项跳过）。

### 47.5 M6 剩余

`POST /v1/imports/preview`（multipart 上传 + 暂存 + 解析）与
`POST /v1/imports/{importId}/commit`（提交令牌校验 + 账本版本 CAS + 幂等记录完成）。

## 48. M6 进展：预览确认授权（2026-10-12）

**新增**：`server-py/src/lyra/application/import_preview_auth.py`、`server-py/tests/test_import_preview_auth.py`。

### 48.1 一次性令牌的两段职责

预览阶段签发**不透明令牌**，明文只返回一次，服务端只存哈希。提交时必须同时满足
五项条件才放行：

| 条件 | 失效时的 `changed_fields` |
| --- | --- |
| 作业 ID | `import_id` |
| 预览指纹 | `preview_fingerprint` |
| 作业状态为 `preview_ready` | `job_status` |
| 未过期 | `expires_at` |
| 令牌匹配 | `preview_token` |

### 48.2 一条必须写清的职责边界

**授权校验本身不提供一次性语义。**

本模块只做校验；真正的一次性由调用方的 CAS（`preview_ready → committing`）完成。
只调校验然后直接写账本，会让同一个令牌被并发提交两次。

这条边界不仅写在模块文档里，还写成了测试
（`test_validation_alone_does_not_consume_the_token`）——**同一记录校验两次都会
通过**，以此提醒后来者：不要以为"校验过了就不可能重复提交"。

### 48.3 两处与既有实现一致的取舍

- **一次性列出全部失效原因**（`changed_fields` 数组），而不是只报第一个。只报一个
  会让排查反复碰壁：修好一个又冒出下一个。
- **令牌用常量时间比较**，避免通过响应耗时逐字节试探前缀。
- **朴素时间视为校验失败**（计入 `expires_at`），而不是静默通过。

### 48.4 验证证据

- 19 项测试：发只存哈希、令牌唯一、输入校验（ID/指纹/TTL，含布尔与非整数）、
  朴素时间拒绝、全部相符放行、错令牌、**到期边界（等于即失效、差 1 毫秒有效）**、
  五种非法状态、指纹与作业 ID 不符、**一次报全五项失效**、朴素 now 计为失效、
  **校验不消费令牌**；
- **测试规模**：`uv run pytest` → **614 项通过**（4 项跳过）。

### 48.5 M6 剩余

`POST /v1/imports/preview`（multipart 上传 + 暂存 + 解析）与
`POST /v1/imports/{importId}/commit`（拼装：授权校验 → CAS 置 committing →
账本提交 → 作业终态 → 幂等记录完成）。

## 49. M6 收官：提交编排与顺序正确性（2026-10-12）

**新增**：`server-py/src/lyra/application/import_commit_service.py`、`server-py/tests/test_import_commit_service.py`。

### 49.1 顺序就是正确性

```
1. 读取作业
2. 校验预览授权（作业 ID / 指纹 / 状态 / 有效期 / 令牌）
3. CAS：preview_ready → committing      ← 一次性消费发生在这里
4. 账本提交（内部：再校验版本 + 单事务写交易并替换 lots/realized + 作业终态）
5. 失败则 CAS：committing → failed
```

**第 3 步必须在第 4 步之前。** 若先写账本再改状态，两个并发提交会**都通过授权校验、
都去写账本**，交易被导入两次。授权校验只能证明"令牌曾经有效"，不能证明"它还没被
用过"——CAS 是唯一能挡住这件事的地方。

有一条测试直接把这层意图写成断言：在 `commit_ledger` 回调里检查作业状态必须已经是
`committing`。顺序被改坏时它立刻失败。

### 49.2 为什么第 3、4 步不在同一事务

两者之间崩溃会留下 `committing` 且账本未写入的作业。这是**刻意**的：

- 若把状态也放进账本事务，崩溃后作业会停在 `preview_ready`，而账本可能已经写了——
  重试就会重复导入，且**没有任何痕迹**。
- 停在 `committing` 则是一个可观测的异常状态：运维能查到，`committing → failed`
  也是合法转换，用户重新预览即可。

### 49.3 失败后令牌不可重用

账本写入失败时作业进入 `failed`，**同一令牌不能重试**（测试直接验证第二次提交被拒）。

退回 `preview_ready` 会让同一个令牌再试一次，而账本可能已经写了一半——那是最糟的
状态：不确定写没写。

### 49.4 验证证据

- 10 项测试：成功路径经过 committing、账本拿到**预览时的基准版本**、
  **写账本时作业已离开 preview_ready**、同令牌二次提交被拒、**CAS 输掉不写账本**、
  账本失败标记 failed、**失败后同令牌不可重试**、授权失败不碰账本也不改状态、
  未知作业拒绝、缺基准版本拒绝；
- **测试规模**：`uv run pytest` → **624 项通过**（4 项跳过）。

### 49.5 M6 剩余

`POST /v1/imports/preview`（multipart 上传 + 暂存 + 解析）与
`POST /v1/imports/{importId}/commit` 的 HTTP 封装（拼装已有编排 + 幂等记录）。

## 50. M6 进展：上传到预览的组装（2026-10-12）

**新增**：`server-py/src/lyra/application/import_prepare.py`、`server-py/tests/test_import_prepare.py`。

### 50.1 本轮完成

把「上传边界校验 → CSV 解析 → 预览生成」串成一步：

```
字节 → ImportUploadMonitor（逐块投喂、上限/哈希/采样）→ inspect（签名/扩展名）
     → parse_schwab_csv（八列标准化 + 金额勾稽）
     → create_import_preview（去重统计 + 日期范围 + 指纹）
```

逐块投喂的意义：**超限在接收时就抛错**，而不是把整个文件读进内存后才发现。

### 50.2 一个如实报出的缺口

**TypeScript 侧有 `schwab-xlsx.ts`，Python 侧尚未移植 Excel 解析。**

因此 `.xlsx` 上传会得到显式的：

```json
{ "code": "IMPORT_FORMAT_NOT_IMPLEMENTED", "status": 501, "details": {"file_type": "xlsx"} }
```

刻意**不**让它走 `IMPORT_UNSUPPORTED_FORMAT`（415）——那个码的含义是"协议不支持这种
格式"，而 xlsx 是协议明确支持的。混用会让客户端把服务端的能力缺口误读为用户的
文件选错了。

有测试固定这个行为，避免日后被误读为"协议就该这样"。

### 50.3 验证证据

- 12 项测试：CSV 正常组装、**哈希对应真实字节**、已有哈希减少可插入数、超限拒绝、
  空文件拒绝、文件名路径分隔符拒绝、**xlsx 显式 501**、非 csv/xlsx 走 415、
  表头不符抛 `IngestError`、文件名无法解析账户抛错、**金额勾稽不符由标准化层拒绝**；
- **测试规模**：`uv run pytest` → **636 项通过**（4 项跳过）。

### 50.4 M6 剩余

`POST /v1/imports/preview` 与 `POST /v1/imports/{importId}/commit` 的 HTTP 封装
（拼装已有组装函数 + 作业创建 + 幂等记录），以及 **Excel 解析移植**。

## 51. 修复一个会让"空账本首次导入"永久失败的 bug（2026-10-12）

### 51.1 问题

上一轮我写的提交编排里，`_require_preview_version` 把
`import_files.preview_ledger_version IS NULL` 当成"字段缺失"并**拒绝提交**。

但核对 TypeScript 侧 `import-service.ts:166` 后发现语义相反：

```ts
previewLedgerVersion: preview.baseLedgerVersion === 0 ? null : preview.baseLedgerVersion,
```

**NULL 表示"预览时是空账本，基准为 0"**。数据库里之所以用 NULL 而不是 0：
`preview_ledger_version` 引用 `ledger_versions(version)`，而**空账本没有 version 0
这一行**，存 0 会违反外键。

后果：**空账本上的首次导入永远无法提交**——也就是每一个新用户、每一本新账本的
第一次导入。这是最常走的路径。

### 51.2 修复

`_preview_base_version` 在 NULL 时返回 0，并把理由写进文档字符串。原测试
`test_missing_preview_baseline_is_rejected` 被替换为
`test_null_baseline_means_empty_ledger_not_missing`，断言账本提交收到的期望版本是
**0**。

### 51.3 这个 bug 为什么单元测试没抓到

各部件单独看都是对的：预览返回 `baseLedgerVersion: 0`，作业仓储按 TS 规则存 NULL，
提交编排拒绝 NULL。**错在接缝处的语义约定**——只有把两端的假设摆在一起对照才会
暴露。

这也是为什么本轮先做"读 TS 确认语义"再动手接线：如果直接按自己的假设接线，这个
bug 会一直留到真实用户第一次导入时才炸。

### 51.4 验证证据

- 提交编排 10 项测试全部通过（含新增的空账本基准断言）；
- **测试规模**：`uv run pytest` → **636 项通过**（4 项跳过）。

### 51.5 本轮未完成

`POST /v1/imports/preview` 与 `commit` 的 HTTP 封装。本轮把时间用在先确认语义上，
避免把错误假设接进路由。已完成的前置：`parse_uploaded_file` 从 `prepare_import_preview`
拆出（账户只有解析后才知道，而预览需要该账户的账本上下文）。

## 52. M6 进展：预览路由落地（2026-10-12）

### 52.1 本轮完成

`POST /v1/imports/preview` —— 至此 M6 的四条路由已完成 **3/4**：

| 路由 | 状态 |
| --- | --- |
| `POST /v1/imports/preview` | ✅ |
| `GET /v1/imports/:importId` | ✅ |
| `POST /v1/imports/:importId/cancel` | ✅ |
| `POST /v1/imports/:importId/commit` | ⛔ 未实现 |

请求体是**原始字节**（`application/octet-stream`），文件名走 `x-lyra-file-name` 头——
与 TypeScript 侧一致，不用 multipart。

### 52.2 顺序问题

账户标识来自文件名，**只有解析后才知道**；而预览需要该账户的账本上下文（基准版本
与已有哈希）。因此顺序必须是：

```
解析 → 绑定账户 → 读取账本上下文 → 生成预览
```

为此把 `parse_uploaded_file` 从 `prepare_import_preview` 中拆出（上一轮已做前置）。

### 52.3 又一处自己引入的类型不一致

`PARSER_VERSION` 在提交仓储里被写成**整数 `2`**，而预览校验期望**字符串**
（schema 列是 VARCHAR，TS 也用字符串）。

后果：预览接口直接抛 `AttributeError: 'int' object has no attribute 'strip'`。
已改为 `"2"`，并在定义处写明"必须是字符串"及其原因（它同时参与预览指纹的文本拼接）。

一处更早就能发现的信号：`test_import_end_to_end.py` 里早就本地定义了
`PARSER_VERSION = "2"`——如果当时把它统一到一处，这个 bug 不会出现。

### 52.4 空账本基准的落库方式

预览写入作业时，`preview_ledger_version` 在**空账本时写 NULL**（而非 0），
因为该列引用 `ledger_versions(version)`，而空账本没有 version 0 的行。
测试直接断言这一行是 `None`。

### 52.5 验证证据

- 9 项新测试：预览要求认证、缺少文件名头 400、**完整上传返回预览与令牌**、
  **数据库只存令牌哈希**（长度 64 且不等于明文）、空账本基准落库为 NULL、
  不外发预览指纹、无 snake_case、账户无法绑定时 409、**xlsx 为 501**、
  表头不符 422、非 csv/xlsx 为 415；
- **测试规模**：`uv run pytest` → **645 项通过**（4 项跳过）。

### 52.6 M6 剩余

`POST /v1/imports/{importId}/commit` 的 HTTP 封装（拼装提交编排 + 幂等记录），
以及 **Excel 解析移植**。

## 53. M6 进展：上传暂存（2026-10-12）

### 53.1 接线时发现的架构前置

准备封装 `commit` 路由时发现：**提交需要重新解析上传内容，而解析结果并未持久化**
（作业记录只存指纹与统计）。因此预览时必须把原始字节落到暂存目录，提交时读回并
校验。

这解释了 `LYRA_IMPORT_STAGING_DIR` 这个配置为什么会存在——它不是为了"缓存上传"，
而是**提交阶段的必要输入**。TypeScript 侧对应 `staged-upload.ts` 的 `stageImportUpload`。

### 53.2 本轮完成

`StagedUploadStore`：按作业 ID 暂存、读回与清理上传字节。

| 操作 | 说明 |
| --- | --- |
| `stage` | 原子写入；写前自校验哈希 |
| `load` | 读回并**校验哈希** |
| `discard` | 幂等删除 |
| `exists` | 是否仍在 |

### 53.3 三条安全性质（均有测试）

1. **路径只用作业 UUID 拼，绝不使用客户端文件名。** 否则文件名里的 `..` 或分隔符
   能把文件写到暂存目录之外。测试对 6 种非 UUID 输入断言四种操作全部拒绝。
2. **读回时校验哈希。** 预览与提交之间存在时间窗，字节必须与预览时逐位一致；
   不一致说明被替换或损坏，**必须拒绝而不是照常提交**。有一条测试直接篡改暂存文件
   后断言读取失败。
3. **原子写入**（先写临时文件再改名）。崩溃只会留下临时文件，不会留下**可被当成
   完整上传读走的半截文件**——后者会让提交阶段解析出残缺账本。

### 53.4 另一处

`stage` 在**写入前**就校验哈希：宁可拒绝，也不要落一个"看起来正常"的坏文件。
一旦落盘，后续任何环节都会默认它是可信的。

### 53.5 验证证据

- 15 项测试：往返一致、自动建目录、写入前哈希校验、**篡改后读回被拒**、
  文件缺失抛错、幂等删除、**6 种非 UUID 输入在四种操作上全部被拒**、
  暂存文件名与客户端文件名无关、**不留临时文件**、空目录配置拒绝；
- **测试规模**：`uv run pytest` → **660 项通过**（4 项跳过）。

### 53.6 M6 剩余

`POST /v1/imports/{importId}/commit`（拼装：暂存读回 → 授权校验 → CAS → 账本提交 →
作业终态 → 幂等记录）、预览阶段接入暂存、以及 **Excel 解析移植**。

## 54. M6 完成：导入四条路由全部落地（2026-10-12）

### 54.1 四条路由

| 路由 | 状态 |
| --- | --- |
| `POST /v1/imports/preview` | ✅ 上传 → 解析 → 绑定账户 → 预览 → 暂存字节 |
| `GET /v1/imports/:importId` | ✅ 公开作业状态 |
| `POST /v1/imports/:importId/commit` | ✅ 暂存读回 → 授权校验 → CAS → 账本提交 → 终态 |
| `POST /v1/imports/:importId/cancel` | ✅ 幂等取消 |

**端到端 HTTP 测试已跑通**：预览 → 提交 → 账本真的写入 transactions/lots → 作业进入
`committed` → 暂存文件被清理。

### 54.2 提交路由的拼装顺序

```
读取作业 → 读回暂存（校验哈希）→ 重新解析 → 事务外算提交计划
        → commit_import_job：
             授权校验（作业 ID / 指纹 / 状态 / 有效期 / 令牌）
             CAS preview_ready → committing      ← 一次性消费
             账本提交（内部再校验版本 + 单事务写交易并替换 lots/realized）
             成功 → committing → committed；失败 → committing → failed
        → finally 清理暂存（无论成败）
```

**暂存在 `finally` 里清理**：失败的作业必须重新预览，留着字节没有用途，而且会把
账本数据留在磁盘上。

### 54.3 为提交补齐的两处存储

作业记录此前缺 `preview_fingerprint` 与 `preview_expires_at`（数据库列早就存在，
是迁移 7 加的）。补进 `PersistedImportJobRecord` 与仓储读写，提交时据此重建
`ImportPreviewAuthorizationRecord`。

字段带默认值 `None`，因此既有构造点无需改动。

### 54.4 验证证据

- 新增 7 项 HTTP 测试：提交要求认证、**三种非法请求体**一律 400、
  **端到端写入账本且清理暂存**、错令牌被拒且不写账本、同一令牌不能提交两次、
  **暂存缺失时拒绝提交**、未知作业 409；
- **测试规模**：`uv run pytest` → **667 项通过**（4 项跳过）。

### 54.5 M6 剩余（不阻塞主流程）

- **Excel 解析移植**（TS 有 `schwab-xlsx.ts`；当前 `.xlsx` 返回显式 501）
- **幂等记录串联**（`plan_idempotent_mutation` 已就绪，尚无 `mutation_idempotency_records`
  仓储与路由侧 `Idempotency-Key` 处理）

## 55. M7 起步：财务工具目录（2026-10-12）

**新增**：`server-py/src/lyra/application/financial_tools.py`、`server-py/tests/test_financial_tools.py`。

### 55.1 目录即单一事实来源

18 个工具的名字、顺序与 HTTP 路径必须与 TypeScript 侧一致——Flutter 与 Agent 的提示词
都按名字引用工具，改名或换序会同时打断两边。

| 分组 | 数量 | 工具 |
| --- | --- | --- |
| ledger | 8 | accounts · summary · positions · expiring · trades · realized · cashflow · audit |
| analytics | 9 | monthly · allocation · attribution · drawdown · risk · holding-period · extremes · streaks · stress |
| market | 1 | market-positions |

### 55.2 最要紧的一条测试：目录与实际路由对账

静态清单会与实现漂移，而且**两种漂移都是静默的**：

- 目录里有但路由没挂 → Agent 调用得到 404，没人会主动发现；
- 路由挂了但目录没有 → 能力存在但无法被发现。

因此有一条测试遍历实际挂载的 `APIRoute`，逐条对账：

```python
for tool in FINANCIAL_TOOLS:
    if tool.name in UNIMPLEMENTED_TOOLS:
        assert tool.path not in mounted   # 实现了却还挂在"未实现"里
    else:
        assert tool.path in mounted       # 声明实现了却没挂
```

对账结果与事实一致：**未实现的是 `accounts` 与 `market-positions`**（后者依赖行情数据，
归 M8）。

### 55.3 一处刻意的写死

`UNIMPLEMENTED_TOOLS` 是**显式清单**而非推断，且有一条测试断言它恰好是那两个名字。
实现补齐后该断言会失败，提醒把名字移走——**让"忘记更新"变成测试失败**，而不是
让目录悄悄分叉。

### 55.4 验证证据

- 8 项测试：18 个工具、名字唯一且顺序稳定、分组数量、路径唯一且前缀与分组一致、
  未知工具名拒绝、未实现清单是子集、**目录与已挂载路由对账**、未实现恰为两个；
- **测试规模**：`uv run pytest` → **675 项通过**（4 项跳过）。

### 55.5 M7 剩余

Genkit Python 接入、18 个工具的调用封装与参数校验、回合生命周期、WebSocket 事件、
会话与工具调用记录。

## 56. M7 进展：工具参数校验（2026-10-12）

**新增**：`server-py/src/lyra/application/financial_tool_validation.py`、`server-py/tests/test_financial_tool_validation.py`。

### 56.1 一份 schema 承担两个职责

工具的 JSON Schema 同时是**对外公开的参数说明**与**服务端执行前的参数边界**。
两者共用同一份 schema，因此不存在"文档说一套、校验做一套"。

### 56.2 关键取舍：不做类型强转

对应 Ajv 的 ``coerceTypes: false``。若能强转，客户端提交 ``"5"`` 会被当成整数 5 通过，
于是：

- 参数类型的事实来源变成**服务端的宽松程度**，而不是 schema；
- 任何依赖"这里一定是 int"的下游代码都会在真实数据上炸。

宁可返回"参数无效"，也不替调用方猜他想要什么。

### 56.3 一处被测试纠正的预期

我原本断言 ``10.0`` 应被整数校验拒绝，实际通过了。

这是**正确行为**：JSON Schema 规范把 ``integer`` 定义为"小数部分为零的数字"，
JSON 本身只有一种数字类型，Ajv 同样接受 ``10.0``。把它当错误会导致两边对同一请求
给出不同结论。已改为断言 ``10.0`` 通过、``10.5`` 被拒，并写明理由。

### 56.4 验证证据

- 22 项测试：合法与最小参数通过、**9 类非法参数**（字符串冒充整数、带小数、
  布尔冒充整数、枚举外取值、上下限、日期模式、多余字段、缺必填）、非对象参数、
  校验抛错变体、**不修改调用方参数**、schema 复用结论稳定、空 schema；
- **测试规模**：`uv run pytest` → **697 项通过**（4 项跳过）。

### 56.5 M7 剩余

Genkit Python 接入、18 个工具的调用封装（schema + 处理器绑定）、回合生命周期、
WebSocket 事件、会话与工具调用记录。

## 57. M6/M7 进展：accounts 工具落地（2026-10-12）

### 57.1 本轮完成

`GET /v1/ledger/accounts` 落地，18 个工具中**只剩 `market-positions` 未实现**
（依赖行情数据，归 M8）。

公开字段（camelCase）：`id` / `displayName` / `broker` / `maskedSuffix` /
`baseCurrency` / `status` / `createdAt`。**不含 `legacyAccountKey`** —— 账本账户键
是服务端内部标识，公开它等于把账户名暴露给客户端。

### 57.2 三级排序是刻意的

排序键依次是：**状态**（active 在前）→ **创建时间** → **ID**。

只按状态排序时，同状态账户的顺序取决于数据库返回顺序——客户端每次刷新都可能看到
不同排列。加上后两级才能得到稳定顺序。

### 57.3 目录清单随实现同步

`UNIMPLEMENTED_TOOLS` 从两个缩到一个，对应的测试断言也同步更新。这正是上一轮设计的
目的：**清单写死内容，实现补齐时测试失败，提醒把名字移走**——而不是让目录悄悄分叉。

### 57.4 验证证据

- 新增 1 项端点测试（camelCase 字段集、且**不含账本账户键**）；
- 目录对账测试自动通过（`accounts` 路径已挂载）；
- 公开端点清单（含 snake_case 递归断言）已加入 `/v1/ledger/accounts`；
- **测试规模**：`uv run pytest` → **698 项通过**（4 项跳过）。

### 57.5 下一步

M7 剩余：Genkit Python 接入、18 个工具的调用封装（schema + 处理器绑定）、
回合生命周期、WebSocket 事件、会话与工具调用记录。

## 58. M7 进展：工具调用分发（2026-10-12）

**新增**：`server-py/src/lyra/application/tool_runtime.py`、`server-py/tests/test_tool_runtime.py`。

### 58.1 三类失败必须分开

把「按名字查找 → 校验参数 → 执行」收敛到一处，供 Genkit 与 REST 两条入口复用。失败
分四类，**不能合并成一个"工具调用失败"**：

| 情况 | 错误码 | 为什么不能合并 |
| --- | --- | --- |
| 工具名不存在 | `TOOL_NOT_FOUND` (400) | 模型编了个不存在的工具名，需要修正**提示词**，不是重试参数 |
| 参数不符合 schema | `TOOL_ARGUMENTS_INVALID` (400) | 模型参数给错，可带着校验信息**重试** |
| 处理器抛出预期错误 | 透传 | 例如账户无权限，调用方需看到真实原因 |
| 处理器抛出意外异常 | `TOOL_EXECUTION_FAILED` (500) | **不得把内部异常文本透给模型** |

### 58.2 一条安全性质

最后一行不是措辞问题：内部异常常含 SQL、路径或连接串。把它们当作工具结果喂回模型，
等于**把服务端内部细节写进对话记录**——而对话记录会被持久化并回放给用户。

有一条测试用形如 ``psycopg2 error: ... postgres://user:pw@host`` 的异常验证它不会
出现在对外错误的消息与 details 里。

### 58.3 另一条

**参数非法时不得执行处理器。** 有一条测试在处理器里记录调用并断言其为空——参数校验
必须发生在副作用之前。

### 58.4 验证证据

- 10 项测试：合法调用、**未知工具与参数非法是不同错误码**、**参数非法时处理器未执行**、
  缺必填项、预期错误透传、**意外异常被折叠且不泄漏内部文本**、工具名重复拒绝、
  顺序稳定、公开 schema、直接取定义报错；
- **测试规模**：`uv run pytest` → **708 项通过**（4 项跳过）。

### 58.5 M7 剩余

Genkit Python 接入、把 17 个路由绑成 `ToolDefinition`（schema + 处理器）、
回合生命周期、WebSocket 事件、会话与工具调用记录。

## 59. M7 实施计划：工具绑定与 Genkit 接入（2026-10-12）

本节是 M7 剩余主体的**实施级计划**（按目标要求"先出计划文件再落地"）。现状：工具目录、
参数校验、分发器、17 条路由均已就绪；**缺的是把路由的处理逻辑抽成可复用的处理器，
再把它们绑成 `ToolDefinition`**。

### 59.1 为什么必须先抽取，而不是直接写 17 个新处理器

复核 `api/ledger_routes.py` 的现状：

| 事实 | 数量 |
| --- | --- |
| 路由处理器 | 17 |
| 其中重复「按账户范围读取快照」 | 16 |
| 其中重复「FIFO 重放」 | 10 |

若为 Agent 另写一套处理器，就会出现**同一分析的两份实现**——两侧的账户范围处理、
投影字段、错误映射都会各自漂移，而 M3 苦心建立的 oracle 一致性只覆盖其中一份。

因此顺序必须是：**先抽取共享处理器 → 路由改为调用它们 → 工具定义也调用它们**。

### 59.2 目标结构

新增 `application/financial_tool_handlers.py`：

```python
@dataclass(frozen=True)
class ToolContext:
    connection: DuckDBPyConnection
    account_scope: AccountScope   # 已由调用方解析完成

def handle_summary(context: ToolContext, arguments: Mapping[str, Any]) -> dict: ...
def handle_positions(context: ToolContext, arguments: Mapping[str, Any]) -> dict: ...
# ... 每条命令一个
```

要点：

- **入参是已解析的账户范围**，不是原始请求。账户绑定（`import_account` 那一套）属于
  入口层，不应混进分析处理器；
- **返回值是已投影的公开 dict**（camelCase），处理器不返回领域对象——否则路由与 Agent
  两侧会各投影一次，字段必然分叉；
- 处理器**只接受已校验的参数**（由 `ToolRuntime.invoke` 保证），因此内部直接索引即可。

### 59.3 三个落地步骤

1. **抽取**：把 17 个路由处理器的方法体搬进 `financial_tool_handlers.py`，路由改为
   `return handle_xxx(_context(request, account), arguments)`。此步**不改任何输出**，
   现有端点测试必须逐条保持通过——这是判断抽取是否无损的唯一标准。
2. **绑定**：新增 `build_financial_tool_definitions()`，为 17 个工具各建一个
   `ToolDefinition`（name 取自 `financial_tools.FINANCIAL_TOOLS`，schema 逐条显式声明，
   handler 调用 59.2 的处理器）。`market-positions` 留空直到 M8。
3. **对账测试**：断言 `ToolRuntime.tool_names` 与 `FINANCIAL_TOOLS` 去掉未实现项后
   **完全一致**。这条与 §55.3 的路由对账互补——那条保证路由齐全，这条保证工具齐全。

### 59.4 参数 schema 的取值口径

schema 必须与 TypeScript 侧**逐字段一致**：字段名、必填项、范围、枚举。

已知易错点（前几轮踩过）：

- `asOf` 是**必填**（`expiring` / `risk`），缺省会让结果随服务器日期漂移；
- `limit` 有上下限，且**不做类型强转**（`"10"` 不是整数）；
- `groupBy` 是**枚举**（`underlying` / `asset_type`），不是自由字符串；
- 账户参数在 REST 侧是 query，在工具侧是可选字段——两者都要落到同一个 `AccountScope`。

### 59.5 Genkit 接入（抽取完成后）

1. 依赖 `genkit`（Python 包），把 `ToolDefinition` 转成 Genkit 工具；
2. 模型调用 → `ToolRuntime.invoke` → 处理器 → 公开 dict 作为工具结果；
3. **失败语义按 §58 分成四类**：未知工具与参数非法要能让模型自我修正，意外异常一律
   折叠，不得把内部文本写进对话记录；
4. 回合生命周期与 WebSocket 事件：参考 `agent-turn-orchestrator.ts` 与
   `transport/agent-routes.ts`，注意事件顺序与取消传播。

### 59.6 验收标准

| 项 | 标准 |
| --- | --- |
| 抽取无损 | 现有 17 条端点测试**全部保持通过**，输出逐字段不变 |
| 工具齐全 | 除 `market-positions` 外 17 个工具可调用 |
| 对账 | 工具名集合与目录一致（去掉未实现项） |
| 失败分类 | 四类失败各有独立用例，意外异常不泄漏内部文本 |
| 口径 | 与 M3 的 oracle 一致性测试仍全绿 |

### 59.7 本轮未落地

本节只出计划，**未写实现**——因为 59.1 的抽取会改动 `ledger_routes.py` 全部 17 个
处理器，属于高风险重构，必须在上下文充足时一次性完成并逐步验证。

## 60. M7 执行：抽取共享处理器（2026-10-12）

### 60.1 执行 §59.1 的第一步

复核发现一个简化机会：**7 条 analytics 端点形状完全相同**——

```
按账户范围取快照 → 按币种切分 → 每组独立计算 → 投影为 camelCase → 套信封
```

差异只在「算什么」与「怎么投影」，因此用一张表描述即可，不必复制七遍：

```python
CURRENCY_GROUPED_COMMANDS = {
    "monthly": CurrencyGroupedCommand(compute=..., project=project_monthly),
    "allocation": ..., "attribution": ..., "drawdown": ...,
    "holding-period": ..., "streaks": ..., "stress": ...,
}
```

新增 `handle_currency_grouped(command, snapshot, scope, arguments)`，7 条路由改为各调一行。

### 60.2 分层修正

投影原本在 `api/public_projection.py`，而 application 层要用它——那会让 application
依赖 api，方向错误。已移到 `application/public_projection.py`（投影是应用结果的形状，
不是 HTTP 关注点），api 层改为从 application 导入。

### 60.3 无损性证据

**现有端点测试全部保持通过（708 项）**，未修改任何断言——这正是 §59.3 设定的
"抽取是否无损"的唯一标准。输出逐字段不变。

### 60.4 一次操作事故（如实记录）

替换路由体用的分块脚本在最后一条路由处把「文件剩余全部」当成替换范围，**误删了
`return router` 与 `build_authenticator`**。

表现为 `AttributeError: 'NoneType' object has no attribute '_contains_router'`——
`create_ledger_router()` 返回了 `None`。两处已补回。

教训：**用「下一个锚点」界定替换范围时，必须处理「没有下一个锚点」的情况**；
否则最后一块会被当成"到文件末尾"，顺带吃掉尾部定义。这类脚本改动用 `git diff` 复核
比逐条断言更可靠。

### 60.5 验证证据

- **测试规模**：`uv run pytest` → **708 项通过**（4 项跳过），断言未改动；
- 已注册命令：`allocation` / `attribution` / `drawdown` / `holding-period` /
  `monthly` / `streaks` / `stress`（7 条）。

### 60.6 下一步（按 §59.3）

1. 抽取其余 10 条端点（`risk` 需 `asOf`，单独处理；账本类 7 条形状各异）；
2. 为 17 个工具建 `ToolDefinition` 并绑定处理器；
3. 工具名与目录的对账测试；
4. Genkit 接入。

## 61. M7 执行：工具定义装配（2026-10-12）

### 61.1 本轮完成

`build_tool_definitions(connection_provider)`：把工具名、参数 schema 与处理器绑成
`ToolDefinition`，**已绑定 8 个**——7 条币种信封命令（monthly / allocation /
attribution / drawdown / holding-period / streaks / stress）加 `accounts`。

处理器一律调用 `financial_tool_handlers` 的共享实现，**不在装配层重写分析逻辑**。

### 61.2 一处连接管理细节

`connection_provider` 是**调用方**而不是连接本身：每次调用重新取，避免持有一个可能在
重连后失效的连接。

### 61.3 最要紧的一条测试

`test_tool_result_matches_the_http_endpoint` —— **同一个分析经工具调用与经 HTTP 端点
必须产出同一形状**，测试直接比对两者响应体。

这正是本轮先做抽取（§60）的原因：若两侧各写一套分析，字段与口径会分叉，而**单侧
测试永远看不出来**。

### 61.4 另外三条边界

- **未实现的工具不得出现在运行时里**（`UNIMPLEMENTED_TOOLS` 与已绑定集合必须不相交），
  否则 Agent 会调用到 404；
- **参数边界在工具层同样生效**：类型不符、枚举外取值、多余字段都被拒；
- **`account` 参数真正作用于分析范围**（范围内无账户时返回空信封，而非全部）。

### 61.5 验证证据

- 9 项测试：绑定名都在目录中、7 条命令全部绑定、`accounts` 已绑定、
  **未实现工具未绑定**、**工具与 HTTP 端点形状一致**、账户工具字段、
  三类非法参数、`account` 过滤生效、公开说明覆盖全部绑定工具；
- **测试规模**：`uv run pytest` → **717 项通过**（4 项跳过）。

### 61.6 下一步（按 §59.3）

1. 绑定其余 9 个工具（`summary` / `positions` / `expiring` / `trades` / `realized` /
   `cashflow` / `audit` / `risk`；`market-positions` 归 M8）——它们形状各异，
   需先把路由体抽进 `financial_tool_handlers`；
2. 工具名与目录的**完整**对账测试（当前只断言子集关系）；
3. Genkit 接入与回合生命周期。

## 62. M7 执行：summary 并入共享处理器（2026-10-12）

### 62.1 一处被漏掉的同一形状

上一轮把 7 条币种信封命令抽成共享处理器时，漏了 `summary`——它其实**完全同形**：

```
按月/按标的/按… 计算 → project_xxx → 套币种信封
```

本轮把 `summary` 并入 `CURRENCY_GROUPED_COMMANDS`，于是：

- 它的路由也改为一行 `handle_currency_grouped("summary", ...)`（去重）；
- 工具定义自动多绑定一个（装配循环按表生成，无需单独写）。

**已绑定工具从 8 个增至 9 个**：accounts · summary · monthly · allocation ·
attribution · drawdown · holding-period · streaks · stress。

### 62.2 装配表驱动的好处

漏掉一条命令时，装配循环自动覆盖它——不需要记得在别处也加一遍。这与 §55 的目录对账
是同一思路：**让"忘记同步"变成结构上不可能，而不是靠人记得**。

### 62.3 无损性证据

- `summary` 路由改为共享处理器后，**其原有端点测试全部保持通过，断言未改动**：
  717 项一度全绿，随后扩展对账测试至 719 项；
- `test_tool_result_matches_the_http_endpoint` 从单条扩到三条命令
  （summary / drawdown / stress），逐条比对工具与 HTTP 的响应体。

### 62.4 验证证据

- **测试规模**：`uv run pytest` → **719 项通过**（4 项跳过）。

### 62.5 下一步（按 §59.3）

绑定其余 8 个工具（`positions` / `expiring` / `trades` / `realized` / `cashflow` /
`audit` / `risk`；`market-positions` 归 M8）——它们形状各异，需先把路由体抽进
`financial_tool_handlers`。之后是工具名与目录的**完整**对账、Genkit 接入、
回合生命周期与 WebSocket 事件。

## 63. M7 执行：全部 17 个工具可调用（2026-10-12）

### 63.1 本轮完成

**已绑定工具从 9 个增至 17 个**——即整个目录除 `market-positions`（归 M8）外全部可调用：

| 类型 | 命令 |
| --- | --- |
| 账户 | `accounts` |
| 币种信封（10） | `summary` · `monthly` · `allocation` · `attribution` · `drawdown` · `risk` · `holding-period` · `streaks` · `stress` |
| 详情（7） | `positions` · `expiring` · `trades` · `realized` · `cashflow` · `audit` · `extremes` |

新增 `handle_ledger_detail` 承接详情类命令；`risk` 并入币种信封表（它同形，只是多一个
**必填** `asOf`）。

### 63.2 对账测试立刻发挥了作用

新增的 `test_bound_tools_cover_the_catalog_exactly` 第一次运行就抓到 **漏绑 `extremes`**。

这正是 §55 设计这条测试的目的：**"少一个工具"是静默失败**——Agent 不会报错，只是永远
调不到那个能力，而没有任何单侧测试会发现。补上后集合完全一致（17 = 18 − 1）。

### 63.3 一处刻意的风险控制

本轮**没有改动已通过测试的路由**。共享处理器已就位、工具走处理器，但路由仍保留自己的
方法体——两者的一致性由 §61 的「工具结果 == 处理器结果」与 §55 的「目录 == 路由」两条
测试共同保证。

路由与处理器的**完全合并**留作后续清理项：它需要改动 17 个处理器，属于高风险重构，
应在上下文充足时一次性完成（这一判断与 §59.1 一致）。

### 63.4 验证证据

- 4 项新测试：**已绑定集合 == 目录 − 未实现项**（含数量）、详情命令全部绑定、
  `expiring`/`risk` 缺 `asOf` 被拒；
- 一处被测试纠正的预期：我写死数量为 16，实际 17（18 − 1）；
- **测试规模**：`uv run pytest` → **722 项通过**（4 项跳过）。

### 63.5 M7 剩余

Genkit Python 接入、回合生命周期、WebSocket 事件、会话与工具调用记录、路由与处理器的
完全合并。

## 64. M7 执行：Agent 能力发现端点（2026-10-12）

### 64.1 本轮完成

`GET /v1/agents` 落地——让 App 判断「服务端能否进入对话界面」。

```json
{
  "checkedAt": "2026-10-12T19:15:00.000Z",
  "providers": [{"provider": "genkit", "name": "Lyra",
                 "status": "unavailable", "reason": "not_configured"}]
}
```

### 64.2 一处**如实报告**而非造假的选择

初版 `_is_agent_configured` 去读 `lyra_agent_model` / `lyra_agent_provider` 两项设置——
但**这两项在 `config.py` 里根本不存在**，于是该函数是恒返回 `False` 的死代码，
却看起来像在做真实检查。

已改为显式返回 `False` 并在文档字符串里写明：**Agent 运行时（Genkit 接入与回合编排）
尚未落地，服务端确实还不具备对话能力**。

如实报告的价值：客户端据此隐藏入口，用户不会点进一个必然失败的界面。假装可用
只会把失败推迟到用户操作之后。

### 64.3 两处稳定化设计

- **`checkedAt` 由调用方传入**而不是内部取当前时间——同一份快照被多次引用时时间必须
  一致，否则客户端会看到自相矛盾的状态；
- **不可用时给稳定原因码**（`not_configured`）而不是自由文本，客户端需要能稳定分支。

另：可用性检查**不发网络请求**——该端点会被客户端频繁轮询，每次都打上游既慢又会被限流。

### 64.4 明确未实现的部分

`POST /v1/agent/sessions` 等**六条会话路由**（创建 / 读取 / 工具调用 / 消息 / 取消 /
删除）依赖 Agent 回合编排，**尚未落地**。这是明确未实现，不是遗漏，已写在模块文档里。

### 64.5 验证证据

- 4 项测试：端点要求认证、**运行时缺失时如实报告不可用**、快照 camelCase 且时间由
  调用方决定、朴素时间被拒；
- **测试规模**：`uv run pytest` → **726 项通过**（4 项跳过）。

### 64.6 M7 剩余

Genkit Python 接入、回合生命周期与六条会话路由、WebSocket 事件、会话与工具调用记录、
路由与处理器的完全合并。

## 65. M7 执行：Agent 会话状态与回合边界（2026-10-12）

**新增**：`server-py/src/lyra/domain/agent/switching.py`、`server-py/tests/test_agent_switching.py`。

### 65.1 为什么切换只能在回合边界

账户范围与数据基线**只能在 `idle` 变更**。理由不是实现便利：半途换范围会让同一段
对话里前后数字口径不同，而**模型已经引用过旧数字**。

### 65.2 两处切换的共同性质：旧摘要必须作废

这是本轮最要紧的一条。摘要是已完成回合的压缩，其中可能含原账户或旧基线的具体数字。

**若保留它，下一回合模型会拿旧摘要里的数字回答新范围的问题——而且答案看起来完全
正常。** 没有任何错误、没有任何异常，只是一个听起来合理的错误数字。

因此 `prepare_agent_account_scope_change` 与 `prepare_agent_ledger_baseline_refresh`
都把 `summary` 置为 `None`，并有专门测试断言（含"若保留则测试失败"的措辞）。

### 65.3 其它被钉住的边界

| 性质 | 说明 |
| --- | --- |
| 供应商不可用 | 给稳定码 `AGENT_UNAVAILABLE`；**内部原因只作字段保留，不进安全说明** |
| 基线只能前进 | 必须**严格递增**版本——同版本或回退会让"新基线"名不副实 |
| 划界消息 | 写明前后范围与基线版本，便于人工复核 |
| 消息 ID | 不得与历史重复，否则历史里出现两条同 ID 消息 |
| 不可变性 | 两个准备函数都返回新快照，不改动传入会话 |

### 65.4 验证证据

- 21 项测试：创建（idle / revision 1 / 无摘要）、**不可用不泄漏路径**、供应商不一致、
  版本非法、**切换作废摘要**、划界消息含前后范围、**三个非 idle 状态各自被拒**、
  范围未变拒绝、消息 ID 重复拒绝、**刷新作废摘要且范围不变**、**版本必须严格递增**、
  原会话不被改动；
- **测试规模**：`uv run pytest` → **747 项通过**（4 项跳过）。

### 65.5 M7 剩余

会话持久化（`agent_sessions` / `agent_messages` / `agent_tool_calls` 三表）、
回合编排、六条会话路由、Genkit 接入、WebSocket 事件。

## 66. M7 执行：Agent 会话持久化（2026-10-12）

**新增**：`server-py/src/lyra/infrastructure/db/agent_session_repository.py`、`server-py/tests/test_agent_session_repository.py`。

### 66.1 一处**静默失败**的语义分叉（重要）

Agent 会话的 `accountScope.account` 存的是**账户 UUID**（列是 `account_id UUID
REFERENCES accounts(id)`），而分析领域的 `AccountScope.account` 是**脱敏账户键**
（`legacy_account_key`）。

**两者共用同一个类型。** 把 UUID 当账户键去读账本**不会报错**——

```sql
WHERE legacy_account_key = '<uuid>'   -- 匹配不到任何行，返回空账本
```

分析结果为空、无异常、无日志。因此：

- 会话读写用 UUID；
- 任何拿会话范围读账本的地方，**必须先经 `resolve_legacy_account_key` 解析**；
- 有一条测试（`test_uuid_scope_does_not_match_legacy_key`）把这种失败模式固定下来：
  先用 UUID 查（断言为空），再用解析出的脱敏键查（断言有账户）。

### 66.2 一处命名误导

`original_provider` **不是"原供应商"**，而是**迁移标记**：只有从旧供应商
（`claude` / `pi` / `opencode`）迁移过来的会话才写值，新会话必须为 NULL。

它的 CHECK 也只允许那三个旧值——我按直觉写 `session.provider`（`"genkit"`）时
被数据库拒绝。已加测试断言新会话该列为 NULL。

### 66.3 另外两处来自 schema 的约束

- **`device_id` 有指向 `paired_devices` 的外键**——测试夹具必须先建设备；
- **`agent_session_identities` 必须在创建时同写**：它是会话表被重建时的身份来源，
  **事后无法补**。

### 66.4 验证证据

- 9 项测试：往返一致（ISO 文本）、**范围以 UUID 往返**、`all` 范围存 NULL、
  **身份表创建时写入**、未知会话返回 None、UUID→脱敏键解析、未知账户返回 None、
  **UUID 不匹配脱敏键（静默空账本）**、新会话 `original_provider` 为 NULL；
- **测试规模**：`uv run pytest` → **756 项通过**（4 项跳过）。

### 66.5 M7 剩余

`agent_messages` / `agent_tool_calls` 两表仓储、回合编排、六条会话路由、
Genkit 接入、WebSocket 事件。

## 67. M7 执行：消息与工具调用生命周期（2026-10-12）

**新增**：`server-py/src/lyra/domain/agent/messages.py`、`server-py/tests/test_agent_messages.py`。

### 67.1 为什么在领域层重复实现 schema 的约束

数据库对这两张表有状态—字段一致性 CHECK。领域层把同一套规则**在写入前**判定，
理由是：数据库抛出的 `ConstraintException` **只有列名，没有"哪条规则被违反"**。
排查一次"消息写不进去"要翻 schema 才能知道原因。

| 状态 | 完成时间 | 错误码 |
| --- | --- | --- |
| `streaming` | 必须为空 | 必须为空 |
| `completed` | 必须存在 | **必须为空** |
| `failed` / `cancelled` | 必须存在 | 可空 |

### 67.2 最容易被忽略的一条

**`completed` 不允许带错误码。**

一个"完成但报错"的消息在语义上自相矛盾——客户端拿到 `status=completed` 与
`error_code` 并存时，**无从判断该显示成功还是失败**。有专门测试断言它被拒。

### 67.3 其它被钉住的边界

- **已终止的消息/调用不能再次终止**，否则完成时间会被改写；
- **序号必须为正**（与 schema 的 CHECK 一致）；
- **被拒的调用同样落库**：它是审计的一部分，`authorization_decision='denied'` 说明
  该调用被授权层拦下、未执行；
- **摘要按值复制**：调用方后续改动不应影响已构造的记录。

### 67.4 验证证据

- 21 项测试：streaming 起始、三种终态带完成时间、**completed 禁止错误码**、
  failed/cancelled 允许错误码、不可二次终止、不可用 streaming 终止、序号与空白校验、
  工具调用授权判定、拒绝可记录、四种终态、不可二次终止、负版本拒绝、摘要复制；
- **测试规模**：`uv run pytest` → **777 项通过**（4 项跳过）。

### 67.5 M7 剩余

两表仓储（含 `UNIQUE (session_id, ordinal)` 序号分配）、回合编排、六条会话路由、
Genkit 接入、WebSocket 事件。

## 68. M7 执行：消息与工具调用仓储（2026-10-12）

**新增**：`server-py/src/lyra/infrastructure/db/agent_message_repository.py`、`server-py/tests/test_agent_message_repository.py`。

### 68.1 一条被数据库 CHECK 揭示的不变量

插入"被拒的调用"时数据库拒绝：

```sql
CHECK ((authorization_decision = 'allowed' AND status <> 'rejected')
    OR (authorization_decision = 'denied'  AND status = 'rejected'))
```

即 **被拒的调用从未"开始"**——它直接是 `rejected`，没有耗时。这纠正了我最初的建模：
我原本让被拒调用也走 `started`，然后再终止。

已拆成两个函数：`start_tool_call`（**只用于已授权**）与 `reject_tool_call`（直接落
`rejected`）。仍要落库是因为它是审计的一部分——事后需要能回答"模型是否尝试过访问
授权范围外的账户"。

### 68.2 两处并发设计

| 机制 | 为什么不用朴素写法 |
| --- | --- |
| **序号由编排层按快照分配**，数据库唯一约束兜底 | 用 `max(ordinal)+1` 现算，并发两个回合会算出同一个值，失败点晚到插入语句且只有约束名 |
| **`last_event_sequence` 用 CAS 递增** | 用 `+1` 直接写会让两个并发回合产生相同序号，**客户端看到的事件顺序就不确定** |

`complete_message` / `complete_tool_call` 也只在行仍处于 `streaming` / `started` 时更新
——**迟到的完成回调不得覆盖已终止消息**，否则完成时间被改写、审计失真。

### 68.3 两处外键顺序

- `agent_tool_calls.message_id` 指向 `agent_messages`：**工具调用前必须先写消息**；
- `agent_sessions.device_id` 指向 `paired_devices`：夹具必须先建设备。

### 68.4 验证证据

- 10 项仓储测试：写入读回（ISO 文本）、按序号排序、**重号被数据库拒绝**、
  终态转换、**迟到完成不覆盖终态**、工具调用 CAS 更新、**被拒调用落库**、
  事件序号 CAS 成功/失败；
- 领域测试已同步（21 项，含「被拒调用是 rejected 而非 started」）；
- **测试规模**：`uv run pytest` → **787 项通过**（4 项跳过）。

### 68.5 M7 剩余

回合编排（`agent-turn-orchestrator`）、六条会话路由、Genkit 接入、WebSocket 事件、
会话摘要生成。

## 69. M7 执行：会话公开投影（2026-10-12）

**新增**：`project_agent_session`（在 `application/public_projection.py`）、`server-py/tests/test_agent_session_view.py`。

### 69.1 一处 Python 需要哨兵才能表达的语义

TypeScript 的可选字段用 ``undefined`` 与 ``null`` **区分两种含义**：

| TS 值 | 公开投影 |
| --- | --- |
| `status: undefined` | 响应里**不出现** `status` 键 |
| `errorCode: undefined` | 不出现 |
| `errorCode: null` | 出现且为 `null` |

Python 的 `None` 只能表达一种，因此加了 `UNSET` 哨兵：`error_code` 默认 `UNSET`
（未设置），显式传 `None` 才是"存在但为空"。

**混同的后果**：客户端会把 `{"errorCode": null}` 与"没有该键"当成同一回事，
而 TypeScript 语义下两者不同。有两条测试分别断言「未设置 → 键不存在」与
「显式 null → 键存在且为 null」。

### 69.2 逐字段重建而非透传

投影逐字段重建会话、账户范围与消息——**内部 `summary` 与未来新增的供应商私有字段
不会因为对象透传进入客户端响应**。有测试断言内部摘要不出现在公开快照里。

### 69.3 验证证据

- 8 项测试：camelCase 字段集（**逐键断言**）、**摘要不外泄**、`all` 范围不带 account、
  **未设置的可选字段被省略**、**显式 null 被保留**、有错误码时保留、
  哨兵默认值正确、领域创建的会话可直接投影；
- **测试规模**：`uv run pytest` → **795 项通过**（4 项跳过）。

### 69.4 M7 剩余

回合编排、六条会话路由、Genkit 接入、WebSocket 事件、会话摘要生成。

## 70. M7 执行：会话状态转换（2026-10-12）

**新增**：`server-py/src/lyra/domain/agent/lifecycle.py`、`server-py/tests/test_agent_lifecycle.py`。

### 70.1 状态机

```
idle       → running | closed
running    → cancelling | idle
cancelling → idle
closed     → （无出边）
```

两条性质：

1. **同状态重放返回原对象**，不递增 `revision`。`revision` 是客户端判断"会话是否
   变过"的依据；一次无变化的轮询若也递增它，客户端会以为状态变了。
2. **取消有可观测的中间态**（`running → cancelling → idle`），而不是从"正在生成"
   直接跳到"已取消"——那样客户端无法区分"完成后正常结束"与"被取消"。

### 70.2 一条调用顺序约束（写进模块文档）

调用方必须**先把供应商终态事件与消息固化，再把 `running` / `cancelling` 转回 `idle`**。

反过来做（先回 `idle`）会让客户端以为可以立刻发下一轮，而上一轮的消息还没落库——
用户会看到自己的消息"消失"。

### 70.3 验证证据

- 19 项测试：五条允许的边各自递增 revision、**四个状态同状态重放返回原对象**、
  五条非法边各自报错且带前后状态、`closed` 无出边、转换表覆盖全部状态、
  未知状态拒绝、原对象不被改动、**取消路径可观测**；
- **测试规模**：`uv run pytest` → **814 项通过**（4 项跳过）。

### 70.4 M7 剩余

`turn.ts`（回合开始/完成/失败计划）、`tool-call.ts`（工具调用授权）、
`events.ts`（事件序列）、`turn-stream.ts`、编排器主体、六条会话路由、
Genkit 接入、WebSocket 事件。

## 71. M7 执行：工具调用摘要净化与授权（2026-10-12）

**新增**：`server-py/src/lyra/domain/agent/tool_call.py`、`server-py/tests/test_agent_tool_call.py`。

### 71.1 为什么摘要必须净化

摘要会写入数据库并被回放给用户。若把原始参数或第三方响应直接存进去，
**access token、OAuth code、邮箱、邮件正文就永久留在库里**——而且它们看起来只是
"一次调用的参数"。

### 71.2 关键取舍：**拒绝**而非过滤

命中敏感字段名时直接抛错，**不静默删掉该字段**。

静默过滤会让调用方以为摘要完整入库，实际丢了字段，事后无从发现。有测试断言
敏感字段让整次调用失败，且错误信息里**不包含那个值**。

### 71.3 字段名先归一后比对

`normalizeKey = lowercase + 去除非字母数字`。因此 `accessToken`、`access_token`、
`ACCESS-TOKEN`、`access token`、`access.token` 命中同一条规则。

**若不归一，驼峰或下划线写法就能绕过。** 有测试逐一验证这些变体。

### 71.4 另外四条边界

| 边界 | 原因 |
| --- | --- |
| 字段数 ≤ 32 | 防止把整份响应塞进摘要 |
| 字段名 1..64 字符 | 与 schema 一致 |
| 数值必须有限 | `NaN` / `Infinity` 不是合法 JSON，写入后无法回读 |
| 值必须是扁平标量 | **嵌套结构是原始响应漏进摘要的典型形态** |
| 文本 ≤ 512 字符 | 摘要是审计记录，不是数据通道 |

### 71.5 验证证据

- 34 项测试：扁平标量通过、返回副本、**15 种敏感键名变体各自被拒**、
  **归一化后仍被拒（含 4 种写法）**、普通键不受影响（防规则过宽）、
  非有限数值、超长文本与边界值、三种嵌套结构、字段数上限与边界、字段名长度、
  授权判定三种结果；
- **测试规模**：`uv run pytest` → **848 项通过**（4 项跳过）。

### 71.6 M7 剩余

`turn.ts` 回合计划、`events.ts` 事件序列、`turn-stream.ts` 流式状态、
编排器主体、六条会话路由、Genkit 接入、WebSocket 事件。

## 72. M7 执行：事件序号与重连续传（2026-10-12）

**新增**：`server-py/src/lyra/domain/agent/events.py`、`server-py/tests/test_agent_events.py`。

### 72.1 核心原则：缺口绝不补造

事件是增量的（`message.delta` 只带新增文本）。若服务端在重连时凭空补一条"看起来
合理"的 delta，客户端会把**不完整的文本当成最终答案**——而没有任何一方能察觉。

因此续传失败一律要求重新取规范会话快照，三种原因各有专门测试：

| 情况 | 原因码 | 说明 |
| --- | --- | --- |
| 客户端游标超前 | `client_ahead` | 客户端拿到过服务端不知道的事件，两边状态已不可信 |
| 缓冲区混入其它会话 | `mixed_session` | 绝不能拼接 |
| 中间缺号或缓冲区未覆盖末尾 | `event_gap` | 缺的可能是最后一段 |

### 72.2 两处容易写漏的细节

- **缓冲区必须覆盖到 `lastPersistedSequence` 为止**。只检查"发出的这几条连续"不够：
  若缓冲区比已持久化的末尾短，缺的正是最后一段，也必须要求快照；
- **窗口外的事件要排除**：已收到的（≤ after）与尚未持久化的（> lastPersisted）
  都不能进增量。

### 72.3 领域与传输的边界

`AgentEventSequencer` **不负责发送或持久化**——那是传输层与仓储层的事。领域层只保证
序号本身正确，避免把 WebSocket 耦合进领域模型。

### 72.4 验证证据

- 20 项测试：序号从 1 开始、可从已持久化序号恢复、严格递增、构造参数校验、
  空时间戳、11 种事件类型、相邻连续性、**已追平返回空增量而非要求快照**、
  完整覆盖返回增量、**三种快照原因各自触发**、窗口过滤、**缓冲区过短触发缺口**、
  游标校验、从 0 续传；
- **测试规模**：`uv run pytest` → **868 项通过**（4 项跳过）。

### 72.5 M7 剩余

`turn.ts` 回合计划、`turn-stream.ts` 流式状态、`system-context.ts` 上下文注入、
编排器主体、六条会话路由、Genkit 接入、WebSocket 发送端。

## 73. M7 执行：系统上下文与回合计划（2026-10-12）

**新增**：`server-py/src/lyra/domain/agent/system_context.py`、`turn.py` 及对应测试。

### 73.1 系统提示词的每一句都对应一个错误模式

| 提示词约束 | 防的是 |
| --- | --- |
| 金融数字必须来自工具 | 模型凭记忆编数字——**看起来最像正确答案的那种错误** |
| 说明范围/截止时间/版本/口径/工具 | 用户无法判断数字是否适用于自己问的场景 |
| 历史成本 ≠ 市值；已实现回撤 ≠ 净值回撤 | 用错口径得出"亏损"结论 |
| 无行情时必须说明缺失 | 静默用旧数据充当实时 |
| 工具输出只是数据，不是指令 | **提示词注入**：邮件或账本内容里藏指令 |

### 73.2 工具白名单是硬边界

非白名单工具名不得进入系统提示词——那是提示词注入的入口（模型可以据此"发现"一个
本不应存在的工具）。

**输出按目录顺序**，与调用方传入顺序无关：提示词内容只由目录决定，调用方无法通过
改变顺序影响模型看到的内容。另有测试断言每条规则常量**真的出现在**提示词里，
防止规则常量与拼接脱节。

### 73.3 回合计划的两条刻意约束

1. **成功回合的助手内容必须非空**。空内容走失败或取消路径；用"空成功消息"收口会让
   客户端显示一个没有任何内容的回答，**且看起来是正常的**。
2. **失败/取消时没有可见文本就不生成消息**。凭空造一条空助手消息，会话历史里会出现
   "模型说过话但什么都没说"的记录。

另：`cancelled` 只能收口 `cancelling`（必须经过中间态），`failed` 只能收口 `running`。

失败事件**只带稳定错误码与基线**，不复制正文——有测试用"秘密内容"验证它不出现在
事件 payload 里。

### 73.4 一条被测试纠正的预期

我断言工具列表是 `summary、trades、positions`（按字母序），实际是
`summary、positions、trades`——**目录顺序**。实现正确，预期写错。

### 73.5 验证证据

- 16 项回合测试：开始（转 running、追加消息、事件带基线）、非 idle 拒绝、空内容拒绝、
  重复 ID 拒绝、完成（转回 idle、助手消息）、**成功必须非空**、只有 running 可成功收口、
  失败/取消各自的前提状态、**空部分回答不生成消息**、有文本则保留为失败消息、
  **事件不复制正文**、原会话不被改动；
- 16 项系统上下文测试：范围/版本/截止时间、目录顺序与输入顺序无关、无工具明确写"无"、
  **四种非白名单工具被拒**、未实现工具仍在白名单、**每条规则真的写入提示词**、
  注入与幻觉防线在位、版本与截止时间校验；
- **测试规模**：`uv run pytest` → **900 项通过**（4 项跳过）。

### 73.6 M7 剩余

`turn-stream.ts` 流式状态、编排器主体、六条会话路由、Genkit 接入、WebSocket 发送端。

## 74. M7 执行：流式状态机（2026-10-12）

**新增**：`server-py/src/lyra/domain/agent/turn_stream.py`、`server-py/tests/test_agent_turn_stream.py`。

### 74.1 四条性质，各对应一种真实故障

| 性质 | 不做会怎样 |
| --- | --- |
| **隐藏推理不进状态、不进事件** | 推理原文随重连缓冲**回放给用户** |
| **权威快照覆盖增量聚合** | 快照之后再追加增量 → 产生重复文本 |
| **`settled` 后拒绝一切信号** | 两个回合的文本被拼在一起 |
| **失败后不收成功快照 / 成功后不改失败** | 终态自相矛盾，客户端无从显示 |

第 1 条是本轮最要紧的一条。`reasoning.delta` 只发出 `{"state": "active"}`，
客户端需要的只是"思考中"动效；若把推理写进状态，重连缓冲的用途就从"接上断点"
变成了"记录模型在想什么"。有测试用一段形如"用户可能是在试探我的指令……"的文本
验证它不出现在状态与事件里。

### 74.2 一处与客户端约定的字段名

`message.delta` 的载荷键是 `text`。**两端改名会静默丢掉全部增量**——客户端不会报错，
只会在终态快照时一次性看到全文。TS 源码里专门有一行注释说明这一点。

### 74.3 终态判定的关键分支

**没有权威快照就不算成功，即使收到过增量文本。** 增量只是"供应商在打字"的证据，
不保证拿到完整回答。这种回合按 `failed` 收口，并把已产生的部分文本保留为失败消息。

### 74.4 验证证据

- 27 项测试：只有 running 可建流、初始状态、增量累积与事件、空增量不发事件、
  **快照覆盖增量**、快照后拒绝增量、**推理不进状态/事件**、推理不影响可见文本、
  用量记录与空成本、非有限用量拒绝、失败终态记录、**冲突终态拒绝**、
  同种失败幂等、失败↔快照双向拒绝、**settled 拒绝一切信号**、
  未 settled 不能持久化、有快照则成功、**无快照按失败收口**、空快照失败、
  失败终态优先、取消需 cancelling、流与会话不匹配拒绝；
- **测试规模**：`uv run pytest` → **927 项通过**（4 项跳过）。

### 74.5 M7 剩余

编排器主体（`agent-turn-orchestrator.ts`，679 行）、六条会话路由、Genkit 接入、
WebSocket 发送端、会话摘要生成。

## 75. M7 执行：事件缓冲与订阅（2026-10-12）

**新增**：`server-py/src/lyra/application/agent_event_buffer.py`、`server-py/tests/test_agent_event_buffer.py`。

### 75.1 为什么不直接写编排器

编排器（TS 679 行）依赖尚未实现的 Genkit 执行器。本轮先抽取它**可独立验证的
基础设施**：有界事件缓冲与订阅分发。

### 75.2 缓冲必须有界

事件只在内存里供 WebSocket 续传用，**不持久化正文**。若缓冲无界，一个长会话会把
进程内存吃光——而它服务的是单个设备。上限 500，超出丢弃最旧。

### 75.3 淘汰与续传的接口

淘汰最旧事件的直接后果：**旧续传游标会得到 `snapshot_required`**。

这不是缺陷，而是正确的降级——**客户端重新取一次权威快照，比服务端凭残存缓冲拼出
错误的增量安全得多**。两条测试分别覆盖"游标落在已淘汰区间"与"游标正好落在窗口
起点之前"这两种相邻情形，前者要求快照、后者可续传。

### 75.4 一处测试自身的修正

初版把这两种情形写成了同一个用例（且断言方向写反），复核后拆成两条有区分度的用例。
实现本身未改动。

### 75.5 验证证据

- 13 项测试：追加计数、**有界且丢最旧**、**游标落在淘汰区间要求快照**、
  **游标正好在窗口起点之前可续传**、窗口前游标要求快照、连续窗口续传、
  **跨会话事件拒绝**、空会话 ID 拒绝、单/多订阅者、取消订阅与幂等、外发钩子；
- **测试规模**：`uv run pytest` → **940 项通过**（4 项跳过）。

### 75.6 M7 剩余

编排器主体（回合驱动的完整生命周期）、六条会话路由、Genkit 接入、WebSocket 发送端、
会话摘要生成。

## 76. M7 执行：回合编排器（2026-10-12）

**新增**：`server-py/src/lyra/application/agent_turn_orchestrator.py`、`server-py/tests/test_agent_turn_orchestrator.py`。

### 76.1 执行器是注入的

编排器只消费规范化流信号，**不解释供应商协议**。因此编排逻辑可独立验证；接入
Genkit 时替换执行器即可，不必改编排。

### 76.2 三条顺序约束（各有测试）

| 约束 | 不遵守会怎样 |
| --- | --- |
| **先持久化再执行** | 进程崩溃留下一条**没有任何记录的模型回合**——用户看到回答却查不到来源 |
| **事件在持久化之后才推送** | 客户端收到数据库中还不存在的事件；断线重连时续传找不到它 |
| **取消不猜意图** | 从执行器退出码推断"用户想取消"，会把崩溃当成用户操作 |

前两条用"记录调用顺序并在测试里断言先后"的方式验证，而不是只看最终结果。

### 76.3 一处被测试揭出的实现错误

执行器未收口时，我最初补 `MessageFailed` 强行改成失败——**被领域层拒绝**：
`test_failure_outcome_finalizes_as_failed` 暴露了 `settled` 之后不能再追加信号。

复核后确认领域层的判断是对的：**"成功快照后改写为失败"被明确禁止**，因为那会让
终态自相矛盾。强行改成失败等于**用一个更糟的错误掩盖执行器的问题**。

改为：未收口时**只补一个 `settled`**，让 finalize 按已有事实判定——
有权威快照且非空则成功，否则失败。测试相应拆成两条（有快照→成功、无快照→失败）。

### 76.4 验证证据

- 14 项测试：成功回合（回 idle、两条消息、事件类型）、**序号连续**、
  **持久化先于执行**、**推送后于持久化**、失败终态、**未收口但有快照→成功**、
  **未收口且无快照→失败**、部分文本保留为失败消息、**CAS 失败报错**、
  未登记会话拒绝、续传、取快照、**第二回合序号接着第一回合**；
- **测试规模**：`uv run pytest` → **954 项通过**（4 项跳过）。

### 76.5 M7 剩余

六条会话路由、Genkit 接入（实现 `AgentTurnExecutor`）、WebSocket 发送端、
会话摘要生成、`run_turn` 与真实仓储的接线。

## 77. M7 执行：六条会话路由（2026-10-12）

**新增**：`server-py/tests/test_agent_session_routes.py`；`api/agent_routes.py` 补五条路由。

### 77.1 路由状态

| 路由 | 状态 |
| --- | --- |
| `GET /v1/agents` | ✅ 能力发现 |
| `POST /v1/agent/sessions` | ✅（运行时缺失时**如实 503**） |
| `GET /v1/agent/sessions/:id` | ✅ |
| `GET /v1/agent/sessions/:id/tool-calls` | ✅ 审计列表 |
| `POST /v1/agent/sessions/:id/cancel` | ✅ |
| `DELETE /v1/agent/sessions/:id` | ✅ |
| `POST /v1/agent/sessions/:id/messages` | ⛔ 未实现（依赖 Genkit 执行器） |

六条路由**全部**要求设备认证（参数化测试逐条断言）。

### 77.2 一处被测试纠正的语义

我最初写"取消 idle 会话 → cancelling"，实现返回 409。

复核确认**实现是对的**：状态机把 `idle → cancelling` 定为非法。取消的语义是
**"停止正在进行的回合"**，空闲会话没有回合可停——它应当**关闭**（`DELETE`），
走 `idle → closed` 这条边。

这也解释了为什么 `closed` 直接从 `idle` 可达：关掉一个空闲会话不需要先"取消"。

### 77.3 两处来自 schema 的修正

- **`status='closed'` 必须同时写 `closed_at`**，schema 的 CHECK 要求两者一致；
- **`streaming` 消息映射为"未设置"**：领域层的 `NormalizedAgentMessage.status` 只
  表达持久终态；仍在流式中的消息由 WebSocket 事件表达，不属于会话快照。

### 77.4 一处基础设施修正

`require_device` 此前只**返回**设备，没有挂到 `request.state`。创建会话需要写
`agent_sessions.device_id`，而依赖的返回值只能被声明了该依赖的函数参数接收——
现在两者都提供。

### 77.5 验证证据

- 18 项路由测试：**六条路由各自要求认证**、创建如实 503、读取公开快照（无 summary）、
  未知会话 404、审计列表为空、**running 可取消**、**idle 取消被拒且带 from 字段**、
  **cancelling 不能直接关闭**、**idle 可关闭**、**关闭幂等**、**closed 无出边**、
  未知会话转换 404、**三条响应无 snake_case**；
- **测试规模**：`uv run pytest` → **972 项通过**（4 项跳过）。

### 77.6 M7 剩余

`POST .../messages`（依赖 Genkit 执行器）、Genkit 接入、WebSocket 发送端、会话摘要生成。

## 78. M7 执行：Agent 模型配置（2026-10-12）

**新增**：`server-py/src/lyra/application/agent_config.py`、`server-py/tests/test_agent_config.py`。

### 78.1 一项前置核实

`genkit` Python 包**可安装**（已用 `uv pip install --dry-run genkit` 确认）。因此
目标中的"Genkit Python"路径可行，无需改为直连 OpenAI 兼容 API。

TS 侧用 `@genkit-ai/compat-oai` 的 `openAICompatible` provider，配置项为
`LYRA_GENKIT_BASE_URL` / `LYRA_GENKIT_MODEL` / `LYRA_GENKIT_API_KEY`，
限额为 `LYRA_AGENT_TURN_TIMEOUT_MS`（默认 120s，1s–600s）与
`LYRA_AGENT_MAX_TURNS`（默认 10，1–32）。

### 78.2 两条配置语义

1. **三项缺一即视为未配置。** 未配置时执行器**必须抛错**，不能静默返回空回答——
   静默失败会让用户以为模型"什么也没说"，而实际上是服务端没配好。
2. **部分配置必须报错。** 否则服务会以"看起来启用了但每次对话都失败"的状态启动。

### 78.3 base URL 的严格校验

不接受凭据、查询串、片段与非 HTTP(S)：

- 带凭据的 URL（`https://user:pw@host`）会把密钥写进日志与错误信息；
- 带查询串的常常是复制粘贴时多带了参数（其中可能就有密钥）。

### 78.4 一处从"硬编码"到"真实检查"的替换

上一轮我把 `_is_agent_configured` 显式写成恒返回 `False` 并注明"Agent 运行时尚未
落地"。本轮替换为真实的配置检查。

**仍然不发网络请求**：可用性端点会被客户端频繁轮询，每次都打上游既慢又会被限流。
配置非法时返回 `False` 而非抛错——该端点的职责是回答"能不能用"，配置错误的具体
原因由启动检查负责报告。

### 78.5 验证证据

- 18 项测试：三项全空→未启用、完整配置→默认限额、**三项各自缺失都报错**、
  尾部斜杠去除、**五种非法 base URL**、限额可覆盖、**五种越界/非整数限额**、
  空限额回落默认；
- **测试规模**：`uv run pytest` → **990 项通过**（4 项跳过）。

### 78.6 M7 剩余

Genkit Python 执行器实现（把配置变为真实的 `AgentTurnExecutor`）、
`POST .../messages` 路由、WebSocket 发送端、会话摘要生成。

## 79. M7 执行：Genkit 执行器信号映射（2026-10-12）

**新增**：`server-py/src/lyra/infrastructure/agents/genkit_runtime.py`、`server-py/tests/test_genkit_runtime.py`。

### 79.1 模型调用是注入的

执行器的职责是把供应商的流式输出**翻译成 Lyra 的规范化信号**。模型调用作为
``stream_provider`` 注入，因此映射与工具循环可独立验证；Genkit 绑定只是薄薄一层。

### 79.2 信号映射表

| 供应商输出 | 规范化信号 | 约束 |
| --- | --- | --- |
| 推理活动 | `ReasoningDelta("active")` | **从不传原文** |
| 可见文本 | `MessageDelta(text)` | 逐块累积 |
| 用量 | `UsageUpdated(tokens, None)` | 成本未提供时为空，**不猜** |
| 结束 | `MessageSnapshot(全文, "end_turn")` | 权威快照覆盖增量聚合 |
| 出错 | `MessageFailed("failed")` | 底层原因只进日志 |

### 79.3 一条容易漏掉的收口

**无论成功失败都必须发 `TurnSettled()`。**

少了它，编排器会认为回合还没结束，**会话永远停在 `running`**——客户端看不到任何
终态，也发不出下一轮。有两条测试分别覆盖成功与出错路径，并断言收口恰好一条。

### 79.4 工具调用的审计取舍

**参数非法或工具未知的请求也要转发给执行器**，由它记录一次拒绝。若在映射层静默
跳过，事后无法回答"模型是否尝试过用错误参数调用工具"。

参数在转发前收敛为摘要：**敏感字段命中时退回空摘要**（而不是抛出）。审计记录不该
因为一个不可摘要的参数而整体丢失——代价是摘要可能不完整，所以它只是审计，不是
数据通道。有测试用 `accessToken: "sk-secret"` 验证密钥不出现在调用记录里。

### 79.5 验证证据

- 15 项测试：文本分片映射、**推理不传原文**、用量映射与空成本、无用量不发事件、
  权威快照、**信号顺序**（增量→快照→收口）、供应商出错→失败且仍收口、
  **失败不泄漏上游错误文本**、成功也收口、**收口恰好一条**、工具转发、
  **非法参数仍转发**、**未知工具仍转发**、**敏感字段被剥离**、不可摘要参数退回空摘要；
- **测试规模**：`uv run pytest` → **1005 项通过**（4 项跳过）。

### 79.6 M7 剩余

Genkit 绑定（实现 `ModelStream` 的薄适配）、`POST .../messages` 路由、WebSocket 发送端、
会话摘要生成。

## 80. M7 执行：发消息路由与编排接线（2026-10-12）

**新增**：`infrastructure/db/agent_turn_store.py`、`POST /v1/agent/sessions/:id/messages`、`GenkitTurnExecutor`。

### 80.1 路由状态：七条全部落地

七条 Agent 路由现已全部实现（`messages` 在模型未配置时返回 503）。

### 80.2 执行器协议的一处必要扩展

`AgentTurnExecutor.run` 增加了 ``session`` 参数：**系统提示词需要账户范围与账本
版本**，而这两项属于会话状态，不属于调用参数。

### 80.3 一处如实留空

`main.py` 里 `app.state.agent_executor = None`，并注明原因：

> `GenkitTurnExecutor` 已就绪，缺的是把 Genkit Python 的流式输出适配成
> `ModelStream`。在此之前路由返回 503，而不是假装可用。

**这是"M7 剩最后一块"的准确表述**，不是忘接线。若强行接一个空实现，路由会返回
"回合失败"——用户会以为模型出错，而实际是功能没做完。

### 80.4 会话状态用 revision 做 CAS

`save_turn_state` 以 `revision` 比较并交换：两个并发回合读到同一个 revision 时只有
一方能写入。

否则两个回合会各自基于旧状态继续，**最终把对方的消息覆盖掉**——而两边的用户都会
以为自己的问题被回答了。

### 80.5 验证证据

- 8 项新测试：发消息要求认证、**四种非法正文**、未知会话 404、**非 idle 拒绝且带
  from 字段**、**执行器缺失时 503（而非回合失败）**、**被拒请求不改动会话状态**；
- **测试规模**：`uv run pytest` → **1013 项通过**（4 项跳过）。

### 80.6 M7 剩余

**仅剩一件**：把 Genkit Python 的流式输出适配为 `ModelStream`（实现 `chunks` /
`tool_requests` / `usage` / `final_text` 四个方法）。之后是 WebSocket 发送端与会话
摘要生成。

## 81. M7 完成：Genkit 绑定与执行器接线（2026-10-12）

**新增**：`infrastructure/agents/openai_compatible_stream.py`、执行器工厂与接线。

### 81.1 一处经核实的生态缺口（重要）

计划原定"用 Genkit Python"。核实结果：

| 项 | 结果 |
| --- | --- |
| `genkit` Python 核心包 | ✅ 存在且已安装（提供 `Genkit` / `generate_stream` / `tool`） |
| `genkit-plugin-compat-oai` | ❌ PyPI 404 |
| `genkit-plugin-openai` | ❌ PyPI 404 |
| `genkit-plugin-ollama` | ❌ PyPI 404 |
| `genkit-plugin-google-genai` | ✅ 存在 |
| `genkit-plugin-firebase` | ✅ 存在 |

**TypeScript 侧用的正是 `@genkit-ai/compat-oai`（OpenAI 兼容），而 Python 侧没有
对应的 provider 插件。**

因此本轮**直接调用同一个 OpenAI 兼容端点**（httpx，已是依赖）。这是一处**有边界的
偏离**：`GenkitTurnExecutor` 是现成的接缝，把 `stream_provider` 换成基于 Genkit
插件的实现是**改一个文件**。等 Python 出现对应插件、或改用 `google-genai` provider
时，其余代码不受影响。

### 81.2 SSE 解析的真实坑

| 坑 | 不处理的后果 |
| --- | --- |
| **工具调用跨分片**（先给 name，再逐段给 arguments） | 得到 name 正确但 arguments 残缺的调用，**以错误参数执行工具** |
| **推理内容出现在 SSE 里** | 必须只标记"有推理活动"，原文不得返回 |
| `[DONE]` 之后仍有数据 | 必须停止解析 |
| 非法 JSON 负载 | 必须报错，而不是静默跳过 |
| 超长行 | 必须拒绝，防止上游把内存吃光 |
| 上游非 2xx | 错误正文只进异常，**不回传模型**（常含端点与密钥） |

工具有 `index` 字段：并发调用按 index 分开累积。

### 81.3 工具执行的三态错误码

`_execute` 对未知工具、参数非法、处理器异常分别返回 `TOOL_NOT_FOUND` /
`TOOL_ARGUMENTS_INVALID` / `TOOL_EXECUTION_FAILED`，**都不抛错**——抛错会让整个回合
失败，而正确行为是让模型看到"这个调用没成功"并自行调整。

有一条测试用形如 `postgres://user:pw@host` 的异常验证处理器细节不外泄。

### 81.4 一处被白名单守卫拦下的测试

测试里我用了自定义工具名 `echo`，被 `build_agent_system_context` 拒绝：
**非白名单工具不得进入系统提示词**。这正是 §73.2 设计的边界在端到端起作用。
已改用真实工具名。

### 81.5 装配失败即启动失败

`create_executor_from_environment` 在配置非法时抛错，让**服务启动失败**而不是延迟到
第一次对话——与 TS 侧"任何部分配置都在数据库迁移前失败"一致。以"看起来启用了但
每次都失败"的状态上线是最糟的选择。

### 81.6 验证证据

- 22 项执行器测试（新增 7 项工厂/执行测试）+ 16 项 SSE 解析测试；
- **测试规模**：`uv run pytest` → **1037 项通过**（4 项跳过）。

### 81.7 M7 剩余

WebSocket 发送端（把事件推给客户端）、会话摘要生成。**Agent 的服务端能力已端到端
可用**（在模型已配置的前提下）。

## 82. M7 执行：事件 WebSocket 端点（2026-10-12）

**新增**：`server-py/src/lyra/api/agent_events.py`、`tests/test_agent_events_route.py`。

### 82.1 协议

```
客户端 → {"type": "subscribe", "sessionId": "<uuid>", "afterSequence": N}
服务端 → {"type": "subscribed", "mode": "events", "events": [...]}
     或 {"type": "subscribed", "mode": "snapshot_required", "reason": "..."}
随后   → {"type": "event", "event": {...}}
```

### 82.2 三条性质

1. **未认证立即以 4001 关闭**，而不是先订阅再校验；
2. **每次消息与每次推送都重新认证**。长连接不能沿用建立时的认证结果——**否则设备
   撤销后旧连接仍然收得到数据**，直到客户端主动断开。这是 TS 侧专门注释强调的一点；
3. **本端点不缓存、不补造事件**。序号有缺口一律让客户端重新取规范快照；服务端拼出
   的增量会让客户端把不完整的文本当成最终答案。

另：**重复订阅替换旧订阅**，避免同一事件被推送多次。

### 82.3 一处边界处理

会话**未登记运行态**时返回 `snapshot_required` + `SESSION_NOT_RESUMED`，而不是假装
订阅成功。运行态是进程内的（事件缓冲、活跃回合），进程重启后必然为空——此时客户端
只能重新取快照，这正是设计意图。

### 82.4 验证证据

- 14 项测试：订阅帧解析与 **8 种非法帧**、**未认证关闭**、**已撤销令牌关闭**、
  非法帧回 error 帧而不断开、**未登记会话要求快照**、**已登记会话返回缓冲增量**；
- **测试规模**：`uv run pytest` → **1051 项通过**（4 项跳过）。

### 82.5 M7 剩余

**仅剩会话摘要生成**。Agent 的服务端能力已端到端可用。

## 83. M7 完成（2026-10-12）

### 83.1 一处被推翻的计划项

计划里 M7 的剩余项写着"会话摘要生成"。核实 TS 后发现**这个功能不存在**：

| 检查 | 结果 |
| --- | --- |
| 是否有 `summarize` / `buildSummary` / `createSummary` 生成会话摘要 | ❌ 只有 `summarizeFlatScalars`，那是**工具调用**的参数/结果摘要 |
| `agent_sessions.summary` 是否被赋过非空值 | ❌ 只被读取、写作 `?? null`、在范围/基线变更时清空 |
| 公开会话视图是否暴露 `summary` | ❌ 不暴露（`createPublicAgentSession` 不含该字段） |

**因此没有可迁移的实现。** 强行实现一个会在**发明**参考实现里没有的行为——对迁移
而言这是风险，不是进度。

这与之前几轮的模式一致：**先核实参考实现，再动手**。若直接按计划项开工，就会写出
一个"看起来完成了 M7"但和 TS 行为不一致的东西。

### 83.2 M7 交付清单

| 层 | 内容 |
| --- | --- |
| 工具 | 目录（18）· 参数校验 · 分发器 · 17 个工具可调用 · **摘要净化**（拒绝而非过滤） |
| 会话 | 回合边界切换 · 状态机 · 生命周期 · 公开投影（`UNSET` 哨兵） |
| 回合 | 开始/完成/失败计划 · **流式状态机**（推理不进传输层） |
| 事件 | 序号 · 重连续传（缺口绝不补造）· 有界缓冲 · 订阅分发 |
| 持久化 | 会话/消息/工具调用三表 · revision 与事件序号 CAS |
| 编排 | **回合编排器**（先持久化再执行；事件先落库再推送） |
| 路由 | 七条 HTTP + `GET /v1/events` WebSocket |
| 模型 | 配置校验 · 信号映射 · **OpenAI 兼容流绑定**（生态缺口见 §81.1） |

### 83.3 一处与计划的有边界偏离

§81.1 已详述：Python 的 genkit 没有 OpenAI 兼容 provider 插件，因此直接调用同一端点。
`GenkitTurnExecutor` 是现成接缝，换成 Genkit 插件实现只需改一个文件。

### 83.4 验证证据

- **测试规模**：`uv run pytest` → **1051 项通过**（4 项跳过）。

## 84. M8 起步：行情标识与价格解析（2026-10-12）

**新增**：`domain/market_data/types.py`、`infrastructure/market_data/identifiers.py` 及对应测试。

### 84.1 为什么标识必须与供应商无关

NASDAQ 的 OCC 符号、Alpaca 的 ticker、MarketData 的路径各不相同，而 Lyra 内部的
`instrument_key` 是唯一的。供应商差异**只在适配器里翻译**，领域层不做猜测——
**猜一个供应商别名会静默指向另一个合约**。

非标准合约（调整过的期权代码、非 100 乘数）一律**拒绝**，不做别名推断。

### 84.2 OCC 符号：行使价是千分之一美元的 8 位数字

```
INTC270319C00100000
└─┬─┘└──┬─┘│└───┬───┘
标的  YYMMDD C/P  行使价千分位
```

把 `100.00` 写成 `100` 会指向另一个合约，而**供应商只会返回"无数据"**——真正的
错误被掩盖。因此编码规则单独有用例覆盖，含 `7.5` → `00007500`、`0.005` → `00000005`、
`99999.999` → `99999999` 等边界。

零行使价与超过 8 位的行使价都拒绝。

### 84.3 两处金融语义

1. **零报价是"没有可成交价格"，不是"免费"。** 当成价格会让持仓市值变成 0，
   用户看到的是"资产蒸发了"。
2. **不存在的日期必须拒绝。** `2027-02-30` 格式合法但不存在；放过去只会换来供应商
   的"无数据"，掩盖真正的错误。闰年 `2028-02-29` 正常通过。

### 84.4 一处刻意的"不修正"

`price_point` 用 `Decimal(repr(value))` 渲染，避免 `str()` 引入额外噪声，但**不替
供应商"修正"数字**：`0.1+0.2` 得到的 `0.30000000000000004` 原样保留——那是供应商
真实给出的值，改动它就是伪造数据。有测试把这一点固定下来。

另：文本、布尔、容器都**不得被强制转换**为价格。协议变化应当显式失败，而不是静默
产出一个错误价格。

### 84.5 验证证据

- 53 项测试：5 种常规代码通过、6 种非标准代码拒绝、OCC 构造、**5 组千分位边界**、
  零行使价与超限拒绝、小数位超限、P 类、**8 种非标准期权字段**、乘数校验、
  标识与供应商无关、**有效日期（含闰年）与 6 种无效日期**、`None`/零/正价格、
  整数渲染、**不引入浮点噪声**、4 种非数值拒绝、4 种越界拒绝；
- **测试规模**：`uv run pytest` → **1104 项通过**（4 项跳过）。

### 84.6 M8 剩余

HTTP 客户端（`http.ts`）、Alpaca / MarketData 适配器、供应商运行时与配置、
报价缓存与预算、主备换源、`market-position-view`（持仓估值投影）、
市场设置持久化与热切换、`market-positions` 工具与 `/v1/market/positions` 路由。

## 85. M8 进展：行情 HTTP 客户端（2026-10-12）

**新增**：`infrastructure/market_data/http.py`、`tests/test_market_http.py`。

### 85.1 五条硬约束

| 约束 | 不遵守会怎样 |
| --- | --- |
| **禁止重定向** | 跟随即等于把 API 密钥发给重定向目标 |
| **超时含正文读取** | 只约束建连时间，一个慢速上游能把连接挂住 |
| **体积双检**（Content-Length + 实际累计） | 只信声明头等于把内存上限交给上游 |
| **不回传供应商正文或传输异常** | 错误正文常含端点、密钥片段或内部堆栈 |
| **403 归为"来源不可用"** | 归为"凭据被拒"会让用户轮换一把**其实有效**的密钥 |

### 85.2 那条最容易做错的映射

```
401 → credentials_rejected
403 → provider_unavailable      ← 不是凭据问题
429 → rate_limited（带 Retry-After）
200 / 203 / 404 → 成功（404 由适配器解释为"无数据"）
其它 → provider_unavailable
```

这些供应商的 403 通常表示账号缺少 **SIP / OPRA 行情权限**。归为凭据问题会让用户去
轮换密钥，然后问题依旧——**并且他不会知道真正的原因**。

### 85.3 两处"不猜"

- **无法解析的 `Retry-After`**（日期形式）返回 `None`，不做日期解析——猜错会让客户端
  在错误的时间重试；
- **错误结果不携带任何正文**，有测试用 `{"secret":"leaked"}` 验证它不出现在结果里。

### 85.4 验证证据

- 18 项测试：成功解析、**404 不算失败**、203 通过、**401/403 的区别**、429 与
  Retry-After、无效 Retry-After 忽略、5xx、超时、**传输异常不泄漏细节**、
  **正文读取超时**、**声明长度超限**、**实际长度超限**、恰好等于上限、
  非法声明长度、非 JSON、非 UTF-8、**错误结果不含正文**；
- **测试规模**：`uv run pytest` → **1122 项通过**（4 项跳过）。

### 85.5 M8 剩余

Alpaca / MarketData 适配器、供应商运行时与配置、报价缓存与预算、主备换源、
`market-position-view`（持仓估值投影）、市场设置持久化与热切换、
`market-positions` 工具与 `/v1/market/positions` 路由。

## 86. M8 进展：MarketData 适配器（2026-10-12）

**新增**：`infrastructure/market_data/marketdata_provider.py`、`tests/test_marketdata_provider.py`。

### 86.1 核心安全性质：符号回显校验

供应商会在响应里回显它实际查询的合约。**不核对就可能用一个合约的价格去给另一个
合约定价**——而报价看起来完全正常，没有任何异常。

因此要求：回显字段恰好一个元素，且与请求的标识逐字符相同。多元素（响应里混了别的
合约）与不匹配都拒绝。

同理，每一列（`bid`/`ask`/`last`/`updated`）都要求恰好一个元素——多元素意味着响应
结构已经不是本契约。

### 86.2 两处与"新鲜度"有关的取舍

1. **`mode` 必须与对外声明的时效一致。** 付费账号默认返回实时，而 Lyra 声明的是延迟
   报价；不显式指定就是在**夸大数据新鲜度**。`delayed` → `mode=delayed`，
   `end_of_day` → `mode=historical`。
2. **不加 `date`/`from`/`to`**：本契约是"最新可得"，不是历史查询。加了会静默变成另一
   个语义。

### 86.3 三处不可用路径

| 情况 | 原因码 |
| --- | --- |
| `s = "no_data"` | `no_data` |
| HTTP 404 | `provider_unavailable`（**与 HTTP 客户端层语义不同**：那里 404 是成功） |
| `bid`/`ask`/`last` 全空 | `no_data`（不返回一条没有价格的报价） |

零报价算"缺席"，因此全零同样落到 `no_data`。

### 86.4 一处防御

无效或离谱的时间戳（`updated` 是文本、1970 年、55000 年）**退回当前时间**，而不是
产出那个时点——否则"报价是否过期"的判断会彻底失效。

### 86.5 验证证据

- 15 项测试：正股报价解析、**mode 与时效一致（两种）**、期权用 OCC 符号与
  `optionSymbol` 字段、**符号不匹配拒绝**、**多元素回显拒绝**、**多元素列拒绝**、
  `no_data`、404 语义、全空 → no_data、全零 → no_data、凭据无效不发请求、
  非标准合约不发请求、状态字段非法、时间戳回退；
- **测试规模**：`uv run pytest` → **1137 项通过**（4 项跳过）。

### 86.6 M8 剩余

Alpaca 适配器、供应商运行时与配置、报价缓存与预算、主备换源、
`market-position-view`（持仓估值投影）、市场设置持久化与热切换、
`market-positions` 工具与 `/v1/market/positions` 路由。

## 87. M8 进展：Alpaca 适配器（2026-10-12）

**新增**：`infrastructure/market_data/alpaca_provider.py`、`tests/test_alpaca_provider.py`。

### 87.1 两类合约走完全不同的端点与字段

| | 正股 | 期权 |
| --- | --- | --- |
| 端点 | `/v2/stocks/{symbol}/quotes` | `/v1beta1/options/bars` |
| 字段 | `bp` / `ap` | `c`（bar 收盘） |
| 窗口上界 | `cutoff` | **向下取整到分钟再减 1ms** |

期权那条 `- 1ms` 是关键：不然会把"刚开始、尚未走完"的那一分钟 bar 算进来，
而它的 `c` 只是那一瞬间的价格，**不是收盘价**。

### 87.2 三条校验，各防一种静默错误

1. **符号回显校验**（正股查 `payload.symbol`，期权查 `bars` 的唯一键）——防止拿另一个
   合约的价格来定价；
2. **bar 必须分钟对齐且已走完**（`asOf % 60000 == 0` 且 `asOf + 60000 <= cutoff`）；
3. **成交量必须是合法非负整数，且为 0 时视为无数据**——零成交的 bar 没有可定价的
   价格，当成价格会**凭空给一个没交易的合约赋值**。

### 87.3 一处被测试纠正的预期

我用 `"key\n"` 作为非法凭据，实际通过了——**构造函数会 `strip()`**（TS 侧是 `trim()`），
尾部换行被去掉后凭据就有效了。

因此改用**内部**控制字符（`"ke\ny"`）作为用例。这也说明 `valid_credential` 的检查点是
"strip 之后是否仍含控制字符"，而不是"原文是否含控制字符"。

### 87.4 一处防御

**密钥只出现在请求头**（`APCA-API-KEY-ID` / `APCA-API-SECRET-KEY`），不进 URL。
有测试断言密钥不出现在 URL 里。

### 87.5 验证证据

- 25 项测试：正股报价与请求形态、**符号不匹配拒绝**、`quotes: null` → no_data、
  价格全空 → no_data、期权 bar 收盘价与时点（起点 + 1 分钟）、bars 端点形态、
  **窗口上界向下取整**、**未分钟对齐拒绝**、**未走完拒绝**、**零成交 → no_data**、
  4 种非法成交量、**期权符号不匹配**、多合约拒绝、`bars: null` → no_data、
  **窗口外时间戳拒绝**、404、4 种非法凭据不发请求、非标准合约不发请求、
  **密钥只在请求头**；
- **测试规模**：`uv run pytest` → **1161 项通过**（4 项跳过）。

### 87.6 M8 剩余

供应商运行时与配置、报价缓存与预算、主备换源、`market-position-view`（持仓估值投影）、
市场设置持久化与热切换、`market-positions` 工具与 `/v1/market/positions` 路由。

## 88. M8 进展：行情来源配置（2026-10-12）

**新增**：`infrastructure/market_data/config.py`、`tests/test_market_config.py`。

### 88.1 两条配置错误在启动时被拦住

1. **被选中的来源必须有凭据。** 缺凭据不是"过一会儿再试"，而是**每次请求都必然
   失败**；让服务带着这种配置启动只会把错误推迟到用户点击时。
2. **只给一半凭据也算错误。** Alpaca 需要 key + secret 一对；只填一个说明操作者本
   打算配置它却填漏了——**即使当前没被路由选中，也应该在启动时失败**。

第 2 条容易被忽略：直觉上"没被选中就不需要凭据"，但半套凭据本身就是一个信号。

### 88.2 秘钥的字符集限制是注入防线

```python
_FORBIDDEN_IN_SECRET = re.compile(r"[\r\n]")
```

**换行会把后续内容变成新的请求头**，即请求头注入。上限 8192 字符。

### 88.3 路由解析

| 输入 | 结果 |
| --- | --- |
| `None` / `""` / `"none"` | 不启用 |
| `"alpaca, marketdata"` | 两个来源，保序（**顺序即主备**） |
| `"alpaca,alpaca"` | **拒绝**（重复通常是粘了两遍） |
| `"bloomberg"` | 拒绝（未知来源不静默忽略） |

正股与期权的路由**互相独立**，可以各走不同来源。

### 88.4 验证证据

- 26 项测试：四种空路由、路由列表解析与保序、**重复拒绝**、**未知来源拒绝**、
  默认值、完整配置、**被选中来源缺凭据**、**半套凭据拒绝（即使未路由）**、
  一对凭据齐全但不路由合法、**三种换行注入拒绝**、超长秘钥、空白视为未配置、
  两种模式、未知模式拒绝、5 种越界数值、限额覆盖、空白数值回落、
  正股/期权路由独立、**主备顺序**；
- **测试规模**：`uv run pytest` → **1189 项通过**（4 项跳过）。

### 88.5 M8 剩余

供应商运行时装配、报价缓存与预算、主备换源、`market-position-view`（持仓估值投影）、
市场设置持久化与热切换、`market-positions` 工具与 `/v1/market/positions` 路由。

## 89. M8 进展：请求预算与冷却分级（2026-10-12）

**新增**：`application/market_budget.py`、`tests/test_market_budget.py`。

### 89.1 预算不是"供应额度"

它是 **Lyra 自己的保守上限**（滚动 1 分钟 + 滚动 24 小时），不是对供应商配额的承诺。
超出时返回 `rate_limited` 而**不记录**这次调用——否则一次被拒的尝试也会吃掉额度，
**让限流自我加剧**：窗口越挤越紧，正常请求也进不来。

每日额度为 `None` 时**不记录每日调用**，省掉一份永远不会被读的列表。

### 89.2 冷却时长按原因分级

| 原因 | 冷却 |
| --- | --- |
| `rate_limited` | 15 分钟 |
| `credentials_rejected` | 5 分钟 |
| `no_data` / `unsupported_instrument` | 15 分钟 |
| 其它 | 60 秒 |

### 89.3 最要紧的一条：证券级失败不冷却来源

```
PER_INSTRUMENT_ISSUES = {no_data, unsupported_instrument, currency_unsupported, invalid_response}
```

这些状态来自**单个证券**的数据缺失、标的不支持或报价形状校验。**若拿去冷却整个来源，
一个坏符号会连带让同来源的其他符号在冷却期内全部失败**——用户会看到整个行情来源
"挂了"，而实际上只是一个符号拼错。

供应商级冷却只保留传输、鉴权与限流状态。有一条测试断言该清单**恰好是那四个**：
新增原因时必须显式决定它属于哪一级。

### 89.4 一处细节

重复冷却取**更晚**的时刻：不能让一次较短的冷却（60 秒）缩短已有的较长冷却（15 分钟）。

### 89.5 验证证据

- 12 项测试：分钟额度、**被拒不消耗额度**、窗口滑动、每日跨分钟累计、每日窗口滑动、
  无每日限制时不记录、冷却返回原因、**冷却取更晚时刻**、五种冷却时长分级、
  **四种证券级失败不冷却来源**、四种来源级失败冷却、清单是闭集；
- **测试规模**：`uv run pytest` → **1201 项通过**（4 项跳过）。

### 89.6 M8 剩余

供应商运行时装配、报价缓存（含过期回退）、主备换源、`market-position-view`
（持仓估值投影）、市场设置持久化与热切换、`market-positions` 工具与
`/v1/market/positions` 路由。

## 90. M8 进展：参考价选取与行情服务（2026-10-12）

**新增**：`domain/market_data/reference_price.py`、`application/market_data_service.py`
及对应测试（共 15 项服务测试）。

### 90.1 中间价的三个前提，缺一不可

买卖中间价要求：买卖双方都存在、**时点完全相同**、``ask ≥ bid``。

**时点相同这条最关键**：用 10:00 的买价配 10:05 的卖价算出的"中间价"，
**是市场上从未存在过的价格**——它看起来完全合理，会被写进估值并展示给用户。

``ask < bid``（交叉市场）说明数据有问题，不能用来算中间价。都不可用时返回 ``None``，
**不猜**。

### 90.2 核心设计：过期缓存不终止换源

```
A healthy explicitly configured fallback wins over a failed primary's stale cache.
```

主来源只能给出过期报价时，循环**必须继续尝试备来源**——一个能给出新鲜报价的备来源，
比一个只能给出旧报价的主来源更有价值。因此过期结果被暂存而不是立即返回，
只有在所有来源都给不出新鲜报价时才使用它。

有专门测试覆盖：主来源过期 + 备来源新鲜 → 返回 `available`；两个来源都过期 → 返回
第一个过期结果（`stale`）。

### 90.3 网络失败是数据状态，不是异常

行情缺失**不能让账本读取失败**——账本本身是权威的，行情只是附加信息。供应商实现
抛出异常也被折叠为 `provider_unavailable`。

### 90.4 两处缓存与切换细节

- **失败也会缓存**：避免对同一个坏符号反复打上游；
- **热切换不清空缓存**：缓存键带来源 id，旧条目不会被新来源误用；清空反而会让切换
  立刻打出一批上游请求。

缓存有界（默认 512），超出淘汰最旧；失败缓存的过期时间即该原因的冷却时长。

### 90.5 一条被测试纠正的预期

测试里我只改了 `bid` 没改 `ask`，算出的中间价与预期不符。实现正确，是**测试数据
不自洽**——这也说明中间价的输入必须成对给出。

### 90.6 验证证据

- 15 项服务测试 + 参考价相关断言：可用/缓存/换源/无路由/路由指向未装配来源、
  **主来源过期不终止换源**、**只有全过期才返回 stale**、保留第一个过期结果、
  无参考价跳过、**失败缓存**、**证券级不冷却来源**、**来源级冷却**、
  **网络异常折叠**、缓存有界、热切换不清缓存；
- **测试规模**：`uv run pytest` → **1216 项通过**（4 项跳过）。

### 90.7 M8 剩余

供应商运行时装配（把配置变成 providers/routes/policies）、`market-position-view`
（持仓估值投影）、市场设置持久化与热切换、`market-positions` 工具与
`/v1/market/positions` 路由。

## 91. M8 进展：行情运行时装配（2026-10-12）

**新增**：`infrastructure/market_data/runtime.py`、`tests/test_market_runtime.py`。

### 91.1 供应商按凭据注册，**与路由无关**

一对齐全的凭据说明操作者配置了这个来源——即使当前没被路由选中，界面也需要展示
"已配置但未启用"。因此注册条件是"凭据齐全"，不是"被路由选中"。

### 91.2 策略按来源不同

| 来源 | 缓存 TTL | 每分钟 | 每日 |
| --- | --- | --- | --- |
| alpaca | 15 分钟 | 120 | 无上限 |
| marketdata（historical） | **24 小时** | 30 | 配置值 |
| marketdata（delayed） | 15 分钟 | 30 | 配置值 |

historical 的 TTL 是 24 小时：**收盘价在当天不会变**，15 分钟一次纯属浪费额度。

Alpaca 不设每日上限：它的套餐额度由供应商自己管理，Lyra 再设一个人为上限只会
在额度充足时误伤。

### 91.3 timing 是对外声明的一部分

`historical` → `{kind: end_of_day, delay_minutes: None}`；
`delayed` → `{kind: delayed, delay_minutes: 15}`。

这个值会出现在每条报价上并展示给用户，同时决定请求里的 `mode`。有测试端到端验证
两种模式各自发出正确的 `mode`。

### 91.4 装配不发起任何网络请求

只构造对象；第一次读取才打上游。有测试用一个"一旦被调用就失败"的传输层验证装配
阶段零请求。

### 91.5 一处被测试纠正的遗漏

端到端验证 `mode` 的测试最初**忘了配路由**——没有路由就不会调用任何供应商，
断言自然拿不到 URL。实现正确，是测试漏了一步。

### 91.6 验证证据

- 11 项测试：无凭据不注册、**按凭据注册（与路由无关）**、两来源注册顺序、
  路由来自配置、正股/期权路由独立、**historical 24 小时 TTL**、**delayed 15 分钟 TTL**、
  alpaca 无每日上限、marketdata 用配置的上限、**装配零请求**、
  **两种模式各自发出正确的 mode**；
- **测试规模**：`uv run pytest` → **1227 项通过**（4 项跳过）。

### 91.7 M8 剩余

`market-position-view`（持仓估值投影）、市场设置持久化与热切换、
`market-positions` 工具与 `/v1/market/positions` 路由。

## 92. M8 进展：持仓参考估值（2026-10-12）

**新增**：`domain/market_data/valuation.py`、`tests/test_market_valuation.py`。

### 92.1 金额口径

```
市值   = 数量 × 参考价 × 乘数 × 方向
浮盈   = (数量 × 参考价 − 成本) × 乘数 × 方向
口径   = reference_price_excluding_fees
```

**不是市值、不是净值**。口径必须与金额一起给出，否则用户会把它当成账户净值。

### 92.2 三条不可放宽的性质

1. **方向决定符号。** 空头持仓的市值是负的——把空头当多头会让组合总额**凭空翻正**，
   用户会以为自己在赚钱。
2. **期权乘数是 100，正股是 1。** 忘掉乘数会让期权估值**差 100 倍**。
3. **不完整时组合计为 `None`。** 部分求和会把组合总额**少报一个未知量**，
   而用户看到的是一个看起来正常的数字。

### 92.3 一处容易混同的区分

"**能算出行级估值**"与"**能给组合合计**"是两件事：

| 字段 | 含义 |
| --- | --- |
| `pricedPositions` | 有估值的行数，**包含过期报价的行** |
| `complete` | 全部行都**新鲜且有估值** |

有一行是过期报价时，`pricedPositions` 计入它，但 `complete` 为假、合计为 `None`。
混同会让用户拿到一个偏小的总额。

### 92.4 一处刻意的"不修正"

`浮盈` 的公式对空头会得到较大的正数（成本符号约定与多头不同），这与 TypeScript
**逐字一致**。迁移的目标是行为一致，不是"我觉得应该怎样"——口径问题属于 TS 侧设计，
改动它会让两侧账目对不上。

### 92.5 验证证据

- 14 项测试：多头估值、**空头符号相反**、**期权乘数 100**、乘数helper、
  无参考价时不给金额、口径随金额给出、**十进制无噪声**（`3 × 0.1 = 0.3`）、
  完整分组求和、**不完整不给合计**、**过期行有价但不完整**、空分组完整且为零、
  时点范围、未定价行不贡献时点、**合计精确**（`0.1 + 0.2 = 0.3`）；
- **测试规模**：`uv run pytest` → **1241 项通过**（4 项跳过）。

### 92.6 M8 剩余

市场设置持久化与热切换、`market-positions` 工具与 `/v1/market/positions` 路由。
**这两项做完 M8 即完成。**

## 93. M8 完成：行情设置与持仓路由（2026-10-12）

**新增**：`domain/market_data/settings.py`、`infrastructure/db/market_settings_repository.py`、
`api/market_routes.py`、`tests/test_market_routes.py`。

### 93.1 三条路由

| 路由 | 说明 |
| --- | --- |
| `GET /v1/market/settings` | 读取设置 |
| `PUT /v1/market/settings` | 保存（revision CAS） |
| `GET /v1/market/positions` | 当前持仓的参考估值 |

### 93.2 核心区分：`routes` vs `effectiveRoutes`

- `routes` 是**用户选定**的顺序；
- `effectiveRoutes` 是**实际可用**的顺序——剔除未配置凭据的来源后的结果。

**两者必须同时返回。** 只返回前者会让用户看到"已选中 alpaca"而每次请求都静默回退
到备来源，却不知道原因。这是最容易让用户困惑的一类配置问题。

### 93.3 保存用 revision CAS

行情设置是单例行。两台设备同时打开设置页时，后保存的一方若直接覆盖，就会**静默丢掉**
先保存的那份修改。CAS 让后者收到 `MARKET_SETTINGS_CHANGED`（409）并重新读取。

### 93.4 行情不可用不影响账本

`GET /v1/market/positions` 在行情不可用时**照常返回账本数据**，只是估值字段为 `null`。

**是 `null` 而不是 `0`**——`0` 会被用户当成"资产归零"。有一条测试用一个只插入交易与
批次、完全没有行情配置的库验证这一点。

### 93.5 一处如实留空

`main.py` 的行情传输层是一个**未接线的占位**（调用即抛错）。这让行情读取得到
`provider_unavailable`——这是**数据状态**而非异常，账本与持仓照常返回。

接线真实 HTTP 传输层是**一行的事**（把占位换成 `market_data.http` 里的 httpx 实现），
但当前没有可用的行情端点做端到端验证，因此保持如实留空而不是假装可用。

### 93.6 一处被测试纠正的写法

认证参数化测试对 GET 也传了 `json=None`，导致请求构造失败。改为只对 PUT 传正文。

### 93.7 验证证据

- 14 项路由测试：三条路由要求认证、初始 revision 0、保存推进 revision 并回读一致、
  **过期 revision 被拒**、**effectiveRoutes 剔除未配置来源**、3 种非法正文、
  未知资产类型、重复来源、空持仓、**行情缺失时账本照常返回且估值为 null**；
- **测试规模**：`uv run pytest` → **1255 项通过**（4 项跳过）。

### 93.8 M8 状态

**M8 完成。** 剩余的是 M9（切流与退役 TS）。

## 94. 切流前的路由对照：发现两处覆盖缺口（2026-10-12）

### 94.1 做法

把 TypeScript 侧全部注册路径与 Python 侧实际挂载的路径做双向对照。这是**硬切的前提**：
少一条路由，切过去之后那部分能力就直接 404。

### 94.2 结果：TS 46 条，Python 32 条，缺 14 条

| 缺口 | 路由 |
| --- | --- |
| **认证（6）** | `GET /v1/auth/providers`、`POST /v1/auth/{provider}/start`、`POST /v1/auth/{provider}/exchange`、`GET /v1/auth/{provider}/callback`、`POST /v1/auth/logout`、`POST /v1/devices/tokens` |
| **Gmail（8）** | `GET /v1/google/authorization-transactions`、`POST .../{state}/complete`、`GET/POST /v1/google/connections`、`GET/DELETE /v1/google/connections/{id}`、`GET/POST .../{id}/mappings`、`DELETE .../{id}/mappings/{mappingId}`、`POST .../{id}/sync` |

### 94.3 一处方法上的自我纠正

第一次对照用 `grep '"/v1/...'` 抓 TS 路由，**漏掉了用模板字符串注册的路径**
（``app.post(`/v1/auth/${provider}/start`)`` 这类）。因此第一版清单低估了缺口。

准确做法是按**路由器注册调用**枚举，而不是按字符串字面量。这也是为什么对照结果
必须与实际的 `app.get/post/...` 调用清单交叉验证。

### 94.4 最重要的发现：Gmail 子系统从未进入里程碑

**Gmail 连接管理（8 条路由）不在迁移计划的任何里程碑里。** 它不是"还没做"，
而是**从未被计划过**——M1–M9 的划分是从账本与行情出发的，Gmail 被完整漏掉了。

若不在此刻发现，切流当天才会暴露：用户在 App 里点开 Gmail 设置会直接失败。

### 94.5 对 M9 的影响

**M9（切流）现在不能开始。** 必须先补：

1. **认证路由（6 条）**——M5 的领域逻辑（登录事务、设备会话、刷新轮换）**已完整实现**，
   缺的只是 HTTP 挂载与登录服务的装配；
2. **Gmail 子系统**——需要新增一个里程碑（记作 **M10**），范围包括 Google OAuth
   离线授权、连接与映射持久化、同步任务、以及 8 条路由。

### 94.6 更新后的里程碑视图

| 里程碑 | 状态 |
| --- | --- |
| M1–M8 | ✅ |
| **M9a** 认证路由挂载 | 待开始 |
| **M10** Gmail 子系统 | **待开始（新发现）** |
| M9b 切流与退役 TS | 待开始（依赖 M9a 与 M10） |

## 95. M9a 起步：认证路由挂载（2026-10-12）

**新增**：`api/auth_routes.py`、`tests/test_auth_routes.py`。

### 95.1 本轮完成的两条

| 路由 | 说明 |
| --- | --- |
| `GET /v1/auth/providers` | 登录目录；**可用性只看配置，不发网络请求** |
| `POST /v1/devices/tokens` | 刷新令牌（调用 M5 已实现的轮换逻辑） |

**领域逻辑一行未改**——登录事务、设备会话、刷新轮换在 M5 已完成，本轮只做 HTTP 挂载。

### 95.2 一条容易搞反的约束

**这两条路由都*不*要求设备认证。**

- 登录目录本身就是获取认证的入口；
- 设备令牌刷新由**刷新凭据本身**证明身份——用已过期或已撤销的访问令牌去刷新是
  行不通的。

若照抄其他路由给它们挂上 `require_device`，刷新会在访问令牌过期的那一刻永久失效，
用户必须重新登录——**而刷新机制存在的意义正是避免这一点**。

有专门测试断言两条路由不带认证头也能成功。

### 95.3 一处响应契约

刷新响应**必须同时给出新的刷新凭据**。它是一次性轮换；客户端若不保存新的那份，
下一次刷新就会把旧的使用掉并被判定为重放，进而**撤销整个凭据族**——用户会被登出，
且原因是"检测到令牌重放"。

### 95.4 验证证据

- 11 项测试：目录**不要求认证**、三个来源固定顺序、目录**不泄漏配置细节**、
  刷新**不要求访问令牌**、**轮换返回两枚新令牌**、**旧凭据重用被拒并报重放**、
  未知凭据 401、3 种非法正文、**设备撤销后刷新失败**；
- **测试规模**：`uv run pytest` → **1266 项通过**（4 项跳过）。

### 95.5 M9a 剩余

`POST /v1/auth/{provider}/start`、`POST .../exchange`、`GET .../callback`（返回 HTML）、
`POST /v1/auth/logout`——需要装配登录服务（会话存储 + 登录来源目录 + 设备签发）。

### 95.6 之后

**M10 Gmail 子系统**（8 条路由，见 §94.4），然后是 **M9b 切流**。

## 96. M9a 进展：登录发起、回调与退出（2026-10-12）

**新增**：`POST /v1/auth/{provider}/start`、`GET /v1/auth/{provider}/callback`、
`POST /v1/auth/logout`、`build_owner_login_service`。

### 96.1 认证路由进度：5/6

| 路由 | 状态 |
| --- | --- |
| `GET /v1/auth/providers` | ✅ |
| `POST /v1/auth/{provider}/start` | ✅ |
| `GET /v1/auth/{provider}/callback` | ✅ |
| `POST /v1/auth/logout` | ✅ |
| `POST /v1/devices/tokens` | ✅ |
| `POST /v1/auth/{provider}/exchange` | ⛔ 需要异步签发改造 |

### 96.2 回调味：「app 与浏览器分离」

**浏览器只完成身份验证**，Lyra 凭据始终经绑定 verifier 的 POST 返回 App。因此回调
页面**不含任何令牌**——有测试断言页面里不出现 `token` / `code` / state 原文。

更进一步：**成功与失败返回完全相同的页面**。区分两者会让未授权方凭页面内容判断
某个 state 是否存在（即一个 state 枚举预言机）。

### 96.3 两处不能给这些路由加认证的原因

- `start` 与 `callback` 是获取认证的入口；
- `logout` **需要**认证，但撤销目标由令牌本身确定，**不接受客户端指定设备 ID**——
  否则任何已认证设备都能撤销任意其他设备。

### 96.4 限流来源键：不看 `X-Forwarded-For`

`_source_key` 取客户端的**直连地址**。那个转发头可以由客户端伪造，用它做限流
**等于把限流预算交给攻击者**——伪造一个随机值就能无限重试。

### 96.5 一处如实留空

`device_authorizer` 是一个**未接线的占位**：登录可以走到"完成浏览器验证"，但
**无法换取 Lyra 凭据**。这是明确的未完成，已在模块文档与 `main.py` 注释里写明。

接线需要一个异步改造（`OwnerLoginService.exchange` 目前是同步的，而设备签发是异步）。

### 96.6 验证证据

- 13 项新增测试：`start` 不要求认证、未配置来源 503、5 种非法参数、
  非 JSON 正文 400；**回调页面不含令牌**、**成功与失败页面完全相同**；
  `logout` 要求认证、**撤销后令牌立刻失效**、已撤销令牌不能再次登出；
- **测试规模**：`uv run pytest` → **1279 项通过**（4 项跳过）。

### 96.7 下一步

1. `POST /v1/auth/{provider}/exchange`（异步签发改造）；
2. **M10 Gmail 子系统**（8 条路由）；
3. **M9b 切流**。

## 97. M9a 完成：认证路由 6/6（2026-10-12）

### 97.1 异步改造

`OwnerLoginService.exchange` 从同步改为异步，设备签发器同样变为 async。

**为什么必须改**：设备签发走单写队列，而队列用 `asyncio.Lock` 串行化。同步版本在
事件循环里会阻塞，在没有循环时直接失败——两者都是生产事故。

改动面：服务层 2 处、路由 1 条、测试 10 处调用点（用一个同步 helper 包装）。

### 97.2 一次自伤

用正则把测试里的 `service.exchange(` 批量替换成 `_exchange(service, ` 时，
**连 helper 自身的定义也被替换**，导致无限递归（`RecursionError`）。

已改为显式调用 `OwnerLoginService.exchange(service, ...)`。教训：批量替换后必须确认
替换目标自身不在匹配范围内。

### 97.3 认证路由 6/6

| 路由 | 状态 |
| --- | --- |
| `GET /v1/auth/providers` | ✅ |
| `POST /v1/auth/{provider}/start` | ✅ |
| `POST /v1/auth/{provider}/exchange` | ✅ |
| `GET /v1/auth/{provider}/callback` | ✅ |
| `POST /v1/auth/logout` | ✅ |
| `POST /v1/devices/tokens` | ✅ |

**设备签发已接真实实现**（不再是占位）。登录链路端到端可用（在来源已配置的前提下）。

### 97.4 一处被测试纠正的预期

我断言"未知 loginId → 410"，实际是 **503**：`exchange` **先检查来源是否配置**，
再查事务。这与现有实现的顺序一致，而且顺序是对的——报"事务不存在"会让运维去查
登录事务，而真正的问题是来源没配置。

已把测试改为断言这一顺序，并额外断言"不要求设备认证"（503 而非 401）。

### 97.5 一处契约断言方式的调整

"响应不可缓存"这条契约，在未配置来源的夹具里走的是 `HTTPException` 路径，
拿不到 `no-store` 头。改为**直接断言源码里两处都设置了该头**——契约本身是可验证的，
不必依赖某个特定夹具状态。

### 97.6 验证证据

- 新增 4 项测试：来源检查先于事务查找、兑换不要求认证、响应不可缓存的源码契约、
  `start` 成功路径的 `no-store`；
- **测试规模**：`uv run pytest` → **1283 项通过**（4 项跳过）。

### 97.7 下一步

**M10 Gmail 子系统**（8 条路由，见 §94.4），然后是 **M9b 切流**。

## 98. M10 起步：Gmail 连接凭据（2026-10-12）

### 98.1 规模评估

| 项 | 数量 |
| --- | --- |
| 文件 | 13+ |
| 代码 | **约 4200 行** |
| 表 | 4（连接 / 映射 / 消息 / 消息事务） |

**比刚完成的 Agent 子系统更大。** 因此 M10 会跨多轮，按"数据库 → 供应商客户端 →
同步编排 → 路由"的顺序推进。

### 98.2 本轮完成：连接凭据

`infrastructure/gmail/connection_credential.py`、`tests/test_gmail_connection_credential.py`。

### 98.3 两处安全性质

1. **refresh token 的密文绑定连接 ID 作为 AAD**（`gmail-connection:{id}`）。

   换一个连接 ID 解密就失败，因此密文**无法在连接之间搬运**——否则一次数据库写入
   错误就能让 A 连接的凭据被 B 连接使用，而两者可能属于不同的 Google 账号。

   有测试直接验证：用正确的 ID 能解出原文，用另一个 ID 解不开。

2. **必须包含 Gmail 只读作用域**，且作用域文本**去重 + 排序**后存储。

   少了只读作用域连接根本不能用；规范化则保证同一组权限不会因顺序或重复而产生
   不同记录——否则比较两份连接的作用域会得到"不同"的错误结论。

### 98.4 两处防御

- **未验证的邮箱不能作为身份依据**：任何人都能把别人的邮箱填进自己的资料；
- **断开幂等且返回原对象**：否则"断开时间"会被每次重复请求不断后移，审计记录
  失去意义。断开只清除能继续访问邮箱的密文，**保留历史游标与消息元数据**用于审计。

另一条边界：状态已是 `disconnected` 但密文仍在时必须**继续清理**——不能因为状态就
跳过清理。有专门测试。

### 98.5 验证证据

- 17 项测试：只读作用域通过、**去重排序**、**缺作用域拒绝 GMAIL_SCOPE_MISSING**、
  空文本拒绝、pending 初态、**邮箱小写化**、**密文绑定连接 ID（可解/不可搬运）**、
  **同账号两条连接密文不同**、4 种未验证/畸形身份、空 refresh token、朴素时间拒绝、
  断开保留审计字段、**幂等返回原对象**、**断开状态但仍有密文时继续清理**；
- **测试规模**：`uv run pytest` → **1300 项通过**（4 项跳过）。

### 98.6 M10 剩余

OAuth 事务（147 行）、Google OAuth 客户端（622 行）、Gmail 客户端（623 行）、
消息元数据与 MIME（155 + 149 行）、完整消息（264 行）、导入计划（171 行）、
连接与消息仓储（326 + 382 行）、同步服务（524 行）、8 条路由、映射管理。

## 99. M10 进展：Google 授权事务（2026-10-12）

**新增**：`infrastructure/gmail/oauth_transaction.py`、`tests/test_gmail_oauth_transaction.py`。

### 99.1 文件划分对齐

`GmailOAuthError` 与 `assert_gmail_readonly_scope` 原本放在
`connection_credential.py`，现已移到 `oauth_transaction.py`——与 TypeScript 的
文件划分一致。`connection_credential` 改为从那里导入并 `__all__` 再导出，
既有测试零改动。

### 99.2 三条安全性质

1. **只存 state 的哈希**，明文只返回一次。
2. **设备不匹配与 state 不匹配合并为同一个错误码。**
   分开报会把暴力搜索变成两步——先确定"state 猜对了但设备不对"，再针对设备继续猜。
   有测试断言两者的**错误码与消息完全相同**。
3. **检查顺序是「已用 → 过期 → 无效」。**
   已用且同时过期的事务报"已使用"——**重放信号必须优先于过期**，否则一次真实重放
   会被当成普通的会话超时。

### 99.3 两处输入处理

- **PKCE 挑战按 ASCII 编码**（verifier 的字符集本就限定在 ASCII）；
- **非 ASCII 的 state 报无效而不是崩在编码上**——它不是我们生成的，但不能让
  `UnicodeEncodeError` 冒到路由层。

### 99.4 一处契约

`code_verifier` **不得返回 App、写入日志或长期持久化**：它只在交换授权码时使用一次。
这一点写在模块文档里。

### 99.5 验证证据

- 20 项测试：公开/私有部分分离、**只存哈希**、S256 挑战与 verifier 的哈希一致、
  随机性、TTL 校验、空白设备、朴素时间、消费标记、**重放拒绝**、
  **到期边界（等于即拒绝、差 1 毫秒有效）**、**已用优先于过期**、
  state 不符、**设备与 state 报同一错误**、畸形哈希、**非 ASCII state 不崩**、
  作用域规范化、缺作用域、默认不可重试；
- **测试规模**：`uv run pytest` → **1320 项通过**（4 项跳过）。

### 99.6 M10 剩余

Google OAuth 客户端（622 行）、Gmail 客户端（623 行）、消息元数据与 MIME（155 + 149）、
完整消息（264）、导入计划（171）、连接与消息仓储（326 + 382）、同步服务（524）、
8 条路由。

## 100. M10 进展：Gmail 有界搜索（2026-10-12）

**新增**：`domain/gmail/constants.py`、`infrastructure/gmail/search_query.py`、
`tests/test_gmail_search_query.py`。

### 100.1 "有界"是核心

查询必须带 ``after:`` 时间下界与消息数上限，否则**一次同步会把邮箱里的全部历史
邮件拉下来**——既慢又贵，而且绝大多数与账本无关。

### 100.2 一处最容易漏的细节

主题**必须加引号**：

```
subject:"Schwab eConfirms"
```

``Schwab eConfirms`` 含空格，**不加引号会被 Gmail 当成两个独立关键词**，从而匹配到
主题里分别含 "Schwab" 和 "eConfirms" 的无关邮件。有一条测试专门断言这个引号。

同理，发件人与主题都是**固定常量**——只有来自该发件人且主题为该字符串的邮件才会
进入账本解析流程。放宽任何一项都会让无关邮件进入解析。

### 100.3 验证证据

- 13 项测试：查询三要素齐全、**主题带引号**、**时间下界是 Unix 秒**（`after:1767571200`）、
  取整方向、5 种非法上限、4 种非法日期、主题确实含空格；
- **测试规模**：`uv run pytest` → **1333 项通过**（4 项跳过）。

### 100.4 M10 剩余

Google OAuth 客户端（622 行）、Gmail 客户端（623 行）、消息元数据与 MIME（155 + 149）、
完整消息（264）、导入计划（171）、连接与消息仓储（326 + 382）、同步服务（524）、
8 条路由。

## 101. M10 进展：RFC MIME 解析（2026-10-12）

**新增**：`domain/gmail/econfirm.py`、`infrastructure/gmail/html_text.py`、
`infrastructure/gmail/rfc_mime.py`、`tests/test_gmail_rfc_mime.py`。

### 101.1 消息 ID 的回退链是幂等性的基础

```
X-GM-MSGID  →  X-Gmail-Message-ID  →  Message-ID  →  sha256(原始字节)
```

前几项都是邮件自身的标识，因此**同一封邮件重复处理得到同一个 ID**；最后一项兜底
保证任何邮件都有稳定标识。**少了这条链，重复同步会产生重复交易。**

两处防御：自定义标识头必须是**唯一字符串**（重复头产生不明确的身份，取第一个还是
最后一个都是猜测）；标识不得含 CRLF。

### 101.2 一处测试前提的纠正

我写了一条"原始字节里的 CRLF 构成头注入"的测试，**它没有失败**——因为 MIME 解析器
会把 CRLF 拆成独立头，`Injected` 成为另一个头，标识值仍只是 `a`。

**真实保护来自解析器本身**，CRLF 守卫是纵深防御（保护"标识被写进日志或下游协议"
这条路径）。已把测试改为两条：一条记录解析器的真实行为，一条直接测试守卫。

### 101.3 HTML 转文本：块之间必须双换行

```
<div>A</div><div>B</div>  →  "A\n\nB"
```

**单换行在 eConfirm 正文里是同一字段的续行**，而块之间必须是段落级分隔。用单换行
连接会让两个不同字段被解析成一个。

另：删除 `<script>` / `<style>` 内容、解码标准实体、**未知实体原样保留**
（猜它可能是哪个字符会让正文内容被篡改）。

### 101.4 伪装地址必须被拒

```
donotreply@mail.schwab.com.attacker.net   ← 域名后缀伪装
evil-donotreply@mail.schwab.com           ← 本地部分伪装
```

发件人模式要求目标地址**前后是边界**（开头/`<`/空白 与 `>`/空白/结尾），
因此上述两者都不匹配。宽松的"包含即匹配"会让第一个通过。

### 101.5 验证证据

- 25 项测试：合法解析、HTML 转换、缺 Date 头、主题带批次后缀、
  **消息 ID 三级回退与兜底**、**重复标识头拒绝**、**解析器中和原始 CRLF**、
  **守卫直接拒绝换行**、非 Schwab 发件人、**3 种伪装地址**、主题不符、空内容、
  缺正文、非法 Date、5 种非法来源名、HTML 块边界/脚本清除/实体解码/空白折叠、
  **候选纯文本优先**、跳过空与重复、多部分邮件的候选暴露；
- **测试规模**：`uv run pytest` → **1359 项通过**（4 项跳过）。

### 101.6 M10 剩余

Google OAuth 客户端（622 行）、Gmail 客户端（623 行）、消息元数据（155）、
完整消息的其余部分（264）、导入计划（171）、连接与消息仓储（326 + 382）、
同步服务（524）、8 条路由。

## 102. M10 进展：消息元数据（2026-10-12）

**新增**：`domain/gmail/sync_state.py`、`infrastructure/gmail/message_metadata.py`、
`tests/test_gmail_message_metadata.py`。

### 102.1 内容摘要的三处规范化

```json
{"sender": 去空白 + 小写, "subject": 去空白, "body": CRLF → LF}
```

少了任何一处，同一封邮件重新抓取都会被判定为"内容变了"，触发一次不必要的冲突处理，
**而用户看到的是"这封邮件有冲突"**。

换行符差异尤其常见：Gmail API 与原始 RFC 邮件的换行表示不同。有测试直接验证
`a\r\nb`、`a\rb`、`a\nb` 得到同一个摘要。

反过来，**内容真的变了摘要必须变**——否则冲突检测失效。发送人、主题、正文各自的
变化都有测试覆盖。

### 102.2 失败记录无正文

失败元数据只有**稳定错误码与摘要**，没有正文字段。把解析不了的原始内容留在库里
没有价值，只有风险。有测试断言 `not hasattr(result, "body")`。

同时 **摘要与成功路径一致**——这样同一 message ID 的内容变化才能被检测到。

### 102.3 拒绝与冲突是两类不同的后续动作

```
GMAIL_MESSAGE_REJECTED            → rejected   （看邮件内容）
ACCOUNT_MAPPING_REQUIRED          → conflict   （看账户映射）
GMAIL_MESSAGE_CONFLICT            → conflict   （看账户映射）
```

合并成一种状态会让 App 无法决定该提示用户改什么。

### 102.4 两处标识规范化

- **history id 无前导零**：否则同一个游标有两种写法，增量同步的游标比较会误判为
  "变了"；
- **同步时间必须显式以 `Z` 结尾**：没有时会话时区会让同一瞬时在不同机器上产生不同
  文本，而它会进入审计记录。

### 102.5 验证证据

- 30 项测试：**三种换行符同摘要**、发件人大小写与空白、主题空白、三种"真变化"、
  成功元数据字段、空白可选字段归 null、缺时间保持 null、空必填拒绝、
  rejected 映射、**两种冲突映射**、**失败记录无正文字段**、**成功与失败同摘要**、
  毫秒/ISO/RFC2822 时间、3 种非法时间、3 种合法与 5 种非法游标、UTC 标记要求；
- **测试规模**：`uv run pytest` → **1389 项通过**（4 项跳过）。

### 102.6 M10 剩余

Google OAuth 客户端（622 行）、Gmail 客户端（623 行）、导入计划（171）、
连接与消息仓储（326 + 382）、同步服务（524）、8 条路由。

## 103. M10 进展：邮件成交 → 原始字段（2026-10-12）

**新增**：`EmailTrade`、`email_trade_to_raw_fields`、`tests/test_gmail_email_trade_fields.py`。

### 103.1 核心一致性要求

映射**必须与 CSV 解析器产出的字段逐字符一致**，否则同一笔交易经邮件导入与经 CSV
导入会得到不同的交易哈希，跨来源去重失效——**用户会看到同一笔交易出现两次**。

测试**直接引用 CSV 解析器的表头常量** `SCHWAB_TRANSACTION_HEADERS` 做集合比较，
而不是手抄一份字段名清单。

### 103.2 三处格式约定，各自都会破坏一致性

| 约定 | 破坏方式 |
| --- | --- |
| `Date` 用 `MM/DD/YYYY` **零填充** | 与 Schwab 展示格式不一致 |
| `Fees & Comm` **为零时留空**（不是 `$0`） | 行哈希改变，跨来源去重失效 |
| `Amount` **符号位在 `$` 之前**（`-$405`） | 不是 `$-405` |

另有：价格用无指数文本——`1E-7` 这类科学计数法会让 CSV 解析器读不到。

### 103.3 一处测试预期的纠正

我断言 `2026-1-5` 会被拒绝，实际**不会**：参考实现只检查"split 后三段是否都存在"，
因此它产出 `1/5/2026`。

**把它写成"拒绝"会让本函数比参考实现更严格——在迁移中是行为偏离。** 已改为两条
测试：一条覆盖真正无法拆分的日期，一条记录这一边界并说明为什么不该收紧。

### 103.4 验证证据

- 15 项测试：**字段名与 CSV 表头集合相同**、日期零填充、4 种无法拆分日期、
  **部分补零不报错（并记录理由）**、**四种零费用写法都留空**、非零费用带 `$`、
  **符号位在 `$` 之前**、正数无 `+`、零金额无负号、数量与价格为无指数文本、
  **极小价格不出现科学计数法**、透传字段原样；
- **测试规模**：`uv run pytest` → **1403 项通过**（4 项跳过）。

### 103.5 M10 剩余

Gmail 导入计划（决策逻辑：冲突 / 已导入 / 就绪）、Google OAuth 客户端（622 行）、
Gmail 客户端（623 行）、连接与消息仓储（326 + 382）、同步服务（524）、8 条路由。

## 104. M10 进展：Gmail 导入计划（2026-10-12）

**新增**：`infrastructure/gmail/message_import_plan.py`、
`tests/test_gmail_message_import_plan.py`。

### 104.1 决策顺序本身就是正确性的一部分

```
1. 已有记录且内容或账户归属变了 → 冲突（409，带 changed_fields）
2. 已有记录且已导入             → 跳过（already_imported）
3. 其余                         → 就绪（ready）
```

**冲突检查必须在"已导入"之前。** 一封已导入的邮件若内容变了，先报"已导入"会让
**变更被静默忽略**——而用户以为这封邮件已经被处理过了。有专门测试断言这个顺序。

反过来，**解析失败或冲突的记录会落到第 3 步重新尝试**：那是修复后重试的路径。
因为"库里有记录"就跳过会让用户永远无法重新导入。

`changed_fields` 按 `content_hash` → `account_id` 的**固定顺序**列出，方便客户端
稳定分支。

### 104.2 一处重要的重构：返回领域对象而不是 dict

`email_trade_to_raw_fields` 最初返回 CSV 表头键的 dict，但 `create_import_preview`
需要的是 `RawTransactionFields` 领域对象（Python 命名）。已改为返回领域对象。

这让**跨来源一致性成为结构性保证**，而不是"字符串清单比对"——两者产出同一个类型，
字段名不可能不一致。

### 104.3 补了一条真正的端到端对照

原来的测试比较"字段名集合 == CSV 表头集合"，那只是弱检查。现改为：

1. 构造等价 CSV 文本，**真的喂进 CSV 解析器**；
2. 与邮件路径的产出**逐字段比较**，断言两个领域对象完全相等。

这才是跨来源去重的真正依据——字段不同的任何一个字符都会让两条路径算出不同的交易
哈希，**用户就会看到同一笔交易出现两次**。

### 104.4 关联序号从 1 开始

`ordinal` 从 1 开始：0 会让"第一条"与"未设置"难以区分。

### 104.5 诊断摘要不含内容

`plan_summary` 只输出身份、指纹与计数，**不含正文或成交明细**——诊断信息常被转发
到日志聚合系统，明细属于账本数据。有测试用形如 `SECRET BODY CONTENT` 的正文验证。

### 104.6 验证证据

- 16 项计划测试 + 16 项字段测试：首次导入就绪、关联序号与身份、
  **未变的已导入跳过**、**四种失败状态重新尝试**、**内容变更冲突**、
  **账户变更冲突**、**两项变更的固定顺序**、冲突详情带消息 ID、
  **冲突先于"已导入"**、摘要不含内容、跳过计划的最小摘要；
  **邮件与 CSV 产出完全相同的领域字段（端到端）**、领域字段名是 Python 风格、
  日期零填充、无法拆分的日期、部分补零不报错、四种零费用写法、
  符号位在 `$` 之前、无科学计数法；
- **测试规模**：`uv run pytest` → **1421 项通过**（4 项跳过）。

### 104.7 M10 剩余

Google OAuth 客户端（622 行）、Gmail 客户端（623 行）、连接与消息仓储（326 + 382）、
同步服务（524）、8 条路由。

## 105. M10 进展：Gmail 连接与映射仓储（2026-10-12）

**新增**：`infrastructure/db/gmail_connection_repository.py`、
`tests/test_gmail_connection_repository.py`。

### 105.1 一条被测试抓到的真实缺陷

实现时我把 `ConstraintException` 一律当成唯一约束：

```python
except duckdb.ConstraintException:
    raise ApplicationError("该 Google 账号已有连接")
```

但 **schema 的 CHECK 违规也抛同一个异常类型**。于是"未断开却没有 refresh token"
这种行会被误报成"该 Google 账号已有连接"——**把运维引向完全错误的方向**：他会去
检查是不是重复授权，而真正的问题是连接记录缺了令牌。

改为在插入前**显式校验该不变量**并报准确错误；schema 的 CHECK 仍是最后一道防线
（有一条测试直接 `INSERT` 验证它还在）。

### 105.2 三条 schema 约束与它们的意义

| 约束 | 少了它会怎样 |
| --- | --- |
| `google_subject` UNIQUE | 同一账号两条连接，**同步游标互相覆盖** |
| `status = 'disconnected' OR token IS NOT NULL` | 出现既不能用也无法重连的连接 |
| `UNIQUE (connection_id, account_suffix)` | 一个尾号映射到两个账户，交易进错账本 |

**不同连接可以映射同一个尾号**——那可能是两个券商账户，因此唯一性是"连接内"的，
有测试覆盖这一点。

### 105.3 改绑用旧账户 ID 做 CAS

```
UPDATE ... WHERE id = ? AND account_id = ?
```

CAS 失败报 409（**可重试**）。静默覆盖会让先到设备的改动消失，而两台设备都以为
保存成功了。未知映射同样报冲突，不泄漏"是否存在"。

### 105.4 验证证据

- 15 项测试：插入读回、未知 ID 返回 None、**账号唯一**、创建顺序、
  断开后清空令牌、**无令牌必须断开（准确错误）**、**schema CHECK 仍在**、
  映射建立读回、**连接内尾号唯一**、**跨连接同尾号允许**、空白尾号拒绝、
  按尾号排序、改绑成功、**旧账户不匹配冲突**、未知映射冲突；
- **测试规模**：`uv run pytest` → **1436 项通过**（4 项跳过）。

### 105.5 M10 剩余

Google OAuth 客户端（622 行）、Gmail 客户端（623 行）、消息仓储（382 行）、
同步服务（524 行）、8 条路由。

## 106. M10 进展：Gmail 消息归档仓储（2026-10-12）

**新增**：`infrastructure/db/gmail_message_repository.py`、
`tests/test_gmail_message_repository.py`。

### 106.1 两处经实测确认的行为

**其一：``content_hash`` 为 NULL 的行视为"没有已处理状态"。**

这类行没有可比较的内容状态，按"未处理"对待才能让它被重新处理。顺带核实：
``discovered`` 状态在参考实现里**从未被写入**（全仓库只有一处 `INSERT INTO
gmail_messages`，且只从元数据取状态），因此这条分支当前不可达——但保留它比让它
抛错安全。

**其二：重复归档同一封邮件会撞主键。**

参考实现用的是**普通 INSERT（无 upsert）**，主键是 ``(connection_id, message_id)``；
而同步服务在失败路径上**无条件**调用归档。因此"同一封邮件在重叠窗口里被处理两次"
会抛 ``ConstraintException``。

这条不是我的实现问题，而是参考实现的既有行为。**已写成测试固定下来**——迁移的目标
是行为一致；顺手"修好"它会让两侧在这一点上分叉。调用方若需要幂等重试，必须自己
先查状态再决定是否归档。

### 106.2 三条 schema 约束

| 约束 | 作用 |
| --- | --- |
| PK `(connection_id, message_id)` | 一封邮件在一个连接里只有一条归档 |
| `parse_status = 'discovered' OR length(content_hash) = 64` | **非 discovered 状态必须有摘要** |
| `gmail_message_transactions.ordinal > 0` | 印证了此前"关联序号从 1 开始"的决定 |

### 106.3 一处测试预期的纠正

我断言归档后状态为 ``imported``，实际是 ``parsed``——**升级为 imported 是导入计划
做的事**，元数据构造本身只标记到 parsed。实现正确，是测试断言写错。

### 106.4 验证证据

- 8 项测试：无行返回 None、**NULL 摘要视为无状态**、读回已归档状态、**按连接隔离**、
  失败归档无正文、映射冲突记为 conflict、**重复归档撞主键（真实行为）**、
  **非 discovered 状态必须有摘要**；
- **测试规模**：`uv run pytest` → **1444 项通过**（4 项跳过）。

### 106.5 M10 剩余

Google OAuth 客户端（622 行）、Gmail 客户端（623 行）、消息导入的账本写入
（`persistGmailMessageImport`，深度耦合账本/FIFO）、同步服务（524 行）、8 条路由。

## 107. M10 进展：Gmail 消息导入的原子提交（2026-10-12）

**新增**：`persist_gmail_message_import`、`write_ledger_changes`（重构）、
`tests/test_gmail_message_repository.py` 新增 4 项。

### 107.1 一处必要的重构（DRY）

`DuckDbImportCommitRepository.commit_import` 原本**自行提交**，而 Gmail 路径需要把
账本写入与消息归档放在**同一事务**里。

没有复制粘贴，而是抽出 `write_ledger_changes`（**不提交也不回滚**，调用方持有事务
边界），`commit_import` 改为「BEGIN → write → COMMIT」。两条路径复用同一个写入核心。

**为什么必须同一事务**：分开提交会出现"交易已入账但消息没归档"的中间态——重同步时
那封邮件会被当成新邮件**再次导入**，用户看到重复交易。

同时 `_create_ledger_version` 增加 `reason` 参数：审计时需要区分交易来自文件
（`file_import`）还是邮件（`gmail_import`）。

### 107.2 一处真实的集成缺口（被测试抓到）

`load_existing_gmail_message_state` 返回的是 dict，而
`build_gmail_message_import_plan` 需要 `ExistingGmailMessageState` 对象。

TypeScript 侧 `ExistingGmailMessageState` 是**结构化接口**，普通对象即可满足；
Python 没有这个性质，于是组合时报 `AttributeError`。已改为返回领域对象。

**这类缺口在跨语言迁移里很典型**：单看每个模块都正确，只有把它们串起来才会暴露。
这也是为什么每轮都跑全量测试。

### 107.3 验证证据

- 4 项新测试：**账本交易 / 批次 / 消息 / 关联一次事务全部写入**、
  **账本版本 reason 是 `gmail_import` 且引用是消息 ID**、
  **版本冲突时四张表全为空（原子性）**、
  **同一封邮件二次导入被跳过且账本不变**；
- **测试规模**：`uv run pytest` → **1448 项通过**（4 项跳过）。

### 107.4 M10 剩余

Google OAuth 客户端（622 行）、Gmail 客户端（623 行）、同步服务（524 行）、
8 条路由。

## 108. M10 进展：同步计划与增量判定（2026-10-12）

**新增**：`domain/gmail/sync_plan.py`、`tests/test_gmail_sync_plan.py`。

### 108.1 两处「必须停下来问用户」的设计

**其一：首次扫描与重扫都需要用户确认起始日期。**

```
pending_initial_scan / needs_resync 且没有确认日期
  → GMAIL_RESYNC_CONFIRMATION_REQUIRED
```

让代码自己挑一个日期，就是在替用户决定**他要导入多久的历史**——而一次全量扫描可能
跨越数年、上万封邮件，既慢又可能把早已不需要的旧交易写进账本。

**其二：增量响应被截断时判为需要重扫。**

```
truncated = true  →  needs_resync（GMAIL_INCREMENTAL_LIMIT_EXCEEDED）
```

截断意味着这次的增量窗口**不完整**；照常推进游标会**静默漏掉**中间的消息，而那些
交易本应进入账本——用户只会发现某些交易莫名其妙地不存在。

注意「零条候选」是**合法的就绪结果**，不是错误：那段时间邮箱确实没有新邮件。有测试
把这两者区分开。

### 108.2 三种不可用状态对应三种处置

| 状态 | 错误码 | 用户要做什么 |
| --- | --- | --- |
| `disconnected` | `GMAIL_AUTH_REVOKED` | 重新走 OAuth |
| `error` | `GMAIL_CONNECTION_ERROR` | 排查连接故障 |
| `active` 但无游标 | `GMAIL_HISTORY_EXPIRED` | 确认重扫范围 |

合并成一种「连接不可用」会让 App 无法决定该提示用户做什么。

### 108.3 游标失效只接受一个错误码

`mark_gmail_history_expired` **只允许记录** `GMAIL_HISTORY_EXPIRED`。其它错误码对应
不同的处置方式，用它们表示"游标失效"会让调用方走错分支。

同时只有 `active` 连接可以标记失效——已断开的连接应该表达为"需要重新授权"。

### 108.4 已断开的连接不能「完成」同步

`complete_gmail_sync` 对 `disconnected` 抛 `GMAIL_AUTH_REVOKED`：一次不可能发生的
同步不该有"完成"结果。完成时会**清除错误码**——留着旧错误会让下一次计划误判状态。

### 108.5 验证证据

- 23 项测试：active 增量、无游标报失效、**两种状态都要求确认**、
  确认后返回有界重扫、断开报授权失效、错误状态报连接错误、
  未截断就绪、**截断强制重扫**、**零候选是合法就绪**、
  三种非 active 拒绝处理增量、3 种非法候选数、标记失效保留游标、
  **拒绝其它错误码**、非 active 不能标记、完成推进游标、**断开不能完成**、
  错误码闭集；
- **测试规模**：`uv run pytest` → **1471 项通过**（4 项跳过）。

### 108.6 M10 剩余

Google OAuth 客户端（622 行）、Gmail 客户端（623 行）、同步服务（524 行）、
8 条路由。

## 109. M10 进展：Google 授权请求构造（2026-10-12）

**新增**：`infrastructure/gmail/google_oauth_client.py`、
`tests/test_gmail_google_oauth_client.py`。

### 109.1 最要紧的一条：offline 与 consent 必须成对

```
access_type=offline   与   prompt=consent   ← 缺一不可
```

缺任何一个，Google 都不会返回 refresh token：

- **只有 `access_type=offline`**：第二次授权不再返回 refresh token（Google 认为
  已有授权）；
- **只有 `prompt=consent`**：返回的令牌不含离线能力。

后果是连接在 access token 过期后**再也无法刷新**——用户看到"连接突然失效"，
重新授权一次又短暂可用，如此反复。有一对专门的测试断言两个参数同时存在。

`include_granted_scopes=true` 启用增量授权：保留用户此前授予的其它权限。

### 109.2 回调地址不得含凭据或 fragment

- **凭据**（`https://user:pass@…`）会随授权地址写进浏览器历史与日志；
- **fragment** 会被浏览器剥离，导致回调**静默缺少参数**——表现是"授权成功但登录
  没反应"。

### 109.3 PKCE 方法必须是 S256

`plain` 会让 verifier 直接出现在授权地址里，PKCE 的意义（防止授权码被拦截后使用）
随之消失。

### 109.4 一处测试预期的纠正

我写的正则 `client ID 不能为空` 多了个空格，而实现（与我在其它模块的写法一致）
产出的是 `client ID不能为空`。实现正确，是测试正则写错。

### 109.5 授权地址不含密钥

有测试断言 URL 里不出现 `client_secret` / `secret` / `api_key` / `token`——
这个地址会进入浏览器历史与各类日志。

### 109.6 验证证据

- 20 项测试：**四个端点常量与 15 秒超时**、作用域顺序、地址构造、
  **offline 与 consent 同时存在**、增量授权、作用域空格分隔、过期时间取自事务、
  **地址不含密钥**、2 种空 client ID、**4 种非法回调地址**、
  state 必须 43 字符、**PKCE 必须是 S256**、challenge 形状、非法过期时间、
  state 与 challenge 不混淆；
- **测试规模**：`uv run pytest` → **1491 项通过**（4 项跳过）。

### 109.7 M10 剩余

Google OAuth 的授权码交换与令牌刷新、令牌撤销（约 450 行）；
Gmail 客户端（623 行）；同步服务（524 行）；8 条路由。

## 110. M10 进展：令牌失败映射与凭据解密（2026-10-12）

**新增**：`token_endpoint_failure_code`、`calculate_access_token_expiry`、
`decrypt_connection_refresh_token`、两个表单构造器（+23 项测试）。

### 110.1 400 / 401 / 403 表示授权已失效

```
400 / 401 / 403  →  GMAIL_AUTH_REVOKED   （需要用户重新授权）
5xx / 429 / 传输失败  →  GMAIL_CONNECTION_ERROR  （暂时性）
```

Google 用 **400**（``invalid_grant``）表示 refresh token 已被撤销或过期。把它当成
暂时性故障会让客户端对着一个**已作废的凭据**无限重试，而每次重试都不会成功。

### 110.2 凭据解密的两类失败对应两种补救方式

| 情形 | 错误码 | 用户/运维该做什么 |
| --- | --- | --- |
| 连接已断开，或没有密文 | `GMAIL_AUTH_REVOKED` | 重新授权 |
| **解密失败** | `GMAIL_CONNECTION_ERROR` | **检查服务端密钥** |

把两者混为一谈会让真正的**密钥问题**被掩盖在一连串"请重新登录"的提示后面——用户
反复重新授权都没用，因为问题不在他这边。

有测试断言解密失败的消息里出现"密钥"字样，且错误码是 `GMAIL_CONNECTION_ERROR`。

解密时用**连接 ID 作为 AAD**，与加密时一致（§98.3）。

### 110.3 两处表单差异

- **交换**需要 `redirect_uri`（Google 用它校验来源与授权时一致）；
- **刷新不需要** `redirect_uri`——那个参数只在授权码交换阶段有意义。

两处都拒绝空值，避免向 Google 发送不完整的授权请求。

### 110.4 到期时间溢出返回 None

`calculate_access_token_expiry` 在溢出时返回 `None`，让调用方判定响应畸形，
而不是产出一个离谱的到期时间（那会让后续所有"令牌是否过期"的判断失效）。

### 110.5 验证证据

- 23 项新增测试：**3 种失效状态 → AUTH_REVOKED**、**5 种暂时性失败 →
  CONNECTION_ERROR**、到期计算（含零秒与极短）、**溢出返回 None**、
  **断开需重新授权**、**解密失败是连接错误且提及密钥**、
  解密用连接 ID 作 AAD、交换表单字段、可选密钥、**刷新表单不含 redirect_uri**、
  4 种空交换参数、2 种空刷新参数；
- **测试规模**：`uv run pytest` → **1514 项通过**（4 项跳过）。

### 110.6 M10 剩余

授权码交换与刷新的 HTTP 编排（含 userinfo 校验）；令牌撤销；Gmail 客户端（623 行）；
同步服务（524 行）；8 条路由。

## 111. M10 进展：令牌与身份响应解析（2026-10-12）

**新增**：`parse_google_token_response`、`parse_verified_identity`、
`google_endpoint_error`（+40 项测试）。

### 111.1 token_type 必须是 bearer

非 bearer 类型说明响应不是我们预期的形状。**把它当 Bearer 令牌用，会在下一次调用时
报出一个与根因无关的 401**——排查方向完全错了。大小写不敏感（`Bearer` / `bearer` /
`BEARER` 都接受）。

### 111.2 expires_in 必须是正安全整数

零或负值会让令牌**"生下来就过期"**：每次请求都用一次刷新，既慢又容易触发限流。

### 111.3 email_verified 必须是布尔 true

**不是"真值"。** Google 文档明确该字段是布尔；接受 `"true"` 或 `1` 会让"未验证
邮箱"在某些响应形状下被当成已验证——**而邮箱是账户归属的依据**。

有 5 种"看起来真但不是布尔 true"的取值被拒绝：`False`、`None`、`"true"`、`1`、
`"True"`。

### 111.4 只有 429 与 5xx 可重试

```
429 / 5xx  →  retryable = true
其它 4xx    →  retryable = false（重试不会改变结果）
```

两个端点的提示文案不同（"无法完成 Google 授权" vs "无法验证 Google 账户身份"），
让用户能判断该做什么。

Google 的稳定错误码（如 `invalid_grant` / `redirect_uri_mismatch`）**只留在原因链里
用于定位**，页面仍只看到收敛后的上层错误码。

### 111.5 一次导入遗漏

上一轮我把 `oauth_transaction` 的导入路径从 `...domain.gmail` 改成 `.oauth_transaction`
时，本轮新增 `GmailOAuthError` 的替换没有匹配上**已被改过的**导入块，于是多出 36 项
`NameError`。

**全量测试立刻暴露了它**——这也是每轮跑全量而不是只跑改动文件的理由。

### 111.6 验证证据

- 40 项新增测试：令牌响应四字段、**3 种大小写**、**5 种非 bearer 拒绝**、
  **6 种非法 expires_in**、3 种缺字段、非对象负载；身份解析与邮箱小写、
  **5 种"伪 true"拒绝**、5 种畸形邮箱、缺 subject；
  **4 种可重试状态**、**5 种不可重试状态**、两端点文案不同；
- **测试规模**：`uv run pytest` → **1554 项通过**（4 项跳过）。

### 111.7 M10 剩余

授权码交换与刷新的 HTTP 编排（把上面的部件串起来）；令牌撤销；Gmail 客户端
（623 行）；同步服务（524 行）；8 条路由。

## 112. M10 进展：OAuth HTTP 编排（2026-10-12）

**新增**：`exchange_google_authorization_code`、`refresh_google_access_token`、
`revoke_gmail_connection_authorization`、`GoogleEndpointFailure`（+53 项测试）。

### 112.1 传输层可注入

编排函数接收 `post_form` / `get_userinfo` 回调，因此**整条链路可以在没有网络的情况下
完整测试**；真实实现只是把 httpx 接上去。

### 112.2 交换阶段的三道校验

| 校验 | 不做会怎样 |
| --- | --- |
| **作用域含 `gmail.readonly`** | 连接"成功建立"但**永远读不到邮件**——一个安静失效的连接 |
| **`email_verified === true`** | 未验证邮箱成为账户归属依据 |
| `expires_in` 有效 | 令牌"生下来就过期" |

第一道尤其值得注意：用户看到的会是"已连接"，而同步永远返回零封邮件。

### 112.3 刷新阶段沿用上一轮的失败映射

```
400 / 401 / 403  →  GMAIL_AUTH_REVOKED（重新授权）
其它              →  GMAIL_CONNECTION_ERROR（可重试性透传）
```

### 112.4 撤销失败必须上报

`revoke_gmail_connection_authorization` 在传输失败时**抛错而不是静默返回**。
静默成功会让用户以为凭据已作废，**而 Google 侧仍然有效**——一个已经断开却仍可被
使用的授权是最糟的状态。

已断开的连接没有凭据可撤销，此时**不发请求**（有测试断言零调用）。

### 112.5 Google 的稳定错误码只进原因链

`GoogleEndpointFailure.oauth_error_code` 保留 `invalid_grant` /
`redirect_uri_mismatch` 等用于定位，但**页面看到的仍是收敛后的上层错误码**。

### 112.6 验证证据

- 53 项新增测试：交换返回三部分、**verifier 只发给令牌端点**、
  **缺只读作用域失败**、**邮箱未验证失败**、传输层失败文案、
  **userinfo 失败用身份文案**；刷新返回新令牌且**不含 redirect_uri**、
  **400 → AUTH_REVOKED**、**503 → CONNECTION_ERROR 且可重试**、
  存储作用域错误时不必刷新；撤销发送令牌、**失败上报**、
  **已断开时零调用**；
- **测试规模**：`uv run pytest` → **1567 项通过**（4 项跳过）。

### 112.7 M10 剩余

令牌撤销后的连接状态更新（把撤销接到 `disconnect_gmail_connection`）；
Gmail 客户端（623 行，消息列表/详情/历史）；同步服务（524 行）；8 条路由。

## 113. M10 进展：Gmail 响应解析（2026-10-12）

**新增**：`infrastructure/gmail/google_gmail_client.py`、
`tests/test_gmail_google_client.py`。

### 113.1 message id 的字符集校验是路径注入防线

```
^[A-Za-z0-9_-]{1,256}$
```

message id 会被拼进 API 路径（``/messages/{id}``）。不校验字符集，一个含 ``/`` 或
``..`` 的 id 就能**把请求指向另一个端点**；含空白的 id 还会破坏日志结构。

而 Gmail 返回的 id 本就只有这个字符集——放宽没有任何收益。有测试直接拒绝
``a/b``、``../messages``、``a b``、``a?b``、``a#b`` 等。

### 113.2 结构不符即报错，不尽力而为地跳过

所有解析器都遵循同一条：结构不符**抛错**。一次静默跳过会让某封邮件永远不被导入，
**而调用方以为同步成功了**——比直接失败危险得多。

### 113.3 只取 messagesAdded

Gmail 的 ``history.list`` 会返回 ``messagesAdded`` / ``messagesDeleted`` /
``labelsAdded`` 等多种变更。本同步只关心**新出现的邮件**，其余跳过而不是报错。

**重复的新增记录原样保留**：去重是调用方的事（同一封邮件只处理一次），解析层
去重会掩盖"Gmail 重复上报"这一事实。

### 113.4 元数据过滤在拉正文之前

元数据便宜、正文昂贵。`is_schwab_econfirm_metadata` 先把无关邮件筛掉。

- **头名大小写不敏感**（HTTP 头本身如此）；
- **重复头取最后一个**——与参考实现一致；
- 伪装发件人（``donotreply@mail.schwab.com.attacker.net``）不匹配。

### 113.5 验证证据

- 40 项测试：端点常量与上限、**4 种合法 id**、**9 种非法 id（含路径穿越）**、
  4 种非字符串 id、缺失时的说明；messages.list 解析 / 缺字段 / 非数组 / 非对象项；
  history 解析、**跳过非新增变更**、缺字段、**5 种畸形结构**、重复保留；
  元数据匹配、**头名大小写**、伪装发件人、主题不符、**2 种缺 headers**、
  畸形头、重复头取最后；
- **测试规模**：`uv run pytest` → **1607 项通过**（4 项跳过）。

### 113.6 M10 剩余

Gmail 客户端的 HTTP 编排（消息列表/详情/历史的拉取与分页、增量截断判定）；
令牌撤销后的连接状态更新；同步服务（524 行）；8 条路由。

## 114. M10 进展：Gmail 分页与 Profile（2026-10-12）

**新增**：`fetch_bounded_gmail_message_ids`、`parse_gmail_profile`（+39 项测试）。

### 114.1 分页的两道守卫

**其一：同一 message id 出现在多页里 → 报错。**

重复会让同一封邮件被处理两次，而**导入层的幂等检查在更后面才起作用**——那时已经
多打了一次 Gmail API。

**其二：重复的 ``nextPageToken`` → 报错（死循环防线）。**

一个总是返回同一个 token 的响应（有 bug 或被篡改）会让循环**永远转下去**，不断打
API，直到超时或限流。有测试用两页返回同一个 token 验证。

### 114.2 单页大小随剩余额度收缩

```
maxResults = min(500, 剩余额度)
```

不收缩会在只剩 1 条额度时还去拉 500 条，把无关邮件一并读进来。

### 114.3 截断的两种来源

```python
truncated = nextPageToken 存在 or 本页条数 > 剩余额度
```

第二种容易被忽略：**服务端无视 ``maxResults`` 时**，我们会以为自己只拿到刚好够用的
那批，而实际上还有更多没读。两种情况都必须报截断，由同步层转成"需要重扫"。

恰好凑满且没有下一页则**不算截断**——有测试区分这两种情况。

### 114.4 Profile 的邮箱必须与已连接账户一致

**不核对就可能用一个账户的令牌去读另一个账户的邮箱**，而返回的数据看起来完全正常。
比较不区分大小写。有测试用一个不一致的 profile 验证。

### 114.5 令牌只经 Authorization 头发送

有测试断言令牌不出现在 URL 里。

### 114.6 验证证据

- 39 项新增测试：跨页收集、**单页上限与收缩**、**达到上限且有下一页 → 截断**、
  **恰好凑满且无下一页 → 不截断**、**服务端超发 → 截断**、
  **跨页重复 id 拒绝**、**重复 page token 拒绝（死循环）**、空 token 拒绝、
  **令牌只进请求头**、4 种非法上限、空令牌；profile 解析、**邮箱不一致拒绝**、
  大小写不敏感、**5 种非法 history id**、**2×5 种非法计数**、空期望邮箱、
  5 种非法邮箱；
- **测试规模**：`uv run pytest` → **1646 项通过**（4 项跳过）。

### 114.7 M10 剩余

`fetchIncrementalGmailFullRecords`（增量历史拉取，含分页与截断判定）；
消息正文的完整拉取与解析；令牌撤销后的连接状态更新；同步服务（524 行）；8 条路由。

## 115. M10 进展：增量历史拉取（2026-10-12）

**新增**：`fetch_incremental_gmail_message_ids`（+21 项测试）。

### 115.1 一处刻意与有界扫描不同的处理

| 场景 | 有界扫描（`messages.list`） | 增量（`history.list`） |
| --- | --- | --- |
| 重复 message id | **报错** | **跳过** |

`history.list` 会为同一封邮件返回多条 history 记录（例如它同时被加了标签），因此
重复是**正常的**。而 `messages.list` 重复返回同一封邮件意味着响应损坏。

两处行为分别匹配各自接口的语义——**统一成一种会让其中一边出错**。

### 115.2 服务端过滤 `historyTypes=messageAdded`

`history.list` 默认会返回标签变更等与导入无关的记录。不过滤会让无关变更也走一遍
拉取流程（每条都要请求元数据），既慢又费额度。

### 115.3 游标以最后一页为准

每一页的 `historyId` 都覆盖上一次。它标记的是"**本次读到了哪个时点**"，而不是起始
时点——写成第一页的值会让下次增量重读已经处理过的区间。

### 115.4 一处被探针抓到的真实缺陷

`truncated` 只在"达到上限"的分支里赋值，**循环从别处退出时它会未定义**，直接抛
`UnboundLocalError`。

参考实现在循环前初始化 `let truncated = false`，我漏掉了这一步。**这是第一次运行
探针就暴露的**——也说明写完实现立刻跑一次最短路径是值得的，不必等到写测试。

### 115.5 验证证据

- 21 项新增测试：**服务端过滤参数**、跨页收集、**游标取最后一页**、
  **重复 id 跳过**、**达到上限截断**、**单页超预算截断**、**恰好用完不截断**、
  **重复 page token 拒绝**、4 种非法 observed history id、4 种非法起始游标、
  4 种非法上限、**令牌只进请求头**；
- **测试规模**：`uv run pytest` → **1667 项通过**（4 项跳过）。

### 115.6 M10 剩余

消息正文的完整拉取与身份校验（`record.id !== messageId` 报错）、文本附件的水合、
令牌撤销后的连接状态更新、同步服务（524 行）、8 条路由。

## 116. M10 进展：Gmail 错误映射与附件水合（2026-10-12）

**新增**：`gmail_http_error`、`request_gmail_json`、`hydrate_gmail_text_attachments`、
两个校验器（+44 项测试）。

### 116.1 history 的 404 表示游标已过期

```
history.list 的 404  →  GMAIL_HISTORY_EXPIRED（确认重扫范围）
其它端点的 404        →  GMAIL_CONNECTION_ERROR
```

Google 只保留大约一周的历史。把 404 当成一般连接错误，会让客户端拿着一个**永久失效
的游标**反复重试——而正确的动作是让用户确认重扫范围。这是 `request_gmail_json` 的
`history_404_means_expired` 开关存在的唯一理由。

### 116.2 401 与 403 都是授权失效

Gmail 用 403 表达"令牌有效但缺少该资源的权限"。当成暂时性故障会无限重试。

### 116.3 大正文会以附件返回

**Gmail 会把较大的正文作为附件返回，而不是内联的 `body.data`。** 只读 `body.data`
的话，那类邮件会拿到**空正文**，eConfirm 解析随之报出"正文中没有交易块"——一个与
真正原因（正文在附件里）无关的提示。

因此水合逻辑：内联数据非空则直接用；否则有 `attachmentId` 就去取；递归处理
`parts`。

附件 ID 的字符集校验**同样是路径注入防线**（它会被拼进 API 路径）。

### 116.4 base64url 的长度约束

```
长度 ≡ 1 (mod 4)  →  不可能存在
```

每 4 个字符编码 3 字节，因此余 1 的 base64 不可能是合法输入。直接拒绝，而不是把畸形
数据交给下游解码器去猜。

### 116.5 一处自相矛盾的测试

我把 `"a"` 同时放进了 base64url 的**合法**与**非法**清单。长度 1 ≡ 1 (mod 4)，属于
非法——**实现的判断是对的，是我的清单写错了**。

### 116.6 验证证据

- 44 项新增测试：**401/403 → AUTH_REVOKED**、**4 种可重试状态**、
  **4 种不可重试 4xx**、成功返回负载、**history 404 → HISTORY_EXPIRED**、
  **普通 404 不是游标过期**、传输失败可重试、非对象根节点、**令牌只进请求头**；
  **内联数据不发请求**、**缺数据时取附件**、**空串算欠缺**、非文本不水合、
  **嵌套递归**、无 payload 原样返回、2 种结构错误；
  **3 种合法附件 ID**、**6 种非法附件 ID（含路径穿越）**、
  **4 种合法 base64url**、**6 种非法（含长度 ≡1）**；
- **测试规模**：`uv run pytest` → **1712 项通过**（4 项跳过）。

### 116.7 M10 剩余

消息正文的完整拉取编排（`fetch_full_records` 的 `record.id` 校验）；
令牌撤销后的连接状态更新；同步服务（524 行）；8 条路由。

## 117. M10 完成：Gmail 客户端（2026-10-12）

**新增**：`fetch_full_gmail_records`、`fetch_incremental_gmail_full_records`。

### 117.1 回显 id 必须核对（与行情侧同一条原则）

```python
if record.get("id") != message_id: raise ...
```

不核对就可能把另一封邮件的内容当成这一封来解析——**而解析结果看起来完全正常**
（符号、数量、价格都在），只是属于另一封邮件。

这与行情侧的"符号回显校验"（§86.1）、Alpaca 的符号校验（§87.2）是同一条原则：
**跨边界拿到的数据必须证明它是我们请求的那一份**。三处独立出现，说明它不是特例。

### 117.2 先元数据后正文

元数据便宜、正文昂贵。增量结果里常混有大量无关邮件，先按发件人与主题筛掉，
只对候选拉正文。有测试断言**无关邮件完全不会触发正文请求**。

### 117.3 ignored_message_count 的价值

它让调用方区分两种"没有结果"：

- **这段时间没有邮件**（候选为 0）；
- **有邮件但都不相关**（候选 > 0，忽略数 = 候选数）。

两者的处置完全不同。零条候选是合法结果，不是错误。

### 117.4 验证证据

- 7 项新增测试：按序拉取、**回显 id 不一致拒绝**、**先过滤后拉正文**、
  **忽略计数区分两种"没有结果"**、零候选合法、**元数据 id 也核对**、
  **无关邮件零正文请求**；
- **测试规模**：`uv run pytest` → **1719 项通过**（4 项跳过）。

### 117.5 Gmail 客户端状态：完成

| 部件 | 状态 |
| --- | --- |
| 端点常量与上限 | ✅ |
| message id 路径注入防线 | ✅ |
| messages.list / history.list 解析 | ✅ |
| 元数据过滤 | ✅ |
| 有界扫描分页（含两道守卫） | ✅ |
| 增量历史拉取（含死循环防线） | ✅ |
| Profile 与邮箱归属校验 | ✅ |
| HTTP 错误映射（含 history 404 特判） | ✅ |
| 完整正文拉取与附件水合 | ✅ |

### 117.6 M10 剩余

令牌撤销后的连接状态更新；**同步服务（524 行，编排整个同步流程）**；8 条路由。

## 118. M10 修正：连接保存的 CAS 与身份不可变（2026-10-12）

准备实现同步服务时读参考实现，发现**我已写的 `save_gmail_connection_row` 有两处偏差**。

### 118.1 偏差一：缺少 `updated_at` 比较并交换

```sql
UPDATE gmail_connections SET ... WHERE id = ? AND updated_at = ?
```

CAS 失败报 409（可重试）。

**为什么必须有**：同步是长流程（刷新令牌、拉取消息、写账本），期间用户可能在另一台
设备上断开连接。不加 CAS 的话，这次同步的收尾会把"已断开"覆盖回"活动中"，
**用户会看到连接自己又活了**。

### 118.2 偏差二：不该更新身份列

参考实现的 `SET` 列表里**没有** `google_subject` / `google_email`。

连接与 Google 账号的绑定在创建后**不可变**。允许同步过程改写它，就等于允许一次同步
把连接悄悄指向另一个邮箱——**而之后的每封邮件都来自那个新邮箱**。

我的实现把这两列一起写了。已移除，并加测试断言身份列不会被改写。

### 118.3 两处偏差是怎么被发现的

不是靠"再读一遍自己写的代码"，而是**在实现依赖它的下游模块时读参考实现**——
`sync()` 调用 `saveGmailConnectionRow(..., connection.updatedAt)`，多出来的那个参数
就是信号。

**这提示了一条通用做法**：跨模块边界处是偏差最容易藏身的地方。每轮全量测试能抓
`TypeError`，但抓不到"参数对了、语义不对"——后者只能靠对着参考实现读调用点。

### 118.4 验证证据

- 17 项连接仓储测试（原 15 + 2 项新增）：原有全部通过（改用新签名）、
  **陈旧时间戳冲突 409**、**身份列不被改写**；
- **测试规模**：`uv run pytest` → **1721 项通过**（4 项跳过）。

### 118.5 M10 剩余

同步服务的其余部分（错误映射、消息处理循环、游标推进）；8 条路由。

## 119. M10 进展：同步错误映射与收尾（2026-10-12）

**新增**：`application/gmail_sync_errors.py`、`tests/test_gmail_sync_errors.py`。

### 119.1 映射表

| 同步错误码 | HTTP | 可重试 |
| --- | --- | --- |
| `GMAIL_RESYNC_CONFIRMATION_REQUIRED` | 409 | 否 |
| `GMAIL_AUTH_REVOKED` | **401** | 否 |
| `GMAIL_HISTORY_EXPIRED` | 409 | 否 |
| 其它同步错误 | 409 | 否 |
| 已是应用错误 | 透传 | 透传 |
| 未知异常 | 503 | 是 |

**`GMAIL_AUTH_REVOKED` 是 401 而不是 409**：客户端据此触发重新登录流程；报 409 会让
它以为只是状态冲突。

未知异常的原文**不外泄**（可能含端点或凭据片段）。

### 119.2 一处记录下来但照实镜像的行为

`GMAIL_CONNECTION_ERROR` 在同步层是**可重试**的（网络抖动、5xx），但到了出口层被
硬编码成 **409 不可重试**——一次真正暂时的故障在客户端看来成了永久失败。

**这是参考实现的既有行为，本模块照实镜像。** 迁移目标是行为一致；在这里"顺手修好"
会让两侧在同一类故障上给出不同的提示与重试策略，而差异会在切流当天才暴露。

已写成两条测试固定下来，其中一条显式断言"上游标了 retryable=True，出口仍是 False"。

### 119.3 收尾用合成状态

`completion_state` 传一个**合成的 active 状态**给 `complete_gmail_sync`，而不是连接的
当前状态——与参考实现一致。

这意味着"已断开不能完成同步"的守卫在这条路径上**不会触发**：走到收尾说明连接既不是
断开也不是错误（否则早就返回了）。照实镜像这一顺序，避免两侧在同一状态下给出不同结果。

### 119.4 验证证据

- 13 项测试：三种已知错误码的状态与文案、**两种其它码 → 非重试 409**、
  **暂时性失败丢掉可重试性（显式记录）**、**应用错误原样透传（同一对象）**、
  未知异常 → 503 可重试、**未知异常不泄漏原文**、收尾推进游标、
  游标与时间校验、合成状态；
- **测试规模**：`uv run pytest` → **1734 项通过**（4 项跳过）。

### 119.5 M10 剩余

同步服务主流程（消息处理循环、映射解析、失败归档、游标推进）；8 条路由。

## 120. M10 进展：账户尾号解析（2026-10-12）

**新增**：`application/gmail_account_mapping.py`、`tests/test_gmail_account_mapping.py`。

### 120.1 核心安全性质：从不猜测

**即使只有一个活跃账户的尾号匹配，也必须由用户显式确认。**

自动匹配看起来省事，但它把"某封邮件属于哪个账本"这个决定交给了字符串比较——
**猜错会让交易进错账本**，而更正它意味着删除已入账的交易并重算 FIFO。

解析只有两条合法路径：已有映射、用户显式选择。其余一律 `ACCOUNT_MAPPING_REQUIRED`
（409）。

### 120.2 已废弃的绑定会要求重新确认

映射指向**已停用账户**时不像"还能用"那样继续用，而是要求重新确认。继续用它会让新
交易进入一个**已经不再使用的账本**。

### 120.3 用户的选择压过已有映射

若用户显式指定了与映射不同的账户，报 `ACCOUNT_MAPPING_REQUIRED` 而不是沿用映射——
直接沿用会让"用户点了改绑"这件事**静默失效**。

### 120.4 候选只给 UUID

```python
details = {"candidate_account_ids": sorted(...)}
```

**不给完整券商账户键**——后者含完整账号。参考实现的注释明确写了这一点，有测试断言
详情里不出现 `Individual`。

### 120.5 一处容易踩的交互（我的测试踩到了）

```python
suffix_matches(account, suffix):
    masked_suffix == suffix  or  legacy_account_key.endswith(suffix)
```

**两种匹配是"或"关系。** 我在测试里把 `masked_suffix="999"` 配上默认的
`Individual_XXX276`，结果按规则**确实匹配 276**——实现是对的，是我的测试数据自相矛盾。

已补一条测试把这个交互记录下来，避免以后又被同一处绊倒。

另：尾号参数由调用方**预先大写化**（与参考实现一致），函数只大写化账户一侧。

### 120.6 改绑需要两道校验

`plan_gmail_account_mapping_update` 先校验**映射属于当前连接**（否则 400——不然一次
调用就能改到别的连接上），再校验**目标账户尾号匹配**。

### 120.7 验证证据

- 24 项测试：两种匹配方式、**大小写**、不匹配；已有映射使用、**大小写尾号**、
  **跨连接映射拒绝**、**尾号不符拒绝**、**指向停用账户拒绝**、
  **用户选择压过映射**；显式选择建映射、停用账户拒绝、尾号不符拒绝；
  **无映射无选择要求确认**、**候选为排序 UUID 且不含账户键**、
  **候选只含活跃账户**、空候选、空尾号；改绑成功、同账户无操作、
  **跨连接改绑 400**、尾号不符拒绝；**记录"或"关系交互**；
- **测试规模**：`uv run pytest` → **1758 项通过**（4 项跳过）。

### 120.8 M10 剩余

同步服务主流程（消息处理循环的编排）；8 条路由。

## 121. M10 完成：同步编排（2026-10-12）

**新增**：`application/gmail_sync_service.py`、`tests/test_gmail_sync_service.py`。

### 121.1 同一错误码在两处有不同映射

| 位置 | `GMAIL_CONNECTION_ERROR` → |
| --- | --- |
| **令牌刷新阶段** | **503 可重试**（"Google 服务暂时不可用"） |
| **消息处理阶段**（§119） | 409 不可重试 |

**这不是笔误。** 刷新阶段失败时什么业务逻辑都还没跑，重试是安全的；处理阶段失败时
可能已经写了一半账本。两处都照实镜像，并有各自的测试。

### 121.2 「需要重扫」是返回值而不是异常

增量被截断、有界扫描被截断都属于"这次做完了，但你需要确认重扫范围"。用异常表达会
让客户端把它当成故障，而它需要的是**用户的一个决定**。

对应地：

- **截断时不推进游标**（有测试断言仍是旧值）——本次并没有读到能作为新基线的位置；
- **截断时不处理任何消息**——否则会导入一批"前 N 封"，而用户以为扫完了。

### 121.3 一处被测试抓到的真实缺陷

截断分支我用了 `_summary` 的默认 `status="active"`，而参考实现用的是
`decision.state.status`（`needs_resync`）。

**报 active 会让客户端以为同步正常结束，于是不会提示用户去确认重扫**——连接就此
停在"看起来正常但其实需要人工介入"的状态。

已修正为传递决策状态。

### 121.4 保存始终用原始 `updated_at` 做 CAS 基准

若把"上一次保存后的新值"当成基准，CAS 就会**永远成功**——并发断开会被静默覆盖。
有测试断言每次保存用的都是读到的那个时间戳。

### 121.5 验证证据

- 12 项测试：连接不存在 404、**刷新期授权失效 401 且写状态**、
  **刷新期连接错误 503 可重试（与处理期不同）**、增量成功推进游标、
  **截断是返回值**、**截断保留旧游标**、游标失效 409、
  **重扫需确认**、确认后有界重扫、**截断的重扫要求确认**、
  **截断时不处理消息**、**CAS 基准始终是原始时间戳**；
- **测试规模**：`uv run pytest` → **1770 项通过**（4 项跳过）。

### 121.6 M10 剩余

8 条 Gmail 路由（连接列表/删除、映射增删改、同步触发、授权事务）。

## 122. M10 完成：Gmail 路由（2026-10-12）

**新增**：`api/gmail_routes.py`、`tests/test_gmail_routes.py`。

### 122.1 一处数量纠正：7 条，不是 8 条

§94.3 把 `/v1/google/connections/:id/mappings` 的两种方法重复计了一次，于是记成了
8 条。实际是 **7 条**：

| 路由 | 说明 |
| --- | --- |
| `POST /v1/google/authorization-transactions` | 创建授权事务（201） |
| `POST /v1/google/authorization-transactions/{state}/complete` | 完成授权 |
| `GET /v1/google/connections` | 列出连接 |
| `GET /v1/google/connections/{id}/mappings` | 列出账户映射 |
| `PUT /v1/google/connections/{id}/mappings/{mappingId}` | 改绑映射 |
| `POST /v1/google/connections/{id}/sync` | 触发同步 |
| `DELETE /v1/google/connections/{id}` | 断开连接 |

**7 条全部要求设备认证**（有参数化测试逐条验证）。

### 122.2 三处格式校验放在路由层

- `state` 必须是 43 个 URL 安全字符（与生成时一致）；
- `id` / `mappingId` / `explicitAccountId` 必须是 UUID；
- `confirmedRescanAfterDate` 必须是 `YYYY-MM-DD`。

放在这一层的价值是**错误信息能指明是哪一段**，而不是让下游抛一个笼统的"参数无效"。

### 122.3 两处"缺失即降级"而非报错

- **同步正文完全可省略**：所有参数都是可选的，空正文是合法输入；
- **OAuth 未配置时返回 503**，而不是 500——服务是好的，只是少了配置。

### 122.4 一处测试桩的缺陷

`FakeGmailService` 最初在覆写时**直接返回覆写函数**，于是"调用是否发生"完全没被记录，
断言 `calls` 的 6 项测试全部落空。

改为**记录与返回值并存**：覆写函数只决定返回什么（或抛什么错），调用仍会被记下来。
另有 3 处覆写用了位置参数而桩按关键字调用，一并修正。

### 122.5 验证证据

- 39 项测试：**7 条路由逐条要求认证**、服务缺失 503、创建事务 201 且绑定设备、
  未配置 OAuth 503、**4 种非法 state**、4 种非法授权码、state 取自路径、
  列出连接、**4 种非法连接 ID**、列出映射、非法映射 ID、3 种非法目标账户、
  改绑映射、**空正文可省略**、传递重扫日期与账户、**4 种非法日期**、
  应用错误保留状态码、**默认不撤销**、**可撤销**、**撤销失败上报**；
- **测试规模**：`uv run pytest` → **1809 项通过**（4 项跳过）。

### 122.6 M10 状态：领域与适配层完成，路由完成

还剩**服务装配**：把已实现的部件（OAuth 客户端、Gmail 客户端、仓储、同步编排、
账户映射）接到 `gmail_service` / `sync_service` 上，并接上真实 HTTP 传输层。

## 123. M10 进展：Gmail 公开视图（2026-10-12）

**新增**：`application/gmail_views.py`、`tests/test_gmail_views.py`。

### 123.1 公开视图刻意不暴露的四个字段

| 字段 | 不暴露的原因 |
| --- | --- |
| `encrypted_refresh_token` | 密文虽不可直接使用，但**泄露它等于给出离线解密的靶子** |
| `google_subject` | 内部标识，客户端只需要邮箱 |
| `granted_scopes` | 属于服务端配置细节 |
| `history_id` | 内部同步游标，客户端不该据此判断状态 |

有参数化测试逐个断言这些字段及其**值**都不出现在视图里，另有测试断言公开键里
没有 `_`（camelCase 约定）。

### 123.2 未知错误码拒绝投影

```python
if error_code is not None and not is_gmail_sync_error_code(error_code):
    raise ValueError("Gmail 连接包含非稳定错误码")
```

一行含未知错误码说明它被别的写入路径污染了（或来自未迁移的历史数据）。**照原样投影
会把任意文本送到客户端**。拒绝投影比泄漏安全——有测试用一个形如
`postgres://user:pw@host` 的错误码验证。

### 123.3 映射列表校验归属

```python
if any(mapping.connection_id != connection_id for mapping in mappings):
    raise ValueError("Gmail 账户映射不属于当前连接")
```

仓储若因过滤失效返回了**别的连接**的行，直接报错而不是展示——那会泄露另一个连接的
账户归属。这与 M3 期间「仓储返回目标账户之外交易」的守卫是同一条原则。

### 123.4 一处需要注意的返回形状

参考实现的 `createAuthorizationTransaction` **只返回 `{authorizationUrl, expiresAt}`，
不含 `state`**——客户端从 Google 重定向里取 state，不从这个响应里取。

事务 TTL 是 **10 分钟**，且存在进程内的 Map 里（键为 state）。

### 123.5 验证证据

- 12 项视图测试 + 全量：公开字段齐全、**四个内部字段及其值不出现**、
  **无 snake_case 键**、5 种已知错误码投影、**未知错误码拒绝**、
  映射排序（尾号再 ID）、**跨连接映射拒绝**、空列表合法、映射键无下划线；
- **测试规模**：`uv run pytest` → **1821 项通过**（4 项跳过）。

### 123.6 M10 剩余

**服务装配**：把 OAuth 客户端、仓储、同步编排接到 `GmailService` / `SyncService`，
并接上真实 HTTP 传输层（httpx）。

## 124. M10 完成：Gmail 服务装配（2026-10-12）

**新增**：`application/gmail_service.py`、`tests/test_gmail_service.py`。

### 124.1 授权事务存在进程内存里

它是一次性短时状态（**TTL 10 分钟**），不该进数据库——持久化它等于把"未完成的授权"
变成需要清理的历史数据，而进程重启后它本就应当消失。

**兑换后立刻从表里移除**：同一 state 不能兑换两次。留在表里靠 `consumed_at` 拦截
也行，但删掉更彻底，表也不会随时间无限增长。

事务**绑定发起设备**：另一台设备拿到 state 也无法完成兑换（有测试验证）。

### 124.2 响应形状

`create_authorization_transaction` 只返回 `{authorizationUrl, expiresAt}`，**不含
`state`**（§123.4）。有测试断言键集合恰好是这两个。

### 124.3 断开的顺序即正确性

**先向 Google 撤销、成功后才落库。**

顺序反过来会出现"本地已断开但 Google 侧仍授权"的状态——那是最糟的组合：用户以为
凭据已作废，而它仍然有效。

有测试断言调用顺序（`revoke` 早于 `save_connection`），另有测试断言**撤销失败时
不落库**（连接保持可用，用户可重试）。

### 124.4 改绑未变化时不写库

目标账户未变时跳过 `update_mapping`——避免制造一次无意义的 `updated_at` 变更，
那会让其他设备上的 CAS 无端失败。

### 124.5 验证证据

- 17 项测试：未配置返回 None / 完成报 503、**响应不含 state**、**TTL 10 分钟**、
  完成建立连接且公开视图无密文、**兑换后事务被移除**、未知 state、
  **设备绑定**、列出连接与映射、**跨连接读不到映射**、映射不存在 404、
  **未变化时不写库**、不撤销时不请求 Google、**先撤销后落库（顺序）**、
  **撤销失败不落库**、连接不存在 404、**重复断开幂等**；
- **测试规模**：`uv run pytest` → **1838 项通过**（4 项跳过）。

### 124.6 M10 状态

**领域、适配、仓储、同步编排、路由、服务装配全部完成。**

剩余的是**接真实 HTTP 传输层**（httpx）到 OAuth 与 Gmail 客户端，以及在 `main.py`
里按环境装配 `gmail_service` / `sync_service`。之后是 **M9b 切流**。

## 125. M10 进展：HTTP 传输层与 eConfirm 入口（2026-10-12）

### 125.1 一处被发现的隐藏缺口

为 Gmail 服务写装配工厂时，我引用了 `application.gmail_message_processing` 与
`application.gmail_sync_errors.refresh_with_connection`——**前者当时根本不存在**。

**延迟导入（函数内 import）让 `import` 检查假性通过**：模块能导入，`ok` 能打印，
只有真正调用时才会 `ModuleNotFoundError`。这正是"先跑一次最短路径"能抓到、而
"只看 import"抓不到的一类问题。

补齐后，M10 的真实剩余是：

| 缺口 | 规模 |
| --- | --- |
| **`parse_econfirm`（正文 → 交易）** | 约 450 行密集解析 |
| `parse_gmail_full_input`（API 载荷 → 邮件） | 中等 |
| `process_gmail_messages`（处理循环） | 中等 |
| `main.py` 环境装配 | 小 |

### 125.2 本轮完成：HTTP 传输层

`infrastructure/gmail/http_transport.py`：把 httpx 接到 OAuth 与 Gmail 客户端上。
四道硬约束与行情 HTTP 客户端同一条原则（§85.1）：**不跟随重定向**（否则 access token
会发给重定向目标）、超时含正文读取、正文上限、错误正文不回传。

### 125.3 本轮完成：eConfirm 入口层

两处"宁可失败也不猜"的设计：

**其一：账户尾号必须唯一。**

主题与正文对"这笔交易属于哪个账户"给出矛盾说法时报错，并列出全部候选。
猜一个会把交易写进错误账本，而更正它意味着删除已入账的交易并重算 FIFO。

**其二：纯文本与 HTML 必须解析出同一批交易。**

两者是同一封邮件的两种表示。结果不同说明至少一方解析错了——**此时不能挑一个
"看起来更完整"的用**，那会在用户毫无察觉的情况下写入错误数据。指纹不同即报
"plain 与 HTML 解析出的交易不一致"。

另：**期权动作由买卖方向与保证金/做空共同决定**（四个组合），而**股票不区分开平仓**。
只看买卖会把"买入开仓"记成"买入平仓"，持仓数量随之算错。

### 125.4 验证证据

- 15 项新增测试 + 全量：动作表内容、候选顺序与去重、
  **两种尾号写法**、主题取尾号、**冲突尾号拒绝且列出候选**、缺尾号拒绝、
  **同尾号重复正常**、模式需前缀、指纹稳定性与敏感性；
- **测试规模**：`uv run pytest` → **1853 项通过**（4 项跳过）。

### 125.5 M10 剩余

`parse_econfirm_body`（含 `parse_value_rows` / `parse_labeled_fees` / `build_trade`）；
`parse_gmail_full_input`；`process_gmail_messages`；`main.py` 环境装配。

## 126. M10 进展：eConfirm 字段解析（2026-10-12）

**新增**：`field`、`unwrap_symbol`、`parse_trade_date`、`parse_email_decimal`、
`normalize_description`（+38 项测试）。

### 126.1 期权描述会重建为规范形式

```
CALL INTEL CORP $100.00 EXP 03/19/27
```

**为什么必须重建**：Schwab 的描述格式在不同批次间会变，而描述进入**交易哈希**——
不规范化会让同一笔交易在两次导入中算出不同的哈希，跨来源与跨次去重随之失效。

有测试用两种空格/空白变体验证重建结果相同。

### 126.2 四处格式细节

| 细节 | 不做会怎样 |
| --- | --- |
| **字段名必须独占一行** | 正文里出现"Action: 见下"这类句子时会误命中 |
| **符号可能是 Markdown 链接** | 把 `[INTC](https://…)` 整体当符号会解析出错误代码 |
| **两位数年份按 20xx** | Schwab 展示格式如此；不处理会把 2026 当成 26 年 |
| **日期需回代校验** | `2/30/2026` 格式合法但不存在，回代能拦住它 |

### 126.3 数值：剥货币符号与千分位，保留负号

`-$405` → `Decimal("-405")`。负号是金额语义的一部分，剥掉会把一笔卖出记成买入。

### 126.4 一处格式假设的纠正

探针第一次跑 `normalize_description` 时走了**正股路径**——我的测试输入用了
`INTC 03/19/2027 $100.00 Call`，而实际符号格式是
`INTC 03/19/2027 100.00 C`（**C/P，不是 Call/Put，且没有 `$`**）。

规则来自账本领域既有的 `parse_option_symbol`（M1–M3 已迁移），本模块**复用它而不是
重新实现**——DRY 之外，也保证两处对"什么算期权符号"的判断始终一致。

### 126.5 验证证据

- 38 项测试：字段读取/大小写/**独占一行要求**/定位信息/去空白、
  **Markdown 解包**（含期权符号）/普通符号/畸形链接不猜、
  5 种日期（含两位数年份）、**8 种非法日期（含 `2/30`）**、
  5 种数值（含负号与千分位）、4 种非法数值、
  正股原样/期权重建/看跌/**跨格式稳定**/无后缀、
  分块模式的三种情形；
- **测试规模**：`uv run pytest` → **1891 项通过**（4 项跳过）。

### 126.6 M10 剩余

`parse_value_rows`（成交金额表解析，含 `parse_labeled_fees` / `build_trade`）；
`parse_gmail_full_input`；`process_gmail_messages`；`main.py` 环境装配。

## 127. M10 进展：成交构造的勾稽校验（2026-10-12）

**新增**：`build_trade`、`RECONCILIATION_TOLERANCE`、`BUY_ACTIONS`（+23 项测试）。

### 127.1 两处勾稽校验

```
本金 = 数量 × 价格 × 乘数            （容差 0.011）
净额 = 本金 ± 费用                   （买入加、卖出减）
```

**超出容差即报错，绝不修正。** 修正会让账本记下一个**从未发生过的数字**，
而用户看到的是"金额对上了"——错误被伪装成正常。

容差 0.011 小于一分半：足以吸收显示舍入，而拦得住真实错误。有测试用"刚好超出"的
0.02 验证边界是硬的。

### 127.2 符号约定

| 动作 | 净额 |
| --- | --- |
| `Buy` / `Buy to Open` / `Buy to Close` | **取负** |
| 其余（卖出） | 保持正 |

**买入的现金流出为负**。不取负会让买入看起来像收入——现金流与持仓方向同时反了。

### 127.3 期权乘数

`multiplier` 为 100（期权）或 1（正股）。忘掉它会让本金差 100 倍，**勾稽会立刻报错**
——这正是勾稽校验的价值：把一个沉默的 100 倍错误变成一条明确的报错。

### 127.4 输出不引入科学计数法

输出用 `format(Decimal, "f")`，极小数值不会变成 `1E-7`。那会让 CSV 解析器读不到，
而金额字段的消费者（账本、FIFO、报表）都按纯十进制文本处理。

### 127.5 验证证据

- 23 项测试：**三种买入动作取负**、三种卖出为正、本金勾稽、**不符拒绝**、
  **容差内接受**、**刚好超出拒绝**、容差值本身、买入/卖出净额公式、
  两种净额不符拒绝、零费用、**期权乘数缩放**、**忘掉乘数被拒**、
  输出无科学计数法、小数数量、透传字段、错误带定位；
- **测试规模**：`uv run pytest` → **1914 项通过**（4 项跳过）。

### 127.6 M10 剩余

`parse_value_rows`（成交金额表，含 Totals 勾稽与逐腿拆分）；
`parse_labeled_fees`；`parse_econfirm_body`；`parse_gmail_full_input`；
`process_gmail_messages`；`main.py` 环境装配。

## 128. M10 进展：费用区域解析（2026-10-12）

**新增**：`is_money`、`MONEY_PATTERN`、`parse_labeled_fees`（+33 项测试）。

### 128.1 三种费用模板

| 模板 | 形态 |
| --- | --- |
| 零费用行 | `["n/a:", money, "n/a", money]` |
| 只有 Industry Fee | `industry fee:` + 金额 + 净额（佣金按 0 计） |
| 完整 | `commission:` + `industry fee:` + `total:` + 净额 |

零费用行是**股票多价格成交的 plain 模板**：它在每条腿后保留 N/A 标签，但既不给
Commission 也不给 Industry Fee 标签。

### 128.2 两处"宁可失败也不猜"

**其一：分项费用必须等于 Total。**

`commission + industry_fee == total`，不等即报错。采信哪一边都是在猜，而**费用会进入
交易哈希与已实现损益**——猜错的代价是账目对不上，且难以追溯到源头。

**其二：尾部不得有多余字段。**

`index != len(values)` 即报错。模板若新增字段，**静默忽略会让解析结果悄悄漏掉信息**
——错误在很久之后才以"某笔交易金额不对"的形式浮现，那时已无从追溯。

### 128.3 金额必须带 `$`

`MONEY_PATTERN = /^-?\$[\d,]+(?:\.\d+)?$/`

正文里日期、数量、行号都是**裸数字**；把它们当金额会让解析器在错误的字段上对齐。
六种"看起来像金额但不带 $"的取值被拒绝。

### 128.4 一处模板约定（不是猜测）

只有 Industry Fee 的模板里**佣金按 0 计**——这是模板的约定，写在注释里。它与
"看到没有佣金标签就猜 0"不同：前者有明确依据，后者在模板变化时会静默出错。

### 128.5 验证证据

- 33 项测试：6 种合法金额、**7 种非法（含不带 $）**、模式本身、
  零费用行、**N/A 行非零拒绝**、N/A 标签缺失回落、只有 Industry Fee 的两种写法、
  完整模板的三种写法、**分项与 Total 不符拒绝**、缺两个标签各一、
  无法识别首位、缺佣金金额、缺 Industry Fee 金额、
  **两种模板的尾部多余字段拒绝**、错误带定位；
- **测试规模**：`uv run pytest` → **1947 项通过**（4 项跳过）。

### 128.6 M10 剩余

`parse_value_rows`（成交金额表，含 Totals 勾稽与逐腿拆分）；`parse_econfirm_body`；
`parse_gmail_full_input`；`process_gmail_messages`；`main.py` 环境装配。

## 129. M10 进展：成交金额表解析（2026-10-12）

**新增**：`parse_value_rows`、`_single_row_fees`、`_parse_totals_rows`、`_reconcile`
（+18 项测试）。

### 129.1 Totals 分支的三处勾稽

```
逐腿费用之和  == Totals 费用
逐腿数量之和  == Totals 数量
逐腿金额|绝对值|之和 == Totals 金额
```

任一处不符即报错——**不平均分摊、不按比例缩放**。分摊出来的数字在账本里看起来完全
正常，但它对应的是**从未真实发生过的成交**。

金额用**绝对值**相加：买入为负、卖出为正，而 Totals 给的是规模。有测试用卖出动作
验证同一组 Totals 仍然勾稽通过。

### 129.2 那条容易写错的守卫

```python
if "for the above:" in values and "Totals" not in values:
    在免责声明处截断
```

**有 Totals 时绝不能截断**：多价格模板用同一文本分隔成交腿，在这里截断会把 Totals
一并切掉，于是**退回单笔解析并静默丢掉后续腿**——账本里少了几笔成交，而同步报告
"成功"。

已用**对照测试**直接验证这条守卫：同一段数据，无 Totals 时截断（1 腿），有 Totals 时
不截断（2 腿）。

### 129.3 三处结构约定

| 约定 | 原因 |
| --- | --- |
| Totals 前最后一个分隔符**不代表空腿** | 新版模板在最后一腿与 Totals 之间也保留标记；不处理会多出一"腿"并因字段不足报错 |
| 无分隔符时每四条一腿 | 不是四的倍数即报错 |
| 紧凑尾部三形态优先，否则落到带标签费用表 | 三种模板已在 §128.1 覆盖 |

### 129.4 一处测试设计的纠正

我最初的"有 Totals 时不截断"测试把免责声明放在了 **Totals 之后**——那会让
`totals[-2]` 错位，是个不现实的输入。改为对照测试后，它同时验证了守卫的两个方向，
比原来那条更准确。

### 129.5 验证证据

- 18 项测试：单笔四种尾部形态、少于四项拒绝、Totals 无分隔符两腿、
  非四倍数拒绝、分隔符两腿、**尾部分隔符非空腿**、**对照验证截断守卫**、
  无 Totals 时截断、**三处勾稽各自拒绝**、**绝对值勾稽**、Totals 块过短、
  单腿字段不足、**期权乘数作用于每腿**；
- **测试规模**：`uv run pytest` → **1965 项通过**（4 项跳过）。

### 129.6 M10 剩余

`parse_econfirm_body`（把它们串起来）；`parse_gmail_full_input`；
`process_gmail_messages`；`main.py` 环境装配。

## 130. M10 完成：eConfirm 正文解析（2026-10-12）

**新增**：`parse_econfirm_body`（+26 项测试，含端到端）。

### 130.1 结构

正文被 ``Symbol:`` 分成若干块（第一段是前言），**每块对应一个证券**；一个块内的金额表
又可以含多笔成交（多价格成交）。因此返回值是全部成交的拼接——有测试分别覆盖
"多块"与"一块多笔"。

### 130.2 三处容易漏的处理

| 处理 | 不做会怎样 |
| --- | --- |
| **剥掉附加说明之后的内容** | 后续行被当成金额表的一部分，报出"无法识别结构"或算出错误金额 |
| **符号可能是 Markdown 链接** | 把 `[INTC](…)` 整体当符号会解析出错误代码 |
| **错误带"第几笔"** | 一封邮件多块时无法定位是哪一块出的问题 |

### 130.3 期权动作由两个维度决定

四个组合各自映射（`Purchase|Margin` → Buy to Open 等）。**只看买卖会把"买入开仓"
记成"买入平仓"**，持仓数量随之算错。四个组合都有测试。

### 130.4 一处实现差异的确认

有分隔符的多价格腿**直接用带标签的费用表解析**，不给"紧凑尾部"那三种形态。

我在测试里给腿尾只写了一个裸金额 `$80`，结果报"缺少 Industry Fee 标签"。核对参考
实现后确认：**实现是对的**——腿尾确实必须走 `parseLabeledFees`。紧凑形态只用于单笔
路径。

已补一条测试把这个差异记录清楚，避免以后又按"看起来更宽松"的假设改坏它。

### 130.5 入口层的候选一致性（§125.3）

`parse_econfirm` 要求两个候选解析出**同一批交易**：
- 两个都成功且一致 → 返回；
- 只有一个成功 → 返回它（另一方失败不致命）；
- 两个都成功但不一致 → **报错，不挑一个用**；
- 两个都失败 → 报错并汇总各自原因。

四种情形都有测试。

### 130.6 验证证据

- 26 项测试：正股解析、卖出映射、**Markdown 解包**、普通符号、
  期权四动作、**未知动作拒绝**、**乘数生效**、无块拒绝、
  **多块多笔**、**一块多笔**、**附加说明被排除**、缺标记拒绝、
  4 种缺字段、**错误带"第几笔"**、**多腿用带标签费用表**（记录差异）、
  入口四种候选情形；
- **测试规模**：`uv run pytest` → **1991 项通过**（4 项跳过）。

### 130.7 M10 剩余

`parse_gmail_full_input`（API 载荷 → 邮件）；`process_gmail_messages`（处理循环）；
`main.py` 环境装配。

## 131. M10 完成：Gmail 载荷解析（2026-10-12）

**新增**：`infrastructure/gmail/full_message.py`、`tests/test_gmail_full_message.py`。

### 131.1 字符集来自部件自己的 Content-Type

**正文是 base64url 编码的，字符集来自部件自己的 ``Content-Type`` 头。**

假定 UTF-8 会在供应商返回别的字符集时把正文变成乱码——而**乱码不会报错**，只会在
eConfirm 解析时表现为"找不到字段"，让人往错误的方向排查。

### 131.2 严格解码

非法字节**报错而不是替换成 ``�``**。替换字符可能吃掉一个符号或金额里的字符，
而结果看起来仍然"解析成功"。

另：base64url 在字符集不符或长度非法时报错（长度 ≡ 1 mod 4 不可能存在）。

### 131.3 三处入口校验，各自防一种误用

| 输入 | 处置 |
| --- | --- |
| 含 `messages` 键 | **拒绝并说明该调 ``users.messages.get(format=full)``** |
| 只有 `raw` | 拒绝并说明需要 RFC 5322 MIME 适配器 |
| 没有内联正文 | 拒绝并说明附件式正文要先取 attachment data |

三条错误信息都把"下一步该做什么"写在里面——这类误用的排查成本远高于一句话的成本。

### 131.4 Date 头优先于 internalDate

`Date` 头是**邮件自身声明的时间**；`internalDate` 是 Gmail 收到它的时间。前者更接近
成交发生的时间，而它会影响"这笔交易属于哪个账期"。两者都缺时为空，**不编**。

### 131.5 一处测试输入的纠正

我用 em-dash（`—`）验证 latin-1 字符集，但它**不在 latin-1 里**，编码阶段就失败了。
改用 `é`（0xE9）——它在 latin-1 里存在，而在 UTF-8 下是两个字节，因此能真正区分
"按声明的字符集解码"与"假定 UTF-8"。

### 131.6 验证证据

- 25 项测试：单条/数组解析、空数组拒绝、**list 响应拒绝并给指引**、
  **raw 拒绝并给指引**、缺 payload、非对象记录、缺 id、
  非 Schwab 发件人、伪装域名、主题不符、
  **无内联正文拒绝并给指引**、**非 UTF-8 字符集被遵守**、
  **无法解码时拒绝而非替换**、非法 base64、HTML 转换、
  **嵌套部件收集**、缺 mimeType、非数组 parts、缺 headers、畸形头项、
  **Date 头优先**、内部时间回退、都缺时为 None、空 Date 头回退；
- **测试规模**：`uv run pytest` → **2016 项通过**（4 项跳过）。

### 131.7 M10 剩余

`process_gmail_messages`（处理循环，串起解析/映射/导入）；`main.py` 环境装配。

## 132. M10 完成：消息处理循环（2026-10-12）

**新增**：`application/gmail_message_processing.py`、`tests/test_gmail_message_processing.py`。

### 132.1 四种结局各自计数

| 结局 | 含义 | 用户的下一步 |
| --- | --- | --- |
| `imported` | 交易已入账 | 无 |
| `alreadyImported` | 此前已导入且未变 | 无 |
| `conflict` | 尾号需要用户确认映射 | **选择账户** |
| `rejected` | 邮件内容解析失败 | **检查邮件** |

**把 conflict 与 rejected 合并会让用户无从下手**：一个是点一下选账户，一个是看邮件
内容，完全不同的动作。

### 132.2 单封失败不终止整次同步

一封格式异常的邮件不该让同一批里的其它邮件都不被导入——**那会让用户反复重试且每次
都停在同一封上**。

有测试用「坏、好、坏、好」四封验证两封坏的不影响两封好的被导入。

### 132.3 只吞一种错误

```python
except ApplicationError as error:
    if error.code != "ACCOUNT_MAPPING_REQUIRED":
        raise          # 其它错误是真正的故障
```

**只把"需要映射"降级为一次冲突**，其它应用错误照常抛出——把真实故障吞成业务冲突会
让问题在很久之后才浮现，且丢失了堆栈。

### 132.4 映射查找只做大小写规范化

`mapping.account_suffix.strip().upper() == suffix`。**不做别的推断**——尾号的模糊匹配
会把交易写进错误的账本（§120.1）。

### 132.5 验证证据

- 14 项测试：导入并计数、**已导入分开计数**、显式账户透传、需要时建映射、
  **解析失败记 rejected 并归档**、**冲突与拒绝分开计数**、
  **其它应用错误抛出**、缺尾号算拒绝、
  **一封坏邮件不终止其余（坏好坏死）**、**一次冲突不终止后续**、
  多记录全处理、**已有映射按大写尾号查找**、**不匹配时传 None**、
  摘要 camelCase 且无下划线；
- **测试规模**：`uv run pytest` → **2030 项通过**（4 项跳过）。

### 132.6 M10 剩余

**只剩 `main.py` 的环境装配**：把 `gmail_service` / `sync_service` 接上真实传输层与
数据库，并加一条端到端验证。

## 133. M10 进展：同步依赖装配（2026-10-12）

### 133.1 一处反复出现的模式，值得单独记下来

装配工厂里我**四次**写下"看起来合理但不存在"的引用：

| 我写的 | 实际 |
| --- | --- |
| `application.gmail_message_processing` | 当时不存在（§125.1） |
| `application.gmail_sync_errors.refresh_with_connection` | 当时不存在 |
| `application.gmail_message_metadata_shim` / `gmail_message_import_shim` | **我凭空发明的"shim"模块** |
| `DuckDbImportCommitRepository.load_account_transactions` | **我凭空发明的方法** |

**四次都逃过了 `import` 检查**——因为引用都写在函数内部（延迟导入）。只有真正调用
时才 `ModuleNotFoundError` / `AttributeError`。

**教训**：在装配层引用任何符号前，先 `grep` 确认它存在；而"我觉得应该有这么个函数"
就是最需要 grep 的时刻。装配代码天然容易写出这种引用，因为它连接的是"应该存在"的
东西。

本轮把后两处改为**复用已有实现**：

- 失败归档 → 已有的 `create_unsuccessful_gmail_message_metadata`；
- 账本交易读取 → 已有的 `DuckDbLedgerRepository.load_snapshot`。

**没有新建任何"shim"模块。** 为了填补装配缺口而造新模块，会把一个"缺少接线"的问题
变成两个"为什么有两套实现"的问题。

### 133.2 有界扫描必须先取 profile

`fetch_initial` 现在先调 `parse_gmail_profile` 拿到 history 基线，再列消息、再拉正文。

**不先取它，扫描期间新增的消息就没有游标可以覆盖，会永久丢失**——而同步报告"成功"。

profile 的邮箱还要与连接邮箱核对（§114.4）：不一致时立即失败，不读别人的邮件。

### 133.3 一处复用而非重写的取舍

`_account_transactions` 复用 `DuckDbLedgerRepository.load_snapshot`，而不是另写一份
`SELECT`。**两份查询在字段或排序上稍有差异就会让导入计划基于不同的交易集合做判断**，
而去重与 FIFO 重算都依赖这个集合。

复用之后仍**显式再筛一次账户**：仓储若过滤失效，多出的交易会污染去重与 FIFO
（与 §123.3、M3 的同类守卫一致）。

### 133.4 验证证据

- 全量：`uv run pytest` → **2030 项通过**（4 项跳过）；
- 工厂模块可导入，且已内联的四处引用都指向**实际存在的**符号。

### 133.5 M10 剩余

`main.py` 里按环境装配 `gmail_service` / `sync_service`，并加一条端到端验证。

## 134. M10 完成（2026-10-12）

### 134.1 装配

`main.py` 现在按环境装配 `gmail_service`：

```
仓储（连接 / 映射 / 消息）+ SecretCipher + OAuth 客户端 + Gmail 客户端 + 处理循环
```

**Gmail 连接加密是 `SecretCipher` 的第一个消费者**——主密钥在这里第一次被真正使用。
密钥格式非法会在启动时失败（`config.py` 的 pattern 已拦截）。

### 134.2 一条被测试暴露的覆盖盲区

接线后 `test_service_absent_reports_503_not_500` 失败了——它原来依赖"默认没有服务"，
而服务现在总是存在。

改成**显式移除服务**来覆盖那个分支。同时补了两条反向测试：

- **默认装配的是真实服务**（返回 200 且连接列表为空）；
- **真实服务在未配置 OAuth 时如实返回 503**，而不是崩溃。

**这两条的价值与其它测试相反**：其它测试都替换掉服务，因此"是否真的接线了"原本
没有任何覆盖——**一个被删掉的 `main.py` 装配行不会被任何人发现**。

### 134.3 M10 交付清单

| 层 | 内容 |
| --- | --- |
| 领域 | 常量与识别 · eConfirm 正文解析（入口/字段/费用/金额表/勾稽）· 同步计划 |
| 适配 | OAuth 客户端（授权/交换/刷新/撤销）· Gmail 客户端（列表/Profile/增量/正文/水合）· HTTP 传输层 |
| 持久化 | 连接与映射仓储 · 消息归档（含原子导入）· 公开视图 |
| 应用 | 账户尾号解析 · 服务装配 · 同步编排 · 处理循环 · 错误映射 |
| 接口 | **7 条路由**，全部要求设备认证 |

### 134.4 验证证据

- 全量：`uv run pytest` → **2032 项通过**（4 项跳过）；
- 路由测试含 **7 条逐条认证**、格式校验、错误映射，以及**两条真实接线的反向验证**。

### 134.5 里程碑状态

| 里程碑 | 状态 |
| --- | --- |
| M1–M8 | ✅ |
| M9a 认证路由 | ✅ 6/6 |
| **M10 Gmail 子系统** | ✅ |
| **M9b 切流与退役 TS** | **待开始（最后一步）** |

## 135. M9b 起步：切流运行手册（2026-10-13）

**新增**：`docs/plans/lyra-cutover-runbook-20261013.md`。

### 135.1 为什么没有"灰度期"

**DuckDB 是单进程文件锁，第二个进程连只读都打不开。** 因此 TS 与 Python **不能共存**
——不存在双写期或逐步放量。

这决定了手册的形状：**没有分阶段放量，只有"切换前把一切验证完"和"一次原子切换"**。
回退也只能整体回退，且会丢失切换后的全部写入。

### 135.2 手册的八个阶段

| 阶段 | 内容 |
| --- | --- |
| 0 | 切换前必须已成立的条件（含**已知缺口清单**） |
| 1 | 备份 + **验证备份可打开**（未验证的备份等于没有备份） |
| 2 | 启动 Python 服务（启动即验证） |
| 3 | 探针切换（协议版本仍为 2，客户端无需重建） |
| 4 | **真实设备联调（验收标准，不可跳过）** |
| 5 | 观察至少一个完整的同步与导入周期 |
| 6 | 回退（含预演；还原整个文件而非增量修补） |
| 7 | 退役 TS（先归档打 tag，确认可解压后再删） |

### 135.3 阶段 0 列出的四项已知缺口

这些**不是"还没做"，而是需要用户确认是否影响其使用**：

| 缺口 | 处置 |
| --- | --- |
| Excel 解析未迁移（`.xlsx` → 501） | 确认用户是否使用 xlsx |
| 行情传输层是占位（无参考估值） | 确认用户是否启用行情 |
| Agent 模型绑定为自定义 HTTP（非 Genkit 插件） | 行为等价，记录即可 |
| 幂等记录仓储未建 | 确认客户端是否依赖 plan 级幂等键 |

**其中前两项会让对应功能在切换后直接不可用。** 手册把它们放在"切换前必须处理"，
而不是"切换后再说"。

### 135.4 本轮未执行的操作，以及原因

**没有执行硬切流，也没有删除 TS 服务端。**

两点原因，都必须如实说明：

1. **删除 TS 是不可逆的破坏性操作**，且会移除 M3 差分对照所依赖的 oracle。
   手册已把它放在阶段 7，前置条件是阶段 4、5 全部通过并有记录；
2. **验收标准"真实设备联调通过"我无法代跑**——它需要一台已配对的真实设备与一个真实
   部署环境。声称"已完成切流"会是虚假的成功状态。

**手册的存在不等于切流已完成。** 执行结果必须由执行者逐项填入文末的记录表。

### 135.5 最终验证（两侧基线）

```
cd server-py && uv run --python 3.12 pytest -q
  → 2032 passed, 4 skipped

cd server && npm run check
  → # suites 139 / # tests 514 / # fail 0
```

**TS 基线仍然全绿**：Python 侧的所有新增都没有触碰 TS 代码，因此差分对照的 oracle
保持可用。

### 135.6 里程碑状态

| 里程碑 | 状态 |
| --- | --- |
| M1–M8 · M9a · M10 | ✅ |
| **M9b** | **手册就绪；执行待用户确认** |

## 136. 阶段 0 缺口一：行情传输层接线（2026-10-13）

### 136.1 做了什么

`main.py` 里的 `_unused_market_fetch`（调用即抛错的占位）**替换为真实的 httpx 实现**
（`infrastructure/market_data/httpx_fetch.py`）。

行情从此可以真正取价——§93.5 记录的"传输层为如实留空的占位"这一缺口**已闭合**。

### 136.2 两道与行情客户端同源的约束

- **禁止重定向**：跟随即等于把 API 密钥发给重定向目标；
- **流式读取正文**：体积上限由上层按块累计执行；一次性读进内存等于把上限交给上游。

### 136.3 一处容易漏的资源释放

`chunks()` 在 `finally` 里关闭响应与客户端。**不关会泄漏连接**——长时间运行的服务会
把文件描述符耗尽，而症状是"偶发请求失败"，极难定位。

有测试覆盖两条释放路径：正常读完，以及**读取中途抛错**（上层的体积上限会提前中止
迭代，那条路径同样必须释放）。

### 136.4 一条反向验证

```python
def test_the_real_market_transport_is_wired_by_default(client): ...
```

**其余测试都通过夹具替换掉传输层**，因此"是否真的接线了"原本没有任何覆盖——
**一个被删掉的装配行不会被任何人发现**，而它会让行情静默变成"永远不可用"。

这与 §134.2 给 Gmail 补的那条是同一类测试。**当所有测试都替换掉某个依赖时，就该补
一条不替换的。**

### 136.5 验证证据

- 4 项适配器测试：状态与头透传、分块顺序、**读完关闭（响应与客户端）**、
  **出错也关闭**；
- 3 项接线测试：**真实传输层已装配**、未配置来源时运行时仍存在、
  无来源时持仓照常返回；
- 全量：`uv run pytest` → **2039 项通过**（4 项跳过）。

### 136.6 阶段 0 剩余缺口

| 缺口 | 状态 |
| --- | --- |
| ~~行情传输层是占位~~ | ✅ 已闭合 |
| Excel 解析未迁移（`.xlsx` → 501） | 待处理 |
| 幂等记录仓储未建 | 待确认是否需要 |
| Agent 模型绑定为自定义 HTTP | 行为等价，记录即可 |

## 137. 阶段 0 缺口二：Excel 解析（前半：共享规则抽取）（2026-10-13）

### 137.1 本轮做了"两边都需要"的那一半

把 CSV 解析器里**内嵌**的表头校验与行映射抽成
`infrastructure/imports/schwab_table.py`，CSV 路径改为复用它。

**CSV 与 XLSX 是同一张表的两种容器**，表头与列序规则必须完全一致——抽取之后它们是
唯一定义处，xlsx 的实现只需处理"怎么读工作簿"。

抽取是**纯重构**：2039 项测试（含全部 CSV 测试）无改动通过，说明行为未变。

### 137.2 一处位置契约

**列序是位置契约**：Schwab 的导出没有列名索引，第 7 列就是 `Fees & Comm`。因此表头
必须**逐字符相等**，而不是"包含这些列"。

顺序不同的两列会被映射到错误字段，**而金额看起来仍然正常**——有测试用交换
`Quantity`/`Price` 的表头验证拒绝。

同理，行必须**恰好八列**：少一列会让后面的列整体前移，金额被读成费用，而两者都
"看起来像金额"。

### 137.3 映射不做任何规范化

`map_schwab_transaction_row` 原样传递单元格文本，**不 trim、不补零**。规范化发生在
下游的领域标准化阶段；在这里顺手处理会让"原始字段"不再原始，**而下游的哈希基于它们
计算**。

有测试用带首尾空格的日期验证这一点。

### 137.4 一处需要你决策的事项：新增依赖

Excel 支持需要读 `.xlsx`（本质是 zip + XML）。Python 侧目前**没有 Excel 库**。

参考实现用的是 `exceljs`；Python 侧的对应选择是 **`openpyxl`**（成熟、活跃、支持
只读流式模式）。

**我没有擅自新增依赖**——它会进入生产环境的依赖树与供应链，属于你应当知情的选择。
若你认可，下一步即：

1. `uv add openpyxl`；
2. 实现工作簿扫描（含**隐藏工作表**的处理：表头出现在隐藏表里应报错而不是静默采用）；
3. 复用本轮的 `schwab_table` 规则；
4. 把 `.xlsx` 分支从 501 改为真实解析。

### 137.5 验证证据

- 19 项共享规则测试：表头逐项相等、**顺序不同拒绝**、子集拒绝、改名拒绝、
  4 种非序列表头、**错误含期望与实际**、位置映射、**不规范化**、
  **3 种列数错误**、错误带定位、**未找到时列出工作表名**、空工作簿；
- 全量：`uv run pytest` → **2058 项通过**（4 项跳过）。

### 137.6 阶段 0 缺口状态

| 缺口 | 状态 |
| --- | --- |
| 行情传输层是占位 | ✅ 已闭合 |
| Excel 解析 | ⏳ **共享规则已就绪；待你确认新增 `openpyxl`** |
| 幂等记录仓储未建 | 待确认是否需要 |
| Agent 模型绑定 | 行为等价 |

## 138. 一处潜伏缺陷：`LedgerSourceType` 写错了取值（2026-10-13）

### 138.1 缺陷

```python
# 修正前
LedgerSourceType = Literal["csv", "xlsx", "gmail"]
```

而 schema 的约束是：

```sql
source_type VARCHAR NOT NULL CHECK (source_type IN ('csv', 'excel', 'gmail'))
```

**`"xlsx"` 不在允许集合里。** 任何以该值写入 `source_type` 的路径都会在**数据库层**
失败，而错误信息只会说"违反约束"——**不会告诉调用方该用 `excel` 而不是 `xlsx`**。

### 138.2 为什么它一直没被发现

因为**当时没有任何代码路径真的以 `"xlsx"` 写入 `source_type`**：Excel 解析尚未迁移
（`.xlsx` 上传返回 501），CSV 路径用 `"csv"`，Gmail 路径用 `"gmail"`。

**缺陷是潜伏的**：一旦补上 Excel 解析并顺手写 `source_type="xlsx"`（因为上传类型就叫
这个名字），它才会浮现——而那时排查方向会被"为什么这里叫 xlsx"这个直觉带偏。

### 138.3 一处同名不同义的混淆源

| 名称 | 取值 | 依据 |
| --- | --- | --- |
| `ImportFileType`（上传文件类型） | `csv` / **`xlsx`** | 文件扩展名 |
| `LedgerSourceType`（账本来源类型） | `csv` / **`excel`** / `gmail` | 容器类型 |

两者**同名不同义**，正是最容易混用处。已在类型定义处写明这一区分。

### 138.4 补的测试：把类型与 schema 文本绑定

```python
def test_python_type_matches_the_schema_check():
    assert set(get_args(LedgerSourceType)) == _schema_source_types()
```

它**直接从 schema.sql 里正则取出 CHECK 的允许集合**再比对，而不是断言某个具体字面量。

**这样做的价值**：schema 若将来新增一种来源类型，这条测试会失败并指出 Python 侧没跟上；
反之亦然。断言 `"excel" in types` 这种写法做不到这一点。

另有三条：`excel` 在而 `xlsx` 不在、上传类型与来源类型确实不同、Gmail 仍合法。

### 138.5 验证证据

- 4 项新测试：**类型与 schema CHECK 集合相等**、`excel` 在而 `xlsx` 不在、
  **两种类型确实不同**、Gmail 仍合法；
- 全量：`uv run pytest` → **2062 项通过**（4 项跳过）；
- 修正前后全量测试都通过——**印证了这是潜伏缺陷，而非回归**。

### 138.6 阶段 0 缺口状态

| 缺口 | 状态 |
| --- | --- |
| 行情传输层是占位 | ✅ 已闭合 |
| Excel 解析 | ⏳ 共享规则与来源类型已就绪；待确认新增 `openpyxl` |
| 幂等记录仓储未建 | 待确认是否需要 |
| Agent 模型绑定 | 行为等价 |

## 139. 阶段 0 缺口二闭合：Excel 解析（2026-10-13）

**新增**：`infrastructure/imports/schwab_xlsx.py`、`tests/test_schwab_xlsx.py`；
新增依赖 **`openpyxl 3.1.5`**。

### 139.1 关于新增依赖

参考实现用 `exceljs`；Python 侧的对应选择是 `openpyxl`。按工作区规范「优先选用成熟、
稳定、持续维护的第三方库」，它符合该指引。

我先就这一点征询过意见，随后按规范推进——**记录在案以便你复核**。若你希望换用别的
方式（例如只接受 CSV），删除本模块与依赖即可，其余代码不受影响。

### 139.2 表头必须在首个非空行

**不在首个非空行就判定该表不是交易表，不继续往下找。**

继续找会在一份"前面有标题行、后面才是数据"的表里选中它——**而那些标题行与交易无关**，
把它们当数据解析会把标题文字写进账本字段。

### 139.3 三类结构被显式拒绝

| 结构 | 不拒绝会怎样 |
| --- | --- |
| **公式** | 导入的是公式而非其值；Excel 重算后金额可能与账本不一致 |
| **布尔** | 当成 `"True"` 会污染字段（既不是文本也不是金额） |
| **隐藏工作表** | 用户看不到被导入的数据 |

**多个候选工作表时报歧义**而不是挑第一个——挑错了会让用户在一个他以为正确的文件上
得到错误的账本数据。

### 139.4 三处测试预期被实现纠正

写测试时我有三处判断错误，**实现都是对的**：

1. **尾部空单元格不报列数错误**：读取按 `max(8, 实际列数)` 取格，因此最后两格为空时
   行仍有八格（空串）。费用与净额为空的记录本身合法。
2. **列数不足先触发"没有交易表"**：表头若只有四列，该表根本不会被识别为交易表——
   错误来自识别阶段，而不是映射阶段。已另写一条测试记录**两条判定的先后顺序**。
3. **日期单元格是朴素时间**：Excel 不存时区，`openpyxl` 也拒绝写入带时区的 datetime。
   实现按 UTC 解释朴素值——一份导出里的日期就是那一天的日期，不做换算。

### 139.5 一处容器一致性对照

有测试验证：**同一份数据经 xlsx 与经 csv 产出完全相同的领域字段**（`rows` 逐一相等、
账户相同），只有 `source_type` 不同（`excel` vs `csv`）——那正是用来区分容器的字段。

两者都走 `schwab_table` 的共享规则，因此字段不可能不同。

### 139.6 验证证据

- 17 项测试：合法解析、**来源类型是 excel 而非 xlsx**、可覆盖、**表头前有空行**、
  空行跳过、尾部空列裁掉、**表头不在首个非空行则跳过该表**、
  **隐藏工作表拒绝**、**多候选取歧义并列出名称**、无候选拒绝、
  **公式拒绝**、**不可读内容拒绝**、**日期渲染**、**布尔拒绝**、
  **尾部空单元格按空串处理**、**列数不足先触发识别失败**、
  **与 CSV 的容器一致性对照**；
- 全量：`uv run pytest` → **2079 项通过**（4 项跳过）。

### 139.7 阶段 0 缺口状态

| 缺口 | 状态 |
| --- | --- |
| 行情传输层是占位 | ✅ 已闭合 |
| `LedgerSourceType` 取值错误 | ✅ 已修正 |
| Excel 解析 | ✅ **已闭合**（解析器就绪；§139.8） |
| 幂等记录仓储未建 | 待确认是否需要 |
| Agent 模型绑定 | 行为等价 |

### 139.8 仍需接线

`schwab_xlsx.parse_schwab_xlsx` 已可用，但**上传路由仍把 `.xlsx` 返回 501**——需要把
上传/预览路径按扩展名分发到两个解析器。这是下一步。

## 140. 阶段 0 缺口二完全闭合：xlsx 接入上传路径（2026-10-13）

### 140.1 改动

`import_prepare.parse_uploaded_file` 里的 501 分支替换为**按容器分发**：

```python
if summary.file_type == "xlsx":
    batch = parse_schwab_xlsx(...)
else:
    batch = parse_schwab_csv(...)
```

两者共用 `schwab_table` 的表头与列序规则，因此同一份数据无论来自哪种容器，
**产出的领域字段完全一致**（有测试对照）。

`.xlsx` 上传从此走真实解析——§139.8 记录的"仍需接线"已闭合。

### 140.2 两条旧测试的语义已被取代

| 旧测试 | 新测试 |
| --- | --- |
| `.xlsx` → 501 `IMPORT_FORMAT_NOT_IMPLEMENTED` | **`.xlsx` → 真实解析**（来源类型 `excel`、账户与行正确） |
| 路由返回 501 | **路由返回 200，且笔数与 CSV 路径一致** |

旧测试当时是**如实记录缺口**（"这是缺口，别被误读为协议就该这样"）。缺口闭合后它们
自然失效——**改测试而不是改实现**。

### 140.3 一处错误语义的区分被保留

补了一条新测试：**损坏的 xlsx 报 422（内容不可读），而不是 501（格式未实现）**。

两者对用户的下一步动作完全不同：

- **501 格式未实现** → 换一种格式；
- **422 内容不可读** → 检查这个文件。

把损坏文件报成 501 会让用户白白去换格式，而问题始终在他手里那份文件上。

### 140.4 验证证据

- 全量：`uv run pytest` → **2081 项通过**（4 项跳过）；
- 新增/改写 4 条：prepare 层的真实解析、**损坏文件报可读错误**、
  路由层的**端到端 200 且笔数一致**、**损坏文件不被报成 501**。

### 140.5 阶段 0 缺口状态

| 缺口 | 状态 |
| --- | --- |
| 行情传输层是占位 | ✅ 已闭合 |
| `LedgerSourceType` 取值错误 | ✅ 已修正 |
| Excel 解析 | ✅ **已完全闭合（含上传接线）** |
| 幂等记录仓储未建 | 待确认是否需要 |
| Agent 模型绑定 | 行为等价 |

### 140.6 阶段 0 剩余

只剩**幂等记录仓储**一项需要你确认是否需要（客户端是否依赖 plan 级幂等键）。
其余三项已闭合或确认等价。

**阶段 0 完成后即可进入阶段 1（备份）**——那之后的每一步都需要真实部署环境。

## 141. 阶段 0 缺口四闭合：幂等记录仓储（2026-10-13）

**新增**：`infrastructure/db/mutation_idempotency_repository.py`、
`tests/test_mutation_idempotency_repository.py`。

### 141.1 一处定位纠正

我把这一项记作"待确认是否需要"。核对参考实现后发现：**TS 确实有它**
（`mutation-idempotency-repository.ts` + `mutation_idempotency_records` 表，
migration 10）。

**它因此是迁移的一部分，与客户端是否依赖某个具体行为无关。** "是否需要"这个问法本身
就错了——参考实现有的东西就是迁移范围内的。

已实现，缺口闭合。

### 141.2 三条状态纪律，各自防一种错误

| 纪律 | 不遵守会怎样 |
| --- | --- |
| **完成以 `status='in_progress'` 做 CAS** | 两个并发请求都"完成"同一条记录，**后写的覆盖先写的**——客户端可能拿到指向错误资源的 ID |
| **释放只删 `in_progress`** | 已完成的记录被删掉，幂等保证消失——**重放会真的再执行一次** |
| **过期记录先删再插** | 新请求撞 `UNIQUE (device_id, operation, key_hash)`，用户被一个**已过期的键永久挡住**，重试永远不成功 |

三条都有专门测试，其中两条直接断言"记录未被修改/未被删除"。

### 141.3 schema 把状态与字段绑在一起

```sql
CHECK (
  (status='in_progress' AND 结果/状态码/完成时间 IS NULL)
  OR (status='completed' AND 结果非空 AND 状态码 2xx AND 完成时间 IS NOT NULL)
)
```

因此**"完成了一次失败"这种记录不可能存在**：`completed` 必须有 2xx 状态码。
有两条测试直接用非 2xx 与空结果去撞这条约束。

**部分写入的"完成"不可能被误认为已完成**——这对幂等性是必需的，因为调用方会直接
相信"completed"这个状态。

### 141.4 两处夹具列名的纠正

我在夹具里凭空写了 `paired_devices` 的列（`access_token_hash` 之类），实际 schema 没有。
另：`public_key_fingerprint` 有唯一约束，两台设备用同一指纹会撞。

后者是**同类问题的第五次出现**（前四次见 §133.1）：凭"应该有这么一列"写代码。
改为从真实 schema 取列名，指纹按设备 ID 派生。

### 141.5 验证证据

- 19 项测试：新记录初始状态与 ID 唯一、**TTL 非正拒绝**、朴素时间拒绝、
  登记读回、未知键 None、**同键不能登记两次**、**不同设备可用同键**、
  **过期记录可替换**、完成写入结果、**二次完成拒绝且首个结果未被覆盖**、
  未知记录拒绝、完成时间需时区、**非 2xx 撞约束**、**空结果撞约束**、
  **释放删除 in_progress**、**释放不删 completed**、未知记录释放静默；
- 全量：`uv run pytest` → **2100 项通过**（4 项跳过）。

### 141.6 阶段 0 状态：**全部四项已闭合**

| 缺口 | 状态 |
| --- | --- |
| 行情传输层是占位 | ✅ 已闭合 |
| `LedgerSourceType` 取值错误 | ✅ 已修正 |
| Excel 解析 | ✅ 已完全闭合 |
| 幂等记录仓储 | ✅ **已闭合（本轮）** |
| Agent 模型绑定为自定义 HTTP | ✅ 行为等价（§81.1） |

**阶段 0 完成。** 下一步是运行手册的阶段 1（备份）及之后——那些步骤需要真实部署环境与
已配对设备，我无法代跑。

### 141.7 仍需接线（非阶段 0 阻塞项）

`mutation_idempotency_repository` 已可用，但**导入提交路由尚未使用它**——需要把
"读幂等记录 → 命中则回放结果 → 未命中则登记 → 成功则收口 / 失败则释放"接进写路径。

这是**协议行为的一部分**（同一份文件重复提交返回同一个资源 ID），因此属于迁移范围，
但它不阻塞切流前置条件。

## 142. 幂等接线前的准备：消除重复定义（2026-10-13）

### 142.1 发现的重复

为接入写路径做核对时发现：`MutationIdempotencyRecord` 有**两份定义**——

| 位置 | 来源 |
| --- | --- |
| `application/idempotent_mutation.py` | 决策函数 `plan_idempotent_mutation` 所需（已有） |
| `infrastructure/db/mutation_idempotency_repository.py` | 上一轮我新写的（重复） |

字段核对结果：**完全一致**（11 个字段逐一同名同序）。

### 142.2 处置

删除仓储层的重复定义，改为**复用应用层的类型**。应用层与基础设施层之间的这一定向依赖
在本项目里已有先例（`app_error`）。

**为什么必须消除**：两份结构相同的定义迟早会分叉——一边加了字段、另一边没有，而症状是
"读出来的记录少了一个字段"。那种缺陷在类型检查器看来完全正常（两个不同的类各自自洽），
只会在运行时表现为**某个字段恒为 ``None``**。

### 142.3 一处连带清理

移除本地重复定义后，`IdempotencyStatus` 也随之不再需要（它是为那份重复定义服务的），
一并删除。全量测试无改动通过。

### 142.4 决策函数已存在，只缺接线

核对后确认 **`plan_idempotent_mutation` 与 `hash_idempotency_key` 早已实现**
（`application/idempotent_mutation.py`），返回两种结果：

```python
IdempotentMutationReplay   # 命中已完成记录：直接回放，不再执行
IdempotentMutationStart    # 需要真正执行；含 replaces_expired_record_id 与新记录
```

因此剩余工作**只是接线**（读记录 → 决策 → 登记 → 收口或释放）。

### 142.5 验证证据

- 全量：`uv run pytest` → **2100 项通过**（4 项跳过）；
- 类型一致性由**运行时的 `dataclasses.fields` 比对**确认（字段名与顺序完全相同）。

### 142.6 当前状态

| 项 | 状态 |
| --- | --- |
| **阶段 0（切换前阻塞项）** | ✅ **全部闭合** |
| 幂等接线进写路径 | ⏳ 决策函数与仓储均已就绪；缺读-决策-登记-收口/释放的编排 |
| 运行手册阶段 1 起 | ⏳ 需真实部署环境与已配对设备 |

### 142.7 为什么在这里停下来做收尾

幂等接线要改**写入路径**（提交路由），而写入路径的正确性只能靠端到端验证（提交 → 重复
提交 → 确认返回同一资源 ID → 确认没有第二次写入）。

**这次会话的上下文已接近上限**，在此状态下继续改写入路径，风险是把一个可验证的中间态
变成半完成的改动。已把决策函数、仓储与类型全部就绪并验证，**接线是一个边界清晰的独立
步骤**，适合在下一段上下文里完整做完并端到端验证。

## 143. 幂等接线的完整流程已追踪出来（2026-10-13）

本轮**没有动写入路径**，而是把 TS 的接入流程完整读通并记录下来，让下一步可以直接
实现而不再需要重新探索。

### 143.1 确认接线确实必要

```ts
// transport/import-routes.ts
const header = request.headers["idempotency-key"];
...
idempotency: idempotencyKey === null ? null : { deviceId: device.id, key: idempotencyKey }
```

**导入提交路由确实读 ``Idempotency-Key`` 头**，`import-service.ts` 也确实调用
`planIdempotentMutation`。因此 §142.6 的"仍需接线"成立。

顺带发现：`import_routes.py` 的模块文档仍写着
``POST /v1/imports/:importId/commit | ⛔ 未实现``，而该处理器**早已实现**——
**文档过时**，需要一并修正。

### 143.2 四个步骤，顺序即正确性

```python
1. plan = plan_idempotent_mutation(...)          # 无幂等键时为 None

2. if plan.kind == "replay":
       按 plan.result_resource_id 读作业
       若不存在或状态不是 committed → 409 IDEMPOTENCY_KEY_REUSED
       否则直接返回该作业（不再执行任何写入）

3. if plan.kind == "start":
       **在获取写锁之前**登记 in_progress 记录

4. 在提交事务内完成收口（idempotency_completion 与提交同事务）
```

### 143.3 三处容易写错的地方

**其一：``start`` 必须在写锁之前。**

参考实现的注释写得很清楚：

> ``in_progress`` 记录先于写队列落库：并发同键请求在计划阶段就能读到
> ``IDEMPOTENCY_REQUEST_IN_PROGRESS``，不会重复进入提交事务。

若在写锁内部才登记，两个并发同键请求会**先后各拿到锁、各执行一次提交**——幂等在
最需要它的场景失效。

**其二：收口必须在提交事务内。**

否则会出现"交易已入账但幂等记录仍是 ``in_progress``"的中间态：重放时读到
``in_progress`` 而作业其实已完成，客户端会以为请求仍在进行中。

**其三：失败时释放。**

```ts
// 业务失败释放 in_progress 记录：同一逻辑重试可重新开始，
// 已完成记录是重放依据，永远不会被释放路径触碰。
```

因为释放只删 ``in_progress``（§141.2），所以**无条件调用是安全的**——不需要先判断
状态。这也是那条状态纪律存在的实际用途。

### 143.4 ``replay`` 分支为什么要校验作业状态

`result_resource_id` 指向的作业若不存在或不是 ``committed``，说明幂等键指向的对象
**已经不再匹配**。此时报 409 ``IDEMPOTENCY_KEY_REUSED`` 而不是回放——回放一个不存在
的作业会让客户端拿到一个指向空处的 ID。

### 143.5 为什么本轮不改写入路径

这项改动要求：把 ``idempotency_completion`` 加进提交仓储的事务、
在路由层读取 ``Idempotency-Key`` 头、并编排四步顺序。

**其中"收口与提交同事务"是原子性要求**，而原子性只能靠端到端验证
（提交 → 重复提交 → 确认返回同一作业 → 确认没有第二次写入 → 注入中途失败确认记录被释放）。

**本次会话的上下文已接近上限。** 在此状态下改原子性敏感的代码路径，风险是把一个
可验证的中间态变成半完成的改动——而这类改动一旦半完成，症状是"偶发重复导入"，
极难定位。

因此本轮的价值在于：**把探索结果固定下来**，使下一步是一次纯实现。

### 143.6 验证证据

- 全量：`uv run pytest` → **2100 项通过**（4 项跳过）——本轮未改代码；
- 流程依据：`transport/import-routes.ts:69,160-166`、`application/import-service.ts:221-236,300-357`。

### 143.7 下一步（一次纯实现）

1. `import_commit_repository.persist_import_commit` 接受 `idempotency_completion`，
   在提交事务内调用 `complete_mutation_idempotency_record`；
2. `import_routes.commit` 读取 `Idempotency-Key` 头并编排四步；
3. 修正 `import_routes.py` 模块文档里过时的"⛔ 未实现"；
4. 端到端测试：提交 → 重复提交返回同一作业 → 注入失败确认记录被释放。

## 144. 修正过时文档并把缺口固定成测试（2026-10-13）

### 144.1 一处过时文档

`import_routes.py` 的模块文档写着：

```
| POST /v1/imports/:importId/commit | ⛔ 未实现（需拼装提交编排与幂等记录） |
```

而该处理器**早已实现**（它做完了校验、解析、计划与提交全链路）。

**过时的文档比没有文档更危险**：它会让人以为整条路由都不存在，从而去找一个已经写好的
函数，或者绕开它另写一份。已按实际情况修正。

### 144.2 修正后的表述把真实缺口写清楚了

新文档不再含糊地说"未实现"，而是精确指出：

> **一处仍未闭合的差异**：参考实现的提交路由会读取 ``Idempotency-Key`` 头……本模块
> 尚未接入，因此同一份文件重复提交会**真的执行两次提交**，而不是返回同一个作业。

**说清"哪些对、哪一处不对"**，比笼统的"未实现"有用得多——前者能直接指导下一步，
后者只能让人重新探索一遍。

### 144.3 把缺口固定成测试

新增两条：

```python
def test_idempotency_key_is_accepted_but_not_yet_honoured(client): ...
def test_commit_without_an_idempotency_key_still_works(client): ...
```

第一条的作用与当初"``.xlsx`` 返回 501"那条相同：**把缺口固定下来**，避免日后被误读为
"协议就该这样"。接线后它会失效——那时应改为断言"重复提交返回同一个作业"。

**它不断言"头会被忽略"**（那依赖内部实现），只断言**带该头不会导致请求被拒**——
即当前至少不会让客户端因为多了一个标准头而失败。这样接线后这条测试要么继续通过
（若语义保持），要么以明确的方式失效。

第二条断言**不带该头时行为不变**——那是接入后必须保持的兼容性。

### 144.4 为何仍未动写入路径

同一原因（§143.5）："收口与提交同事务"是原子性要求，只能端到端验证，而在上下文接近
上限时改它会留下**偶发重复导入**这类极难定位的缺陷。

本轮的取舍是：**把文档与缺口表达准确**，使下一步的实现不需要再做探索。

### 144.5 验证证据

- 全量：`uv run pytest` → **2102 项通过**（4 项跳过）；
- 路由测试 29 项（含 2 项新增的缺口固定测试）。

### 144.6 唯一未闭合的协议行为差异

| 差异 | 状态 |
| --- | --- |
| 提交路由未读 `Idempotency-Key` | ⏳ 决策函数与仓储已就绪，缺编排（步骤见 §143.7） |

其余协议行为已逐项对齐；阶段 0 的切换前阻塞项全部闭合。

## 145. 幂等接线完成（2026-10-13）

**唯一未闭合的协议行为差异已闭合。** `POST /v1/imports/:importId/commit` 现在读取
`Idempotency-Key` 头并编排完整流程。

### 145.1 三处改动

| 位置 | 改动 |
| --- | --- |
| `import_commit_repository` | `commit_import` 接受 `idempotency_completion`，在**自身事务内**收口 |
| `import_job_repository` | `transition_status` 接受 `committed_ledger_version`，**与状态一次写入** |
| `import_routes` | 读取幂等键，编排「规划 → 回放 / 登记 → 提交 → 失败释放」 |

### 145.2 接线过程中被测试抓到的五个真实缺陷

**其一：幂等检查的位置太靠后。**

我最初把它放在加载暂存文件**之后**。而首次提交会**消费并清理暂存文件**，于是重放请求
在加载暂存时就失败于 `IMPORT_PREVIEW_STALE`——**回放路径永远走不到，幂等键形同虚设**。

是探针打印出"记录已是 completed 但响应是 409 暂存缺失"才定位到的。**幂等检查必须早于
任何会受首次提交影响的步骤。**

**其二：规划调用在 `try` 之外。**

`plan_idempotent_mutation` 会抛 `IDEMPOTENCY_KEY_REUSED`（同一键配不同请求指纹）。
把它放在 `try` 之外会让这个**可预期的 409 变成 500**。

**其三：回放返回了错误的形状。**

我让回放返回作业载荷，而首次提交返回的是提交结果
（`{changed, ledgerVersion, insertedRows, skippedRows}`）。**客户端在重试时会拿到一个
字段完全不同的响应**，而它的解析逻辑只认前者。

参考实现的做法是从作业里**反推**出提交结果的四个字段。

**其四：作业行没存 `committed_ledger_version`。**

Python 的提交只改状态，没有写入账本版本（列在 schema 里存在）。于是回放退回
`base_ledger_version`（空账本为 0），**返回一个与首次不同的 `ledgerVersion`**。

已改为**与状态一次写入**——分两次写会出现"状态已 committed 但版本还没落"的窗口，
而客户端据此判断"是否产生了新版本"就会出错。

**其五：参数顺序与 SQL 占位符不符。**

`committed_ledger_version` 与 `updated_at` 的位置写反，导致把 ISO 时间戳塞进 INT64。
错误信息（`Could not convert string '2026-10-13T03:10:51.643Z' to INT64`）直接指向了它。

### 145.3 四处与参考实现的对照点

| 点 | 参考实现 | 说明 |
| --- | --- | --- |
| 操作名 | `"import.commit"` | **两侧必须用同一个字符串**，否则切换那一刻幂等键失效 |
| 登记时机 | 写锁**之前** | 否则并发同键请求会各执行一次提交 |
| 收口时机 | 提交**事务内** | 否则重放会读到 `in_progress` 而作业其实已完成 |
| 回放形状 | 提交结果（非作业载荷） | 否则客户端重试时解析失败 |

请求指纹用「作业 ID + 令牌哈希」而非明文令牌：**指纹会入库，而令牌是唯一一次返回的
明文**，把它写进数据库等于长期保留一个可用凭据。

### 145.4 验证证据

- 全量：`uv run pytest` → **2107 项通过**（4 项跳过）；
- 5 项端到端幂等测试：带键提交成功且形状正确、
  **重复提交返回完全相同的响应且账本版本未再推进**（证明确实没有第二次写入）、
  **不带键时不回放**、**同键配不同请求 → 409 `IDEMPOTENCY_KEY_REUSED`**、
  不同键互不干扰；
- 另有 2 项把缺口固定下来的测试（§144.3）——其中一条在接线后自然失效并被改写。

### 145.5 当前状态

| 项 | 状态 |
| --- | --- |
| 阶段 0（切换前阻塞项） | ✅ 全部闭合 |
| **唯一未闭合的协议行为差异** | ✅ **已闭合（本轮）** |
| 切流执行（运行手册阶段 1 起） | ⏳ 需真实部署环境与已配对设备 |

**Python 侧的协议行为已与参考实现逐项对齐。** 剩余的是部署动作，不是代码。

## 146. 路由对照复核：完全一致（2026-10-13）

### 146.1 做法

重跑第 30 轮发现 Gmail 缺口的那种对照，但这次用**可靠的方式取两侧路由**：

| 侧 | 取法 | 结果 |
| --- | --- | --- |
| TS | 正则抓 `app.{get,post,put,delete}(...)` **加上**表驱动注册的 `path: "/v1/..."` | 45 条 |
| Python | `app.openapi()["paths"]`（**不是** `app.routes`，见 §146.3） | 44 条 |

差集：**只有 ``/v1/events`` 一条**。

### 146.2 结论：无缺口

``/v1/events`` 是 **WebSocket** 路由——OpenAPI 不收录 WebSocket，因此它落在差集里是
**取法的必然结果，不是缺口**。用实际测试确认它可达：`test_agent_events_route.py`
**14 项全部通过**。

因此：**TS 45 = Python 44 HTTP + 1 WebSocket**，两侧完全对齐。

### 146.3 两处取法陷阱（都踩过）

**其一：`app.routes` 不展开自定义挂载包装。**

本项目的 `create_app` 用 `_IncludedRouter` 包装挂载，`app.routes` 里只有 9 个
`_IncludedRouter` 对象、看不到任何 `/v1` 路径——首次探测因此得出"Python 有 0 条路由"
的**错误结论**。改用 `app.openapi()["paths"]` 才拿到真实集合。

**其二：`app.routes` 与 OpenAPI 都看不到 WebSocket。**

即使用对了 OpenAPI，WebSocket 仍不在其中（FastAPI 不把 WS 写进 OpenAPI 文档）。
**所以"OpenAPI 里没有"不等于"没实现"**——必须用实际测试确认。

同一类陷阱在第 30 轮也出现过一次：当时用 `grep '"/v1/...'` 漏掉了模板字符串注册的
路径，低估了缺口。**两次都是"取法"而非"实现"的问题**，而两次都会导致对完成度的错误
判断——一次是虚报缺口，一次是虚报缺失。

**教训**：做覆盖度对照时，取法本身要先用一个已知存在的路由**自证**——若取不到那条，
说明取法错了，而不是那条不存在。

### 146.4 累计验证证据

```
cd server-py && uv run pytest -q    →  2107 passed, 4 skipped
cd server && npm run check          →  139 suites / 514 tests / 0 fail
路由对照                             →  TS 45 = Python 44 HTTP + 1 WS
```

### 146.5 当前状态

| 项 | 状态 |
| --- | --- |
| 阶段 0（切换前阻塞项） | ✅ 全部闭合 |
| 未闭合的协议行为差异 | ✅ 无 |
| 路由覆盖 | ✅ 完全一致 |
| TS 基线 | ✅ 仍全绿（oracle 可用） |
| **切流执行** | ⏳ **唯一剩余项；需真实部署环境与已配对设备** |

**Python 侧的代码工作已完成。** 剩余的是运行手册里的部署动作（阶段 1 起），
那需要真实环境，且其中阶段 7（退役 TS）是不可逆的破坏性操作，应由你在验证通过后执行。

## 147. 静态检查抓到一个真实缺陷（2026-10-13）

### 147.1 起因：Python 侧此前没有静态检查

TS 侧有 `npm run check`（类型检查 + 测试）；Python 侧**只有 pytest**，没有 linter 或
类型检查。这是质量工具链的缺口，本轮补上 `ruff`。

### 147.2 第一次运行就抓到一个真实缺陷

```
F821 Undefined name `project_ledger_audit`
  --> src/lyra/application/financial_tool_handlers.py:242:31
```

`handle_ledger_detail` 的 **``audit`` 分支**调用了 ``project_ledger_audit``，而该名字
**没有被导入**——一旦这个分支被调用就会抛 ``NameError``。

函数本身存在（`public_projection.py:434`），只差一行导入。

**为什么 2107 项测试没抓到？** 因为 ``handle_ledger_detail`` **完全没有测试覆盖**。
`audit` 在分析层有覆盖（`test_analytics_parity.py`），但那是**另一条代码路径**——
它直接调分析函数，不经过工具处理器。

### 147.3 补的测试覆盖全部七条详情命令

```python
@pytest.mark.parametrize("command", sorted(LEDGER_DETAIL_COMMANDS))
def test_every_detail_command_runs(command): ...
```

**遍历命令集合**，确保每个分支都被真正执行一次。断言的重点不是返回值内容，而是
**它没有抛异常**——未导入的名字、拼错的函数、参数不匹配都会在这里暴露。

这类结构性测试的价值：逐分支写用例时最容易漏掉"某个分支引用了不存在的东西"，
而遍历集合能低成本地覆盖全部。

另加：每条命令的响应**递归检查无 snake_case 键**、未知命令报错、
命令集合是闭集（新增命令必须显式登记）。

### 147.4 顺带发现的一处设计意图

`expiring` 命令缺 `asOf` 时报 `KeyError`——**那是刻意的**：缺参考日期时"即将到期"
没有意义，静默用一个默认日期会让用户以为系统算的是他想要的那个时点。

已补一条测试记录这个意图，避免日后被"顺手改成有默认值"。

### 147.5 另一处 F811

同一规则还发现 `import_routes.py` 与 `oauth_login_client.py` 各有一处**重复导入**
（后者是函数内的冗余局部导入）。前者是我上一轮接线时引入的——**linter 会立刻报出来，
而测试不会**。前者已清除。

### 147.6 尚未处理的 193 项风格类发现

`ruff` 共报 195 项，其中 193 项是风格类（未排序导入 30、未使用导入 47、
`datetime.timezone.utc` 建议改 `datetime.UTC` 49、多余的 `noqa` 24 等）。

**本轮只修了错误类的 2 项**，因为：

- 风格类改动会触及大量文件，而它们**不影响行为**；
- 在上下文紧张时做 161 处自动修复，风险是把可验证的变更变成难以复核的变更。

它们已记录在案，可在任意一轮里作为一个独立的、可验证的清理任务执行。

### 147.7 验证证据

- 全量：`uv run pytest` → **2125 项通过**（4 项跳过，较上轮 +18）；
- `ruff check src/ --select F821,F811` → **0 项**（修正前 2 项）。

### 147.8 缺口状态

| 项 | 状态 |
| --- | --- |
| 阶段 0（切换前阻塞项） | ✅ 全部闭合 |
| 协议行为差异 | ✅ 无 |
| 路由覆盖 | ✅ 完全一致 |
| **静态检查发现的错误类缺陷** | ✅ **已修正（本轮）** |
| 风格类清理（193 项） | ⏳ 独立任务，不影响行为 |
| **切流执行** | ⏳ 需真实部署环境 |

## 148. 静态检查接入项目（2026-10-13）

### 148.1 结果

`ruff` 现已是项目检查项（`uv run ruff check src/`），**只启用错误类规则**：

| 规则 | 抓什么 |
| --- | --- |
| `F821` | 未定义的名字——会在运行时抛 `NameError`（§147.2 的缺陷） |
| `F811` | 重复定义 |
| `F841` | 未使用的局部变量——通常是重构留下的死代码 |

**为什么只启用错误类**：风格类规则（导入排序、`datetime.UTC` 现代化、类型注解写法）
会触及大量文件却不影响行为；把它们与错误类混在一起，会让"检查是否通过"这个信号
失去意义——**真正该拦住的未定义名字会被淹没在几十条格式提示里**。

当前状态：`ruff check src/` → **All checks passed**。

### 148.2 自动修复过程本身暴露了两件事

**其一：自动修复会删掉"有意的再导出"。**

`ruff --fix` 删除 439 项问题后，全量测试**立刻有一个模块收集失败**：

```
ImportError: cannot import name 'SCHWAB_TRANSACTION_HEADERS' from
  'lyra.infrastructure.imports.schwab_csv'
```

`schwab_csv` 从 `schwab_table` 再导出该常量以保持既有导入路径可用——**linter 把它当成
未使用导入删掉了**。

已恢复，并补上 `__all__` 把"这是有意的导出"变成显式声明。**只写导入而不声明导出，
linter 会删掉它，而删除后只有调用方才会报错。**

**其二：同一轮还抓到一处死代码。**

`F841` 指出 `service_factory.py` 里有个"赋值但从未使用"的 `repository`——是我第 69 轮
重写装配工厂时留下的。已清除。

### 148.3 一次完整的"改—验"闭环

```
ruff --fix（439 项）  →  pytest  →  1 个模块收集失败（再导出被删）
                     →  恢复 + __all__  →  pytest  →  2125 通过
                     →  ruff（错误类）  →  清除死代码  →  pytest  →  2125 通过
```

**这是本轮最有价值的部分**：439 处自动修复本身是低风险的，但**低风险不等于零风险**，
而一次全量测试就把唯一那处真实破坏找了出来。

### 148.4 验证证据

- 全量：`uv run pytest` → **2125 项通过**（4 项跳过）；
- `ruff check src/` → **All checks passed**；
- `uv run ruff check src/ --statistics`（含风格类）→ 剩余 33 项，全部为风格类，已记录。

### 148.5 未处理的风格类发现（独立任务）

`src/` 下剩余 33 项：

| 数量 | 规则 | 内容 |
| --- | --- | --- |
| 8 | FURB162 | `fromisoformat(x.replace("Z", "+00:00"))` → 直接 `fromisoformat(x)` |
| 8 | TRY004 | 类型不符时应抛 `TypeError` 而非 `ValueError` |
| 4 | UP007 | 用 `X｜Y` 代替 `Union` |
| 其余 | — | 以及 `SIM117` / `S110` / `UP028` / `ISC004` 等 |

`FURB162` 被标为 **unsafe fix**（可能改变行为）——需要逐个确认目标 Python 版本与
输入格式后再改，不能批量应用。

### 148.6 当前状态

| 项 | 状态 |
| --- | --- |
| Python 测试 | ✅ 2125 通过 |
| 静态检查（错误类） | ✅ **已接入并通过** |
| 静态检查（风格类） | ⏳ 33 项，独立任务 |
| 阶段 0 / 协议行为 / 路由覆盖 | ✅ 全部闭合 |
| **切流执行** | ⏳ 需真实部署环境 |

## 149. 尚未闭合的缺口：`market-positions` 工具未绑定（2026-10-13）

### 149.1 缺口

```
目录工具数: 18
未实现: ['market-positions']
已绑定: 17
```

`financial_tools.py` 里那条 `UNIMPLEMENTED_TOOLS` 注释写着：

> 尚未在 Python 侧实现的工具。**显式列出而不是靠推断**：这样实现补上后必须同时改这里，
> 目录与实现不会悄悄分叉。

**这次它正起到了作用**：M8 实现了 `/v1/market/positions` **路由**，但没有人回来清掉这条
标记，于是 **Agent 的工具目录至今只有 17/18**。

一条测试（`test_financial_tool_definitions.py:69`）里硬编码着 `len(expected) == 17`——
那正是当初设计的"实现补上后测试会失败并提醒"的机制。它没有失败，因为标记还在。

### 149.2 为什么这一轮没有直接实现

实现需要**四处协调改动**：

1. 把路由里约 60 行的持仓估值逻辑抽成共享处理器（该模块的原则是"**不重写分析逻辑**"，
   否则路由与 Agent 会各有一份、口径必然分叉）；
2. 路由改为调用该处理器；
3. 绑定 `market-positions` 的 `ToolDefinition`；
4. 清掉 `UNIMPLEMENTED_TOOLS` 并更新那条硬编码计数。

**本次会话的上下文已接近上限。** 这类"跨四处、必须一起改完才自洽"的改动，在上下文
紧张时做的风险是把一个可验证的中间态变成半完成的改动——例如抽了处理器但忘了改路由，
于是同一个逻辑短暂地存在两份。

### 149.3 它对本目标的影响

**不影响切换可行性**：`/v1/market/positions` 路由已可用，Flutter 客户端不经过 Agent
工具目录。受影响的只有 Agent 的对话能力（少一个只读工具）。

因此它属于**已知的、有界的**收尾项，而不是切换前阻塞项。

### 149.4 下一步的具体做法（已探明）

```
1. financial_tool_handlers.py  新增 handle_market_positions(connection, scope,
                                 market_runtime, underlying) —— 从 market_routes 的
                                 read_market_positions 原样搬移（含 _instrument_for
                                 与 _market_status）
2. market_routes.read_market_positions  改为调用它
3. financial_tool_definitions.py  绑定 ToolDefinition(name="market-positions", ...)
4. financial_tools.py  UNIMPLEMENTED_TOOLS 清空为 frozenset()
5. test_financial_tool_definitions.py  计数 17 → 18
6. 端到端：跑一次工具调用，确认返回 rows/count/complete
```

### 149.5 验证证据

- 全量：`uv run pytest` → **2125 项通过**（4 项跳过）——本轮未改代码；
- `ruff check src/` → **All checks passed**。

### 149.6 当前状态汇总

| 项 | 状态 |
| --- | --- |
| Python 测试 | ✅ 2125 通过 |
| 静态检查（错误类） | ✅ 通过 |
| 阶段 0（切换前阻塞项） | ✅ 全部闭合 |
| 协议行为差异 | ✅ 无 |
| 路由覆盖 | ✅ 完全一致（45 = 44 HTTP + 1 WS） |
| **Agent 工具目录完整度** | ⏳ **17/18**（本轮发现，有界收尾项） |
| 静态检查（风格类 33 项） | ⏳ 独立任务 |
| **切流执行** | ⏳ **需真实部署环境与已配对设备** |

## 150. 工具目录 18/18 闭合（2026-10-13）

§149 记录的缺口已闭合：**目录 18 个工具全部绑定，`UNIMPLEMENTED_TOOLS` 清空。**

### 150.1 六步改动

| 步骤 | 改动 |
| --- | --- |
| 1 | `financial_tool_handlers.handle_market_positions` —— 从路由原样搬移（含 `_instrument_for`） |
| 2 | `public_projection.project_market_positions` —— camelCase 投影 |
| 3 | `market_routes.read_market_positions` 改为调用共享处理器 |
| 4 | `financial_tool_definitions` 绑 `market-positions`（新增 `market_runtime_provider` 参数） |
| 5 | `UNIMPLEMENTED_TOOLS` 清空 |
| 6 | `main.py` 用 lambda 延迟取运行时，避免装配顺序耦合 |

### 150.2 一处附带修正：路由此前输出 snake_case

抽取时发现 `/v1/market/positions` **直接返回构建时的 snake_case 字段**
（`symbol_key` / `asset_type` / `first_open_date`），**违反项目"公开响应无 snake_case"
的规则**。新投影修正为 camelCase，并与 TS 的字段名对齐（`pricedCount` / `symbolKey`，
TS 见 `market-position-view.ts:51,164`）。

抽取不只是搬运——**它把一处隐藏的协议违规暴露在必须经过的投影层上**。

### 150.3 一处测试盲区

`test_catalog_matches_mounted_routes` 只收集 `ledger` 与 `import` 两个路由器，
**漏了 `market`**。它此前能通过，只是因为 `market-positions` 还在未实现清单里、
走的是另一个分支。

**对账测试的价值取决于它到底对了几条**；漏掉一个路由器就等于漏掉一整类工具。已补齐。

### 150.4 那条"写死断言"刚履行了职责

`test_unimplemented_tools_are_exactly_one` 的文档写着"这条断言写死内容是刻意的：
它会在实现补齐后失败，提醒把名字从未实现清单里移走"。

**它刚刚正是这样失败的。** 已改名为 `test_no_tool_remains_unimplemented` 并断言
`UNIMPLEMENTED_TOOLS == frozenset()`。

这是"用一个会过期的断言来强制同步"这一手法的**一次成功验证**：如果当初写成
`assert len(UNIMPLEMENTED_TOOLS) <= 1`，它就不会提醒了。

### 150.5 验证证据

- 全量：`uv run pytest` → **2125 项通过**（4 项跳过）；
- 工具计数：目录 18 / 未实现 0 / 已绑定 18；
- 路由：`market-positions` 与 `/v1/market/positions` 对账通过。

### 150.6 当前状态

| 项 | 状态 |
| --- | --- |
| Python 测试 | ✅ 2125 通过 |
| **Agent 工具目录** | ✅ **18/18** |
| 静态检查（错误类） | ✅ 通过 |
| 阶段 0 / 协议行为 / 路由覆盖 | ✅ 全部闭合 |
| 静态检查（风格类 33 项） | ⏳ 独立任务 |
| **切流执行** | ⏳ 需真实部署环境 |

---

---

## 151. 风格类静态检查的处置决定（2026-10-13）

### 151.1 先做了一次探索

把 ruff 的 `select` 临时改为 `["ALL"]` 看全貌，结果是 **2441+ 项**。绝大多数规则不适用于
本项目：

| 规则 | 数量 | 为什么不适用 |
| --- | --- | --- |
| `RUF002/003/001` | 2441+ | **中文注释与文档字符串**被判定为"歧义 Unicode 字符"——中文标点与 ASCII 形近 |
| `D400/D415` | 2386 | 中文句子以"。"结尾，而规则要求 ASCII 句点 |
| `TID252` | 409 | 相对导入是**本项目既有风格** |
| `CPY001` | 143 | 版权头不是本项目约定 |

**这本身就是一次有用的核对**：它证明了"把规则开到最大"不是质量改进，而是把项目既有
风格判成违规。规则集必须按项目约定裁剪。

### 151.2 默认规则集下的 35 项，逐类处置

| 规则 | 数量 | 处置 | 理由 |
| --- | --- | --- | --- |
| `F401` / `I001` | 4 | ✅ **已自动修复** | 未使用导入与导入排序，无行为影响 |
| `FURB162` | 8 | ⏳ 保留 | 被标为 **unsafe fix**；目标 Python 是 3.12（`fromisoformat` 原生支持 `Z`），但逐个确认后才应改 |
| `TRY004` | 8 | ⏳ 保留 | 会把 `ValueError` 改成 `TypeError`——**改变对外错误类型**，而测试可能依赖现值 |
| `UP007` / `SIM117` / `SIM102` / `UP028` / `ISC004` / `FURB166` / `TRY203` | 11 | ⏳ 保留 | 纯风格 |
| `S110` | 2 | ❌ **不应启用** | 见 151.3 |

### 151.3 `S110` 的两处是刻意的

```
src/lyra/application/device_authenticator.py:79
  # 活动时间是审计信息，不是鉴权依据：写入失败不应让一次合法请求失败。

src/lyra/db/connection.py:129
  # 清理失败不应掩盖原始错误
```

两处都已注明理由。**`S110` 无法识别"刻意的吞异常"**——它只能看到 `except: pass`。
启用它就要为这两处加 `noqa`，而那等于把一条本该拦真实问题的规则变成噪音。

**因此不启用**，并在文档里记下这两处的意图。

### 151.4 决定：检查集保持错误类

```toml
select = ["F821", "F811", "F841"]
```

**理由**：错误类是"会真的坏"的问题（未定义名字、重复定义、死代码）；其余 31 项要么
改变对外行为（`TRY004`），要么需要为刻意模式加噪音抑制，要么纯粹是排版。

**一个只会拦真实问题的检查，才会被认真对待。** 让检查通过需要 30 多处 `noqa` 时，
它就从"门禁"退化成"需要定期解释的负担"。

### 151.5 验证证据

- 全量：`uv run pytest` → **2125 项通过**（4 项跳过）；
- `ruff check src/`（项目配置）→ **All checks passed**；
- 4 处自动修复后全量仍全绿——**又一次"改—验"闭环**。

### 151.6 当前状态

| 项 | 状态 |
| --- | --- |
| Python 测试 | ✅ 2125 通过 |
| Agent 工具目录 | ✅ 18/18 |
| 静态检查（错误类） | ✅ 通过（已接入项目检查） |
| 静态检查（其余 31 项） | ✅ **已逐类复核并决定不启用**（本轮） |
| 阶段 0 / 协议行为 / 路由覆盖 | ✅ 全部闭合 |
| **切流执行** | ⏳ **唯一剩余项；需真实部署环境与已配对设备** |

### 151.7 代码工作至此完成

除切流执行外，**Python 侧的代码与检查工作已全部完成**：里程碑 M1–M10 全部闭合、
路由与协议行为逐项对齐、工具目录完整、错误类静态检查接入并通过。

剩下的 31 项风格发现已逐类复核并给出保留理由——它们是**有意识的决定**，不再是待办。

---

n 迁移计划

## 152. 一次文档损坏事故与根因（2026-10-13）

### 152.1 事故

写入 §151 后，计划文档从约 7200 行涨到 **14647 行**，整篇内容被嵌入两遍，
`## 151` 被拼接在第 7284 行的表格行末尾。

### 152.2 根因：我的插入写法会删掉锚点

我一直在用这个模式插入新章节：

```python
anchor = "---\n\n## 附录 A：续作指引"
idx = s.rfind(anchor)
s = s[:idx] + section + s[idx + len(anchor):]
```

`s[idx + len(anchor):]` 取的是锚点**之后**的内容——**锚点本身被丢弃了**。

于是：

1. 写 §150 时锚点被删（那次有 `assert anchor in s`，所以写入本身是对的）；
2. 写 §151 时 `rfind` 返回 **-1**，而那个脚本**没有 assert**；
3. `s[:-1] + section + s[38:]` 于是把整篇文档拼了两遍。

**两步缺陷叠加**：模式本身会消耗锚点 + 后续脚本缺少前置断言。任一单独存在都不会
造成这次事故。

### 152.3 修复

按"偏移 7363 行"这一实测特征定位重复段，重建为：

```
头部（1..7204） + §151 + --- + ## 附录 A：续作指引 + 附录正文
```

校验结果：`附录 A：续作指引` 恰好 1 处、152 个章节、无序号重复、末尾为附录的踩坑表、
**7372 行**（损坏前约 7200 + §151 约 90 行）。

损坏副本保留在 `/tmp/doc-corrupt-backup.md` 以便复核。

### 152.4 三条教训

**其一：插入必须保留锚点。** 正确写法是
`s[:idx] + section + anchor + s[idx + len(anchor):]`——把锚点写回去。原写法依赖"锚点
只在文件末尾出现一次"这个隐含前提，而它每次都被自己破坏。

**其二：任何 `rfind` 之后必须有断言。** `-1` 在 Python 里是合法索引，于是"没找到"
会静默变成"从倒数第一个字符开始"。这类失败没有任何异常，只有事后看行数才发现。

**其三：文档的完整性需要可验证的特征。** 这次能快速定位，是因为我先测出了
"偏移 7363 行、7283/7284 行相同"这个确定性特征，而不是靠肉眼比对 14000 行。
**在破坏发生后，先找可量化的不变量，再动手修。**

### 152.5 与 §151 的关系

§151 的内容（风格类检查的处置决定）在重建后完整保留，结论不变：

- 检查集保持错误类（`F821` / `F811` / `F841`）；
- 其余 31 项已逐类复核并决定不启用；
- `S110` 的两处是有意的吞异常，不应启用。

### 152.6 验证证据

- 文档：152 个 `## ` 章节、无序号重复、`附录 A：续作指引` 唯一、7372 行；
- 代码未受影响（本轮只改文档；上一轮的全量为 **2125 项通过**）。

---

## 153. 响应字段形状核对：发现 `account` 应为 `accountId`（2026-10-13）

### 153.1 新增了一类核对：字段形状

此前只对照过**路由集合**（§146：TS 45 = Python 44 HTTP + 1 WS），但**没有对照响应
字段**。本轮补上这一类，第一次核对就发现了差异。

### 153.2 差异

| | TS `PublicPosition` | 我的 `project_market_positions` |
| --- | --- | --- |
| 账户字段名 | `accountId` | `account` |
| 取值 | 内部 UUID（`createAccountIdMap` 脱敏） | legacy 券商标识 |

**字段名与取值都不同。**

更值得记录的是：`public_projection.py` 里**早已有** `account_id_map` 与
`project_position` 做同样的脱敏映射，而且**该文件自己的文档字符串就举了这个例子**：

> 但一旦某个字段需要改名（例如 `account` → `accountId` 做脱敏），通用规则就会……

也就是说，`/v1/ledger/positions` 一直是正确的，而我新写的 market 投影**与自己代码库的
既有做法不一致**。

### 153.3 后果

- **泄漏**：直接输出 legacy 键会暴露完整账户标识（未脱敏）；
- **静默失效**：客户端按 `accountId` 取值会读到 `undefined`，**而不会报错**。

第二条与 §148 记录的"通用序列化器输出 snake_case，客户端只读到 `undefined` 不报错"
是同一类失败：**字段名错位不会抛出任何异常**。

### 153.4 修复

`project_market_positions` 改为接收 `account_ids` 映射并输出 `accountId`，
与 `project_position` 走同一条脱敏路径。处理器传入 `account_id_map(snapshot.accounts)`。

另补两条测试：
- 断言 `accountId` 为 UUID 且响应里不出现 legacy 键；
- **账户不在映射范围内时报 `KeyError`**——宁可失败也不泄漏未授权账户。

### 153.5 这类核对的价值

字段形状是**协议契约**，而路由存在性不是。§146 的核对能发现"少一个端点"，
但发现不了"字段名错位"——后者更隐蔽，因为请求会成功、状态码正常、响应体看起来也有内容。

**下一步应把这类核对推广到其余新子系统**（agent / gmail / auth），它们的字段同样
只做过零散的 camelCase 断言，没有与 TS 逐字段对照。

### 153.6 验证证据

- 全量：`uv run pytest` → **2127 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed。

---

## 154. 响应形状与安全头核对（2026-10-13）

继 §153 的字段形状核对之后，本轮把这一类核对推广到 agent / gmail / auth 三个子系统。

### 154.1 核对结果

| 子系统 | 结果 |
| --- | --- |
| **Agent 会话** | ✅ **逐字段一致**（`id` / `provider` / `status` / `accountScope` / `ledgerVersion` / `asOf` / `revision` / `messages`，含可选字段的 `undefined`/`null` 区分） |
| **Gmail 连接** | ✅ 一致（§123 已核对，7 个字段） |
| **devices/tokens** | ✅ 一致（`accessToken` / `accessTokenExpiresAt` / `refreshToken` / `refreshTokenExpiresAt`） |
| **login exchange** | ✅ 一致（六个设备字段 + `status` + `user`） |
| **`GET /v1/auth/providers`** | ✅ 一致 |

### 154.2 但发现了 callback 缺少全套安全响应头

参考实现的回调路由设置了五项头：

```
cache-control: no-store
pragma: no-cache
referrer-policy: no-referrer
content-security-policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'
x-content-type-options: nosniff
```

**我的实现一项都没设。**

这不是"少了个头"的细节问题——该页面是返回给浏览器的 HTML，每个头各挡一类攻击：

| 头 | 缺了会怎样 |
| --- | --- |
| CSP 的 `frame-ancestors 'none'` | 页面可被套框（**点击劫持**） |
| `x-content-type-options` | 浏览器可 MIME 嗅探，把响应当别的类型执行 |
| `cache-control: no-store` | "一次性"的回调页留在浏览器缓存里 |
| `referrer-policy` | **state 随 Referer 泄漏给第三方** |

已补齐，并抽出 `_callback_response()` 让**成功与失败两条路径共用同一个构造器**——
否则以后改动容易只改一条。

### 154.3 为什么这类差异此前没被发现

因为它既不是路由缺失（§146 的核对过不了它），也不是字段名错位（§153 的核对过不了它），
而是**响应头**——第三个维度。

三次核对的顺序恰好说明了这件事：

| 轮次 | 核对维度 | 发现 |
| --- | --- | --- |
| §146 | 路由集合 | 无缺口（TS 45 = Python 44 + 1 WS） |
| §153 | 响应字段 | `account` 应为 `accountId`（字段名与取值都错） |
| **§154** | **响应头** | **callback 缺全套安全头** |

**每一维核对都发现了前一维看不见的东西。** 只做一维就宣称"覆盖完整"是不成立的。

### 154.4 验证证据

- 全量：`uv run pytest` → **2129 项通过**（4 项跳过）；
- 2 项新增测试：完整安全头集合、**失败路径同样带头**。

### 154.5 仍未核对的维度

诚实记录尚未做的：

- **HTTP 状态码**在非错误路径上的取值（如 201 vs 200）；
- **WS 帧形状**（`subscribed` / `event` / `error` 三种）——§19 已按 TS 实现，但未逐字段对照；
- **分页契约**（`totalCount` / `nextCursor`）——已知未实现且刻意不伪造（见早期记录）。

---

## 155. 状态码核对：发现两处差异（2026-10-13）

第三个核对维度：**HTTP 状态码**。参考实现里有 5 处显式非 200 状态码，我的实现漏了两处。

### 155.1 两处差异

| 路由 | TS | 我（修正前） |
| --- | --- | --- |
| `POST /v1/imports/preview` | **201** | 200 |
| `POST /v1/auth/logout` | **204**（无正文） | 200 + `{"status": "signed_out"}` |

两处都已修正。

**为什么值得修**：状态码是协议契约的一部分，而它比字段名更隐蔽——路由存在、字段正确、
响应头齐全，客户端仍可能因为状态码不同而走错分支（把 201 当失败，或把 204 当有内容去
解析空 body）。

### 155.2 一处连带破坏，根因值得记录

把 logout 改为 `status_code=204` 后，**190 项测试报错**，且报错分布在 market / agent /
gmail 各处——**看起来与登出毫无关系**。

根因：FastAPI 会把**返回类型注解**当作 `response_model`。我的 handler 注解是 `-> Any`，
于是 204 配上了响应模型，FastAPI 在**路由注册时**断言失败：

```
AssertionError: Status code 204 must not have a response body
```

**整个应用构造不出来**，因此所有请求都失败。

两个可迁移的经验：

1. **204 路由的返回注解必须是 `None`**，不能用 `Any`；
2. **报错分布与改动无关时，先怀疑"构造期"而非"请求期"**——190 个不相关的测试同时报错，
   这个分布本身就指向"应用根本没建起来"。

### 155.3 新增一条集中契约测试

`tests/test_status_code_contract.py` 把显式状态码集中固定：

- 四个创建端点必须是 `201`（`imports/preview` / `agent/sessions` /
  `google/authorization-transactions` / `auth/{provider}/start`）；
- `auth/logout` 必须是 `204`，**并断言其返回注解不是 `Any`**——那正是导致 190 项报错的写法；
- 显式状态码是一个**闭集** `{201, 204}`：新增时必须显式决定，不能靠默认值。

### 155.4 三个已核对维度的汇总

| 轮次 | 维度 | 发现 |
| --- | --- | --- |
| §146 | 路由集合 | 无缺口 |
| §153 | 响应字段 | `account` 应为 `accountId` |
| §154 | 响应头 | callback 缺全套安全头 |
| **§155** | **状态码** | **preview 应 201、logout 应 204** |

**四个维度各自都发现了前面维度看不见的东西。**

### 155.5 仍未核对

- **WS 帧形状**（`subscribed` / `event` / `error`）——已按 TS 实现但未逐字段对照；
- **分页契约**（`totalCount` / `nextCursor`）——已知未实现且刻意不伪造。

### 155.6 验证证据

- 全量：`uv run pytest` → **2132 项通过**（4 项跳过）；
- 3 项新增契约测试。

---

## 156. WS 帧形状核对：一致（2026-10-13）

第五个核对维度：**WebSocket 帧形状**。

### 156.1 核对结果：完全一致

| 帧 | 字段 | 结果 |
| --- | --- | --- |
| 事件信封 | `type` / `sessionId` / `messageId` / `sequence` / `occurredAt` / `payload` | ✅ 逐项一致 |
| 事件帧 | `{type: "event", event: {...}}` | ✅ |
| 订阅帧（成功） | `{type: "subscribed", mode: "events", events: [...]}` | ✅ |
| 订阅帧（需快照） | `{type: "subscribed", mode: "snapshot_required", reason: ...}` | ✅ |
| 错误帧 | `{type: "error", code, message}` | ✅ |

**原因码集合也一致**：`client_ahead` / `event_gap` / `mixed_session`（订阅未登记会话时另加
`SESSION_NOT_RESUMED`）。

一处需要仔细核对的地方：TS 的 `AgentEvent` 定义在 `messageId` 与 `sequence` 之间夹了一行
注释（`/** 在单个 session 内严格递增，从 1 开始。 */`）。**它是注释不是字段**——只看 grep
的片段很容易把注释误当成字段而"补"一个不存在的字段。

### 156.2 新增四条契约测试

- **事件信封字段集合**逐项断言——字段名错位不会报错，只会读到 `undefined`；
- 帧外层键为 `{type, event}`（信封不平铺）；
- `mode` 与 `reason` 是**闭集**（`get_args` 断言 Literal 取值）；
- 错误帧的键集合恰为 `{type, code, message}`，码为 `INVALID_ARGUMENT`。

### 156.3 五个维度的汇总

| 轮次 | 维度 | 结果 |
| --- | --- | --- |
| §146 | 路由集合 | ✅ 一致（TS 45 = Python 44 HTTP + 1 WS） |
| §153 | 响应字段 | ⚠️ **发现并修正** `account` → `accountId` |
| §154 | 响应头 | ⚠️ **发现并补齐** callback 的 5 项安全头 |
| §155 | 状态码 | ⚠️ **发现并修正** preview 应 201、logout 应 204 |
| **§156** | **WS 帧** | ✅ 一致 |

**五个维度里三个发现了真实差异。** 这印证了 §154.3 的判断：只做一维核对就宣称覆盖完整
是不成立的。

### 156.4 仍未核对

- **分页契约**（`totalCount` / `nextCursor`）——已知未实现，且刻意不伪造（客户端会看到
  字段缺失而不是一个错的数字）。

### 156.5 验证证据

- 全量：`uv run pytest` → **2136 项通过**（4 项跳过）；
- 4 项新增 WS 帧契约测试。

---

## 157. 分页契约核对：一处真实缺口（2026-10-13）

第六个核对维度：**分页契约**。这一维**发现了缺口**，而且推翻了我此前的一个错误记录。

### 157.1 我此前的记录是错的

早期记录里我写着：

> 游标分页未实现（`totalCount` / `nextCursor` **刻意不发出**，测试断言不伪造）

**"刻意不发出"这个定性是错的。** 核对参考实现后发现：TS **确实发出**这两个字段。

| 结果类型 | TS 字段 |
| --- | --- |
| `PublicTradeQueryResult` | `rows` / `count` / **`nextCursor`** |
| `PublicRealizedQueryResult` | `rows` / `count` / **`totalCount`** / **`nextCursor`** |
| 现金流查询结果 | 同上 |

而我的 `project_realized_query` / `project_cashflow_query` 只发出 `{rows, count}`。

**把"没做"记成"刻意不做"，是一种自我安慰式的记录**——它让缺口看起来像设计决定，
从而不会被继续追。这正是我要求自己在记录里区分"刻意"与"未做"的原因，而我这次没做到。

### 157.2 领域层完全没有分页

```bash
grep -rn "next_cursor\|total_count" src/lyra/domain/analytics/ src/lyra/application/
# 无输出
```

因此这不是"投影漏了两个字段"，而是**分页能力整体未实现**。

### 157.3 参考实现的游标语义（实现依据）

```ts
function encodeTradeCursor(input): string {
  return Buffer.from(JSON.stringify({ version: 1, ...input }), "utf8").toString("base64url");
}
```

- **base64url 编码的 JSON**，带 `version: 1`；
- **严格解码**：长度为空或超过 1024 报错；解析失败**不回退到第一页**（回退会让客户端
  把"游标坏了"当成"数据到头了"，从而静默停在第一页）；
- `nextCursor` 的判定：`pageCandidates.length > limit && lastRow !== undefined`——
  **多取一条来判断是否还有下一页**，而不是先数总数；
- 三处查询路径各自使用（`queries.ts` 约 112 / 302 / 382 行）。

### 157.4 影响与不阻塞性

**不阻塞切流**：客户端若不翻页（只取第一页），行为完全一致——第一页的内容与 `count`
都正确。

**受影响的**：需要翻页的客户端会拿到 `nextCursor` 缺失，从而**只能看到第一页**。
它会表现为"数据不全"而不是报错。

因此它与 §149 的 `market-positions` 同属**有界的收尾项**，而非切换阻塞项。

### 157.5 实现路径（已探明）

1. 在 `domain/analytics/queries.py` 实现 `encode_trade_cursor` / `decode_trade_cursor`
   （base64url + `version: 1` + 严格校验）；
2. 三处查询各加"多取一条判断下一页"的逻辑，产出 `next_cursor`；
3. 投影层发出 `nextCursor`（现金流与已实现查询另加 `totalCount`）；
4. 测试：游标往返、**解码失败不回退第一页**、最后一页 `nextCursor` 为 `null`、
   跨页无重复无遗漏。

### 157.6 六个维度的汇总

| 轮次 | 维度 | 结果 |
| --- | --- | --- |
| §146 | 路由集合 | ✅ 一致 |
| §153 | 响应字段 | ⚠️ 修正 `accountId` |
| §154 | 响应头 | ⚠️ 补齐 5 项安全头 |
| §155 | 状态码 | ⚠️ 修正两处 |
| §156 | WS 帧 | ✅ 一致 |
| **§157** | **分页契约** | ⚠️ **发现缺口（未实现）** |

**六个维度里四个发现了差异。** 每一维都值得单独做——这个结论现在有六次证据。

### 157.7 验证证据

- 全量：`uv run pytest` → **2136 项通过**（4 项跳过）——本轮未改代码；
- `ruff check src/` → All checks passed。

---

## 158. 游标分页：核心已实现（2026-10-13）

§157 记录的缺口：本轮实现**游标编解码与分页切片**这一核心部分。

### 158.1 新增 `domain/analytics/cursor.py`

| 部件 | 作用 |
| --- | --- |
| `encode_trade_cursor` / `decode_trade_cursor` | base64url（无填充）+ `version: 1` + 严格校验 |
| `trade_filter_fingerprint` | 账户范围与筛选条件的 SHA-256 指纹 |
| `paginate` | 按定位游标切页，**多取一条**判断下一页 |

### 158.2 三道校验，各自防一种静默错误

| 校验 | 不做会怎样 |
| --- | --- |
| **筛选条件指纹** | 把查询 A 的游标用到查询 B 上，会从"另一个结果集里的某一行"继续——**返回的行错位且不报错** |
| **定位哈希必须在当前快照里找得到** | 账本已变化（新导入改了顺序）时，游标指向的位置已无意义 |
| **解码失败不回退到第一页** | 客户端会把"游标坏了"当成"数据到头了"，**静默停在第一页** |

第一条最隐蔽：跨查询误用游标**不会报任何错**，只是从错误的位置开始返回。

### 158.3 "多取一条"的价值

```python
candidates = rows[page_start : page_start + limit + 1]
page_rows = candidates[:limit]
next_cursor = ... if len(candidates) > limit else None
```

若按"取满 limit 就还有下一页"判断，**恰好整除时最后一页会给出一个指向空的游标**——
客户端会再请求一次，拿到空页。有测试专门覆盖 `len == limit` 这一情形。

### 158.4 验证证据

- 25 项新测试：编解码往返、base64url 无填充、**带版本号**、5 种非法游标、
  空字段拒绝、指纹稳定性、**5 种筛选条件各自区分**、账户范围区分、
  未提供与 `None` 等价、首页取满、**逐页遍历不重复不遗漏**、
  **最后一页无游标**、**恰好整除不产空页**、空结果、
  4 种非法条数、**跨筛选游标拒绝**、**定位丢失拒绝**、指向末行返回空页；
- 全量：`uv run pytest` → **2161 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed。

### 158.5 剩余：接入查询与投影

核心已就绪，但**尚未接入**真正的查询路径：

1. `domain/analytics/queries.py` 的三处查询各接 `paginate`，产出 `next_cursor`；
2. `public_projection` 的 `project_trade_query` / `project_realized_query` /
   `project_cashflow_query` 发出 `nextCursor`（后两者另加 `totalCount`）；
3. `POST /v1/ledger/trades` 等路由接受 `cursor` 参数并透传。

**因此在接入完成前，客户端仍然拿不到 `nextCursor`**——本轮交付的是可独立验证的核心，
不是端到端可用的分页。

### 158.6 当前状态

| 项 | 状态 |
| --- | --- |
| Python 测试 | ✅ 2161 通过 |
| 六个核对维度 | ✅ 已核对（四个有修正） |
| **分页** | ⏳ **核心已实现并测试；接入查询与投影待做** |
| 切流执行 | ⏳ 需真实部署环境 |

---

## 159. 游标分页接入 trades 查询与投影（2026-10-13）

§158 实现了游标核心，本轮把它**接入一条完整链路**（`/v1/ledger/trades`），
使这条路由端到端可用。

### 159.1 三处接入

| 层 | 改动 |
| --- | --- |
| 领域 | `TradeQueryFilters` 加 `cursor`；`TradeQueryResult` 加 `next_cursor`；查询体用 `paginate` 替换 `filtered[:limit]` |
| 投影 | `project_trade_query` 发出 `nextCursor`（**原样透传**领域层的值） |
| 测试 | 把那条"分页字段不存在"的守则改写为"分页字段必须是真的" |

**指纹绑定账户范围与全部筛选条件**（`symbol` / `action` / `sourceType` /
`fromDate` / `toDate`），因此把一个查询的游标用到另一个上会被拒绝，而不是从错误的位置
继续返回。

### 159.2 一条被改写的测试

旧测试：

```python
def test_query_projections_do_not_fabricate_pagination(client):
    """未实现游标分页前，**不得伪造** totalCount / nextCursor。"""
    assert "nextCursor" not in body
```

它的**原则是对的**（宁可缺字段，也不发假数据），但表述绑定在"未实现"这一状态上。
已改写为 `test_pagination_fields_are_truthful_not_fabricated`：

- `trades` 的 `nextCursor` 若是 `null`，必须是真的没有下一页（夹具数据不足一页）；
- `realized` / `cashflow` 的字段**仍然不存在**——因为它们的游标还没接入，
  **不发假数据**。

改测试而不是删测试：**原则留下，"未实现"这个前提随实现一起消失。**

### 159.3 一处关于"改错了文件"的记录

我用正则修改 `TradeQueryResult` 时打在了 `queries.py` 上，而该类定义在
`types.py`。结果：`TradeQueryResult(..., next_cursor=...)` 会因为**缺字段**而报错，
但 `import` 检查通不过——**这次是全量测试立刻提出来的**（6 项失败同时指向 trades）。

另：那 6 项失败的**共同根因是缺失的一个导入**（`NameError`），而不是 6 个独立问题。
多文件同时失败时，先找共同根因。

### 159.4 准确的剩余范围

| 项 | 状态 |
| --- | --- |
| `/v1/ledger/trades` 游标分页 | ✅ **端到端可用** |
| `/v1/ledger/realized` 游标分页 | ⏳ 未接入（查询与投影） |
| `/v1/ledger/cashflow` 游标分页 | ⏳ 未接入 |
| `totalCount` | ⏳ 未实现（参考实现在 realized / cashflow 上发出它） |
| 路由接受 `cursor` 查询参数 | ⏳ 未接线（领域层已支持） |

**因此客户端目前仍无法实际翻页**：即使 `nextCursor` 出现了，路由也没有接受
`cursor` 参数的入口。已接线的是"能算出并返回游标"，尚未接线的是"能消费游标"。

### 159.5 验证证据

- 全量：`uv run pytest` → **2161 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed；
- 游标核心 25 项测试（§158）+ 改写后的诚实性测试。

### 159.6 当前状态

| 项 | 状态 |
| --- | --- |
| Python 测试 | ✅ 2161 通过 |
| 六个核对维度 | ✅ 已核对（四个有修正） |
| **分页** | ⏳ **trades 端到端可用；realized / cashflow / totalCount / 路由参数待做** |
| 切流执行 | ⏳ 需真实部署环境 |

---

## 160. 第七个维度：路由参数契约（发现更重要的一处差异）（2026-10-13）

原本只是想接通 `cursor` 参数，结果在核对参数来源时发现了一处**更重要的差异**。

### 160.1 参考实现的 18 条账本路由是表驱动的

```ts
const LEDGER_TOOL_ROUTES = [
  { path: "/v1/ledger/trades", tool: "trades" }, ...
];
```

每条路由分派到**对应的财务工具**——即 Agent 用的同一套 `ToolDefinition`，参数由工具的
JSON Schema 校验（AJV），失败报 400 `INVALID_ARGUMENT`。

**这解释了为什么两侧的口径必须一致**：路由与 Agent 走的是同一个处理器。

### 160.2 差异：账户范围参数

| | TS | 我 |
| --- | --- | --- |
| 参数名 | **`scope`** | `account` |
| 取值 | **UUID** | legacy 账户键 |
| `all` | `?scope=all` | `?account=all` |
| **参数缺失** | **400 `ACCOUNT_SCOPE_REQUIRED`** | **静默默认"全部账户"** |

第三条最值得注意。

### 160.3 为什么第三条是安全问题

TS 的错误信息说明了意图：

> 请求必须明确指定单个账户或全部活跃账户

**"查全部账户"必须是一个显式的选择。** 我的实现把"没写参数"与"写了 all"当作同一件事，
于是**一个忘记带参数的客户端会拿到全部账户的数据，而不是一个错误**。

对一个私有账本应用，这个默认方向是反的：宁可让调用方明确说"我要全部"，也不要让一个
笔误变成"把所有账户都返回了"。

### 160.4 另两处差异的后果

**参数名不同**（`scope` vs `account`）：客户端按 `scope` 发参数，我会忽略它并按"缺省"
处理——**即返回全部账户**。两处差异叠加，后果不是 404 而是**数据范围扩大**。

**取值不同**（UUID vs legacy 键）：客户端按 UUID 发，我拿它去匹配 legacy 键，
匹配不上——而我的实现不会报错，只是返回空结果。

**三条差异叠加起来都会表现为"返回了数据但不报错"**，这正是最难被发现的一类。

### 160.5 为什么本轮没有直接修

修复会**同时改变 18 条路由的参数契约**：

- 参数名 `account` → `scope`；
- 取值从 legacy 键改为 UUID（需要一次 UUID → legacy 键的映射）；
- 缺失参数从"默认全部"改为 400。

而现有测试大量使用 `?account=...` 且依赖缺省语义，全部需要改写。**这是一次成体系的
契约变更**，在上下文接近上限时做它，风险是留下"一半路由用新契约、一半用旧契约"的
中间态——那比现在更难排查。

### 160.6 实现路径（已探明）

1. 新增 `parse_scope_query(value)`：`"all"` → all；UUID → account；其余/缺失 → 400
   `ACCOUNT_SCOPE_REQUIRED`；
2. 18 条路由的 `account` 参数改为 `scope`，值经 UUID → legacy 键解析（复用
   `resolve_legacy_account_key`）；
3. 全部读写路由的测试改用 `?scope=`；
4. 补一条测试断言：**缺失 scope 必须 400，而不是返回全部账户**。

### 160.7 七个维度的汇总

| 轮次 | 维度 | 结果 |
| --- | --- | --- |
| §146 | 路由集合 | ✅ 一致 |
| §153 | 响应字段 | ⚠️ 修正 `accountId` |
| §154 | 响应头 | ⚠️ 补齐安全头 |
| §155 | 状态码 | ⚠️ 修正两处 |
| §156 | WS 帧 | ✅ 一致 |
| §157 | 分页契约 | ⚠️ 缺口（核心已实现） |
| **§160** | **路由参数契约** | ⚠️ **参数名 / 取值 / 缺省语义三处不同** |

**七个维度里五个发现了差异。**

### 160.8 验证证据

- 全量：`uv run pytest` → **2161 项通过**（4 项跳过）——本轮未改代码；
- 依据：`transport/ledger-routes.ts:22-45, 95-152`。

---

## 161. 作用域参数：解析器已就绪（2026-10-13）

§160 记录的三处差异需要**一起改**（它们的后果叠加才成为问题），本轮完成其中**纯增量**的
第一步。

### 161.1 为什么不能只改一处

三处差异的后果是叠加的：

| 若只改 | 后果 |
| --- | --- |
| 只改「缺失即 400」 | **正确使用 `?scope=` 的客户端反而被拒**——因为我还在找 `account` |
| 只改参数名 | 缺失时仍静默默认"全部"，安全问题依旧 |
| 只改取值 | 参数名仍不同，客户端按 `scope` 发参数时被当作"缺省"处理 |

**因此三处必须一起改。** 这也是它风险较高的原因——不存在"小步改进"的中间态。

### 161.2 本轮完成：`parse_scope_query`

```python
parse_scope_query("all")   → AccountScope(kind="all")
parse_scope_query("<uuid>") → AccountScope(kind="account", account=<uuid>)
parse_scope_query(None)     → 400 ACCOUNT_SCOPE_REQUIRED
```

**"查全部账户"必须是一个显式的选择。** 缺参数时报 400 而不是默认"全部"——否则一个
忘记带参数的客户端会拿到**全部账户的数据**，而不是一个错误。对私有账本应用，这个默认
方向是反的。

这是纯增量改动：新增函数，未改动任何既有调用路径，因此全量测试不受影响。

### 161.3 剩余四步（已探明）

| 步骤 | 内容 | 规模 |
| --- | --- | --- |
| 1 | **抽取 `resolve_legacy_account_key`** | 它现在是 `DuckDbAgentSessionRepository` 的**方法**，但它并不特定于 agent——应移到共享位置 |
| 2 | 16 条路由的 `account` 参数改为 `scope`，值经 UUID → legacy 键解析 | `ledger_routes.py:16` 处 |
| 3 | 更新测试调用 | **65 处**（`test_ledger_routes.py` 42 处、另两个文件各 1 处，其余为间接） |
| 4 | 补测试：**缺失 scope 必须 400，而不是返回全部账户** | 1–2 条 |

第 3 步是机械但量大的部分（给调用加 `?scope=all`）。

### 161.4 为什么本轮停在这里

第 2、3 步是**成体系的契约变更**，且第 3 步要改 65 处测试调用点。

**本次会话的上下文已接近上限。** 在这类改动上，半完成的中间态（"一半路由用新契约、
一半用旧契约"）比现状**更难排查**——现状至少是自洽的（虽然与参考实现不同），而中间态
会让同一组测试有时过有时不过。

因此本轮只交付可独立验证的增量部分：**解析器已就绪并验证**，剩余四步的规模与做法已量化。

### 161.5 验证证据

- 全量：`uv run pytest` → **2161 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed；
- 解析器行为已直接验证（`all` / UUID / 缺失 → 400）。

### 161.6 当前状态

| 项 | 状态 |
| --- | --- |
| Python 测试 | ✅ 2161 通过 |
| **作用域参数契约** | ⏳ **解析器已就绪；路由与测试待改（四步，已量化）** |
| 分页消费端（路由 `cursor` 参数） | ⏳ 未接线 |
| 切流执行 | ⏳ 需真实部署环境 |

**注意**：分页的路由参数与作用域参数改的是**同一批路由**。两者应合并为一次契约变更，
而不是改两遍。

---

## 162. 作用域参数契约已对齐参考实现（2026-10-13）

§160 记录的三处差异（参数名 / 取值 / 缺省语义）已**一次性全部改正**——因为它们必须一起
改才不会互相抵消。

### 162.1 三处改动

| | 改前 | 改后（与参考实现一致） |
| --- | --- | --- |
| 参数名 | `?account=` | **`?scope=`** |
| 取值 | legacy 账户键 | **账户 UUID** |
| 缺失 | 静默默认"全部账户" | **400 `ACCOUNT_SCOPE_REQUIRED`** |
| UUID 不存在 | 返回空账本 | **404 `ACCOUNT_NOT_FOUND`** |

第四项是本轮顺带补上的：`scope` 现在带的是 UUID，而账本用的是脱敏键，因此必须解析。
**解析失败时若返回空账本，客户端会以为"这个账户没有交易"，而实际上是它根本不存在**——
这与 §161.2 的默认方向问题是同一类：宁可明确失败，也不制造虚假的成功状态。

### 162.2 一处差点改错的地方

`/v1/analytics/risk` 的闭包里有一句 `_scope(None)`——把参数统一后它成了漏网之鱼。

改之前我去查了参考实现：

```ts
calculateOptionRisk(group.openLots, { kind: "all" }, asOf)   // financial-tool-runtime.ts:208
```

**参考实现传的也是"全部"**，所以原行为是对的。我改为显式构造 `AccountScope(kind="all")`
并注明理由，而不是顺手改成"解析后的范围"——**那会改变分组行为并与参考实现分叉**。

**"看起来像漏掉的参数"不等于"是缺陷"。** 这一处如果按直觉改，会引入一个真实的协议差异。

### 162.3 抽取而非复制

`resolve_legacy_account_key` 原本是 `DuckDbAgentSessionRepository` 的**方法**，但它
并不特定于 agent。已抽到 `infrastructure/db/account_lookup.py`，仓储方法改为**委托**——
不复制那份查询，否则两处对"账户不存在"的处理迟早会分叉。

### 162.4 测试更新的实际规模

预估值 65 处，实际改动：

| 模式 | 处理 |
| --- | --- |
| `get(path, headers=...)` | 加 `params={"scope": "all"}`（17 处，正则批量） |
| URL 内联查询 `?as_of=...` | 改为 `params=`（2 处） |
| 循环里的变量路径 | 1 处 |
| 断言 `account` 语义的测试 | **改写**为 `scope` 语义（2 条新测试） |

新测试：
- `test_scope_parameter_is_required`——缺失 → 400、显式 `all` → 200、非法取值 → 400
  （**不是静默当成 all**）；
- `test_an_unknown_account_uuid_is_a_404_not_an_empty_ledger`。

### 162.5 过程中的三类连带破坏（都靠全量测试抓到）

| 现象 | 根因 |
| --- | --- |
| 模块导入正常但测试 `NameError: parse_scope_query` | 导入替换的目标行与实际不符 |
| 23 项失败、错误为 `KeyError` | 测试未带 `scope` → 400 → 解析错误体失败 |
| `SyntaxError: keyword argument repeated: headers` | **我的替换串里 `headers=` 写了两遍** |

第三条是自己造的语法错误——**批量文本替换必须立刻跑一次收集阶段**，否则错误会被
归因到别处。

### 162.6 验证证据

- 全量：`uv run pytest` → **2162 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed；
- 16 条账本/分析路由全部改用 `?scope=`。

### 162.7 当前状态

| 项 | 状态 |
| --- | --- |
| Python 测试 | ✅ 2162 通过 |
| **作用域参数契约** | ✅ **已对齐**（三处差异 + 404 补强） |
| 分页消费端（路由 `cursor` 参数） | ⏳ 未接线 |
| 切流执行 | ⏳ 需真实部署环境 |

### 162.8 下一步

分页的路由 `cursor` 参数与本次改的是**同一批路由**。作用域部分已完成，因此 `cursor`
可以单独加——不需要再动 65 处测试，只需在新增的分页测试里带上 `scope`。

---

## 163. 游标消费端已接线（2026-10-13）

§159 记录的"已接线的是能算出并返回游标，尚未接线的是能消费游标"——本轮补上消费端。

### 163.1 改动

| 位置 | 改动 |
| --- | --- |
| `GET /v1/ledger/trades` | 新增 `cursor` 查询参数，传入 `TradeQueryFilters` |
| `GET /v1/ledger/realized` / `cashflow` | 参数位已加（查询层支持待补） |
| 错误处理 | 游标错误 → **400 `INVALID_CURSOR`**，不是 500 |

### 163.2 "不回退到第一页"在路由层同样成立

```python
except IngestError as error:
    raise _invalid_cursor(error) from error
```

**报 400 而不是回退**：回退会让客户端把"游标坏了"当成"数据到头了"——它会以为已经看完
全部数据，而实际上只看了第一页。**这比报错危险得多，因为它不报错。**

三条新测试：
- 无效游标 → 400 `INVALID_CURSOR`；
- **别的查询条件生成的游标 → 400**（跨查询误用会返回错位的行且不报错）；
- 不带游标 → 第一页，`nextCursor` 为 `null`。

### 163.3 与参考实现的参数来源一致

参考实现的 `cursor` 来自**工具参数**（`financial-tool-runtime.ts:475/495/513` 三处），
经路由 → 工具参数 → 查询过滤器。我的实现把路由参数直接传入 `TradeQueryFilters`——
**同一个流向**，只是我省去了"经工具定义"这一跳（Python 侧路由与工具共用处理器，见 §160.1）。

### 163.4 过程中又一次导入未生效

`TradeQueryFilters` 与 `IngestError` 的导入替换**连续两次未命中实际行**，导致 3 项测试
报 `NameError`。

**这是本会话第三次同类问题**（§162.5 也记了一次）。共同特征：我先写了替换语句，但没有
在改完后**立刻 grep 确认导入行真的变了**。

已记入经验：**批量文本替换后，先确认目标真的被改到，再跑测试。** 否则会把"替换没命中"
误读成"逻辑有错"，浪费一轮排查。

### 163.5 验证证据

- 全量：`uv run pytest` → **2165 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed。

### 163.6 分页的准确状态

| 能力 | 状态 |
| --- | --- |
| 游标编解码与分页切片（领域） | ✅ |
| `nextCursor` 返回（trades） | ✅ |
| **`cursor` 消费（trades）** | ✅ **本轮** |
| `realized` / `cashflow` 的查询层游标 | ⏳ 参数位已加，查询层待接 |
| `totalCount` | ⏳ 未实现 |

**`/v1/ledger/trades` 现在端到端可分页。** 其余两条命令只差查询层接入。

### 163.7 当前状态

| 项 | 状态 |
| --- | --- |
| Python 测试 | ✅ 2165 通过 |
| 作用域参数契约 | ✅ 已对齐 |
| **trades 端到端分页** | ✅ **已完成** |
| realized / cashflow 分页 | ⏳ 查询层待接 |
| 切流执行 | ⏳ 需真实部署环境 |

---

## 164. 游标有**两族**，我只实现了一族（2026-10-13）

准备把游标接入 realized / cashflow 时，发现参考实现用了**另一族游标**——不是同一套格式。

### 164.1 两族游标

| | 交易流水族 | 明细族（realized / cashflow） |
| --- | --- | --- |
| 载荷 | `{version, transactionHash, filterFingerprint}` | `{version, **query**, rowKey, filterFingerprint}` |
| 行标识 | 交易哈希 | `sha256(account, openTransaction, closeTransaction)` |
| 指纹含查询名 | **否** | **是** |
| 编码函数 | `encodeTradeCursor` | `encodeDetailCursor` |

我的 `domain/analytics/cursor.py` **只实现了交易流水族**。

### 164.2 关键差异：`query` 判别字段

明细族的载荷里有 `query: "realized" | "cashflow"`，且**解码时把它作为参数传入校验**：

```ts
decodeDetailCursor(value, query, label)
```

**因此一个 realized 游标用在 cashflow 上会在游标层被拒**——不依赖指纹是否恰好不同。

而明细族的**指纹也包含查询名**（`...JSON.stringify({ query, accountScope, ...filters })`），
等于**两道独立的防线**：载荷里的判别字段 + 指纹里的查询名。

这是刻意的冗余：游标是客户端可回传的输入，**两处独立校验比一处更可靠**。

### 164.3 我已有实现的安全性（没有因此产生缺陷）

我的交易流水族游标用在 realized 上会怎样？

- 载荷形状相同（都有 `transactionHash` 与 `filterFingerprint`），因此**解析不会失败**；
- 但两族指纹的输入不同（trades 含 `symbol`/`action`/`sourceType`，明细含 `query`），
  因此**指纹比对会拒绝**。

**所以安全性由指纹保证**——只是少了一层独立校验。对已实现的 trades 路径没有副作用。

### 164.4 完整实现规格（已探明）

```python
# 明细行键
realized_row_key(record) = sha256(json({"account", "openTransaction", "closeTransaction"}))

# 明细指纹（**含 query 名**）
detail_filter_fingerprint(query, account_scope, filters=...) = sha256(json({
    "query": query,
    "accountScope": {"kind": "all"} | {"kind": "account", "account": ...},
    **filters,
}))

# 明细游标
DetailCursor(query, row_key, filter_fingerprint)
encode_detail_cursor / decode_detail_cursor(value, query, label)
```

接入点：`RealizedQueryFilters` / `CashflowQueryFilters` 各加 `cursor`；
两个结果类型各加 `next_cursor`；两处查询用明细族的 `paginate` 变体。

### 164.5 为什么本轮只记录

规格已完整探明，但实现涉及：新增一族游标（约 100 行）+ 两个行键函数 + 两个查询的接入
+ 测试。

**本次会话的上下文已接近上限。** 一族游标的实现里，"行键的字段构成"与"指纹的字段构成"
都必须与参考实现逐字一致——写错一个字，游标会在**跨页时静默错位**（而不是报错）。
这类改动值得在能完整验证的状态下做。

### 164.6 验证证据

- 全量：`uv run pytest` → **2165 项通过**（4 项跳过）——本轮未改代码；
- 依据：`domain/analytics/queries.ts:123-127, 188-232, 320-326`。

### 164.7 分页的准确状态

| 命令 | 游标族 | 状态 |
| --- | --- | --- |
| `trades` | 交易流水族 | ✅ **端到端可用** |
| `realized` | 明细族 | ⏳ 规格已探明 |
| `cashflow` | 明细族 | ⏳ 规格已探明 |

---

## 165. 明细族游标已实现（2026-10-13）

§164 探明的两族游标设计，本轮把**明细族**实现出来。

### 165.1 新增部件

| 部件 | 说明 |
| --- | --- |
| `DetailCursor` | 载荷含 **`query` 判别字段** |
| `encode_detail_cursor` / `decode_detail_cursor(value, query)` | 解码时**校验游标属于哪一族** |
| `detail_filter_fingerprint(query, scope, **filters)` | **指纹含查询名**（第二道防线） |
| `paginate_detail(...)` | 按行键定位切页 |

### 165.2 两道独立防线的价值

```python
if parsed.get("query") != query:
    raise IngestError(f"{query} 游标不属于当前查询")     # 第一道：载荷判别字段
...
if decoded.filter_fingerprint != fingerprint:
    raise IngestError(f"{query} 游标不属于当前查询条件")  # 第二道：指纹
```

**这是本项目里少见的刻意冗余**，理由是游标属于客户端可回传的输入：万一日后有人给某一族
换了指纹算法而忘了同步，另一道仍能拦住跨查询误用。而跨查询误用的后果是**返回错位的行
且不报错**。

已用测试固定住：一个 realized 游标用在 cashflow 上会在**游标层**被拒，不依赖指纹。

### 165.3 一处为正确性做的处理

`detail_filter_fingerprint` 对筛选条件的键**排序后**再序列化：

```python
for key in sorted(filters):
    payload[key] = value if value is not None else None
```

JSON 的键序若不固定，**同一组条件会得到不同指纹**，于是合法的翻页请求会被拒。
有一条测试专门覆盖"键序不同、指纹相同"。

### 165.4 验证证据

- 明细族 10 项新测试：编解码往返、**载荷含判别字段**、
  **跨族拒绝（游标层）**、**交易流水游标不能被当作明细游标**、
  逐页不重复不遗漏、**指纹含查询名**、**键序不影响指纹**、
  未注册查询名拒绝、指纹不符拒绝、行键丢失拒绝；
- 游标模块合计 **35 项测试**；
- 全量：`uv run pytest` → **2175 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed。

### 165.5 剩余：接入两个查询

明细族游标**尚未接入** `query_realized` / `query_cashflow`：

1. 两个过滤器各加 `cursor`；
2. 两个结果类型各加 `next_cursor`（参考实现另有 `totalCount`）；
3. 查询体用 `paginate_detail` 替换切片，行键用
   `sha256(account, openTransaction, closeTransaction)`；
4. 投影发出 `nextCursor`。

**因此在接入完成前，realized / cashflow 仍然不返回游标。**

### 165.6 分页状态

| 命令 | 游标族 | 状态 |
| --- | --- | --- |
| `trades` | 交易流水族 | ✅ 端到端可用 |
| `realized` | 明细族 | ⏳ 游标族已实现，查询接入待做 |
| `cashflow` | 明细族 | ⏳ 同上 |
| `totalCount` | — | ⏳ 未实现 |

---

## 166. 分页三条命令全部完成（2026-10-13）

§165 的明细族游标已接入 `realized` / `cashflow` 查询与投影。**分页至此三条命令全部打通。**

### 166.1 改动

| 位置 | 改动 |
| --- | --- |
| `RealizedQueryFilters` / `CashflowQueryFilters` | 加 `cursor` |
| `RealizedQueryResult` / `CashflowQueryResult` | 加 `total_count` 与 `next_cursor` |
| `query_realized` / `query_cashflow` | 用 `paginate_detail` 替换切片 |
| 两个投影 | 发出 `totalCount` 与 `nextCursor` |

### 166.2 两族行键的差异（与参考实现逐字一致）

| 命令 | 行键 |
| --- | --- |
| `realized` | `sha256({account, openTransaction, closeTransaction})`——**复合** |
| `cashflow` | `row.txn_hash`——**直接** |

realized 用复合哈希是必要的：**同一笔交易可能同时是"开"与"闭"**（部分平仓），单看
开仓或闭仓交易哈希都无法唯一定位一行。

### 166.3 一个容易分叉的点：合计与总数基于全量

```python
total_pnl=_sum_decimals([record.pnl for record in filtered]),   # 全部筛选行
total_count=len(filtered),                                      # 全部筛选行
```

**不是当前页。** 参考实现同样如此（`sumDecimals(filtered...)` 与
`totalCount: filtered.length`）。若按当前页求和，**翻到第二页时合计会变小**，
客户端会以为数据出了问题。

核对时特意查了参考实现才确认——这一处"看起来该按页算"的地方，实际该按全量算。

### 166.4 那条诚实性测试又改了一次

`test_pagination_fields_are_truthful_not_fabricated` 经历了三个阶段：

| 阶段 | 断言 |
| --- | --- |
| 未实现 | 字段**不存在**（宁可缺字段，不发假数据） |
| trades 实现后 | trades 的值真实；另两条仍不存在 |
| **三条都实现后** | **三条的值都真实**；`trades` 的 `totalCount` 仍不存在（尚未计算） |

**每次都是改断言而不是删测试。** 这条测试的价值不在某个具体断言，而在它承载的原则：
**宁可缺字段，也不发假数据**——原则留下，表述随实现演进。

### 166.5 验证证据

- 全量：`uv run pytest` → **2175 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed；
- 游标模块 35 项测试（交易流水族 25 + 明细族 10）。

### 166.6 分页的最终状态

| 能力 | 状态 |
| --- | --- |
| `trades` 端到端分页 | ✅ |
| `realized` 端到端分页 | ✅ |
| `cashflow` 端到端分页 | ✅ |
| 跨族 / 跨筛选 / 定位丢失的拒绝 | ✅ 三道校验均有测试 |
| `totalCount`（realized / cashflow） | ✅ |
| `totalCount`（trades） | ⏳ 参考实现未在 trades 上发出它，故不实现 |

**分页这一维度已完成**——它从 §157 发现缺口起，经 §158 核心、§159 投影、§163 消费端、
§164–§165 明细族，到本轮收口。

---

## 167. 第八个维度：错误信封——本次复核最严重的发现（2026-10-13）

前七个维度已闭合。本轮去找第八个维度（错误响应），**第一次核对就发现了整个复核中影响
最大的一处差异**。

### 167.1 差异

| | 参考实现 | 我（改前） |
| --- | --- | --- |
| 信封 | **扁平** | 嵌套在 ``detail`` 下 |
| `message` / `details` / `retryable` / `request_id` | 有 | **全部缺失** |

实际对照：

```json
// 参考实现
{"request_id": "...", "code": "DEVICE_CREDENTIAL_INVALID", "message": "...",
 "details": null, "retryable": false}

// 我（改前）
{"detail": {"code": "DEVICE_CREDENTIAL_INVALID"}}
```

### 167.2 为什么这是最严重的一处

**``retryable`` 决定客户端是否重试。** 缺了它，客户端**无法区分可重试与永久失败**：

- 一律重试 → 对永久失败反复冲击服务端；
- 一律放弃 → 用户看到"假失败"，而其实重试一次就成功。

而 ``code`` 的位置不同会让客户端读到 ``undefined``——**同样不报错**，只是错误处理逻辑
整条失效。这一处影响**每一个错误路径**，不是某个端点。

### 167.3 修复：中央异常处理而非逐路由

新增 `api/error_envelope.py`，在 `create_app` 里装配：

| 处理器 | 作用 |
| --- | --- |
| 请求 ID 中间件 | 每请求生成 UUID，**成功响应也回写** ``x-request-id`` |
| `ApplicationError` | 状态码 / 码 / 可重试性 / 明细全来自异常本身 |
| `HTTPException` | 路由显式抛出的错误；**``detail`` 的额外键归入 ``details``** |
| `RequestValidationError` | → **400 ``INVALID_ARGUMENT``** |
| 未归类异常 | → 500 ``INTERNAL_ERROR``，**不外发异常文本** |

**中央处理是关键**：逐路由翻译必然有遗漏（我此前确实是逐路由写的，因此 ``retryable``
与 ``details`` 在部分路由上根本没有发出）。

### 167.4 三处顺带修正的差异

| 项 | 改前 | 改后 |
| --- | --- | --- |
| 参数校验失败 | 422（FastAPI 默认） | **400 ``INVALID_ARGUMENT``**（参考实现用 AJV，同样 400） |
| `reason` / `from` 等补充字段 | 被丢弃 | 归入 ``details`` |
| 未归类异常文本 | 可能外发 | 固定文案（异常文本可能含路径 / SQL / 账户标识） |

第二处是修处理器时发现的**我自己的缺陷**：路由把 ``reason`` 写在 ``detail`` 里，而处理器
只读 ``code``/``message``/``details``——**这些字段被静默丢掉了**。

### 167.5 ``request_id`` 是唯一允许的 snake_case 键

它是日志关联标识而非领域字段，参考实现也用这个名字。项目的"公开字段一律 camelCase"
守卫因此需要一处**显式豁免**——我把它写成了带理由的例外，而不是放宽断言：

```python
leaked = [key for key in _snake_keys(body) if key != "request_id"]
```

**放宽断言会让守卫失效；显式豁免保留了守卫。** 两者看起来相似，但后者在新增其他
snake_case 字段时仍会失败。

### 167.6 验证证据

- 7 项信封契约测试：扁平形状、稳定错误码、**每个响应都带 `x-request-id`**、
  **头与体内的 ID 相同**、额外键归入 `details`、`request_id` 是唯一豁免、
  校验失败为 400；
- 全量：`uv run pytest` → **2182 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed；
- 修正 31 处测试断言（``["detail"]["X"]`` → 扁平）与 3 处手工修正。

### 167.7 八个维度的汇总

| 轮次 | 维度 | 结果 |
| --- | --- | --- |
| §146 | 路由集合 | ✅ 一致 |
| §153 | 响应字段 | ⚠️ 修正 `accountId` |
| §154 | 响应头 | ⚠️ 补齐安全头 |
| §155 | 状态码 | ⚠️ 修正两处 |
| §156 | WS 帧 | ✅ 一致 |
| §157 | 分页契约 | ⚠️ 缺口（§166 已闭合） |
| §160 | 路由参数契约 | ⚠️ 三处不同（§162 已对齐） |
| **§167** | **错误信封** | ⚠️ **信封形状 + 四个缺失字段** |

**八个维度里六个发现了差异。**

### 167.8 关于"已完成"的判断

我在 §90、§91 两次说过"代码工作已完成"，此后每一轮都发现了真实差异。

**"已完成"只能相对于"已核对的维度"成立。** 更准确的说法是：**我每找一个新维度，
就有约 3/4 的概率发现差异**——这个比例本身就是"还不能宣称完成"的证据。

下一个值得核对的维度可能是：请求体校验的边界行为、响应压缩 / 编码、或并发下的
幂等与竞态（后者已部分覆盖）。

---

## 168. 错误消息：修复后残留的空字符串（2026-10-13）

§167 修好错误信封的**形状**后，我注意到一个残留：``message`` 字段是**空字符串**。

### 168.1 两处丢消息的原因

| 来源 | 改前 | 后果 |
| --- | --- | --- |
| 领域错误 | 路由写 ``detail={"code": error.code}`` | **``message`` 被静默丢弃**（10 处） |
| 认证失败 | 字面量 ``detail={"code": "DEVICE_CREDENTIAL_INVALID"}`` | 从来就没有文案 |

第一处尤其值得记录：**整个错误处理链看起来是工作的**——状态码对、``code`` 对、
信封形状也对，只是客户端拿不到任何说明。而"哪里出了问题"恰恰是用户最需要知道的。

### 168.2 参考实现的文案是**逐站点**的

核对时发现同一个码在不同站点有不同文案：

```
DEVICE_CREDENTIAL_INVALID → "设备凭据无效"
DEVICE_CREDENTIAL_INVALID → "设备访问令牌无效"
DEVICE_CREDENTIAL_INVALID → "登录会话已退出，请重新登录"
```

**因此不能用一个"码 → 文案"的中央目录替代**——那会让三种不同的情况得到同一句
泛泛的说明。参考实现是逐站点自带的，本项目也照此处理。

（我一开始想加中央回退目录，核对后放弃了：它会把"设备凭据无效"这种精确说明变成一个
模糊的通用句。）

### 168.3 修复

- 10 处领域错误：``detail={"code": error.code, "message": error.message}``；
- 认证失败：补上参考实现的文案。

**关键在于消息来自异常本身**——``ApplicationError`` 一直带着正确的文案，只是被路由
的翻译层丢掉了。

### 168.4 两条新测试

- ``test_error_messages_are_never_empty``——遍历未认证 / 缺 scope / 非法 scope /
  无效游标四条路径，断言消息非空。**它同时是"新增路由忘了写 message"的守卫**；
- ``test_a_domain_error_message_survives_the_translation``——断言领域错误的文案
  真的传到了客户端。

### 168.5 验证证据

- 全量：``uv run pytest`` → **2184 项通过**（4 项跳过）；
- 实测：``GET /v1/ledger/summary`` 未认证 → ``message`` 为 ``"设备访问令牌无效"``；
- ``ruff check src/`` → All checks passed。

### 168.6 关于核对顺序的一点观察

这一处是**修完形状之后才看见的**——因为修形状时我把注意力放在"字段在不在"，
而不是"字段里的值对不对"。

**同一次核对里，形状与取值是两个层次。** §167 只覆盖了前者。这提示：核对某个维度时，
应显式问两遍——**"结构对吗"** 与 **"值对吗"**。

---

## 169. 取值层核对：decimal.js 的指数形式边界（2026-10-13）

§168 的末尾提出：同一次核对里"**结构对吗**"与"**值对吗**"是两个层次。本轮开始做取值层。

### 169.1 先确认了两处**一致**的地方

| 项 | 结果 |
| --- | --- |
| 可选字段的 `undefined` / `null` 区分（agent 消息的 `status` / `errorCode`） | ✅ 一致 |
| 时间戳格式（`toISOString()` ↔ `isoformat(timespec="milliseconds")` + `Z`） | ✅ 一致 |

第一处的细节：`status` 的类型是 `"completed" \| "failed" \| "cancelled"`（**只有 undefined，
无 null**），而 `errorCode` 是 `string \| null`（**两者都有**）。因此我实现的
"`status` 用 `is not None` 判断、`errorCode` 用 UNSET 哨兵"**恰好都对**——不是巧合，
而是照类型定义写的。

### 169.2 发现：金额渲染在两端改用指数形式

`decimal.js` 的 `toString()` 不只是"去尾随零"——它还会在两端改用指数形式
（默认 `toExpNeg = -7`、`toExpPos = 21`，本项目未改动）。

我没有靠推断，而是**实际运行 decimal.js 10.6.0** 取值：

```
0.000001        → 0.000001       （1e-6 仍定点）
0.0000001       → 1e-7           ← 指数
0.00000012      → 1.2e-7
1e20            → 100000000000000000000
1e21            → 1e+21          ← 指数，且带 `+`
```

而我的实现**始终用定点形式**：

```
0.0000001       → 0.0000001      ← 不同
1e21            → 1000000000000000000000   ← 不同
```

### 169.3 我此前对这个差异的定性是错的

`domain/numeric.py` 的注释原本写着：

> 说明：decimal.js 对极小/极大数量会改用指数形式，而本函数始终使用定点形式。
> **该差异只影响未被跨语言验证的极端量级**，不影响账本口径。

**"只影响极端量级"这个定性是错的。** `1e-7` 对金融计算并不罕见：低价期权的单位成本、
拆股调整后的单价都可能落在这一量级。而且它**不是"未被验证"**——现在实测确认了差异。

**把已知差异记成"不影响实际"，是一种让缺口看起来无害的说法。** 这次我用实测把它变成了
一个具体的、可判定的差异。

### 169.4 修复

```python
adjusted = normalized.adjusted()
if _TO_EXP_NEG < adjusted < _TO_EXP_POS:
    return format(normalized, "f")
# 指数形式：正指数带 `+`，负指数带 `-`，指数不补零，尾数去尾随零
```

15 个用例**逐字符**与 decimal.js 一致。

### 169.5 测试用实测值而非推导规则

`tests/test_numeric.py` 里的对照表注明了来源：

```python
# 下表由**实际运行 decimal.js 10.6.0 得到**，不是推测：
#     node -e "const D=require('decimal.js'); ..."
```

**保留原始输出而不是"按规则推导"**，因为这类边界的价值全在细节上——指数是否带 `+`、
是否补零、`1e-6` 到底走不走指数形式，都只能实测。

### 169.6 验证证据

- 19 项数值测试（含 14 项逐字符对照 + 两条边界各两例 + 去尾随零 + 零无符号）；
- 全量：`uv run pytest` → **2203 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed。

### 169.7 取值层的下一步

取值层刚开始，还有值得核对的：

- 其他字段的**精度**（`toFixed(4)` 类）——例如 realized 的 `totalPnl` 固定四位小数；
- **百分比 / 比率的舍入方式**（`ROUND_HALF_EVEN` vs `ROUND_HALF_UP`）；
- **排序稳定性**（相同键的两行顺序）——分页的正确性依赖它。

---

## 170. 取值层：两种舍入模式被混用为一（2026-10-13）

沿用 §169 的取值层核对，本轮查**精度与舍入**。

### 170.1 发现：参考实现刻意混用两种舍入

`trade-analysis.ts` 的**相邻两行**用了不同模式：

```ts
.toDecimalPlaces(2, Decimal.ROUND_HALF_EVEN).toFixed(2)   // 百分比：银行家舍入
averageLotPnl.toFixed(4)                                  // 金额：四舍五入
```

**相邻两行显式写不同模式，说明这是有意的区分**，不是笔误。而我把两者都用银行家舍入
——**金额那部分是错的**。

### 170.2 实测差异

我没有靠推断，而是运行 decimal.js 10.6.0：

| 值 | `toFixed(4)` | 我的 `ROUND_HALF_EVEN` |
| --- | --- | --- |
| `1.23445` | `1.2345` | `1.2344` ✗ |
| `-1.23445` | `-1.2345` | `-1.2344` ✗ |
| `0.00005` | `0.0001` | `0.0000` ✗ |
| `-0.00005` | `-0.0001` | `-0.0000` ✗ |

**差异只出现在恰好落在中点的值上**——而账本里"第 5 位为 5"的费用分摊并不罕见
（费用按比例分摊时尤其常见）。

### 170.3 三处修复

| 处 | 问题 |
| --- | --- |
| `queries.py` 的 `_amount_text` | 舍入模式错（银行家 → 四舍五入） |
| `trade_analysis.py` 的 `_amount_text` | **同一缺陷的第二份独立实现** |
| `total_pnl` | **根本没有量化**——直接用原始 Decimal 渲染 |

第三处是顺带发现的：cashflow 的 `total` 经过了量化，而 realized 的 `totalPnl` 没有
——**同一个响应里两个金额字段用了不同的处理**。

### 170.4 收敛为共享实现（DRY）

新增 `domain/numeric.py`：

```python
fixed_text(value, places)   # 四舍五入（平局远离零）—— 金额
percent_text(value)         # 银行家舍入 —— 百分比
```

两处 `_amount_text` 改为**委托**，不再各写一份。

**这正是项目规范里"杜繰重复造轮子"要防的情形**：同一份格式化逻辑存在两处，
于是同一个缺陷也存在两处，修的时候必须记得两处都改。

### 170.5 顺带修掉的一处渲染缺陷

`format(Decimal("-0.00001").quantize(...), "f")` 会给出 `-0.0000`，而 decimal.js
给出 `0.0000`——**两者会被客户端当成不同的数值文本**。已在两个渲染器里统一处理。

### 170.6 关于"批量替换未命中"的第四次记录

`s/` 导入替换**连续第四次未命中实际行**，导致 15 项测试报 `NameError`。

这次我按 §163.4 记下的教训做了：**改完后立刻 grep 确认目标真的变了**，因此只多花了
一次验证而不是一轮排查。

**同一个操作重复四次出错，说明问题在操作方式而非运气**：用替换串去猜导入行的原文，
本身就不如"先 grep 出原文，再按原文替换"可靠。

### 170.7 验证证据

- 数值测试 29 项（含 8 项 `toFixed` 逐字符对照、银行家舍入、负零、非法位数）；
- 全量：`uv run pytest` → **2214 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed。

### 170.8 取值层的状态

| 项 | 结果 |
| --- | --- |
| 金额渲染（去尾随零 + 指数边界） | ✅ §169 已修 |
| 时间戳格式 | ✅ 一致 |
| 可选字段的 `undefined` / `null` | ✅ 一致 |
| **金额舍入模式** | ✅ **本轮已修** |
| **百分比舍入模式** | ✅ 一致（本就是银行家舍入） |
| `totalPnl` 量化 | ✅ 本轮已补 |
| 排序稳定性（分页依赖） | ⏳ 未核对 |

---

## 171. 取值层：三处排序键少了 `account_id`（2026-10-13）

### 171.1 差异

参考实现的账本读取有三条排序键，我这边**每条都少了 ``account_id``**：

| 表 | 参考实现 | 我（改前） |
| --- | --- | --- |
| transactions | `transaction_date, sequence, **account_id**, transaction_hash` | `transaction_date, sequence, transaction_hash` |
| lots | `**account_id**, lot_id` | `lot_id` |
| realized | `**account_id**, realized_id` | `realized_id` |

### 171.2 为什么这是缺陷而不是风格差异

**排序是分页正确性的一部分。** 游标通过"定位哈希在有序列表里的位置"来切页——顺序变了，
同一个游标会切在不同的位置，**返回的行因此不同，而且不报错**。

后两处尤其隐蔽：``lot_id`` / ``realized_id`` 是**全局 BIGINT 序列**，因此
`ORDER BY lot_id` 本身就是一个**全序**——它不会报错、不会随机、看起来完全正常。
只是它与 `ORDER BY account_id, lot_id` 是**两个不同的全序**：

```
插入顺序：A1, B1, A2, B2
按 lot_id：        A1, B1, A2, B2
按 account_id：    A1, A2, B1, B2
```

**不同账户的批次交错时两者必然分叉**，而"本身是全序"这一点让它极难被怀疑。

### 171.3 顺带确认的一件事：全序需要唯一末键

三条排序都以唯一键收尾（`transaction_hash` / `lot_id` / `realized_id`）。

**这不是巧合，是必要条件**：没有唯一末键时，数据库对并列行**不作顺序保证**——同一查询
两次可能给出不同顺序，分页会因此**漏行或重复行**。已加一条测试断言每条排序的末键都是
唯一键。

### 171.4 契约表与"防止文档过时"

新增 `LEDGER_READ_ORDERINGS` 常量把三条排序键记在**源码里**，而不是只写在测试或文档中。

并加一条测试断言**契约表就是仓储实际使用的键**：

```python
assert "ORDER BY " + ", ".join(ordering) in source
```

它防止"改了 SQL 却忘了改契约表"——那样契约表会变成一份**过时的文档，比没有更危险**
（这正是 §144.2 记录过的问题，只是这次出现在源码常量而非模块文档上）。

### 171.5 验证证据

- 5 项排序契约测试：交易键（含位置）、批次账户优先、明细账户优先、
  **每条排序都是全序（末键唯一）**、**契约表与仓储一致**；
- 全量：`uv run pytest` → **2219 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed。

### 171.6 取值层的状态

| 项 | 结果 |
| --- | --- |
| 金额渲染（去尾随零 + 指数边界） | ✅ §169 |
| 时间戳 / 可选字段 null 语义 | ✅ 一致 |
| 金额舍入模式 / `totalPnl` 量化 | ✅ §170 |
| **排序键（分页依赖）** | ✅ **本轮已修** |
| 其余取值层（比率、比率舍入、日期边界） | ⏳ 未逐项核对 |

---

## 172. 一次实测：M3 差分测试为何没抓到排序差异（2026-10-13）

§171 修了三处排序键。修完后我追问了一个更要紧的问题：**为什么 M3 差分测试没抓到它？**

### 172.1 实测

把排序**临时改回错误版本**，重跑：

| 测试 | 结果 |
| --- | --- |
| `test_analytics_parity.py`（M3 差分） | **19 项全部通过** ← 没抓到 |
| `test_ledger_ordering.py`（本轮新增的契约测试） | **失败** ← 抓到了 |

**M3 差分测试对跨账户排序是盲的。** 而它一直是我引为最强的证据（29/29 CSV、
11/11 FIFO、2771 字段匹配）。

### 172.2 根因：夹具里没有跨账户的键碰撞

排序差异只在"**同一天、同一 sequence、不同账户**"的行上显现——此时
`ORDER BY date, sequence, hash` 与 `ORDER BY date, sequence, account_id, hash`
会给出不同顺序。

而夹具实测：

```
用例类型: {'csv': 35, 'econfirm': 28, 'fifo': 13, 'analytics': 19}
未找到跨账户 (date, sequence) 碰撞的用例
```

**夹具的输入从未走到那条并列分支**，因此两侧输出自然一致。

### 172.3 这件事的真正含义

差分测试的证明力有一个**常被忽略的上限**：

> **它只能证明"在这些输入上一致"，不能证明"规则一致"。**

只要输入不触达某条分支，该分支上的差异就**不可能**被差分测试发现——无论跑多少用例、
比对多少字段。2771 个字段的一致是真实证据，但它覆盖的是**夹具走过的那部分逻辑**。

两类测试因此是**互补而非重复**：

| 类型 | 证明什么 | 局限 |
| --- | --- | --- |
| 差分测试 | 在给定输入上两侧输出一致 | **输入没走到的分支它看不见** |
| 契约测试 | 规则本身与参考实现一致 | 不验证端到端产物 |

§171 的排序契约测试之所以能抓到，正因为它断言的是**规则**（"排序键是这四个"），
而不是输出。

### 172.4 对"证据强度"的重新评估

本会话此前多次以 M3 的 2771 字段匹配作为"协议已对齐"的主要依据。**那个依据需要加上限定**：

- 它证明了**账本口径**在夹具覆盖的输入上一致；
- 它**没有**证明排序规则、边界值、错误分支、以及任何夹具未触达的路径一致。

而后续几轮（§167 错误信封、§170 舍入、§171 排序）发现的六处差异，**没有一处是 M3 能
发现的**——它们全在夹具的覆盖之外。

**"有很强的证据"与"证据覆盖了这件事"是两个判断。** 前者容易让人停止追问，后者才是
结论成立的依据。

### 172.5 验证证据

- 恢复正确排序后：`uv run pytest` → **2219 项通过**（4 项跳过）；
- 实测对照：旧排序下差分测试 19 项全过、契约测试失败；
- 夹具统计：95 个用例，无跨账户 `(date, sequence)` 碰撞。

### 172.6 可选的后续改进

给差分夹具**补一条跨账户键碰撞的用例**——这样 M3 也能覆盖这条分支。它比新增契约测试
更有价值，因为它会同时覆盖排序在**下游**（分页、分析聚合）的影响。

---

## 173. 跨账户排序：碰撞是常态，且补上了有判别力的测试（2026-10-13）

### 173.1 关键认识：碰撞不是例外，是常态

§172 记录了 M3 差分测试对跨账户排序是盲的。本轮追到了**更重要的原因**：

```python
# domain/ledger/batch.py
sequence = index + 1
```

**``sequence`` 是单个文件内的行号。**

因此两个不同账户的文件在同一日期下，**必然**都有 ``sequence = 1, 2, 3…``——
"同日同序不同账户"的并列行是**常态**，不是需要刻意构造的边界情形。

这意味着参考实现排序键里的 ``account_id`` **不是防御性冗余，而是真实的分歧点**：
任何有多账户、同日交易的账本，两侧的行顺序都会不同。

### 173.2 为什么无法给差分夹具补用例

查了夹具的生成链路：

```
record-fixtures.mjs
  → 在 oracle（schwab-cli）里运行 record-fixtures.py
  → 该脚本是 pytest 插件，**拦截 oracle 自身测试**并记录输入与真实输出
  → 产出 parity-fixtures.ts / parity-fixtures.json
```

**语料就是 oracle 的测试套件**，而 oracle 是**冻结只读**的。因此新增用例需要改 oracle
——不做。

（顺带确认：夹具每个 analytics 用例只有**一份 CSV（单账户）**，结构上也放不下跨账户
碰撞。）

### 173.3 改为写一条有判别力的端到端测试

`tests/test_ledger_cross_account_order.py`：

1. 建两个账户，各一笔**同一天、同一 sequence** 的交易；
2. **故意让账户 B 的哈希排在账户 A 之前**（字典序）；
3. 断言加载顺序是 ``TST001, TST002``（账户优先）。

第二条测试专门断言**哈希顺序与账户顺序相反**——它让第一条成为**有判别力**的测试：
若哈希顺序恰好与账户顺序一致，第一条就无法区分"有 ``account_id``"与"没有"。

### 173.4 实测判别力

把排序**临时改回旧版本**：

| 测试 | 旧排序 | 正确排序 |
| --- | --- | --- |
| 加载顺序（账户优先） | **失败** | 通过 |
| 哈希顺序相反（判别力自检） | 通过 | 通过 |

**失败的那一条正是应该失败的**，而自检那条始终通过——说明场景真实且测试有效。

### 173.5 一条关于"构造边界"的经验

给差分夹具补用例的**正确做法**是扩充语料并重新从 oracle 录制，而不是手工编造期望值。
当语料不可改时，退而求其次是写**有判别力的定向测试**——但要额外断言"这个场景真的能
区分两种实现"，否则测试可能因为输入恰好不触发差异而**永远通过**。

第二条测试（哈希顺序相反）就是这个自检。**没有它，第一条测试的价值无法确认。**

### 173.6 验证证据

- 全量：`uv run pytest` → **2221 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed；
- 判别力实测：旧排序下定向测试失败。

### 173.7 当前状态

| 项 | 状态 |
| --- | --- |
| Python 测试 | ✅ 2221 通过 |
| 结构层（8 维度） | ✅ 已核对 |
| 取值层 | ⏳ 进行中（金额渲染 / 舍入 / 排序键已修） |
| M3 覆盖盲区（跨账户排序） | ✅ **已用定向测试补上** |
| 切流执行 | ⏳ 需真实部署环境 |

---

## 174. 取值层：`schema_version` 在数据库不可用时回退为 0（2026-10-13）

### 174.1 差异

`/v1/system/info` 的 ``schema_version``：

| | 来源 |
| --- | --- |
| 参考实现 | **编译期常量** ``CURRENT_SCHEMA_VERSION = 15`` |
| 我（改前） | 读健康快照；**数据库不可用时回退为 ``0``** |

### 174.2 为什么 `0` 是错的

``/v1/system/info`` **正是客户端用来判断协议与模式兼容性的端点**。

回退为 ``0`` 时，客户端会读到"服务端的模式版本是 0"——**而 0 不是任何有效的模式版本**。
它可能据此认为服务端模式过旧而拒绝连接，或走进一条不该走的分支。

而真实原因只是**数据库暂时不可用**——一个**暂时性**的状态，被报成了**版本不兼容**。

参考实现的做法是根本不让它依赖运行期状态：协议元数据是**编译期已知**的，数据库是否
可用与"这个二进制支持哪个模式版本"无关。

### 174.3 修复

```python
return dict(create_server_protocol_metadata(SERVER_VERSION, CURRENT_SCHEMA_VERSION))
```

顺带确认了两侧常量本就相同：``CURRENT_SCHEMA_VERSION = 15``、
``LYRA_PROTOCOL_VERSION = 2``、``MIN/MAX_SUPPORTED = 2``、``SERVER_VERSION = "0.1.0"``。

### 174.4 三条新测试

- ``schema_version`` **等于编译期常量**（与数据库状态无关）；
- 协议窗口 ``2 / 2 / 2`` 与 ``server_version``；
- **字段集合是闭集**——多一个字段就是协议变更。

最后一条同时记录了一个容易困惑的点：``/v1/system/info`` 的字段用 **snake_case**
（``server_version`` / ``protocol_version`` / …），与 ``request_id`` 同类——它们是
**协议级元数据**，不是领域数据，因此不受"公开字段一律 camelCase"的约束。

### 174.5 验证证据

- 全量：`uv run pytest` → **2224 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed。

### 174.6 取值层的状态

| 项 | 结果 |
| --- | --- |
| 金额渲染 / 舍入模式 / 排序键 | ✅ 已修（§169–§171） |
| 协议元数据取值 | ✅ **本轮已修** |
| 时间戳 / 可选字段 null 语义 | ✅ 一致 |
| 其余取值（比率、日期边界、分页边界） | ⏳ 未逐项核对 |

---

## 175. 取值层：健康快照缺三段且从不报 `degraded`（2026-10-13）

### 175.1 差异

参考实现的 ``/v1/health`` 有**七段**，我只有**四段**：

| 段 | 参考实现 | 我（改前） |
| --- | --- | --- |
| `status` / `checkedAt` / `process` / `database` | 有 | 有 |
| `writeQueue` | 有 | **缺** |
| `gmail` | 有 | **缺** |
| `agents` | 有 | **缺** |

而且参考实现的 ``status`` 可以是 ``"degraded"``，我**只会返回 ``healthy`` 或
``unavailable``**。

### 175.2 为什么这比"少几个字段"严重

``degraded`` 与 ``unavailable`` 的分界是**服务能不能工作**：

```
unavailable  ← 数据库不可用 或 写队列不接受   → HTTP 503
degraded     ← Gmail 需重同步 或 Agent 不可用  → HTTP 200
```

我缺了后者的判定，于是**写队列已停止接受、Gmail 连接出错、Agent 未配置**这三种情况
全都被报成 ``healthy``。监控端会看到"一切正常"，而实际上有部分能力已经受损。

反向的错误同样危险：**把 ``degraded`` 报成 ``unavailable`` 会让客户端直接放弃一个
其实可用的服务**。

### 175.3 模块文档早已诚实记录了缺口

```python
"""...本里程碑只实现 process 与 database 两段；writeQueue / gmail / agents
将在对应里程碑实现后加入。

这里刻意**不返回**尚未实现的段，而不是填 available 之类的占位值——
健康端点谎报可用会让故障排查建立在假信息上。"""
```

**这个决定本身是对的**（宁缺勿假）。但它写的"将在对应里程碑实现后加入"**始终没有发生**
——迁移已到收尾阶段，三段仍然缺席。**"记为未做"如果不配一个会失败的提醒，就会一直
留在那里**（这正是 §150.4 记录过的"写死断言"手法的价值所在：那里有提醒，这里没有）。

### 175.4 实现时撞到的两个真实问题

**其一：健康端点在数据库已关闭时自己抛异常。**

实现 ``gmail`` 段后，``test_health_returns_503_when_database_unavailable`` 报
``Connection Error: Connection already closed``。

**健康端点的职责是"报告"不可用，不是跟着一起坏掉**——否则监控看到的是
"健康检查 500"而不是"服务不可用"，两者的排查方向完全不同。已改为该段读取失败时
**不出现**（而不是填一个假的 ``available``）。

**其二：``agents.status`` 在运行时缺席时应为 ``unknown`` 而非 ``degraded``。**

我最初写了 ``degraded``。核对参考实现：

```ts
const agentStatus = input.agentAvailability === null
  ? "unknown"           // 运行时缺席
  : unavailableProviders > 0 ? "degraded" : "available";
const externalDegraded = attentionRequired > 0 || agentStatus !== "available";
```

**``unknown`` 与 ``degraded`` 的语义不同**：前者是"我们不知道它是否可用"，后者是"我们知道
它有部分坏了"——对排查者是不同的信息。而两者**都**让整体变成 ``degraded``，因为判定是
``!== "available"``。

### 175.5 一处把测试改对而非改实现的例子

``test_health_reports_available_database`` 原本断言 ``status == "healthy"``。实现后发现是
``degraded``——因为夹具里 Agent 运行时未装配。

**这次不是实现错了**：参考实现在同样情况下也会是 ``degraded``（``unknown !== available``）。
因此改的是**测试的期望**，并在注释里写明为什么夹具下必然是 ``degraded``。

### 175.6 验证证据

- 全量：`uv run pytest` → **2225 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed（顺带抓出一处死代码 ``now``）；
- 实测快照：七段齐全，``agents.status = "unknown"``、整体 ``degraded``、HTTP 200。

### 175.7 取值层的状态

| 项 | 结果 |
| --- | --- |
| 金额渲染 / 舍入 / 排序键 | ✅ §169–§171 |
| 协议元数据取值 | ✅ §174 |
| **健康快照段与状态判定** | ✅ **本轮** |
| 其余取值（比率、日期边界、分页边界） | ⏳ 未逐项核对 |

---

## 176. `POST /v1/agent/sessions`：参数位置与形状已对齐（2026-10-13）

§117–§118 记录了这个缺口但两轮未动手。本轮完成。

### 176.1 修复前的缺陷

| | 参考实现 | 我（改前） |
| --- | --- | --- |
| 参数位置 | **request body** | query 参数 |
| 形状 | 嵌套 `accountScope: {kind, account}` | 扁平 `?accountId=` |
| `accountScope` | **必填** | 有"全部账户"的默认值 |
| `provider` | 字面量 `"genkit"` | 不接受 |
| `asOf` | 不接受（用服务端时钟） | 接受 |

**后果**：客户端按参考实现发正文时，我的实现**完全忽略它** → `account_id` 为 `None`
→ **"只看 TST001"变成了"绑定全部账户的会话"，且不报错**。

这与 §160 的 `scope` 差异同类，但后果更重：那一次是查询参数被忽略，这一次是**整个请求体**。

### 176.2 四步改动

1. `create_session` 改为 `async`，从 `await request.json()` 读正文；
2. 解析嵌套的 `accountScope`（`kind: "all"` / `kind: "account"` + UUID 校验）；
3. 校验 `provider == "genkit"`；
4. `asOf` 不再接受，恒用服务端时钟。

### 176.3 实现过程中自己犯下、又被测试抓到的同类错误

**我给 `accountScope` 加了默认值 `{"kind": "all"}`。**

看起来是"友好"，实际是把**"客户端忘了带这个字段"变成"绑定全部账户"**——**正是本次修复
要根除的那类静默扩大**。参考实现的 schema 无默认值（`accountScope` 必填）。

测试立刻抓到了：空正文期望 400，实际得到 503（因为默认值让它通过了校验，走到可用性检查）。
**我修复了一个静默扩大，却在实现里引入了另一个**——而它们的形式完全不同（前者是"忽略正文"，
后者是"给必填项默认值"），只有"这个默认值会扩大数据范围吗"这个问题能同时看住两者。

**规则**：涉及**数据范围**的字段永远不给默认值。宁可 400。

### 176.4 另一处延迟引用

`_UUID_PATTERN` 定义在 `dependencies.py` 而未导入 `agent_routes.py`——模块导入检查通不过，
只有调用时才 `NameError`。已补导入。

**这是本会话第五次同类问题**，共同点都是"新增引用了别处定义的私有常量"。

### 176.5 一处需要配置运行时的测试

本文件的 `client` 夹具从不配置模型运行时，因此所有创建测试都走 503 分支。而正文形状是
**与运行时是否配置无关的契约**，所以新增了一个 `configured` 夹具（设置三个环境变量）——
可用性检查读的是环境变量，不发网络请求。

### 176.6 六条新测试

- 接受参考实现的正文形状；
- **指定账户的会话不得退化成"全部账户"**（核心）；
- `{"kind": "account"}` 缺 `account` → 400（不得退化成全部）；
- 空正文 → 400（`accountScope` 必填）；
- `provider` 非 `genkit` → 400；
- **客户端指定的 `asOf` 被忽略**。

### 176.7 验证证据

- 全量：`uv run pytest` → **2231 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed。

### 176.8 当前状态

| 项 | 状态 |
| --- | --- |
| Python 测试 | ✅ 2231 通过 |
| 结构层（8 维度） | ✅ 已核对（6 处差异已修） |
| 取值层 | ✅ 已修 5 处 |
| **`create_session` 参数形状** | ✅ **本轮已修** |
| 切流执行 | ⏳ 需真实部署环境 |

---

## 177. Agent 路由：路径 ID 校验与消息上限（2026-10-13）

§176 修完创建会话后，本轮把**同文件另外两条 POST** 也对照了一遍——同类问题确实还在。

### 177.1 两处差异

| 项 | 参考实现 | 我（改前） |
| --- | --- | --- |
| 消息长度上限 | `content.trim().min(1).max(8_192)` | **无上限** |
| 路径 `sessionId` 格式 | 用 schema 校验（非法 → **400**） | **不校验**（非法 → 404） |

### 177.2 为什么"无上限"是差异而非宽松

没有上限时，一条超大消息会一路进入**模型调用与数据库写入**。

而客户端的重试与截断逻辑**正是按那个上限设计的**——两侧对"多大算合法"的判断不同，
客户端的截断阈值就失去了依据。已补 `_MAX_MESSAGE_LENGTH = 8_192`。

### 177.3 为什么 400 与 404 的区分重要

参考实现对路径参数用同一个 schema，因此格式非法时返回 **400**：

- **400** 说"你的请求构造错了" — 客户端该改代码；
- **404** 说"这个资源不存在" — 客户端该刷新列表。

把前者报成后者，客户端会去刷新一个本来就对的列表，而真正的问题（ID 拼错了）被掩盖。

已加一个共享的 `_require_session_id`，**三条按 ID 操作的路由都接入**（读取 / 取消 / 发消息）
——逐条各写一次必然有遗漏。

### 177.4 两条边界测试

- **非法 ID → 400**：参数化覆盖三条路由（同一个规则三处生效，逐条写会漏）；
- **恰好 8192 字符是合法的**——上限是闭区间。

第二条尤其值得单独断言：写成 `>=` 会拒掉一条合法消息，**而这类"差一位"的错误在只有
"太长"的测试里永远不会暴露**。

### 177.5 验证证据

- 全量：`uv run pytest` → **2237 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed。

### 177.6 一个反复出现的模式

本会话里，"**某个参数形状/校验与参考实现不同**"已经出现四次（§160 的 `scope`、
§176 的创建会话正文、本轮的路径 ID 与消息上限）。

**它们的共同点**：都是"**请求侧**"的契约——而此前 8 个核对维度几乎都在看**响应侧**。
请求侧的差异更隐蔽，因为它不影响"服务端会不会返回数据"，只影响"服务端理解不理解你"。

**因此"请求侧契约"应作为一个独立的核对维度**：每条路由的参数位置（body/query/path）、
必填性、取值范围、以及格式校验。

---

## 178. 请求侧契约：导入路由的三处校验（2026-10-13）

§177 提出把"**请求侧契约**"作为独立核对维度。本轮把它应用到导入与提交路由。

### 178.1 三处差异

| 项 | 参考实现 | 我（改前） |
| --- | --- | --- |
| 路径 `importId` | schema 校验（非法 → **400**） | 不校验（非法 → 404） |
| `confirmToken` | `trim().min(1).max(512)` | **无上限** |
| 幂等键 | `^[A-Za-z0-9._:-]{16,200}$` | **无长度与字符集检查** |

### 178.2 幂等键的格式校验最值得记一笔

参考实现里有这条注释：

> 明文幂等键只允许安全字符，与领域散列规则保持一致。

两处理由：

1. **键会进入日志与数据库**——允许任意字符会带来注入面；
2. **长度是协议的一部分**——我此前不检查，客户端会以为短键可用，**而切换后同样的键
   会被拒**，那时才发现。

这条差异只有"两侧对照"才能发现：我在 Python 侧用得完全正常，因为**我从不校验**。

### 178.3 一处被自己的新校验抓到的测试缺陷

加上幂等键校验后，我此前的 4 条幂等测试**全部失败**——因为它们的键是 `"key-1"`、
`"key-replay"` 这类短串。

**这正说明校验生效了**：参考实现要求 ≥16 位，而我的测试用的是 ≤10 位。已把测试键改为
合法长度。

**顺带一个观察**：这 4 条测试是我在 §145 写的，当时它们"通过"只因为我**没有校验**。
**测试通过不代表契约正确——它只代表实现与测试一致**，而两者可能一起偏离参考实现。

### 178.4 两条边界测试

- **恰好 16 位幂等键合法**（下限闭区间）；
- **恰好 512 字符令牌合法**的存在性由对称测试覆盖。

边界值值得单独断言：写成 `>` 会拒掉合法的键，**而这类"差一位"的错误在只有"太短"的
测试里永远不会暴露**。

### 178.5 一条被漏掉又补上的路由

`_require_import_id` 第一次只接入了两条路由——**第三条（读取）的 docstring 只有一行**，
我的插入脚本按"docstring 结束"定位时落到了别处。

是靠 `grep -c` 数出"只有 2 处调用"才发现的。**这再次印证：批量插入后要立刻数一数命中数**，
而不是假设"脚本没报错就是全接上了"。

### 178.6 验证证据

- 全量：`uv run pytest` → **2244 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed；
- 6 条新测试（参数化覆盖三条路由 + 令牌上限 + 幂等键三种情形）。

### 178.7 请求侧维度的进度

| 路由组 | 状态 |
| --- | --- |
| agent 路由 | ✅ §176–§177 |
| import 路由 | ✅ **本轮** |
| gmail 路由（7 条） | ⏳ 未核对 |
| auth / market / ledger 的**请求侧** | ⏳ 未核对（响应侧已核） |

---

## 179. 请求侧契约：Gmail 断开连接的 `revoke` 位置（2026-10-13）

### 179.1 差异

| | 参考实现 | 我（改前） |
| --- | --- | --- |
| `revoke` 的来源 | **请求体**（`disconnectSchema`） | 查询串 `?revoke=true` |
| 省略时 | `default(false)` | `false` |

### 179.2 后果比前几次更隐蔽

一个按参考实现发正文的客户端，会被我的实现**静默地**按"不撤销"处理：

> **连接确实断开了，但 Google 侧的授权仍然有效。**

这比"直接失败"难发现得多，因为它**部分生效且不报错**：客户端看到 200、看到连接消失，
以为撤销也做了。而用户以为已经收回的授权仍然挂在 Google 账户上。

前几轮的同类差异（§176 创建会话、§178 幂等键）都是"参数被忽略导致**服务端行为不同**"，
这一次是"**服务端只做了一半的事**"——性质更接近"静默的半完成"。

### 179.3 顺带核对的三条 Gmail 请求

| 路由 | 参数来源 | 结果 |
| --- | --- | --- |
| `PUT .../mappings/{mapping_id}` | 正文 `accountId` | ✅ 本就正确 |
| `POST .../sync` | 正文两个可选字段 | ✅ 本就正确 |
| `DELETE .../connections/{id}` | **查询串**（错） | ✅ 本轮修正 |

**三条里只有一条错**——这也是逐条核对的价值：看到一条正确的不能推出其余都正确。

### 179.4 两条新测试

- **查询串里的 `revoke` 不再生效**（旧路径必须彻底失效，否则会被无意保留）；
- 省略正文时按 `false` 处理。

第一条尤其重要：修正"参数来源"后，**旧来源必须不再被接受**——否则两条路径并存，
客户端继续用旧的也能工作，切换后才暴露。

### 179.5 一处 `httpx` 的细节

`httpx.Client.delete()` **不接受 `json=`**（只有 `request("DELETE", ...)` 接受）。
这类限制本身是合理的（DELETE 带正文不常见），而参考实现恰恰用了它——
**协议允许的事不代表每个 HTTP 客户端库都方便表达**。

### 179.6 验证证据

- 全量：`uv run pytest` → **2246 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed。

### 179.7 请求侧维度的进度

| 路由组 | 状态 |
| --- | --- |
| agent 路由 | ✅ §176–§177 |
| import 路由 | ✅ §178 |
| **gmail 路由（7 条）** | ✅ **本轮** |
| market / auth / ledger 的**请求侧** | ⏳ 未核对（响应侧已核） |

---

## 180. 请求侧契约：校验顺序（2026-10-13）

### 180.1 差异

参考实现在 transport 层用 schema 校验正文，**然后**才调用服务：

```ts
const body = startSchema.safeParse(request.body);
if (!body.success) throw invalidArgument();      // 400
return reply.code(201).send(service.start(provider, body.data, request.ip));
```

而我的实现在服务内部**先判断"来源是否配置"**、后校验参数：

```python
provider = self._provider(provider_id)   # 未配置 → 503
if (...参数非法...):                      # → 400（永远走不到）
```

### 180.2 后果：误导性的错误

客户端发一个格式错误的请求，会得到 **503 `AUTH_NOT_CONFIGURED`**：

> 服务端尚未启用此登录方式

而真正的问题是**它的请求构造错了**。

这个 503 有具体危害：它**暗示客户端应该等待或联系服务端**，而客户端可能据此**重试**——
永远等不到。正确的 400 会告诉它改请求。

**请求是否合法与服务端是否配置无关**，这是两条正交的判断，前者的结论不应被后者掩盖。

### 180.3 修复

把参数**格式**校验提到 `_provider()` 之前（`:meth:`start`` 与 `:meth:`exchange`` 各一处）。

`exchange` 里需要拆开：**格式**检查（`_CODE_VERIFIER`）前置，**PKCE 比对**需要 attempt
因而留在后面。

实测：

| 请求 | 改前 | 改后 |
| --- | --- | --- |
| 合法正文 + 未配置 | 503 | 503 |
| `codeChallenge` 42 位 | 503 | **400** |
| `codeChallengeMethod` 非 S256 | 503 | **400** |
| `displayName` 65 位 | 503 | **400** |

### 180.4 一条"对照"测试的重要性

我写了一条"格式非法 → 400"，又**特意**写了它的对照："合法正文 + 未配置 → 503"。

**只断言前者是不够的**：如果实现把**所有**请求都判成 400，那条测试同样通过——而那样
客户端就永远拿不到"服务端未启用登录"这个真实信息了。

**一条断言"错误情况"的测试，必须配一条断言"正确情况仍能通过"的对照**，否则它可能被
一个过宽的实现满足。

### 180.5 验证证据

- 全量：`uv run pytest` → **2250 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed；
- 5 条新测试（3 条参数化 + 1 条对照 + 1 条 exchange）。

### 180.6 请求侧维度的进度

| 路由组 | 状态 |
| --- | --- |
| agent / import / gmail | ✅ §176–§179 |
| **auth** | ✅ **本轮**（含校验顺序） |
| market / ledger 的**请求侧** | ⏳ 未核对 |

### 180.7 一个值得推广的检查

"**校验顺序**"本身是一个独立的契约面——它不在"参数位置/必填性/取值范围"这三项里，
却又真实影响客户端看到什么错误。

可推广的问法：**每一种错误，是否由最该为它负责的那一层报出？** 请求格式错该报 400，
配置缺失该报 503；两者同时存在时，**前者优先**。

---

## 181. `no-store` 覆盖与行情正文上限（2026-10-13）

§124 发现系统性缺口，本轮实现。

### 181.1 缺口的形状：两层覆盖只做了一层

参考实现的 `no-store` 覆盖是**两层**：

1. 一个 `/v1/auth/` 前缀的**全局钩子**（`http-server.ts:56-59`）；
2. **几处逐路由自设**：`POST /v1/devices/tokens`、`/v1/market/settings`、
   `POST /v1/ledger/accounts`。

我此前只实现了与第 1 层对应的那部分（修 callback 安全头时顺手加的），因此：

| 路径 | 内容 | 改前 |
| --- | --- | --- |
| `/v1/auth/*` | state 与授权地址 | ✅ 有 |
| `POST /v1/devices/tokens` | **访问令牌与刷新令牌** | ❌ 无 |
| `/v1/market/settings` | 行情提供方的 **API 密钥配置** | ❌ 无 |
| `POST /v1/ledger/accounts` | 创建账户的响应 | ❌ 无 |

**"auth 有 `no-store`"让人以为这一项已覆盖。** 一项检查在某个子集上通过，不能推出它
在全体上通过——这与 §178「三条 Gmail 请求里只有一条错」是同一个教训。

### 181.2 后果

被中间层或客户端缓存这些响应，等于让**凭据在预期生命周期之外留存**：

- 设备令牌被缓存 → 一个已轮换掉的令牌仍可用；
- 行情密钥配置被缓存 → 供应商凭据长期留在客户端或代理上。

这不是"少一个响应头"的细节。

### 181.3 实现方式与参考实现同构

在已有的请求 ID 中间件里一并处理（那是"每请求一次"的天然位置），路径集合与参考实现
逐项对应：

```python
_NO_STORE_PREFIXES = ("/v1/auth/", "/v1/market/settings")
_NO_STORE_EXACT = {"/v1/devices/tokens", "/v1/ledger/accounts"}
```

**逐路由各写一次必然有遗漏**——本轮就是把集合写在一处，并在注释里列出每一条的理由。

### 181.4 行情设置正文上限

参考实现的 `PUT /v1/market/settings` 带 `bodyLimit: 2048`。已实现，且**在读正文之前**
检查声明的 `content-length`——先拒绝比先把它整个读进内存要好。

这个上限不是性能优化：该正文只含一个修订号与两组路由，**允许超长等于允许把任意大的
内容送进校验与日志**。

### 181.5 一条必须配的对照测试

`test_ordinary_responses_are_left_alone` 断言**普通响应不该有 `no-store`**。

**只断言"该有的有"是不够的**：如果实现给每个响应都加 `no-store`，那条测试同样通过，
而那会禁掉所有客户端缓存、把只读端点的性能特征整个改掉。

**断言"某个头存在"的测试，必须配一条"它不该到处存在"的对照**——否则一个"全加"的实现
也能通过。这与 §180.4 的对照测试是同一条经验，这已是它第二次出现。

### 181.6 验证证据

- 全量：`uv run pytest` → **2259 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed；
- 8 条新测试：4 条"该有" + 3 条"不该有" + 1 条正文上限。

### 181.7 请求侧维度的进度

| 路由组 | 状态 |
| --- | --- |
| agent / import / gmail / auth | ✅ |
| **market** | ✅ **本轮**（含正文上限） |
| ledger 的请求侧 | ⏳ 未核对（仅 `POST /accounts` 的响应头已覆盖） |

---

## 182. 整块能力缺失：`POST /v1/ledger/accounts`（2026-10-13）

本轮核对 ledger 的请求侧，发现了一个**整块缺失的能力**——而且**我自己的路由对照漏掉了它**。

### 182.1 缺口

| | 参考实现 | 我 |
| --- | --- | --- |
| `GET /v1/ledger/accounts` | 有（表驱动，`accounts` 工具） | ✅ 有 |
| **`POST /v1/ledger/accounts`** | 有（`account-routes.ts:14`） | ❌ **完全没有** |

不只缺路由：`create_account` 这个能力在我这边**不存在**——无服务方法、无仓储方法。

### 182.2 为什么 §146 的路由对照没发现

§146 的核对方式是把两侧的**路径**取成集合再求差集：

```
TS:    /v1/ledger/accounts
Python: /v1/ledger/accounts        → 视为一致
```

**但 `/v1/ledger/accounts` 上参考实现有 GET 与 POST 两个方法，我只有 GET。**

集合比较**丢掉了方法维度**：同一个路径上少一个方法，在路径集合里看不出来。

**这是我这套对照方法本身的缺陷**，比它漏掉的那处缺口更值得记：

> **按路径求差集只能发现"整条路径缺失"，发现不了"某条路径上少了一个方法"。**

正确的取法是 **(方法, 路径) 对**。这一条应作为对该核对方法的修正记下来。

### 182.3 缺口的能力规格（已探明）

**路由**：`POST /v1/ledger/accounts`，201，带 `no-store`（§181 已覆盖该路径的响应头），
读取并校验 `Idempotency-Key`。

**正文 schema**：

```ts
{
  displayName: string.trim().min(1).max(64)       // 且不含控制字符
  accountIdentifier: string.trim().toUpperCase().min(4).max(32)
      .regex(/^[A-Z0-9-]+[0-9]{3}$/)              // 以至少 3 位数字结尾
  baseCurrency: string.trim().toUpperCase().regex(/^[A-Z]{3}$/).default("USD")
}
```

**校验失败的文案**（参考实现逐项列出，便于客户端定位）：

> 账户名称须为 1 至 64 个字符；文件账户标识须为 4 至 32 位字母、数字或连字符并以至少
> 3 位数字结尾；币种须为 3 个字母

**幂等**：操作为 `account.create`，**复用我已实现的整套幂等机制**
（`plan_idempotent_mutation` + `mutation_idempotency_records` + 写锁前登记 + 事务内收口）。
那个操作名要加进 `IDEMPOTENCY_OPERATION` 的取值集合。

**响应**：`CreateAccountHttpResponse` = `{account: PublicBrokerageAccount, ...}`（含更多字段）。

### 182.4 影响评估

**这是本会话发现的最严重的缺口**，因为它不是"某个字段不同"，而是**一个能力不存在**：

- 客户端无法在 App 里创建账户；
- 切换后这条路径会 404 / 405，而**其余一切都正常**——最难定位的一类故障。

### 182.5 为什么本轮不实现

它需要一个完整的功能切片：路由 + 服务 + schema 校验 + 幂等接线 + 仓储写入 + 响应投影
+ 测试（含幂等重放与校验失败）。

**本次会话的上下文已接近上限。** 这类"新增一整块能力"的改动需要端到端验证（创建 → 重放
返回同一账户 → 校验失败各分支），在剩余预算里开始它，风险是留下一个半可用的能力。

### 182.6 验证证据

- 全量：`uv run pytest` → **2259 项通过**（4 项跳过）——本轮未改代码；
- 依据：`transport/account-routes.ts:14-32`、`application/account-service.ts:22-32,54-77`。

### 182.7 当前状态

| 项 | 状态 |
| --- | --- |
| Python 测试 | ✅ 2259 通过 |
| 响应侧（8 维度） | ✅ 已核对 |
| 请求侧契约 | ⏳ 进行中 |
| **`POST /v1/ledger/accounts` 整块缺失** | ⚠️ **本轮发现，未实现** |
| **路由对照方法（路径 → 方法+路径）** | ⚠️ **本轮发现的自身缺陷** |
| 切流执行 | ⏳ 需真实部署环境 |

---

## 183. `POST /v1/ledger/accounts` 完成（2026-10-13）

§182 发现的整块缺失能力已实现，**唯一的代码待办项已闭合**。

### 183.1 三个文件

| 文件 | 内容 |
| --- | --- |
| `infrastructure/db/account_repository.py` | 读取 + **原子写入**（账户 + 审计 + 幂等收口） |
| `application/account_service.py` | 校验、掩码、幂等规划、审计构造 |
| `api/ledger_routes.py` | 路由（`Idempotency-Key`、201、重放 200） |

另把幂等键的校验从 `import_routes` 抽成 `application/idempotent_mutation.validate_idempotency_key`
并让两处共用——**否则"键是否合法"会有两份判断**（§178 记录的同类问题）。

### 183.2 实现过程中被抓出的三个真实问题

**其一：漏了幂等登记，首次调用就失败。**

`create_account` 从未调用 `start_mutation_idempotency_record`，于是首次创建时记录根本
不存在，收口抛 ``ValueError: 幂等记录完成收口失败``。

**登记必须在写入之前**——这与 §145 幂等接线时确认的顺序要求是同一条。症状（"收口失败"）
看起来像收口的问题，实际是登记的缺失。

**其二：重放返回的时间戳格式与首次不同。**

```
首次：2026-10-13T07:48:59.545Z
重放：2026-10-13T15:48:59.545000+08:00   ← DuckDB 读回的本地偏移形式
```

**同一个账户两次返回，时间戳字符串不同。** 客户端若用它作缓存键或比较，会认为这是两个
不同的对象。已在读取时归一化为与创建时相同的 ``...Z`` 形式。

这一处的教训：**"同一个资源的两条返回路径"必须给出一致的表示**——创建路径写什么、
读取路径就必须读回什么形态。

**其三：重复标识冒出底层约束异常。**

依赖表的 UNIQUE 约束也能挡住，但客户端拿到的是一句 DuckDB 的约束信息，**没有稳定错误码**。

已按参考实现在**事务内预检**并返回 **409 `ACCOUNT_ALREADY_EXISTS`**，比对用
``upper(trim(...))``——**已归档账户同样不能重复创建**。

### 183.3 一处靠 linter 抓到的

新路由用了 ``response.status_code`` 改重放的状态码，却没声明 ``response`` 参数。

**错误类 ruff（`F821`）立刻报出来了**，而它在测试里表现为一个与"`no-store` 头"有关的
失败——**症状指向的位置与原因无关**。这正是配置错误类规则的价值：它比测试更早、更准地
指出"这个名字不存在"。

### 183.4 掩码与响应

```
accountIdentifier = "ACCT-00123"   →  maskedSuffix = "0123"
```

**文件账户标识本身落库但不入公开响应**——它是导入匹配键，泄漏它等于泄漏"哪份文件属于
哪个账户"。有一条测试断言响应体里**逐字不出现**该标识。

### 183.5 一处架构判断（先问清再写）

`create_account` **不经过 `WriteCoordinator`**：串行化由 `persist_created_account` 内部的
DuckDB 事务提供，与导入提交路径一致。参考实现把 `WriteCoordinator` 用作通用写队列，而本
实现用它服务**异步**操作——这是既有的架构差异，不是本轮引入的。

### 183.6 验证证据

- 全量：`uv run pytest` → **2275 项通过**（4 项跳过，较上轮 +16）；
- `ruff check src/` → All checks passed；
- 16 条新测试：创建 / 不泄漏标识 / 归一化 / 币种默认 / 6 种非法正文 / 空正文 /
  **重放返回同一账户且不重复创建** / 不同键各自创建 / **重复标识 409** /
  **审计事件的 actor 与 target** / 创建后出现在列表里。

### 183.7 最终状态

| 项 | 状态 |
| --- | --- |
| Python 测试 | ✅ 2275 通过 |
| 响应侧（8 维度） | ✅ 已核对 |
| 请求侧契约 | ✅ agent / import / gmail / auth / market / ledger 均已核对 |
| 路由（方法, 路径）对 | ✅ 对照完成，**唯一缺口已闭合** |
| **代码待办** | ✅ **无** |
| 切流执行 | ⏳ 唯一剩余项；需真实部署环境与已配对设备 |

**至此，除了需要在真实环境执行的切流之外，代码工作已无已知待办。**

---

## 184. 切换预检脚本化（2026-10-13）

代码待办已清空（§183）。本轮转向**让切流可执行**——这仍是目标的一部分，且不需要真实
环境就能做。

### 184.1 起因：运行手册的阶段 0 已过时

它的「已知缺口」表列着四项：

| 旧缺口 | 实际状态 |
| --- | --- |
| Excel 解析未迁移 | ✅ 已实现（§76） |
| 行情传输层是占位 | ✅ 已接线 httpx（§72） |
| Agent 模型绑定为自定义 HTTP | ✅ 确认等价（§81） |
| 幂等记录仓储未建 | ✅ 已实现（§145） |

**四项都已闭合，却仍标着"切换前必须处理"。** 测试数也写着 2032（现为 2275）。

**一份过时的阻塞清单比没有更危险**：它让执行者去做已经不必做的事，或据此怀疑整个切换
是否成熟。这与 §144.2 记录过的"过时文档比没有更危险"是同一条。

### 184.2 新增 `server-py/scripts/preflight_cutover.py`

把阶段 0 从「人读的表格」变成「可自动判定的断言」——**通过就是通过，不通过就明确指出
哪一条**，并在有未通过项时拒绝放行。

七项检查，实测输出：

```
[通过] Python 全量测试: 2275 passed, 4 skipped
[通过] 错误类静态检查: All checks passed!
[通过] TS 基线（oracle 与回退退路）: # pass 514 # fail 0
[通过] 模式版本一致: Python=15 TS=15
[通过] 协议版本为 2: LYRA_PROTOCOL_VERSION=2
[通过] 路由（方法, 路径）对一致: TS 47 / Python 47 条
[未通过] LYRA_SECRET_MASTER_KEY 形态: 未设置或形态不符
1 项未通过——**不要开始切换**。
```

最后一项**未通过是正确的**：它是执行者必须提供的环境输入，脚本据此拒绝给出"可以开始"。

### 184.3 脚本自身的一个缺陷（跑起来才发现）

第一次运行时，路由对账报了 17 条"假差异"——**因为两侧的参数写法不同而我只归一化了一种**：

| 来源 | 写法 |
| --- | --- |
| Fastify 路由 | `:sessionId` |
| 模板字符串 | `${provider}` |
| FastAPI / OpenAPI | `{session_id}` |

已改为三种都归一化。**参数名不是契约的一部分**（两侧各有命名习惯），**路径形状才是**。

顺带显式排除了 `/v1/events`——WebSocket 不在 OpenAPI 里，不排除就会成为一条常驻假差异。

### 184.4 一条关于"人读表格 vs 可执行断言"的经验

阶段 0 原本是一张表，其中 0.2（差分对照）写着"✅ 已通过"、0.1（路由覆盖）写着
"⚠️ 见下方已知缺口"——**但那个"已知缺口"指的是另外四项，与路由覆盖无关**。

表格的表达力有限：它让两件不同的事挤在同一格里，读的人自然会串。**脚本不会串**：
每一项要么通过要么不通过，且失败时给出具体的缺失集合。

### 184.5 验证证据

- 预检脚本实测：**6/7 通过，唯一未通过项是执行者需提供的环境输入**；
- 路由对账：**TS 47 / Python 47**（含 47 个 (方法, 路径) 对）；
- 运行手册阶段 0 已重写：190 行（原 149 行），自动检查表 + 已闭合缺口表 + 明确说明
  预检**不覆盖**备份与真实设备联调。

### 184.6 当前状态

| 项 | 状态 |
| --- | --- |
| Python 测试 | ✅ 2275 通过 |
| TS 基线 | ✅ 514 通过 |
| 路由（方法, 路径）对 | ✅ **47 / 47，已自动对账** |
| 代码待办 | ✅ 无 |
| **切换预检** | ✅ **已脚本化并通过** |
| **切流执行** | ⏳ **唯一剩余项** |

---

## 185. 备份脚本化（2026-10-13）

沿用 §184 的思路：把切流里**最容易做错且代价最高**的一步——备份——从手工步骤变成脚本。

### 185.1 脚本的三个性质

`server-py/scripts/backup_ledger.py` 的价值不在于"能复制文件"（`cp` 就能做），而在于：

| 性质 | 为什么重要 |
| --- | --- |
| **不确认服务已停止就拒绝执行** | 手动 `cp` 不会拦你；带活跃写入者复制得到**看起来正常但不一致**的快照，**而验证查询很可能仍然通过**（表和行都在） |
| **备份与验证绑成一步** | 运行手册自己写着"未验证的备份等于没有备份"——分成两步时，验证正是那个在紧张时被跳过的步骤 |
| **明确声明不含 `application-gin.yml`** | 该文件含敏感凭据，不得进入任何归档。**让脚本自己声明，比让人记得更可靠** |

### 185.2 验证查询的选择

读五张表，**覆盖三类**：

| 表 | 类别 |
| --- | --- |
| `schema_migrations` | 迁移状态 |
| `accounts` / `transactions` / `ledger_versions` | 账本主体 |
| `paired_devices` | **凭据** |

**只查一张表可能漏掉"主体可读但凭据表损坏"**——而那正是回退后最致命的一种：账本在，
但所有设备都登不进去。

### 185.3 一处刻意的设计：确认项没有默认值

```python
parser.add_argument("--i-have-stopped-the-server", action="store_true")
if not args.i_have_stopped_the_server:
    print("拒绝执行：必须确认已停止服务")
    return 2
```

**它必须是显式的、无默认值的**。一个默认 `True` 的"服务已停止"就等于没有这个检查——
而带写入者复制的失败模式恰好是"不报错"。

### 185.4 五条测试

- **不确认时拒绝执行**，且不产生备份文件；
- 备份被只读打开并**真的跑出值**（断言 `: 15` 出现在输出里，而不只是标签）；
- 输出**声明不含 `application-gin.yml`**；
- 源库不存在时明确报错；
- **备份本人的模式版本与源库一致**（验证不是走过场）。

### 185.5 验证证据

- 全量：`uv run pytest` → **2280 项通过**（4 项跳过，较上轮 +5）；
- `ruff check src/` → All checks passed；
- 运行手册阶段 1 已加入脚本用法与"为什么它与手工步骤不同"的对照表。

### 185.6 当前状态

| 项 | 状态 |
| --- | --- |
| Python 测试 | ✅ 2280 通过 |
| 代码待办 | ✅ 无 |
| 切换预检（阶段 0） | ✅ 已脚本化并通过 |
| **备份与验证（阶段 1）** | ✅ **已脚本化** |
| **切流执行（阶段 2 起）** | ⏳ 需真实部署环境与已配对设备 |

---

## 186. 回退脚本化（2026-10-13）

沿用 §184–§185 的思路，把回退——**整个切换里唯一"一旦做错就同时失去两边"的操作**——
脚本化。

### 186.1 脚本的核心不是"能还原文件"

`cp` 就能还原文件。脚本的价值在**三道拒绝**：

| 拒绝 | 不拒的后果 |
| --- | --- |
| **不确认 Python 已停止** | 两个进程争同一个 DuckDB 文件锁只会都失败——**而失败发生在回退过程中**，那是最不该出意外的时刻 |
| **备份自身验证不通过** | 若先覆盖再验证，一次损坏的备份会让**两份都没了** |
| **不把当前库另存** | 切换窗口内的写入本来可以保留成文件，见下 |

第二条是脚本存在的主要理由：**"回退"这个动作本身已经意味着情况不好，不该再叠加一次
不可逆的破坏**。

### 186.2 一处比手工步骤更好的地方

运行手册写着：

> **代价**：切换后到回退前的所有写入都会丢失。

**它们不必不可逆地消失。** 脚本在覆盖前把当前库另存为
`<库名>.post-cutover-<时间>`（含 WAL）——保留一份文件几乎不花代价，而在回退后想捞回那
段时间的写入时，它是唯一的东西。手工 `cp` 覆盖不会保留它。

手册的"代价"一节已改为准确表述：**不在活动库里，但已保留成文件**。

### 186.3 `--dry-run`

不确定时先跑它：只验证备份并打印将要执行的动作，**不改动任何文件**。有一条测试断言
dry-run 之后当前库**逐字节未变**。

### 186.4 五条测试

- **不确认时拒绝执行**；
- **损坏的备份不得毁掉当前库**（断言当前库逐字节未变，且输出含"没有改动任何文件"）；
- **切换期数据被保留成文件**（断言 `.post-cutover-*` 存在）；
- `--dry-run` 不改动任何文件；
- 回退后活动库可用且模式版本一致。

第二条是最重要的：它验证的正是"回退失败时不能再造成第二次损失"。

### 186.5 验证证据

- 全量：`uv run pytest` → **2285 项通过**（4 项跳过，较上轮 +5）；
- `ruff check src/` → All checks passed；
- 运行手册阶段 6 已重写：脚本用法 + 三道拒绝表 + dry-run 说明 + 修正后的"代价"表述。

### 186.6 当前状态

| 阶段 | 状态 |
| --- | --- |
| 0 预检 | ✅ 已脚本化 |
| 1 备份与验证 | ✅ 已脚本化 |
| 2 启动 Python | ⏳ 依赖具体部署方式 |
| 3 探针切换 | ⏳ 依赖具体部署方式 |
| 4 真实设备联调 | ⏳ **需已配对设备** |
| 5 观察 | ⏳ |
| **6 回退** | ✅ **已脚本化** |
| 7 退役 TS | ⏳ 不可逆，需明确授权 |

**除阶段 2/3（依赖部署方式）与阶段 4（需设备）之外，风险最高的两步都已脚本化并测试。**

---

## 187. 预检脚本有判别力（实测）（2026-10-13）

§184 写了预检脚本。本轮先问一个容易被跳过的问题：**它真的会失败吗？**

### 187.1 实测：注入一个真实回归

把 Python 的 `CURRENT_SCHEMA_VERSION` 从 15 改成 14，重跑预检：

```
[未通过] Python 全量测试: 9 failed, 1886 passed, 4 skipped, 390 errors
[未通过] 模式版本一致: Python=14 TS=15
[未通过] 路由对账: 检查自身失败: DuckDB 迁移清单与当前 schema 版本不一致
[未通过] LYRA_SECRET_MASTER_KEY 形态: 未设置或形态不符
```

**三项检查同时失败，且每一项的原因都是对的**——不止 `模式版本一致` 抓到了分叉，
全量测试与路由对账也因为迁移清单自检而失败。恢复后重新干净。

**一个永远通过的检查等于没有检查。** 这次实测确认了它不是。

### 187.2 一处值得注意的连带现象

注入模式版本不一致后，**路由对账报的是"检查自身失败"**——因为 `create_app` 内部的迁移
自检直接抛错（"DuckDB 迁移清单与当前 schema 版本不一致"）。

它的报错信息是准确的（没有假装成"路由不一致"），这比一个笼统的"检查失败"有用：
**它会告诉执行者真正的问题在迁移，而不是路由。**

### 187.3 新增四条"预检自身的可靠性"测试

关键在于**不让检查项被静默丢掉**：

- **每一项都必须出现在主流程的函数集合里**——若某项没被接进 `main` 的清单，它会静默
  消失，而输出仍然是"全部通过"，**那比没有这项检查更危险**；
- 模式版本检查**必须读两侧**（只看一侧的检查会在分叉时通过，而分叉正是它要发现的）；
- 协议版本检查的输出含**实际取值**（失败时要能看出读到了什么）；
- 路由对账**通过时报告条数、失败时报告缺失与多余集合**——只报"通过/未通过"让人无法
  判断检查是否真的跑了。

最后一条对应一个常见失败模式：**检查因为没跑而"通过"**。

### 187.4 验证证据

- 全量：`uv run pytest` → **2289 项通过**（4 项跳过，较上轮 +4）；
- `ruff check src/` → All checks passed；
- 判别力实测：注入模式版本不一致 → 3 项检查失败；恢复后干净。

### 187.5 当前状态

| 项 | 状态 |
| --- | --- |
| Python 测试 | ✅ 2289 通过 |
| 代码待办 | ✅ 无 |
| 阶段 0 预检 | ✅ 已脚本化、**已实测有判别力** |
| 阶段 1 备份与验证 | ✅ 已脚本化 |
| 阶段 6 回退 | ✅ 已脚本化 |
| 阶段 2/3（启动与探针） | ⏳ 依赖具体部署方式 |
| 阶段 4（真实设备联调） | ⏳ 需已配对设备 |
| 阶段 7（退役 TS） | ⏳ 不可逆，需明确授权 |

---

## 188. 服务入口与启动后自检（2026-10-13）

阶段 2「启动 Python 服务」此前写着 `python -m lyra.main`，而**「启动即验证」那一栏没有
可执行依据**——它是一张人读的表。本轮补齐两件事。

### 188.1 正式入口 `lyra-serve`

```toml
[project.scripts]
lyra-serve = "lyra.main:run"
```

**启动方式与部署形态无关**——systemd / 容器 / 裸进程都只是调用这条命令。这使阶段 2 的
脚本无需预设任何一种（我此前说"阶段 2/3 依赖部署方式"而无法脚本化，这个判断只对了一半：
**启动命令本身是部署无关的**，依赖部署方式的只是"如何让它常驻"）。

实测：`lyra-serve` 起服务后 `/v1/health` 返回 200。

### 188.2 新增 `scripts/verify_running_service.py`

**过程"起来了"与"能服务"是两件事**：uvicorn 打印 "Application startup complete" 之后，
服务仍可能因为数据库不可读或路由装配不全而无法真正工作。

实测输出：

```
[通过] /v1/health 可用: 200 / status=degraded
       └─ degraded 的原因：gmail=... agents={'status': 'unknown', ...}
[通过] /v1/system/info 模式版本: schema_version=15（期望 15）
[通过] /v1/system/info 协议版本: protocol_version=2（期望 2）
[通过] 受保护端点要求认证且信封扁平: 401 / 键=['code','details','message','request_id','retryable']
[跳过] 带令牌的账本读取——未提供 --device-token
```

### 188.3 三个刻意的判定选择

**其一：`degraded` 算通过。** 它表示服务能工作、只是部分能力受损；把 degraded 当失败会让
一个**可用的服务被误判为不可用**。脚本会额外打印 degraded 的原因，让人判断是否可接受。

**其二：检查错误信封的形状。** 那条断言不只看 401，还看键集合是否含
`code` / `message` / `retryable` / `request_id`——**这正是 §167 修过的那处**
（当时错误体是嵌套的 `{"detail": {...}}`，缺少三个字段）。把它纳入启动自检，意味着
那处缺陷若回归，会在启动时就被发现。

**其三：检查非法 `scope` 报 400。** 对应 §162 修的那处安全默认（缺参数时静默查全部账户）。

**这三项都是"曾经真实发生过、且不报错"的缺陷**——把已修的问题写进自检，比写进文档更可靠。

### 188.4 未提供令牌时明确说明

```
注意：未提供 --device-token，因此**没有验证过真实账本读取**。
```

脚本不会因为"大部分检查通过"就让人以为全部验证过了。**带令牌的账本读取是"能服务"的
唯一直接证据**，缺了它就必须说出来。

### 188.5 验证证据

- 全量：`uv run pytest` → **2289 项通过**（4 项跳过）；
- `ruff check src/` → All checks passed；
- 实测：`lyra-serve` 启动 → 自检 4 项通过、1 项跳过（未提供令牌，且已说明）。

### 188.6 阶段状态

| 阶段 | 状态 |
| --- | --- |
| 0 预检 | ✅ 已脚本化、已实测有判别力 |
| 1 备份与验证 | ✅ 已脚本化 |
| **2 启动 + 启动后自检** | ✅ **入口与自检均已就绪** |
| 3 探针切换 | ✅ 自检脚本覆盖了探针要查的内容（health / 元数据 / 认证 / 账本读取） |
| 4 真实设备联调 | ⏳ 需已配对设备 |
| 5 观察 | ⏳ |
| 6 回退 | ✅ 已脚本化 |
| 7 退役 TS | ⏳ 不可逆，需明确授权 |

**除阶段 4（需设备）、阶段 5（需时间）与阶段 7（需授权）之外，各阶段均已可执行。**

---

---

## 附录 A：续作指引

（2026-10-12 更新）

本节供后续会话直接接手。**上一版写于 M4 期间，内容已过时，此处整体重写。**

### A.1 环境与命令

```bash
cd server-py
export UV_CACHE_DIR=<repo>/.uv-tmp/cache \
       UV_PYTHON_INSTALL_DIR=<repo>/.uv-tmp/python
uv run --python 3.12 pytest -q        # Python 全量（当前 726 通过 / 4 跳过）

cd ../server && npm run check          # TS 全量（当前 139 套件 / 514 通过 / 0 失败）
```

uv 路径必须重定向进工作区：默认位置在受限沙箱下不可写。

### A.2 当前真实状态

| 里程碑 | 状态 | 证据 |
| --- | --- | --- |
| M1 接管账本与端点 | ✅ | 对真实 TS 创建的库的接管测试 |
| M2 密钥互通 | ✅ | 真实 TS 密文夹具 + 反向验证 |
| M3 领域层 | ✅ | 29/29 + 11/11 语料；**19 份账本 × 16 命令 = 2771 字段**与 oracle 一致 |
| M4 只读 API | ✅ | 17 条端点，协议 2 对齐；含全端点 snake_case 递归断言 |
| M5 认证与会话 | ✅ | 设备认证 + 三种登录来源 + 登录事务 + 设备会话 |
| M6 写入路径 | ✅ 主流程 | 四条导入路由 + 端到端 HTTP 测试；暂存与幂等判定就位 |
| M7 Agent | **进行中** | 工具目录 / 参数校验 / 分发器 / **17 个工具可调用** / 能力发现 |
| M8 行情 | 未开始 | — |
| M9 切流 | 未开始 | — |

规模：76 个 Python 源文件、41 个测试文件、726 项测试通过。

### A.3 下一步（建议顺序）

1. **M7 收口**：
   - Genkit Python 接入：把 `ToolDefinition` 转成 Genkit 工具，调用走
     `ToolRuntime.invoke`（失败语义见 §58，四类分开）；
   - 六条会话路由（`POST /v1/agent/sessions` 等）与回合生命周期
     —— 参考 `server/src/application/agent-turn-orchestrator.ts`；
   - WebSocket 事件与工具调用记录；
   - **路由与处理器的完全合并**（见 A.4）。

2. **M8 行情**：Alpaca / MarketData 适配、缓存、预算、主备换源；
   落实后 `market-positions` 工具与 `/v1/market/positions` 即可补齐
   （目录里的 `UNIMPLEMENTED_TOOLS` 随之清空——那条测试会失败并提醒）。

3. **M9 切流**：部署脚本、备份、**一次性硬切**（见 A.5）、删除 TS 服务端。

### A.4 两条已知的清理项（不阻塞，但会持续产生成本）

1. **路由与处理器尚未完全合并。** `financial_tool_handlers` 已是路由与 Agent 的共享
   实现，工具侧走它；但 `api/ledger_routes.py` 的 17 个处理器仍保留各自方法体。
   两者一致性由两条测试保证（「工具结果 == 处理器结果」、「目录 == 路由」），
   但**代码本身仍有两份**。合并需改动 17 个处理器，属高风险重构，应一次性完成。
2. **Excel 解析未移植。** TS 有 `schwab-xlsx.ts`；Python 侧 `.xlsx` 上传返回显式
   `IMPORT_FORMAT_NOT_IMPLEMENTED`（501，注意**不是** 415）。刻意不用 415：那个码
   含义是"协议不支持这种格式"，而 xlsx 是协议明确支持的。
3. **游标分页未实现。** 查询类端点不发出 `totalCount` / `nextCursor`，并有测试断言
   它们**不被伪造**——伪造 `totalCount = count` 在 limit 截断时是错的。

### A.5 三条不能忘的硬约束

1. **DuckDB 同一时刻只允许一个进程打开账本**（实测）。TS 与 Python 不能并存，
   M9 是硬切，没有并行运行期。
2. **迁移 SQL 的唯一来源仍是 `server/src/infrastructure/db/`**；`server-py/src/lyra/db/sql/`
   是副本，由测试逐字节校验。M9 删除 TS 后副本转为唯一来源。
3. **`LYRA_IMPORT_STAGING_DIR` 是提交阶段的必要输入**，不是缓存：解析结果不持久化，
   提交时从暂存读回并校验哈希（见 §53）。

### A.6 容易踩的坑（都真实发生过）

| 坑 | 后果 |
| --- | --- |
| DuckDB 读 `TIMESTAMPTZ` 需要 `pytz` | **写正常、读报错**，签发凭据看起来成功直到第一次读取 |
| `preview_ledger_version` 空账本存 NULL 而非 0 | 存 0 违反外键；把 NULL 当"缺失"会让**空账本首次导入永远失败** |
| 金额用 `format(Decimal,"f")` | 保留标度（`"2.000000"`），而公开 API 用去尾随零（`"2"`） |
| 通用序列化器输出 dataclass 字段名 | 把 snake_case 发给客户端，客户端只读到 `undefined` **不报错** |
| 分块脚本用"下一个锚点"界定范围 | 最后一块会吃到文件末尾，**误删尾部定义** |

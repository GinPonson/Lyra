# Lyra App 功能设计 — 基于 Schwab CLI 命令

> 文档状态：功能设计稿，作为 [lyra-product-technical-design.md](lyra-product-technical-design.md) 第 15 章（信息架构）的命令级落地细化
> 日期：2026-09-06
> 命令来源：`/Users/Gin/Workspace/Schwab`（Python CLI，全部命令支持 `--json`）
> 服务端现状：全部查询/分析命令已封装为只读金融工具，并通过 `server/src/transport/ledger-routes.ts` 暴露为 `/v1/ledger/*`、`/v1/analytics/*` 路由
> App 现状：两个一级入口（总览 / 交易记录）+ Agent 二级页；总览已有已实现损益、持仓成本指标与快捷块；交易记录 tab 为通用 `SectionScreen`（全部交易 / 按股票分组 / 按账户分组）

## 1. 设计输入

### 1.1 Schwab CLI 命令清单

| 分组 | 命令 | 关键参数 |
| --- | --- | --- |
| 数据管道 | `import` | 一个或多个官方 CSV |
| 数据管道 | `import-email` | Gmail API JSON / `.eml`，`--account` |
| 数据管道 | `reconcile` | eConfirm 输入文件，只读对账 |
| 数据管道 | `rebuild` | 无（重放 FIFO） |
| 查询 | `summary` | 无 |
| 查询 | `positions` | `-u/--underlying` |
| 查询 | `expiring` | `--days N`、`--as-of` |
| 查询 | `trades` | `--symbol`、`--action`、`--from`、`--to`、`--limit` |
| 查询 | `realized` | `--symbol`、`--from`、`--to` |
| 查询 | `cashflow` | `--from`、`--to` |
| 查询 | `audit` | 无 |
| 高级分析 | `monthly` | `--from`、`--to` |
| 高级分析 | `allocation` | `--group-by underlying\|asset-type` |
| 高级分析 | `attribution` | `--group-by underlying\|asset-type\|direction\|close-action`、`--from`、`--to` |
| 高级分析 | `drawdown` | `--from`、`--to` |
| 高级分析 | `risk` | `--as-of` |
| 高级分析 | `holding-period` | 无 |
| 高级分析 | `extremes` | `--limit N`（默认 10） |
| 高级分析 | `streaks` | 无 |
| 高级分析 | `stress` | 无 |

服务端在 CLI 之上补充了 `accounts` 工具（`GET /v1/ledger/accounts`），供 App 构建账户范围切换器。

### 1.2 设计约束（继承自产品方案）

- 每个功能必须显示口径四元组：账户范围、数据截止时间（`as_of`）、账本版本（`ledger_version`）、计算口径（`basis`）。
- 无行情源：不出现"当前总市值""实时涨跌"等指标；持仓展示数量、历史成本、方向。
- 精确数值以十进制字符串传输与解析，禁止 `double` 参与金融计算。
- 图标走 `LyraIcons` 语义映射，界面无表情符号。
- 命令参数 → App 筛选器一一对应，不引入 CLI 没有的隐式过滤。

## 2. 命令 → App 功能总表

| # | Schwab 命令 | 服务端路由 | App 功能 | 位置 | App 现状 |
| --- | --- | --- | --- | --- | --- |
| F1 | `accounts`（服务端扩展） | `GET /v1/ledger/accounts` | 账户范围切换器 | 总览顶部 | 未接 |
| F2 | `summary` | `GET /v1/ledger/summary` | 核心指标卡 | 总览 | 部分接（已实现损益、持仓成本） |
| F3 | `audit` | `GET /v1/ledger/audit` | 数据可信度状态条 + 审计详情 | 总览 / 交易记录 | 未接 |
| F4 | `expiring` | `GET /v1/ledger/expiring` | 期权到期提醒卡与列表 | 总览提醒 + 持仓内 | 未接 |
| F5 | `reconcile`（官方 CSV 对账） | Gmail 路由 + 导入提醒 | 每月官方文件对账提醒 | 总览待办 | 服务端已有 `official-import-reminder` |
| F6 | `trades` | `GET /v1/ledger/trades` | 交易流水时间线 + 筛选 | 交易记录 | 部分接（无筛选） |
| F7 | `realized` | `GET /v1/ledger/realized` | 已实现损益配对明细 | 交易记录 | 未接（仅总览合计） |
| F8 | `cashflow` | `GET /v1/ledger/cashflow` | 现金流时间线（转账/股息/利息/税项） | 交易记录 | 未接 |
| F9 | `positions` | `GET /v1/ledger/positions` | 持仓列表（股票 + 期权） | 交易记录 tab 内持仓 section | 未接 |
| F10 | `monthly` | `GET /v1/analytics/monthly` | 月度绩效图 | 分析中心 | 未接 |
| F11 | `allocation` | `GET /v1/analytics/allocation` | 仓位配置环图 | 分析中心 | 未接 |
| F12 | `attribution` | `GET /v1/analytics/attribution` | 利润归因条形图 | 分析中心 | 未接 |
| F13 | `drawdown` | `GET /v1/analytics/drawdown` | 已实现回撤曲线 | 分析中心 | 未接 |
| F14 | `risk` | `GET /v1/analytics/risk` | 期权到期与集中度风险 | 分析中心 | 未接 |
| F15 | `holding-period` | `GET /v1/analytics/holding-period` | 持有周期绩效分布 | 分析中心 | 未接 |
| F16 | `extremes` | `GET /v1/analytics/extremes` | 最佳/最差平仓交易 Top N | 分析中心 | 未接 |
| F17 | `streaks` | `GET /v1/analytics/streaks` | 连胜/连亏 | 分析中心 | 未接 |
| F18 | `stress` | `GET /v1/analytics/stress` | 长期权权利金归零压力测试 | 分析中心 | 未接 |
| F19 | `import` / `rebuild` | 导入路由（提交时自动重建） | 文件导入向导 | 账户中心 / 总览入口 | 已有 `import_sheet` |
| F20 | `import-email`（Gmail 增量） | Gmail 路由 | Gmail 连接与同步状态 | 账户中心 | 已有连接流程 |
| F21 | 全部只读工具 | Agent Tool Registry | Agent 快捷提问与工具卡片 | Agent 页 | 已有会话与事件流 |

## 3. 信息架构

保持现有"两个一级入口 + 二级页"的克制结构，按命令覆盖度分三步演进：

```text
阶段 1（命令全覆盖）
├── 总览（tab 1）
│   ├── 账户切换器（F1）
│   ├── 核心指标卡（F2）
│   ├── 数据可信度条（F3）
│   ├── 待办卡：期权到期（F4）＋ 官方对账（F5）
│   └── 快捷块：交易记录 / 分析中心 / 问 Agent
├── 交易记录（tab 2）
│   ├── 全部交易（F6）
│   ├── 已实现损益（F7）
│   ├── 现金流（F8）
│   ├── 持仓（F9）
│   └── 审计（F3 详情）
└── 分析中心（二级页，从总览推入）
    └── 月度 / 配置 / 归因 / 回撤 / 风险 / 持有期 / 极值 / 连胜连亏 / 压力测试（F10–F18）

阶段 2：若分析中心成为高频入口，升级为第三个一级 tab。
阶段 3：账户中心（配对、设备、Gmail、备份）从总览"账户中心"入口独立成二级页集合。
```

升级为一级入口的判定标准（写入后续迭代评审）：分析中心周均打开次数超过交易记录 tab 的一半，或 9 个分析功能中有 4 个以上完成图表化。

## 4. 总览页功能

### F1 账户范围切换器（`accounts`）

- 数据：`GET /v1/ledger/accounts`，展示 `display_name`、`masked_suffix`、`base_currency`。
- 交互：置顶大号切换器，提供"单账户"与"全部账户"两档；切换后以下查询全部携带新的 `account_scope`，页面内数字以轻量过渡刷新。
- 规则：不存在隐式默认全部；记住最近选择但每次可见当前范围。`all` 范围内若账户基础币种不同，只分组展示、不生成伪总计。
- Agent 联动：已有 Agent 会话切换范围时，发送明确的上下文变更消息（产品方案 11.2），不悄悄改变此前回答含义。

### F2 核心指标卡（`summary`）

CLI `summary` 输出 12 项指标，App 分三组呈现，每组标注口径：

| 组 | 指标 | 来源字段 | 呈现 |
| --- | --- | --- | --- |
| 已实现 | 已实现损益-合计 / 股票 / 期权 | `realized_pnl_total` / `realized_pnl_stock` / `realized_pnl_option` | 主卡大数字（FIFO 口径），股票与期权拆分为两行 |
| 持仓与资金 | 未平仓数量、推算现金余额、净入金 | `open_positions`、`cash_balance`、`net_deposits` | 行卡；现金余额标注"Σamount 推算" |
| 累计 | 费用合计、股息、利息、税项 | `total_fees`、`dividends`、`interest`、`taxes` | 紧凑行卡 |
| 元信息 | 交易记录数、数据区间 | `txn_count`、`date_range` | 指标卡底部小字，同时承担 `as_of` 展示 |

- 已实现损益主卡沿用现有"FIFO 口径"标注；盈亏色同时使用符号（+/−）与语义色，不只靠红绿。
- 现状：总览已有已实现损益与持仓成本两张卡；本次补齐股票/期权拆分、现金余额、费用/股息/利息/税项与元信息行。

### F3 数据可信度状态条（`audit`）

- 数据：`GET /v1/ledger/audit`，CLI 输出 `checks[]`（`status`、`severity`、`check`、`count`、`expectation`）与 `ok`、`failed`、`transaction_count`。
- 总览呈现：单行状态条——"账本健康 · N 项检查通过"或"账本异常 · N 项检查失败"，点击进入审计详情页（置于交易记录 tab 末尾 section）。
- 详情页：按 `severity` 分组列出检查项，失败项给出 `expectation` 说明；本页同时展示 `ledger_version`、最近导入时间、数据来源分布（CSV / Gmail）。
- 状态条是总览的常驻元素：账本不可信时，其余指标的视觉权重应让位于它。

### F4 期权到期提醒卡（`expiring`）

- 数据：`GET /v1/ledger/expiring?days=30`，字段 `status`（expired / expiring）、`days_remaining`、`symbol_key`、`direction`、`qty`、`premium_dollars`、`expiry`、`first_open_date`。
- 总览呈现：仅当存在记录时显示待办卡——"3 个期权 30 天内到期，2 个已过期未平仓"，点击进入到期列表（持仓 section 内）。
- 列表页：`--days` 提供默认 30、可调（7/30/60/90）；已过期与将到期分两段；显示剩余天数、方向、数量与权利金金额。
- 排序沿用 CLI：按到期日近远排列。

### F5 官方对账提醒（`reconcile` 对应的产品化）

- 背景：Gmail eConfirm 只覆盖成交，股息、税项、转账、到期指派依赖官方 CSV。
- 总览呈现：每个账户按自然月配置提醒；到期未对账时显示待办卡"本月尚未导入官方数据"，点击直达导入向导（F19）。
- 服务端 `official-import-reminder` 已存在，本功能仅为 App 呈现与跳转。

## 5. 交易记录 tab 功能

现有 `SectionScreen` 的三个 section（全部交易 / 按股票分组 / 按账户分组）扩展为五个 section，全部走命令参数映射的筛选器。

### F6 交易流水（`trades`）

- 数据：`GET /v1/ledger/trades`，行字段 `txn_date`、`action`、`raw_symbol`、`quantity`、`price`、`fees`、`amount`。
- 筛选器（与 CLI 参数一一对应）：
  - 标的：`--symbol` 文本搜索（自动大写）。
  - 动作：`--action` 下拉，选项来自账本实际 Action 分布（Buy / Sell 到期指派 / MoneyLink Transfer 等）。
  - 日期范围：`--from` / `--to` 日期选择器。
  - 数量：`--limit` 映射为分页 page size（默认 50），滚动加载。
- 行卡：日期、动作徽标、标的（股票代码或期权合约）、数量 × 价格、费用、净额；期权行显示完整 `raw_symbol`。
- 时间线按月分组吸顶；来源标识（CSV / Gmail）以小徽标显示。

### F7 已实现损益明细（`realized`）

- 数据：`GET /v1/ledger/realized`，行字段 `close_date`、`symbol_key`、`direction`、`qty`、`open_price`、`close_price`、`open_date`、`pnl`、`close_action`；extra 提供 `total_pnl`。
- 筛选器：`--symbol`、`--from`、`--to`（平仓日期口径）。
- 呈现：顶部合计（`total_pnl`，FIFO 已实现口径）；列表为开平配对卡——开仓日期/价格 → 平仓日期/价格、方向、数量、`close_action`（Sell / 到期 / 指派 / 行权）、本次盈亏。
- 点击任一配对卡，跳转交易流水并预填 `--symbol` + `--to close_date`，展示两次原始成交。

### F8 现金流（`cashflow`）

- 数据：`GET /v1/ledger/cashflow`，按 `--from` / `--to` 过滤。
- 呈现：四类分组页签或分段列表——转账（MoneyLink）、股息、利息、税项；每类小计 + 明细行（日期、金额、说明）。
- 与 `summary` 中四个累计指标互为"汇总 ↔ 明细"关系：总览股息卡点击可跳转本页股息页签。

### F9 持仓（`positions`）

- 数据：`GET /v1/ledger/positions`，字段 `underlying`、`symbol_key`、`asset_type`、`direction`、`qty`、`cost`、`expiry`、`strike`、`option_type`、`first_open_date`；`-u` 映射为标的筛选。
- 呈现：按 `asset_type`（stock / option）分段；股票行显示股数、方向、成本、最早开仓日；期权行显示合约键、方向（Long/Short）、张数、行权价、到期日、权利金成本。
- 明确不展示：市值、未实现盈亏、涨跌幅（无行情源）；行内可显示"未接入行情"口径说明入口。
- 到期风险联动：30 天内到期的期权行显示提醒标记，点击进入 F4 列表。

### F10 审计详情（`audit`）

见 F3；作为交易记录 tab 的第五个 section，承接状态条跳转。

## 6. 分析中心（二级页）

从总览快捷块推入的独立页面，聚合 9 个分析命令。页面顶部提供两个公共控件：

- 日期范围（映射各命令的 `--from` / `--to`，`risk` / `expiring` 用 `--as-of`）。
- 口径入口：每张图表右上角"口径"按钮，弹出说明——数据范围、是否仅已实现、是否含费用、多账户聚合方式、缺失数据源。

### F11 月度绩效（`monthly`）

- 字段：`month`、`closed_lots`、`close_transactions`、`wins`、`losses`、`win_rate_pct`、`gross_profit`、`gross_loss`、`profit_factor`、`net_pnl`、`fees`。
- 呈现：`net_pnl` 按月柱状图（盈亏双色 + 符号），下方月度表格（胜率、毛利/毛损、profit factor、费用）。点击柱进入该月已实现明细（F7 预填 `--from`/`--to` 为该月）。

### F12 仓位配置（`allocation`）

- 字段：`category`、`positions`、`long_positions`、`short_positions`、`cost_basis`、`allocation_pct`。
- 维度切换：chips 实现 `--group-by underlying | asset-type`。
- 呈现：环图按历史成本占比 + 右侧图例（多/空持仓数、成本）；口径注明"按历史成本，非市值"。

### F13 利润归因（`attribution`）

- 字段：`category`、`closed_lots`、`close_transactions`、`wins`、`losses`、`win_rate_pct`、`net_pnl`、`absolute_contribution_pct`、`fees`。
- 维度切换：`--group-by` 四档 chips（underlying / asset-type / direction / close-action）。
- 呈现：正负分向条形图（`net_pnl`），标注贡献占比；点击条目展开该组的胜率与费用。

### F14 已实现回撤（`drawdown`）

- 字段：逐日 `date`、`daily_pnl`、`cumulative_pnl`、`running_peak`、`drawdown`；extra 含 `ending_cumulative_pnl` 与最大回撤。
- 呈现：累计已实现曲线 + 回撤阴影区域；标注最大回撤区间与结束累计值。
- 口径提示必须写明："基于已实现盈亏，不代表账户净值回撤"（产品方案 13.8）。

### F15 期权风险（`risk`）

- 字段：按到期 bucket 的 `positions`、`contracts`、多/空持仓与张数、`long_premium_paid`、`short_premium_received`、`long_premium_pct`。
- 呈现：到期日分布条形图（多头/空头堆叠），顶部摘要（未平仓合约数、多头权利金占比）；`--as-of` 支持回看历史基准日。

### F16 持有周期（`holding-period`）

- 字段：`bucket`、`closed_lots`、`wins`、`losses`、`win_rate_pct`、`average_holding_days`、`average_lot_pnl`、`net_pnl`。
- 呈现：按持有期 bucket 的分组柱状图 + 每档平均盈亏；表格补充胜率与平均持有天数。

### F17 最佳/最差交易（`extremes`）

- 字段：`side`（best/worst）、`rank`、`close_date`、`underlying`、`symbol_key`、`asset_type`、`direction`、`close_action`、`qty`、`pnl`、`fees`。
- `--limit` 映射为"Top N"选择（默认 10）。
- 呈现：两段榜单卡（最佳 N / 最差 N），每项显示盈亏、标的、方向、平仓动作；点击进入 F7 配对明细。

### F18 连胜/连亏（`streaks`）

- 字段：`kind`、`count`、`start_date`、`end_date`、`pnl`；extra 注明口径为聚合平仓交易。
- 呈现：最长连胜、最长连亏两张对比卡，附起止日期与区间盈亏。

### F19 压力测试（`stress`）

- 字段：`underlying`、`contracts`、`premium_at_risk`、`loss_if_zero`、`portfolio_premium_pct`、`nearest_expiry`、`farthest_expiry`。
- 呈现：按标的的"权利金归零"损失表 + 占比条；顶部说明文字解释假设（所有多头期权价值归零）与该假设的局限。

## 7. Agent 功能（F20）

服务端 Tool Registry 已注册全部 18 个工具，Agent 页新增两件事：

1. 快捷提问（chips，映射命令意图）：
   - summary → "我的整体已实现盈亏怎么样？"
   - expiring → "最近有哪些期权快到期？"
   - attribution → "我的利润主要来自哪些标的？"
   - drawdown → "最大回撤发生在什么时候？"
   - stress → "如果所有多头期权归零我会亏多少？"
2. 工具卡片：回答中每个金融数字关联来源工具与口径入口（范围 / 截止时间 / 账本版本 / 口径），点击展开 `agent_tool_calls` 详情。

## 8. 通用设计规则

### 8.1 口径四元组

每个含金融数字的页面/卡片满足：账户范围常驻可见、`as_of` 与 `ledger_version` 在页面级显示一次、计算口径通过标签或口径入口展示、数据来源（CSV / Gmail）在明细行级可见。

### 8.2 数值与语义

- 金额、数量、比例使用 Geist Mono / tabular figures；十进制字符串解析。
- 盈亏：符号 + 语义色 + 方向图形三重编码；负数括号或 − 号统一为一种。

### 8.3 空态与错误态

| 场景 | 呈现 |
| --- | --- |
| 无数据（新账本） | section 级空态文案 + "导入官方数据"入口，不显示 0 值图表 |
| `QUERY_DATABASE_ERROR` 类服务错误 | 保留页面骨架，错误条给稳定错误码与重试 |
| 筛选无结果 | "无匹配记录"，提供清除筛选 |
| `--from` 晚于 `--to` | 客户端预校验，提交前阻止（与 CLI `INVALID_ARGUMENT` 语义一致） |

### 8.4 筛选器组件

统一四个组件：日期范围选择器（from/to）、文本搜索（symbol/action）、维度 chips（group-by）、Top N 选择（limit）。所有筛选状态写入页面 URL/深链参数，保证分享与回退一致。

## 9. 交付顺序建议

| 批次 | 内容 | 依赖 |
| --- | --- | --- |
| P0 | F1 账户切换器、F2 指标卡补齐、F6 流水筛选器、F7 已实现明细 | 服务端路由已就绪，纯 App 工作 |
| P1 | F8 现金流、F9 持仓、F4 到期提醒、F3 审计状态条与详情 | 纯 App 工作 |
| P2 | 分析中心（F11–F19）九个图表功能 + 公共筛选/口径组件 | fl_chart 接入、图表设计规范 |
| P3 | F20 Agent 快捷提问与工具卡片、F5 对账提醒呈现、分析中心升级一级入口评审 | Agent 会话流已通 |

每批次完成后按产品方案第 23 章补齐对应 widget 测试与 golden 基线。

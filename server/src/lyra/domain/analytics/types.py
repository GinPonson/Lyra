"""分析层类型。

字段名刻意与 oracle（`schwab-cli`）输出的 JSON 键保持一致：这样对照测试可以
直接逐字段比较，而不必依赖"键名归一后取交集"的宽松规则——后者会掩盖命名差异
背后的真实口径差异。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from ..fifo.types import LotDirection
from ..ledger.types import AssetType, LocalDate, OptionType


@dataclass(frozen=True)
class AccountScope:
    """显式账户范围。

    调用方必须显式给出范围，不提供"隐式全部账户"的默认值——分析结果一旦
    跨账户静默合并，用户看到的合计就不再对应任何真实账户。
    """

    kind: Literal["all", "account"]
    account: str | None = None


@dataclass
class PositionRow:
    """当前持仓聚合行；与 oracle 的 ``v_positions`` 同口径。"""

    account: str
    symbol_key: str | None
    asset_type: AssetType
    underlying: str | None
    expiry: LocalDate | None
    strike: Decimal | None
    option_type: OptionType | None
    direction: LotDirection
    qty: Decimal
    cost: Decimal
    first_open_date: LocalDate


@dataclass(frozen=True)
class ExpiringPositionRow:
    """到期窗口内的未平期权持仓。"""

    account: str
    symbol_key: str | None
    underlying: str | None
    direction: LotDirection
    qty: Decimal
    cost: Decimal
    expiry: LocalDate
    strike: Decimal | None
    option_type: OptionType | None
    first_open_date: LocalDate
    status: Literal["expired", "expires_today", "upcoming"]
    days_remaining: int
    premium_dollars: Decimal


@dataclass(frozen=True)
class ExpiringPositionsResult:
    """到期明细、截止日期与各状态计数。"""

    rows: tuple[ExpiringPositionRow, ...]
    count: int
    as_of: str
    cutoff: LocalDate
    expired: int
    expires_today: int
    upcoming: int


@dataclass(frozen=True)
class AccountSummary:
    """账户总览；金额已按现有 schema 的小数位输出为文本。"""

    net_deposits: str
    realized_pnl_stock: str
    realized_pnl_option: str
    realized_pnl_total: str
    open_positions: int
    total_fees: str
    dividends: str
    interest: str
    taxes: str
    cash_balance: str
    txn_count: int
    date_range: str | None


@dataclass(frozen=True)
class MonthlyPerformanceRow:
    """单个月份的已实现绩效。"""

    month: str
    closed_lots: int
    close_transactions: int
    wins: int
    losses: int
    gross_profit: str
    gross_loss: str
    net_pnl: str
    fees: str
    win_rate_pct: str | None
    profit_factor: str | None


@dataclass(frozen=True)
class MonthlyPerformanceResult:
    """月度绩效全量结果。"""

    rows: tuple[MonthlyPerformanceRow, ...]
    months: int
    total_pnl: str
    total_fees: str
    basis: str


@dataclass(frozen=True)
class AllocationRow:
    """一个分类的历史成本配置。"""

    category: str | None
    positions: int
    long_positions: int
    short_positions: int
    cost_basis: str
    allocation_pct: str | None


@dataclass(frozen=True)
class AllocationResult:
    """历史成本配置结果；不是实时市值配置。"""

    rows: tuple[AllocationRow, ...]
    groups: int
    total_cost_basis: str
    basis: str


@dataclass(frozen=True)
class AttributionRow:
    """一个维度的已实现损益贡献。"""

    category: str | None
    closed_lots: int
    close_transactions: int
    wins: int
    losses: int
    net_pnl: str
    fees: str
    win_rate_pct: str | None
    absolute_contribution_pct: str | None


@dataclass(frozen=True)
class AttributionResult:
    """归因结果。"""

    rows: tuple[AttributionRow, ...]
    groups: int
    total_pnl: str
    basis: str


@dataclass(frozen=True)
class DrawdownRow:
    """日度累计已实现损益曲线上的一个点。"""

    date: LocalDate
    daily_pnl: str
    cumulative_pnl: str
    running_peak: str
    drawdown: str


@dataclass(frozen=True)
class DrawdownResult:
    """日度曲线、最大回撤峰谷与恢复日期。"""

    rows: tuple[DrawdownRow, ...]
    days: int
    ending_cumulative_pnl: str
    max_drawdown: str
    peak_date: LocalDate | None
    trough_date: LocalDate | None
    recovery_date: LocalDate | None
    basis: str


OptionRiskBucket = Literal["expired", "0-30d", "31-60d", "61-90d", "90d+"]


@dataclass(frozen=True)
class OptionRiskRow:
    """一个到期窗口内的期权敞口汇总。"""

    bucket: OptionRiskBucket
    positions: int
    contracts: str
    long_positions: int
    short_positions: int
    long_contracts: str
    short_contracts: str
    long_premium_paid: str
    short_premium_received: str
    long_premium_pct: str | None


@dataclass(frozen=True)
class LongOptionUnderlyingRisk:
    """单一标的的长期权历史成本集中度。"""

    underlying: str | None
    long_premium_paid: str
    long_premium_pct: str | None


@dataclass(frozen=True)
class OptionRiskResult:
    """期权风险桶与长期权集中度；仅描述历史权利金，不是完整风险价值。"""

    rows: tuple[OptionRiskRow, ...]
    as_of: str
    option_positions: int
    total_long_premium_paid: str
    total_short_premium_received: str
    top_three_long_underlyings: tuple[LongOptionUnderlyingRisk, ...]
    top_three_long_concentration_pct: str | None
    basis: str


@dataclass
class ClosedTransaction:
    """一笔平仓交易：把跨越多个 FIFO 批次的 realized 行重新聚合后的结果。

    字段名与 oracle 输出一致（``close_txn`` 而不是 ``close_transaction``），
    否则对照测试会因为键名不同而**跳过一个本该比对的字段**。
    """

    close_txn: str
    account: str
    close_date: LocalDate
    symbol_key: str | None
    underlying: str | None
    asset_type: AssetType
    direction: LotDirection
    close_action: str
    qty: Decimal
    pnl: Decimal
    fees: Decimal
    earliest_open_date: LocalDate
    latest_open_date: LocalDate


@dataclass
class ExtremeTradeRow(ClosedTransaction):
    """最佳/最差交易明细；继承平仓交易字段以便与 oracle 的扁平行直接对照。

    与基类一样是可变 dataclass：Python 的 dataclass 不允许冻结的子类继承
    非冻结的基类，而基类在聚合过程中需要就地累加。
    """

    side: Literal["best", "worst"]
    rank: int


@dataclass(frozen=True)
class ExtremeTradesResult:
    """最佳与最差各 N 笔。"""

    rows: tuple[ExtremeTradeRow, ...]
    per_side: int
    basis: str


@dataclass(frozen=True)
class HoldingPeriodRow:
    """一个持有期桶的已实现绩效。"""

    bucket: str
    closed_lots: int
    close_transactions: int
    wins: int
    losses: int
    average_holding_days: str
    net_pnl: str
    average_lot_pnl: str
    win_rate_pct: str | None


@dataclass(frozen=True)
class HoldingPeriodResult:
    """持有期汇总结果。"""

    rows: tuple[HoldingPeriodRow, ...]
    buckets: int
    basis: str


@dataclass(frozen=True)
class StreakRow:
    """一段连续盈利或连续亏损。"""

    kind: Literal["win", "loss"]
    count: int
    start_date: LocalDate
    end_date: LocalDate
    pnl: str


@dataclass(frozen=True)
class StreaksResult:
    """最长连续盈利与连续亏损。"""

    rows: tuple[StreakRow, ...]
    close_transactions: int
    neutral_transactions: int
    basis: str


@dataclass(frozen=True)
class OptionStressRow:
    """单一标的的长期权历史成本压力。"""

    underlying: str | None
    contracts: str
    premium_at_risk: str
    nearest_expiry: LocalDate | None
    farthest_expiry: LocalDate | None
    loss_if_zero: str
    portfolio_premium_pct: str | None


@dataclass(frozen=True)
class OptionStressResult:
    """长期权权利金归零的压力结果；不含市场数据。"""

    rows: tuple[OptionStressRow, ...]
    long_option_contracts: str
    all_long_options_loss_if_zero: str
    top_three_loss_if_zero: str
    basis: str


@dataclass(frozen=True)
class TradeRow:
    """交易流水行；字段名与 oracle 的 ``transactions`` 列一致。"""

    txn_hash: str
    account: str
    record_date: LocalDate
    txn_date: LocalDate
    action: str
    raw_symbol: str | None
    description: str | None
    asset_type: AssetType
    underlying: str | None
    expiry: LocalDate | None
    strike: Decimal | None
    option_type: OptionType | None
    quantity: Decimal | None
    price: Decimal | None
    fees: Decimal | None
    amount: Decimal | None
    source_hash: str
    seq: int


@dataclass(frozen=True)
class TradeQueryResult:
    """交易流水查询结果。"""

    rows: tuple[TradeRow, ...]
    count: int
    # 下一页游标；``None`` 表示已是最后一页。
    next_cursor: str | None = None


@dataclass(frozen=True)
class RealizedRow:
    """已实现明细行；字段名与 oracle 的 ``realized`` 列一致。

    刻意不含 ``realized_id``：那是数据库自增主键，领域层没有也不需要它。
    """

    account: str
    symbol_key: str | None
    asset_type: AssetType
    underlying: str | None
    direction: LotDirection
    open_date: LocalDate
    close_date: LocalDate
    qty: Decimal
    open_price: Decimal
    close_price: Decimal
    open_fees: Decimal
    close_fees: Decimal
    pnl: Decimal
    close_action: str
    open_txn: str
    close_txn: str


@dataclass(frozen=True)
class RealizedQueryResult:
    """已实现明细查询结果。"""

    rows: tuple[RealizedRow, ...]
    count: int
    total_pnl: str
    # **全部筛选行的条数，而非当前页条数。**
    total_count: int = 0
    # 下一页游标；``None`` 表示已是最后一页。
    next_cursor: str | None = None


@dataclass(frozen=True)
class CashflowRow:
    """现金流水行；字段名与 oracle 的 ``v_cashflows`` 列一致。"""

    account: str
    txn_date: LocalDate
    action: str
    description: str | None
    amount: Decimal | None
    txn_hash: str


@dataclass(frozen=True)
class CashflowQueryResult:
    """现金流水查询结果。"""

    rows: tuple[CashflowRow, ...]
    count: int
    total: str
    by_action: dict[str, str]
    # **全部筛选行的条数，而非当前页条数。**
    total_count: int = 0
    # 下一页游标；``None`` 表示已是最后一页。
    next_cursor: str | None = None


@dataclass(frozen=True)
class LedgerAuditCheck:
    """一项账本一致性检查。"""

    check: str
    severity: str
    count: int
    expectation: str
    status: str


@dataclass(frozen=True)
class LedgerAuditResult:
    """只读审计报告。"""

    rows: tuple[LedgerAuditCheck, ...]
    ok: bool
    failed: int
    transaction_count: int
    latest_transaction_date: LocalDate | None
    read_only: bool
    scope_note: str

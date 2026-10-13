"""回撤与期权风险。

镜像 TypeScript 版 ``risk.ts``。两者都只描述**历史已实现或已支付**的数字：
回撤基于日度已实现损益，期权风险基于历史权利金，都不使用市场价格，也不等同
于完整的风险价值。在输出与文档里都不能把它们说成"当前风险"。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from ..fifo.types import Lot, RealizedRecord
from .dates import (
    days_between,
    is_account_in_scope,
    is_date_in_range,
    parse_iso_date,
    validate_date_range,
)
from .positions import OPTION_MULTIPLIER, aggregate_positions
from .types import (
    AccountScope,
    DrawdownResult,
    DrawdownRow,
    LongOptionUnderlyingRisk,
    OptionRiskBucket,
    OptionRiskResult,
    OptionRiskRow,
)

# 期权风险桶的固定展示顺序。
RISK_BUCKET_ORDER: tuple[OptionRiskBucket, ...] = (
    "expired", "0-30d", "31-60d", "61-90d", "90d+",
)

_AMOUNT_QUANTUM = Decimal("0.0001")
_PCT_QUANTUM = Decimal("0.01")
_PREMIUM_QUANTUM = Decimal("0.01")
_CONTRACT_QUANTUM = Decimal("0.000001")


def calculate_drawdown(
    records: tuple[RealizedRecord, ...],
    account_scope: AccountScope,
    from_date: str | None = None,
    to_date: str | None = None,
) -> DrawdownResult:
    """按日计算累计已实现损益与峰谷回撤。

    @param records: FIFO 已实现明细。
    @param account_scope: 显式账户范围。
    @param from_date: 可选起始平仓日期。
    @param to_date: 可选截止平仓日期。
    @returns 日度曲线、最大回撤峰谷与恢复日期。
    @raises ValueError: 日期无效或范围反向。
    """
    validate_date_range(from_date, to_date)
    daily: dict[str, Decimal] = {}
    for record in records:
        if (
            not is_account_in_scope(record.account, account_scope)
            or not is_date_in_range(record.close_date, from_date, to_date)
        ):
            continue
        daily[record.close_date] = daily.get(record.close_date, Decimal(0)) + record.pnl

    cumulative = Decimal(0)
    peak = Decimal(0)
    peak_date: str | None = None
    max_drawdown = Decimal(0)
    max_peak_date: str | None = None
    trough_date: str | None = None
    recovery_date: str | None = None
    active_peak = Decimal(0)
    rows: list[DrawdownRow] = []

    for date in sorted(daily):
        daily_pnl = daily[date]
        cumulative += daily_pnl
        # 严格大于才更新峰值：同值峰值取**最早**出现的那次。
        if cumulative > peak:
            peak = cumulative
            peak_date = date
        current_drawdown = peak - cumulative
        if current_drawdown > max_drawdown:
            max_drawdown = current_drawdown
            max_peak_date = peak_date
            trough_date = date
            recovery_date = None
            active_peak = peak
        elif (
            trough_date is not None
            and recovery_date is None
            and date > trough_date
            and cumulative >= active_peak
        ):
            recovery_date = date
        rows.append(DrawdownRow(
            date=date,
            daily_pnl=_amount_text(daily_pnl),
            cumulative_pnl=_amount_text(cumulative),
            running_peak=_amount_text(peak),
            drawdown=_amount_text(current_drawdown),
        ))

    return DrawdownResult(
        rows=tuple(rows),
        days=len(rows),
        ending_cumulative_pnl=_amount_text(cumulative),
        max_drawdown=_amount_text(max_drawdown),
        peak_date=max_peak_date,
        trough_date=trough_date,
        recovery_date=recovery_date,
        basis="daily_realized_fifo",
    )


@dataclass
class _RiskBucket:
    """风险桶累加器；仅在函数内部使用。"""

    positions: int = 0
    contracts: Decimal = Decimal(0)
    long_positions: int = 0
    short_positions: int = 0
    long_contracts: Decimal = Decimal(0)
    short_contracts: Decimal = Decimal(0)
    long_premium_paid: Decimal = Decimal(0)
    short_premium_received: Decimal = Decimal(0)


def calculate_option_risk(
    lots: tuple[Lot, ...],
    account_scope: AccountScope,
    as_of: str,
) -> OptionRiskResult:
    """按到期窗口分别汇总当前长期权历史成本与短期权历史收入。

    每个账户的 FIFO 批次先按持仓口径聚合，再进入风险桶——若不先聚合，
    同一合约的多个批次会被重复计为一个"持仓"。

    @param lots: FIFO 未平批次。
    @param account_scope: 显式账户范围。
    @param as_of: ISO 分析基准日。
    @returns 风险桶、长期权集中度与历史权利金合计。
    @raises ValueError: 基准日非法。
    """
    analysis_date = parse_iso_date(as_of)
    positions = [
        position for position in aggregate_positions(lots, account_scope)
        if position.asset_type == "option" and position.expiry is not None
    ]
    buckets: dict[OptionRiskBucket, _RiskBucket] = {}
    long_by_underlying: dict[str | None, Decimal] = {}
    total_long_paid = Decimal(0)
    total_short_received = Decimal(0)

    for position in positions:
        assert position.expiry is not None  # 上面的过滤已保证
        days_remaining = days_between(parse_iso_date(position.expiry), analysis_date)
        bucket = _risk_bucket(days_remaining)
        premium = (abs(position.cost) * OPTION_MULTIPLIER).quantize(
            _PREMIUM_QUANTUM, rounding=ROUND_HALF_EVEN
        )
        item = buckets.setdefault(bucket, _RiskBucket())
        item.positions += 1
        item.contracts += position.qty

        if position.direction == "long":
            item.long_positions += 1
            item.long_contracts += position.qty
            item.long_premium_paid += premium
            long_by_underlying[position.underlying] = (
                long_by_underlying.get(position.underlying, Decimal(0)) + premium
            )
            total_long_paid += premium
        else:
            item.short_positions += 1
            item.short_contracts += position.qty
            item.short_premium_received += premium
            total_short_received += premium

    rows = tuple(
        OptionRiskRow(
            bucket=bucket,
            positions=item.positions,
            contracts=format(item.contracts.quantize(_CONTRACT_QUANTUM), "f"),
            long_positions=item.long_positions,
            short_positions=item.short_positions,
            long_contracts=format(item.long_contracts.quantize(_CONTRACT_QUANTUM), "f"),
            short_contracts=format(item.short_contracts.quantize(_CONTRACT_QUANTUM), "f"),
            long_premium_paid=format(item.long_premium_paid, "f"),
            short_premium_received=format(item.short_premium_received, "f"),
            long_premium_pct=_share_pct(item.long_premium_paid, total_long_paid),
        )
        for bucket in RISK_BUCKET_ORDER
        if (item := buckets.get(bucket)) is not None
    )

    ranked = sorted(
        long_by_underlying.items(),
        key=lambda item: (-item[1], item[0] or ""),
    )
    top_three = ranked[:3]
    top_three_cost = sum((cost for _, cost in top_three), Decimal(0))

    return OptionRiskResult(
        rows=rows,
        as_of=as_of,
        option_positions=len(positions),
        total_long_premium_paid=format(total_long_paid, "f"),
        total_short_premium_received=format(total_short_received, "f"),
        top_three_long_underlyings=tuple(
            LongOptionUnderlyingRisk(
                underlying=underlying,
                long_premium_paid=format(cost, "f"),
                long_premium_pct=_share_pct(cost, total_long_paid),
            )
            for underlying, cost in top_three
        ),
        top_three_long_concentration_pct=_share_pct(top_three_cost, total_long_paid),
        basis="long_paid_and_short_received_premium_not_market_risk",
    )


def _risk_bucket(days_remaining: int) -> OptionRiskBucket:
    """把相对到期天数映射到稳定风险桶。"""
    if days_remaining < 0:
        return "expired"
    if days_remaining <= 30:
        return "0-30d"
    if days_remaining <= 60:
        return "31-60d"
    if days_remaining <= 90:
        return "61-90d"
    return "90d+"


def _share_pct(part: Decimal, total: Decimal) -> str | None:
    """计算占比；合计为零时返回 ``None`` 而不是 0，避免把"无数据"说成"占比为零"。"""
    if total == 0:
        return None
    return format(
        (part * 100 / total).quantize(_PCT_QUANTUM, rounding=ROUND_HALF_EVEN), "f"
    )


def _amount_text(value: Decimal) -> str:
    """按既有 schema 的四位小数渲染金额。"""
    return format(value.quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_EVEN), "f")

"""绩效、配置与归因。

镜像 TypeScript 版 ``performance.ts``。三条命令共享同一套"先分组、再排序、
最后渲染"的结构，因此这里把公共部分抽出来，避免三处各自实现舍入与占比规则。

**排序稳定性是口径的一部分**：占比相同的分类必须按名称升序，否则同一份账本
两次运行的表格顺序可能不同。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

from ..fifo.types import Lot, RealizedRecord
from .dates import is_account_in_scope, is_date_in_range, validate_date_range
from .positions import aggregate_positions
from .types import (
    AccountScope,
    AllocationResult,
    AllocationRow,
    AttributionResult,
    AttributionRow,
    MonthlyPerformanceResult,
    MonthlyPerformanceRow,
)

AllocationGroupBy = Literal["underlying", "asset-type"]
AttributionGroupBy = Literal["underlying", "asset-type", "direction", "close-action"]

_PCT_QUANTUM = Decimal("0.01")
_AMOUNT_QUANTUM = Decimal("0.0001")
_FEE_QUANTUM = Decimal("0.000001")


def calculate_monthly_performance(
    records: tuple[RealizedRecord, ...],
    account_scope: AccountScope,
    from_date: str | None = None,
    to_date: str | None = None,
) -> MonthlyPerformanceResult:
    """按平仓月份汇总 FIFO 已实现绩效。

    @param records: FIFO 已实现明细。
    @param account_scope: 显式账户范围。
    @param from_date: 可选起始平仓日期，闭区间。
    @param to_date: 可选截止平仓日期，闭区间。
    @returns 月度胜率、盈亏比、净损益与费用。
    @raises ValueError: 日期无效或范围反向。
    """
    validate_date_range(from_date, to_date)
    groups: dict[str, list[RealizedRecord]] = {}
    for record in records:
        if (
            not is_account_in_scope(record.account, account_scope)
            or not is_date_in_range(record.close_date, from_date, to_date)
        ):
            continue
        groups.setdefault(record.close_date[:7], []).append(record)

    rows: list[MonthlyPerformanceRow] = []
    for month in sorted(groups):
        monthly = groups[month]
        wins = sum(1 for record in monthly if record.pnl > 0)
        losses = sum(1 for record in monthly if record.pnl < 0)
        decisive = wins + losses
        gross_profit = _sum_decimals([r.pnl for r in monthly if r.pnl > 0])
        gross_loss = _sum_decimals([r.pnl for r in monthly if r.pnl < 0])
        net_pnl = _sum_decimals([record.pnl for record in monthly])
        fees = _sum_decimals([record.open_fees + record.close_fees for record in monthly])

        rows.append(MonthlyPerformanceRow(
            month=month,
            closed_lots=len(monthly),
            close_transactions=len({record.close_transaction for record in monthly}),
            wins=wins,
            losses=losses,
            gross_profit=format(gross_profit.quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_EVEN), "f"),
            gross_loss=format(gross_loss.quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_EVEN), "f"),
            net_pnl=format(net_pnl.quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_EVEN), "f"),
            fees=format(fees.quantize(_FEE_QUANTUM, rounding=ROUND_HALF_EVEN), "f"),
            win_rate_pct=_ratio_pct(wins, decisive),
            profit_factor=(
                format(
                    (gross_profit / abs(gross_loss)).quantize(_PCT_QUANTUM, rounding=ROUND_HALF_EVEN),
                    "f",
                )
                if gross_loss < 0
                else None
            ),
        ))

    # 合计取自行里**已舍入**的文本，与现有实现一致：合计必须等于表格上数字的和。
    total_pnl = _sum_decimals([Decimal(row.net_pnl) for row in rows])
    total_fees = _sum_decimals([Decimal(row.fees) for row in rows])
    return MonthlyPerformanceResult(
        rows=tuple(rows),
        months=len(rows),
        total_pnl=format(total_pnl.quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_EVEN), "f"),
        total_fees=format(total_fees.quantize(_FEE_QUANTUM, rounding=ROUND_HALF_EVEN), "f"),
        basis="realized_fifo",
    )


@dataclass
class _AllocationGroup:
    """配置分组的累加器；仅在函数内部使用。"""

    positions: int = 0
    long_positions: int = 0
    short_positions: int = 0
    cost_basis: Decimal = Decimal(0)


def calculate_allocation(
    lots: tuple[Lot, ...],
    account_scope: AccountScope,
    group_by: AllocationGroupBy,
) -> AllocationResult:
    """按标的或资产类型汇总当前未平仓历史成本配置。

    @param lots: FIFO 未平批次。
    @param account_scope: 显式账户范围。
    @param group_by: ``underlying`` 或 ``asset-type``。
    @returns 历史成本及其绝对金额占比；结果不是实时市值配置。
    """
    positions = aggregate_positions(lots, account_scope)
    groups: dict[str | None, _AllocationGroup] = {}

    for position in positions:
        category = position.underlying if group_by == "underlying" else position.asset_type
        group = groups.setdefault(category, _AllocationGroup())
        group.positions += 1
        if position.direction == "long":
            group.long_positions += 1
        else:
            group.short_positions += 1
        multiplier = 100 if position.asset_type == "option" else 1
        group.cost_basis += abs(position.cost) * multiplier

    raw_total_cost = _sum_decimals([group.cost_basis for group in groups.values()])

    ordered = sorted(
        groups.items(),
        key=lambda item: (-item[1].cost_basis, item[0] or ""),
    )
    rows = tuple(
        AllocationRow(
            category=category,
            positions=group.positions,
            long_positions=group.long_positions,
            short_positions=group.short_positions,
            cost_basis=format(
                group.cost_basis.quantize(_PCT_QUANTUM, rounding=ROUND_HALF_EVEN), "f"
            ),
            allocation_pct=(
                None
                if raw_total_cost == 0
                else format(
                    (group.cost_basis * 100 / raw_total_cost).quantize(
                        _PCT_QUANTUM, rounding=ROUND_HALF_EVEN
                    ),
                    "f",
                )
            ),
        )
        for category, group in ordered
    )

    return AllocationResult(
        rows=rows,
        groups=len(rows),
        total_cost_basis=format(
            raw_total_cost.quantize(_PCT_QUANTUM, rounding=ROUND_HALF_EVEN), "f"
        ),
        basis="historical_cost_not_market_value",
    )


def calculate_attribution(
    records: tuple[RealizedRecord, ...],
    account_scope: AccountScope,
    group_by: AttributionGroupBy,
    from_date: str | None = None,
    to_date: str | None = None,
) -> AttributionResult:
    """按指定维度拆解 FIFO 已实现损益贡献。

    @param records: FIFO 已实现明细。
    @param account_scope: 显式账户范围。
    @param group_by: 标的、资产类型、方向或平仓动作。
    @param from_date: 可选起始平仓日期。
    @param to_date: 可选截止平仓日期。
    @returns 分组净损益、胜率、费用与绝对贡献占比。
    @raises ValueError: 日期无效或范围反向。
    """
    validate_date_range(from_date, to_date)
    groups: dict[str | None, list[RealizedRecord]] = {}
    for record in records:
        if (
            not is_account_in_scope(record.account, account_scope)
            or not is_date_in_range(record.close_date, from_date, to_date)
        ):
            continue
        groups.setdefault(_attribution_category(record, group_by), []).append(record)

    aggregates = [
        (
            category,
            grouped,
            sum(1 for r in grouped if r.pnl > 0),
            sum(1 for r in grouped if r.pnl < 0),
            _sum_decimals([r.pnl for r in grouped]),
            _sum_decimals([r.open_fees + r.close_fees for r in grouped]),
        )
        for category, grouped in groups.items()
    ]
    total_absolute_pnl = _sum_decimals([abs(item[4]) for item in aggregates])

    ordered = sorted(aggregates, key=lambda item: (-item[4], item[0] or ""))
    rows = tuple(
        AttributionRow(
            category=category,
            closed_lots=len(grouped),
            close_transactions=len({r.close_transaction for r in grouped}),
            wins=wins,
            losses=losses,
            win_rate_pct=_ratio_pct(wins, wins + losses),
            net_pnl=format(net_pnl.quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_EVEN), "f"),
            absolute_contribution_pct=(
                None
                if total_absolute_pnl == 0
                else format(
                    (abs(net_pnl) * 100 / total_absolute_pnl).quantize(
                        _PCT_QUANTUM, rounding=ROUND_HALF_EVEN
                    ),
                    "f",
                )
            ),
            fees=format(fees.quantize(_FEE_QUANTUM, rounding=ROUND_HALF_EVEN), "f"),
        )
        for category, grouped, wins, losses, net_pnl, fees in ordered
    )

    total_pnl = _sum_decimals([item[4] for item in aggregates])
    return AttributionResult(
        rows=rows,
        groups=len(rows),
        total_pnl=format(total_pnl.quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_EVEN), "f"),
        basis="realized_fifo",
    )


def _attribution_category(
    record: RealizedRecord, group_by: AttributionGroupBy
) -> str | None:
    """返回归因维度对应的字段值。"""
    if group_by == "underlying":
        return record.underlying
    if group_by == "asset-type":
        return record.asset_type
    if group_by == "direction":
        return record.direction
    return record.close_action


def _ratio_pct(numerator: int, denominator: int) -> str | None:
    """把胜率渲染为两位小数百分比；没有决定性样本时返回 ``None``。"""
    if denominator == 0:
        return None
    return format(
        (Decimal(numerator) * 100 / Decimal(denominator)).quantize(
            _PCT_QUANTUM, rounding=ROUND_HALF_EVEN
        ),
        "f",
    )


def _sum_decimals(values: list[Decimal]) -> Decimal:
    """合计 Decimal，空集合保持精确 Decimal 零。"""
    total = Decimal(0)
    for value in values:
        total += value
    return total

"""交易统计：平仓交易聚合、持有期、极值、连续与压力。

镜像 TypeScript 版 ``trade-analysis.ts``。四条命令都建立在同一个聚合之上：
**把跨越多个 FIFO 批次的 realized 行重新聚合为一笔平仓交易**。聚合键不同会
让"一笔交易"被算成多笔，进而让胜率、连续区间和极值排名全部偏移。

注意本模块的舍入模式与其他模块**不同**：``holding-period`` 的均值使用
``ROUND_HALF_UP``（与 oracle 的 ``round(avg(...))`` 一致），而胜率仍用
``ROUND_HALF_EVEN``。统一成一种会让个别桶的均值差一分。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Literal

from ..fifo.types import Lot, RealizedRecord
from ..numeric import fixed_text, percent_text
from .dates import days_between, is_account_in_scope, parse_iso_date
from .positions import OPTION_MULTIPLIER, aggregate_positions
from .types import (
    AccountScope,
    ClosedTransaction,
    ExtremeTradeRow,
    ExtremeTradesResult,
    HoldingPeriodResult,
    HoldingPeriodRow,
    OptionStressResult,
    OptionStressRow,
    StreakRow,
    StreaksResult,
)

# 持有周期桶的固定顺序。
HOLDING_BUCKET_ORDER: tuple[str, ...] = ("0-7d", "8-30d", "31-90d", "90d+")

_AMOUNT_QUANTUM = Decimal("0.0001")
_PCT_QUANTUM = Decimal("0.01")
_PREMIUM_QUANTUM = Decimal("0.01")
_CONTRACT_QUANTUM = Decimal("0.000001")


def aggregate_closed_transactions(
    records: tuple[RealizedRecord, ...],
    account_scope: AccountScope,
) -> tuple[ClosedTransaction, ...]:
    """把一笔平仓跨越的多个 FIFO realized 行重新聚合为一笔交易。

    @param records: FIFO 已实现明细。
    @param account_scope: 显式账户范围。
    @returns 按平仓日期、交易哈希稳定排序的平仓交易。
    """
    groups: dict[tuple[object, ...], ClosedTransaction] = {}

    for record in records:
        if not is_account_in_scope(record.account, account_scope):
            continue
        key = (
            record.close_transaction,
            record.account,
            record.close_date,
            record.symbol_key,
            record.underlying,
            record.asset_type,
            record.direction,
            record.close_action,
        )
        existing = groups.get(key)
        if existing is None:
            groups[key] = ClosedTransaction(
                close_txn=record.close_transaction,
                account=record.account,
                close_date=record.close_date,
                symbol_key=record.symbol_key,
                underlying=record.underlying,
                asset_type=record.asset_type,
                direction=record.direction,
                close_action=record.close_action,
                qty=record.quantity,
                pnl=record.pnl,
                fees=record.open_fees + record.close_fees,
                earliest_open_date=record.open_date,
                latest_open_date=record.open_date,
            )
            continue
        existing.qty += record.quantity
        existing.pnl += record.pnl
        existing.fees += record.open_fees + record.close_fees
        existing.earliest_open_date = min(existing.earliest_open_date, record.open_date)
        existing.latest_open_date = max(existing.latest_open_date, record.open_date)

    return tuple(sorted(groups.values(), key=lambda trade: (trade.close_date, trade.close_txn)))


def calculate_holding_period(
    records: tuple[RealizedRecord, ...],
    account_scope: AccountScope,
) -> HoldingPeriodResult:
    """按 FIFO lot 的实际持有天数区间汇总已实现绩效。

    @param records: FIFO 已实现明细。
    @param account_scope: 显式账户范围。
    @returns 四个固定持有期桶中实际存在的结果。
    """
    groups: dict[str, list[tuple[RealizedRecord, int]]] = {}
    for record in records:
        if not is_account_in_scope(record.account, account_scope):
            continue
        holding_days = days_between(
            parse_iso_date(record.close_date), parse_iso_date(record.open_date)
        )
        groups.setdefault(_holding_bucket(holding_days), []).append((record, holding_days))

    rows: list[HoldingPeriodRow] = []
    for bucket in HOLDING_BUCKET_ORDER:
        group = groups.get(bucket)
        if group is None:
            continue
        wins = sum(1 for record, _ in group if record.pnl > 0)
        losses = sum(1 for record, _ in group if record.pnl < 0)
        net_pnl = sum((record.pnl for record, _ in group), Decimal(0))
        # 均值使用 HALF_UP，与 oracle 的 round(avg(...)) 对齐——不是 HALF_EVEN。
        average_holding_days = (
            Decimal(sum(days for _, days in group)) / len(group)
        ).quantize(_PCT_QUANTUM, rounding=ROUND_HALF_UP)
        average_lot_pnl = (net_pnl / len(group)).quantize(
            _AMOUNT_QUANTUM, rounding=ROUND_HALF_UP
        )
        rows.append(HoldingPeriodRow(
            bucket=bucket,
            closed_lots=len(group),
            close_transactions=len({record.close_transaction for record, _ in group}),
            wins=wins,
            losses=losses,
            average_holding_days=format(average_holding_days, "f"),
            net_pnl=format(net_pnl.quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_EVEN), "f"),
            average_lot_pnl=format(average_lot_pnl, "f"),
            win_rate_pct=_ratio_pct(wins, wins + losses),
        ))

    return HoldingPeriodResult(rows=tuple(rows), buckets=len(rows), basis="realized_fifo_lots")


def calculate_extreme_trades(
    records: tuple[RealizedRecord, ...],
    account_scope: AccountScope,
    limit: int,
) -> ExtremeTradesResult:
    """按平仓交易净损益列出最佳和最差各 N 笔。

    @param records: FIFO 已实现明细。
    @param account_scope: 显式账户范围。
    @param limit: 每一侧返回数量，必须大于零。
    @returns 两侧明细，``side`` 为 ``best`` / ``worst``，``rank`` 从 1 起。
    @raises ValueError: limit 不是正整数。
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit 必须是大于 0 的整数")
    trades = aggregate_closed_transactions(records, account_scope)
    # 并列时的次序由平仓日期与交易哈希决定，保证同一份账本结果稳定。
    best = sorted(trades, key=lambda t: (-t.pnl, t.close_date, t.close_txn))[:limit]
    worst = sorted(trades, key=lambda t: (t.pnl, t.close_date, t.close_txn))[:limit]

    rows: list[ExtremeTradeRow] = []
    for index, trade in enumerate(best):
        rows.append(_with_ranking(trade, "best", index + 1))
    for index, trade in enumerate(worst):
        rows.append(_with_ranking(trade, "worst", index + 1))

    return ExtremeTradesResult(
        rows=tuple(rows), per_side=limit, basis="aggregated_close_transaction"
    )


def calculate_streaks(
    records: tuple[RealizedRecord, ...],
    account_scope: AccountScope,
) -> StreaksResult:
    """按平仓交易日期与哈希顺序统计最长连续盈利与连续亏损。

    零损益交易会**结束**当前连续区间，但不计入任何序列。

    @param records: FIFO 已实现明细。
    @param account_scope: 显式账户范围。
    @returns 最长连续盈利与连续亏损（各自至多一条）。
    """
    trades = aggregate_closed_transactions(records, account_scope)
    longest: dict[str, StreakRow] = {}
    current: StreakRow | None = None
    neutral_transactions = 0

    def finish_current() -> None:
        nonlocal current
        if current is None:
            return
        previous = longest.get(current.kind)
        if previous is None or current.count > previous.count:
            longest[current.kind] = current

    for trade in trades:
        if trade.pnl > 0:
            kind: Literal["win", "loss"] | None = "win"
        elif trade.pnl < 0:
            kind = "loss"
        else:
            kind = None

        if kind is None:
            neutral_transactions += 1
            finish_current()
            current = None
            continue
        if current is None or current.kind != kind:
            finish_current()
            current = StreakRow(
                kind=kind,
                count=1,
                start_date=trade.close_date,
                end_date=trade.close_date,
                pnl=_amount_text(trade.pnl),
            )
            continue
        # 累计时重新解析**已舍入**的文本，与现有实现一致。
        current = StreakRow(
            kind=current.kind,
            count=current.count + 1,
            start_date=current.start_date,
            end_date=trade.close_date,
            pnl=_amount_text(Decimal(current.pnl) + trade.pnl),
        )
    finish_current()

    rows = tuple(longest[kind] for kind in ("win", "loss") if kind in longest)
    return StreaksResult(
        rows=rows,
        close_transactions=len(trades),
        neutral_transactions=neutral_transactions,
        basis="aggregated_close_transaction",
    )


@dataclass
class _StressGroup:
    """压力分组的累加器；仅在函数内部使用。"""

    contracts: Decimal = Decimal(0)
    premium_at_risk: Decimal = Decimal(0)
    nearest_expiry: str | None = None
    farthest_expiry: str | None = None


def calculate_option_stress(
    lots: tuple[Lot, ...],
    account_scope: AccountScope,
) -> OptionStressResult:
    """模拟当前全部长期权权利金归零的历史成本压力。

    @param lots: FIFO 未平批次。
    @param account_scope: 显式账户范围。
    @returns 按标的的最大已支付权利金损失，不含市场数据。
    """
    positions = [
        position for position in aggregate_positions(lots, account_scope)
        if position.asset_type == "option" and position.direction == "long"
    ]
    groups: dict[str | None, _StressGroup] = {}

    for position in positions:
        group = groups.setdefault(position.underlying, _StressGroup())
        group.contracts += position.qty
        group.premium_at_risk += position.cost * OPTION_MULTIPLIER
        if position.expiry is not None:
            if group.nearest_expiry is None or position.expiry < group.nearest_expiry:
                group.nearest_expiry = position.expiry
            if group.farthest_expiry is None or position.expiry > group.farthest_expiry:
                group.farthest_expiry = position.expiry

    ordered = sorted(groups.items(), key=lambda item: (-item[1].premium_at_risk, item[0] or ""))
    total = sum((group.premium_at_risk for _, group in ordered), Decimal(0)).quantize(
        _PREMIUM_QUANTUM, rounding=ROUND_HALF_EVEN
    )
    top_three = sum(
        (group.premium_at_risk for _, group in ordered[:3]), Decimal(0)
    ).quantize(_PREMIUM_QUANTUM, rounding=ROUND_HALF_EVEN)
    total_contracts = sum((group.contracts for _, group in ordered), Decimal(0))

    rows = tuple(
        OptionStressRow(
            underlying=underlying,
            contracts=format(group.contracts.quantize(_CONTRACT_QUANTUM), "f"),
            premium_at_risk=format(premium, "f"),
            nearest_expiry=group.nearest_expiry,
            farthest_expiry=group.farthest_expiry,
            loss_if_zero=format(-premium, "f"),
            portfolio_premium_pct=_share_pct(premium, total),
        )
        for underlying, group in ordered
        for premium in [
            group.premium_at_risk.quantize(_PREMIUM_QUANTUM, rounding=ROUND_HALF_EVEN)
        ]
    )

    return OptionStressResult(
        rows=rows,
        long_option_contracts=format(total_contracts.quantize(_CONTRACT_QUANTUM), "f"),
        all_long_options_loss_if_zero=format(-total, "f"),
        top_three_loss_if_zero=format(-top_three, "f"),
        basis="maximum_paid_premium_loss_no_market_data",
    )


def _with_ranking(
    trade: ClosedTransaction, side: Literal["best", "worst"], rank: int
) -> ExtremeTradeRow:
    """把平仓交易与排名组合为一条极值明细。"""
    return ExtremeTradeRow(
        close_txn=trade.close_txn,
        account=trade.account,
        close_date=trade.close_date,
        symbol_key=trade.symbol_key,
        underlying=trade.underlying,
        asset_type=trade.asset_type,
        direction=trade.direction,
        close_action=trade.close_action,
        qty=trade.qty,
        pnl=trade.pnl,
        fees=trade.fees,
        earliest_open_date=trade.earliest_open_date,
        latest_open_date=trade.latest_open_date,
        side=side,
        rank=rank,
    )


def _holding_bucket(days: int) -> str:
    """将持有天数映射到固定区间。"""
    if days <= 7:
        return "0-7d"
    if days <= 30:
        return "8-30d"
    if days <= 90:
        return "31-90d"
    return "90d+"


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


def _share_pct(part: Decimal, total: Decimal) -> str | None:
    """计算占比；合计为零时返回 ``None``。"""
    if total == 0:
        return None
    return percent_text(part * 100 / total)


def _amount_text(value: Decimal) -> str:
    """按四位小数渲染金额。

    委托给共享实现——**此前这里是第二份独立实现，且舍入模式与参考实现不符**
    （用了银行家舍入，而 ``toFixed`` 是四舍五入）。
    """
    return fixed_text(value, 4)

"""持仓聚合与到期分析。

镜像 TypeScript 版 ``positions.ts``。聚合键是「账户 + 完整合约 + 方向」：
不同方向的同一合约必须分成两行，否则多空的成本会被错误抵消。
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from ..fifo.types import Lot
from .dates import (
    days_between,
    format_iso_date,
    is_account_in_scope,
    parse_iso_date,
    shift_days,
)
from .types import (
    AccountScope,
    ExpiringPositionRow,
    ExpiringPositionsResult,
    PositionRow,
)

# 期权一张合约对应 100 股；仅用于把权利金折算成美元金额。
OPTION_MULTIPLIER = 100


def aggregate_positions(
    lots: tuple[Lot, ...],
    account_scope: AccountScope,
    underlying: str | None = None,
) -> tuple[PositionRow, ...]:
    """按账户、完整合约与方向聚合当前未平 FIFO 批次。

    @param lots: FIFO 重放得到的未平批次。
    @param account_scope: 必填账户范围；``all`` 仍保留每个账户的独立聚合行。
    @param underlying: 可选标的过滤，大小写不敏感。
    @returns 与 ``v_positions`` 同口径的稳定排序结果。
    """
    normalized_underlying = underlying.upper() if underlying is not None else None
    groups: dict[tuple[object, ...], PositionRow] = {}

    for lot in lots:
        if lot.remaining_quantity <= 0 or not is_account_in_scope(lot.account, account_scope):
            continue
        if normalized_underlying is not None and lot.underlying != normalized_underlying:
            continue

        key = (
            lot.account,
            lot.symbol_key,
            lot.asset_type,
            lot.underlying,
            lot.expiry,
            None if lot.strike is None else str(lot.strike),
            lot.option_type,
            lot.direction,
        )
        existing = groups.get(key)
        contribution = lot.remaining_quantity * lot.open_price
        if existing is None:
            groups[key] = PositionRow(
                account=lot.account,
                symbol_key=lot.symbol_key,
                asset_type=lot.asset_type,
                underlying=lot.underlying,
                expiry=lot.expiry,
                strike=lot.strike,
                option_type=lot.option_type,
                direction=lot.direction,
                qty=lot.remaining_quantity,
                cost=contribution,
                first_open_date=lot.open_date,
            )
            continue

        existing.qty += lot.remaining_quantity
        existing.cost += contribution
        existing.first_open_date = min(existing.first_open_date, lot.open_date)

    return tuple(sorted(groups.values(), key=_position_order))


def analyze_expiring_positions(
    positions: tuple[PositionRow, ...],
    as_of: str,
    days: int,
) -> ExpiringPositionsResult:
    """选出截止窗口内的未平期权并标记已过期、今日到期或即将到期。

    @param positions: 已由 :func:`aggregate_positions` 聚合的持仓。
    @param as_of: ISO 基准日。
    @param days: 向未来观察的天数，不能为负。
    @returns 到期明细、截止日期与各状态计数。
    @raises ValueError: 日期非法或天数为负。
    """
    analysis_date = parse_iso_date(as_of)
    if not isinstance(days, int) or isinstance(days, bool) or days < 0:
        raise ValueError("days 必须是非负整数")
    cutoff = format_iso_date(shift_days(analysis_date, days))

    rows: list[ExpiringPositionRow] = []
    for position in positions:
        if position.asset_type != "option" or position.expiry is None:
            continue
        if position.expiry > cutoff:
            continue
        days_remaining = days_between(parse_iso_date(position.expiry), analysis_date)
        if days_remaining < 0:
            status = "expired"
        elif days_remaining == 0:
            status = "expires_today"
        else:
            status = "upcoming"
        rows.append(ExpiringPositionRow(
            account=position.account,
            symbol_key=position.symbol_key,
            underlying=position.underlying,
            direction=position.direction,
            qty=position.qty,
            cost=position.cost,
            expiry=position.expiry,
            strike=position.strike,
            option_type=position.option_type,
            first_open_date=position.first_open_date,
            status=status,
            days_remaining=days_remaining,
            premium_dollars=(position.cost * OPTION_MULTIPLIER).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_EVEN
            ),
        ))

    rows.sort(key=_expiring_order)
    return ExpiringPositionsResult(
        rows=tuple(rows),
        count=len(rows),
        as_of=as_of,
        cutoff=cutoff,
        expired=sum(1 for row in rows if row.status == "expired"),
        expires_today=sum(1 for row in rows if row.status == "expires_today"),
        upcoming=sum(1 for row in rows if row.status == "upcoming"),
    )


def _position_order(position: PositionRow) -> tuple[str, str, str, str, str, str]:
    """保持资产、标的、到期日顺序，并补充账户与合约作为稳定最终键。"""
    return (
        position.asset_type,
        position.underlying or "",
        position.expiry or "",
        position.account,
        position.symbol_key or "",
        position.direction,
    )


def _expiring_order(
    row: ExpiringPositionRow,
) -> tuple[str, str, tuple[int, Decimal], str]:
    """按到期日、标的、行权价、方向排序。

    空行权价必须排在**最前**，与现有实现的 ``compareNullableDecimal`` 一致；
    因此用 ``(0, 0)`` 作为空值哨兵，非空值用 ``(1, 实际值)`` 参与比较。
    """
    strike_key: tuple[int, Decimal] = (
        (0, Decimal(0)) if row.strike is None else (1, row.strike)
    )
    return (
        row.expiry or "",
        row.underlying or "",
        strike_key,
        row.direction,
    )

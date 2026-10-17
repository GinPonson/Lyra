"""持仓的参考估值与按币种汇总。

镜像 TypeScript 版 ``application/market-position-view.ts`` 的金额口径部分。

**金额口径是「参考价 × 数量 × 乘数 × 方向，不含费用」**，不是市值、不是净值。

三条不可放宽的性质：

1. **方向决定符号。** 空头持仓的市值是负的——把空头当多头会让组合总额凭空翻正。
2. **期权乘数是 100，正股是 1。** 一张期权合约代表 100 股；忘掉乘数会让期权估值
   差 100 倍。
3. **不完整时组合计为 ``None``，而不是"能算的那几项之和"。** 部分求和会**把组合总额
   少报一个未知量**，而用户看到的是一个看起来正常的数字。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Literal

from ..numeric import fixed_point_text

# 与 TypeScript 侧一致的精度：估值含乘法，精度不足会引入舍入误差。
_PRECISION = 64

Direction = Literal["long", "short"]
AssetType = Literal["stock", "option"]


@dataclass(frozen=True)
class PositionValuation:
    """一行持仓的参考估值。"""

    market_value: str | None
    unrealized_pnl: str | None
    multiplier: str | None
    basis: Literal["reference_price_excluding_fees"] = "reference_price_excluding_fees"


def multiplier_for(asset_type: AssetType) -> str:
    """期权乘数 100，正股 1。"""
    return "100" if asset_type == "option" else "1"


def value_position(
    *,
    quantity: str,
    cost: str,
    reference_price: str | None,
    asset_type: AssetType,
    direction: Direction,
) -> PositionValuation:
    """计算一行持仓的参考估值。

    @param quantity: 持仓数量。
    @param cost: 历史成本（不含费用）。
    @param reference_price: 参考价；``None`` 表示没有可用价格。
    @param asset_type: 资产类型，决定乘数。
    @param direction: 持仓方向，决定符号。
    @returns 估值；无参考价时两个金额字段为 ``None``。
    """
    multiplier = multiplier_for(asset_type)
    if reference_price is None:
        return PositionValuation(
            market_value=None, unrealized_pnl=None, multiplier=multiplier
        )

    sign = Decimal(1) if direction == "long" else Decimal(-1)
    # 空头持仓的市值是负的：把空头当多头会让组合总额凭空翻正。
    with localcontext() as context:
        context.prec = _PRECISION
        quoted_units = Decimal(quantity) * Decimal(reference_price)
        factor = Decimal(multiplier) * sign
        market_value = quoted_units * factor
        unrealized = (quoted_units - Decimal(cost)) * factor
    return PositionValuation(
        # 参考实现是 ``.toFixed()``（定点），不是 ``toString()``（极小量级会变指数形式）。
        market_value=fixed_point_text(market_value),
        unrealized_pnl=fixed_point_text(unrealized),
        multiplier=multiplier,
    )


@dataclass(frozen=True)
class CurrencyGroupTotal:
    """一个币种分组的合计。"""

    positions: int
    priced_positions: int
    complete: bool
    market_value: str | None
    unrealized_pnl: str | None
    as_of_from: str | None
    as_of_to: str | None


def aggregate_currency_group(rows: list[dict]) -> CurrencyGroupTotal:
    """汇总一个币种分组。

    每行需含 ``valuation``（``market_value`` / ``unrealized_pnl``）、``available``
    （报价是否为新鲜可用）与 ``as_of``（参考价时点）。

    **只有全部行都"新鲜且有估值"时才给出合计**；否则合计为 ``None``。
    部分求和会把组合总额少报一个未知量，而用户看到的是一个看起来正常的数字。

    @param rows: 该分组的持仓行。
    @returns 分组合计。
    """
    priced = [row for row in rows if row["valuation"].market_value is not None]
    complete = all(row["available"] and row["valuation"].market_value is not None for row in rows)

    dates = sorted(row["as_of"] for row in priced if row["as_of"] is not None)
    return CurrencyGroupTotal(
        positions=len(rows),
        priced_positions=len(priced),
        complete=complete,
        market_value=_sum(priced, "market_value") if complete else None,
        unrealized_pnl=_sum(priced, "unrealized_pnl") if complete else None,
        as_of_from=dates[0] if dates else None,
        as_of_to=dates[-1] if dates else None,
    )


def _sum(rows: list[dict], field: str) -> str:
    """对估值字段求和。"""
    with localcontext() as context:
        context.prec = _PRECISION
        total = Decimal(0)
        for row in rows:
            value = getattr(row["valuation"], field)
            if value is not None:
                total += Decimal(value)
    return fixed_point_text(total)

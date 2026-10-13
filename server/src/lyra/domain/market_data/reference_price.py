"""参考价的选取。

镜像 TypeScript 版 ``domain/market-data/reference-price.ts``。

**买卖中间价有三个前提，缺一不可**：买卖双方都存在、**时点完全相同**、且 ``ask ≥ bid``。

时点相同这条最关键：用 10:00 的买价配 10:05 的卖价算出的"中间价"，
**是市场上从未存在过的价格**——它看起来完全合理，会被写进估值并展示给用户。

``ask < bid``（交叉市场）说明数据有问题，不能用来算中间价。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Literal

from .types import MarketQuote

ReferenceBasis = Literal["mid", "last", "close"]

# 与 TypeScript 侧一致的精度：中间价是除法结果，精度不足会引入舍入误差。
_PRICE_PRECISION = 64


@dataclass(frozen=True)
class MarketReferencePrice:
    """一项用于估值的参考价及其口径。"""

    value: str
    as_of: str
    basis: ReferenceBasis


def select_market_reference_price(quote: MarketQuote) -> MarketReferencePrice | None:
    """选取参考价。

    优先中间价，其次 ``last``，最后 ``close``；都不可用时返回 ``None``
    （**不猜**，让调用方明确处理"没有参考价"）。

    @param quote: 行情报价。
    @returns 参考价；无可用价格时 ``None``。
    """
    bid, ask = quote.bid, quote.ask
    if (
        bid is not None
        and ask is not None
        and bid.as_of == ask.as_of
        and _greater_than_zero(ask.value)
        and _greater_than_zero(bid.value)
        and Decimal(ask.value) >= Decimal(bid.value)
    ):
        with localcontext() as context:
            context.prec = _PRICE_PRECISION
            mid = (Decimal(bid.value) + Decimal(ask.value)) / 2
        return MarketReferencePrice(
            value=_decimal_text(mid), as_of=bid.as_of, basis="mid"
        )

    for basis, point in (("last", quote.last), ("close", quote.close)):
        if point is not None and _greater_than_zero(point.value):
            return MarketReferencePrice(value=point.value, as_of=point.as_of, basis=basis)  # type: ignore[arg-type]
    return None


def is_crossed_market(quote: MarketQuote) -> bool:
    """判断是否为交叉市场（``ask < bid``，且时点相同）。

    时点不同时不判定：那不是"交叉"，只是两个不同时刻的报价。
    """
    bid, ask = quote.bid, quote.ask
    if bid is None or ask is None or bid.as_of != ask.as_of:
        return False
    return Decimal(ask.value) < Decimal(bid.value)


def _greater_than_zero(value: str) -> bool:
    """判断十进制文本是否大于零。

    无法解析的文本视为不满足——**不猜**。
    """
    try:
        return Decimal(value) > 0
    except Exception:  # noqa: BLE001 - 非法文本不构成可用价格
        return False


def _decimal_text(value: Decimal) -> str:
    """渲染为无指数、去尾随零的十进制文本。"""
    from ..numeric import decimal_text

    return decimal_text(value)

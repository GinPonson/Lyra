"""正股最新成交与上一交易日收盘摘要类型。

摘要只描述一个公开股票身份，不包含用户、账户或持仓数据。最新成交与前收盘分开
建模，避免把对比基准混入现有 quote/估值语义。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .types import MarketPricePoint


@dataclass(frozen=True)
class MarketPreviousClose:
    """一项带纽约交易日期的拆股复权前收盘价。

    @param value: 正的定点十进制收盘价文本。
    @param session_date: 纽约民用日期 ``YYYY-MM-DD``。
    """

    value: str
    session_date: str


@dataclass(frozen=True)
class MarketStockSummary:
    """单一来源对正股成交与前收盘比较的结果。

    `available` 表示取得了合法最新成交价；前收盘缺失时仍保留成交价，并用
    ``change_issue`` 明确说明涨跌幅不可计算。

    @param status: 是否取得合法最新成交价。
    @param price: 最新实际成交价；可用结果必须非空。
    @param previous_close: 上一实际有成交日的拆股复权收盘价。
    @param change_percent: 相对前收盘涨跌百分比定点文本。
    @param change_issue: 仅影响涨跌幅的稳定原因码。
    @param issue: 整个成交摘要不可用时的稳定原因码。
    @param feed: 最新成交使用的供应商 feed 标识；Alpaca 快照为 ``delayed_sip``。
    @param fetched_at: 本次供应商读取开始时间；缓存命中保留该原始时间。
    """

    status: Literal["available", "unavailable"]
    price: MarketPricePoint | None = None
    previous_close: MarketPreviousClose | None = None
    change_percent: str | None = None
    change_issue: str | None = None
    issue: str | None = None
    feed: str | None = None
    fetched_at: str | None = None

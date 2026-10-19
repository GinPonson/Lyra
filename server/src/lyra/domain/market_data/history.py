"""历史行情领域类型。

历史 bar 的时间戳表示区间起点；价格使用定点十进制文本，成交量保持整数，避免
客户端把浮点噪声或缺失成交误当成真实行情。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MarketHistoryRange = Literal["1d", "1w", "1m", "3m"]
MarketHistoryTimeframe = Literal["15Min", "1Hour", "1Day"]


@dataclass(frozen=True)
class MarketHistoryBar:
    """一根已完成且有成交的历史 K 线。

    @param timestamp: bar 区间起点的 UTC ISO 时间。
    @param open: 开盘价定点十进制文本。
    @param high: 最高价定点十进制文本。
    @param low: 最低价定点十进制文本。
    @param close: 收盘价定点十进制文本。
    @param volume: 非负整数成交量；零成交 bar 不进入该类型。
    """

    timestamp: str
    open: str
    high: str
    low: str
    close: str
    volume: int


@dataclass(frozen=True)
class MarketHistoryResult:
    """单一行情来源的历史结果，不含用户、账户或持仓信息。"""

    status: Literal["available", "unavailable"]
    bars: tuple[MarketHistoryBar, ...] = ()
    issue: str | None = None
    feed: str | None = None
    fetched_at: str | None = None
    cutoff: str | None = None

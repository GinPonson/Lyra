"""FIFO 领域类型。

镜像 TypeScript 版 ``fifo/types.ts``。``Lot`` 刻意可变：重放过程中剩余数量与
已分摊费用会随每笔平仓变化，把它做成不可变对象只会带来无意义的复制。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from ..ledger.types import LocalDate, PositionAction

LotDirection = Literal["long", "short"]
FifoAssetType = Literal["stock", "option"]


class FifoError(Exception):
    """FIFO 重放失败。

    与 TypeScript 版一致，任何顺序不可证明、超卖、无持仓可平或动作不受支持
    都立即抛出，**不返回部分结果**。
    """


@dataclass
class Lot:
    """一个尚未完全平仓的 FIFO 批次。"""

    account: str
    symbol_key: str | None
    asset_type: FifoAssetType
    underlying: str | None
    expiry: LocalDate | None
    strike: Decimal | None
    option_type: Literal["C", "P"] | None
    direction: LotDirection
    open_date: LocalDate
    open_price: Decimal
    open_quantity: Decimal
    remaining_quantity: Decimal
    open_fees: Decimal
    open_transaction: str
    # 已进入 realized 的开仓费用，只用于保证分摊尾差对齐。
    fees_allocated: Decimal


@dataclass(frozen=True)
class RealizedRecord:
    """一次平仓对某个开仓批次形成的已实现结果。"""

    account: str
    symbol_key: str | None
    asset_type: FifoAssetType
    underlying: str | None
    direction: LotDirection
    open_date: LocalDate
    close_date: LocalDate
    quantity: Decimal
    open_price: Decimal
    close_price: Decimal
    open_fees: Decimal
    close_fees: Decimal
    pnl: Decimal
    close_action: PositionAction
    open_transaction: str
    close_transaction: str


@dataclass(frozen=True)
class FifoReplayResult:
    """纯领域 FIFO 重放的完整结果；数据库层负责在事务中原子替换旧产物。"""

    transactions_replayed: int
    open_lots: tuple[Lot, ...]
    realized_records: tuple[RealizedRecord, ...]
    total_realized_pnl: str
    warnings: tuple[str, ...]

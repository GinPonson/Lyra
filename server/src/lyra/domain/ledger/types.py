"""账本领域类型。

镜像 TypeScript 版 ``types.ts``。日期一律用不带时区的 ISO 文本
（``YYYY-MM-DD``）表示，避免时区把同一笔交易挪到不同交易日。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

# 不带时区的 ISO 日历日期，形如 ``2026-07-28``。
LocalDate = str

AssetType = Literal["stock", "option", "cash"]
OptionType = Literal["C", "P"]
LotDirection = Literal["long", "short"]
PositionAction = Literal[
    "Buy",
    "Sell",
    "Buy to Open",
    "Sell to Open",
    "Buy to Close",
    "Sell to Close",
    "Expired",
    "Assigned",
]
CashAction = Literal[
    "MoneyLink Transfer",
    "Credit Interest",
    "Qualified Dividend",
    "Non-Qualified Div",
    "Pr Yr Non-Qual Div",
    "NRA Tax Adj",
    "Pr Yr NRA Tax",
    "Foreign Tax Paid",
]
TransactionAction = PositionAction | CashAction
# 账本来源类型。**是 "excel" 而不是 "xlsx"**——schema 的 CHECK 约束写的是
# ('csv', 'excel', 'gmail')。写成 "xlsx" 会在写入时撞约束，而报错发生在数据库层，
# 与"为什么这里叫 xlsx"这个直觉性的假设相去甚远。
#
# 注意与 `ImportFileType`（上传文件类型，取扩展名 "xlsx"）区分：两者同名不同义。
LedgerSourceType = Literal["csv", "excel", "gmail"]


@dataclass(frozen=True)
class RawTransactionFields:
    """Schwab 导出的八个原始文本字段。

    这里用 Python 命名而不是照搬 CSV 表头（``Fees & Comm`` 不是合法标识符）；
    表头到字段的映射只发生在 CSV 适配器里，领域层因此不必携带导出的列名。
    """

    date: str
    action: str
    symbol: str
    description: str
    quantity: str
    price: str
    fees: str
    amount: str


@dataclass(frozen=True)
class ParsedDateField:
    """Date 列解析结果：记录日期与交易生效日期。"""

    record_date: LocalDate
    transaction_date: LocalDate


@dataclass(frozen=True)
class ParsedOptionSymbol:
    """Schwab 文本期权代码解析结果。"""

    underlying: str
    expiry: LocalDate
    strike: Decimal
    option_type: OptionType


@dataclass(frozen=True)
class ParsedTransaction:
    """标准化后的交易；字段顺序与 TypeScript 版保持一致以便对照。"""

    account: str
    source_type: LedgerSourceType
    source_reference: str
    record_date: LocalDate
    transaction_date: LocalDate
    action: TransactionAction
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
    sequence: int
    occurrence: int
    business_hash: str
    transaction_hash: str

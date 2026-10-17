"""Schwab Transactions 表格的共享规则。

CSV 与 XLSX 是同一张表的两种容器，**表头与列序规则必须完全一致**——因此这里是唯一的
定义处。两条路径各自解析容器格式，然后共用本模块做校验与映射。

镜像 TypeScript 版 ``infrastructure/imports/schwab-table.ts``。

**列序是位置契约**：Schwab 的导出没有列名索引，第 7 列就是 ``Fees & Comm``。因此
表头必须**逐字符相等**，而不是"包含这些列"——顺序不同的两列会被映射到错误字段，
而金额看起来仍然正常。
"""

from __future__ import annotations

from ...domain.ledger.errors import IngestError
from ...domain.ledger.types import RawTransactionFields

# Schwab Transactions 导出的唯一受支持列集合及物理顺序。
SCHWAB_TRANSACTION_HEADERS: tuple[str, ...] = (
    "Date",
    "Action",
    "Symbol",
    "Description",
    "Quantity",
    "Price",
    "Fees & Comm",
    "Amount",
)

# 行必须恰好八列；少一列会让后面的列整体前移。
FIELD_COUNT = len(SCHWAB_TRANSACTION_HEADERS)


def is_schwab_transaction_headers(headers: object) -> bool:
    """判断是否为标准的交易表头。

    @param headers: 候选表头。
    @returns 是否逐项相等。
    """
    if not isinstance(headers, (list, tuple)):
        return False
    return tuple(headers) == SCHWAB_TRANSACTION_HEADERS


def assert_schwab_transaction_headers(headers: object, where: str) -> None:
    """校验表头。

    @param headers: 候选表头。
    @param where: 错误定位说明。
    @raises IngestError: 表头不符（错误里同时给出期望与实际）。
    """
    if not is_schwab_transaction_headers(headers):
        actual = list(headers) if isinstance(headers, (list, tuple)) else headers
        raise IngestError(
            f"{where}: 表头不符,期望 {list(SCHWAB_TRANSACTION_HEADERS)},实际 {actual}"
        )


def map_schwab_transaction_row(values: tuple[str, ...], where: str) -> RawTransactionFields:
    """按位置把一行映射为领域字段。

    @param values: 一行单元格的文本。
    @param where: 错误定位说明。
    @returns 领域字段。
    @raises IngestError: 列数不是八。
    """
    if len(values) != FIELD_COUNT:
        raise IngestError(f"{where}: 必须包含八个文本字段")
    return RawTransactionFields(
        date=values[0],
        action=values[1],
        symbol=values[2],
        description=values[3],
        quantity=values[4],
        price=values[5],
        fees=values[6],
        amount=values[7],
    )


def create_schwab_unknown_headers_error(worksheets: tuple[str, ...]) -> IngestError:
    """构造"工作簿里没有交易表"的错误。

    **列出实际的工作表名**：用户需要知道脚本翻了哪些地方才没找到，否则他只能猜自己
    是不是选错了文件。

    @param worksheets: 工作簿里的工作表名。
    @returns 可直接抛出的错误。
    """
    return IngestError(
        f"工作簿中没有 Schwab Transactions 表,已检查: {list(worksheets)}"
    )

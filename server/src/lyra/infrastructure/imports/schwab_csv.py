"""Schwab Transactions CSV 适配器。

镜像 TypeScript 版 ``schwab-csv.ts`` / ``schwab-table.ts`` 的语法层规则：
只接受 UTF-8、唯一受支持的八列表头与物理顺序、每行恰好八列且全部为文本。
上游格式一旦变化必须明确失败，而不是把金额等字段静默映射到错误列。
"""

from __future__ import annotations

import csv
import io

from ...domain.ledger.batch import TransactionBatchInput
from ...domain.ledger.errors import IngestError
from ...domain.ledger.parsers import parse_account
from ...domain.ledger.types import LedgerSourceType, RawTransactionFields

# Schwab Transactions 导出的唯一受支持列集合及物理顺序。
# 表头与列序规则由 schwab_table 唯一定义；这里再导出一次以保持既有导入路径可用。
# 表头与列序规则由 schwab_table 唯一定义；这里再导出一次以保持既有导入路径可用。
# **必须配 `__all__`**：只写导入而不声明导出，linter 会把它当成未使用导入删掉——
# 删除后导入路径静默失效，而**只有调用方才会报错**。
from .schwab_table import (
    SCHWAB_TRANSACTION_HEADERS,
    assert_schwab_transaction_headers,
    map_schwab_transaction_row,
)

__all__ = [
    "SCHWAB_TRANSACTION_HEADERS",
    "assert_schwab_transaction_headers",
    "map_schwab_transaction_row",
    "parse_schwab_csv",
]


def parse_schwab_csv(
    content: bytes,
    *,
    file_name: str,
    source_hash: str,
    source_reference: str,
    source_type: LedgerSourceType = "csv",
) -> TransactionBatchInput:
    """严格解析 Schwab Transactions CSV，并转换为统一批次输入。

    @param content: 原始文件字节；必须为 UTF-8（允许 BOM）。
    @param file_name: 仅文件名，用于提取账户标识与错误定位。
    @param source_hash: 文件内容摘要，用于审计与去重。
    @param source_reference: 来源引用；不能为空。
    @param source_type: 来源类型，默认 ``csv``。
    @returns 可直接交给 :func:`normalize_transaction_batch` 的批次输入。
    @raises IngestError: 编码非法、表头不符或行字段数错误。
    """
    try:
        # utf-8-sig 会剥离开头的 BOM；非法字节直接失败而不是替换成 U+FFFD，
        # 否则损坏的金额会以看起来正常的形式进入账本。
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise IngestError(f"{file_name}: 文件不是有效的 UTF-8 编码") from error

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        records = [row for row in reader if row != []]
    except csv.Error as error:
        raise IngestError(f"{file_name}: CSV 语法无效: {error}") from error

    if not records:
        # 空文件也走表头校验，让错误信息与"表头不符"保持一致。
        assert_schwab_transaction_headers([], file_name)

    assert_schwab_transaction_headers(records[0], file_name)

    rows: list[RawTransactionFields] = [
        map_schwab_transaction_row(
            tuple(values), f"{file_name} 第{index}行"
        )
        for index, values in enumerate(records[1:], start=1)
    ]

    return TransactionBatchInput(
        file_name=file_name,
        account=parse_account(file_name),
        source_hash=source_hash,
        source_type=source_type,
        source_reference=source_reference,
        rows=tuple(rows),
    )

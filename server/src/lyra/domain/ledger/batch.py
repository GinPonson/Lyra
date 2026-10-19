"""批次标准化：为一个文件内的全部行生成标准交易。

镜像 TypeScript 版 ``batch.ts``。只在**所有行都成功**后返回结果；任何一行抛错
都会直接终止调用，不返回部分交易——半导入状态比拒绝整份文件更难排查。

完全相同的业务交易按文件出现顺序编号，使两笔真实重复交易获得不同的
``transaction_hash``，同时保持跨快照导入时可比较。
"""

from __future__ import annotations

from dataclasses import dataclass

from .transactions import (
    ParseTransactionContext,
    parse_transaction_row,
    with_occurrence,
)
from .types import LedgerSourceType, ParsedTransaction, RawTransactionFields


@dataclass(frozen=True)
class TransactionBatchInput:
    """已完成文件语法解析、待领域标准化的一批行。"""

    file_name: str
    account: str
    source_hash: str
    source_type: LedgerSourceType
    source_reference: str
    rows: tuple[RawTransactionFields, ...]


@dataclass(frozen=True)
class TransactionBatchPreview:
    """标准化结果：全部交易、动作统计与文件元数据。"""

    file_name: str
    account: str
    source_hash: str
    total_rows: int
    by_action: dict[str, int]
    transactions: tuple[ParsedTransaction, ...]


def normalize_transaction_batch(batch: TransactionBatchInput) -> TransactionBatchPreview:
    """标准化一个文件内的全部交易行。

    @param batch: 文件名、账户、来源信息与已完成 CSV/Excel 语法解析的行。
    @returns 全部标准交易、动作统计和文件元数据。
    @raises IngestError: 透传具体行的领域校验错误。
    """
    occurrence_by_business_hash: dict[str, int] = {}
    by_action: dict[str, int] = {}
    transactions: list[ParsedTransaction] = []

    for index, fields in enumerate(batch.rows):
        sequence = index + 1
        parsed = parse_transaction_row(
            fields,
            ParseTransactionContext(
                account=batch.account,
                source_hash=batch.source_hash,
                source_type=batch.source_type,
                source_reference=batch.source_reference,
                sequence=sequence,
                where=f"{batch.file_name} 第{sequence}行",
            ),
        )

        occurrence = occurrence_by_business_hash.get(parsed.business_hash, 0) + 1
        occurrence_by_business_hash[parsed.business_hash] = occurrence
        transaction = with_occurrence(parsed, occurrence)

        transactions.append(transaction)
        by_action[transaction.action] = by_action.get(transaction.action, 0) + 1

    return TransactionBatchPreview(
        file_name=batch.file_name,
        account=batch.account,
        source_hash=batch.source_hash,
        total_rows=len(transactions),
        by_action=by_action,
        transactions=tuple(transactions),
    )

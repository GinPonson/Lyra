"""导入预览与提交快照校验。

镜像 TypeScript 版 ``import-preview.ts``。

导入是**两阶段**的：先预览（告诉用户会插入多少、跳过多少），用户确认后再提交。
两阶段之间存在时间差，期间账本可能被别的导入改动，因此预览必须携带一份
**指纹**，提交时逐项复核——任一绑定条件变化都要求重新预览。

预览绑定的条件有四项：文件哈希、解析器版本、账户、基准账本版本。
**解析器版本也参与绑定**：标准化规则变了，旧预览里的行就不再代表同一批交易。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..domain.ledger.batch import TransactionBatchInput, normalize_transaction_batch
from ..domain.ledger.types import ParsedTransaction
from .app_error import ApplicationError


@dataclass(frozen=True)
class CreateImportPreviewInput:
    """生成预览所需的输入。"""

    batch: TransactionBatchInput
    # 标准化规则版本；规则变化后旧预览必须失效。
    parser_version: str
    # 预览时观察到的最新账本版本；空账本使用 0。
    base_ledger_version: int
    # 当前目标账户中已经存在的规范交易哈希。
    existing_transaction_hashes: frozenset[str]


@dataclass(frozen=True)
class ImportPreviewResult:
    """可安全返回给导入预览 API 的完整应用层结果。"""

    preview_fingerprint: str
    file_name: str
    file_hash: str
    parser_version: str
    account: str
    base_ledger_version: int
    total_rows: int
    insertable_rows: int
    existing_rows: int
    by_action: dict[str, int]
    first_transaction_date: str | None
    last_transaction_date: str | None
    # 提交阶段复用的完整标准交易；HTTP 序列化层不直接暴露该字段。
    transactions: tuple[ParsedTransaction, ...]


@dataclass(frozen=True)
class ImportCommitSnapshot:
    """提交时重新读取的不可变条件。"""

    file_hash: str
    parser_version: str
    account: str
    ledger_version: int


def create_import_preview(input: CreateImportPreviewInput) -> ImportPreviewResult:
    """全量校验批次并生成绑定当前账本快照的导入预览。

    @param input: 已由 CSV/Excel 适配器提取的原始行及当前账本哈希集合。
    @returns 含预计新增/跳过数、日期范围与提交指纹的预览。
    @raises IngestError: 任一交易行不符合标准化规则（透传领域层错误）。
    @raises ValueError: 账本版本或解析器版本非法。
    """
    if (
        not isinstance(input.base_ledger_version, int)
        or isinstance(input.base_ledger_version, bool)
        or input.base_ledger_version < 0
    ):
        raise ValueError("baseLedgerVersion 必须是非负安全整数")
    if input.parser_version.strip() == "":
        raise ValueError("parserVersion 不能为空")

    normalized = normalize_transaction_batch(input.batch)
    existing_rows = sum(
        1
        for transaction in normalized.transactions
        if transaction.transaction_hash in input.existing_transaction_hashes
    )
    dates = sorted(transaction.transaction_date for transaction in normalized.transactions)
    fingerprint = _preview_fingerprint(
        file_hash=input.batch.source_hash,
        parser_version=input.parser_version,
        account=input.batch.account,
        base_ledger_version=input.base_ledger_version,
        transaction_hashes=[t.transaction_hash for t in normalized.transactions],
    )

    return ImportPreviewResult(
        preview_fingerprint=fingerprint,
        file_name=normalized.file_name,
        file_hash=normalized.source_hash,
        parser_version=input.parser_version,
        account=normalized.account,
        base_ledger_version=input.base_ledger_version,
        total_rows=normalized.total_rows,
        insertable_rows=normalized.total_rows - existing_rows,
        existing_rows=existing_rows,
        by_action=dict(normalized.by_action),
        first_transaction_date=dates[0] if dates else None,
        last_transaction_date=dates[-1] if dates else None,
        transactions=normalized.transactions,
    )


def assert_import_preview_current(
    preview: ImportPreviewResult,
    current: ImportCommitSnapshot,
) -> None:
    """在进入数据库事务前确认提交条件仍与预览完全一致。

    **一次性列出全部变化的字段**，而不是遇到第一个就返回：用户需要知道到底哪些
    条件变了，只报一个会让他在重新预览时反复撞上新的冲突。

    @param preview: 用户确认的既有预览。
    @param current: 提交瞬间重新读取的文件、解析器、账户与账本版本。
    @raises ApplicationError: 任一绑定条件变化（409），调用方必须要求重新预览。
    """
    changed: list[str] = []
    if preview.file_hash != current.file_hash:
        changed.append("file_hash")
    if preview.parser_version != current.parser_version:
        changed.append("parser_version")
    if preview.account != current.account:
        changed.append("account")
    if preview.base_ledger_version != current.ledger_version:
        changed.append("ledger_version")
    if changed:
        raise ApplicationError(
            code="IMPORT_PREVIEW_STALE",
            message="导入预览已失效，必须重新预览",
            status_code=409,
            retryable=False,
            details={"changed_fields": changed},
        )


def _preview_fingerprint(
    *,
    file_hash: str,
    parser_version: str,
    account: str,
    base_ledger_version: int,
    transaction_hashes: list[str],
) -> str:
    """计算预览指纹。

    键顺序固定且无多余空白，与 JavaScript ``JSON.stringify`` 同形——指纹是内部
    一致性凭据，格式漂移会让同一份预览在两处算出不同值。

    @returns SHA-256 十六进制文本。
    """
    payload: dict[str, Any] = {
        "fileHash": file_hash,
        "parserVersion": parser_version,
        "account": account,
        "baseLedgerVersion": base_ledger_version,
        "transactionHashes": transaction_hashes,
    }
    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

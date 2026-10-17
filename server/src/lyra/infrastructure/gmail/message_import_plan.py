"""Gmail 消息的导入计划。

镜像 TypeScript 版 ``infrastructure/gmail/message-import-plan.ts``。

决策顺序（**顺序本身就是正确性的一部分**）：

```
1. 已有记录且内容或账户归属变了  → 冲突（409，带 changed_fields）
2. 已有记录且已导入              → 跳过（already_imported）
3. 其余                          → 就绪（ready）
```

**冲突检查必须在"已导入"之前。** 一封已导入的邮件若内容变了，先报"已导入"会让变更
被静默忽略——而用户以为这封邮件已经被处理过了。

反过来，已导入且内容未变才跳过；**解析失败（rejected/conflict）的记录会落到第 3 步
重新尝试**——那是修复后重试的路径，不能因为"库里有记录"就跳过。

``ledger_account`` 用的是 Schwab 的**完整账户键**而不是 UUID：交易哈希要跨来源一致
（邮件导入与 CSV 导入必须算出同一个哈希），而账户键进入哈希输入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ...application.app_error import ApplicationError
from ...application.import_commit import (
    ImportCommitPlan,
    ImportCommitPreview,
    build_import_commit_plan,
)
from ...application.import_preview import (
    CreateImportPreviewInput,
    create_import_preview,
)
from ...domain.gmail.econfirm import EmailTrade, GmailMessage, email_trade_to_raw_fields
from ...domain.ledger.batch import TransactionBatchInput
from ...domain.ledger.types import ParsedTransaction
from .message_metadata import (
    ParsedGmailMessageMetadata,
    create_parsed_gmail_message_metadata,
)

ExistingParseStatus = Literal["discovered", "parsed", "imported", "rejected", "conflict"]


@dataclass(frozen=True)
class ExistingGmailMessageState:
    """库中已有的同一封邮件的状态。"""

    content_hash: str
    account_id: str | None
    parse_status: ExistingParseStatus


@dataclass(frozen=True)
class GmailMessageTransactionLink:
    """消息与账本交易的关联。"""

    connection_id: str
    message_id: str
    account_id: str
    transaction_hash: str
    # 从 1 开始：0 会让"第一条"与"未设置"难以区分。
    ordinal: int


@dataclass(frozen=True)
class AlreadyImportedGmailMessagePlan:
    """该消息已经导入且内容未变。"""

    kind: Literal["already_imported"]
    connection_id: str
    message_id: str
    content_hash: str


@dataclass(frozen=True)
class ReadyGmailMessageImportPlan:
    """该消息可以导入。"""

    kind: Literal["ready"]
    metadata: ParsedGmailMessageMetadata
    preview_fingerprint: str
    commit: ImportCommitPlan
    links: tuple[GmailMessageTransactionLink, ...]


GmailMessageImportPlan = AlreadyImportedGmailMessagePlan | ReadyGmailMessageImportPlan


def build_gmail_message_import_plan(
    *,
    connection_id: str,
    thread_id: str | None,
    message: GmailMessage,
    trades: tuple[EmailTrade, ...],
    account_id: str,
    ledger_account: str,
    history_id: str | None,
    synced_at: datetime,
    parser_version: str,
    base_ledger_version: int,
    existing_message: ExistingGmailMessageState | None,
    existing_account_transactions: tuple[ParsedTransaction, ...],
) -> GmailMessageImportPlan:
    """为一条 Gmail 消息建立导入计划。

    @param connection_id: 连接 ID。
    @param thread_id: 会话 ID。
    @param message: 已解析的邮件。
    @param trades: 提取到的成交。
    @param account_id: 映射到的账户 UUID。
    @param ledger_account: Schwab 完整账户键，用于跨来源一致的交易哈希。
    @param history_id: 同步游标。
    @param synced_at: 服务端可信时间。
    @param parser_version: 解析规则版本。
    @param base_ledger_version: 当前账本版本。
    @param existing_message: 库中已有状态；首次导入为 ``None``。
    @param existing_account_transactions: 目标账户的现有交易快照。
    @returns 跳过或就绪的计划。
    @raises ApplicationError: 同一消息的内容或账户归属发生变化（409）。
    """
    metadata = create_parsed_gmail_message_metadata(
        connection_id=connection_id, thread_id=thread_id, message=message,
        trade_count=len(trades), account_id=account_id, history_id=history_id,
        synced_at=synced_at,
    )

    if existing_message is not None:
        content_changed = existing_message.content_hash != metadata.content_hash
        account_changed = existing_message.account_id != metadata.account_id
        if content_changed or account_changed:
            # **先报冲突**：先报"已导入"会让变更被静默忽略。
            raise ApplicationError(
                code="GMAIL_MESSAGE_CONFLICT",
                message="同一 Gmail 消息的内容或账户归属已发生变化",
                status_code=409,
                retryable=False,
                details={
                    "message_id": metadata.message_id,
                    "changed_fields": [
                        *(["content_hash"] if content_changed else []),
                        *(["account_id"] if account_changed else []),
                    ],
                },
            )
        if existing_message.parse_status == "imported":
            return AlreadyImportedGmailMessagePlan(
                kind="already_imported",
                connection_id=metadata.connection_id,
                message_id=metadata.message_id,
                content_hash=metadata.content_hash,
            )
        # 其余状态（rejected / conflict / parsed / discovered）落到下面重新尝试——
        # 那是修复后重试的路径。

    existing_hashes = frozenset(
        transaction.transaction_hash for transaction in existing_account_transactions
    )
    preview = create_import_preview(CreateImportPreviewInput(
        batch=TransactionBatchInput(
            file_name=f"gmail:{metadata.message_id}",
            account=ledger_account,
            source_hash=metadata.content_hash,
            source_type="gmail",
            source_reference=metadata.message_id,
            rows=tuple(email_trade_to_raw_fields(trade) for trade in trades),  # type: ignore[arg-type]
        ),
        parser_version=parser_version,
        base_ledger_version=base_ledger_version,
        existing_transaction_hashes=existing_hashes,
    ))
    commit = build_import_commit_plan(
        ImportCommitPreview(account=preview.account, transactions=preview.transactions),
        existing_account_transactions,
    )
    return ReadyGmailMessageImportPlan(
        kind="ready",
        metadata=_as_imported(metadata),
        preview_fingerprint=preview.preview_fingerprint,
        commit=commit,
        links=tuple(
            GmailMessageTransactionLink(
                connection_id=metadata.connection_id,
                message_id=metadata.message_id,
                account_id=account_id,
                transaction_hash=transaction.transaction_hash,
                ordinal=index + 1,
            )
            for index, transaction in enumerate(preview.transactions)
        ),
    )


def _as_imported(metadata: ParsedGmailMessageMetadata) -> ParsedGmailMessageMetadata:
    """把元数据标记为已导入。"""
    from dataclasses import replace

    return replace(metadata, parse_status="imported")


def plan_summary(plan: GmailMessageImportPlan) -> dict[str, Any]:
    """为日志与诊断给出计划的简要描述。

    **不含正文或成交明细**：诊断输出常被转发到日志聚合，明细属于账本数据。
    """
    if plan.kind == "already_imported":
        return {
            "kind": plan.kind, "messageId": plan.message_id,
            "contentHash": plan.content_hash,
        }
    return {
        "kind": plan.kind,
        "messageId": plan.metadata.message_id,
        "previewFingerprint": plan.preview_fingerprint,
        "linkCount": len(plan.links),
        "insertedRows": plan.commit.inserted_rows,
        "skippedRows": plan.commit.skipped_rows,
    }
